"""Task-23 API, frontend polling, and protocol fault-injection contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.backtesting.run_router import _response
from app.backtesting.runner_process import LaunchIdentity
from app.backtesting.runner_protocol import build_completion_marker, evaluate_terminal
from app.backtesting.runner_integrity import compute_result_integrity
from app.backtesting.runner_worker import write_completion_marker


ROOT = Path(__file__).parents[2]


def _rows() -> dict[str, list[dict[str, object]]]:
    return {
        name: []
        for name in (
            "backtest_steps",
            "backtest_decisions",
            "backtest_orders",
            "backtest_order_updates",
            "backtest_fills",
            "backtest_positions",
            "backtest_equity_curve",
            "backtest_metrics",
        )
    }


def test_api_projects_legacy_storage_to_canonical_runtime_and_terminal_fields() -> None:
    """The public response must not expose duplicate storage-era names."""

    run_id = uuid4()
    row = SimpleNamespace(
        id=run_id,
        run_kind="backtest_run",
        profile="formal@1",
        status="indeterminate",
        terminal_status="indeterminate",
        config_hash="a" * 64,
        progress=0.375,
        current_trading_date=date(2026, 8, 31),
        current_step="step-3",
        last_heartbeat_at=datetime(2026, 8, 31, 1, 2, 3, tzinfo=UTC),
        runner_exit_code=10,
        runner_exit_code_protocol="runner_exit_code@1",
        runner_exit_category="failed",
        child_start_identity="must-not-leak",
        completion_marker_protocol="completion_marker@1",
        completion_marker_validation={"valid": False, "errors": ["conflict"]},
        result_integrity_status="failed",
        result_integrity_evidence={"status": "failed", "errors": ["mismatch"]},
        result_counts={"steps": 3},
        terminal_decision_reason="completion_evidence_conflict",
        failure_phase="runtime",
        failure_type="WorkerError",
        recovery_action="terminate_no_restart",
    )

    response = _response(row)
    payload = response.model_dump()

    assert payload["progress_ratio"] == 0.375
    assert payload["current_trading_date"] == date(2026, 8, 31)
    assert payload["child_exit_code"] == 10
    assert payload["child_exit_code_protocol"] == "runner_exit_code@1"
    assert payload["completion_marker_validation"]["valid"] is False
    assert payload["terminal_decision_reason"] == "completion_evidence_conflict"
    assert "progress" not in payload
    assert "current_date" not in payload
    assert "runner_exit_code" not in payload
    assert "child_start_identity" not in payload


def test_marker_writer_rejects_uncommitted_result_and_does_not_call_writer() -> None:
    writes: list[dict[str, object]] = []
    with pytest.raises(RuntimeError, match="committed result transaction"):
        write_completion_marker(writes.append, {"protocol_version": "completion_marker@1"}, result_transaction_committed=False)
    assert writes == []


def test_forced_termination_cannot_become_a_determinate_result() -> None:
    run_id = uuid4()
    config_hash = "b" * 64
    integrity = compute_result_integrity(_rows(), config_hash=config_hash)
    marker = build_completion_marker(
        run_id=run_id,
        declared_category="failed",
        digest=integrity.digest,
        result_counts=integrity.counts,
        failure_phase="runtime",
        failure_type="WorkerError",
        config_hash=config_hash,
    )
    result = evaluate_terminal(
        marker=marker,
        exit_code=10,
        integrity=integrity,
        run_id=run_id,
        config_hash=config_hash,
        forced=True,
    )
    assert result.status == "indeterminate"
    assert result.reason == "forced_termination_without_provable_completion"


def test_frontend_declares_visibility_polling_and_all_terminal_stops() -> None:
    source = (ROOT / "frontend/src/pages/BacktestRunsPage.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "frontend/src/api/backtestRuns.ts").read_text(encoding="utf-8")
    log_source = (ROOT / "frontend/src/pages/LogPage.tsx").read_text(encoding="utf-8")

    assert 'FOREGROUND_POLLING_PROTOCOL = "foreground_polling@1"' in api_source
    assert "document.visibilityState" in source
    assert 'document.addEventListener("visibilitychange"' in source
    assert "FOREGROUND_POLL_INTERVAL_MS" in source
    assert "pollInFlightRef" in source
    assert "pollAbortRef" in source
    assert "listInFlightRef" in source
    assert "detailInFlightRef" in source
    assert "activeRunsRef.current" in source
    for terminal in ("succeeded", "failed", "cancelled", "timed_out", "indeterminate"):
        assert f'"{terminal}"' in api_source
    for event in (
        "backtest_completion_marker_received",
        "backtest_completion_marker_validated",
        "backtest_result_integrity_checked",
        "backtest_terminal_evidence_reconciled",
        "backtest_terminal_state_written",
        "backtest_heartbeat_persisted",
        "backtest_heartbeat_lost",
        "backtest_recovery_evidence_reconciled",
    ):
        assert event in log_source


def test_launch_identity_type_remains_available_for_fault_injection_adapters() -> None:
    """The test seam keeps process identity evidence explicit and typed."""

    run_id, launch_id = uuid4(), uuid4()
    identity = LaunchIdentity(run_id, launch_id, 1234, "start-token", 1234)
    assert identity.pid == 1234
    assert identity.start_identity == "start-token"
