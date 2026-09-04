"""Pure and metadata-level checks for the runner-owned queue boundary."""

from pathlib import Path
from uuid import uuid4

import pytest

from app.backtesting.models import BacktestQueueGuardRecord, BacktestRunRecord
from app.backtesting.run_execution import BacktestQueue
from app.backtesting.run_repository import (
    FORMAL_KIND,
    INTERNAL_KIND,
    DatabaseRunRepository,
)


def test_runner_schema_exposes_all_supervision_fields_and_guard_model():
    columns = set(BacktestRunRecord.__table__.columns.keys())
    required = {
        "idempotency_scope",
        "claimed_at",
        "launch_id",
        "worker_id",
        "child_start_identity",
        "child_process_group_id",
        "worker_handshake_at",
        "current_trading_date",
        "current_step",
        "last_heartbeat_at",
        "last_progress_persisted_at",
        "cancel_requested_at",
        "termination_requested_at",
        "termination_reason",
        "runner_exit_code_protocol",
        "runner_exit_category",
        "runner_exit_report",
        "stdout_bytes",
        "stdout_digest",
        "stdout_truncated",
        "resource_limit_evidence",
        "completion_marker_protocol",
        "completion_marker_validation",
        "result_integrity_evidence",
        "random_seed",
        "terminal_decision_reason",
        "failure_phase",
        "failure_type",
        "error_message",
        "recovery_action",
        "formal_gate_evidence",
    }
    assert required <= columns
    assert BacktestQueueGuardRecord.__table__.primary_key.columns.keys() == [
        "queue_kind"
    ]
    constraint_names = {
        constraint.name for constraint in BacktestRunRecord.__table__.constraints
    }
    assert any(name.endswith("backtest_finished_at_consistent") for name in constraint_names)
    assert any(name.endswith("backtest_queued_identity_clear") for name in constraint_names)
    assert any(name.endswith("backtest_running_identity_complete") for name in constraint_names)


def test_formal_gate_migration_persists_an_immutable_projection():
    migration = (
        Path(__file__).parents[1]
        / "app/db/migrations/versions/20260905_01_add_formal_gate_evidence.py"
    ).read_text(encoding="utf-8")
    assert "formal_gate_evidence" in migration
    assert "formal_gate_evidence_immutable" in migration


def test_runner_migration_installs_permanent_guards_and_orphan_check():
    migration = (
        Path(__file__).parents[1]
        / "app/db/migrations/versions/20260831_02_add_backtest_runner_supervision.py"
    ).read_text(encoding="utf-8")
    assert "backtest_queue_guards" in migration
    assert "internal_link_acceptance" in migration
    assert "refusing to invent identity" in migration
    assert "backtest_running_identity_complete" in migration


def test_in_memory_queue_keeps_formal_priority_and_explicit_worker_quota():
    formal_id, internal_id = uuid4(), uuid4()
    queue = BacktestQueue(formal_limit=2, internal_limit=1, workers=1)
    queue.enqueue(internal_id, INTERNAL_KIND)
    queue.enqueue(formal_id, FORMAL_KIND)
    assert queue.claim() == formal_id
    assert queue.claim() is None
    queue.release(formal_id)
    assert queue.claim() == internal_id


def test_database_repository_limits_internal_queue_to_a_smaller_value():
    with pytest.raises(ValueError):
        DatabaseRunRepository(object(), formal_limit=2, internal_limit=2)
