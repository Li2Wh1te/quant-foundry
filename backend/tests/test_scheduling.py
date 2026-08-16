import unittest
from concurrent.futures import Future
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ValidationError
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry, task_registry
from app.scheduling.repository import SchedulerRepository
from app.scheduling.runtime import SchedulerRuntime
from app.scheduling.schemas import (
    CronSchedule,
    RunStatus,
    TaskCreate,
    TaskState,
    TaskUpdate,
    TriggerType,
)
from app.scheduling.service import SchedulerService, TaskConflictError
from app.scheduling.triggers import build_trigger


API_TOKEN = "a" * 64
TEST_TASK_TYPE = "test.noop"


class TestTaskParameters(BaseModel):
    pass


def test_task_handler(
    context: TaskContext, parameters: TestTaskParameters
) -> dict[str, str]:
    return {"status": "ok"}


def make_test_registry() -> TaskRegistry:
    registry = TaskRegistry()
    registry.register(
        TaskDefinition(
            key=TEST_TASK_TYPE,
            name="Test noop",
            parameters_model=TestTaskParameters,
            handler=test_task_handler,
        )
    )
    return registry


def make_task(**overrides):
    values = {
        "id": uuid4(),
        "name": "Test task",
        "description": None,
        "task_type": TEST_TASK_TYPE,
        "parameters": {},
        "parameter_version": 1,
        "schedule": {
            "type": "cron",
            "expression": "0 18 * * 1-5",
            "timezone": "Asia/Shanghai",
        },
        "state": "active",
        "concurrency_limit": 1,
        "overlap_policy": "skip",
        "queue_limit": 1,
        "priority": 0,
        "version": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SchedulingSchemaTestCase(unittest.TestCase):
    def test_etf_daily_parameters_omit_derived_dates_and_accept_legacy_tasks(self) -> None:
        parameters = task_registry.require(
            "data.sync_etf_daily_incremental"
        ).parameters_model.model_validate(
            {
                "calendar_exchange": "SSE",
                "initial_start_date": "20050101",
                "request_interval_ms": 1_000,
            }
        )

        self.assertEqual(parameters.request_interval_ms, 1_000)
        self.assertNotIn("initial_start_date", parameters.model_dump())
        self.assertNotIn("calendar_exchange", parameters.model_dump())

    def test_trade_calendar_parameters_accept_tushare_compact_date(self) -> None:
        parameters = task_registry.require(
            "data.sync_trade_calendar"
        ).parameters_model.model_validate(
            {"exchange": "SZSE", "initial_start_date": "19910703"}
        )

        self.assertEqual(parameters.initial_start_date, date(1991, 7, 3))

    def test_validates_discriminated_schedule_and_custom_parameters(self) -> None:
        payload = TaskCreate.model_validate(
            {
                "name": " Daily log ",
                "task_type": TEST_TASK_TYPE,
                "parameters": {},
                "schedule": {
                    "type": "cron",
                    "expression": "0 18 * * 1-5",
                    "timezone": "Asia/Shanghai",
                },
            }
        )

        self.assertEqual(payload.name, "Daily log")
        self.assertIsInstance(build_trigger(payload.schedule), CronTrigger)

    def test_rejects_naive_schedule_times_and_null_updates(self) -> None:
        with self.assertRaises(ValidationError):
            TaskCreate.model_validate(
                {
                    "name": "Once",
                    "task_type": TEST_TASK_TYPE,
                    "schedule": {
                        "type": "once",
                        "run_at": "2026-08-15T18:00:00",
                    },
                }
            )
        with self.assertRaises(ValidationError):
            TaskUpdate.model_validate({"version": 1, "name": None})

    def test_cron_semantics_are_validated_by_trigger_builder(self) -> None:
        schedule = CronSchedule(
            type="cron",
            expression="99 99 * * *",
            timezone="UTC",
        )

        with self.assertRaises(ValueError):
            build_trigger(schedule)


class SchedulerServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock()
        self.service = SchedulerService(self.session, make_test_registry())
        self.service.repository = Mock()

    def test_creates_skipped_run_when_skip_policy_is_at_capacity(self) -> None:
        task = make_task()
        expected_run = object()
        self.service.repository.get_task.return_value = task
        self.service.repository.count_runs.side_effect = [1, 0, 0]
        self.service.repository.add_run.return_value = expected_run

        run = self.service.enqueue_run(
            task.id,
            trigger_type=TriggerType.MANUAL,
            max_queued_runs=100,
        )

        self.assertIs(run, expected_run)
        self.service.repository.add_run.assert_called_once_with(
            task,
            trigger_type=TriggerType.MANUAL,
            status=RunStatus.SKIPPED,
            error_message="Task concurrency limit reached.",
            scheduled_at=None,
        )

    def test_queues_run_and_snapshots_parameters(self) -> None:
        task = make_task(overlap_policy="queue", queue_limit=2)
        expected_run = object()
        self.service.repository.get_task.return_value = task
        self.service.repository.count_runs.side_effect = [1, 1, 10]
        self.service.repository.add_run.return_value = expected_run

        run = self.service.enqueue_run(
            task.id,
            trigger_type=TriggerType.SCHEDULED,
            max_queued_runs=100,
            scheduled_at=datetime.now(UTC),
        )

        self.assertIs(run, expected_run)
        call = self.service.repository.add_run.call_args
        self.assertEqual(call.kwargs["status"], RunStatus.QUEUED)

    def test_completed_task_must_be_rescheduled_before_resume(self) -> None:
        task = make_task(state="completed")
        self.service.repository.get_task.return_value = task

        with self.assertRaisesRegex(TaskConflictError, "only paused tasks"):
            self.service.change_state(
                task.id,
                expected_version=task.version,
                target=TaskState.ACTIVE,
            )

    def test_completed_once_task_allows_non_schedule_edits(self) -> None:
        task = make_task(
            state="completed",
            schedule={
                "type": "once",
                "run_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            },
        )
        self.service.repository.get_task.return_value = task

        updated = self.service.update_task(
            task.id,
            TaskUpdate(version=task.version, description="Historical task"),
        )

        self.assertIs(updated, task)
        self.assertEqual(task.state, "completed")
        self.assertEqual(task.description, "Historical task")


class SchedulerRepositoryTestCase(unittest.TestCase):
    def test_run_keeps_a_deep_parameter_snapshot(self) -> None:
        session = Mock()
        task = make_task(parameters={"symbols": ["000001.SZ"]})

        run = SchedulerRepository(session).add_run(
            task,
            trigger_type=TriggerType.MANUAL,
            status=RunStatus.QUEUED,
        )
        task.parameters["symbols"].append("600519.SH")

        self.assertEqual(run.task_type, TEST_TASK_TYPE)
        self.assertEqual(run.parameters, {"symbols": ["000001.SZ"]})

    def test_lists_latest_run_for_each_task_with_one_ranked_query(self) -> None:
        session = Mock()
        first_task_id, second_task_id = uuid4(), uuid4()
        first_run = SimpleNamespace(task_id=first_task_id)
        second_run = SimpleNamespace(task_id=second_task_id)
        session.scalars.return_value = [first_run, second_run]

        latest_runs = SchedulerRepository(session).list_latest_runs_for_tasks(
            [first_task_id, second_task_id]
        )

        self.assertEqual(
            latest_runs,
            {first_task_id: first_run, second_task_id: second_run},
        )
        statement = session.scalars.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("row_number() OVER", sql)
        self.assertIn("PARTITION BY task_runs.task_id", sql)
        self.assertIn("ORDER BY task_runs.created_at DESC, task_runs.id DESC", sql)

    def test_skips_latest_run_query_when_task_list_is_empty(self) -> None:
        session = Mock()

        latest_runs = SchedulerRepository(session).list_latest_runs_for_tasks([])

        self.assertEqual(latest_runs, {})
        session.scalars.assert_not_called()


class SchedulerRuntimeTestCase(unittest.TestCase):
    def test_default_registry_registers_etf_daily_incremental_sync_task(self) -> None:
        definition = task_registry.require("data.sync_etf_daily_incremental")

        self.assertEqual(definition.name, "ETF日线增量采集")
        self.assertEqual(
            definition.english_name, "Incremental Tushare ETF daily bars"
        )

    def test_default_registry_registers_etf_daily_full_sync_task(self) -> None:
        definition = task_registry.require("data.sync_etf_daily_full")

        self.assertEqual(definition.name, "ETF日线全量采集")
        self.assertEqual(definition.english_name, "Full Tushare ETF daily bars")

    def test_default_registry_registers_etf_basic_sync_task(self) -> None:
        definition = task_registry.require("data.sync_etf_basics")

        self.assertEqual(definition.name, "ETF基础信息采集")
        self.assertEqual(definition.english_name, "Sync Tushare ETF basics")

    def test_default_registry_registers_trade_calendar_sync_task(self) -> None:
        definition = task_registry.require("data.sync_trade_calendar")

        self.assertEqual(definition.name, "交易日历采集")
        self.assertEqual(definition.english_name, "Sync Tushare trade calendar")

    def test_disabled_runtime_does_not_start_scheduler(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            scheduler_enabled=False,
            _env_file=None,
        )
        runtime = SchedulerRuntime(settings)

        runtime.start()
        try:
            self.assertFalse(runtime.running)
        finally:
            runtime.stop()

    def test_dispatch_interval_is_registered_in_seconds(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            scheduler_dispatch_interval_ms=500,
            _env_file=None,
        )
        with (
            patch("app.scheduling.runtime.Session") as session_class,
            patch("app.scheduling.runtime.get_engine"),
            patch("app.scheduling.runtime.SchedulerRepository") as repository_class,
        ):
            repository_class.return_value.interrupt_running_runs.return_value = 0
            repository_class.return_value.list_active_task_ids.return_value = []
            runtime = SchedulerRuntime(settings)
            runtime.scheduler = Mock()
            runtime.start()

        runtime.scheduler.add_job.assert_called_once_with(
            runtime.dispatch_queued_runs,
            trigger="interval",
            seconds=0.5,
            id="scheduler:dispatch",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        session_class.return_value.__enter__.return_value.commit.assert_called_once_with()
        runtime.stop()

    def test_worker_crash_finalizes_lingering_running_run(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            scheduler_enabled=False,
            _env_file=None,
        )
        runtime = SchedulerRuntime(settings)
        run_id = uuid4()
        future: Future[None] = Future()

        with patch.object(runtime, "_finish_running_run") as finish_running_run:
            with runtime._futures_lock:
                runtime._futures[future] = run_id
            future.set_exception(ConnectionError("Tushare connection dropped"))
            runtime._on_run_future_done(future)

        finish_running_run.assert_called_once_with(
            run_id,
            status=RunStatus.FAILED,
            error_type="ConnectionError",
            error_message="Tushare connection dropped",
        )
        runtime.stop()


if __name__ == "__main__":
    unittest.main()
