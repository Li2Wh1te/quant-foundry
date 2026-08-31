from datetime import date

import pytest

from app.backtesting.run_binding import (
    GateOrchestrator,
    IdempotencyKeyReusedError,
    QueueFullError,
    RunBindingBuilder,
    RunCreationService,
)
from app.backtesting.spec import BacktestSpec


def _binding(**kwargs):
    spec = BacktestSpec(date(2024, 1, 1), date(2024, 1, 2), 100, [])
    return RunBindingBuilder().build(spec, **kwargs)


def test_hash_is_stable_and_kind_profile_are_server_bound():
    a, b = _binding(strategy={"revision": "r1"}), _binding(strategy={"revision": "r1"})
    assert a.config_hash == b.config_hash
    assert a.profile == "formal@1"


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
