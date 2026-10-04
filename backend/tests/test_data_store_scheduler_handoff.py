"""Paused handoff persistence and restart admission in a disposable PG schema."""
from uuid import UUID
from unittest.mock import Mock
import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_db_session
from app.main import create_app
from app.data_store.scheduler_tasks import TASK_KEY, register_tasks
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskRegistry
from app.scheduling.runtime import SchedulerRuntime
from app.scheduling.schemas import TaskState, TriggerType
from app.scheduling.service import SchedulerService, TaskConflictError
from tests.test_data_store_api import ASGIClient, TOKEN
from tests.test_data_store_kernel import database  # noqa: F401 - owned schema fixture


pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1",
                                reason="isolated PostgreSQL required")


def test_paused_task_survives_http_creation_and_restart_without_early_enqueue(database, monkeypatch):
    engine, _ = database
    ScheduledTask.__table__.create(engine)
    TaskRun.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE data_store_legacy_maintenance (
            singleton integer PRIMARY KEY, phase text, plan_hash text,
            completed_json jsonb DEFAULT '[]', files_started boolean DEFAULT false)"""))
        connection.execute(text("INSERT INTO data_store_legacy_maintenance(singleton,phase) VALUES (1,'reset_done')"))
    settings = Settings(api_token=TOKEN, cursor_signing_key="b" * 64,
                        database_password="isolated-test", environment="test",
                        scheduler_enabled=False, _env_file=None)
    app = create_app(settings)
    http_runtime = SchedulerRuntime(settings)
    app.state.scheduler_runtime = http_runtime

    def sessions():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = sessions
    client = ASGIClient(app)
    try:
        response = client.post("/api/admin/tasks", headers={"Authorization": "Bearer " + TOKEN}, json={
            "name": "本地更新交接", "task_type": TASK_KEY, "parameters": {},
            "initial_state": "paused", "concurrency_limit": 1, "overlap_policy": "skip", "queue_limit": 1,
            "schedule": {"type": "cron", "expression": "0 * * * *", "timezone": "Asia/Shanghai"},
        })
    finally:
        http_runtime.stop()
    assert response.status_code == 201, response.text
    task_id = UUID(response.json()["id"])
    assert response.json()["state"] == "paused" and response.json()["version"] == 1
    with Session(engine) as session:
        saved = session.get(ScheduledTask, task_id)
        assert saved.state == "paused" and saved.parameters["datasets"] == []
        assert saved.parameters["maximum_passes"] == 256 and saved.parameters["pass_seconds"] == 300
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0

    registry = TaskRegistry()
    register_tasks(registry)
    with Session(engine) as session:
        # Paused tasks allow manual execution in the ordinary scheduler, but
        # local updates reject an incomplete reset receipt regardless of its
        # phase label. Final R01 readiness is a separate guard.
        with pytest.raises(TaskConflictError, match="维护"):
            SchedulerService(session, registry).enqueue_run(
                task_id, trigger_type=TriggerType.MANUAL, max_queued_runs=10)
        session.rollback()

    monkeypatch.setattr("app.scheduling.runtime.get_engine", lambda: engine)
    runtime_settings = settings.model_copy(update={"scheduler_enabled": True})

    def task_jobs_after_restart():
        # Read persisted state through the real repository in a new runtime.
        # The clock/scheduler is mocked so no source work or timed job can run.
        runtime = SchedulerRuntime(runtime_settings, registry=registry)
        runtime.scheduler = Mock()
        try:
            runtime.start()
            return [call.kwargs["id"] for call in runtime.scheduler.add_job.call_args_list
                    if call.kwargs["id"] != "scheduler:dispatch"]
        finally:
            runtime.stop()

    assert task_jobs_after_restart() == []
    with engine.begin() as connection:
        assert connection.execute(text("SELECT phase FROM data_store_legacy_maintenance")).scalar_one() == "reset_done"
        # Complete only the synthetic reset safety receipt. Pending business
        # input can now be consumed without falsely completing all-60 finish.
        connection.execute(text("""UPDATE data_store_legacy_maintenance SET
            plan_hash=:hash, completed_json='["hooks","derived","originals","functions","files"]',
            files_started=true"""), {'hash': 'a' * 64})
    with Session(engine) as session:
        resumed = SchedulerService(session, registry).change_state(
            task_id, expected_version=1, target=TaskState.ACTIVE)
        assert resumed.version == 2
        session.commit()
    assert task_jobs_after_restart() == [SchedulerRuntime.task_job_id(task_id)]
    assert task_jobs_after_restart() == [SchedulerRuntime.task_job_id(task_id)]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ScheduledTask)) == 1
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
        assert session.execute(text('SELECT phase FROM data_store_legacy_maintenance')).scalar_one() == 'reset_done'
        queued = SchedulerService(session, registry).enqueue_run(
            task_id, trigger_type=TriggerType.MANUAL, max_queued_runs=10)
        assert queued.status == 'queued'
        session.rollback()


def test_dispatch_keeps_healthy_queue_claims_after_real_maintenance_lock_timeout(database, monkeypatch):
    from concurrent.futures import Future
    from app.data_store.availability import require_operable
    from app.data_store.errors import DataStoreError
    from app.scheduling.schemas import TaskCreate
    from tests.test_scheduling import make_test_registry, TEST_TASK_TYPE

    engine, _ = database
    ScheduledTask.__table__.create(engine)
    TaskRun.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE data_store_legacy_maintenance (
            singleton integer PRIMARY KEY, phase text, plan_hash text,
            completed_json jsonb DEFAULT '[]', files_started boolean DEFAULT false)"""))
        connection.execute(text("""INSERT INTO data_store_legacy_maintenance VALUES
            (1,'reset_done',:hash,'["hooks","derived","originals","functions","files"]',true)"""),
            {'hash': 'a' * 64})
    registry = make_test_registry()
    register_tasks(registry)
    with Session(engine) as session:
        service = SchedulerService(session, registry)
        runs = {}
        for task_type in (TASK_KEY, TEST_TASK_TYPE):
            task = service.create_task(TaskCreate.model_validate({
                'name': task_type, 'task_type': task_type, 'parameters': {},
                'schedule': {'type': 'cron', 'expression': '0 * * * *'},
            }))
            runs[task_type] = service.enqueue_run(task.id,
                trigger_type=TriggerType.MANUAL, max_queued_runs=10).id
        session.commit()
    monkeypatch.setattr('app.scheduling.runtime.get_engine', lambda: engine)
    errors = []
    def gate(session):
        try:
            return require_operable(session)
        except DataStoreError as error:
            errors.append(error.code)
            raise
    monkeypatch.setattr('app.scheduling.runtime.require_operable', gate)
    settings = Settings(api_token=TOKEN, cursor_signing_key='b' * 64,
        database_password='isolated-test', environment='test', _env_file=None)
    runtime = SchedulerRuntime(settings, registry=registry)
    submitted = []
    def submit(callback, run_id):
        # Observe dispatch without starting either synthetic handler or a writer.
        submitted.append(run_id)
        return Future()
    monkeypatch.setattr(runtime.executor, 'submit', submit)
    try:
        with engine.begin() as maintenance:
            maintenance.execute(text('LOCK TABLE data_store_legacy_maintenance IN EXCLUSIVE MODE'))
            runtime.dispatch_queued_runs()
            assert errors == ['LOCK_TIMEOUT']
            assert submitted == [runs[TEST_TASK_TYPE]]
            with Session(engine) as session:
                assert session.get(TaskRun, runs[TASK_KEY]).status == 'queued'
                assert session.get(TaskRun, runs[TEST_TASK_TYPE]).status == 'running'
    finally:
        runtime.stop()
