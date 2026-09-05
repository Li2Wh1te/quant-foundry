"""Regression coverage for shared admission and audit boundaries."""
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import uuid4
import pytest
from pydantic import BaseModel, ValidationError
from app.backtesting.fees import FeeSchedule, FeeScheduleSnapshot
from app.backtesting.run_router import _binding
from app.backtesting.run_schemas import InternalRunCreateRequest, RunCreateRequest
from app.backtesting.runner_progress import FrozenTimelineProgress, ProgressReporter
from app.core.config import Settings
from app.scheduling.registry import TaskDefinition, TaskRegistry


@pytest.mark.parametrize("version", [None, True, 0, -1, "1", 1.5])
@pytest.mark.parametrize("schedule_type", [FeeSchedule, FeeScheduleSnapshot])
def test_every_fee_snapshot_requires_an_exact_version(schedule_type, version):
    with pytest.raises(ValueError):
        schedule_type(key="commission", version=version, fee_rules=())


def test_formal_queue_overflow_is_rejected_before_database_access():
    with pytest.raises(ValidationError) as error:
        Settings(backtest_max_queued_runs=33, _env_file=None)
    assert "backtest_max_queued_runs" in str(error.value)


@pytest.mark.parametrize("field", ["key", "name", "english_name"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_invalid_task_labels_never_enter_registry(field, value):
    values = dict(key="test", name="测试", english_name="Test", parameters_model=BaseModel,
                  handler=lambda *_: None)
    values[field] = value
    registry = TaskRegistry()
    with pytest.raises(ValueError):
        registry.register(TaskDefinition(**values))
    assert registry.list() == []


def internal_payload():
    return dict(strategy_revision_id=uuid4(), backtest_config={
        "start_date": "2026-01-01", "end_date": "2026-01-02"},
        internal_fixtures=[dict(fixture_key="quantity_coverage", fixture_version=1,
            capability="quantity_action_coverage", content_hash="a" * 64,
            proof_summary="Frozen complete-zero acceptance fixture",
            scope={"instrument_ids": [str(uuid4())], "start_date": "2026-01-01", "end_date": "2026-01-02"})])


def test_internal_fact_is_frozen_and_changes_configuration_identity():
    payload = internal_payload()
    first = _binding(InternalRunCreateRequest(**payload), kind="internal_link_acceptance")
    payload["internal_fixtures"][0]["fixture_version"] = 2
    second = _binding(InternalRunCreateRequest(**payload), kind="internal_link_acceptance")
    assert first.config_hash != second.config_hash
    fact = first.data_request["internal_fixtures"][0]
    assert fact["fixture_version"] == 1 and fact["fixture_only"] is True
    with pytest.raises(ValidationError):
        RunCreateRequest(**payload)


def test_internal_fact_outside_run_dates_is_rejected():
    payload = internal_payload()
    payload["internal_fixtures"][0]["scope"]["start_date"] = "2026-01-02"
    with pytest.raises(ValidationError):
        InternalRunCreateRequest(**payload)


def test_repeating_progress_fraction_survives_report_and_flush():
    writes = []
    reporter = ProgressReporter(uuid4(), persist_progress=writes.append, persist_heartbeat=lambda _: None)
    with localcontext() as context:
        context.prec = 6
        report = reporter.report(FrozenTimelineProgress(3, 1), now=datetime.now(UTC), force=True)
    assert report.progress == Decimal("0.3333333333333333333333333333")
    assert (report.completed_steps, report.total_steps) == (1, 3)
    reporter.heartbeat()
    reporter.flush()
    assert (writes[-1].completed_steps, writes[-1].total_steps) == (1, 3)


def test_run_filters_apply_before_pagination_and_cursor_cannot_cross_filters(monkeypatch):
    from datetime import timedelta
    from sqlalchemy import Column, JSON, MetaData, Table, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import Session
    from app.backtesting.models import BacktestRunRecord
    from app.backtesting.run_repository import DatabaseRunRepository
    from app.backtesting.pagination import CursorQueryMismatchError

    from sqlalchemy import DateTime, TypeDecorator

    class SQLiteUTCDateTime(TypeDecorator):
        # SQLite discards timezone offsets; PostgreSQL's real column retains
        # them. Restore UTC in this test adapter to exercise the same cursor.
        impl = DateTime
        cache_ok = True

        def process_result_value(self, value, dialect):
            return value.replace(tzinfo=UTC) if value is not None else None

    monkeypatch.setattr(BacktestRunRecord.__table__.c.created_at, "type", SQLiteUTCDateTime())
    engine = create_engine("sqlite://")
    metadata = MetaData()
    # Keep the real column names/types, excluding unrelated PostgreSQL queue
    # constraints: this test executes filtering and signed pagination in SQL.
    table = Table("backtest_runs", metadata, *[
        Column(column.name, JSON() if isinstance(column.type, JSONB) else column.type,
               primary_key=column.primary_key) for column in BacktestRunRecord.__table__.columns
    ])
    metadata.create_all(engine)
    revision = uuid4()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    ids = [uuid4() for _ in range(5)]
    with engine.begin() as connection:
        connection.execute(table.insert(), [dict(id=rid, run_kind="backtest_run",
            idempotency_scope="owner", strategy_revision_id=str(revision),
            status="queued" if index == 0 else "succeeded",
            created_at=start + timedelta(days=index),
            backtest_config={"note": "literal 10%" if index < 4 else "literal 100"})
            for index, rid in enumerate(ids)])
    filters = dict(owner_scope="owner", strategy_revision_id=str(revision), status="succeeded",
                   created_after=start + timedelta(days=1), created_before=start + timedelta(days=4),
                   config_summary="10%")
    with Session(engine) as session:
        repository = DatabaseRunRepository(session)
        assert [row.id for row in repository.list(**filters, limit=2)] == ids[1:3]
        first = repository.list_page(**filters, signing_key="test-key", limit=1)
        assert first.items[0].id == ids[1] and first.has_more
        second = repository.list_page(**filters, signing_key="test-key", limit=1, cursor=first.next_cursor)
        assert second.items[0].id == ids[2]
        with pytest.raises(CursorQueryMismatchError):
            repository.list_page(**{**filters, "config_summary": "literal"}, signing_key="test-key", limit=1, cursor=first.next_cursor)
        assert repository.list(**{**filters, "owner_scope": "other"}) == []
    engine.dispose()


def test_bar_time_range_and_optional_tick_book_are_typed_facts():
    from datetime import timedelta
    from app.backtesting.data.facts import Bar, Tick
    from tests.test_backtesting_memory_provider import make_evidence
    now = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
    values = dict(instrument_id=uuid4(), trade_date=now.date(), frequency="1m",
                  open="10", high="10", low="10", close="10", volume="1", amount="10",
                  evidence=make_evidence())
    with pytest.raises(ValueError):
        Bar(**values)
    bar = Bar(**values, start_time=now, end_time=now + timedelta(minutes=1))
    assert bar.end_time - bar.start_time == timedelta(minutes=1)
    with pytest.raises(ValueError):
        Bar(**values, start_time=now, end_time=now)
    tick = Tick(instrument_id=values["instrument_id"], traded_at=now, price="10", quantity="1",
                evidence=make_evidence(), bid="9.99", ask="10.01")
    assert tick.bid == Decimal("9.99") and tick.ask == Decimal("10.01")


def test_progress_precision_migration_preserves_existing_observation():
    from importlib import import_module
    from unittest.mock import patch
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text
    migration = import_module("app.db.migrations.versions.20260909_01_preserve_backtest_progress_precision")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE backtest_runs (id INTEGER PRIMARY KEY, progress NUMERIC(6,5) NOT NULL)"))
        connection.execute(text("INSERT INTO backtest_runs VALUES (1, 0.33333)"))
        with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
            migration.upgrade()
            assert inspect(connection).get_columns("backtest_runs")[1]["type"].scale is None
            assert connection.scalar(text("SELECT progress FROM backtest_runs")) == 0.33333
            migration.downgrade()
            assert inspect(connection).get_columns("backtest_runs")[1]["type"].scale == 5
    engine.dispose()


def test_internal_labels_are_consistent_and_logging_never_reads_database(caplog):
    import logging
    from unittest.mock import Mock
    from types import SimpleNamespace
    from app.backtesting.run_schemas import RunResponse
    from app.backtesting.runner_supervisor import RunnerSupervisor
    run_id = uuid4()
    response = RunResponse(run_id=run_id, run_kind="internal_link_acceptance", profile="internal@1",
                           status="queued", config_hash="a" * 64)
    assert response.label == "内部链路验收" and response.visibility == "internal"
    supervisor = object.__new__(RunnerSupervisor)
    supervisor.repository = Mock()
    supervisor._remember_run(SimpleNamespace(id=run_id, run_kind="internal_link_acceptance"))
    with caplog.at_level(logging.INFO):
        supervisor._log(logging.INFO, "backtest_claimed", "运行已领取。", run_id=str(run_id))
    assert "内部链路验收" in caplog.records[-1].getMessage()
    assert supervisor.repository.mock_calls == []


def test_missing_position_identity_cannot_become_uuid_display_labels():
    from types import SimpleNamespace
    from app.backtesting.runtime import DeterministicBacktestRunner
    runner = object.__new__(DeterministicBacktestRunner)
    runner._portfolio = SimpleNamespace(account=object(), positions={uuid4(): SimpleNamespace(is_zero=False)})
    runner._identities = {}
    with pytest.raises(ValueError, match="missing effective identity facts"):
        runner._build_portfolio_dto()


def test_draft_state_compares_saved_source_schema_and_defaults():
    from types import SimpleNamespace
    from app.strategies.router import _draft_changed_since_revision
    values = dict(source_hash="a", parameter_schema={"type": "object"}, default_parameters={"risk": 1})
    revision = SimpleNamespace(**values)
    assert not _draft_changed_since_revision(SimpleNamespace(**values), revision)
    for field, value in [("source_hash", "b"), ("parameter_schema", {}), ("default_parameters", {"risk": 2})]:
        assert _draft_changed_since_revision(SimpleNamespace(**{**values, field: value}), revision)
    assert not _draft_changed_since_revision(SimpleNamespace(**values), None)


@pytest.mark.parametrize("version", [True, 1.0, 0, "latest"])
def test_internal_fixture_version_cannot_be_coerced_or_floating(version):
    payload = internal_payload()
    payload["internal_fixtures"][0]["fixture_version"] = version
    with pytest.raises(ValidationError):
        InternalRunCreateRequest(**payload)
