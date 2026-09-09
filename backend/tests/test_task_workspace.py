"""Exercise workspace projections on SQLite and the CI PostgreSQL database."""

from datetime import UTC, datetime, timedelta
import os
import unittest
from unittest.mock import Mock
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import Column, JSON, MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data_sources.models import DataSourceConfig
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskDefinition, TaskRegistry
from app.scheduling.workspace import task_workspace


class TaskWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.postgres = os.getenv('POSTGRES_TEST_ENABLED') == '1'
        self.engine = create_engine(get_settings().database_url if self.postgres else 'sqlite://')
        if not self.postgres:
            metadata = MetaData()
            for model in (DataSourceConfig, ScheduledTask, TaskRun):
                Table(model.__tablename__, metadata, *[
                    Column(col.name, JSON() if isinstance(col.type, JSONB) else col.type, primary_key=col.primary_key)
                    for col in model.__table__.columns])
            metadata.create_all(self.engine)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin() if self.postgres else None
        self.session = Session(self.connection, join_transaction_mode='create_savepoint')
        self.now = datetime.now(UTC)
        self.source_key = 'fixture.' + uuid4().hex
        self.task_type = 'test.' + uuid4().hex
        self.registry = TaskRegistry()
        self.registry.register(TaskDefinition(key=self.task_type, name='测试交易日历', english_name='Calendar Fixture',
            parameters_model=BaseModel, handler=lambda *_: None, source_key=self.source_key))
        self.source = DataSourceConfig(key=self.source_key, initialized=True, enabled=True,
            encrypted_secrets='fixture-only-not-a-credential', values={}, updated_at=self.now)
        self.session.add(self.source)
        self.runtime = Mock()
        self.runtime.next_run_at.return_value = self.now + timedelta(hours=1)
        self.session.flush()

    def tearDown(self):
        self.session.close()
        if self.transaction is not None:
            self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def task(self, **overrides):
        values = dict(name='日历任务', task_type=self.task_type, state='active', parameters={},
            schedule={'type':'once','run_at':(self.now+timedelta(hours=1)).isoformat()},
            created_at=self.now, updated_at=self.now)
        values.update(overrides)
        task = ScheduledTask(**values)
        self.session.add(task)
        self.session.flush()
        return task

    def add_run(self, task, status, step):
        when = self.now + timedelta(seconds=step)
        # Mirror the production lifecycle constraints so PostgreSQL fixtures
        # represent valid executions, including skips that never started.
        started = when if status not in ('queued', 'skipped', 'interrupted') else None
        finished = when if status not in ('queued', 'running') else None
        run = TaskRun(task_id=task.id, task_version=1, task_type=task.task_type, trigger_type='manual',
            parameters={}, parameter_version=1, status=status, created_at=when, available_at=when,
            started_at=started, finished_at=finished)
        self.session.add(run)
        self.session.flush()
        return run

    def page(self, **options):
        # Unique source ownership isolates each case from shared CI fixtures.
        return task_workspace(self.session, self.runtime, self.registry, source_key=self.source_key, **options)

    def test_old_running_executions_survive_newer_history_and_pagination(self):
        task = self.task()
        for step in range(2):
            self.add_run(task, 'running', step)
        for step in range(2, 5):
            self.add_run(task, 'queued', step)
        for step in range(5, 126):
            latest = self.add_run(task, 'skipped', step)
        page = self.page(status='running', limit=1)
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].running_count, 2)
        self.assertEqual(page.items[0].queued_count, 3)
        self.assertEqual(page.items[0].latest_run.id, latest.id)
        self.assertEqual(self.page(status='queued').total, 1)
        self.assertEqual(self.page(status='attention').total, 0)

    def test_total_is_filtered_before_pagination_and_ties_have_stable_order(self):
        tasks = [self.task(name=f'同日任务{i}') for i in range(5)]
        self.task(state='archived')
        first = self.page(limit=2)
        second = self.page(limit=2, offset=2)
        self.assertEqual(first.total, 5)
        self.assertEqual(second.total, 5)
        expected = sorted((task.id for task in tasks), reverse=True)
        self.assertEqual([item.id for item in first.items + second.items], expected[:4])
        self.assertEqual(self.page(limit=2, offset=99).items, [])
        self.assertEqual(self.page(limit=2, offset=99).total, 5)

    def test_search_matches_bilingual_type_names_and_escapes_wildcards(self):
        special = self.task(name='范围100%_A')
        self.task(name='普通任务')
        self.assertEqual(self.page(query='calendar fixture').total, 2)
        self.assertEqual(self.page(query='交易日历').total, 2)
        for query in ('%', '_', '  100%_  '):
            self.assertEqual([item.id for item in self.page(query=query).items], [special.id])
        self.assertEqual(self.page(query='不存在').total, 0)
        self.assertEqual(task_workspace(self.session, self.runtime, self.registry, source_key='unknown').total, 0)

    def test_task_state_filters_are_independent_from_live_runs(self):
        paused = self.task(state='paused')
        self.add_run(paused, 'running', 0)
        completed = self.task(state='completed')
        self.assertEqual([item.id for item in self.page(status='paused').items], [paused.id])
        self.assertEqual([item.id for item in self.page(status='running').items], [paused.id])
        self.assertEqual([item.id for item in self.page(status='completed').items], [completed.id])
        self.assertEqual(self.page(status='active').total, 0)
        self.assertTrue(all(item.next_run_at is None for item in self.page().items))

    def test_attention_tracks_latest_failure_not_obsolete_failures(self):
        for status in ('failed', 'interrupted', 'timed_out', 'indeterminate'):
            task = self.task()
            self.add_run(task, status, 0)
        recovered = self.task()
        self.add_run(recovered, 'failed', 0)
        self.add_run(recovered, 'succeeded', 1)
        self.assertEqual(self.page(status='attention').total, 4)
        self.assertNotIn(recovered.id, [item.id for item in self.page(status='attention').items])

    def test_source_gate_retains_plan_without_exposing_credentials(self):
        task = self.task()
        self.source.enabled = False
        self.session.flush()
        item = self.page().items[0]
        self.assertEqual(item.state, 'active')
        self.assertIsNone(item.next_run_at)
        self.assertFalse(item.source_enabled)
        self.assertTrue(item.source_configured)
        self.assertEqual(item.task_type_name, '测试交易日历')
        self.assertEqual(item.task_type_english_name, 'Calendar Fixture')
        self.assertNotIn('fixture-only', item.model_dump_json())
        self.assertEqual(item.schedule.type, 'once')
        self.runtime.next_run_at.assert_not_called()
        self.source.enabled = True
        self.source.initialized = False
        self.session.flush()
        self.assertIsNone(self.page().items[0].next_run_at)
        self.source.initialized = True
        self.session.flush()
        self.assertEqual(self.page().items[0].next_run_at, self.runtime.next_run_at.return_value)
        self.assertEqual(self.session.get(ScheduledTask, task.id).version, 1)

    def test_unregistered_history_remains_visible_in_unfiltered_workspace(self):
        unknown = self.task(task_type='removed.' + uuid4().hex, name='唯一历史'+self.source_key)
        page = task_workspace(self.session, self.runtime, self.registry, query=unknown.name)
        item = page.items[0]
        self.assertFalse(item.registered)
        self.assertIsNone(item.task_type_name)
        self.assertIsNone(item.source_key)
        self.assertIsNone(item.next_run_at)
        self.assertEqual(self.page().total, 0)


class TaskWorkspaceHttpTest(unittest.TestCase):
    def test_authentication_query_validation_and_response_envelope(self):
        import asyncio
        import json
        from unittest.mock import patch
        from app.core.config import Settings
        from app.db.session import get_db_session
        from app.main import create_app
        from app.scheduling.schemas import TaskWorkspaceResponse

        app = create_app(Settings(api_token='a' * 64, database_password='test-secret', _env_file=None))
        app.state.scheduler_runtime = Mock()
        app.dependency_overrides[get_db_session] = lambda: Mock()

        async def request(query=b'', authenticated=True):
            path = '/api/admin/task-workspace'
            scope = {'type':'http', 'asgi':{'version':'3.0','spec_version':'2.3'}, 'http_version':'1.1',
                'method':'GET', 'scheme':'http', 'path':path, 'raw_path':path.encode(), 'query_string':query,
                'headers':[(b'authorization', ('Bearer '+'a'*64).encode())] if authenticated else [],
                'client':('test-client',12345), 'server':('test-server',80), 'root_path':''}
            messages = []
            sent = False
            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type':'http.request','body':b'', 'more_body':False}
                await asyncio.Future()
            async def send(message):
                messages.append(message)
            await app(scope, receive, send)
            status = next(m['status'] for m in messages if m['type']=='http.response.start')
            body = b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
            return status, json.loads(body)

        with patch('app.scheduling.router.task_workspace', return_value=TaskWorkspaceResponse(
            items=[],total=0,limit=2,offset=4)) as read:
            self.assertEqual(asyncio.run(request(authenticated=False))[0],401)
            for query in (b'limit=0',b'limit=101',b'offset=-1',b'status=unknown',b'query='+b'x'*201):
                self.assertEqual(asyncio.run(request(query))[0],422)
            read.assert_not_called()
            status, body = asyncio.run(request(b'limit=2&offset=4&status=running&source_key=tushare&query=ETF'))
            self.assertEqual(status,200)
            self.assertEqual(body,{'items':[],'total':0,'limit':2,'offset':4})
            self.assertEqual(read.call_args.kwargs,{'query':'ETF','source_key':'tushare','status':'running','limit':2,'offset':4})
        app.dependency_overrides.clear()
