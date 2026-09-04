from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.backtesting.production_runtime import _formal_gate_checks
from app.backtesting.run_admission import build_gate_evidence
from app.backtesting.run_binding import (
    GateOrchestrator,
    IdempotencyKeyReusedError,
    QueueFullError,
    RunBindingBuilder,
    RunCreationService,
)
from app.backtesting.spec import BacktestSpec, ComponentSelection


def _binding(**kwargs):
    spec = BacktestSpec(date(2024, 1, 1), date(2024, 1, 2), 100, [])
    return RunBindingBuilder().build(spec, **kwargs)


def test_hash_is_stable_and_kind_profile_are_server_bound():
    a, b = _binding(strategy={"revision": "r1"}), _binding(strategy={"revision": "r1"})
    assert a.config_hash == b.config_hash
    assert a.profile == "formal@1"


def test_builder_rejects_a_resolved_component_that_differs_from_the_spec():
    spec = BacktestSpec(
        date(2024, 1, 1),
        date(2024, 1, 2),
        100,
        [],
        slippage_model=ComponentSelection(
            "bps", 1, {"slippage_bps": "10", "price_tick": "0.01"}
        ),
    )
    with pytest.raises(ValueError, match="slippage model"):
        RunBindingBuilder().build(
            spec,
            components={
                "slippage_model": {
                    "key": "none",
                    "version": 1,
                    "parameters": {"price_tick": "0.01"},
                }
            },
        )


def test_idempotency_rejects_changed_payload():
    service = RunCreationService()
    service.create(_binding(strategy={"revision": "r1"}), idempotency_key="k")
    with pytest.raises(IdempotencyKeyReusedError):
        service.create(_binding(strategy={"revision": "r2"}), idempotency_key="k")


def test_queue_capacity_and_metric_disable():
    service = RunCreationService(formal_capacity=1)
    with pytest.raises(QueueFullError):
        service.create(_binding(), queued=1)
    decision = GateOrchestrator().evaluate(
        run_kind="backtest_run",
        checks={"phase1": True, "phase2a": True, "formal_basic": True, "formal_complete": True},
        metric_checks={"sharpe": False},
    )
    assert decision.allowed and decision.disabled_metrics == ("sharpe",)


def test_formal_creation_projects_and_persists_all_gate_evidence():
    report = SimpleNamespace(
        session_summary={"production_capabilities": {"status": "complete"}},
        status="ready",
        report_hash="a" * 64,
        issues=(),
    )
    checks = _formal_gate_checks(
        report,
        preflight_allowed=True,
        strategy={"revision_id": "revision"},
        account={"profile_id": "account"},
        components={"execution_model": {"key": "bar_market"}},
    )
    decision = GateOrchestrator().evaluate(run_kind="backtest_run", checks=checks)
    evidence = build_gate_evidence(
        decision,
        report_hash=report.report_hash,
        report_status=report.status,
        checked_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert checks == {
        "phase1": True,
        "phase2a": True,
        "formal_basic": True,
        "formal_complete": True,
    }
    assert evidence["allowed"] is True
    assert set(evidence["gates"]) == {
        "phase1",
        "phase2a",
        "formal_basic",
        "formal_complete",
    }
    assert all(item["report_hash"] == report.report_hash for item in evidence["gates"].values())
    assert all(item["status"] == "ready" for item in evidence["gates"].values())

    blocked_report = SimpleNamespace(
        session_summary={"production_capabilities": {"status": "incomplete"}},
        status="blocked",
        report_hash="b" * 64,
        issues=(),
    )
    blocked_checks = _formal_gate_checks(
        blocked_report,
        preflight_allowed=True,
        strategy={"revision_id": "revision"},
        account={"profile_id": "account"},
        components={"execution_model": {"key": "bar_market"}},
    )
    blocked = GateOrchestrator().evaluate(
        run_kind="backtest_run", checks=blocked_checks
    )
    assert not blocked.allowed
    assert blocked.reason == "formal_complete"
