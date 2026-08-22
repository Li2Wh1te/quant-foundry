"""Worker entry point for the isolated startup contract check.

Run as ``python -m app.strategy_protocol.worker``.  The parent checker sends
one JSON request document on stdin; this worker loads the published strategy
source, builds the deterministic synthetic ``DecisionContext``, calls
``run(context, parameters)`` once, validates the returned payload, and writes
exactly one JSON result line to stdout.

The process is deliberately self-contained: it never touches the database,
the network, or any mutable external state beyond what the request contains.
Strategy output is redirected to stderr so stdout carries only the single
machine-readable result document.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from uuid import UUID

if os.name == "posix" and os.environ.get("QF_CONTRACT_CHECK_MEM_MB"):
    # Best-effort address-space cap; applied before importing app modules so
    # even imports run under the limit where the platform supports it.
    import resource

    _limit_bytes = int(os.environ["QF_CONTRACT_CHECK_MEM_MB"]) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (_limit_bytes, _limit_bytes))

from types import ModuleType

from app.strategy_protocol.adapter import FunctionStrategyAdapter  # noqa: E402
from app.strategy_protocol.contract import (  # noqa: E402
    STRATEGY_CONTRACT_VERSION,
)
from app.strategy_protocol.synthetic import (  # noqa: E402
    SYNTHETIC_SESSION_OFFSETS,
    ContractCheckParameters,
    SyntheticIdentityRow,
    build_synthetic_context,
)

MAX_STRATEGY_OUTPUT_BYTES = 65_536
"""Upper bound on bytes a strategy may print during the contract check."""

MAX_TRACEBACK_CHARS = 4_000
"""Upper bound on the technical traceback tail returned with a failure."""

STRATEGY_MODULE_NAME = "published_strategy"
"""Filename recorded for compiled strategy source, used for line lookups."""


STRATEGY_MODULE_PATH = "app.strategy_protocol.worker"
"""Dotted import path of this module, used for the worker-role check.

When run via ``python -m``, this file executes as ``__main__`` and its
``__spec__.name`` records this dotted path.
"""


def _require_worker_process() -> None:
    """Refuse to run unless this process is the isolated contract-check worker.

    Two independent signals must hold: the process must have been started as
    ``python -m app.strategy_protocol.worker`` (its ``__main__`` spec matches
    :data:`STRATEGY_MODULE_PATH`), and the parent checker must have set the
    worker marker environment variable.  Importing this module from an API,
    Runner, or test process does not satisfy either condition.
    """

    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    started_as_worker = (
        main_spec is not None and main_spec.name == STRATEGY_MODULE_PATH
    )
    marked_by_parent = os.environ.get("QF_CONTRACT_CHECK_WORKER") == "1"
    if not (started_as_worker and marked_by_parent):
        raise RuntimeError(
            "策略源码只能在隔离的回测契约检查子进程中编译执行。"
        )


def _load_published_module(source_code: str) -> ModuleType:
    """Compile and execute published strategy source inside this worker.

    Private on purpose: this is the only ``exec`` path in the codebase and it
    is unreachable unless :func:`_require_worker_process` passes, so no API
    or Runner process can load user source through an importable helper.
    """

    _require_worker_process()
    module = ModuleType(STRATEGY_MODULE_NAME)
    compiled = compile(source_code, STRATEGY_MODULE_NAME, "exec")
    exec(compiled, module.__dict__)  # noqa: S102 - isolated subprocess only
    return module


class _BoundedStderrWriter:
    """Strategy-facing stdout replacement that forwards to stderr.

    Strategy ``print`` output must never mix into the JSON result document on
    real stdout, so it is redirected to stderr under a byte budget; exceeding
    the budget fails the check instead of growing without bound.
    """

    def __init__(self, underlying, limit: int) -> None:
        self._underlying = underlying
        self._limit = limit
        self._written = 0

    def write(self, text: str) -> int:
        self._written += len(text.encode("utf-8"))
        if self._written > self._limit:
            raise RuntimeError(
                f"strategy output exceeded {self._limit} bytes during the "
                "contract check"
            )
        return self._underlying.write(text)

    def flush(self) -> None:
        self._underlying.flush()


def _parse_request(raw: bytes) -> dict:
    request = json.loads(raw.decode("utf-8"))
    if not isinstance(request, dict):
        raise ValueError("contract-check request must be a JSON object")
    return request


def _parse_initial_positions(rows: list) -> tuple[dict, ...]:
    """Decode position rows whose instrument ids arrive as UUID strings."""

    parsed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each initial position must be a JSON object")
        decoded = dict(row)
        decoded["instrument_id"] = UUID(str(decoded.get("instrument_id")))
        parsed.append(decoded)
    return tuple(parsed)


def _identity_row_to_evidence(row: SyntheticIdentityRow) -> dict:
    return {
        "instrument_id": str(row.instrument_id),
        "trading_code": row.trading_code,
        "name": row.name,
        "display_name": row.display_name,
    }


def perform_contract_check(request: dict) -> dict:
    """Execute the full check and return the JSON-serializable result."""

    static_ids = tuple(UUID(value) for value in request.get("static_instrument_ids", []))
    initial_positions = _parse_initial_positions(request.get("initial_positions", []))
    parameters = ContractCheckParameters(
        session_date=date.fromisoformat(request["session_date"]),
        decision_time=datetime.fromisoformat(request["decision_time"]),
        data_cutoff=datetime.fromisoformat(request["data_cutoff"]),
        static_instrument_ids=static_ids,
        initial_positions=initial_positions,
        parameters=request.get("default_parameters", {}),
    )
    context, identity_rows = build_synthetic_context(parameters)

    real_stdout = sys.stdout
    # stdout stays reserved for the one JSON result for the entire strategy
    # lifetime: module-level code, compilation, and the run() call all write
    # to the bounded stderr forwarder instead.
    sys.stdout = _BoundedStderrWriter(sys.stderr, MAX_STRATEGY_OUTPUT_BYTES)
    try:
        adapter = FunctionStrategyAdapter(
            _load_published_module(request["source_code"]),
            parameters=request.get("default_parameters", {}),
        )
        decision = adapter.on_step(context)
    finally:
        sys.stdout = real_stdout

    return {
        "ok": True,
        "evidence": {
            "contract_version": STRATEGY_CONTRACT_VERSION,
            "mode": decision.mode,
            "target_count": len(decision.targets),
            "session_dates": [
                (context.session_date - timedelta(days=offset)).isoformat()
                for offset in SYNTHETIC_SESSION_OFFSETS
            ],
            "identity_rows": [
                _identity_row_to_evidence(row) for row in identity_rows
            ],
        },
    }


def main() -> int:
    """Read one request from stdin and emit exactly one result document.

    The result goes to the dedicated descriptor the parent passed via
    ``QF_CONTRACT_CHECK_RESULT_FD``, never to the strategy-visible stdout.
    """

    try:
        request = _parse_request(sys.stdin.buffer.read())
    except Exception as exc:  # noqa: BLE001 - reported as structured failure
        _emit_result({"ok": False, "failure": _failure_payload(exc)})
        return 0
    try:
        _emit_result(perform_contract_check(request))
    except Exception as exc:  # noqa: BLE001 - every failure must be locatable
        _emit_result({"ok": False, "failure": _failure_payload(exc)})
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    return 0


def _failure_payload(exc: Exception) -> dict:
    """Build a display-safe, locatable failure record.

    ``line`` prefers an explicit syntax-error line number and otherwise falls
    back to the deepest frame inside the strategy module, so runtime failures
    stay locatable.  A bounded sanitized traceback is attached as the
    expandable technical detail.
    """

    line = getattr(exc, "lineno", None)
    if line is None:
        line = _strategy_line(exc)
    return {
        "error_type": type(exc).__name__,
        "message": str(exc),
        "line": line,
        "traceback": _bounded_traceback(exc),
        "phase": "strategy_contract_check",
    }


def _strategy_line(exc: Exception) -> int | None:
    """Return the deepest strategy-source frame line, when available."""

    for summary in reversed(traceback.extract_tb(exc.__traceback__)):
        if summary.filename == STRATEGY_MODULE_NAME:
            return summary.lineno
    return None


def _bounded_traceback(exc: Exception) -> str:
    """Render the traceback tail without leaking unrelated internals."""

    rendered = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).strip()
    if len(rendered) > MAX_TRACEBACK_CHARS:
        rendered = rendered[-MAX_TRACEBACK_CHARS:]
    return rendered


def _emit_result(result: dict) -> None:
    """Write the result document to the dedicated parent-only descriptor."""

    document = json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n"
    fd_name = os.environ.get("QF_CONTRACT_CHECK_RESULT_FD")
    if fd_name is None:
        # Manual/debug invocation without a parent checker: fall back to
        # stdout, which real checks never parse.
        sys.stdout.buffer.write(document)
        sys.stdout.buffer.flush()
        return
    fd = int(fd_name)
    written = 0
    while written < len(document):
        written += os.write(fd, document[written:])
    os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
