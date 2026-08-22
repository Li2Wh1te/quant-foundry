"""Parent-side runner for the isolated startup contract check.

This module spawns ``python -m app.strategy_protocol.worker`` as a separate
process, feeds it one JSON request, and enforces the run protections the
design approved for this phase:

* wall-clock timeout that kills the whole worker process tree (the worker
  starts in its own POSIX session so grandchildren die with it);
* a cancel signal hook with the same tree-kill guarantee;
* streaming byte caps on the result document and stderr, so neither the
  strategy nor a noisy child can grow the parent's memory without bound;
* a dedicated result file descriptor separate from the strategy-visible
  stdout/stderr pipes, plus strict validation of the complete result
  protocol and the worker exit code;
* an optional platform-supported address-space cap passed to the worker.

The checker never executes strategy source inside the caller's process.  A
failed check produces a locatable failure record and the
``strategy_contract_check`` failure phase; it never modifies or rolls back a
published revision.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from .contract import STRATEGY_CONTRACT_VERSION, FAILURE_PHASE_STRATEGY_CONTRACT_CHECK

DEFAULT_CONTRACT_CHECK_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULT_BYTES = 1_048_576
MAX_STDERR_BYTES = 262_144
"""Streaming cap on captured worker stderr before the check is failed."""
_CANCEL_POLL_INTERVAL_SECONDS = 0.05
_CLEANUP_JOIN_SECONDS = 2.0
"""Wall-clock budget for reaping a killed process tree during cleanup."""


@dataclass(frozen=True, slots=True)
class ContractCheckRequest:
    """Everything the isolated worker needs for one deterministic check."""

    source_code: str
    parameter_schema: Mapping[str, Any]
    default_parameters: Mapping[str, Any]
    static_instrument_ids: tuple[str, ...] = ()
    initial_positions: tuple[Mapping[str, Any], ...] = ()
    session_date: date | None = None
    decision_time: datetime | None = None
    data_cutoff: datetime | None = None


@dataclass(frozen=True, slots=True)
class ContractCheckResult:
    """Outcome of one contract check, safe to persist as run evidence."""

    ok: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)
    failure_phase: str | None = None
    error_type: str | None = None
    message: str | None = None
    line: int | None = None
    technical: str | None = None


SYNTHETIC_FALLBACK_TIMEZONE = timezone(timedelta(hours=8))
"""Fixed fallback timezone; never derived from the host clock or locale."""


def _default_session_dates() -> tuple[date, datetime]:
    """Deterministic fallback session built from fixed constants.

    The default uses a fixed synthetic date instead of the wall clock so the
    check inputs stay reproducible even when callers omit explicit dates.
    """

    fixed_day = date(2030, 1, 15)
    fixed_time = datetime(2030, 1, 15, 15, 0, 0, tzinfo=SYNTHETIC_FALLBACK_TIMEZONE)
    return fixed_day, fixed_time


def _jsonify(value: Any) -> Any:
    """Convert domain values (UUID, Decimal, date) into JSON-safe forms."""

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        # Decimal crosses the boundary as an exact decimal string.
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


def build_worker_payload(request: ContractCheckRequest) -> bytes:
    """Serialize the request into the worker's stdin JSON document."""

    if request.session_date is not None:
        session_date = request.session_date
    else:
        session_date, _ = _default_session_dates()
    decision_time = request.decision_time or datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        15,
        0,
        0,
        tzinfo=SYNTHETIC_FALLBACK_TIMEZONE,
    )
    data_cutoff = request.data_cutoff or decision_time
    payload = {
        "source_code": request.source_code,
        "parameter_schema": _jsonify(dict(request.parameter_schema)),
        "default_parameters": _jsonify(dict(request.default_parameters)),
        "static_instrument_ids": [str(value) for value in request.static_instrument_ids],
        "initial_positions": [_jsonify(dict(row)) for row in request.initial_positions],
        "session_date": session_date.isoformat(),
        # Serialized with offset so the worker receives tz-aware timestamps.
        "decision_time": decision_time.isoformat(),
        "data_cutoff": data_cutoff.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def run_strategy_contract_check(
    request: ContractCheckRequest,
    *,
    timeout_seconds: float = DEFAULT_CONTRACT_CHECK_TIMEOUT_SECONDS,
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
    memory_limit_mb: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
    python_executable: str | None = None,
) -> ContractCheckResult:
    """Run the isolated worker once and return its structured outcome.

    Timeout, cancellation, output overflow, protocol violations, and worker
    crashes all map to a failed :class:`ContractCheckResult` carrying the
    ``strategy_contract_check`` phase; none of them raise into the caller.
    """

    payload = build_worker_payload(request)
    # The worker returns its result on a dedicated descriptor that is not the
    # strategy-visible stdout, so stray writes to fd 1 cannot forge results.
    # Worker stdout itself carries nothing the parent needs (the strategy's
    # prints are forwarded to stderr by the bounded redirect), so it goes to
    # DEVNULL: an unwritten pipe can never block a noisy strategy.
    result_read_fd, result_write_fd = os.pipe()
    environment = _worker_environment(memory_limit_mb)
    environment["QF_CONTRACT_CHECK_RESULT_FD"] = str(result_write_fd)
    try:
        process = subprocess.Popen(
            [
                python_executable or sys.executable,
                "-m",
                "app.strategy_protocol.worker",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(result_write_fd,),
            env=environment,
            cwd=_backend_root(),
            start_new_session=(os.name == "posix"),
        )
    except BaseException:
        os.close(result_read_fd)
        os.close(result_write_fd)
        raise
    os.close(result_write_fd)
    return _supervise_worker(
        process,
        request_payload=payload,
        result_read_fd=result_read_fd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        max_result_bytes=max_result_bytes,
        should_cancel=should_cancel,
    )


def _supervise_worker(
    process: subprocess.Popen,
    *,
    request_payload: bytes,
    result_read_fd: int,
    environment: dict[str, str],
    timeout_seconds: float,
    max_result_bytes: int,
    should_cancel: Callable[[], bool] | None,
) -> ContractCheckResult:
    """Stream the worker's pipes under byte caps until a terminal condition.

    All reads are non-blocking and size-capped, so neither the strategy nor a
    grandchild process holding the inherited descriptors can block the parent
    indefinitely or grow its memory without bound.
    """

    deadline = time.monotonic() + timeout_seconds
    os.set_blocking(result_read_fd, False)
    assert process.stderr is not None
    os.set_blocking(process.stderr.fileno(), False)

    result_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    result_bytes = 0
    stderr_bytes = 0
    stdin_written = False
    timed_out = False
    cancelled = False
    result_overflow = False
    stderr_overflow = False
    result_open = True
    stderr_open = True
    try:
        while True:
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not stdin_written:
                try:
                    process.stdin.write(request_payload)  # type: ignore[union-attr]
                    process.stdin.close()  # type: ignore[union-attr]
                    stdin_written = True
                except BrokenPipeError:
                    # Worker died before reading stdin; the exit-code check
                    # below reports the crash.
                    stdin_written = True
            watch_fds: list[int] = []
            if result_open and not result_overflow:
                watch_fds.append(result_read_fd)
            if stderr_open and not stderr_overflow:
                watch_fds.append(process.stderr.fileno())  # type: ignore[union-attr]
            if process.poll() is not None and not watch_fds:
                break
            readable, _, _ = select.select(
                watch_fds, [], [], min(_CANCEL_POLL_INTERVAL_SECONDS, remaining)
            )
            for readable_fd in readable:
                if readable_fd == result_read_fd:
                    result_bytes, result_overflow, result_open = _drain_fd(
                        readable_fd, result_chunks, result_bytes, max_result_bytes, result_open
                    )
                else:
                    stderr_bytes, stderr_overflow, stderr_open = _drain_fd(
                        readable_fd, stderr_chunks, stderr_bytes, MAX_STDERR_BYTES, stderr_open
                    )
            if result_overflow or stderr_overflow:
                break
            if (
                process.poll() is not None
                and stdin_written
                and not result_open
                and not stderr_open
            ):
                break
    finally:
        exit_code = _terminate_process_tree(process)
        # The result channel must close on every exit path; EOF-driven
        # closing only happens when the loop drains it to the end, so early
        # exits (timeout, cancel, overflow) would otherwise leak the fd.
        if result_open:
            try:
                os.close(result_read_fd)
            except OSError:
                pass

    stderr_tail = _stderr_tail(b"".join(stderr_chunks))

    if cancelled:
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckCancelled",
            message="运行契约检查已被取消。",
            technical=stderr_tail,
        )
    if timed_out:
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckTimeout",
            message=f"运行契约检查超过 {timeout_seconds} 秒超时限制，已终止。",
            technical=stderr_tail,
        )
    if result_overflow:
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckOutputLimit",
            message="运行契约检查结果超过上限，已终止。",
            technical=stderr_tail,
        )
    if stderr_overflow:
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckOutputLimit",
            message="运行契约检查 stderr 超过上限，已终止。",
            technical=stderr_tail,
        )

    # A worker that exits nonzero never produced a trustworthy result, even
    # if a partial document arrived on the result channel.
    if exit_code != 0:
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckCrashed",
            message="运行契约检查进程异常退出。",
            technical=stderr_tail,
        )
    try:
        decoded = json.loads(b"".join(result_chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckCrashed",
            message="运行契约检查未返回有效的结果文档。",
            technical=stderr_tail,
        )
    return _validate_result_document(decoded, stderr_tail)


def _drain_fd(
    fd: int, chunks: list[bytes], total: int, cap: int, is_open: bool
) -> tuple[int, bool, bool]:
    """Read whatever is available from one non-blocking fd under a cap.

    Returns the running byte total, whether the cap was exceeded, and whether
    the descriptor is still open (EOF marks it closed so the supervisor never
    busy-loops on an exhausted pipe).
    """

    if not is_open:
        return total, False, False
    while True:
        try:
            chunk = os.read(fd, 65_536)
        except (BlockingIOError, InterruptedError):
            return total, False, True
        except OSError:
            return total, False, False
        if not chunk:
            # EOF: the writer closed its end.
            try:
                os.close(fd)
            except OSError:
                pass
            return total, False, False
        total += len(chunk)
        if total > cap:
            return total, True, True
        chunks.append(chunk)


def _terminate_process_tree(process: subprocess.Popen) -> int | None:
    """Kill the worker and any inherited-descriptor grandchildren, then reap.

    The worker runs in its own POSIX session, so killing the process group
    takes down grandchildren that would otherwise keep the pipes open.  The
    reaping phase itself is bounded so cleanup can never block the caller.
    """

    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    else:  # pragma: no cover - Windows path
        process.kill()
    try:
        return process.wait(timeout=_CLEANUP_JOIN_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        process.kill()
        try:
            return process.wait(timeout=_CLEANUP_JOIN_SECONDS)
        except subprocess.TimeoutExpired:
            return None
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except (OSError, ValueError):
                pass


def _validate_result_document(
    decoded: object, stderr_tail: str | None
) -> ContractCheckResult:
    """Enforce the complete worker result protocol before trusting it.

    A success verdict is only accepted from a fully shaped document with the
    required evidence fields; anything else is treated as a crashed check.
    """

    if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
        return _protocol_violation(stderr_tail)
    if decoded["ok"] is False:
        failure = decoded.get("failure")
        if not isinstance(failure, dict) or not isinstance(
            failure.get("error_type"), str
        ):
            return _protocol_violation(stderr_tail)
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type=failure.get("error_type"),
            message=failure.get("message"),
            line=failure.get("line"),
            technical=failure.get("traceback") or stderr_tail,
        )

    evidence = decoded.get("evidence")
    if not isinstance(evidence, dict):
        return _protocol_violation(stderr_tail)
    if evidence.get("contract_version") != STRATEGY_CONTRACT_VERSION:
        return _protocol_violation(stderr_tail)
    if not isinstance(evidence.get("mode"), str) or not evidence["mode"]:
        return _protocol_violation(stderr_tail)
    target_count = evidence.get("target_count")
    if isinstance(target_count, bool) or not isinstance(target_count, int) or target_count < 0:
        return _protocol_violation(stderr_tail)
    if not isinstance(evidence.get("session_dates"), list):
        return _protocol_violation(stderr_tail)
    identity_rows = evidence.get("identity_rows")
    if not isinstance(identity_rows, list):
        return _protocol_violation(stderr_tail)
    for row in identity_rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(field_name), str)
            for field_name in ("instrument_id", "trading_code", "name", "display_name")
        ):
            return _protocol_violation(stderr_tail)
    return ContractCheckResult(ok=True, evidence=evidence)


def _protocol_violation(stderr_tail: str | None) -> ContractCheckResult:
    """Map a malformed result document to a failed check."""

    return ContractCheckResult(
        ok=False,
        failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
        error_type="ContractCheckCrashed",
        message="运行契约检查结果协议不完整，已拒绝。",
        technical=stderr_tail,
    )


MAX_TECHNICAL_DETAIL_CHARS = 4_000
"""Upper bound on stderr/traceback detail kept in a check result."""


def _stderr_tail(stderr_bytes: bytes) -> str | None:
    """Return a bounded worker stderr tail as expandable technical detail."""

    if not stderr_bytes:
        return None
    rendered = stderr_bytes.decode("utf-8", errors="replace").strip()
    if len(rendered) > MAX_TECHNICAL_DETAIL_CHARS:
        rendered = rendered[-MAX_TECHNICAL_DETAIL_CHARS:]
    return rendered or None


def _worker_environment(memory_limit_mb: int | None) -> dict[str, str]:
    """Environment for the worker; carries the optional memory cap.

    The parent's full environment is inherited so ``uv run``-style virtualenv
    resolution keeps working; only the determinism and limit overrides are
    added on top.
    """

    environment = dict(os.environ)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    # Marks the child as a real contract-check worker; the worker refuses to
    # compile strategy source without this marker plus its __main__ role.
    environment["QF_CONTRACT_CHECK_WORKER"] = "1"
    if memory_limit_mb is not None:
        environment["QF_CONTRACT_CHECK_MEM_MB"] = str(memory_limit_mb)
    return environment


def _backend_root() -> str:
    """Working directory that makes ``app.*`` importable in the worker."""

    # checker.py lives at <backend>/app/strategy_protocol/checker.py.
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


__all__ = [
    "ContractCheckRequest",
    "ContractCheckResult",
    "build_worker_payload",
    "run_strategy_contract_check",
]
