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
    writer(dict(marker))


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

    try:
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
        digest, counts = _integrity_values(result.integrity)
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
        exit_code = result.exit_code if result.exit_code is not None else CATEGORY_TO_EXIT_CODE[result.category]
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
    """Production entry point remains closed until dependency wiring is present."""

    args = _parse_args(argv)
    logger.error(
        "回测 worker 正式依赖尚未接入，已拒绝执行策略源码。",
        extra={
            "event": "backtest_worker_not_configured",
            "run_id": args.run_id,
            "launch_id": args.launch_id,
            "exit_code": CATEGORY_TO_EXIT_CODE[ExitCategory.FAILED.value],
        },
    )
    return CATEGORY_TO_EXIT_CODE[ExitCategory.FAILED.value]


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
