"""Single-instance Supervisor for isolated backtest worker processes.

This module owns process lifecycle and terminal evidence reconciliation.  It
does not execute strategy code and it never treats an OS exit code as a
business result on its own.  All queue, process, heartbeat, and terminal
operations are guarded by the long-lived advisory lock supplied by
``supervisor_lock``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
import os
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import UUID, uuid4

from . import runner_process as process_api
from .runner_integrity import ResultIntegrityChecker
from .runner_process import LaunchIdentity, StdoutCapture, WorkerProcessLauncher
from .runner_progress import is_lost_heartbeat
from .runner_protocol import (
    COMPLETION_MARKER_PROTOCOL,
    EXIT_CODE_PROTOCOL,
    evaluate_terminal,
    map_runner_exit_code,
    validate_completion_marker,
)
from .supervisor_lock import PostgresAdvisoryLock, SupervisorLockNotHeld


logger = logging.getLogger("backtesting.runner.supervisor")

TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "indeterminate", "terminal"}
)
ACTIVE_STATUSES = frozenset({"starting", "running", "cancel_requested"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _row_id(row: Any) -> Any:
    return _row_value(row, "run_id", _row_value(row, "id"))


def _set_row_value(row: Any, name: str, value: Any) -> None:
    if isinstance(row, dict):
        row[name] = value
    else:
        try:
            setattr(row, name, value)
        except Exception:
            # A read-only projection is not a safe place to infer state; the
            # repository callback below is responsible for durable updates.
            pass


def _call_repository(repository: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        method = getattr(repository, name, None)
        if callable(method):
            return method(*args, **kwargs)
    return None


@dataclass(frozen=True, slots=True)
class SupervisorSettings:
    """Frozen launch settings captured once per Supervisor process."""

    max_workers: int = 1
    run_timeout_seconds: float = 7_200
    cancel_grace_seconds: float = 10
    stdout_max_bytes: int = 1_048_576
    memory_limit_mib: int | None = 1_024
    heartbeat_max_interval_seconds: float = 15
    lost_heartbeat_seconds: float = 60
    progress_persist_interval_seconds: float = 5
    handshake_timeout_seconds: float = 15

    def __post_init__(self) -> None:
        if isinstance(self.max_workers, bool) or self.max_workers <= 0:
            raise ValueError("max_workers must be positive")
        for name in (
            "run_timeout_seconds",
            "cancel_grace_seconds",
            "stdout_max_bytes",
            "heartbeat_max_interval_seconds",
            "lost_heartbeat_seconds",
            "progress_persist_interval_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.cancel_grace_seconds >= self.run_timeout_seconds:
            raise ValueError("cancel grace must be shorter than run timeout")
        if self.lost_heartbeat_seconds < 3 * self.heartbeat_max_interval_seconds:
            raise ValueError("lost heartbeat threshold must be at least three heartbeat intervals")
        if self.progress_persist_interval_seconds > self.heartbeat_max_interval_seconds:
            raise ValueError("progress persistence interval must not exceed heartbeat interval")

    def as_dict(self) -> dict[str, Any]:
        """Return the effective launch controls as durable, non-secret evidence."""
        return {
            "max_workers": self.max_workers,
            "run_timeout_seconds": self.run_timeout_seconds,
            "cancel_grace_seconds": self.cancel_grace_seconds,
            "stdout_max_bytes": self.stdout_max_bytes,
            "memory_limit_mib": self.memory_limit_mib,
            "heartbeat_max_interval_seconds": self.heartbeat_max_interval_seconds,
            "lost_heartbeat_seconds": self.lost_heartbeat_seconds,
            "progress_persist_interval_seconds": self.progress_persist_interval_seconds,
            "handshake_timeout_seconds": self.handshake_timeout_seconds,
        }

    @classmethod
    def from_settings(cls, settings: Any) -> "SupervisorSettings":
        """Project only ``QF_BACKTEST_*`` fields from application settings."""

        def value(primary: str, *aliases: str, default: Any = None) -> Any:
            for name in (primary, *aliases):
                if hasattr(settings, name):
                    return getattr(settings, name)
            return default

        return cls(
            max_workers=value("backtest_max_workers", default=1),
            run_timeout_seconds=value("backtest_run_timeout_seconds", default=7_200),
            cancel_grace_seconds=value("backtest_cancel_grace_seconds", default=10),
            stdout_max_bytes=value("backtest_stdout_max_bytes", default=1_048_576),
            memory_limit_mib=value("backtest_memory_limit_mib", default=1_024),
            heartbeat_max_interval_seconds=value(
                "backtest_heartbeat_max_interval_seconds", default=15
            ),
            lost_heartbeat_seconds=value("backtest_lost_heartbeat_seconds", default=60),
            progress_persist_interval_seconds=value(
                "backtest_progress_persist_interval_seconds", default=5
            ),
            handshake_timeout_seconds=value(
                "backtest_handshake_timeout_seconds",
                default=min(15, value("backtest_heartbeat_max_interval_seconds", default=15)),
            ),
        )


@dataclass
class ChildHandle:
    """In-memory supervision state for one launch attempt."""

    run_id: UUID | str
    launch_id: UUID | str
    process: Any
    identity: LaunchIdentity
    started_monotonic: float
    handshake_deadline: float
    capture: StdoutCapture
    handshake_received: bool = False
    term_sent_at: float | None = None
    force_kill_sent: bool = False
    termination_reason: str | None = None
    failure_phase: str | None = None
    forced: bool = False
    # Wall-clock launch time is used only as a fallback when no durable
    # heartbeat has ever been written.  The timeout itself remains monotonic.
    started_at: datetime | None = None

    @property
    def pid(self) -> int:
        return self.identity.pid


class InMemoryRunRepository:
    """Small repository adapter useful for protocol/process unit tests."""

    def __init__(self, rows: Iterable[Any] = ()) -> None:
        self.rows: dict[str, Any] = {}
        for row in rows:
            self.add(row)
        self.events: list[dict[str, Any]] = []

    def add(self, row: Any) -> Any:
        run_id = str(_row_id(row))
        self.rows[run_id] = row
        return row

    def get(self, run_id: UUID | str) -> Any | None:
        return self.rows.get(str(run_id))

    get_run = get

    def list_active(self) -> list[Any]:
        return [row for row in self.rows.values() if _row_value(row, "status") in ACTIVE_STATUSES]

    def list_nonterminal(self) -> list[Any]:
        return [row for row in self.rows.values() if _row_value(row, "status") not in TERMINAL_STATUSES]

    def claim_next(self, *, formal_first: bool = True) -> Any | None:
        rows = [row for row in self.rows.values() if _row_value(row, "status") == "queued"]
        if not rows:
            return None
        rows.sort(
            key=lambda row: (
                0 if _row_value(row, "run_kind") == "backtest_run" else 1,
                _row_value(row, "created_at") or datetime.min.replace(tzinfo=UTC),
                str(_row_id(row)),
            )
        )
        row = rows[0]
        _set_row_value(row, "status", "starting")
        _set_row_value(row, "claimed_at", _utc_now())
        self.events.append({"event": "claimed", "run_id": str(_row_id(row))})
        return row

    def write_terminal(self, run_id: UUID | str, *, status: str, **evidence: Any) -> bool:
        row = self.get(run_id)
        if row is None or _row_value(row, "status") in TERMINAL_STATUSES:
            return False
        _set_row_value(row, "status", status)
        _set_row_value(row, "terminal_status", status)
        for key, value in evidence.items():
            _set_row_value(row, key, value)
        if _row_value(row, "finished_at") is None:
            _set_row_value(row, "finished_at", _utc_now())
        self.events.append({"event": "terminal", "run_id": str(run_id), "status": status})
        return True

    def record_child_identity(self, run_id: UUID | str, identity: LaunchIdentity) -> bool:
        row = self.get(run_id)
        if row is None:
            return False
        for name, value in (
            ("launch_id", identity.launch_id),
            ("child_pid", identity.pid),
            ("pid", identity.pid),
            ("child_start_identity", identity.start_identity),
            ("process_start_token", identity.start_identity),
            ("child_process_group_id", identity.process_group_id),
            ("process_group_id", identity.process_group_id),
        ):
            _set_row_value(row, name, value)
        return True

    def record_handshake(self, run_id: UUID | str, handshake: Any) -> bool:
        row = self.get(run_id)
        if row is None:
            return False
        _set_row_value(row, "status", "running")
        _set_row_value(row, "worker_handshake_at", handshake.observed_at or _utc_now())
        _set_row_value(row, "started_at", handshake.observed_at or _utc_now())
        _set_row_value(row, "last_heartbeat_at", handshake.observed_at or _utc_now())
        _set_row_value(row, "worker_id", handshake.worker_id)
        return True

    def record_termination_request(
        self,
        run_id: UUID | str,
        *,
        reason: str,
        requested_at: datetime,
    ) -> bool:
        """Persist termination audit evidence without changing lifecycle state."""

        row = self.get(run_id)
        if row is None:
            return False
        if _row_value(row, "termination_requested_at") is None:
            _set_row_value(row, "termination_requested_at", requested_at)
        if _row_value(row, "termination_reason") is None:
            _set_row_value(row, "termination_reason", reason)
        return True

    def commit(self) -> None:
        return None


class SqlAlchemyRunnerRepository:
    """PostgreSQL repository adapter used by the standalone entry point."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def _model(self):
        from .models import BacktestRunRecord

        return BacktestRunRecord

    def get(self, run_id: UUID | str) -> Any | None:
        return self.session.get(self._model(), UUID(str(run_id)))

    get_run = get

    def list_active(self) -> list[Any]:
        from sqlalchemy import select

        return list(
            self.session.scalars(
                select(self._model()).where(self._model().status.in_(tuple(ACTIVE_STATUSES)))
            )
        )

    def list_nonterminal(self) -> list[Any]:
        from sqlalchemy import select

        return list(
            self.session.scalars(
                select(self._model()).where(~self._model().status.in_(tuple(TERMINAL_STATUSES)))
            )
        )

    def claim_next(self, *, formal_first: bool = True) -> Any | None:
        """Claim one row with row locking and a status predicate."""

        from sqlalchemy import case, select

        model = self._model()
        order = case((model.run_kind == "backtest_run", 0), else_=1)
        row = self.session.scalars(
            select(model)
            .where(model.status == "queued")
            .order_by(order, model.created_at, model.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).first()
        if row is None:
            return None
        # This second status check protects adapters that refresh a stale ORM
        # row between SELECT and UPDATE.  PostgreSQL reports no row if another
        # transaction already moved it out of the queue.
        from sqlalchemy import update

        changed = self.session.execute(
            update(model)
            .where(model.id == row.id, model.status == "queued")
            .values(status="starting", claimed_at=_utc_now())
        ).rowcount
        if changed != 1:
            self.session.rollback()
            return None
        self.session.refresh(row)
        self.session.commit()
        return row

    def record_child_identity(self, run_id: UUID | str, identity: LaunchIdentity) -> bool:
        from sqlalchemy import update

        model = self._model()
        changed = self.session.execute(
            update(model)
            .where(model.id == UUID(str(run_id)), model.status == "starting")
            .values(
                launch_id=UUID(str(identity.launch_id)),
                child_pid=identity.pid,
                process_start_token=identity.start_identity,
                child_start_identity=identity.start_identity,
                process_group_id=identity.process_group_id,
                child_process_group_id=identity.process_group_id,
            )
        ).rowcount
        self.session.commit()
        return changed == 1

    def record_handshake(self, run_id: UUID | str, handshake: Any) -> bool:
        from sqlalchemy import update

        model = self._model()
        timestamp = handshake.observed_at or _utc_now()
        changed = self.session.execute(
            update(model)
            .where(model.id == UUID(str(run_id)), model.status == "starting")
            .values(
                status="running",
                worker_id=handshake.worker_id,
                worker_handshake_at=timestamp,
                started_at=timestamp,
                last_heartbeat_at=timestamp,
            )
        ).rowcount
        self.session.commit()
        return changed == 1

    def record_termination_request(
        self,
        run_id: UUID | str,
        *,
        reason: str,
        requested_at: datetime,
    ) -> bool:
        """Write cancellation/timeout audit fields while retaining the state."""

        from sqlalchemy import update

        model = self._model()
        changed = self.session.execute(
            update(model)
            .where(
                model.id == UUID(str(run_id)),
                model.status.in_(tuple(ACTIVE_STATUSES)),
                model.termination_requested_at.is_(None),
            )
            .values(
                termination_requested_at=requested_at,
                termination_reason=reason,
            )
        ).rowcount
        self.session.commit()
        return changed == 1

    def write_terminal(self, run_id: UUID | str, *, status: str, **evidence: Any) -> bool:
        from sqlalchemy import update

        model = self._model()
        allowed = {
            "terminal_status": status,
            "finished_at": evidence.pop("finished_at", _utc_now()),
        }
        # Only mapped runner evidence is forwarded; arbitrary user data must
        # not become a SQL column assignment.
        columns = set(model.__table__.columns.keys())
        allowed.update({key: value for key, value in evidence.items() if key in columns})
        changed = self.session.execute(
            update(model)
            .where(
                model.id == UUID(str(run_id)),
                ~model.status.in_(tuple(TERMINAL_STATUSES)),
            )
            .values(status=status, **allowed)
        ).rowcount
        self.session.commit()
        return changed == 1

    def commit(self) -> None:
        self.session.commit()


class RunnerSupervisor:
    """Own the one active Supervisor and its child process registry."""

    def __init__(
        self,
        *,
        repository: Any,
        lock: PostgresAdvisoryLock,
        launcher: Any | None = None,
        settings: SupervisorSettings | Any | None = None,
        integrity_checker_factory: Callable[[Any], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.repository = repository
        self.lock = lock
        if isinstance(settings, SupervisorSettings):
            self.settings = settings
        elif settings is None:
            self.settings = SupervisorSettings()
        else:
            self.settings = SupervisorSettings.from_settings(settings)
        self.launcher = launcher or WorkerProcessLauncher(memory_limit_mib=self.settings.memory_limit_mib)
        self.integrity_checker_factory = integrity_checker_factory
        self.monotonic = monotonic
        self.clock = clock
        self.children: dict[str, ChildHandle] = {}
        self._stopping = False

    @property
    def active_children(self) -> tuple[ChildHandle, ...]:
        return tuple(self.children.values())

    @property
    def is_leader(self) -> bool:
        return bool(getattr(self.lock, "held", False))

    def _require_lock(self) -> None:
        if not self.is_leader:
            raise SupervisorLockNotHeld(
                "RunnerSupervisor must hold the advisory lock before touching a run"
            )
        assert_held = getattr(self.lock, "assert_held", None)
        if callable(assert_held):
            assert_held()

    def _safe_stop_children_without_lock(self) -> None:
        """Stop known workers without touching durable state after lock loss.

        PostgreSQL releases a session advisory lock when its connection dies.
        Once that happens this process must not claim, update, or finalize any
        run.  It may still use the previously verified process identities to
        request a cooperative group termination; the next Supervisor performs
        the durable recovery scan.
        """

        for handle in tuple(self.children.values()):
            try:
                if process_api.process_identity_matches(handle.identity):
                    process_api.send_graceful_termination(handle.identity)
            except Exception:
                # Identity uncertainty is fail-closed: do not signal an
                # unrelated PID and leave recovery to the next instance.
                continue

    def acquire_lock(self) -> bool:
        acquired = self.lock.acquire()
        if acquired:
            self._log(
                logging.INFO,
                "runner_supervisor_lock_acquired",
                "回测 Supervisor 已取得单实例锁，开始负责队列和终态。",
            )
        return acquired

    def release_lock(self) -> None:
        self.lock.release()

    def _log(self, level: int, event: str, message: str, **fields: Any) -> None:
        # ``message`` is a reserved LogRecord attribute in the stdlib.  Put
        # the Chinese operator summary in the log message itself while the
        # structured event key and technical fields remain in ``extra``.
        extra = {"event": event, **fields}
        logger.log(level, message, extra=extra)

    def _get(self, run_id: UUID | str) -> Any | None:
        for name in ("get", "get_run", "find"):
            method = getattr(self.repository, name, None)
            if callable(method):
                return method(run_id)
        rows = getattr(self.repository, "rows", None)
        if isinstance(rows, Mapping):
            return rows.get(str(run_id), rows.get(run_id))
        return None

    def _list_active(self) -> list[Any]:
        for name in ("list_active", "list_nonterminal", "recover"):
            method = getattr(self.repository, name, None)
            if callable(method):
                rows = method()
                return [row for row in rows if _row_value(row, "status") in ACTIVE_STATUSES]
        return []

    def _list_nonterminal(self) -> list[Any]:
        """Return all non-terminal rows for queued-cancellation handling."""

        for name in ("list_nonterminal", "list_active", "recover"):
            method = getattr(self.repository, name, None)
            if callable(method):
                rows = method()
                return [
                    row
                    for row in rows
                    if _row_value(row, "status") not in TERMINAL_STATUSES
                ]
        return []

    def _commit(self) -> None:
        commit = getattr(self.repository, "commit", None)
        if callable(commit):
            commit()

    def _claim_next(self) -> Any | None:
        self._require_lock()
        method = getattr(self.repository, "claim_next", None)
        row = None
        if callable(method):
            try:
                row = method(formal_first=True)
            except TypeError:
                row = method()
        if row is None:
            queue = getattr(self.repository, "queue", None)
            claim = getattr(queue, "claim", None)
            if callable(claim):
                claimed = claim()
                row = self._get(claimed) if claimed is not None else None
        if row is None:
            return None
        return row

    def _mark_starting(self, row: Any, launch_id: UUID) -> None:
        _set_row_value(row, "status", "starting")
        _set_row_value(row, "launch_id", launch_id)
        _set_row_value(row, "claimed_at", self.clock())
        callback = getattr(self.repository, "mark_starting", None)
        if callable(callback):
            try:
                callback(_row_id(row), launch_id=launch_id, claimed_at=self.clock())
            except TypeError:
                callback(_row_id(row), launch_id)
        self._commit()

    def _record_identity(self, row: Any, identity: LaunchIdentity) -> None:
        _set_row_value(row, "launch_id", identity.launch_id)
        for name, value in (
            ("child_pid", identity.pid),
            ("pid", identity.pid),
            ("process_start_token", identity.start_identity),
            ("child_start_identity", identity.start_identity),
            ("process_group_id", identity.process_group_id),
            ("child_process_group_id", identity.process_group_id),
        ):
            _set_row_value(row, name, value)
        callback = getattr(self.repository, "record_child_identity", None)
        if callable(callback):
            callback(_row_id(row), identity)
        self._commit()

    def _is_cancelled_before_start(self, row: Any) -> bool:
        return _row_value(row, "status") in {"queued", "starting", "cancel_requested"} and (
            bool(_row_value(row, "cancel_requested", False))
            or _row_value(row, "cancel_requested_at") is not None
            or _row_value(row, "status") == "cancel_requested"
        )

    def launch_next(self) -> ChildHandle | None:
        """Claim and launch at most one row, preserving formal priority."""

        self._require_lock()
        if len(self.children) >= self.settings.max_workers:
            return None
        row = self._claim_next()
        if row is None:
            return None
        run_id = _row_id(row)
        if self._is_cancelled_before_start(row):
            self._write_terminal(
                run_id,
                status="cancelled",
                marker=None,
                exit_code=None,
                integrity=None,
                reason="cancelled_before_start",
                failure_phase=None,
                forced=False,
                recovery_action=None,
            )
            return None
        launch_id = uuid4()
        self._mark_starting(row, launch_id)
        _set_row_value(row, "runner_config_evidence", self.settings.as_dict())
        evidence_factory = getattr(self.launcher, "resource_limit_evidence", None)
        if callable(evidence_factory):
            try:
                resource_evidence = evidence_factory()
                _set_row_value(
                    row,
                    "resource_limit_evidence",
                    resource_evidence.as_dict()
                    if hasattr(resource_evidence, "as_dict")
                    else resource_evidence,
                )
            except Exception as exc:
                _set_row_value(
                    row,
                    "resource_limit_evidence",
                    {
                        "resource": "address_space_mib",
                        "requested": self.settings.memory_limit_mib,
                        "supported": False,
                        "applied": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                    },
                )
        self._commit()
        try:
            process = self.launcher.start(run_id, launch_id)
            identity = getattr(process, "launch_identity", None)
            if not isinstance(identity, LaunchIdentity):
                identity = process_api.identity_from_process(
                    process, run_id=run_id, launch_id=launch_id
                )
        except Exception as exc:
            self._write_terminal(
                run_id,
                status="indeterminate",
                marker=None,
                exit_code=None,
                integrity=None,
                reason="worker_launch_failed",
                failure_phase="runner_supervisor_startup",
                failure_type=type(exc).__name__,
                forced=False,
                recovery_action=None,
            )
            self._log(
                logging.ERROR,
                "backtest_worker_launch_failed",
                "回测 worker 启动失败，运行进入不确定终态。",
                run_id=str(run_id),
                failure_type=type(exc).__name__,
            )
            return None
        self._record_identity(row, identity)
        handle = ChildHandle(
            run_id,
            launch_id,
            process,
            identity,
            self.monotonic(),
            self.monotonic() + self.settings.handshake_timeout_seconds,
            StdoutCapture(self.settings.stdout_max_bytes),
            started_at=self.clock(),
        )
        self.children[str(run_id)] = handle
        self._log(
            logging.INFO,
            "backtest_worker_started",
            "回测运行已领取并启动 worker，等待身份 handshake。",
            run_id=str(run_id),
            launch_id=str(launch_id),
            child_pid=identity.pid,
            process_group_id=identity.process_group_id,
            queue_kind=_row_value(row, "run_kind"),
        )
        return handle

    launch_one = launch_next

    def handle_handshake(self, payload: Mapping[str, Any]) -> bool:
        """Accept a handshake only when all launch identity fields match."""

        self._require_lock()
        run_id = payload.get("run_id") if isinstance(payload, Mapping) else None
        handle = self.children.get(str(run_id))
        if handle is None:
            return False
        validation = process_api.validate_handshake(
            payload,
            expected_run_id=handle.run_id,
            expected_launch_id=handle.launch_id,
            expected_pid=handle.identity.pid,
            expected_start_identity=handle.identity.start_identity,
            expected_process_group_id=handle.identity.process_group_id,
        )
        if not validation.valid or validation.handshake is None:
            handle.termination_reason = "worker_handshake_invalid"
            self._terminate(handle, reason=handle.termination_reason, force=False)
            self._write_terminal(
                handle.run_id,
                status="indeterminate",
                marker=None,
                exit_code=None,
                integrity=None,
                reason="worker_handshake_invalid",
                failure_phase="runner_supervisor_startup",
                failure_type="WorkerHandshakeError",
                forced=False,
                recovery_action=None,
                completion_marker_validation={"valid": False, "errors": list(validation.errors)},
            )
            return False
        handle.handshake_received = True
        row = self._get(handle.run_id)
        callback = getattr(self.repository, "record_handshake", None)
        if callable(callback):
            callback(handle.run_id, validation.handshake)
        elif row is not None:
            _set_row_value(row, "status", "running")
            _set_row_value(row, "worker_handshake_at", validation.handshake.observed_at or self.clock())
            _set_row_value(row, "started_at", validation.handshake.observed_at or self.clock())
            _set_row_value(row, "worker_id", validation.handshake.worker_id)
        self._commit()
        self._log(
            logging.INFO,
            "backtest_worker_handshake",
            "回测 worker handshake 已验证，运行进入执行状态。",
            run_id=str(handle.run_id),
            launch_id=str(handle.launch_id),
            child_pid=handle.identity.pid,
            process_group_id=handle.identity.process_group_id,
        )
        return True

    accept_handshake = handle_handshake

    def _record_termination_request(self, handle: ChildHandle, reason: str) -> None:
        """Persist the first termination request before signalling the child."""

        row = self._get(handle.run_id)
        requested_at = _row_value(row, "termination_requested_at") if row is not None else None
        is_new_request = requested_at is None
        if requested_at is None:
            requested_at = self.clock()
            _set_row_value(row, "termination_requested_at", requested_at)
        if row is not None and _row_value(row, "termination_reason") is None:
            _set_row_value(row, "termination_reason", reason)
        callback = getattr(self.repository, "record_termination_request", None)
        if is_new_request and callable(callback):
            try:
                callback(handle.run_id, reason=reason, requested_at=requested_at)
            except TypeError:
                callback(handle.run_id, reason, requested_at)
        self._commit()

    def _terminate(self, handle: ChildHandle, *, reason: str, force: bool) -> bool:
        handle.termination_reason = handle.termination_reason or reason
        row = self._get(handle.run_id)
        if row is not None:
            if _row_value(row, "termination_requested_at") is None:
                _set_row_value(row, "termination_requested_at", self.clock())
            _set_row_value(row, "termination_reason", handle.termination_reason)
            self._commit()
        self._record_termination_request(handle, reason)
        if force:
            sent = process_api.send_force_kill(handle.identity)
            handle.force_kill_sent = sent or handle.force_kill_sent
            handle.forced = True
        else:
            sent = process_api.send_graceful_termination(handle.identity)
            if sent:
                handle.term_sent_at = self.monotonic()
        if sent:
            self._log(
                logging.WARNING if force else logging.INFO,
                "backtest_worker_termination_requested",
                "回测 worker 已收到终止请求。",
                run_id=str(handle.run_id),
                launch_id=str(handle.launch_id),
                termination_reason=reason,
                forced=force,
            )
        return sent

    def _observe_output(self, handle: ChildHandle) -> None:
        process_api.drain_output(handle.process, handle.capture)
        if handle.capture.truncated and handle.termination_reason is None:
            handle.termination_reason = "stdout_limit_exceeded"
            _set_row_value(self._get(handle.run_id), "termination_reason", handle.termination_reason)
            self._terminate(handle, reason=handle.termination_reason, force=False)

    def _row_cancel_requested(self, row: Any) -> bool:
        return bool(_row_value(row, "cancel_requested", False)) or _row_value(row, "cancel_requested_at") is not None or _row_value(row, "status") == "cancel_requested"

    def process_queued_cancellations(self) -> tuple[str, ...]:
        """Close cancellation requests that never acquired a worker.

        The API records a request (and may move the row to
        ``cancel_requested``) but is not allowed to choose a terminal state.
        Scanning before claiming makes the Supervisor the sole owner of this
        queued cancellation exception and prevents a cancelled row from being
        stranded outside the normal claimable queue.
        """

        self._require_lock()
        outcomes: list[str] = []
        for row in self._list_nonterminal():
            status = _row_value(row, "status")
            if status not in {"queued", "cancel_requested"} or not self._row_cancel_requested(row):
                continue
            if any(
                _row_value(row, name) is not None
                for name in (
                    "launch_id",
                    "child_pid",
                    "child_start_identity",
                    "child_process_group_id",
                    "worker_handshake_at",
                )
            ):
                # Once any launch identity exists this is no longer the
                # queued exception; the child registry/recovery path owns it.
                continue
            run_id = _row_id(row)
            changed = self._write_terminal(
                run_id,
                status="cancelled",
                marker=None,
                exit_code=None,
                integrity=None,
                reason="cancelled_before_start",
                failure_phase=None,
                forced=False,
                recovery_action=None,
            )
            if changed:
                outcomes.append(str(run_id))
        return tuple(outcomes)

    def process_cancellations(self) -> None:
        self._require_lock()
        for handle in tuple(self.children.values()):
            row = self._get(handle.run_id)
            if row is None or _row_value(row, "status") in TERMINAL_STATUSES:
                continue
            if not self._row_cancel_requested(row):
                continue
            if handle.term_sent_at is None:
                handle.termination_reason = "cancel_requested"
                self._terminate(handle, reason="cancel_requested", force=False)
                continue
            if not handle.force_kill_sent and self.monotonic() - handle.term_sent_at >= self.settings.cancel_grace_seconds:
                self._terminate(handle, reason="cancel_grace_expired", force=True)

    def process_deadlines(self) -> None:
        self._require_lock()
        now = self.monotonic()
        for handle in tuple(self.children.values()):
            if now - handle.started_monotonic < self.settings.run_timeout_seconds:
                continue
            if handle.term_sent_at is None:
                handle.termination_reason = "wall_clock_timeout"
                self._terminate(handle, reason=handle.termination_reason, force=False)
            elif not handle.force_kill_sent and now - handle.term_sent_at >= self.settings.cancel_grace_seconds:
                self._terminate(handle, reason="wall_clock_timeout_grace_expired", force=True)

    def process_heartbeat_timeouts(self, *, now: datetime | None = None) -> tuple[str, ...]:
        self._require_lock()
        timestamp = now or self.clock()
        lost: list[str] = []
        for handle in tuple(self.children.values()):
            row = self._get(handle.run_id)
            if row is None:
                continue
            last_heartbeat = _row_value(row, "last_heartbeat_at")
            # A worker that never writes its first heartbeat is also lost.
            # Use the durable started_at when available and otherwise the
            # Supervisor's wall-clock launch timestamp as the reference.
            heartbeat_reference = last_heartbeat
            if heartbeat_reference is None:
                heartbeat_reference = _row_value(row, "started_at") or handle.started_at
            if not is_lost_heartbeat(
                last_heartbeat,
                now=timestamp,
                started_at=handle.started_at or _row_value(row, "started_at"),
                lost_heartbeat_seconds=self.settings.lost_heartbeat_seconds,
            ):
                continue
            lost.append(str(handle.run_id))
            handle.failure_phase = "runner_lost_heartbeat"
            if handle.term_sent_at is None:
                handle.termination_reason = "runner_lost_heartbeat"
                self._terminate(handle, reason=handle.termination_reason, force=False)
            elif not handle.force_kill_sent and self.monotonic() - handle.term_sent_at >= self.settings.cancel_grace_seconds:
                self._terminate(handle, reason="runner_lost_heartbeat_grace_expired", force=True)
            _set_row_value(row, "failure_phase", "runner_lost_heartbeat")
            _set_row_value(row, "recovery_action", "terminate_no_restart")
            self._log(
                logging.WARNING,
                "backtest_runner_lost_heartbeat",
                "回测运行连续 60 秒无有效心跳，已进入终止复核流程且不会自动重启。",
                run_id=str(handle.run_id),
                failure_phase="runner_lost_heartbeat",
                recovery_action="terminate_no_restart",
            )
        return tuple(lost)

    def _integrity_for(self, row: Any, marker: Mapping[str, Any] | None) -> Any:
        if marker is None:
            return None
        config_hash = _row_value(row, "config_hash")
        if self.integrity_checker_factory is not None:
            checker = self.integrity_checker_factory(row)
            if hasattr(checker, "verify"):
                return checker.verify(marker)
            if callable(checker):
                return checker()
            return checker
        persisted = _row_value(row, "result_integrity_evidence")
        if persisted is not None:
            return persisted
        provider = _call_repository(self.repository, ("integrity_rows", "read_integrity_rows"), _row_id(row))
        if provider is not None and isinstance(config_hash, str):
            return ResultIntegrityChecker(provider, config_hash=config_hash).verify(marker)
        return None

    def _reconcile_handle(self, handle: ChildHandle) -> str | None:
        self._require_lock()
        self._observe_output(handle)
        poll = getattr(handle.process, "poll", None)
        exit_code = poll() if callable(poll) else _row_value(self._get(handle.run_id), "runner_exit_code")
        if exit_code is None:
            return None
        row = self._get(handle.run_id)
        marker = _row_value(row, "completion_marker") if row is not None else None
        integrity = self._integrity_for(row, marker) if row is not None else None
        evaluation = evaluate_terminal(
            marker=marker,
            exit_code=exit_code,
            integrity=integrity,
            run_id=handle.run_id,
            config_hash=_row_value(row, "config_hash") if row is not None else None,
            forced=handle.forced,
        )
        reason = handle.termination_reason or evaluation.reason
        self._write_terminal(
            handle.run_id,
            status=evaluation.status,
            marker=marker,
            exit_code=exit_code,
            integrity=integrity,
            reason=reason,
            failure_phase=handle.failure_phase,
            failure_type=("RunnerLostHeartbeat" if handle.failure_phase == "runner_lost_heartbeat" else None),
            forced=handle.forced,
            recovery_action="terminate_no_restart" if handle.failure_phase else None,
            stdout_evidence=handle.capture.evidence(),
            completion_marker_validation=(
                validate_completion_marker(
                    marker,
                    run_id=handle.run_id,
                    config_hash=_row_value(row, "config_hash") if row else None,
                ).as_dict()
                if marker is not None
                else None
            ),
        )
        self.children.pop(str(handle.run_id), None)
        self._log(
            logging.INFO if evaluation.status != "indeterminate" else logging.WARNING,
            "backtest_run_terminal",
            f"回测运行已进入{evaluation.status}终态，Supervisor 已完成证据复核。",
            run_id=str(handle.run_id),
            launch_id=str(handle.launch_id),
            exit_code=exit_code,
            exit_category=evaluation.exit_category,
            terminal_decision_reason=reason,
            integrity_status=getattr(integrity, "status", None) if integrity is not None else None,
        )
        return evaluation.status

    def reap_children(self) -> tuple[str, ...]:
        self._require_lock()
        results: list[str] = []
        for handle in tuple(self.children.values()):
            poll = getattr(handle.process, "poll", None)
            if callable(poll) and poll() is None:
                continue
            result = self._reconcile_handle(handle)
            if result is not None:
                results.append(result)
        return tuple(results)

    def _write_terminal(
        self,
        run_id: UUID | str,
        *,
        status: str,
        marker: Mapping[str, Any] | None,
        exit_code: int | None,
        integrity: Any,
        reason: str,
        failure_phase: str | None,
        forced: bool,
        recovery_action: str | None,
        failure_type: str | None = None,
        stdout_evidence: Mapping[str, Any] | None = None,
        completion_marker_validation: Mapping[str, Any] | None = None,
        recovery_observed_at: datetime | None = None,
        recovery_process_state: Mapping[str, Any] | None = None,
    ) -> bool:
        self._require_lock()
        row = self._get(run_id)
        if row is not None and _row_value(row, "status") in TERMINAL_STATUSES:
            return False
        if status not in {"succeeded", "failed", "cancelled", "timed_out", "indeterminate"}:
            status = "indeterminate"
        integrity_dict: Mapping[str, Any] | None = None
        if integrity is not None:
            if hasattr(integrity, "as_dict"):
                integrity_dict = integrity.as_dict()
            elif isinstance(integrity, Mapping):
                integrity_dict = dict(integrity)
        validation_dict = dict(completion_marker_validation or {})
        if marker is not None and not validation_dict:
            validation = validate_completion_marker(
                marker,
                run_id=run_id,
                config_hash=_row_value(row, "config_hash") if row is not None else None,
            )
            validation_dict = {"valid": validation.valid, "errors": list(validation.errors)}
        exit_evidence = (
            map_runner_exit_code(
                exit_code,
                signal_number=(-exit_code if isinstance(exit_code, int) and exit_code < 0 else None),
            )
            if exit_code is not None
            else None
        )
        exit_classification = exit_evidence.category if exit_evidence is not None else None
        detail_errors: list[str] = list(validation_dict.get("errors", ()))
        if integrity_dict is not None:
            integrity_errors = integrity_dict.get("errors", ())
            if isinstance(integrity_errors, str):
                detail_errors.append(integrity_errors)
            elif isinstance(integrity_errors, Iterable):
                detail_errors.extend(str(error) for error in integrity_errors)
        evidence: dict[str, Any] = {
            "terminal_status": status,
            "completion_marker": dict(marker) if isinstance(marker, Mapping) else marker,
            "runner_exit_code": exit_code,
            "runner_exit_code_protocol": EXIT_CODE_PROTOCOL if exit_code is not None else None,
            "runner_exit_category": exit_classification,
            "runner_exit_report": exit_evidence.as_dict() if exit_evidence is not None else None,
            "completion_marker_protocol": marker.get("protocol_version") if isinstance(marker, Mapping) else None,
            "completion_marker_validation": validation_dict or None,
            "result_integrity_evidence": integrity_dict,
            "result_integrity_status": (
                integrity_dict.get("status") if integrity_dict is not None else None
            ),
            "terminal_decision_reason": reason,
            "failure_phase": failure_phase,
            "failure_type": failure_type,
            "recovery_action": recovery_action,
            "recovery_observed_at": recovery_observed_at,
            "finished_at": self.clock(),
            "error_message": ("；".join(detail_errors)[:1_800] if detail_errors else None),
        }
        if recovery_observed_at is not None:
            evidence["recovery_observed_at"] = recovery_observed_at
        if recovery_process_state is not None:
            evidence["recovery_process_state"] = dict(recovery_process_state)
        if integrity_dict is not None:
            counts = integrity_dict.get("counts", integrity_dict.get("actual_counts"))
            if isinstance(counts, Mapping):
                evidence["result_counts"] = dict(counts)
        if stdout_evidence is not None:
            evidence.update(
                {
                    "stdout_bytes": stdout_evidence.get("stdout_bytes", stdout_evidence.get("bytes")),
                    "stdout_digest": stdout_evidence.get("stdout_digest", stdout_evidence.get("digest")),
                    "stdout_truncated": stdout_evidence.get("stdout_truncated", stdout_evidence.get("truncated")),
                }
            )
        callback = getattr(self.repository, "write_terminal", None)
        if callable(callback):
            try:
                changed = bool(callback(run_id, status=status, **evidence))
            except TypeError:
                changed = bool(callback(run_id, status, evidence))
        else:
            changed = False
            if row is not None:
                _set_row_value(row, "status", status)
                for name, value in evidence.items():
                    _set_row_value(row, name, value)
                changed = True
                self._commit()
        if changed:
            self._log(
                logging.INFO,
                "backtest_terminal_written",
                f"回测运行已写入{status}终态，原因已保留为结构化证据。",
                run_id=str(run_id),
                terminal_decision_reason=reason,
                exit_code=exit_code,
            )
        return changed

    def reconcile_run(
        self,
        run_id: UUID | str,
        *,
        marker: Mapping[str, Any] | None,
        exit_code: int | None,
        integrity: Any = None,
        forced: bool = False,
        reason: str | None = None,
        failure_phase: str | None = None,
        recovery_observed_at: datetime | None = None,
        recovery_process_state: Mapping[str, Any] | None = None,
    ) -> str:
        """Reconcile persisted evidence for a child that is already gone."""

        self._require_lock()
        row = self._get(run_id)
        evaluation = evaluate_terminal(
            marker=marker,
            exit_code=exit_code,
            integrity=integrity,
            run_id=run_id,
            config_hash=_row_value(row, "config_hash") if row is not None else None,
            forced=forced,
        )
        self._write_terminal(
            run_id,
            status=evaluation.status,
            marker=marker,
            exit_code=exit_code,
            integrity=integrity,
            reason=reason or evaluation.reason,
            failure_phase=failure_phase,
            forced=forced,
            recovery_action="terminate_no_restart" if failure_phase else None,
            recovery_observed_at=recovery_observed_at,
            recovery_process_state=recovery_process_state,
        )
        return evaluation.status

    def startup_recovery(self) -> tuple[str, ...]:
        """Scan active roots after acquiring the lock without adopting children."""

        self._require_lock()
        # Queue cancellation is resolved before active-run recovery.  It has
        # no process identity and therefore must not be misclassified as an
        # identity failure after a Supervisor restart.
        outcomes: list[str] = list(self.process_queued_cancellations())
        for row in self._list_active():
            run_id = _row_id(row)
            if _row_value(row, "status") in TERMINAL_STATUSES:
                continue
            identity = self._identity_from_row(row)
            recovery_observed_at = self.clock()
            audit = {
                "recovery_action": "recovery_scan",
                "recovery_observed_at": recovery_observed_at.isoformat(),
                "recovery_run_id": str(run_id),
            }
            _set_row_value(row, "recovery_observed_at", recovery_observed_at)
            _set_row_value(row, "recovery_action", "recovery_scan")
            self._commit()
            if identity is None:
                self._write_terminal(
                    run_id,
                    status="indeterminate",
                    marker=_row_value(row, "completion_marker"),
                    exit_code=_row_value(row, "runner_exit_code"),
                    integrity=_row_value(row, "result_integrity_evidence"),
                    reason="recovery_identity_missing",
                    failure_phase="runner_supervisor_recovery",
                    failure_type="identity_unverified",
                    forced=False,
                    recovery_action="identity_unverified",
                    recovery_observed_at=recovery_observed_at,
                    recovery_process_state={"identity_present": False, "state": "unknown"},
                )
                outcomes.append(str(run_id))
                continue
            alive = process_api.is_process_alive(identity.pid)
            same = process_api.process_identity_matches(identity) if alive else False
            if alive and not same:
                # PID reuse is never a valid signal target.
                self._write_terminal(
                    run_id,
                    status="indeterminate",
                    marker=_row_value(row, "completion_marker"),
                    exit_code=_row_value(row, "runner_exit_code"),
                    integrity=_row_value(row, "result_integrity_evidence"),
                    reason="recovery_identity_unverified",
                    failure_phase="runner_supervisor_recovery",
                    failure_type="identity_unverified",
                    forced=False,
                    recovery_action="identity_unverified",
                    recovery_observed_at=recovery_observed_at,
                    recovery_process_state={"identity_present": True, "pid_alive": True, "identity_matches": False},
                )
                outcomes.append(str(run_id))
                continue
            if alive and same:
                # The prior Supervisor's child is an orphan from this process;
                # terminate it, but do not reattach pipes or adopt execution.
                process_api.send_graceful_termination(identity)
                process_api.send_force_kill(identity)
                self._write_terminal(
                    run_id,
                    status="indeterminate",
                    marker=_row_value(row, "completion_marker"),
                    exit_code=_row_value(row, "runner_exit_code"),
                    integrity=_row_value(row, "result_integrity_evidence"),
                    reason="recovery_orphan_terminated",
                    failure_phase="runner_supervisor_recovery",
                    failure_type="orphan_worker",
                    forced=True,
                    recovery_action="terminate_orphan_no_adopt",
                    recovery_observed_at=recovery_observed_at,
                    recovery_process_state={"identity_present": True, "pid_alive": True, "identity_matches": True},
                )
                outcomes.append(str(run_id))
                continue
            # The child already exited.  Only durable exit/marker/integrity
            # evidence may determine the result; no run is launched again.
            outcome = self.reconcile_run(
                run_id,
                marker=_row_value(row, "completion_marker"),
                exit_code=_row_value(row, "runner_exit_code"),
                integrity=_row_value(row, "result_integrity_evidence"),
                forced=False,
                reason="recovery_child_already_exited",
                failure_phase="runner_supervisor_recovery",
                recovery_observed_at=recovery_observed_at,
                recovery_process_state={"identity_present": True, "pid_alive": False, "identity_matches": False},
            )
            outcomes.append(f"{run_id}:{outcome}")
        self._commit()
        self._log(
            logging.INFO,
            "runner_supervisor_recovery",
            f"回测 Supervisor 启动恢复扫描完成，处理 {len(outcomes)} 个非终态运行，未自动重启。",
            recovery_count=len(outcomes),
        )
        return tuple(outcomes)

    recover = startup_recovery

    def _identity_from_row(self, row: Any) -> LaunchIdentity | None:
        run_id = _row_id(row)
        launch_id = _row_value(row, "launch_id")
        pid = _row_value(row, "child_pid", _row_value(row, "pid"))
        start = _row_value(row, "child_start_identity", _row_value(row, "process_start_token"))
        group = _row_value(row, "child_process_group_id", _row_value(row, "process_group_id"))
        if run_id is None or launch_id is None or pid is None or not start or group is None:
            return None
        try:
            return LaunchIdentity(run_id, launch_id, int(pid), str(start), int(group))
        except (TypeError, ValueError):
            return None

    def run_once(self) -> tuple[str, ...]:
        """Run one non-blocking supervision cycle in the frozen order."""

        try:
            self._require_lock()
        except SupervisorLockNotHeld:
            self._safe_stop_children_without_lock()
            raise
        # Observe -> cancellation -> deadlines -> heartbeat timeout -> reap /
        # reconcile -> claim.  Each operation is bounded so one child cannot
        # starve supervision of another active child.
        for handle in tuple(self.children.values()):
            self._observe_output(handle)
            if not handle.handshake_received and self.monotonic() >= handle.handshake_deadline:
                handle.termination_reason = "worker_handshake_timeout"
                self._terminate(handle, reason=handle.termination_reason, force=False)
        self.process_queued_cancellations()
        self.process_cancellations()
        self.process_deadlines()
        self.process_heartbeat_timeouts()
        outcomes = list(self.reap_children())
        while len(self.children) < self.settings.max_workers:
            launched = self.launch_next()
            if launched is None:
                break
        return tuple(outcomes)

    tick = run_once

    def run_forever(
        self,
        *,
        stop_event: Any | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        """Acquire the lock, recover once, and serve until asked to stop."""

        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if not self.acquire_lock():
            self._log(
                logging.INFO,
                "runner_supervisor_lock_busy",
                "已有回测 Supervisor 持有单实例锁，本实例不领取运行。",
            )
            return
        try:
            self.startup_recovery()
            while not self._stopping and not (stop_event is not None and stop_event.is_set()):
                self.run_once()
                time.sleep(poll_interval_seconds)
        except SupervisorLockNotHeld:
            # The session may have disconnected or lost its lock.  Stop only
            # already-identified workers and let the next instance reconcile
            # their durable evidence.
            self._safe_stop_children_without_lock()
        finally:
            self.release_lock()

    def stop(self) -> None:
        self._stopping = True


__all__ = [
    "ACTIVE_STATUSES",
    "ChildHandle",
    "InMemoryRunRepository",
    "RunnerSupervisor",
    "SqlAlchemyRunnerRepository",
    "SupervisorSettings",
    "TERMINAL_STATUSES",
]
