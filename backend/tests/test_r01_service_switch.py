"""Finite switch sequences and real read-only PG/ext4 admission proofs.

All failures are synthetic and isolated. No deployment host, supplier, task
pause, environment file or shared volume is accessed by this test module.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_data_store_kernel import database, store, limits  # noqa: F401

path = Path(__file__).resolve().parents[2] / 'scripts/r01_service_switch.py'
spec = importlib.util.spec_from_file_location('r01_service_switch', path)
switcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = switcher
spec.loader.exec_module(switcher)
PG = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                        reason='owned PostgreSQL/ext4 fixture required')


@pytest.fixture
def plan(tmp_path):
    return switcher.Plan(project_directory=str(tmp_path),
        compose_files=[str(tmp_path / 'compose.p01.yaml')],
        candidate_compose_file=str(tmp_path / 'candidate.yaml'),
        env_file=str(tmp_path / '.env'), manifest_file=str(tmp_path / 'manifest.json'),
        old_image='sha256:' + '1' * 64, candidate_image='sha256:' + '2' * 64,
        candidate_head='3' * 40, task_fingerprint='4' * 64,
        owned_definition_hash='5' * 64, definitions_hash='6' * 64)


def snapshot(plan):
    return {'task_count': 178, 'task_fingerprint': plan.task_fingerprint,
        'original_fingerprint': plan.original_fingerprint,
        'owned_definition_hash': plan.owned_definition_hash,
        'definitions_hash': '6' * 64, 'source_configs_hash': '7' * 64,
        'activity': {'accepted_runs': 0, 'backtests': 0, 'processors': 0},
        'records': [{'id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                     'identity_hash': '8' * 64, 'record_hash': '9' * 64}],
        'resources': {'held_locks': 0, 'lock_files': 16,
                      'retained_seals': [{'owner': 'pipeline.E23', 'progress_hash': 'a' * 64}]}}


class FakeGate:
    def __init__(self, host, number):
        self.host, self.number, self.checks = host, number, 0

    def drained(self):
        self.host.calls.append(('drained', self.number))
        if self.number == 2 and self.host.busy_candidate:
            raise switcher.Refused('ACCEPTED_WORK_NOT_IDLE')
        return {'task_count': 178, 'task_fingerprint': self.host.plan.task_fingerprint,
                'accepted_runs': 0, 'backtests': 0,
                'release_deadline_monotonic': time.monotonic() + 180}

    def check(self, seconds):
        self.checks += 1
        self.host.calls.append(('check', self.number, self.checks))
        if self.number == 1 and self.checks == self.host.expire_at:
            raise switcher.Refused('RELEASE_WINDOW_INSUFFICIENT')

    def release(self):
        self.host.calls.append(('release', self.number))
        if self.number == 1 and self.host.fail_release:
            raise switcher.Refused('RELEASE_NOT_CONFIRMED')

    def close(self):
        self.host.calls.append(('close', self.number))
        return True, 2


class FakeHost:
    def __init__(self, plan):
        self.plan, self.calls = plan, []
        self.current = {s: {'image': plan.old_image, 'running': True,
                            'stopped': False, 'status': 'running'} for s in switcher.SERVICES}
        self.gates = 0
        self.probes = 0
        self.expire_at = None
        self.fail_preflight = False
        self.fail_stop = None
        self.fail_release = False
        self.fail_start = False
        self.fail_health = False
        self.busy_candidate = False
        self.change = None

    def preflight(self):
        self.calls.append(('preflight',))
        if self.fail_preflight:
            raise switcher.Refused('COMPOSE_CHANGE_BEYOND_IMAGE')

    def probe(self, captured, resources):
        self.probes += 1
        self.calls.append(('probe', self.probes, resources))
        value = snapshot(self.plan)
        if self.change:
            self.change(self.probes, value)
        if not resources:
            value.pop('resources')
        return value

    def gate(self):
        self.gates += 1
        self.calls.append(('gate', self.gates))
        return FakeGate(self, self.gates)

    def states(self):
        return copy.deepcopy(self.current)

    def require_services(self, image, running):
        for service in running or switcher.SERVICES:
            assert self.current[service]['image'] == image
            assert self.current[service]['running'] == (service in running)

    def stop(self, service):
        self.calls.append(('stop', service))
        if service == self.fail_stop:
            raise switcher.Refused('GRACEFUL_STOP_NOT_COMPLETE')
        self.current[service].update(running=False, stopped=True, status='exited')

    def start(self, image, services):
        self.calls.append(('start', image, tuple(services)))
        for service in services:
            assert self.current[service]['stopped'], 'must never replace a running process'
            if self.fail_start and image == self.plan.candidate_image and service == 'runner':
                raise switcher.Refused('COMMAND_FAILED')
            self.current[service].update(image=image, running=True, stopped=False, status='running')

    def verify_candidate(self):
        self.calls.append(('verify_candidate',))
        if self.fail_health:
            raise switcher.Refused('BACKEND_NOT_HEALTHY')


def execute(plan, host):
    events = []
    code = switcher.switch(plan, host,
        lambda stage, message, **fields: events.append({'stage': stage, 'message': message, **fields}))
    return code, events


def test_uninterrupted_success_orders_stop_release_exit_before_up_and_preserves_seal(plan):
    host = FakeHost(plan)
    code, events = execute(plan, host)
    assert code == 0
    assert [c for c in host.calls if c[0] in ('gate', 'stop', 'release', 'start')] == [
        ('gate', 1), ('stop', 'backend'), ('stop', 'runner'), ('release', 1),
        ('start', plan.candidate_image, ('backend', 'runner'))]
    assert events[-1]['stage'] == 'completed'
    assert next(e for e in events if e['stage'] == 'drained')['resources']['retained_seals'] == [
        {'owner': 'pipeline.E23', 'progress_hash': 'a' * 64}]
    assert all('message' in e and any('\u4e00' <= c <= '\u9fff' for c in e['message']) for e in events)


@pytest.mark.parametrize('expiry', [1, 2])
def test_expired_gate_before_stop_leaves_both_old_services_running(plan, expiry):
    host = FakeHost(plan)
    host.expire_at = expiry
    code, events = execute(plan, host)
    assert code == 2 and events[-1]['stage'] == 'unchanged'
    assert not any(c[0] in ('stop', 'start') for c in host.calls)
    assert all(s['running'] and s['image'] == plan.old_image for s in host.states().values())


@pytest.mark.parametrize('expiry,missing', [(3, ('backend',)), (4, ('backend', 'runner')),
                                         (5, ('backend', 'runner'))])
def test_expiry_after_stop_recovers_once_without_claiming_switch_success(plan, expiry, missing):
    host = FakeHost(plan)
    host.expire_at = expiry
    code, events = execute(plan, host)
    assert code == 2 and not any(e['stage'] == 'completed' for e in events)
    assert [c for c in host.calls if c[0] == 'start'] == [('start', plan.old_image, missing)]
    assert events[-1]['restored_old'] is True and host.gates == 1


def test_partial_stop_failure_preserves_running_runner_and_restores_backend(plan):
    host = FakeHost(plan)
    host.fail_stop = 'runner'
    code, events = execute(plan, host)
    assert code == 2
    assert [c for c in host.calls if c[0] == 'start'] == [('start', plan.old_image, ('backend',))]
    assert events[-1]['restored_old'] is True


def test_failed_release_after_both_stops_restarts_old_without_candidate_start(plan):
    host = FakeHost(plan)
    host.fail_release = True
    code, events = execute(plan, host)
    assert code == 2
    assert [c for c in host.calls if c[0] == 'start'] == [('start', plan.old_image, switcher.SERVICES)]
    assert events[-1]['restored_old'] is True


def test_partial_candidate_start_uses_one_fresh_finite_gate_before_replacement(plan):
    host = FakeHost(plan)
    host.fail_start = True
    code, events = execute(plan, host)
    assert code == 2 and host.gates == 2
    assert [c for c in host.calls if c[0] in ('stop', 'release', 'start')] == [
        ('stop', 'backend'), ('stop', 'runner'), ('release', 1),
        ('start', plan.candidate_image, switcher.SERVICES),
        ('stop', 'backend'), ('release', 2), ('start', plan.old_image, switcher.SERVICES)]
    assert events[-1]['guarded'] is True and events[-1]['restored_old'] is True


def test_busy_candidate_survives_refused_recovery_gate_and_only_missing_runner_restarts(plan):
    host = FakeHost(plan)
    host.fail_start, host.busy_candidate = True, True
    code, events = execute(plan, host)
    assert code == 2 and host.gates == 2
    assert [c for c in host.calls if c[0] == 'stop'] == [('stop', 'backend'), ('stop', 'runner')]
    assert [c for c in host.calls if c[0] == 'start'][-1] == ('start', plan.old_image, ('runner',))
    assert host.states()['backend']['image'] == plan.candidate_image
    assert events[-1]['guarded'] is False and events[-1]['restored_old'] is False


@pytest.mark.parametrize('failed_stage,missing', [('stopped', ('backend',)),
    ('released', switcher.SERVICES), ('completed', switcher.SERVICES)])
def test_persistently_failed_event_sink_cannot_bypass_real_switch_recovery(plan, capfd, failed_stage, missing):
    host = FakeHost(plan)
    broken, observed = [False], []
    def failing_sink(stage, message, **fields):
        if stage == failed_stage:
            broken[0] = True
        if broken[0]:
            # Failure remains active for refused/close/recovery events. This
            # exercises the actual exception path that previously skipped the
            # restart when the evidence disk or operator stdout stayed broken.
            raise OSError('synthetic sink failure containing a secret that must not be logged')
        observed.append(stage)
    assert switcher.switch(plan, host, failing_sink) == 2
    assert all(s['running'] and s['image'] == plan.old_image for s in host.states().values())
    assert [c for c in host.calls if c[0] == 'start' and c[1] == plan.old_image] == [
        ('start', plan.old_image, missing)]
    assert host.gates == (2 if failed_stage == 'completed' else 1)
    error = capfd.readouterr().err
    assert 'R01_SWITCH_EVIDENCE_UNAVAILABLE' in error and 'secret' not in error


def test_failed_pre_stop_event_sink_returns_failure_without_any_stop_or_restart(plan, capfd):
    host = FakeHost(plan)
    def failed(*args, **kwargs):
        raise OSError('synthetic full evidence disk')
    assert switcher.switch(plan, host, failed) == 2
    assert not any(c[0] in ('gate', 'stop', 'start') for c in host.calls)
    assert 'R01_SWITCH_EVIDENCE_UNAVAILABLE' in capfd.readouterr().err


@pytest.mark.parametrize('field', ['owned_definition_hash', 'original_fingerprint',
                                 'definitions_hash', 'source_configs_hash'])
def test_changed_task_or_configuration_after_drain_refuses_before_stop(plan, field):
    host = FakeHost(plan)
    host.change = lambda n, s: s.update({field: 'f' * 64}) if n == 2 else None
    code, _ = execute(plan, host)
    assert code == 2 and not any(c[0] in ('stop', 'start') for c in host.calls)


@pytest.mark.parametrize('activity', ['accepted_runs', 'backtests', 'processors'])
def test_each_real_activity_guard_refuses_before_stop(plan, activity):
    host = FakeHost(plan)
    host.change = lambda n, s: s['activity'].update({activity: 1}) if n == 2 else None
    code, _ = execute(plan, host)
    assert code == 2 and not any(c[0] == 'stop' for c in host.calls)


@pytest.mark.parametrize('change', ['record_delete', 'record_change', 'seal_change'])
def test_fenced_record_or_seal_change_after_stop_causes_one_old_restart(plan, change):
    host = FakeHost(plan)
    def mutate(n, s):
        if n == 3:
            if change == 'record_delete':
                s['records'] = []
            elif change == 'record_change':
                s['records'][0]['record_hash'] = 'f' * 64
            else:
                s['resources']['retained_seals'] = []
    host.change = mutate
    code, events = execute(plan, host)
    assert code == 2 and events[-1]['restored_old'] is True
    assert [c for c in host.calls if c[0] == 'start'] == [('start', plan.old_image, switcher.SERVICES)]


def test_captured_queue_can_become_running_after_release_without_losing_identity(plan):
    host = FakeHost(plan)
    def accepted_after_release(n, s):
        if n == 4:
            s['activity']['accepted_runs'] = 1
            s['records'][0]['record_hash'] = 'f' * 64
    host.change = accepted_after_release
    assert execute(plan, host)[0] == 0


def compose_config():
    return {'name': 'quant-foundry-p01', 'services': {
        'frontend': {'image': 'unchanged-frontend'},
        **{s: {'image': 'old', 'environment': {'QF_SECRET': 'synthetic-secret'},
               'labels': {'application': 'unchanged'},
               'volumes': [{'type': 'volume', 'source': 'shared', 'target': '/app/data'}]}
           for s in switcher.SERVICES}}, 'volumes': {'shared': {'external': True}}}


@pytest.mark.parametrize('change', ['env', 'label', 'volume', 'frontend', 'new_service'])
def test_resolved_compose_rejects_every_change_beyond_selected_images(change):
    old, new = compose_config(), compose_config()
    new['services']['backend']['image'] = new['services']['runner']['image'] = 'candidate'
    if change == 'env':
        new['services']['runner']['environment']['QF_SECRET'] = 'changed'
    elif change == 'label':
        new['services']['backend']['labels']['application'] = 'changed'
    elif change == 'volume':
        new['services']['backend']['volumes'][0]['source'] = 'other'
    elif change == 'frontend':
        new['services']['frontend']['image'] = 'changed'
    else:
        new['services']['other'] = {}
    with pytest.raises(switcher.Refused):
        switcher.image_only_configs(old, new)
    assert switcher.image_only_configs(old, old) == switcher.digest({
        **old, 'services': {s: {k: v for k, v in cfg.items() if k != 'image' or s == 'frontend'}
                           for s, cfg in old['services'].items()}})


def test_host_commands_explicitly_use_SIGTERM_and_scoped_no_pull_no_build_up(plan):
    host = switcher.DockerHost(plan)
    calls = []
    old = compose_config()
    host.compose_hashes = {False: switcher.digest(old), True: switcher.digest(old)}
    host.run = lambda args, seconds=10: calls.append(args) or ''
    host.config = lambda candidate: old
    host.states = lambda deadline=None: {s: {'stopped': True} for s in switcher.SERVICES}
    host.containers = lambda deadline=None: {s: {'Id': s + '-id', 'State': {'Running': True},
                                                  'Image': plan.old_image} for s in switcher.SERVICES}
    host.stop('backend')
    host.start(plan.candidate_image, ('backend', 'runner'))
    assert calls[0][-4:] == ['kill', '--signal', 'SIGTERM', 'backend-id']
    assert 'stop' not in calls[0] and 'SIGKILL' not in str(calls)
    up = calls[-1]
    assert up[-2:] == ['backend', 'runner'] and 'frontend' not in up
    for flag in ('--no-deps', '--no-build', '--wait'):
        assert flag in up
    assert up[up.index('--pull') + 1] == 'never'
    assert up[up.index('--timeout') + 1] == '-1'


@pytest.mark.parametrize('kwargs', [{'hold_seconds': 100}, {'drain_seconds': 601},
    {'stop_seconds': 46}, {'project': 'other'}, {'candidate_head': 'main'},
    {'original_fingerprint': '0' * 64}, {'old_image': 'mutable:tag'}])
def test_plan_finite_identity_guards(plan, kwargs):
    with pytest.raises(switcher.Refused):
        replace(plan, **kwargs).validate()


def test_runtime_spec_retains_app_labels_environment_and_mounts_but_normalizes_generated_identity():
    def container(identity):
        return {'Id': identity, 'Config': {'Hostname': identity[:12], 'Image': 'immutable',
                'Env': ['secret=synthetic', 'other=kept'], 'Labels': {'app': 'kept',
                'com.docker.compose.config-hash': identity, 'com.docker.compose.image': identity,
                'com.docker.compose.project.config_files': identity}},
                'HostConfig': {'Init': True, 'RestartPolicy': {'Name': 'unless-stopped'}},
                'Mounts': [{'Type': 'volume', 'Name': 'shared', 'Source': '/actual/shared',
                            'Destination': '/app/data', 'RW': True}]}
    old, new = container('1' * 64), container('2' * 64)
    assert switcher.service_spec(old) == switcher.service_spec(new)
    for field in ('Env', 'Labels', 'Mounts', 'Hostname'):
        altered = copy.deepcopy(new)
        if field == 'Env':
            altered['Config']['Env'][0] = 'secret=changed'
        elif field == 'Labels':
            altered['Config']['Labels']['app'] = 'changed'
        elif field == 'Mounts':
            altered['Mounts'][0]['Source'] = '/other'
        else:
            altered['Config']['Hostname'] = 'explicit-changed-hostname'
        assert switcher.service_spec(old) != switcher.service_spec(altered)


def scheduler_tables(engine):
    from app.scheduling.models import ScheduledTask, TaskRun
    from app.data_sources.models import DataSourceConfig
    ScheduledTask.__table__.create(engine)
    TaskRun.__table__.create(engine)
    DataSourceConfig.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE backtest_runs (id uuid PRIMARY KEY, finished_at timestamptz)'))
    with Session(engine) as session:
        session.add(DataSourceConfig(key='tonghuashun', initialized=True, enabled=True,
            values={'preserved': 'synthetic'}, encrypted_secrets='synthetic-secret', version=7))
        tasks = [ScheduledTask(name='isolated-' + str(i), task_type='isolated.task', parameters={},
            schedule={'type': 'cron', 'expression': '0 * * * *'}, state='active') for i in range(177)]
        tasks.append(ScheduledTask(id=switcher.OWNED_TASK, name='owned', task_type='data_store.update_local',
            parameters={'datasets': []}, schedule={'type': 'cron', 'expression': '0 * * * *'}, state='active'))
        session.add_all(tasks)
        session.flush()
        queued = TaskRun(task_id=tasks[0].id, task_version=1, task_type='isolated.task',
            trigger_type='manual', parameters={}, parameter_version=1, status='queued')
        session.add(queued)
        session.commit()
        return str(queued.id)


@PG
def test_real_pg_probe_preserves_all_178_full_definitions_credentials_and_queued_records(database):
    from app.scheduling.process_drain import task_fingerprint
    engine, _ = database
    queued = scheduler_tables(engine)
    first = switcher.database_snapshot(engine, switcher.OWNED_TASK, [])
    second = switcher.database_snapshot(engine, switcher.OWNED_TASK, [queued])
    assert first == second and first['task_count'] == 178
    assert first['activity'] == {'accepted_runs': 0, 'backtests': 0, 'processors': 0}
    with Session(engine) as session:
        assert task_fingerprint(session) == (first['task_fingerprint'], 178)
        assert session.execute(text('SELECT encrypted_secrets FROM data_source_configs')).scalar_one() == 'synthetic-secret'
        assert session.execute(text('SELECT status FROM task_runs')).scalar_one() == 'queued'
        session.execute(text("SET LOCAL default_transaction_read_only='on'"))
    assert 'synthetic-secret' not in json.dumps(first)
    with engine.begin() as connection:
        connection.execute(text('UPDATE scheduled_tasks SET parameters=\'{"datasets":["changed"]}\'::jsonb WHERE id=:id'),
                           {'id': switcher.OWNED_TASK})
    changed = switcher.database_snapshot(engine, switcher.OWNED_TASK, [queued])
    assert changed['task_fingerprint'] == first['task_fingerprint']
    assert changed['owned_definition_hash'] != first['owned_definition_hash']
    assert changed['definitions_hash'] != first['definitions_hash']


def gate_child(database, plan, hold):
    engine, schema = database
    scheduler_tables(engine)
    from app.scheduling.process_drain import task_fingerprint
    with Session(engine) as session:
        fingerprint, _ = task_fingerprint(session)
    actual_plan = replace(plan, task_fingerprint=fingerprint, release_seconds=1)
    # The actual official module and PG source-row gate execute in a separate
    # process. Only engine/schema and a synthetic source registry are injected;
    # there is no substitute gate algorithm, supplier client or production DSN.
    code = (
        'from tests.test_data_store_kernel import make_engine;'
        'import app.db.session as db;'
        f'db.get_engine=lambda:make_engine({schema!r});'
        'import app.scheduling.registry as reg;'
        'from tests.test_scheduling import TestTaskParameters,noop_task_handler;'
        'r=reg.TaskRegistry();'
        'r.register(reg.TaskDefinition(key="isolated.task",name="隔离",english_name="Isolated",'
        'source_key="tonghuashun",parameters_model=TestTaskParameters,handler=noop_task_handler));'
        'reg.task_registry=r;'
        'from app.scheduling.process_drain import main;'
        f'raise SystemExit(main(["--expect-task-fingerprint",{fingerprint!r},'
        f'"--drain-seconds","2","--hold-seconds",{str(hold)!r}]))')
    child = switcher.Child([sys.executable, '-c', code], interactive=True)
    return switcher.Gate(child, actual_plan)


@PG
def test_real_official_pg_gate_exposes_same_deadline_releases_and_exits_zero(database, plan):
    gate = gate_child(database, plan, 20)
    try:
        ready = gate.drained()
        assert ready['task_count'] == 178 and ready['accepted_runs'] == ready['backtests'] == 0
        assert ready['monotonic_namespace'] == os.readlink('/proc/self/ns/time')
        actual_deadline = ready['release_deadline_monotonic']
        assert 0 < actual_deadline - time.monotonic() <= 20
        gate.check(2)
        assert gate.deadline == actual_deadline
        gate.release()
        assert gate.child.process.poll() == 0
    finally:
        gate.close()
    assert switcher.database_snapshot(database[0], switcher.OWNED_TASK, [])['activity']['accepted_runs'] == 0


@PG
def test_real_official_pg_gate_expiry_refuses_and_releases_row_locks(database, plan):
    gate = gate_child(database, plan, 1)
    try:
        gate.drained()
        time.sleep(1.1)
        with pytest.raises(switcher.Refused, match='GATE_REFUSED|GATE_EXITED|RELEASE_WINDOW'):
            gate.check(0)
    finally:
        assert gate.close() == (True, 2)
    with database[0].begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout='200ms'"))
        assert connection.execute(text('SELECT key FROM data_source_configs FOR UPDATE')).scalar_one() == 'tonghuashun'


@PG
def test_real_official_pg_gate_EOF_close_uses_five_seconds_not_a_fresh_drain_window(database, plan):
    gate = gate_child(database, plan, 20)
    ready = gate.drained()
    original_deadline = ready['release_deadline_monotonic']
    began = time.monotonic()
    assert gate.close() == (True, 2)
    assert gate.deadline == original_deadline
    assert gate.close_deadline <= began + 5.1
    assert time.monotonic() - began < 5
    with database[0].begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout='200ms'"))
        connection.execute(text('SELECT key FROM data_source_configs FOR UPDATE')).all()


@PG
def test_real_official_pg_gate_release_SQL_crossing_original_deadline_is_refused(database, monkeypatch):
    from types import SimpleNamespace
    import app.db.session as db
    import app.scheduling.process_drain as module
    import app.scheduling.registry as reg
    from tests.test_scheduling import TestTaskParameters, noop_task_handler
    engine, _ = database
    scheduler_tables(engine)
    with Session(engine) as session:
        fingerprint, _ = module.task_fingerprint(session)
    registry = reg.TaskRegistry()
    registry.register(reg.TaskDefinition(key='isolated.task', name='隔离', english_name='Isolated',
        source_key='tonghuashun', parameters_model=TestTaskParameters, handler=noop_task_handler))
    monkeypatch.setattr(db, 'get_engine', lambda: engine)
    monkeypatch.setattr(reg, 'task_registry', registry)
    clock, calls, events = [0.0], [0], []
    original_fingerprint = module.task_fingerprint
    def query(session):
        result = original_fingerprint(session)
        calls[0] += 1
        if calls[0] == 3:
            # Only the final release query crosses the deadline. The same real
            # PostgreSQL locks/queries/rollback execute; a delayed query cannot
            # borrow a fresh hold window or produce a successful release event.
            clock[0] = 21.0
        return result
    monkeypatch.setattr(module, 'task_fingerprint', query)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: None))
    monkeypatch.setattr(module, 'emit', lambda stage, message, **fields: events.append({'stage': stage, **fields}))
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd) as reader, os.fdopen(write_fd, 'w') as writer:
        writer.write('release ' + fingerprint + '\n')
        writer.flush()
        monkeypatch.setattr(module, 'sys', SimpleNamespace(stdin=reader))
        assert module.main(['--expect-task-fingerprint', fingerprint,
                            '--drain-seconds', '2', '--hold-seconds', '20']) == 2
    assert events[-1] == {'stage': 'refused', 'reason': 'RELEASE_WINDOW_EXPIRED'}
    assert not any(e['stage'] == 'released' for e in events)
    assert next(e for e in events if e['stage'] == 'drained')['release_deadline_monotonic'] == 20.0
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout='200ms'"))
        connection.execute(text('SELECT key FROM data_source_configs FOR UPDATE')).all()


@PG
def test_real_ext4_sealed_merge_spool_is_preserved_and_active_scratch_is_refused(store):
    from app.data_store.adapters.registry import ENTRIES
    from app.data_store.merge import MergeSpool
    entry = next(e for e in ENTRIES if e.id == 'E23')
    with store.budget.reserve('read', pending='pipeline.E23') as space:
        spool = MergeSpool(space, entry.spec)
        try:
            progress = {'identity': 'a' * 64, 'source_selection': 'b' * 64,
                        'scan_complete': True, 'after': 'p0001', 'cycle': uuid4().hex}
            spool.db.execute('INSERT INTO progress VALUES (1,?)', (json.dumps(progress),))
            spool.db.commit()
        finally:
            spool.close()
        with pytest.raises(switcher.Refused, match='HELD_STORAGE_LOCK'):
            switcher.storage_snapshot(store.files.root, store.limits.scratch_bytes)
    all_files = sorted(p for p in store.files.root.rglob('*') if p.is_file())
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in all_files}
    actual = switcher.storage_snapshot(store.files.root, store.limits.scratch_bytes)
    assert actual['held_locks'] == 0 and len(actual['retained_seals']) == 1
    assert actual['retained_seals'][0]['owner'] == 'pipeline.E23'
    assert actual['retained_seals'][0]['progress_hash'] == switcher.digest(progress)
    assert {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in all_files} == before
    assert sorted(p for p in store.files.root.rglob('*') if p.is_file()) == all_files
    # A different admitted writer/reader must also prevent a deployment probe;
    # a retained, unlocked seal above is deliberately not treated as a holder.
    with store.locks._hold(entry.spec.name, 'pipeline', 2, time.monotonic_ns(), None):
        with pytest.raises(switcher.Refused, match='HELD_STORAGE_LOCK'):
            switcher.storage_snapshot(store.files.root, store.limits.scratch_bytes)


@PG
@pytest.mark.parametrize('failure', ['unsealed', 'journal', 'quota', 'symlink'])
def test_real_ext4_unknown_or_inconsistent_scratch_fails_closed_without_cleanup(store, failure):
    slot = store.files.root / '.scratch/000'
    quota = slot / 'quota'
    quota.write_text(json.dumps({'kind': 'read', 'bytes': 4096, 'pending': 'pipeline.E23'}))
    spool = slot / 'spill/lfd02-merge.sqlite'
    if failure == 'symlink':
        spool.symlink_to(quota)
    else:
        import sqlite3
        with sqlite3.connect(spool) as db:
            db.execute('CREATE TABLE progress(id integer primary key,body text)')
            db.execute('INSERT INTO progress VALUES (1,?)', (json.dumps({
                'scan_complete': failure != 'unsealed', 'identity': 'a' * 64,
                'source_selection': 'b' * 64}),))
        quota.write_text(json.dumps({'kind': 'read', 'bytes': spool.stat().st_size,
                                   'pending': 'pipeline.E23'}))
        if failure == 'journal':
            spool.with_name(spool.name + '-journal').write_bytes(b'preserve')
        if failure == 'quota':
            quota.write_text(json.dumps({'kind': 'read', 'bytes': 1, 'pending': 'pipeline.E23'}))
    before = quota.read_bytes()
    with pytest.raises((switcher.Refused, OSError)):
        switcher.storage_snapshot(store.files.root, store.limits.scratch_bytes)
    assert quota.read_bytes() == before and spool.exists()
