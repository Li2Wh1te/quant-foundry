"""Completed-reset writer admission on an isolated PostgreSQL schema.

The production receipt motivates the regression; all fixtures here are synthetic.
No test starts a supplier request or changes a production maintenance receipt.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import os
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from pydantic import SecretStr
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry
from app.data_store import scheduler_tasks as tasks
from app.data_store.availability import (
    check_local_update_ready, read_availability, require_local_update_ready,
    require_ready,
)
from app.data_store.errors import DataStoreError
from app.legacy_reset.catalog import ResetRefused
from app.legacy_reset.operations import finish_rebuild
from app.scheduling import runtime as scheduling_runtime
from tests.test_data_store_kernel import database, limits, store, KEY  # noqa: F401
from tests.test_data_store_domain_samples import ready  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                                reason='isolated PostgreSQL required')
GROUPS = ['hooks', 'derived', 'originals', 'functions', 'files']


def install_admission(engine):
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE data_store_legacy_maintenance ('
                         'singleton integer PRIMARY KEY, phase text, plan_hash text, '
                         'completed_json jsonb, files_started boolean)')
        c.execute(text("INSERT INTO data_store_legacy_maintenance VALUES "
                       "(1,'reset_done',:hash,CAST(:groups AS jsonb),true)"),
                  {'hash': 'a' * 64, 'groups': json.dumps(GROUPS)})
        c.exec_driver_sql('CREATE TABLE scheduled_tasks (id uuid PRIMARY KEY, '
                         'task_type text, state text, version integer)')
        c.exec_driver_sql('CREATE TABLE task_runs (id uuid PRIMARY KEY, task_id uuid, '
                         'task_type text, status text)')
        c.exec_driver_sql('CREATE TABLE IF NOT EXISTS data_store_entry_status ('
                         'entry_id text PRIMARY KEY, summary_json text, '
                         'updated_at timestamptz DEFAULT clock_timestamp())')
        c.exec_driver_sql('CREATE TABLE data_store_legacy_restrictions ('
                         'origin_key text PRIMARY KEY, located boolean)')


@pytest.fixture
def admission(database):
    install_admission(database[0])
    return database[0]


def phase(engine):
    with engine.connect() as c:
        return c.scalar(text('SELECT phase FROM data_store_legacy_maintenance'))


def test_complete_reset_allows_local_work_without_global_ready_or_finish(admission):
    with Session(admission) as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        state = check_local_update_ready(session)
        assert state.phase == 'reset_done' and not state.ready and not state.fresh_install
        with pytest.raises(DataStoreError, match='维护'):
            require_ready(session)
    with admission.connect() as c:
        database_name = c.scalar(text('SELECT current_database()'))
    with pytest.raises(ResetRefused) as error:
        finish_rebuild(admission, expect_database=database_name)
    assert error.value.code == 'REBUILD_NOT_READY' and phase(admission) == 'reset_done'


@pytest.mark.parametrize('field,value', [
    ('phase', 'rebuilding'), ('phase', 'resetting'), ('phase', 'unknown'),
    ('plan_hash', None), ('plan_hash', 'not-a-plan-hash'),
    ('completed_json', GROUPS[:-1]), ('completed_json', list(reversed(GROUPS))),
    ('files_started', False),
])
def test_incomplete_or_malformed_reset_cannot_admit_work(admission, field, value):
    with admission.begin() as c:
        expression = 'CAST(:value AS jsonb)' if field == 'completed_json' else ':value'
        c.execute(text(f'UPDATE data_store_legacy_maintenance SET {field}={expression}'),
                  {'value': json.dumps(value) if field == 'completed_json' else value})
    with Session(admission) as session:
        with pytest.raises(DataStoreError) as error:
            require_local_update_ready(session)
        assert error.value.code == 'DATA_STORE_REBUILDING'
        assert session.scalar(text('SELECT 1')) == 1


@pytest.mark.parametrize('kind', ['table', 'function', 'trigger', 'active_task',
                                  'queued_run', 'running_run'])
def test_reintroduced_retired_objects_and_writers_block_completed_receipt(admission, kind):
    with admission.begin() as c:
        if kind == 'table':
            c.exec_driver_sql('CREATE TABLE foundation_returned (id integer)')
        elif kind == 'function':
            c.exec_driver_sql("CREATE FUNCTION foundation_returned() RETURNS integer "
                             "LANGUAGE sql AS 'SELECT 1'")
        elif kind == 'trigger':
            c.exec_driver_sql('CREATE TABLE guard_inputs (id integer)')
            c.exec_driver_sql('CREATE FUNCTION guard_trigger() RETURNS trigger LANGUAGE plpgsql '
                             'AS $$BEGIN RETURN NEW; END$$')
            c.exec_driver_sql('CREATE TRIGGER foundation_hook BEFORE INSERT ON guard_inputs '
                             'FOR EACH ROW EXECUTE FUNCTION guard_trigger()')
        elif kind == 'active_task':
            c.execute(text("INSERT INTO scheduled_tasks VALUES (:id,"
                           "'foundation.formalize_local_updates','active',1)"), {'id': uuid4()})
        else:
            c.execute(text("INSERT INTO task_runs VALUES (:id,:task,"
                           "'foundation.formalize_local_updates',:status)"),
                      {'id': uuid4(), 'task': uuid4(), 'status': kind.removesuffix('_run')})
    with Session(admission) as session:
        with pytest.raises(DataStoreError) as error:
            require_local_update_ready(session)
        assert error.value.code == 'DATA_STORE_REBUILDING'


def test_preserved_paused_retired_definition_is_not_a_running_writer(admission):
    with admission.begin() as c:
        c.execute(text("INSERT INTO scheduled_tasks VALUES (:id,"
                       "'foundation.formalize_local_updates','paused',14)"), {'id': uuid4()})
    with Session(admission) as session:
        assert require_local_update_ready(session).phase == 'reset_done'
    assert phase(admission) == 'reset_done'


def test_maintenance_fence_is_held_until_outer_transaction_ends(admission):
    with Session(admission) as session:
        session.execute(text("SET LOCAL lock_timeout='250ms'"))
        require_local_update_ready(session)
        assert session.scalar(text('SHOW lock_timeout')) == '250ms'
        with pytest.raises(OperationalError) as error:
            with admission.begin() as c:
                c.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                c.exec_driver_sql("UPDATE data_store_legacy_maintenance SET phase='resetting'")
        assert error.value.orig.sqlstate == '55P03'
    with admission.begin() as c:
        c.exec_driver_sql("UPDATE data_store_legacy_maintenance SET phase='resetting'")
    assert phase(admission) == 'resetting'


def test_gate_lock_timeout_does_not_abort_unrelated_claim_transaction(admission):
    with admission.begin() as blocker:
        blocker.exec_driver_sql('LOCK TABLE data_store_legacy_maintenance IN ACCESS EXCLUSIVE MODE')
        with Session(admission) as session:
            with pytest.raises(DataStoreError) as error:
                require_local_update_ready(session)
            assert error.value.code == 'LOCK_TIMEOUT'
            assert session.scalar(text('SELECT 1')) == 1


def test_finish_waits_before_catalogue_locks_and_keeps_strict_conditions(admission):
    waiting = Event()
    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if statement == 'LOCK TABLE data_store_legacy_maintenance IN SHARE ROW EXCLUSIVE MODE':
            waiting.set()
    event.listen(admission, 'before_cursor_execute', before_execute)
    try:
        with admission.connect() as c:
            database_name = c.scalar(text('SELECT current_database()'))
        with ThreadPoolExecutor(max_workers=1) as pool, Session(admission) as writer:
            require_local_update_ready(writer)
            future = pool.submit(finish_rebuild, admission, expect_database=database_name)
            assert waiting.wait(2)
            writer.execute(text("SET LOCAL lock_timeout='200ms'"))
            writer.execute(text('UPDATE data_store_entry_status SET summary_json=summary_json'))
            writer.rollback()
            with pytest.raises(ResetRefused) as error:
                future.result(timeout=3)
            assert error.value.code == 'REBUILD_NOT_READY'
        assert phase(admission) == 'reset_done'
    finally:
        event.remove(admission, 'before_cursor_execute', before_execute)


@pytest.mark.parametrize('qualified', [True, False])
def test_handler_retains_fence_and_bounds_legacy_plan_without_promoting_quality(
        admission, monkeypatch, tmp_path, qualified):
    monkeypatch.setattr(tasks, 'get_engine', lambda: admission)
    monkeypatch.setattr(tasks, 'get_settings', lambda: SimpleNamespace(
        data_store_root=tmp_path, cursor_signing_key=SecretStr('isolated-key' * 4)))
    monkeypatch.setattr(tasks, 'CurrentStore', lambda *a, **k: nullcontext(object()))
    monkeypatch.setattr(tasks, 'NativeSources', lambda *a, **k: object())
    observed = []
    def run(store, sources, *, entries, options):
        observed.append((options.maximum_passes, options.pass_seconds, [e.id for e in entries]))
        with pytest.raises(OperationalError):
            with admission.begin() as c:
                c.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                c.exec_driver_sql("UPDATE data_store_legacy_maintenance SET phase='resetting'")
        return [{'entry_id': 'E41', 'complete': True, 'qualified': qualified}]
    monkeypatch.setattr(tasks, 'run_local', run)
    parameters = tasks.LocalUpdateParameters(datasets=['fund.company.reported'],
                                             maximum_passes=4096, pass_seconds=300)
    context = TaskContext(task_id=uuid4(), run_id=uuid4())
    if qualified:
        assert tasks.update_local(context, parameters)['complete']
    else:
        with pytest.raises(RuntimeError, match='DATA_STORE_UPDATE_INCOMPLETE'):
            tasks.update_local(context, parameters)
    assert observed == [(1, 180, ['E41'])]
    assert parameters.maximum_passes == 4096 and parameters.pass_seconds == 300
    with Session(admission) as session:
        assert not read_availability(session).ready
    assert phase(admission) == 'reset_done'


def test_real_native_refresh_keeps_healthy_siblings_moving_before_global_finish(ready, monkeypatch):
    from tests.test_data_store_failure_isolation import A, B, C, changed, values
    from app.data_store.pipeline import read_entry_status
    engine = ready.catalog.engine
    install_admission(engine)
    monkeypatch.setattr(tasks, 'get_engine', lambda: engine)
    monkeypatch.setattr(tasks, 'get_settings', lambda: SimpleNamespace(
        data_store_root=ready.files.root, cursor_signing_key=SecretStr(KEY.decode())))
    with engine.begin() as c:
        c.exec_driver_sql('ALTER TABLE trading_calendar_days RENAME TO missing_calendar')
    changed(ready, 10)
    with pytest.raises(RuntimeError, match='DATA_STORE_UPDATE_INCOMPLETE'):
        tasks.update_local(TaskContext(task_id=uuid4(), run_id=uuid4()),
            tasks.LocalUpdateParameters(datasets=[e.spec.name for e in (B, A, C)],
                                        maximum_passes=4096, pass_seconds=300))
    values(ready, 10)
    assert read_entry_status(ready, B.id)['refresh']['outcome'] == 'failed'
    assert all(read_entry_status(ready, e.id)['refresh']['outcome'] == 'updated' for e in (A, C))
    assert phase(engine) == 'reset_done'


@pytest.mark.parametrize('maintenance_phase,allowed', [('reset_done', True), ('resetting', False)])
def test_runtime_claims_use_independent_admission_and_keep_other_types(
        admission, monkeypatch, maintenance_phase, allowed):
    with admission.begin() as c:
        c.execute(text('UPDATE data_store_legacy_maintenance SET phase=:phase'),
                  {'phase': maintenance_phase})
        c.execute(text('INSERT INTO task_runs VALUES (:id,:task,:type,\'queued\')'),
                  {'id': uuid4(), 'task': uuid4(), 'type': tasks.TASK_KEY})
    registry = TaskRegistry()
    registry.register(TaskDefinition(key=tasks.TASK_KEY, name='local', english_name='Local',
                                     parameters_model=tasks.LocalUpdateParameters, handler=tasks.update_local))
    registry.register(TaskDefinition(key='test.collector', name='collector', english_name='Collector',
                                     parameters_model=tasks.LocalUpdateParameters, handler=lambda *a: {}))
    repository = Mock()
    repository.claim_queued_runs.return_value = []
    monkeypatch.setattr(scheduling_runtime, 'get_engine', lambda: admission)
    monkeypatch.setattr(scheduling_runtime, 'lock_source_gates', lambda *a: {})
    monkeypatch.setattr(scheduling_runtime, 'skip_source_queue', lambda *a: None)
    monkeypatch.setattr(scheduling_runtime, 'SchedulerRepository', lambda *a: repository)
    runtime = scheduling_runtime.SchedulerRuntime(Settings(api_token='a' * 64,
        database_password='isolated', scheduler_enabled=False, _env_file=None), registry)
    try:
        runtime.dispatch_queued_runs()
        types = repository.claim_queued_runs.call_args.kwargs['task_types']
        assert (tasks.TASK_KEY in types) is allowed and 'test.collector' in types
    finally:
        runtime.executor.shutdown()
