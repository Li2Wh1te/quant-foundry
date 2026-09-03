"""Fail-closed backtest worker entry point.

Only the Supervisor starts this module.  The command line carries a run ID
and a launch ID, while all frozen strategy/configuration input is loaded by an
injected repository inside the child.  The worker writes facts, progress,
heartbeat and completion evidence; it never writes the final run status.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
import logging
import signal
from types import ModuleType
from typing import Any, Callable, Mapping
from uuid import UUID

from .runner_integrity import IntegrityEvidence
from .runner_process import (
    HANDSHAKE_PROTOCOL,
    ResourceLimitEvidence,
    WorkerHandshake,
    WorkerHandshakeError,
    apply_memory_limit,
    build_handshake,
)
from .runner_protocol import (
    CATEGORY_TO_EXIT_CODE,
    DETERMINATE_CATEGORIES,
    EXIT_CODE_PROTOCOL,
    ExitCategory,
    RESULT_COUNT_KEYS,
    build_completion_marker,
    map_runner_exit_code,
)


logger = logging.getLogger("backtesting.runner.worker")


class WorkerDependencyUnavailable(RuntimeError):
    """Raised when a production worker dependency is not wired."""


@dataclass(frozen=True, slots=True)
class WorkerContext:
    run_id: UUID
    launch_id: UUID
    config_hash: str
    worker_id: str
    handshake: WorkerHandshake
    resource_limit_evidence: ResourceLimitEvidence | None = None


@dataclass(frozen=True, slots=True)
class WorkerExecutionResult:
    """A worker result before protocol marker persistence."""

    category: str
    integrity: IntegrityEvidence | Mapping[str, Any]
    failure_phase: str | None = None
    failure_type: str | None = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if self.category not in DETERMINATE_CATEGORIES:
            raise ValueError("worker result category is not determinate")
        if self.category == ExitCategory.SUCCEEDED.value:
            if self.failure_phase is not None or self.failure_type is not None:
                raise ValueError("successful worker result cannot contain failure evidence")
        elif not self.failure_phase or not self.failure_type:
            raise ValueError("non-success worker result requires failure evidence")


class WorkerSignalState:
    """Signal handlers only request cooperative stop; they do not write status."""

    def __init__(self) -> None:
        self.requested_signal: int | None = None

    @property
    def cancellation_requested(self) -> bool:
        return self.requested_signal in {getattr(signal, "SIGTERM", 15), getattr(signal, "SIGINT", 2)}

    def handler(self, signum: int, _frame: Any) -> None:
        self.requested_signal = signum


def install_signal_handlers(state: WorkerSignalState | None = None) -> WorkerSignalState:
    state = state or WorkerSignalState()
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, state.handler)
    return state


def _identity_value(binding: Any, name: str, default: Any = None) -> Any:
    if isinstance(binding, Mapping):
        return binding.get(name, default)
    return getattr(binding, name, default)


def _integrity_values(integrity: IntegrityEvidence | Mapping[str, Any]) -> tuple[str, Mapping[str, int]]:
    digest = getattr(integrity, "digest", None)
    counts = getattr(integrity, "counts", None)
    if isinstance(integrity, Mapping):
        digest = integrity.get("digest", digest)
        counts = integrity.get("counts", integrity.get("result_counts", counts))
    if not isinstance(digest, str) or not isinstance(counts, Mapping):
        raise WorkerDependencyUnavailable("result integrity evidence is unavailable")
    values = {key: counts.get(key, 0) for key in RESULT_COUNT_KEYS}
    return digest, values


def _invoke_runtime(
    execute: Callable[..., Any],
    binding: Any,
    *,
    context: WorkerContext,
    progress_reporter: Any,
    signal_state: WorkerSignalState,
) -> Any:
    """Call a runtime adapter while preserving a minimal one-argument seam."""

    try:
        signature = inspect.signature(execute)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = {
            name: value
            for name, value in {
                "context": context,
                "progress_reporter": progress_reporter,
                "signal_state": signal_state,
            }.items()
            if accepts_kwargs or name in parameters
        }
        return execute(binding, **kwargs)
    # A callable implemented by a C extension may not expose a signature;
    # retain the smallest supported adapter shape in that case.
    return execute(binding)


def write_worker_handshake(writer: Callable[[Mapping[str, Any]], Any] | None, handshake: WorkerHandshake) -> None:
    """Write handshake evidence through a caller-provided short transaction."""

    if writer is None:
        raise WorkerDependencyUnavailable("worker handshake writer is not configured")
    writer(handshake.as_dict())


def write_completion_marker(
    writer: Callable[[Mapping[str, Any]], Any] | None,
    marker: Mapping[str, Any],
    *,
    result_transaction_committed: bool,
) -> None:
    """Persist a marker only after the result transaction has committed."""

    if not result_transaction_committed:
        raise RuntimeError("completion marker requires a committed result transaction")
    if writer is None:
        raise WorkerDependencyUnavailable("worker completion marker writer is not configured")
    written = writer(dict(marker))
    if written is False:
        # A callback may use ``False`` to report a conditional-write miss
        # (stale launch, terminal row, or a failed transaction).  Treat that
        # as a worker failure so the Supervisor cannot mistake missing marker
        # evidence for a successful completion.
        raise WorkerDependencyUnavailable(
            "worker completion marker was not persisted"
        )


def _start_progress_reporter(reporter: Any) -> None:
    """Start the reporter's heartbeat fallback when the adapter supports it."""

    start = getattr(reporter, "start", None)
    if callable(start):
        start()


def _validate_progress_reporter_identity(
    reporter: Any,
    *,
    run_id: UUID,
    launch_id: UUID,
) -> None:
    """Reject a reporter bound to another run or launch attempt."""

    if reporter is None:
        return
    for name, expected in (("run_id", run_id), ("launch_id", launch_id)):
        observed = getattr(reporter, name, None)
        if observed is not None and str(observed) != str(expected):
            raise WorkerDependencyUnavailable(
                f"progress reporter {name} does not match worker launch"
            )


def _stop_progress_reporter(reporter: Any) -> None:
    """Stop and flush worker progress before completion evidence is written."""

    if reporter is None:
        return
    stop = getattr(reporter, "stop", None)
    if callable(stop):
        try:
            parameters = inspect.signature(stop).parameters
        except (TypeError, ValueError):
            parameters = None
        accepts_flush = parameters is None or "flush" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in (parameters or {}).values()
        )
        if accepts_flush:
            stop(flush=True)
        else:
            # Small legacy test adapters expose ``stop()`` without the
            # protocol keyword.  Signature inspection avoids catching a
            # TypeError raised from inside a real persistence callback and
            # accidentally issuing a duplicate stop/flush.
            stop()
        return
    flush = getattr(reporter, "flush", None)
    if callable(flush):
        flush()


def _load_strategy_module(source_code: str) -> ModuleType:
    """Compile published source only inside the isolated backtest worker."""

    if not isinstance(source_code, str) or not source_code.strip():
        raise WorkerDependencyUnavailable("published strategy source is empty")
    module = ModuleType("published_strategy")
    module.__file__ = "published_strategy"
    exec(compile(source_code, "published_strategy", "exec"), module.__dict__)  # noqa: S102
    return module


def _production_callbacks(run_id: UUID, launch_id: UUID, expected_config_hash: str | None):
    """Build database callbacks for one Supervisor-owned worker launch."""

    from sqlalchemy.orm import Session

    from app.backtesting.models import BacktestRunRecord
    from app.backtesting.production_runtime import binding_from_row, execute_runtime
    from app.backtesting.result_writer import BacktestResultContext, BacktestResultPersistenceService
    from app.core.config import get_settings
    from app.db.session import get_engine
    from app.strategies.models import StrategyRevision

    engine = get_engine()

    def load_binding(_run_id: UUID):
        with Session(engine) as session:
            row = session.get(BacktestRunRecord, _run_id)
            if row is None:
                raise WorkerDependencyUnavailable("persisted run root does not exist")
            if expected_config_hash is not None and row.config_hash != expected_config_hash:
                raise WorkerDependencyUnavailable("persisted run config hash does not match launch")
            binding = binding_from_row(row)
            revision = session.get(StrategyRevision, UUID(str(row.strategy_revision_id)))
            if revision is None:
                raise WorkerDependencyUnavailable("published strategy revision does not exist")
            binding.strategy = dict(binding.strategy)
            binding.strategy["source_code"] = revision.source_code
            binding.strategy["parameter_schema"] = revision.parameter_schema or {}
            binding.strategy["parameters"] = row.parameters or revision.default_parameters or {}
            return binding

    def write_handshake(payload: Mapping[str, Any]):
        from app.backtesting.run_repository import DatabaseRunRepository
        with Session(engine) as session:
            row = session.get(BacktestRunRecord, run_id)
            if row is None or row.launch_id != launch_id or row.status != "starting":
                return False
            changed = DatabaseRunRepository(session).record_handshake(
                run_id,
                WorkerHandshake(
                    run_id=str(payload["run_id"]),
                    launch_id=str(payload["launch_id"]),
                    pid=int(payload["pid"]),
                    start_identity=str(payload["start_identity"]),
                    process_group_id=int(payload["process_group_id"]),
                    protocol_version=str(payload.get("protocol_version", HANDSHAKE_PROTOCOL)),
                    worker_id=payload.get("worker_id"),
                    observed_at=(
                        datetime.fromisoformat(str(payload["observed_at"]))
                        if payload.get("observed_at")
                        else datetime.now(UTC)
                    ),
                ),
            )
            session.commit()
            return bool(changed)

    def write_resource_evidence(payload: Mapping[str, Any]):
        from sqlalchemy import update
        with Session(engine) as session:
            session.execute(
                update(BacktestRunRecord)
                .where(BacktestRunRecord.id == run_id, BacktestRunRecord.launch_id == launch_id)
                .values(resource_limit_evidence=dict(payload))
            )
            session.commit()

    def execute_one(binding, *, context, progress_reporter=None, signal_state=None):
        del signal_state
        with Session(engine) as session:
            return execute_runtime(
                binding,
                session=session,
                launch_id=launch_id,
                strategy_module=_load_strategy_module(binding.strategy["source_code"]),
                worker_id=context.worker_id,
                progress_reporter=progress_reporter,
            )

    def persist_results(raw_result):
        # execute_runtime writes result rows in savepoints; this outer commit
        # is the transaction boundary that makes the marker eligible.
        return True

    def write_marker(payload: Mapping[str, Any]):
        with Session(engine) as session:
            row = session.get(BacktestRunRecord, run_id)
            if row is None:
                return False
            context = BacktestResultContext(
                run_id=run_id,
                run_kind=row.run_kind,
                profile=row.profile,
                config_hash=row.config_hash,
                owner_scope=row.tenant_id,
                launch_id=launch_id,
            )
            writer = BacktestResultPersistenceService(session, context)
            writer.record_completion_marker(payload, exit_code=0)
            session.commit()
            return True

    return load_binding, execute_one, write_handshake, persist_results, write_marker, write_resource_evidence


def run_worker(
    run_id: UUID | str,
    launch_id: UUID | str,
    *,
    load_binding: Callable[[UUID], Any] | None,
    execute: Callable[..., Any] | None,
    write_handshake: Callable[[Mapping[str, Any]], Any] | None,
    persist_results: Callable[[Any], Any] | None,
    write_marker: Callable[[Mapping[str, Any]], Any] | None,
    expected_config_hash: str | None = None,
    worker_id: str | None = None,
    memory_limit_mib: int | None = None,
    progress_reporter: Any = None,
    signal_state: WorkerSignalState | None = None,
    write_resource_evidence: Callable[[Mapping[str, Any]], Any] | None = None,
) -> int:
    """Execute one injected frozen binding and emit only worker-owned evidence.

    This function is intentionally explicit about its dependencies.  A
    missing loader, runtime, result writer, or marker writer fails closed with
    a non-success OS code and no fabricated completion marker.
    """

    try:
        parsed_run_id = UUID(str(run_id))
        parsed_launch_id = UUID(str(launch_id))
    except (TypeError, ValueError) as exc:
        raise WorkerHandshakeError("run_id and launch_id must be UUIDs") from exc
    if load_binding is None or execute is None or persist_results is None or write_marker is None:
        logger.error(
            "回测 worker 依赖未配置，运行已安全拒绝且未写入伪造完成标记。",
            extra={
                "event": "backtest_worker_dependency_unavailable",
                "run_id": str(parsed_run_id),
                "launch_id": str(parsed_launch_id),
                "exit_code": CATEGORY_TO_EXIT_CODE[ExitCategory.FAILED.value],
            },
        )
        return CATEGORY_TO_EXIT_CODE[ExitCategory.FAILED.value]

    binding = load_binding(parsed_run_id)
    binding_run_id = _identity_value(binding, "run_id", _identity_value(binding, "id"))
    if binding_run_id is not None and str(binding_run_id) != str(parsed_run_id):
        raise WorkerDependencyUnavailable("frozen binding run_id does not match launch")
    binding_launch_id = _identity_value(binding, "launch_id")
    if binding_launch_id is not None and str(binding_launch_id) != str(parsed_launch_id):
        raise WorkerDependencyUnavailable("frozen binding launch_id does not match launch")
    binding_status = _identity_value(binding, "status")
    if binding_status is not None and binding_status not in {
        "starting",
        "running",
        "cancel_requested",
    }:
        raise WorkerDependencyUnavailable("worker launch is not in an executable state")
    config_hash = _identity_value(binding, "config_hash")
    if not isinstance(config_hash, str) or len(config_hash) != 64:
        raise WorkerDependencyUnavailable("frozen binding config_hash is missing")
    if expected_config_hash is not None and config_hash != expected_config_hash:
        raise WorkerDependencyUnavailable("frozen binding config_hash does not match launch evidence")

    # Apply resource limits in this process only; the launcher records the
    # platform evidence separately.  ``RLIMIT_AS`` is best effort and never a
    # reason to claim a limit was applied when the platform lacks it.
    resource_evidence = apply_memory_limit(memory_limit_mib)
    logger.info(
        "回测 worker 资源限制已记录，平台能力按实际结果保留。",
        extra={
            "event": "backtest_worker_resource_limit",
            "run_id": str(parsed_run_id),
            "resource_limit_evidence": resource_evidence.as_dict(),
        },
    )
    if write_resource_evidence is not None:
        write_resource_evidence(resource_evidence.as_dict())

    state = install_signal_handlers(signal_state)
    handshake = build_handshake(
        run_id=parsed_run_id,
        launch_id=parsed_launch_id,
        worker_id=worker_id or f"backtest-worker:{parsed_run_id}",
        observed_at=datetime.now(UTC),
    )
    write_worker_handshake(write_handshake, handshake)
    context = WorkerContext(
        parsed_run_id,
        parsed_launch_id,
        config_hash,
        worker_id or f"backtest-worker:{parsed_run_id}",
        handshake,
        resource_evidence,
    )

    reporter_started = False
    try:
        if progress_reporter is not None:
            _validate_progress_reporter_identity(
                progress_reporter,
                run_id=parsed_run_id,
                launch_id=parsed_launch_id,
            )
            _start_progress_reporter(progress_reporter)
            reporter_started = True
        # The existing runtime is passed in by the application boundary; this
        # worker does not import strategy source or reconstruct mutable config.
        raw_result = _invoke_runtime(
            execute,
            binding,
            context=context,
            progress_reporter=progress_reporter,
            signal_state=state,
        )
        if isinstance(raw_result, WorkerExecutionResult):
            result = raw_result
        elif isinstance(raw_result, Mapping):
            result = WorkerExecutionResult(
                str(raw_result.get("category", raw_result.get("status", "failed"))),
                raw_result.get("integrity", raw_result.get("result_integrity", {})),
                raw_result.get("failure_phase"),
                raw_result.get("failure_type"),
                raw_result.get("exit_code"),
            )
        else:
            raise WorkerDependencyUnavailable("worker runtime returned no protocol result")
        if state.cancellation_requested and result.category == ExitCategory.SUCCEEDED.value:
            raise WorkerDependencyUnavailable("worker received cancellation before successful completion")
        # ``False`` is an explicit transaction-failed signal.  ``None`` is
        # retained as a compatible success value for existing repository
        # adapters whose commit method has no return value.
        persisted = persist_results(raw_result)
        if persisted is False:
            raise WorkerDependencyUnavailable("result transaction was not committed")
        # Stop the reporter only after the result transaction has completed.
        # Its final flush therefore precedes marker computation and prevents
        # a late heartbeat from changing the run root after marker commit.
        _stop_progress_reporter(progress_reporter)
        reporter_started = False
        digest, counts = _integrity_values(result.integrity)
        exit_code = (
            result.exit_code
            if result.exit_code is not None
            else CATEGORY_TO_EXIT_CODE[result.category]
        )
        classification = map_runner_exit_code(exit_code)
        if not classification.mapped or classification.category != result.category:
            # A worker cannot declare one business category while returning a
            # different protocol code; the Supervisor must otherwise classify
            # the launch as indeterminate.  Reject the pair before marker
            # persistence so no inconsistent evidence is published.
            raise WorkerDependencyUnavailable(
                "worker result category conflicts with runner_exit_code@1"
            )
        marker = build_completion_marker(
            run_id=parsed_run_id,
            declared_category=result.category,
            digest=digest,
            result_counts=counts,
            failure_phase=result.failure_phase,
            failure_type=result.failure_type,
            config_hash=config_hash,
        )
        # The persistence callback must commit result rows before this call;
        # its contract is represented by this explicit boolean argument.
        write_completion_marker(
            write_marker,
            marker,
            result_transaction_committed=True,
        )
        logger.info(
            "回测 worker 已完成结果写入和完成标记，等待 Supervisor 复核终态。",
            extra={
                "event": "backtest_worker_completed",
                "run_id": str(parsed_run_id),
                "launch_id": str(parsed_launch_id),
                "exit_code": exit_code,
                "exit_category": result.category,
                "completion_marker_protocol": marker["protocol_version"],
            },
        )
        return exit_code
    except Exception as exc:
        if reporter_started:
            try:
                _stop_progress_reporter(progress_reporter)
            except Exception:
                logger.warning(
                    "回测 worker 关闭进度报告器失败，继续保留未知终态证据。",
                    exc_info=True,
                    extra={
                        "event": "backtest_worker_progress_shutdown_failed",
                        "run_id": str(parsed_run_id),
                        "launch_id": str(parsed_launch_id),
                    },
                )
        logger.exception(
            "回测 worker 执行失败，未直接写入 Supervisor 终态。",
            extra={
                "event": "backtest_worker_failed",
                "run_id": str(parsed_run_id),
                "launch_id": str(parsed_launch_id),
                "failure_type": type(exc).__name__,
            },
        )
        return CATEGORY_TO_EXIT_CODE[ExitCategory.FAILED.value]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quant Foundry backtest worker")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--launch-id", required=True)
    # Deliberately no strategy/config/token arguments are accepted here.
    args = parser.parse_args(argv)
    try:
        UUID(args.run_id)
        UUID(args.launch_id)
    except ValueError as exc:
        parser.error("--run-id and --launch-id must be UUIDs")
    return args


def main(argv: list[str] | None = None) -> int:
    """Load the frozen run and execute it inside this isolated child."""

    args = _parse_args(argv)
    run_id = UUID(args.run_id)
    launch_id = UUID(args.launch_id)
    from sqlalchemy.orm import Session

    from app.backtesting.models import BacktestRunRecord
    from app.backtesting.runner_progress import DatabaseProgressPersistence, ProgressReporter
    from app.core.config import get_settings
    from app.db.session import get_engine

    engine = get_engine()
    with Session(engine) as session:
        row = session.get(BacktestRunRecord, run_id)
        if row is None:
            logger.error(
                "回测 worker 找不到持久化运行，已拒绝执行。",
                extra={"event": "backtest_worker_dependency_unavailable", "run_id": args.run_id, "launch_id": args.launch_id},
            )
            return CATEGORY_TO_EXIT_CODE[ExitCategory.FAILED.value]
        expected_hash = row.config_hash

    (
        load_binding,
        execute,
        write_handshake,
        persist_results,
        write_marker,
        write_resource_evidence,
    ) = _production_callbacks(run_id, launch_id, expected_hash)
    settings = get_settings()
    progress_persistence = DatabaseProgressPersistence(lambda: Session(engine))
    reporter = ProgressReporter(
        run_id,
        launch_id=launch_id,
        worker_id=f"backtest-worker:{run_id}",
        persist_progress=progress_persistence.persist_progress,
        persist_heartbeat=progress_persistence.persist_heartbeat,
        heartbeat_max_interval_seconds=settings.backtest_heartbeat_max_interval_seconds,
        progress_persist_interval_seconds=settings.backtest_progress_persist_interval_seconds,
        lost_heartbeat_seconds=settings.backtest_lost_heartbeat_seconds,
    )
    return run_worker(
        run_id,
        launch_id,
        load_binding=load_binding,
        execute=execute,
        write_handshake=write_handshake,
        persist_results=persist_results,
        write_marker=write_marker,
        expected_config_hash=expected_hash,
        worker_id=f"backtest-worker:{run_id}",
        memory_limit_mib=settings.backtest_memory_limit_mib,
        progress_reporter=reporter,
        write_resource_evidence=write_resource_evidence,
    )


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(main())


__all__ = [
    "HANDSHAKE_PROTOCOL",
    "WorkerContext",
    "WorkerDependencyUnavailable",
    "WorkerExecutionResult",
    "WorkerSignalState",
    "install_signal_handlers",
    "main",
    "run_worker",
    "write_completion_marker",
    "write_worker_handshake",
]
