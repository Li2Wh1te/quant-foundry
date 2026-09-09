"""Exercise overview SQL against real rows, including PostgreSQL in CI.

Local SQLite uses only column mirrors because production CHECK constraints use
PostgreSQL JSON operators. CI runs the same assertions on the migrated schema;
each test rolls back its fixture transaction and never touches deployed data.
"""

import os
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import Response
from pydantic import BaseModel, SecretStr
from sqlalchemy import Column, JSON, MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.overview.router import read_overview
from app.overview.service import OverviewService, shanghai_day_bounds
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskDefinition, TaskRegistry, task_registry


NOW = datetime(2026, 9, 9, 6, tzinfo=UTC)


class OverviewTest(unittest.TestCase):
    def setUp(self):
        if os.getenv("POSTGRES_TEST_ENABLED") == "1":
            self.engine = create_engine(get_settings().database_url)
        else:
            self.engine = create_engine("sqlite://")
            metadata = MetaData()
            for model in (ScheduledTask, TaskRun):
                Table(model.__tablename__, metadata, *[
                    Column(column.name, JSON() if isinstance(column.type, JSONB) else column.type,
                           primary_key=column.primary_key)
                    for column in model.__table__.columns
                ])
            metadata.create_all(self.engine)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(self.connection)
        # Unique registered keys isolate counts from other CI fixtures without
        # deleting pre-existing records in the disposable test database.
        self.key = "test." + uuid4().hex
        self.other_key = "test." + uuid4().hex
        self.registry = TaskRegistry()
        for key, source in ((self.key, "tushare"), (self.other_key, None)):
            self.registry.register(TaskDefinition(key=key, name="测试采集",
                english_name="Test ingestion", parameters_model=BaseModel,
                handler=lambda *_: None, source_key=source))
        self.service = OverviewService(self.session, self.registry)

    def tearDown(self):
        self.session.close()
        if self.transaction.is_active:
            self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def task(self, *, state="active", key=None):
        task = ScheduledTask(id=uuid4(), name="演示任务", task_type=key or self.key,
            parameters={}, parameter_version=1,
            schedule={"type": "interval", "seconds": 60, "start_at": NOW.isoformat()},
            state=state, created_at=NOW-timedelta(days=10), updated_at=NOW)
        self.session.add(task)
        self.session.flush()
        return task

    def add_run(self, task, status="succeeded", *, at=NOW, created_at=None):
        started = at if status not in ("queued", "skipped") else None
        finished = at+timedelta(seconds=5) if status not in ("queued", "running") else None
        run = TaskRun(id=uuid4(), task_id=task.id, task_type=task.task_type,
            task_version=1, trigger_type="scheduled", status=status,
            parameters={"sensitive": "never-return-this"}, parameter_version=1,
            created_at=created_at or at-timedelta(seconds=1), available_at=at,
            started_at=started, finished_at=finished, result={"private": "never-return-this"},
            error_message="never-return-this")
        self.session.add(run)
        self.session.flush()
        return run

    def read(self, configured=False):
        # Match a new request's database-loaded identity map. SQLite strips
        # timezone offsets on load, unlike the production PostgreSQL dialect.
        self.session.expire_all()
        return self.service.read(tushare_configured=configured, now=NOW)

    def test_empty_and_configuration_do_not_imply_connectivity(self):
        result = self.read(True)
        self.assertEqual(result.metrics.active_tasks, 0)
        self.assertEqual(result.metrics.today_runs, 0)
        self.assertEqual(result.metrics.attention_tasks, 0)
        self.assertEqual(result.recent_runs, [])
        self.assertEqual(result.metrics.configured_sources, 1)
        self.assertEqual(result.sources[0].connection_status, "not_checked")
        self.assertIsNone(result.sources[0].last_success_at)

    def test_counts_more_than_one_hundred_tasks_and_runs(self):
        for _ in range(105):
            self.add_run(self.task())
        result = self.read()
        self.assertEqual(result.metrics.active_tasks, 105)
        self.assertEqual(result.metrics.today_runs, 105)
        self.assertEqual(result.metrics.today_succeeded, 105)
        self.assertEqual(len(result.recent_runs), 4)
        self.assertNotIn("never-return-this", result.model_dump_json())
        self.assertEqual(result.recent_runs[0].duration_seconds, 5)
        self.assertEqual(result.recent_runs[0].task_type_english_name, "Test ingestion")

    def test_day_boundaries_use_start_not_creation_and_exclude_unstarted(self):
        task = self.task()
        start, end = shanghai_day_bounds(NOW)
        self.assertEqual(start, datetime(2026, 9, 8, 16, tzinfo=UTC))
        self.add_run(task, at=start-timedelta(microseconds=1))
        self.add_run(task, at=start, created_at=start-timedelta(days=1))
        self.add_run(task, at=end-timedelta(seconds=10))
        self.add_run(task, at=end)
        self.add_run(task, "skipped")
        self.add_run(task, "queued")
        self.add_run(task, "running")
        result = self.read()
        self.assertEqual((result.metrics.today_runs, result.metrics.today_succeeded), (3, 2))
        self.assertEqual((result.metrics.queued_runs, result.metrics.running_runs), (1, 1))
        with self.assertRaises(ValueError):
            shanghai_day_bounds(NOW.replace(tzinfo=None))

    def test_skip_does_not_clear_failure_and_retry_is_not_recovery(self):
        task = self.task()
        self.add_run(task, "failed", at=NOW-timedelta(minutes=5))
        failed = self.add_run(task, "timed_out", at=NOW-timedelta(minutes=4))
        self.add_run(task, "skipped", at=NOW-timedelta(minutes=3))
        self.add_run(task, "running", at=NOW-timedelta(minutes=2))
        result = self.read()
        self.assertEqual(result.metrics.attention_tasks, 1)
        self.assertEqual(result.attention[0].run.id, failed.id)
        self.assertTrue(result.attention[0].retrying)
        self.add_run(task, "succeeded")
        self.assertEqual(self.read().metrics.attention_tasks, 0)

    def test_cancelled_or_successful_latest_outcome_clears_older_error(self):
        for status in ("succeeded", "cancelled"):
            task = self.task()
            self.add_run(task, "failed", at=NOW-timedelta(minutes=2))
            self.add_run(task, status)
        self.assertEqual(self.read().metrics.attention_tasks, 0)

    def test_older_concurrent_run_is_not_a_retry(self):
        task = self.task()
        self.add_run(task, "running", at=NOW-timedelta(minutes=4))
        self.add_run(task, "failed", at=NOW-timedelta(minutes=2))
        self.assertFalse(self.read().attention[0].retrying)
        self.add_run(task, "queued")
        self.assertTrue(self.read().attention[0].retrying)

    def test_archived_and_unrelated_tasks_are_not_active_or_attention(self):
        archived = self.task(state="archived")
        self.add_run(archived, "failed")
        self.add_run(self.task(key=self.other_key), "failed")
        self.add_run(self.task(state="paused"), "failed")
        result = self.read()
        self.assertEqual(result.metrics.active_tasks, 0)
        self.assertEqual(result.metrics.attention_tasks, 1)
        self.assertEqual(result.metrics.today_runs, 2)  # Includes archived history.

    def test_attention_preview_limit_does_not_truncate_metric(self):
        for i in range(12):
            self.add_run(self.task(), ("failed", "interrupted", "timed_out", "indeterminate")[i % 4])
        result = self.read()
        self.assertEqual(result.metrics.attention_tasks, 12)
        self.assertEqual(len(result.attention), 10)

    def test_source_refresh_ignores_later_failures_and_skipped_runs(self):
        task = self.task()
        successful = self.add_run(task, at=NOW-timedelta(hours=1))
        self.add_run(task, "failed")
        self.add_run(task, "skipped", at=NOW+timedelta(minutes=1))
        self.assertEqual(self.read().sources[0].last_success_at.replace(tzinfo=UTC),
                         successful.finished_at.replace(tzinfo=UTC))


class OverviewContractTest(unittest.TestCase):
    def test_every_builtin_ingestion_task_has_explicit_source_metadata(self):
        definitions = task_registry.list()
        self.assertTrue(definitions)
        for definition in definitions:
            self.assertEqual(definition.source_key, "tushare")

    def test_route_is_authenticated_and_has_no_write_methods(self):
        from app.main import create_app
        import asyncio
        from tests.test_auth import request_status
        app = create_app()
        operations = app.openapi()["paths"]["/api/admin/overview"]
        self.assertEqual(set(operations), {"get"})
        self.assertEqual(operations["get"]["security"], [{"API Token": []}])
        self.assertEqual(asyncio.run(request_status(app, "/api/admin/overview")), 401)

    @patch("app.overview.router.OverviewService")
    def test_route_passes_only_presence_and_disables_caching(self, service):
        response = Response()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(
            tushare_token=SecretStr("private-provider-credential")))))
        session = Mock()
        session.get.return_value = SimpleNamespace(initialized=True, encrypted_secrets="encrypted")
        read_overview(request, response, session)
        service.return_value.read.assert_called_once_with(tushare_configured=True)
        session.connection.assert_called_once_with(
            execution_options={"isolation_level": "REPEATABLE READ"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        session.get.return_value = None
        service.return_value.read.reset_mock()
        read_overview(request, response, session)
        service.return_value.read.assert_called_once_with(tushare_configured=False)
