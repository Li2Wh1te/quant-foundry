"""Persistence and queue coordination for backtest run roots.

The database repository is intentionally small and explicit.  A queue guard
row is locked before counting ``queued`` roots, so separate API processes share
the same serialization point without depending on an in-process mutex.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import backtest_event_message
from .pagination import (
    CursorPage,
    build_cursor,
    compute_query_digest,
    encode_sort_element,
    hmac_compare,
    normalize_limit,
    parse_cursor,
    CursorQueryMismatchError,
)
from .supervisor_lock import assert_supervisor_lock_held

from .models import BacktestQueueGuardRecord, BacktestRunRecord
from app.strategies.models import StrategyRevision
from .run_binding import BacktestRun, IdempotencyKeyReusedError, QueueFullError
from .run_execution import InvalidRunTransition, RunStateMachine


FORMAL_KIND = "backtest_run"
INTERNAL_KIND = "internal_link_acceptance"
RUN_KINDS = frozenset({FORMAL_KIND, INTERNAL_KIND})
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "indeterminate"}
)
NON_TERMINAL_STATUSES = frozenset(
    {"queued", "starting", "running", "cancel_requested"}
)


class QueueSchemaError(RuntimeError):
    """The migration did not install a required queue guard row/table."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_value(value: Any) -> Any:
    """Convert frozen mapping/date/UUID values into JSONB-safe primitives."""

    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (UUID, date, datetime)):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return _json_value(value.value)
    return value


def _raise_queue_full(
    *,
    queue_kind: str,
    queued_count: int,
    queue_limit: int | None,
    disabled: bool = False,
) -> QueueFullError:
    """Build a structured queue error while retaining the legacy exception type."""

    message = (
        "内部验收等待队列未启用"
        if disabled
        else (
            "内部链路验收等待队列已满，请稍后重试"
            if queue_kind == INTERNAL_KIND
            else "回测等待队列已满，请稍后重试"
        )
    )
    error = QueueFullError(message)
    # These attributes are deliberately attached to the established exception
    # class so older callers that only catch QueueFullError remain compatible.
    error.queue_kind = queue_kind  # type: ignore[attr-defined]
    error.queued_count = queued_count  # type: ignore[attr-defined]
    error.queue_limit = queue_limit  # type: ignore[attr-defined]
    error.code = "backtest_queue_full"  # type: ignore[attr-defined]
    error.disabled = disabled  # type: ignore[attr-defined]
    return error


class RunRepository:
    """Small in-memory adapter retained for pure state-machine tests.

    Production API code uses :class:`DatabaseRunRepository`; this adapter has
    no capacity authority and must not be used for persisted requests.
    """

    def __init__(self):
        self._rows: dict[UUID, BacktestRun] = {}
        self._states = RunStateMachine()
        self._queue_state: dict[UUID, dict[str, str]] = {}
        self._cancel_evidence: dict[UUID, dict[str, Any]] = {}
        self.last_message = ""

    def add(self, run: BacktestRun) -> BacktestRun:
        self._rows[run.run_id] = run
        return run

    def get(self, run_id: UUID | str) -> BacktestRun | None:
        return self._rows.get(UUID(str(run_id)))

    def transition(self, run_id: UUID | str, target: str) -> BacktestRun:
        key = UUID(str(run_id))
        row = self._rows[key]
        if target in TERMINAL_STATUSES or target == "terminal":
            raise PermissionError("terminal status is owned by the locked Supervisor")
        self._states.transition(row.status, target)
        row = replace(row, status=target)
        self._rows[key] = row
        self.last_message = backtest_event_message(
            "回测状态变更", str(key), f"状态为 {target}"
        )
        return row

    def request_cancel(self, run_id: UUID | str) -> BacktestRun:
        key = UUID(str(run_id))
        row = self._rows[key]
        if row.status in TERMINAL_STATUSES or row.status == "terminal":
            return row
        return self.transition(key, "cancel_requested")

    def mark_queued(self, run_id: UUID | str, kind: str) -> None:
        self._queue_state[UUID(str(run_id))] = {"kind": kind, "state": "queued"}

    def mark_claimed(self, run_id: UUID | str) -> None:
        key = UUID(str(run_id))
        if key in self._queue_state:
            self._queue_state[key]["state"] = "claimed"

    def record_cancel_evidence(
        self,
        run_id: UUID | str,
        *,
        grace_seconds: int,
        forced: bool = False,
        terminated_at: datetime | None = None,
    ) -> dict[str, Any]:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        self._cancel_evidence[UUID(str(run_id))] = {
            "grace_seconds": grace_seconds,
            "forced": bool(forced),
            "terminated_at": terminated_at,
        }
        return self._cancel_evidence[UUID(str(run_id))]

    def cancel_evidence(self, run_id: UUID | str) -> dict[str, Any] | None:
        return self._cancel_evidence.get(UUID(str(run_id)))

    def adjudicate(
        self,
        run_id: UUID | str,
        *,
        marker: Mapping[str, Any] | None,
        exit_code: int | None,
        expected_count: int | None = None,
    ) -> str:
        """Reject the historical repository's terminal-write bypass.

        This adapter predates the dedicated ``RunnerSupervisor`` and is kept
        only for non-persistent state-machine tests.  It may expose lifecycle
        reads and non-terminal transitions, but it must never turn a pure
        evidence decision into a terminal root mutation.
        """

        raise PermissionError("terminal status is owned by the locked Supervisor")

    def recover(self) -> tuple[BacktestRun, ...]:
        """Return non-terminal roots for supervisor scan; never auto-requeues."""

        return tuple(
            row
            for row in self._rows.values()
            if row.status not in TERMINAL_STATUSES and row.status != "terminal"
        )

    def recoverable(self) -> tuple[dict[str, Any], ...]:
        """Return roots with evidence without changing their lifecycle state."""

        rows = tuple(
            {
                "run_id": row.run_id,
                "status": row.status,
                "cancel_evidence": self.cancel_evidence(row.run_id),
            }
            for row in self.recover()
        )
        self.last_message = backtest_event_message(
            "回测恢复扫描",
            f"非终态运行 {len(rows)} 个",
            "扫描完成，未自动重排队",
        )
        return rows


class DatabaseRunRepository:
    """Repository for durable roots, queue guards, and guarded transitions."""

    def __init__(
        self,
        session: Session,
        *,
        formal_limit: int = 32,
        internal_limit: int | None = None,
    ) -> None:
        if not isinstance(formal_limit, int) or isinstance(formal_limit, bool):
            raise ValueError("formal queue limit must be an integer")
        if formal_limit < 1 or formal_limit > 32:
            raise ValueError("formal queue limit must be between 1 and 32")
        if internal_limit is not None and (
            not isinstance(internal_limit, int)
            or isinstance(internal_limit, bool)
            or internal_limit < 1
            or internal_limit >= formal_limit
            or internal_limit >= 32
        ):
            raise ValueError("internal queue limit must be smaller than formal limit")
        self.session = session
        self.limits = {
            FORMAL_KIND: formal_limit,
            INTERNAL_KIND: internal_limit,
        }

    def _validate_kind(self, kind: str) -> None:
        if kind not in RUN_KINDS:
            raise ValueError(f"unsupported backtest run kind: {kind}")

    def get(
        self,
        run_id: UUID | str,
        *,
        expected_kind: str | None = None,
        owner_scope: str | None = None,
        for_update: bool = False,
    ) -> BacktestRunRecord | None:
        """Load one root with an optional authoritative kind guard."""

        key = UUID(str(run_id))
        statement = select(BacktestRunRecord).where(BacktestRunRecord.id == key)
        if expected_kind is not None:
            self._validate_kind(expected_kind)
            statement = statement.where(BacktestRunRecord.run_kind == expected_kind)
        if owner_scope is not None:
            statement = statement.where(
                BacktestRunRecord.idempotency_scope == owner_scope
            )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_by_idempotency(
        self,
        scope: str,
        key: str,
        *,
        for_update: bool = False,
    ) -> BacktestRunRecord | None:
        """Find by scope/key before any queue-capacity lock is taken."""

        if not scope or not key:
            raise ValueError("idempotency scope and key are required")
        statement = select(BacktestRunRecord).where(
            BacktestRunRecord.idempotency_scope == scope,
            BacktestRunRecord.idempotency_key == key,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    # Explicit aliases keep the repository vocabulary discoverable to callers
    # that use the task-package terms rather than the older creation-only name.
    find_by_idempotency = get_by_idempotency

    def queued_count(self, queue_kind: str) -> int:
        """Count only queued roots for one logical queue."""

        self._validate_kind(queue_kind)
        return int(
            self.session.scalar(
                select(func.count(BacktestRunRecord.id)).where(
                    BacktestRunRecord.run_kind == queue_kind,
                    BacktestRunRecord.status == "queued",
                )
            )
            or 0
        )

    count_queued = queued_count

    def _ensure_guard_row(self, queue_kind: str) -> BacktestQueueGuardRecord:
        """Ensure test-created metadata has the same permanent guard rows as a migration."""

        guard = self.session.scalar(
            select(BacktestQueueGuardRecord).where(
                BacktestQueueGuardRecord.queue_kind == queue_kind
            )
        )
        if guard is not None:
            return guard
        guard = BacktestQueueGuardRecord(queue_kind=queue_kind)
        try:
            # Keep a competing seed insert inside a savepoint.  Rolling back
            # the caller's transaction here could discard an otherwise valid
            # admission decision before the queue lock is acquired.
            with self.session.begin_nested():
                self.session.add(guard)
                self.session.flush()
        except IntegrityError:
            # Another transaction may have seeded the permanent row first.
            guard = self.session.scalar(
                select(BacktestQueueGuardRecord).where(
                    BacktestQueueGuardRecord.queue_kind == queue_kind
                )
            )
            if guard is None:
                raise QueueSchemaError(
                    f"queue guard row missing for {queue_kind}"
                )
        return guard

    def lock_queue_guard(self, queue_kind: str) -> BacktestQueueGuardRecord:
        """Lock the permanent guard row used by a capacity transaction."""

        self._validate_kind(queue_kind)
        self._ensure_guard_row(queue_kind)
        guard = self.session.scalar(
            select(BacktestQueueGuardRecord)
            .where(BacktestQueueGuardRecord.queue_kind == queue_kind)
            .with_for_update()
        )
        if guard is None:
            raise QueueSchemaError(f"queue guard row missing for {queue_kind}")
        return guard

    def _capacity_error(self, queue_kind: str, queued: int) -> QueueFullError:
        limit = self.limits[queue_kind]
        return _raise_queue_full(
            queue_kind=queue_kind,
            queued_count=queued,
            queue_limit=limit,
            disabled=limit is None,
        )

    def create(
        self,
        binding,
        *,
        tenant_id: str = "default",
        idempotency_key: str,
        idempotency_scope: str | None = None,
        idempotency_request_hash: str | None = None,
    ) -> BacktestRunRecord:
        """Create one queued root with idempotency-before-capacity ordering.

        The first lookup is intentionally outside the guard lock: a retry for
        an existing key must return the original root even when the queue is
        full.  A second lookup after locking closes the concurrent-create race.
        """

        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        scope = idempotency_scope or tenant_id
        kind = str(binding.run_kind)
        self._validate_kind(kind)

        existing = self.get_by_idempotency(scope, idempotency_key)
        if existing is not None:
            if (
                existing.config_hash != binding.config_hash
                or (
                    idempotency_request_hash is not None
                    and getattr(existing, "idempotency_request_hash", None)
                    not in (None, idempotency_request_hash)
                )
            ):
                raise IdempotencyKeyReusedError(
                    "idempotency key already used with different request"
                )
            return existing

        limit = self.limits[kind]
        if limit is None:
            raise self._capacity_error(kind, 0)

        # The guard row is the only serialization point for this logical
        # queue.  API processes do not share Python state or an app mutex.
        self.lock_queue_guard(kind)
        existing = self.get_by_idempotency(scope, idempotency_key)
        if existing is not None:
            if (
                existing.config_hash != binding.config_hash
                or (
                    idempotency_request_hash is not None
                    and getattr(existing, "idempotency_request_hash", None)
                    not in (None, idempotency_request_hash)
                )
            ):
                raise IdempotencyKeyReusedError(
                    "idempotency key already used with different request"
                )
            return existing

        queued = self.queued_count(kind)
        if queued >= limit:
            raise self._capacity_error(kind, queued)

        spec = binding.spec
        account = binding.account if isinstance(binding.account, Mapping) else {}
        fee_schedule = account.get("fee_schedule", {})
        if not isinstance(fee_schedule, Mapping):
            fee_schedule = {}
        strategy = binding.strategy if isinstance(binding.strategy, Mapping) else {}
        metadata = binding.metadata if isinstance(binding.metadata, Mapping) else {}
        data_request = (
            binding.data_request if isinstance(binding.data_request, Mapping) else {}
        )
        chunk_policy = data_request.get("data_chunk_policy", {})
        if not isinstance(chunk_policy, Mapping):
            chunk_policy = {}
        query_boundary = data_request.get("query_boundary", {})
        if not isinstance(query_boundary, Mapping):
            query_boundary = {}
        pit_cutoff_at = query_boundary.get("data_cutoff")
        if isinstance(pit_cutoff_at, str):
            try:
                pit_cutoff_at = datetime.fromisoformat(pit_cutoff_at)
            except ValueError:
                raise ValueError("frozen data request has an invalid data_cutoff")
        positions = [
            {
                "instrument_id": str(item.instrument_id),
                "side": item.side.value,
                "quantity": str(item.quantity),
                "available_quantity": str(item.available_quantity),
                "average_price": (
                    None if item.average_price is None else str(item.average_price)
                ),
            }
            for item in spec.initial_positions
        ]
        row = BacktestRunRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            idempotency_scope=scope,
            idempotency_key=idempotency_key,
            idempotency_request_hash=idempotency_request_hash,
            run_kind=kind,
            profile=binding.profile,
            status="queued",
            config_hash=binding.config_hash,
            backtest_config=_json_value(binding.config),
            strategy_revision_id=(
                str(strategy["revision_id"]) if strategy.get("revision_id") else None
            ),
            strategy_source_hash=(
                str(strategy["source_hash"]) if strategy.get("source_hash") else None
            ),
            strategy_contract_version=(
                str(strategy["contract_version"])
                if strategy.get("contract_version")
                else None
            ),
            parameters=_json_value(strategy.get("parameters", {})),
            initial_cash=str(spec.initial_cash),
            initial_positions=positions,
            data_request=_json_value(data_request),
            data_evidence=_json_value(metadata.get("data_evidence", {})),
            pit_snapshot_hash=metadata.get("pit_snapshot_hash"),
            pit_cutoff_at=pit_cutoff_at,
            data_provider_key=data_request.get("provider_key"),
            max_lookback_sessions=int(data_request.get("max_lookback_sessions", 512)),
            data_chunk_policy_key=str(
                chunk_policy.get("key", "fixed_trading_sessions")
            ),
            data_chunk_policy_version=int(chunk_policy.get("version", 1)),
            data_chunk_size_sessions=int(
                data_request.get("data_chunk_size_sessions", 20)
            ),
            data_admission_preflight_hash=metadata.get("admission_report_hash"),
            data_preflight_hash=metadata.get("preflight_hash"),
            account_profile_id=(
                str(account.get("profile_id", account.get("account_profile_id")))
                if account.get("profile_id", account.get("account_profile_id"))
                else None
            ),
            account_profile_version=(
                str(account.get("version", account.get("profile_version")))
                if account.get("version", account.get("profile_version"))
                else None
            ),
            fee_schedule_key=(
                str(account.get("fee_schedule_key", fee_schedule.get("key")))
                if account.get("fee_schedule_key", fee_schedule.get("key"))
                else None
            ),
            fee_schedule_version=(
                str(account.get("fee_schedule_version", fee_schedule.get("version")))
                if account.get("fee_schedule_version", fee_schedule.get("version"))
                else None
            ),
            fee_schedule_snapshot=_json_value(fee_schedule),
            random_seed=binding.random_seed,
            analyzer_specs=(
                _json_value(binding.components.get("analyzer", ()))
                if isinstance(binding.components, Mapping)
                else []
            ),
            behavior_versions=_json_value(metadata.get("behavior_versions", {})),
        )
        # Use a savepoint so a concurrent unique-key loser can recover and
        # return the already committed row without poisoning the caller's
        # transaction.  The guard lock remains held by the outer transaction.
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            existing = self.get_by_idempotency(scope, idempotency_key)
            if existing is not None:
                if (
                    existing.config_hash != binding.config_hash
                    or (
                        idempotency_request_hash is not None
                        and getattr(existing, "idempotency_request_hash", None)
                        not in (None, idempotency_request_hash)
                    )
                ):
                    raise IdempotencyKeyReusedError(
                        "idempotency key already used with different request"
                    )
                return existing
            raise
        return row

    def create_rerun(
        self,
        original_run_id: UUID | str,
        *,
        owner_scope: str,
        idempotency_key: str,
    ) -> BacktestRunRecord | None:
        """Create a queued formal run from an owner-visible frozen root.

        Reruns copy persisted inputs rather than rebuilding a binding from
        mutable strategy/account defaults.  The original row remains
        untouched and is linked from the new root for auditability.
        """

        if not owner_scope or not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("owner_scope and idempotency_key are required")
        original = self.get(
            original_run_id,
            expected_kind=FORMAL_KIND,
            owner_scope=owner_scope,
        )
        if original is None:
            return None

        existing = self.get_by_idempotency(owner_scope, idempotency_key)
        if existing is not None:
            if (
                existing.config_hash != original.config_hash
                or existing.rerun_of_run_id != original.id
            ):
                raise IdempotencyKeyReusedError(
                    "idempotency key already used for a different rerun"
                )
            return existing

        self.lock_queue_guard(FORMAL_KIND)
        original = self.get(
            original.id,
            expected_kind=FORMAL_KIND,
            owner_scope=owner_scope,
            for_update=True,
        )
        if original is None:
            return None
        existing = self.get_by_idempotency(owner_scope, idempotency_key)
        if existing is not None:
            if (
                existing.config_hash != original.config_hash
                or existing.rerun_of_run_id != original.id
            ):
                raise IdempotencyKeyReusedError(
                    "idempotency key already used for a different rerun"
                )
            return existing
        queued = self.queued_count(FORMAL_KIND)
        limit = self.limits[FORMAL_KIND]
        if limit is None or queued >= limit:
            raise self._capacity_error(FORMAL_KIND, queued)

        row = BacktestRunRecord(
            id=uuid4(),
            tenant_id=owner_scope,
            idempotency_scope=owner_scope,
            idempotency_key=idempotency_key,
            rerun_of_run_id=original.id,
            run_kind=original.run_kind,
            profile=original.profile,
            status="queued",
            config_hash=original.config_hash,
            backtest_config=_json_value(original.backtest_config),
            strategy_revision_id=original.strategy_revision_id,
            strategy_source_hash=original.strategy_source_hash,
            strategy_contract_version=original.strategy_contract_version,
            parameters=_json_value(original.parameters),
            initial_cash=original.initial_cash,
            initial_positions=_json_value(original.initial_positions),
            data_request=_json_value(original.data_request),
            data_evidence=_json_value(original.data_evidence),
            pit_snapshot_hash=original.pit_snapshot_hash,
            pit_cutoff_at=original.pit_cutoff_at,
            data_provider_key=original.data_provider_key,
            max_lookback_sessions=original.max_lookback_sessions,
            data_chunk_policy_key=original.data_chunk_policy_key,
            data_chunk_policy_version=original.data_chunk_policy_version,
            data_chunk_size_sessions=original.data_chunk_size_sessions,
            data_admission_preflight_hash=original.data_admission_preflight_hash,
            data_preflight_hash=original.data_preflight_hash,
            account_profile_id=original.account_profile_id,
            account_profile_version=original.account_profile_version,
            fee_schedule_key=original.fee_schedule_key,
            fee_schedule_version=original.fee_schedule_version,
            fee_schedule_snapshot=_json_value(original.fee_schedule_snapshot),
            analyzer_specs=_json_value(original.analyzer_specs),
            behavior_versions=_json_value(original.behavior_versions),
            random_seed=original.random_seed,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            existing = self.get_by_idempotency(owner_scope, idempotency_key)
            if existing is not None:
                if (
                    existing.config_hash != original.config_hash
                    or existing.rerun_of_run_id != original.id
                ):
                    raise IdempotencyKeyReusedError(
                        "idempotency key already used for a different rerun"
                    )
                return existing
            raise
        return row

    def list(
        self,
        *,
        queue_kind: str = FORMAL_KIND,
        tenant_id: str | None = None,
        owner_scope: str | None = None,
        strategy_revision_id: str | None = None,
        strategy_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[BacktestRunRecord]:
        """List one logical queue; formal callers never see internal roots."""

        self._validate_kind(queue_kind)
        if limit < 1 or limit > 500 or offset < 0:
            raise ValueError("limit must be between 1 and 500 and offset non-negative")
        statement = select(BacktestRunRecord).where(
            BacktestRunRecord.run_kind == queue_kind
        )
        visible_scope = owner_scope if owner_scope is not None else tenant_id
        if visible_scope is not None:
            statement = statement.where(
                BacktestRunRecord.idempotency_scope == visible_scope
            )
        if strategy_revision_id is not None:
            statement = statement.where(
                BacktestRunRecord.strategy_revision_id == strategy_revision_id
            )
        if strategy_id is not None:
            statement = statement.join(
                StrategyRevision,
                StrategyRevision.id == BacktestRunRecord.strategy_revision_id,
            ).where(StrategyRevision.strategy_id == strategy_id)
        statement = (
            statement.order_by(BacktestRunRecord.created_at, BacktestRunRecord.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def list_page(
        self,
        *,
        signing_key: str,
        queue_kind: str = FORMAL_KIND,
        tenant_id: str | None = None,
        owner_scope: str | None = None,
        strategy_revision_id: str | None = None,
        strategy_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> CursorPage:
        """List durable run roots with the shared signed opaque-cursor contract.

        The legacy collection endpoints still expose offset pagination for
        compatibility.  Canonical scoped consumers use this method so the
        cursor is bound to the owner/filter/page-size query and to the maximum
        row visible when the walk starts.
        """

        self._validate_kind(queue_kind)
        checked_limit = normalize_limit(limit)
        statement = select(BacktestRunRecord).where(
            BacktestRunRecord.run_kind == queue_kind
        )
        visible_scope = owner_scope if owner_scope is not None else tenant_id
        if visible_scope is not None:
            statement = statement.where(
                BacktestRunRecord.idempotency_scope == visible_scope
            )
        if strategy_revision_id is not None:
            statement = statement.where(
                BacktestRunRecord.strategy_revision_id == strategy_revision_id
            )
        if strategy_id is not None:
            statement = statement.join(
                StrategyRevision,
                StrategyRevision.id == BacktestRunRecord.strategy_revision_id,
            ).where(StrategyRevision.strategy_id == strategy_id)

        query_payload = {
            "resource": "backtest_runs",
            "run_kind": queue_kind,
            "tenant_id": visible_scope,
            "strategy_revision_id": strategy_revision_id,
            "strategy_id": strategy_id,
            "limit": checked_limit,
            "sort": ["created_at", "id"],
            "direction": "asc",
        }
        key_kinds = ("ts", "uuid")
        upper_bound_columns = {"created_at": "ts", "id": "uuid"}

        if cursor is None:
            upper_row = self.session.execute(
                statement.with_only_columns(
                    BacktestRunRecord.created_at, BacktestRunRecord.id
                )
                .order_by(
                    BacktestRunRecord.created_at.desc(), BacktestRunRecord.id.desc()
                )
                .limit(1)
            ).first()
            if upper_row is None:
                return CursorPage(items=(), next_cursor=None, has_more=False)
            upper_bound = {
                "created_at": upper_row[0],
                "id": upper_row[1],
            }
            expected_digest = compute_query_digest(
                {
                    **query_payload,
                    "query_upper_bound": {
                        name: encode_sort_element(kind, upper_bound[name])
                        for name, kind in upper_bound_columns.items()
                    },
                }
            )
        else:
            parsed = parse_cursor(
                cursor,
                signing_key=signing_key,
                key_kinds=key_kinds,
                upper_bound_columns=upper_bound_columns,
            )
            upper_bound = dict(parsed.query_upper_bound)
            expected_digest = compute_query_digest(
                {
                    **query_payload,
                    "query_upper_bound": {
                        name: encode_sort_element(kind, upper_bound[name])
                        for name, kind in upper_bound_columns.items()
                    },
                }
            )
            if not hmac_compare(parsed.query_digest, expected_digest):
                raise CursorQueryMismatchError(
                    "cursor belongs to a different query; restart from the first page"
                )
            last_created_at, last_id = parsed.last_sort_key
            statement = statement.where(
                or_(
                    BacktestRunRecord.created_at > last_created_at,
                    and_(
                        BacktestRunRecord.created_at == last_created_at,
                        BacktestRunRecord.id > last_id,
                    ),
                )
            )

        statement = statement.where(
            or_(
                BacktestRunRecord.created_at < upper_bound["created_at"],
                and_(
                    BacktestRunRecord.created_at == upper_bound["created_at"],
                    BacktestRunRecord.id <= upper_bound["id"],
                ),
            )
        ).order_by(BacktestRunRecord.created_at, BacktestRunRecord.id)
        rows = list(self.session.scalars(statement.limit(checked_limit + 1)))
        has_more = len(rows) > checked_limit
        items = tuple(rows[:checked_limit])
        next_cursor = None
        if has_more:
            last = items[-1]
            next_cursor = build_cursor(
                signing_key=signing_key,
                query_digest=expected_digest,
                key_kinds=key_kinds,
                last_sort_key=(last.created_at, last.id),
                upper_bound_columns=upper_bound_columns,
                query_upper_bound=upper_bound,
            )
        return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    def transition(
        self,
        run_id: UUID | str,
        target: str,
        *,
        expected_status: str | None = None,
        now: datetime | None = None,
    ) -> BacktestRunRecord | None:
        """Apply a restricted lifecycle transition under a row lock."""

        row = self.get(run_id, for_update=True)
        if row is None:
            return None
        current = row.status
        if expected_status is not None and current != expected_status:
            return None
        if target in TERMINAL_STATUSES or target == "terminal":
            # The generic repository transition API is intentionally limited
            # to live-state changes.  A terminal transition must carry the
            # complete evidence bundle and the Supervisor lock through the
            # dedicated conditional-CAS writer below.
            raise PermissionError("terminal status is owned by the locked Supervisor")
        else:
            RunStateMachine().transition(current, target)
            row.status = target
        self.session.flush()
        return row

    def claim_next(
        self,
        *,
        worker_id: str | None = None,
        launch_id: UUID | None = None,
    ) -> BacktestRunRecord | None:
        """Claim one formal-first row using ``FOR UPDATE SKIP LOCKED``."""

        row = self._claim_kind(FORMAL_KIND)
        if row is None:
            row = self._claim_kind(INTERNAL_KIND)
        if row is None:
            return None
        claim_time = _utcnow()
        effective_launch_id = launch_id or uuid4()
        # Keep the lifecycle predicate on the UPDATE as well as the SELECT;
        # this remains safe if a caller changes the transaction strategy or a
        # future implementation has more than one claimant.
        claimed = self.session.execute(
            update(BacktestRunRecord)
            .where(
                BacktestRunRecord.id == row.id,
                BacktestRunRecord.status == "queued",
                # A cancellation request is an explicit queue-side decision.
                # Keep it out of the claim CAS as well as the SELECT so a
                # request that wins the row-lock race cannot receive a
                # launch identity and later be mislabeled as
                # ``cancelled_before_start`` after claiming.
                BacktestRunRecord.cancel_requested.is_(False),
            )
            .values(
                status="starting",
                claimed_at=claim_time,
                launch_id=effective_launch_id,
                worker_id=worker_id,
            )
        )
        if claimed.rowcount != 1:
            return None
        self.session.flush()
        self.session.refresh(row)
        return row

    def _claim_kind(self, kind: str) -> BacktestRunRecord | None:
        statement = (
            select(BacktestRunRecord)
            .where(
                BacktestRunRecord.run_kind == kind,
                BacktestRunRecord.status == "queued",
                # ``cancel_requested`` is intentionally separate from the
                # lifecycle status for queued rows.  The Supervisor closes
                # such rows through its queued-cancellation path; a normal
                # worker claim must never assign them a launch identity.
                BacktestRunRecord.cancel_requested.is_(False),
            )
            .order_by(BacktestRunRecord.created_at, BacktestRunRecord.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return self.session.scalar(statement)

    def claim_queued_runs(
        self,
        slots: int,
        *,
        worker_id_prefix: str = "backtest-worker",
    ) -> list[BacktestRunRecord]:
        """Claim at most ``slots`` rows while retaining formal priority."""

        if slots < 0:
            raise ValueError("slots must be non-negative")
        rows: list[BacktestRunRecord] = []
        for index in range(slots):
            row = self.claim_next(worker_id=f"{worker_id_prefix}:{index}")
            if row is None:
                break
            rows.append(row)
        return rows

    claim = claim_next

    def request_cancel(
        self,
        run_id: UUID | str,
        *,
        expected_kind: str | None = None,
        owner_scope: str | None = None,
        now: datetime | None = None,
    ) -> BacktestRunRecord | None:
        """Record a cancellation request, never a terminal decision."""

        row = self.get(
            run_id,
            expected_kind=expected_kind,
            owner_scope=owner_scope,
            for_update=True,
        )
        if row is None:
            return None
        if row.status in TERMINAL_STATUSES:
            return row
        if row.status not in NON_TERMINAL_STATUSES:
            raise InvalidRunTransition(f"cannot cancel run in state {row.status}")
        timestamp = row.cancel_requested_at or now or _utcnow()
        row.cancel_requested_at = timestamp
        row.cancel_requested = True
        # A queued cancellation retains ``status=queued`` plus the explicit
        # request flag.  The Supervisor scans and closes that row before a
        # normal claim; the claim CAS also excludes the flag as a race guard.
        # Active runs enter the explicit cancel_requested state.
        if row.status in {"starting", "running"}:
            row.status = "cancel_requested"
        self.session.flush()
        return row

    def cancel_queued_before_start(
        self,
        run_id: UUID | str,
        *,
        supervisor_lock: Any = None,
        reason: str = "cancelled_before_start",
        now: datetime | None = None,
    ) -> BacktestRunRecord | None:
        """Supervisor-only closure for a queued cancellation request."""

        assert_supervisor_lock_held(supervisor_lock)
        changed, row = self._set_terminal_cas(
            run_id,
            terminal_status="cancelled",
            terminal_decision_reason=reason,
            now=now,
            supervisor_lock=supervisor_lock,
            allow_queued_cancel=True,
        )
        return row

    def record_progress(
        self,
        run_id: UUID | str,
        progress: float,
        *,
        launch_id: UUID | str | None = None,
        current_trading_date: date | None = None,
        current_step: str | None = None,
        checkpoint: Mapping[str, Any] | None = None,
        heartbeat_at: datetime | None = None,
        persisted_at: datetime | None = None,
    ) -> BacktestRunRecord | None:
        """Persist monotonic progress/heartbeat evidence for a live run."""

        if not 0 <= float(progress) <= 1:
            raise ValueError("progress must be in [0, 1]")
        if launch_id is None:
            # A run identity without its launch identity is not sufficient to
            # distinguish a late worker from the currently active worker.
            # Durable progress and heartbeat writes therefore fail closed.
            raise ValueError("launch_id is required for progress persistence")
        row = self.get(run_id, for_update=True)
        if row is None:
            return None
        if row.launch_id is None or str(row.launch_id) != str(launch_id):
            # A previous worker must never update the run after a new launch
            # has acquired the same run root.  Returning ``False`` lets the
            # progress reporter distinguish an identity miss from a commit.
            return False
        if row.status not in {"starting", "running", "cancel_requested"}:
            raise InvalidRunTransition("terminal run cannot receive progress")
        if float(progress) < float(row.progress or 0):
            raise ValueError("progress cannot move backwards")
        timestamp = heartbeat_at or _utcnow()
        # Keep the launch/status/monotonic predicates in the database write.
        # The row lock above serializes normal callers, while this CAS also
        # protects adapters that refresh a stale ORM object between the read
        # and the flush.
        values: dict[str, Any] = {
            "progress": progress,
            "last_heartbeat_at": timestamp,
            "last_progress_persisted_at": persisted_at or timestamp,
        }
        if current_trading_date is not None:
            values["current_trading_date"] = current_trading_date
            values["current_date"] = current_trading_date.isoformat()
        if current_step is not None:
            values["current_step"] = str(current_step)
        if checkpoint is not None:
            values["checkpoint"] = _json_value(checkpoint)
        changed = self.session.execute(
            update(BacktestRunRecord)
            .where(
                BacktestRunRecord.id == UUID(str(run_id)),
                BacktestRunRecord.launch_id == UUID(str(launch_id)),
                BacktestRunRecord.status.in_(
                    ("starting", "running", "cancel_requested")
                ),
                BacktestRunRecord.progress <= progress,
            )
            .values(**values)
        ).rowcount
        if changed != 1:
            return False
        self.session.flush()
        refresh = getattr(self.session, "refresh", None)
        if callable(refresh):
            refresh(row)
        else:
            # Minimal repository doubles may not expose SQLAlchemy's refresh
            # method.  Keep their returned row coherent without weakening the
            # production CAS above.
            for name, value in values.items():
                setattr(row, name, value)
        return row

    def set_terminal(
        self,
        run_id: UUID | str,
        terminal_status: str,
        *,
        supervisor_lock: Any = None,
        terminal_decision_reason: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        result_integrity_status: str | None = None,
        result_counts: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        terminal_evidence: Mapping[str, Any] | None = None,
    ) -> BacktestRunRecord | None:
        """Write a terminal state through the locked conditional-CAS path.

        ``set_terminal`` is deliberately unusable by API/Worker callers: the
        live Supervisor advisory-lock capability is mandatory.  The evidence
        and status fields are included in one SQL ``UPDATE`` so a competing
        reconciliation either wins the row predicate or observes the already
        committed first decision without overwriting it.
        """

        _changed, row = self._set_terminal_cas(
            run_id,
            terminal_status=terminal_status,
            supervisor_lock=supervisor_lock,
            terminal_decision_reason=terminal_decision_reason,
            evidence=evidence,
            result_integrity_status=result_integrity_status,
            result_counts=result_counts,
            now=now,
            terminal_evidence=terminal_evidence,
        )
        return row

    def write_terminal(
        self,
        run_id: UUID | str,
        *,
        status: str,
        supervisor_lock: Any = None,
        **evidence: Any,
    ) -> bool:
        """Supervisor callback returning whether this CAS won the row."""

        finished_at = evidence.get("finished_at")
        changed, _row = self._set_terminal_cas(
            run_id,
            terminal_status=status,
            supervisor_lock=supervisor_lock,
            terminal_decision_reason=evidence.get("terminal_decision_reason"),
            terminal_evidence=evidence,
            now=finished_at if isinstance(finished_at, datetime) else None,
            allow_queued_cancel=(
                status == "cancelled"
                and evidence.get("terminal_decision_reason") == "cancelled_before_start"
            ),
        )
        return changed

    def _set_terminal_cas(
        self,
        run_id: UUID | str,
        *,
        terminal_status: str,
        supervisor_lock: Any = None,
        terminal_decision_reason: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        result_integrity_status: str | None = None,
        result_counts: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        terminal_evidence: Mapping[str, Any] | None = None,
        allow_queued_cancel: bool = False,
    ) -> tuple[bool, BacktestRunRecord | None]:
        """Apply the shared locked CAS and return ``(changed, current_row)``."""

        assert_supervisor_lock_held(supervisor_lock)
        if terminal_status not in TERMINAL_STATUSES:
            raise ValueError(f"unsupported terminal status: {terminal_status}")
        reason = terminal_decision_reason or (
            "terminal_decision" if terminal_status != "indeterminate" else None
        )
        if terminal_status == "indeterminate" and not reason:
            raise ValueError("indeterminate requires terminal_decision_reason")
        # Read/lock first so a caller cannot finalize a still-queued run
        # directly.  The only queued exception is a Supervisor-owned
        # cancellation before a worker acquired any identity.
        current = self.get(run_id, for_update=True)
        if current is None:
            return False, None
        if current.status in TERMINAL_STATUSES:
            return False, current
        allowed_active = current.status in {"starting", "running", "cancel_requested"}
        allowed_queued_cancel = (
            allow_queued_cancel
            and terminal_status == "cancelled"
            and reason == "cancelled_before_start"
            and current.status in {"queued", "cancel_requested"}
            and all(
                value is None
                for value in (
                    current.launch_id,
                    current.child_pid,
                    current.child_start_identity,
                    current.child_process_group_id,
                    current.worker_handshake_at,
                )
            )
        )
        if not allowed_active and not allowed_queued_cancel:
            raise InvalidRunTransition(
                f"{current.status} -> {terminal_status} is not allowed"
            )
        if terminal_status == "cancelled" and current.status in {"queued", "cancel_requested"} and not allowed_queued_cancel:
            raise InvalidRunTransition(
                "queued cancellation requires the Supervisor cancellation path"
            )
        values: dict[str, Any] = {
            "status": terminal_status,
            "terminal_status": terminal_status,
            "finished_at": now or _utcnow(),
            "terminal_decision_reason": reason,
        }
        if evidence is not None:
            values["result_integrity_evidence"] = _json_value(evidence)
        if result_integrity_status is not None:
            values["result_integrity_status"] = result_integrity_status
        if result_counts is not None:
            values["result_counts"] = _json_value(result_counts)
        if terminal_evidence is not None:
            columns = set(BacktestRunRecord.__table__.columns.keys())
            for key, value in terminal_evidence.items():
                if key in {"status", "terminal_status", "finished_at"}:
                    continue
                if key in columns:
                    values[key] = _json_value(value)
        statement = (
            update(BacktestRunRecord)
            .where(
                BacktestRunRecord.id == UUID(str(run_id)),
                BacktestRunRecord.status == current.status,
                BacktestRunRecord.status.not_in(TERMINAL_STATUSES),
            )
            .where(
                # Keep queued cancellation's identity guard in the SQL CAS,
                # not merely in the stale ORM snapshot above.
                *(
                    (
                        BacktestRunRecord.status.in_(("queued", "cancel_requested")),
                        BacktestRunRecord.launch_id.is_(None),
                        BacktestRunRecord.child_pid.is_(None),
                        BacktestRunRecord.child_start_identity.is_(None),
                        BacktestRunRecord.child_process_group_id.is_(None),
                        BacktestRunRecord.worker_handshake_at.is_(None),
                    )
                    if allowed_queued_cancel
                    else ()
                )
            )
            .values(**values)
        )
        result = self.session.execute(statement)
        self.session.flush()
        row = self.get(run_id)
        # A zero-row update intentionally returns the already committed state;
        # callers must never overwrite a concurrent terminal decision.
        return result.rowcount == 1, row


class DatabaseRunCreationRepository(DatabaseRunRepository):
    """Compatibility name retained for callers that only create roots."""


BacktestRunRepository = DatabaseRunRepository


__all__ = [
    "BacktestRunRepository",
    "DatabaseRunCreationRepository",
    "DatabaseRunRepository",
    "FORMAL_KIND",
    "INTERNAL_KIND",
    "NON_TERMINAL_STATUSES",
    "QueueSchemaError",
    "RunRepository",
    "TERMINAL_STATUSES",
]
