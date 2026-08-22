"""Worker entry point for the isolated startup contract check.

Run as ``python -m app.strategy_protocol.worker``.  The parent checker sends
one JSON request document on stdin; this worker loads the published strategy
source, builds the deterministic synthetic ``DecisionContext``, calls
``run(context, parameters)`` once, validates the returned payload, and writes
exactly one JSON result line to stdout.

The process is deliberately self-contained: it never touches the database,
the network, or any mutable external state beyond what the request contains.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from uuid import UUID

if os.name == "posix" and os.environ.get("QF_CONTRACT_CHECK_MEM_MB"):
    # Best-effort address-space cap; applied before importing app modules so
    # even imports run under the limit where the platform supports it.
    import resource

    _limit_bytes = int(os.environ["QF_CONTRACT_CHECK_MEM_MB"]) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (_limit_bytes, _limit_bytes))

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

MAX_STRATEGY_STDOUT_BYTES = 65_536
"""Upper bound on bytes a strategy may print during the contract check."""


class _BoundedWriter:
    """stdout replacement that fails once the byte budget is exhausted."""

    def __init__(self, underlying, limit: int) -> None:
        self._underlying = underlying
        self._limit = limit
        self._written = 0

    def write(self, text: str) -> int:
        self._written += len(text.encode("utf-8"))
        if self._written > self._limit:
            raise RuntimeError(
                f"strategy stdout exceeded {self._limit} bytes during the "
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
    initial_positions = tuple(request.get("initial_positions", []))
    parameters = ContractCheckParameters(
        session_date=date.fromisoformat(request["session_date"]),
        decision_time=datetime.fromisoformat(request["decision_time"]),
        data_cutoff=datetime.fromisoformat(request["data_cutoff"]),
        static_instrument_ids=static_ids,
        initial_positions=initial_positions,
        parameters=request.get("default_parameters", {}),
    )
    context, identity_rows = build_synthetic_context(parameters)
    adapter = FunctionStrategyAdapter.from_source(
        request["source_code"],
        parameters=request.get("default_parameters", {}),
    )

    real_stdout = sys.stdout
    sys.stdout = _BoundedWriter(real_stdout, MAX_STRATEGY_STDOUT_BYTES)
    try:
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
    """Read one request from stdin and emit exactly one result line."""

    try:
        request = _parse_request(sys.stdin.buffer.read())
    except Exception as exc:  # noqa: BLE001 - reported as structured failure
        _emit({"ok": False, "failure": _failure_payload(exc)})
        return 0
    try:
        _emit(perform_contract_check(request))
    except Exception as exc:  # noqa: BLE001 - every failure must be locatable
        _emit({"ok": False, "failure": _failure_payload(exc)})
    finally:
        sys.stdout.flush()
    return 0


def _failure_payload(exc: Exception) -> dict:
    """Build a display-safe, locatable failure record."""

    return {
        "error_type": type(exc).__name__,
        "message": str(exc),
        # Syntax errors carry a source line; runtime errors do not.
        "line": getattr(exc, "lineno", None),
        "phase": "strategy_contract_check",
    }


def _emit(result: dict) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
