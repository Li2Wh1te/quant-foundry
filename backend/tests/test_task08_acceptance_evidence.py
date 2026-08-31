"""Focused evidence tests for task package 08 review items.

These tests pin the non-negotiable contracts without requiring a PostgreSQL
service: schema checks are inspected from SQLAlchemy metadata and migration
source, while visibility/logging contracts are exercised directly.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from app.backtesting.models import BacktestRunRecord
from app.backtesting.pagination import compute_query_digest
from app.backtesting.result_repository import (
    InternalResultNotVisibleError,
    UnknownResultKindError,
    enforce_root_kind,
)
from app.core.logging import backtest_event_message
from app.backtesting.run_execution import (
    BacktestQueue,
    ChunkCommit,
    ChunkResultWriter,
    InvalidRunTransition,
    RunStateMachine,
    decide_terminal,
)


def test_run_schema_has_review_constraints_and_indexes():
    table = BacktestRunRecord.__table__
    constraints = {
        c.name: str(c.sqltext)
        for c in table.constraints
        if c.name and hasattr(c, "sqltext")
    }
    assert any(name.endswith("backtest_kind_profile_match") for name in constraints)
    assert any(name.endswith("backtest_progress_range") for name in constraints)
    assert any(name.endswith("backtest_config_hash_sha256") for name in constraints)
    assert any(name.endswith("backtest_chunk_policy_fixed") for name in constraints)
    assert any(i.name == "uq_backtest_runs_idempotency" and i.unique for i in table.indexes)
    migration = Path(__file__).parents[1] / "app/db/migrations/versions/20260831_01_add_backtest_runs.py"
    source = migration.read_text(encoding="utf-8")
    assert "ondelete='RESTRICT'" in source
    assert "backtest_kind_profile_match" in source


def test_result_visibility_requires_existing_root_and_exact_kind():
    with pytest.raises(UnknownResultKindError):
        enforce_root_kind(None)
    with pytest.raises(InternalResultNotVisibleError):
        enforce_root_kind("internal_link_acceptance")
    enforce_root_kind("backtest_run")


def test_cursor_digest_is_bound_to_run_and_kind():
    base = {"kind": "orders", "run_id": str(uuid4()), "filters": {}, "limit": 100}
    digest = compute_query_digest(base)
    changed_run = dict(base, run_id=str(uuid4()))
    changed_kind = dict(base, kind="metrics")
    assert digest != compute_query_digest(changed_run)
    assert digest != compute_query_digest(changed_kind)


def test_operator_event_message_is_chinese_and_contains_scope_outcome():
    message = backtest_event_message("回测分块写入", "run-123/chunk-2", "已完成（20 个交易日）")
    assert "回测分块写入" in message
    assert "run-123/chunk-2" in message
    assert "已完成" in message
    assert message.endswith("。")


def test_state_machine_rejects_terminal_overwrite_and_invalid_jump():
    machine = RunStateMachine()
    assert machine.transition("queued", "starting") == "starting"
    assert machine.transition("starting", "running") == "running"
    assert machine.transition("running", "terminal") == "terminal"
    with pytest.raises(InvalidRunTransition):
        machine.transition("terminal", "running")


def test_chunk_writer_is_idempotent_and_rejects_conflicts():
    writer = ChunkResultWriter()
    commit = ChunkCommit(0, "sha256:" + "a" * 64, 0.5, {"session": "2026-01-01"})
    assert writer.append(commit) == commit
    assert writer.append(commit) == commit
    with pytest.raises(ValueError):
        writer.append(ChunkCommit(0, "sha256:" + "b" * 64, 0.5, {"session": "2026-01-01"}))


def test_terminal_adjudication_fails_closed_on_inconsistent_evidence():
    marker = {
        "protocol": "completion_marker@1",
        "status": "succeeded",
        "run_id": "run-1",
        "config_hash": "h" * 64,
        "result_count": 2,
    }
    assert decide_terminal(marker=marker, exit_code=0, expected_count=2, run_id="run-1", config_hash="h" * 64) == "succeeded"
    assert decide_terminal(marker=marker, exit_code=1, expected_count=2, run_id="run-1", config_hash="h" * 64) == "indeterminate"
    assert decide_terminal(marker=marker, exit_code=0, expected_count=3, run_id="run-1", config_hash="h" * 64) == "indeterminate"


def test_queue_claims_formal_before_internal():
    queue = BacktestQueue(formal_limit=2, internal_limit=1)
    formal_id, internal_id = uuid4(), uuid4()
    queue.enqueue(internal_id, "internal_link_acceptance")
    queue.enqueue(formal_id, "backtest_run")
    assert queue.claim() == formal_id
    assert queue.claim() == internal_id
