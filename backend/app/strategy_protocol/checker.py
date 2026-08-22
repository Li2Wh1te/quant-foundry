"""Parent-side runner for the isolated startup contract check.

This module spawns ``python -m app.strategy_protocol.worker`` as a separate
process, feeds it one JSON request, and enforces the run protections the
design approved for this phase:

* wall-clock timeout with kill;
* a cancel signal hook;
* a bounded result document (stdout is capped, oversized output fails);
* an optional platform-supported address-space cap passed to the worker.

The checker never executes strategy source inside the caller's process.  A
failed check produces a locatable failure record and the
``strategy_contract_check`` failure phase; it never modifies or rolls back a
published revision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from .contract import FAILURE_PHASE_STRATEGY_CONTRACT_CHECK

DEFAULT_CONTRACT_CHECK_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULT_BYTES = 1_048_576
_CANCEL_POLL_INTERVAL_SECONDS = 0.05


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

    Timeout, cancellation, output overflow, and worker crashes all map to a
    failed :class:`ContractCheckResult` carrying the
    ``strategy_contract_check`` phase; none of them raise into the caller.
    """

    payload = build_worker_payload(request)
    environment = dict(_worker_environment(memory_limit_mb))
    process = subprocess.Popen(
        [
            python_executable or sys.executable,
            "-m",
            "app.strategy_protocol.worker",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        cwd=_backend_root(),
    )
    deadline = time.monotonic() + timeout_seconds
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""
    timed_out = False
    cancelled = False
    input_sent = False
    try:
        while True:
            if should_cancel is not None and should_cancel():
                cancelled = True
                process.kill()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            try:
                # stdin is written by the first communicate call only; retries
                # after a poll-level timeout must not resend the payload.
                stdout_bytes, stderr_bytes = process.communicate(
                    input=payload if not input_sent else None,
                    timeout=min(_CANCEL_POLL_INTERVAL_SECONDS, remaining),
                )
                input_sent = True
                break
            except subprocess.TimeoutExpired:
                input_sent = True
                continue
    finally:
        if process.poll() is None:  # defensive: never leak the worker
            process.kill()
            process.communicate()

    if cancelled:
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckCancelled",
            message="运行契约检查已被取消。",
        )
    if timed_out:
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckTimeout",
            message=f"运行契约检查超过 {timeout_seconds} 秒超时限制，已终止。",
        )

    if len(stdout_bytes) > max_result_bytes:
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckOutputLimit",
            message="运行契约检查输出超过上限，已终止。",
        )
    try:
        decoded = json.loads(stdout_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ContractCheckResult(
            ok=False,
            failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
            error_type="ContractCheckCrashed",
            message="运行契约检查进程异常退出。",
        )
    if decoded.get("ok") is True:
        evidence = decoded.get("evidence", {})
        return ContractCheckResult(ok=True, evidence=evidence)
    failure = decoded.get("failure", {})
    return ContractCheckResult(
        ok=False,
        failure_phase=FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
        error_type=failure.get("error_type"),
        message=failure.get("message"),
        line=failure.get("line"),
    )


def _worker_environment(memory_limit_mb: int | None) -> dict[str, str]:
    """Environment for the worker; carries the optional memory cap.

    The parent's full environment is inherited so ``uv run``-style virtualenv
    resolution keeps working; only the determinism and limit overrides are
    added on top.
    """

    environment = dict(os.environ)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
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
