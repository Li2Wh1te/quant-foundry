"""Verify source admission races using PostgreSQL locks in the disposable CI DB."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
from threading import Event
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data_sources.models import DataSourceConfig
from app.data_sources.providers import PROVIDERS, TushareProvider
from app.data_sources.service import DataSourceService, lock_source_gates, skip_source_queue
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskDefinition, TaskRegistry
from app.scheduling.repository import SchedulerRepository
from app.scheduling.schemas import RunStatus, TriggerType
from app.scheduling.service import SchedulerService, TaskConflictError
from tests.test_data_sources import settings


@unittest.skipUnless(os.getenv("POSTGRES_TEST_ENABLED") == "1", "requires disposable PostgreSQL")
class SourceGateRaceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(get_settings().database_url)
        self.key = "race." + uuid4().hex
        self.registry = TaskRegistry()
        self.registry.register(TaskDefinition(key=self.key, name="并发测试", english_name="Concurrency test",
            source_key=self.key, parameters_model=BaseModel, handler=lambda *_: None))
        provider = TushareProvider()
        provider.key = self.key
        self.providers_patch = patch.dict(PROVIDERS, {self.key: provider})
        self.providers_patch.start()
        from app.data_sources.credentials import encrypt
        with Session(self.engine) as session:
            session.add(DataSourceConfig(key=self.key, initialized=True, enabled=True,
                values={"api_url": "https://example.test"},
                encrypted_secrets=encrypt(settings(), self.key, {"token": "synthetic"})))
            task = ScheduledTask(name="并发测试", task_type=self.key, parameters={}, parameter_version=1,
                schedule={"type": "interval", "seconds": 60,
                          "start_at": datetime.now(UTC).isoformat()}, state="active")
            session.add(task)
            session.flush()
            self.task_id = task.id
            run = SchedulerRepository(session).add_run(task, trigger_type=TriggerType.MANUAL, status=RunStatus.QUEUED)
            self.run_id = run.id
            session.commit()

    def tearDown(self):
        self.providers_patch.stop()
        with Session(self.engine) as session:
            session.execute(delete(TaskRun).where(TaskRun.task_type == self.key))
            session.execute(delete(ScheduledTask).where(ScheduledTask.task_type == self.key))
            session.execute(delete(DataSourceConfig).where(DataSourceConfig.key == self.key))
            session.commit()
        self.engine.dispose()

    def wait_for_lock(self, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.engine.connect() as connection:
                waiting = connection.scalar(text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid})
            if waiting:
                return
            time.sleep(0.02)
        self.fail("concurrent request did not wait for the source row lock")

    def race(self, held_action, waiting_action):
        ready = Event()
        pids = []
        def worker():
            with Session(self.engine) as session:
                session.execute(text("SET lock_timeout = '8s'"))
                pids.append(session.scalar(text("SELECT pg_backend_pid()")))
                ready.set()
                result = waiting_action(session)
                session.commit()
                return result
        with Session(self.engine) as held, ThreadPoolExecutor(max_workers=1) as pool:
            held_action(held)
            future = pool.submit(worker)
            try:
                self.assertTrue(ready.wait(3))
                self.wait_for_lock(pids[0])
            finally:
                # Always release the row before waiting for the worker so a
                # failed assertion cannot deadlock test cleanup.
                held.commit()
            return future.result(timeout=10)

    def test_disable_wins_before_manual_enqueue(self):
        def disable(session):
            row = lock_source_gates(session, self.registry)[self.key]
            row.enabled = False
            skip_source_queue(session, [self.key], "数据源已停用。")
        def enqueue(session):
            with self.assertRaises(TaskConflictError):
                SchedulerService(session, self.registry).enqueue_run(self.task_id,
                    trigger_type=TriggerType.MANUAL, max_queued_runs=100)
            return True
        self.assertTrue(self.race(disable, enqueue))

    def test_disable_wins_before_queue_claim(self):
        def disable(session):
            lock_source_gates(session, self.registry)[self.key].enabled = False
            skip_source_queue(session, [self.key], "数据源已停用。")
        def claim(session):
            row = lock_source_gates(session, self.registry)[self.key]
            if not row.enabled:
                skip_source_queue(session, [self.key], "数据源已停用。")
            ids = SchedulerRepository(session).claim_queued_runs(100, task_types=[self.key])
            return self.run_id in ids
        self.assertFalse(self.race(disable, claim))

    def test_claim_wins_and_disable_preserves_running_execution(self):
        def claim(session):
            lock_source_gates(session, self.registry)
            self.assertIn(self.run_id, SchedulerRepository(session).claim_queued_runs(100, task_types=[self.key]))
        def disable(session):
            return DataSourceService(session, settings(), self.registry).set_enabled(self.key, 1, False)["skipped_runs"]
        self.assertEqual(self.race(claim, disable), 0)
        with Session(self.engine) as session:
            self.assertEqual(session.get(TaskRun, self.run_id).status, "running")

    def test_enqueue_wins_and_disable_skips_the_new_queue(self):
        def enqueue(session):
            task = session.get(ScheduledTask, self.task_id)
            task.overlap_policy, task.concurrency_limit = "queue", 2
            session.flush()
            SchedulerService(session, self.registry).enqueue_run(self.task_id,
                trigger_type=TriggerType.MANUAL, max_queued_runs=100)
        def disable(session):
            return DataSourceService(session, settings(), self.registry).set_enabled(self.key, 1, False)["skipped_runs"]
        self.assertEqual(self.race(enqueue, disable), 2)
