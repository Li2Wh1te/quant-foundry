"""Migration and PostgreSQL coordination coverage for runner supervision."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


MIGRATION = (
    Path(__file__).parents[1]
    / "app/db/migrations/versions/20260831_02_add_backtest_runner_supervision.py"
)


def test_runner_migration_has_upgrade_downgrade_and_orphan_guards() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "def upgrade()" in source
    assert "def downgrade()" in source
    assert "backtest_queue_guards" in source
    assert "refusing to invent identity" in source
    assert "backtest_finished_at_consistent" in source


def test_runner_migration_reconciles_legacy_cancellation_and_identity() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "cancel_requested_at = updated_at" in source
    assert "child_start_identity = process_start_token" in source
    assert "child_process_group_id = process_group_id" in source
    assert "recovery_observed_at" in source


@pytest.mark.skipif(
    not os.getenv("QF_TEST_POSTGRES_DSN"),
    reason="set QF_TEST_POSTGRES_DSN to run real PostgreSQL lock integration",
)
def test_postgres_advisory_lock_and_skip_locked_integration() -> None:
    """Run the real session-lock/queue semantics when an integration DB is supplied."""

    from sqlalchemy import create_engine, text

    from app.backtesting.supervisor_lock import PostgresAdvisoryLock

    engine = create_engine(os.environ["QF_TEST_POSTGRES_DSN"])
    first = PostgresAdvisoryLock(engine, allow_test_fallback=False)
    second = PostgresAdvisoryLock(engine, allow_test_fallback=False)
    try:
        assert first.acquire()
        assert not second.acquire()
        assert first.connection_is_alive()
        with engine.begin() as connection:
            connection.execute(text("CREATE TEMP TABLE qf_runner_lock_probe (id integer)"))
            connection.execute(text("INSERT INTO qf_runner_lock_probe VALUES (1), (2)"))
            row = connection.execute(
                text("SELECT id FROM qf_runner_lock_probe ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1")
            ).first()
            assert row is not None
    finally:
        first.release()
        second.release()
        engine.dispose()
