"""Formal and internal backtest run lifecycle APIs.

The API only validates/freeze-binds a request, persists a queued root, and
records cancellation intent.  A worker or supervisor owns every terminal
decision; no endpoint executes strategy code or writes a final status.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import logging
from typing import Any, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.auth import (
    AuthenticatedPrincipal,
    require_internal_backtest_token,
)
from app.core.config import get_settings
from app.core.logging import backtest_event_message
from app.strategies.repository import StrategyRepository
from app.db.session import get_db_session

from .domain import PositionSide
from .spec import BacktestSpec, ComponentSelection, InitialPositionInput
from .run_admission import RunAdmissionService
from .run_binding import (
    BacktestRun,
    IdempotencyKeyReusedError,
    RunBinding,
    RunBindingBuilder,
    RunCreationService,
    QueueFullError,
)
from .run_repository import (
    DatabaseRunRepository,
    FORMAL_KIND,
    INTERNAL_KIND,
    TERMINAL_STATUSES,
)
from .run_schemas import InternalRunCreateRequest, RunCreateRequest, RunResponse
from .production_runtime import build_formal_binding, default_components
from app.instruments.rule_snapshots_repository import RunRuleSnapshotRepository
from .data.reports import canonical_hash


router = APIRouter(prefix="/api/admin/backtest-runs", tags=["backtests"])
# The documented collection is ``/api/admin/backtests``.  Keep the
# ``backtest-runs`` collection as a compatibility alias while both delegate to
# the same repository and formal-only visibility guard.
formal_alias_router = APIRouter(prefix="/api/admin/backtests", tags=["backtests"])
internal_router = APIRouter(
    prefix="/api/internal/backtests", tags=["internal-backtests"]
)

_service = RunAdmissionService()
_creation = RunCreationService()
# Direct function callers in older unit tests do not supply a database
# dependency; the fallback remains deliberately private and is never used by
# an ASGI request, where a SQLAlchemy Session is always injected.
_runs: dict[str, BacktestRun] = {}
logger = logging.getLogger("backtesting.run")

ERROR_MESSAGES = {
    "invalid_request": "请求参数无效",
    "idempotency_key_reused": "幂等键已用于不同请求",
    "queue_full": "回测等待队列已满，请稍后重试",
    "run_not_found": "运行不存在",
    "internal_disabled": "内部验收运行入口未启用",
    "account_not_selected": "请选择回测账户。",
    "account_version_not_found": "回测账户版本不存在。",
    "account_version_unavailable": "回测账户版本当前不可用。",
    "zero_cost_formal_forbidden": "正式或内部运行禁止使用零费用方案。",
    "strategy_revision_not_published": "策略版本尚未发布。",
    "data_preflight_blocked": "数据准入预检未通过。",
    "formal_gate_blocked": "正式回测门禁未通过。",
    "backtest_run_kind_forbidden": "运行类型不允许用于当前接口。",
    "backtest_cancel_not_allowed": "当前运行状态不允许取消。",
}


def require_internal_capability(
    capability: str = Depends(require_internal_backtest_token),
) -> str:
    """Keep the internal capability dependency name for route compatibility."""

    # Direct unit callers historically passed the capability label itself;
    # deployed requests always receive the value from the private token
    # dependency above, never from a client-controlled role header.
    return capability


def _owner_scope(request: Request | None) -> str:
    """Return the authenticated owner, with a test-only direct-call fallback."""

    if request is not None:
        principal = getattr(request.state, "authenticated_principal", None)
        if isinstance(principal, AuthenticatedPrincipal):
            return principal.owner_scope
    return "default"


def _request_fingerprint(payload: RunCreateRequest, kind: str) -> str:
    """Hash request-controlled inputs before resolving external dependencies."""

    return canonical_hash(
        {
            "run_kind": kind,
            "strategy_revision_id": str(payload.strategy_revision_id),
            "account_profile_id": (
                str(payload.account_profile_id)
                if payload.account_profile_id is not None
                else None
            ),
            "random_seed": payload.random_seed,
            "parameters": payload.parameters,
            "backtest_config": payload.backtest_config.model_dump(mode="json"),
            "slippage_model": payload.slippage_model.model_dump(mode="json"),
            "degraded": payload.degraded,
            "confirmed_admission_report_hash": payload.confirmed_admission_report_hash,
        }
    )


def _effective_idempotency_key(
    payload: RunCreateRequest,
    header_key: str | None,
) -> str:
    """Prefer the standard header, then the explicit client request id/body key."""

    candidates = [
        value.strip()
        for value in (header_key, payload.client_request_id, payload.idempotency_key)
        if isinstance(value, str) and value.strip()
    ]
    if not candidates:
        raise ValueError("Idempotency-Key or client_request_id is required")
    if len(set(candidates)) != 1:
        raise ValueError("Idempotency-Key and body idempotency identifiers differ")
    return candidates[0]


def _is_session(value: object) -> bool:
    """Recognize a direct SQLAlchemy session without importing test sentinels."""

    return isinstance(value, Session) or (
        value is not None
        and not hasattr(value, "dependency")
        and callable(getattr(value, "execute", None))
        and callable(getattr(value, "flush", None))
    )


def _settings(request: Request | None) -> object:
    if request is not None:
        attached = getattr(getattr(request, "app", None), "state", None)
        configured = getattr(attached, "settings", None)
        if configured is not None:
            return configured
    try:
        return get_settings()
    except Exception:
        # Direct route-function tests may not provide environment secrets.
        return None


def _limit(settings: object, *names: str, default: int | None) -> int | None:
    for name in names:
        value = getattr(settings, name, None) if settings is not None else None
        if value is not None:
            return value
    return default


def _db_repository(session: object, request: Request | None) -> DatabaseRunRepository:
    settings = _settings(request)
    formal_limit = _limit(
        settings,
        "backtest_max_queued_runs",
        "backtest_formal_queue_limit",
        "backtest_queue_limit",
        default=32,
    )
    internal_limit = _limit(
        settings,
        "backtest_internal_max_queued_runs",
        "backtest_internal_queue_limit",
        default=None,
    )
    return DatabaseRunRepository(
        session, formal_limit=formal_limit or 32, internal_limit=internal_limit
    )


def _build_spec(
    payload: RunCreateRequest, *, internal: bool = False
) -> BacktestSpec:
    """Create the one immutable domain input used by preflight and binding."""

    config = payload.backtest_config
    positions: list[InitialPositionInput] = []
    if not internal:
        positions = [
            InitialPositionInput(
                instrument_id=item.instrument_id,
                side=PositionSide(item.side),
                quantity=item.quantity,
                available_quantity=(
                    item.quantity
                    if item.available_quantity is None
                    else item.available_quantity
                ),
                average_price=item.average_price,
            )
            for item in config.initial_positions
        ]
    return BacktestSpec(
        start_date=config.start_date,
        end_date=config.end_date,
        initial_cash=config.initial_cash,
        initial_positions=positions,
        dynamic_universe=config.dynamic_universe,
        instrument_ids=config.instrument_ids,
        exchanges=config.exchanges,
        strategy_price_bases=config.strategy_price_bases,
        strategy_revision_id=payload.strategy_revision_id,
        strategy_parameters=payload.parameters,
        account_profile_id=payload.account_profile_id,
        slippage_model=ComponentSelection(
            payload.slippage_model.key,
            payload.slippage_model.version,
            payload.slippage_model.parameters,
        ),
        random_seed=payload.random_seed,
        currency=config.currency,
        timezone=config.timezone,
        frequency=config.frequency,
        warmup_sessions=config.warmup_sessions,
    )


def _binding(payload: RunCreateRequest, *, kind: str) -> RunBinding:
    if kind not in {FORMAL_KIND, INTERNAL_KIND}:
        raise ValueError("unsupported trusted run kind")
    spec = _build_spec(payload, internal=kind == INTERNAL_KIND)
    components = default_components(spec.slippage_model)
    resolved_slippage = components["slippage_model"]
    spec = replace(
        spec,
        slippage_model=ComponentSelection(
            str(resolved_slippage["key"]),
            int(resolved_slippage["version"]),
            dict(resolved_slippage["parameters"]),
        ),
    )
    # Direct-call compatibility still freezes the same input shape as the DB
    # path; only storage-backed resolution is unavailable in this branch.
    return RunBindingBuilder().build(
        spec,
        run_kind=kind,
        strategy={
            "revision_id": str(payload.strategy_revision_id),
            "published": True,
            "parameters": dict(payload.parameters or {}),
        },
        components=components,
        data_request={},
        account=(
            {"profile_id": str(payload.account_profile_id)}
            if payload.account_profile_id is not None
            else {}
        ),
    )


def _uuid_or_none(value: object) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _wire_value(value: Any) -> Any:
    """Materialize immutable binding containers for JSON response encoding."""

    if isinstance(value, Mapping):
        return {str(key): _wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire_value(item) for item in value]
    if isinstance(value, (date, UUID)):
        return value.isoformat()
    return value


def _response(run: BacktestRun | object) -> RunResponse:
    """Project domain or ORM rows without exposing stdout/secrets/source."""

    if isinstance(run, BacktestRun):
        binding = run.binding
        spec = binding.spec
        values: dict[str, Any] = {
            "run_id": run.run_id,
            "run_kind": binding.run_kind,
            "profile": binding.profile,
            "status": run.status,
            "config_hash": binding.config_hash,
            "rerun_of_run_id": run.rerun_of_run_id,
            "strategy_revision_id": _uuid_or_none(
                binding.strategy.get("revision_id")
                if isinstance(binding.strategy, Mapping)
                else None
            ),
            "parameters": _wire_value(
                binding.strategy.get("parameters", {})
                if isinstance(binding.strategy, Mapping)
                else {}
            ),
            "backtest_config": _wire_value(binding.config),
            "data_request": _wire_value(binding.data_request),
            "behavior_versions": _wire_value(binding.metadata.get("behavior_versions", {}))
            if isinstance(binding.metadata, Mapping)
            else {},
            "formal_gates": _wire_value(binding.metadata.get("formal_gates", {}))
            if isinstance(binding.metadata, Mapping)
            else {},
            "account_profile_id": _uuid_or_none(
                binding.account.get("profile_id", binding.account.get("account_profile_id"))
                if isinstance(binding.account, Mapping)
                else None
            ),
            "account_profile_version": (
                str(binding.account.get("version"))
                if isinstance(binding.account, Mapping) and binding.account.get("version") is not None
                else None
            ),
            "fee_schedule_key": (
                str(binding.account.get("fee_schedule_key"))
                if isinstance(binding.account, Mapping) and binding.account.get("fee_schedule_key") is not None
                else None
            ),
            "fee_schedule_version": (
                str(binding.account.get("fee_schedule_version"))
                if isinstance(binding.account, Mapping) and binding.account.get("fee_schedule_version") is not None
                else None
            ),
            "random_seed": binding.random_seed,
            "progress_ratio": 0,
        }
        return RunResponse(**values)

    binding = getattr(run, "binding", None)
    run_id = getattr(run, "id", getattr(run, "run_id", None))
    run_kind = getattr(run, "run_kind", None) or (
        getattr(binding, "run_kind", FORMAL_KIND)
    )
    profile = getattr(run, "profile", None) or getattr(binding, "profile", "formal@1")
    status = getattr(run, "status", "queued")
    current_date = getattr(run, "current_trading_date", None)
    if current_date is None:
        # Legacy task-08 rows may carry only the textual date alias.  Convert
        # it into the canonical date field without returning the alias.
        legacy_date = getattr(run, "current_date", None)
        if isinstance(legacy_date, str) and legacy_date.strip():
            try:
                current_date = date.fromisoformat(legacy_date[:10])
            except ValueError:
                current_date = None
    parameters = getattr(run, "parameters", None)
    if parameters is None and binding is not None:
        parameters = {}
    config = getattr(run, "backtest_config", None)
    if config is None and binding is not None:
        config = dict(binding.config)
    data_request = getattr(run, "data_request", None) or {}
    behavior_versions = getattr(run, "behavior_versions", None) or {}
    formal_gates = getattr(run, "formal_gate_evidence", None) or getattr(run, "formal_gates", None) or {}
    if not formal_gates:
        data_evidence = getattr(run, "data_evidence", None)
        if isinstance(data_evidence, Mapping):
            formal_gates = data_evidence.get("formal_gates", {}) or {}
    if not formal_gates and isinstance(config, Mapping):
        metadata = config.get("metadata")
        if isinstance(metadata, Mapping):
            formal_gates = metadata.get("formal_gates", {}) or {}
    failure_evidence = getattr(run, "failure_evidence", None)
    if not isinstance(failure_evidence, Mapping):
        failure_evidence = {}
    raw_current_step = getattr(run, "current_step", None)
    values = {
        "run_id": _uuid_or_none(run_id),
        "run_kind": run_kind,
        "profile": profile,
        "status": status,
        "terminal_status": getattr(run, "terminal_status", None),
        "config_hash": getattr(run, "config_hash", ""),
        "rerun_of_run_id": _uuid_or_none(getattr(run, "rerun_of_run_id", None)),
        "strategy_revision_id": _uuid_or_none(
            getattr(run, "strategy_revision_id", None)
            or (
                binding.strategy.get("revision_id")
                if isinstance(binding, RunBinding)
                and isinstance(binding.strategy, Mapping)
                else None
            )
        ),
        "parameters": _wire_value(parameters or {}),
        "backtest_config": _wire_value(config or {}),
        "data_request": _wire_value(data_request),
        "behavior_versions": _wire_value(behavior_versions),
        "formal_gates": _wire_value(formal_gates),
        "account_profile_id": _uuid_or_none(getattr(run, "account_profile_id", None)),
        "account_profile_version": (
            str(getattr(run, "account_profile_version", None))
            if getattr(run, "account_profile_version", None) is not None
            else None
        ),
        "fee_schedule_key": getattr(run, "fee_schedule_key", None),
        "fee_schedule_version": (
            str(getattr(run, "fee_schedule_version", None))
            if getattr(run, "fee_schedule_version", None) is not None
            else None
        ),
        "random_seed": getattr(run, "random_seed", None),
        # The database column is retained for compatibility with the existing
        # run root, while the API exposes the canonical protocol name only.
        "progress_ratio": float(
            getattr(run, "progress_ratio", None)
            if getattr(run, "progress_ratio", None) is not None
            else getattr(run, "progress", 0)
            or 0
        ),
        "current_trading_date": current_date,
        "current_step": str(raw_current_step) if raw_current_step is not None else None,
        "created_at": getattr(run, "created_at", None),
        "started_at": getattr(run, "started_at", None),
        "finished_at": getattr(run, "finished_at", None),
        "claimed_at": getattr(run, "claimed_at", None),
        "child_pid": getattr(run, "child_pid", None),
        "child_process_group_id": getattr(run, "child_process_group_id", None),
        "worker_id": getattr(run, "worker_id", None),
        "worker_handshake_at": getattr(run, "worker_handshake_at", None),
        "last_heartbeat_at": getattr(run, "last_heartbeat_at", None),
        "last_progress_persisted_at": getattr(run, "last_progress_persisted_at", None),
        "cancel_requested_at": getattr(run, "cancel_requested_at", None),
        "cancel_requested": bool(getattr(run, "cancel_requested", False)),
        "termination_requested_at": getattr(run, "termination_requested_at", None),
        "termination_reason": getattr(run, "termination_reason", None),
        "forced_termination": bool(getattr(run, "forced_termination", False)),
        "recovery_observed_at": getattr(run, "recovery_observed_at", None),
        "recovery_action": getattr(run, "recovery_action", None),
        "recovery_process_state": getattr(run, "recovery_process_state", None),
        # Persisted task-22 names are projected to the task-23 wire contract.
        "child_exit_code": (
            getattr(run, "child_exit_code", None)
            if getattr(run, "child_exit_code", None) is not None
            else getattr(run, "runner_exit_code", None)
        ),
        "child_exit_code_protocol": (
            getattr(run, "child_exit_code_protocol", None)
            if getattr(run, "child_exit_code_protocol", None) is not None
            else getattr(run, "runner_exit_code_protocol", None)
        ),
        "runner_exit_category": getattr(run, "runner_exit_category", None),
        "completion_marker_protocol": getattr(run, "completion_marker_protocol", None),
        "completion_marker_validation": getattr(
            run, "completion_marker_validation", None
        ),
        "result_integrity_status": getattr(run, "result_integrity_status", None),
        "terminal_decision_reason": getattr(run, "terminal_decision_reason", None),
        "failure_phase": getattr(run, "failure_phase", None)
        or failure_evidence.get("failure_phase"),
        "failure_step": failure_evidence.get("failure_step"),
        "failure_type": getattr(run, "failure_type", None)
        or failure_evidence.get("error_type"),
        "source_line": failure_evidence.get("source_line"),
        "technical_detail": failure_evidence.get("technical_detail"),
        "error_message": getattr(run, "error_message", None)
        or failure_evidence.get("message"),
        "failure_evidence": getattr(run, "failure_evidence", None),
        "stdout_bytes": getattr(run, "stdout_bytes", None),
        "stdout_digest": getattr(run, "stdout_digest", None),
        "stdout_truncated": getattr(run, "stdout_truncated", None),
        "stdout_evidence": getattr(run, "stdout_evidence", None),
        "resource_limit_evidence": getattr(run, "resource_limit_evidence", None),
        "runner_config_evidence": getattr(run, "runner_config_evidence", None),
        "completion_marker": getattr(run, "completion_marker", None),
        "runner_exit_report": getattr(run, "runner_exit_report", None),
        "result_integrity_evidence": getattr(run, "result_integrity_evidence", None),
        "result_counts": getattr(run, "result_counts", None) or {},
    }
    # A malformed persisted row must not become a 500 serialization leak.  The
    # repository's root guard still controls visibility; this fallback only
    # preserves a stable response for UUIDs from legacy test doubles.
    if values["run_id"] is None:
        raise ValueError("run id is invalid")
    return RunResponse(**values)


def _queue_error(exc: QueueFullError) -> HTTPException:
    queue_kind = getattr(exc, "queue_kind", None)
    queued_count = getattr(exc, "queued_count", None)
    queue_limit = getattr(exc, "queue_limit", None)
    disabled = bool(getattr(exc, "disabled", False))
    if disabled:
        return HTTPException(
            status_code=503,
            detail={
                "code": "internal_disabled",
                "message": ERROR_MESSAGES["internal_disabled"],
                "queue_kind": queue_kind,
            },
        )
    return HTTPException(
        status_code=429,
        detail={
            "code": "backtest_queue_full",
            "message": (
                "内部链路验收等待队列已满，请稍后重试"
                if queue_kind == INTERNAL_KIND
                else ERROR_MESSAGES["queue_full"]
            ),
            "queue_kind": queue_kind,
            "queued_count": queued_count,
            "queue_limit": queue_limit,
        },
    )


def _rollback(session: object) -> None:
    if _is_session(session):
        session.rollback()


def _existing_or_conflict(
    repository: DatabaseRunRepository,
    *,
    scope: str,
    key: str,
    binding: RunBinding,
) -> object | None:
    existing = repository.get_by_idempotency(scope, key)
    if existing is None:
        return None
    if existing.config_hash != binding.config_hash:
        raise IdempotencyKeyReusedError(
            "idempotency key already used with different request"
        )
    return existing


def _admit_internal_binding(binding: RunBinding) -> RunBinding:
    """Apply the internal Phase 1/2a gate before accepting a run root."""

    admission = _service.admission(
        binding,
        {
            "phase1": bool(binding.strategy),
            "phase2a": bool(binding.data_request),
        },
    )
    if not admission.allowed:
        error = ValueError(admission.message)
        error.admission_result = admission
        raise error
    if not admission.formal_gates:
        return binding
    metadata = dict(binding.metadata)
    data_evidence = metadata.get("data_evidence", {})
    data_evidence = dict(data_evidence) if isinstance(data_evidence, Mapping) else {}
    data_evidence["formal_gates"] = dict(admission.formal_gates)
    metadata.update(
        {
            "data_evidence": data_evidence,
            "formal_gates": dict(admission.formal_gates),
        }
    )
    return replace(binding, metadata=metadata)


def _create(
    payload: RunCreateRequest,
    *,
    kind: str,
    session: object,
    request: Request | None,
    idempotency_key: str | None = None,
) -> RunResponse:
    scope = _owner_scope(request)
    effective_key = idempotency_key or _effective_idempotency_key(payload, None)
    request_fingerprint = _request_fingerprint(payload, kind)
    repository = _db_repository(session, request) if _is_session(session) else None
    if repository is not None:
        # Idempotency lookup must precede revision/account/data resolution. A
        # retry can return its committed run even if a dependency is currently
        # unavailable, and a full queue cannot turn it into a duplicate.
        existing = repository.get_by_idempotency(scope, effective_key)
        if existing is not None:
            existing_fingerprint = getattr(existing, "idempotency_request_hash", None)
            if existing_fingerprint and existing_fingerprint != request_fingerprint:
                raise IdempotencyKeyReusedError(
                    "idempotency key already used with different request"
                )
            return _response(existing)

    rule_snapshot_bundle = None
    formal_gate_evidence: Mapping[str, Any] | None = None
    if _is_session(session) and kind == FORMAL_KIND:
        revision = StrategyRepository(session).get_revision(payload.strategy_revision_id)
        if revision is None:
            raise ValueError("published strategy revision is required")
        binding_result = build_formal_binding(
            spec=_build_spec(payload),
            revision=revision,
            session=session,
            degraded=payload.degraded,
            confirmed_report_hash=payload.confirmed_admission_report_hash,
        )
        binding = binding_result.binding
        rule_snapshot_bundle = binding_result.rule_snapshot_bundle
        formal_gate_evidence = binding_result.formal_gate_evidence
    else:
        binding = _binding(payload, kind=kind)
    if _is_session(session):
        # Idempotency lookup is before admission/capacity.  A retry therefore
        # succeeds even when the queue filled after the original request.
        existing = _existing_or_conflict(
            repository,
            scope=scope,
            key=effective_key,
            binding=binding,
        )
        if existing is not None:
            return _response(existing)
        if kind == INTERNAL_KIND:
            binding = _admit_internal_binding(binding)
        row = repository.create(
            binding,
            tenant_id=scope,
            idempotency_scope=scope,
            idempotency_key=effective_key,
            idempotency_request_hash=request_fingerprint,
            formal_gate_evidence=formal_gate_evidence,
        )
        if rule_snapshot_bundle is not None:
            snapshot_repository = RunRuleSnapshotRepository(session)
            if snapshot_repository.snapshot_hash_for(row.id) is None:
                snapshot_repository.write_bundle(
                    rule_snapshot_bundle.for_run(row.id)
                )
        session.commit()
        session.refresh(row)
        logger.info(
            "backtest_created",
            extra={
                "event": "backtest_created",
                "message": backtest_event_message(
                    "回测运行创建",
                    f"{binding.spec.start_date} 至 {binding.spec.end_date}",
                    "已进入正式等待队列"
                    if kind == FORMAL_KIND
                    else "已进入内部等待队列",
                ),
                "run_id": str(row.id),
                "run_kind": row.run_kind,
                "config_hash": row.config_hash,
                "status": row.status,
            },
        )
        return _response(row)

    existing = next(
        (
            item
            for item in _runs.values()
            if item.idempotency_key == effective_key
            and item.binding.run_kind == kind
            and item.owner_scope == scope
        ),
        None,
    )
    if existing is not None:
        if existing.binding.config_hash != binding.config_hash:
            raise IdempotencyKeyReusedError(
                "idempotency key already used with different request"
            )
        return _response(existing)
    # Compatibility for direct calls that bypass FastAPI dependency injection.
    # Keep the same idempotency-before-admission order as the DB path.
    if kind == INTERNAL_KIND:
        binding = _admit_internal_binding(binding)
    if kind == FORMAL_KIND:
        admission = _service.admission(
            binding,
            {},
            degraded=payload.degraded,
            confirmed_report_hash=payload.confirmed_admission_report_hash,
        )
        if not admission.allowed:
            raise ValueError(admission.message)
    run = _creation.create(
        binding,
        idempotency_key=effective_key,
        tenant_id=scope,
    )
    _runs[str(run.run_id)] = run
    return _response(run)


@router.post("", response_model=RunResponse, status_code=201)
def create_formal(
    payload: RunCreateRequest,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    idempotency_header: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
) -> RunResponse:
    try:
        return _create(
            payload,
            kind=FORMAL_KIND,
            session=session,
            request=request,
            idempotency_key=_effective_idempotency_key(payload, idempotency_header),
        )
    except QueueFullError as exc:
        _rollback(session)
        raise _queue_error(exc) from exc
    except IdempotencyKeyReusedError as exc:
        _rollback(session)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_key_reused",
                "message": ERROR_MESSAGES["idempotency_key_reused"],
                "details": {"error_type": type(exc).__name__},
            },
        ) from exc
    except Exception as exc:
        _rollback(session)
        gate_evidence = getattr(exc, "formal_gate_evidence", None)
        if gate_evidence is not None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "formal_gate_blocked",
                    "message": "正式回测门禁未通过。",
                    "status": "blocked",
                    "report_hash": gate_evidence.get("report_hash"),
                    "gates": gate_evidence.get("gates", {}),
                    "issues": gate_evidence.get("issues", []),
                },
            ) from exc
        admission = getattr(exc, "admission_result", None)
        if admission is not None:
            outcome = getattr(admission, "outcome", None)
            outcome_status = getattr(outcome, "status", "blocked") if outcome is not None else "blocked"
            raise HTTPException(
                status_code=422,
                detail={
                    "code": getattr(admission, "reason_code", None) or "formal_preflight_blocked",
                    "message": str(exc),
                    "status": getattr(outcome_status, "value", outcome_status),
                    "report_hash": getattr(admission, "report_hash", None),
                    "gates": getattr(admission, "formal_gates", {}) or {},
                    "issues": [
                        item.as_dict() if hasattr(item, "as_dict") else item
                        for item in (
                            getattr(getattr(outcome, "report", None), "issues", ())
                            if outcome is not None
                            else ()
                        )
                    ],
                },
            ) from exc
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_request",
                "message": ERROR_MESSAGES["invalid_request"],
                "details": {"error_type": type(exc).__name__},
            },
        ) from exc


@router.post("/preflight")
def preflight(
    payload: RunCreateRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Run the same side-effect-free formal admission used by creation."""

    try:
        revision = StrategyRepository(session).get_revision(payload.strategy_revision_id)
        if revision is None:
            raise ValueError("published strategy revision is required")
        binding_result = build_formal_binding(
            spec=_build_spec(payload),
            revision=revision,
            session=session,
            degraded=payload.degraded,
            confirmed_report_hash=payload.confirmed_admission_report_hash,
        )
        binding = binding_result.binding
        evidence = binding.metadata.get("data_evidence", {})
        return {
            "status": binding.metadata.get("data_preflight_status", "ready"),
            "code": None,
            "message": "正式回测准入预检通过",
            "report_hash": binding.metadata.get("admission_report_hash"),
            "issues": evidence.get("issues", []) if isinstance(evidence, Mapping) else [],
            "gates": binding_result.formal_gate_evidence or {},
        }
    except Exception as exc:
        gate_evidence = getattr(exc, "formal_gate_evidence", None)
        if gate_evidence is not None:
            return {
                "status": "blocked",
                "code": "formal_gate_blocked",
                "message": "正式回测门禁未通过。",
                "report_hash": gate_evidence.get("report_hash"),
                "issues": gate_evidence.get("issues", []),
                "gates": gate_evidence.get("gates", {}),
            }
        admission = getattr(exc, "admission_result", None)
        if admission is not None:
            outcome = getattr(admission, "outcome", None)
            outcome_status = getattr(outcome, "status", "blocked") if outcome is not None else "blocked"
            return {
                "status": getattr(outcome_status, "value", outcome_status),
                "code": getattr(admission, "reason_code", None) or "formal_preflight_blocked",
                "message": str(exc),
                "report_hash": getattr(admission, "report_hash", None),
                "gates": getattr(admission, "formal_gates", {}) or {},
                "issues": [
                    item.as_dict() if hasattr(item, "as_dict") else item
                    for item in (
                        getattr(getattr(outcome, "report", None), "issues", ())
                        if outcome is not None
                        else ()
                    )
                ],
            }
        session.rollback()
        return {
            "status": "blocked",
            "code": "formal_preflight_failed",
            "message": "正式回测准入预检失败",
            "report_hash": None,
            "issues": [{"error_type": type(exc).__name__}],
        }


@internal_router.post(
    "/link-acceptance",
    response_model=RunResponse,
    status_code=201,
    dependencies=[Depends(require_internal_capability)],
)
def create_internal(
    payload: InternalRunCreateRequest,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    idempotency_header: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
) -> RunResponse:
    try:
        return _create(
            payload,
            kind=INTERNAL_KIND,
            session=session,
            request=request,
            idempotency_key=_effective_idempotency_key(payload, idempotency_header),
        )
    except QueueFullError as exc:
        _rollback(session)
        raise _queue_error(exc) from exc
    except IdempotencyKeyReusedError as exc:
        _rollback(session)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_key_reused",
                "message": ERROR_MESSAGES["idempotency_key_reused"],
                "details": {"error_type": type(exc).__name__},
            },
        ) from exc
    except Exception as exc:
        _rollback(session)
        if isinstance(exc, QueueFullError) and getattr(exc, "disabled", False):
            raise _queue_error(exc) from exc
        admission = getattr(exc, "admission_result", None)
        if admission is not None:
            outcome = getattr(admission, "outcome", None)
            status_value = getattr(getattr(outcome, "status", None), "value", getattr(outcome, "status", "blocked"))
            raise HTTPException(
                status_code=422,
                detail={
                    "code": getattr(admission, "code", None) or getattr(admission, "reason_code", None) or "internal_preflight_blocked",
                    "message": str(exc),
                    "status": status_value,
                    "report_hash": getattr(admission, "report_hash", None),
                    "gates": getattr(admission, "formal_gates", {}) or {},
                },
            ) from exc
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_request",
                "message": ERROR_MESSAGES["invalid_request"],
                "details": {"error_type": type(exc).__name__},
            },
        ) from exc


def _cancel(
    run_id: str,
    *,
    expected_kind: str,
    session: object,
    owner_scope: str = "default",
) -> RunResponse:
    if _is_session(session):
        row = DatabaseRunRepository(session).request_cancel(
            run_id, expected_kind=expected_kind, owner_scope=owner_scope
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": ERROR_MESSAGES["run_not_found"]},
            )
        session.commit()
        session.refresh(row)
        logger.info(
            "backtest_cancel_requested",
            extra={
                "event": "backtest_cancel_requested",
                "message": backtest_event_message(
                    "回测取消请求", run_id, "已记录，等待 Supervisor 裁决"
                ),
                "run_id": run_id,
                "run_kind": row.run_kind,
                "status": row.status,
            },
        )
        return _response(row)
    run = _runs.get(run_id)
    if run is None or run.binding.run_kind != expected_kind:
        raise HTTPException(
            status_code=404,
            detail={"code": "run_not_found", "message": ERROR_MESSAGES["run_not_found"]},
        )
    if run.status in TERMINAL_STATUSES or run.status in {"terminal", "cancel_requested"}:
        return _response(run)
    updated = replace(run, status="cancel_requested")
    _runs[run_id] = updated
    return _response(updated)


@router.post("/{run_id}/cancel")
def cancel(
    run_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
) -> RunResponse:
    try:
        return _cancel(
            run_id,
            expected_kind=FORMAL_KIND,
            session=session,
            owner_scope=_owner_scope(request),
        )
    except HTTPException:
        _rollback(session)
        raise
    except Exception as exc:
        _rollback(session)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "backtest_cancel_not_allowed",
                "message": ERROR_MESSAGES["backtest_cancel_not_allowed"],
                "details": {"error_type": type(exc).__name__},
            },
        ) from exc


@internal_router.post("/{run_id}/cancel", dependencies=[Depends(require_internal_capability)])
def cancel_internal(
    run_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
) -> RunResponse:
    try:
        return _cancel(
            run_id,
            expected_kind=INTERNAL_KIND,
            session=session,
            owner_scope=_owner_scope(request),
        )
    except HTTPException:
        _rollback(session)
        raise
    except Exception as exc:
        _rollback(session)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "backtest_cancel_not_allowed",
                "message": ERROR_MESSAGES["backtest_cancel_not_allowed"],
                "details": {"error_type": type(exc).__name__},
            },
        ) from exc


def _list(
    *,
    kind: str,
    session: object,
    limit: int,
    offset: int,
    owner_scope: str = "default",
    strategy_revision_id: str | None = None,
    strategy_id: str | None = None,
) -> dict[str, list[RunResponse]]:
    # FastAPI resolves Query defaults before dispatch; direct function callers
    # receive Query marker objects instead, so normalize them here as well.
    if not isinstance(limit, int):
        limit = 100
    if not isinstance(offset, int):
        offset = 0
    if _is_session(session):
        rows = DatabaseRunRepository(session).list(
            queue_kind=kind,
            owner_scope=owner_scope,
            limit=limit,
            offset=offset,
            strategy_revision_id=strategy_revision_id,
            strategy_id=strategy_id,
        )
        return {"items": [_response(row) for row in rows]}
    rows = [
        run
        for run in _runs.values()
        if run.binding.run_kind == kind
        and run.owner_scope == owner_scope
        and (
            strategy_revision_id is None
            or str(run.binding.strategy.get("strategy_id", run.binding.strategy.get("revision_id", "")))
            == strategy_revision_id
        )
    ]
    return {"items": [_response(run) for run in rows[offset : offset + limit]]}


@router.get("")
def list_runs(
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, list[RunResponse]]:
    return _list(
        kind=FORMAL_KIND,
        session=session,
        owner_scope=_owner_scope(request),
        limit=limit,
        offset=offset,
    )


@internal_router.get("", dependencies=[Depends(require_internal_capability)])
def list_internal_runs(
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, list[RunResponse]]:
    return _list(
        kind=INTERNAL_KIND,
        session=session,
        owner_scope=_owner_scope(request),
        limit=limit,
        offset=offset,
    )


@router.get("/strategies/{strategy_id}/backtests")
def list_strategy_runs(
    strategy_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, list[RunResponse]]:
    return _list(
        kind=FORMAL_KIND,
        session=session,
        limit=limit,
        offset=offset,
        owner_scope=_owner_scope(request),
        strategy_id=strategy_id,
    )


@router.get("/{run_id}")
def get_run(
    run_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
) -> RunResponse:
    if _is_session(session):
        try:
            row = DatabaseRunRepository(session).get(
                run_id,
                expected_kind=FORMAL_KIND,
                owner_scope=_owner_scope(request),
            )
        except (TypeError, ValueError):
            row = None
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": ERROR_MESSAGES["run_not_found"]},
            )
        return _response(row)
    run = _runs.get(run_id)
    if (
        run is None
        or run.binding.run_kind != FORMAL_KIND
        or run.owner_scope != _owner_scope(request)
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "run_not_found", "message": ERROR_MESSAGES["run_not_found"]},
        )
    return _response(run)


@internal_router.get("/{run_id}", dependencies=[Depends(require_internal_capability)])
def get_internal_run(
    run_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    if _is_session(session):
        try:
            row = DatabaseRunRepository(session).get(
                run_id,
                expected_kind=INTERNAL_KIND,
                owner_scope=_owner_scope(request),
            )
        except (TypeError, ValueError):
            row = None
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "backtest_run_not_found",
                    "message": "内部验收运行不存在",
                },
            )
        response = _response(row).model_dump(mode="json")
        response.update(
            {
                "visibility": "internal",
                "label": "内部链路验收",
                "preflight_profile": row.profile,
            }
        )
        return response
    run = _runs.get(run_id)
    if (
        run is None
        or run.binding.run_kind != INTERNAL_KIND
        or run.owner_scope != _owner_scope(request)
    ):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "backtest_run_not_found",
                "message": "内部验收运行不存在",
            },
        )
    response = _response(run).model_dump(mode="json")
    response.update(
        {"visibility": "internal", "label": "内部链路验收", "preflight_profile": run.binding.profile}
    )
    return response


@formal_alias_router.post("", response_model=RunResponse, status_code=201)
def create_formal_alias(
    payload: RunCreateRequest,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    idempotency_header: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
) -> RunResponse:
    return create_formal(
        payload,
        session=session,
        request=request,
        idempotency_header=idempotency_header,
    )


@formal_alias_router.get("")
def list_runs_alias(
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, list[RunResponse]]:
    return list_runs(session=session, request=request, limit=limit, offset=offset)


@formal_alias_router.get("/{run_id}")
def get_run_alias(
    run_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
) -> RunResponse:
    return get_run(run_id, session=session, request=request)


@formal_alias_router.post("/{run_id}/cancel")
def cancel_alias(
    run_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
) -> RunResponse:
    return cancel(run_id, session=session, request=request)


@formal_alias_router.post("/{run_id}/rerun", response_model=RunResponse, status_code=201)
def rerun_alias(
    run_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    idempotency_header: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
) -> RunResponse:
    return rerun(
        run_id,
        session=session,
        request=request,
        idempotency_header=idempotency_header,
    )


@router.post("/{run_id}/rerun", response_model=RunResponse, status_code=201)
def rerun(
    run_id: str,
    session: Session = Depends(get_db_session),
    request: Request = None,  # type: ignore[assignment]
    idempotency_header: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
) -> RunResponse:
    import uuid as _uuid

    owner_scope = _owner_scope(request)
    key = idempotency_header or str(_uuid.uuid4())
    if _is_session(session):
        repository = _db_repository(session, request)
        try:
            fresh = repository.create_rerun(
                run_id,
                owner_scope=owner_scope,
                idempotency_key=key,
            )
        except QueueFullError as exc:
            _rollback(session)
            raise _queue_error(exc) from exc
        except IdempotencyKeyReusedError as exc:
            _rollback(session)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_key_reused",
                    "message": ERROR_MESSAGES["idempotency_key_reused"],
                },
            ) from exc
        if fresh is None:
            _rollback(session)
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "backtest_rerun_not_allowed",
                    "message": "正式回测不存在或不可重新运行",
                },
            )
        session.commit()
        session.refresh(fresh)
        return _response(fresh)

    original = _runs.get(run_id)
    if (
        original is None
        or original.binding.run_kind != FORMAL_KIND
        or original.owner_scope != owner_scope
    ):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "backtest_rerun_not_allowed",
                "message": "正式回测不存在或不可重新运行",
            },
        )
    try:
        fresh = _creation.create(
            original.binding,
            idempotency_key=key,
            tenant_id=owner_scope,
        )
    except QueueFullError as exc:
        raise _queue_error(exc) from exc
    fresh = replace(
        fresh,
        rerun_of_run_id=original.run_id,
        owner_scope=owner_scope,
    )
    _runs[str(fresh.run_id)] = fresh
    return _response(fresh)


__all__ = [
    "cancel",
    "cancel_internal",
    "create_formal",
    "create_internal",
    "formal_alias_router",
    "get_internal_run",
    "get_run",
    "internal_router",
    "list_runs",
    "list_strategy_runs",
    "preflight",
    "require_internal_capability",
    "router",
]
