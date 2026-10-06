"""Synthetic window failures and real Linux process-loss recovery.

No deployment host, provider, real task, environment file or shared volume is
accessed. PostgreSQL coverage uses only the established random-schema fixture.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
import io
from contextlib import redirect_stdout
from uuid import UUID

import pytest

from tests.test_tonghuashun_default_repair import native_engine

PATH = Path(__file__).resolve().parents[2] / 'scripts/r01_source_window.py'
SPEC = importlib.util.spec_from_file_location('r01_source_window', PATH)
w = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = w
SPEC.loader.exec_module(w)


class Clock:
    def __init__(self):
        self.value = 100.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def synthetic_snapshot():
    tasks = []
    for task_id, (dataset, mode, token) in w.TARGETS.items():
        params = {'refresh_today': False, 'batch_size': 20 if dataset == 'stock_daily' else 5,
            'max_requests': 40, 'max_seconds': 180, 'subjects': None,
            'asset_types': ['a-share'] if dataset == 'stock_daily' else
                ['fund-etf', 'fund-lof', 'fund-reits', 'fund-otc'],
            'mode': mode, 'start_date': None, 'end_date': None}
        assert w.digest(params) == token
        tasks.append({'id': task_id, 'name': 'Synthetic ' + dataset + ' ' + mode,
            'description': None, 'task_type': 'data.ths.' + dataset, 'parameters': params,
            'parameter_version': 1, 'schedule': {'type': 'cron', 'timezone': 'Asia/Shanghai',
                'expression': '*/10 * * * *'}, 'state': 'active', 'concurrency_limit': 1,
            'overlap_policy': 'skip', 'queue_limit': 1, 'priority': 0, 'version': 4})
    for n in range(168):
        tasks.append({'id': str(UUID(int=n+1)), 'name': 'Synthetic protected task',
            'description': None, 'task_type': 'test.noop', 'parameters': {},
            'parameter_version': 1, 'schedule': {'type': 'cron', 'timezone': 'Asia/Shanghai',
                'expression': '*/10 * * * *'}, 'state': 'active' if n >= 70 else 'paused',
            'concurrency_limit': 1, 'overlap_policy': 'skip', 'queue_limit': 1,
            'priority': 0, 'version': 4})
    updater = copy.deepcopy(tasks[-1])
    updater.update(id=w.UPDATER, name='Synthetic updater', version=24)
    tasks.append(updater)
    tasks.sort(key=lambda t: t['id'])
    source = next(t for t in tasks if t['id'] in w.TARGETS)
    run = {'id': str(UUID(int=10000)), 'task_id': source['id'], 'task_version': 4,
        'task_type': source['task_type'], 'status': 'queued', 'trigger_type': 'scheduled',
        'parameters': copy.deepcopy(source['parameters']), 'parameter_version': 1,
        'priority': 0, 'created_at': '2026-01-01 00:00:00+00',
        'available_at': '2026-01-01 00:00:00+00'}
    return {'tasks': tasks, 'runs': [run], 'saved': [run],
            'source_configs_sha256': w.CONFIG_SHA, 'attempt_exists': False,
            'pins_valid': True, 'source_scopes': [{'synthetic_scope': i} for i in range(3)]}


@pytest.fixture
def sample(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    snapshot = synthetic_snapshot()
    monkeypatch.setattr(w, 'ORIGINAL_SHA', w.digest([t for t in snapshot['tasks'] if t['id'] != w.UPDATER]))
    manifest = {'tool_head': '1' * 40, 'tool_sha256': w.hashlib.sha256(PATH.read_bytes()).hexdigest(),
        'project_directory': w.PROJECT_DIRECTORY, 'compose_files': [w.PROJECT_DIRECTORY + '/compose.p01.yaml'],
        'backend_id': '2' * 64, 'runner_id': '3' * 64}
    runtime = {'synthetic_runtime': True}
    plan = {'protocol': 'r01-nine-source-window@1', 'manifest': manifest, 'created_at': w.utc(),
        'runtime': runtime, 'snapshot': copy.deepcopy(snapshot), 'source_plan_sha256': w.SOURCE_SHA,
        'limits': [w.TOTAL, w.DRAIN, w.LAUNCH, w.BATCH, w.RECOVERY], 'target_ids': sorted(w.TARGETS)}
    return plan, tmp_path


class FakeClient:
    def __init__(self, plan, clock):
        self.plan, self.clock = plan, clock
        self.live = copy.deepcopy(plan['snapshot'])
        self.calls, self.source_calls = [], 0
        self.fail_pause = self.fail_resume = self.external_edit = None
        self.keep_queue = self.change_pins = False
        self.after_pause_reads = 0

    def preflight(self, **kwargs):
        return copy.deepcopy(self.plan['runtime'])

    def verify_runner(self, **kwargs): return True

    def snapshot(self, *, pins=False, ready=False, accepted=(), seconds=10):
        if all(t['state'] == 'paused' for t in self.live['tasks'] if t['id'] in w.TARGETS):
            self.after_pause_reads += 1
            if self.external_edit and self.after_pause_reads == 1:
                t = next(t for t in self.live['tasks'] if t['id'] == self.external_edit)
                t['version'] += 1
                t['parameters']['max_requests'] += 1
            if self.after_pause_reads >= 3 and not self.keep_queue:
                self.live['runs'] = []
        if pins and self.change_pins and self.after_pause_reads:
            raise w.Refused('NATIVE_PINS_CHANGED')
        result = copy.deepcopy(self.live)
        if ready: result['all_three_admitted'] = True
        result['saved'] = [r for r in self.plan['snapshot']['saved'] if r['id'] in accepted]
        return result

    def call(self, action, **kwargs):
        assert action == 'snapshot'
        kwargs.pop('recovery', None)
        return self.snapshot(**kwargs)

    def task(self, task_id, **kwargs):
        return copy.deepcopy(next(t for t in self.live['tasks'] if t['id'] == task_id))

    def change(self, task_id, version, target, **kwargs):
        self.calls.append((task_id, version, target))
        task = next(t for t in self.live['tasks'] if t['id'] == task_id)
        assert task['version'] == version
        task.update(state=target, version=version + 1)
        if target == 'paused' and task_id == self.fail_pause:
            raise w.Refused('API_ACK_LOST', uncertain=True)
        if target == 'active' and task_id == self.fail_resume:
            raise w.Refused('API_ACK_LOST', uncertain=True)
        return {'id': task_id, 'state': target, 'version': version+1,
                'definition_sha256': w.digest(w.definition(task))}

    def source(self, *args, **kwargs):
        self.source_calls += 1
        self.live['attempt_exists'] = True
        return 'a' * 64

    def source_state(self, *args, **kwargs):
        return {'id': 'a' * 64, 'running': False, 'status': 'exited', 'exit_code': 0}

    def stop_source(self, value, **kwargs):
        return self.source_state() if value['source_launch_intent'] else None

    def receipt(self, **kwargs):
        workers = [{'operation_id': str(UUID(int=20000+i)), 'outcome': 'succeeded',
            'counts_complete': True, 'unknown_publication': False,
            'publication': {'status': 'verified'}, 'counts': {'http_attempts': n}}
            for i,n in enumerate((1,2,2))]
        return {'batch': {'outcome': 'completed', 'scopes': self.plan['snapshot']['source_scopes'],
            'counts_complete': True, 'process_wall_budget_met': True,
            'budget': {'retries': 0, 'wall_seconds': 840, 'scope_seconds': 180, 'http_attempts': 5},
            'attempts_started': 3, 'results': [{**x, 'process_exited': True} for x in workers],
            'counts': {'http_attempts': 5}}, 'tagged_activity': [], 'workers': workers}


def unit_window(sample, monkeypatch):
    plan, root = sample
    clock = Clock()
    identity = {'pid': 101, 'start_ticks': '1234', 'boot_id': 'synthetic-test-boot'}
    monkeypatch.setattr(w, 'process_identity', lambda pid=None: identity)
    monkeypatch.setattr(w.time, 'monotonic', clock.now)
    journal = w.Journal(root / 'state.json')
    value = w.new_state(plan, identity)
    value.update(watchdog=identity, watchdog_ready=True)
    journal.create(value)
    client = FakeClient(plan, clock)
    return journal, client, clock


def execute(journal, client, clock):
    return w.run_window(journal, client, clock=clock.now, sleep=clock.sleep,
                        liveness=lambda _: True)


def test_one_full_batch_restores_exact_nine_and_keeps_frozen_queue(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    before = copy.deepcopy(client.live['tasks'])
    result = execute(journal, client, clock)
    assert result['phase'] == 'restored' and result['source_outcome'] == 'completed'
    assert client.source_calls == 1 and len(client.calls) == 18
    assert {i for i, _, _ in client.calls} == set(w.TARGETS)
    assert result['accepted'] == {r['id']: w.frozen(r) for r in sample[0]['snapshot']['saved']}
    for old, current in zip(before, client.live['tasks']):
        if old['id'] in w.TARGETS:
            assert current['state'] == 'active' and current['version'] == 6
            assert w.definition(current) == w.definition(old)
        else:
            assert current == old
    with pytest.raises(FileExistsError):
        journal.create(w.new_state(sample[0], result['worker']))


@pytest.mark.parametrize('source_sha', [
    'd630c0f94d15a16fa2fdfe006a9970bb6b2da1539faf703da3295a1f223d7549',
    'f' * 64,
], ids=['retained-r20-plan', 'unreviewed-replacement'])
def test_previous_or_unreviewed_source_plan_is_refused_before_window_creation(
        sample, monkeypatch, capsys, source_sha):
    plan, root = sample
    stale = copy.deepcopy(plan)
    stale['source_plan_sha256'] = source_sha
    path, state = root / 'retained-plan.json', root / 'window.json'
    w.write_private(path, stale, exclusive=True)
    preserved = path.read_bytes()
    monkeypatch.setattr(w, 'process_identity', lambda *args: {
        'pid': 101, 'start_ticks': '1234', 'boot_id': 'synthetic-test-boot'})

    def forbidden(*args, **kwargs):
        pytest.fail('A rejected source pin must not create Docker work or a watchdog')

    monkeypatch.setattr(w, 'DockerClient', forbidden)
    monkeypatch.setattr(w, 'arm_watchdog', forbidden)
    code = w.main(['run', '--plan', str(path), '--state', str(state),
                   '--expect-plan-sha256', w.digest(stale)])
    response = json.loads(capsys.readouterr().out)
    assert code == 2 and response['reason'] == 'PLAN_SCOPE_CHANGED'
    assert not state.exists() and not list(root.glob('window.json*'))
    assert path.read_bytes() == preserved


@pytest.mark.parametrize('live_revision,new_observation', [
    (20, False), (21, False), (22, False), (22, True),
], ids=['past-r20', 'fixed-r21', 'future-r22-same-body', 'future-r22-new-body'])
def test_fixed_r21_worker_never_follows_another_default_revision(
        native_engine, tmp_path, monkeypatch, live_revision, new_observation):
    from dataclasses import replace
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.data_ingestion.models.tonghuashun import TonghuashunCollectionState, TonghuashunObservation
    from app.data_ingestion.tonghuashun import bounded
    from tests import test_tonghuashun_default_repair as repair

    # All identities and HTTP are invented. A revision counter can advance
    # without replacing its last-good observation; that case must still reject
    # a different fixed revision even when the body and observation ID match.
    body = {'item': [repair.bar(repair.DAY)], 'adjust': 'none'}
    first = repair.selection(native_engine, 'stock_daily', body, day=repair.DAY)
    scope = bounded.RepairScope(replace(first.selection, revision=21))
    with Session(native_engine) as session:
        state = session.get(TonghuashunCollectionState, ('stock_daily', repair.STOCK, 'default'))
        state.revision = 21 if new_observation else live_revision
        session.commit()
    if new_observation:
        repair.publish(native_engine, 'stock_daily',
            {'item': [repair.bar(repair.DAY, '3')], 'adjust': 'none'}, expected=21)
    selection_before = copy.deepcopy(scope.as_dict())
    with Session(native_engine) as session:
        state = session.get(TonghuashunCollectionState, ('stock_daily', repair.STOCK, 'default'))
        before = (state.revision, state.observation_id,
                  tuple(session.scalars(select(TonghuashunObservation.id).order_by(TonghuashunObservation.id))))
    code, receipt, get = repair.worker(native_engine, scope, tmp_path, monkeypatch,
        [repair.http_response({'item': [repair.bar(repair.DAY, '2')], 'adjust': 'none'})])
    assert scope.as_dict() == selection_before
    if live_revision == 21:
        assert code == 0 and receipt['outcome'] == 'succeeded'
        assert get.call_count == receipt['counts']['http_attempts'] == 1
        assert receipt['publication']['status'] == 'verified'
        with Session(native_engine) as session:
            state = session.get(TonghuashunCollectionState, ('stock_daily', repair.STOCK, 'default'))
            assert state.revision == 22 and state.observation_id != before[1]
    else:
        assert code == 2 and receipt['outcome'] == 'blocked' and receipt['problem'] == 'baseline_changed'
        assert get.call_count == receipt['counts']['http_attempts'] == 0
        assert receipt['publication']['status'] == 'not_attempted'
        assert receipt['counts_complete'] is True and receipt['unknown_publication'] is False
        with Session(native_engine) as session:
            state = session.get(TonghuashunCollectionState, ('stock_daily', repair.STOCK, 'default'))
            after = (state.revision, state.observation_id,
                     tuple(session.scalars(select(TonghuashunObservation.id).order_by(TonghuashunObservation.id))))
        assert after == before


@pytest.mark.parametrize('change', ['version', 'mode', 'schedule', 'missing', 'protected', 'config', 'extra'])
def test_scope_definition_config_and_protected_set_changes_refuse_before_pause(sample, change):
    original = sample[0]['snapshot']
    live = copy.deepcopy(original)
    target = next(t for t in live['tasks'] if t['id'] in w.TARGETS)
    if change == 'version': target['version'] += 1
    elif change == 'mode': target['parameters']['mode'] = 'backfill'
    elif change == 'schedule': target['schedule']['expression'] = '* * * * *'
    elif change == 'missing': live['tasks'].remove(target)
    elif change == 'protected': next(t for t in live['tasks'] if t['id'] not in w.TARGETS)['version'] += 1
    elif change == 'config': live['source_configs_sha256'] = '0' * 64
    else: live['tasks'].append(copy.deepcopy(target))
    with pytest.raises(w.Refused): w.validate_snapshot(live)


def test_pending_queue_expires_at_40minutes_without_source_or_cancellation(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    client.keep_queue = True
    original_run = copy.deepcopy(client.live['runs'])
    result = execute(journal, client, clock)
    assert client.source_calls == 0 and result['source_launch_intent'] is False
    assert clock.value == result['drain_deadline']
    assert result['phase'] == 'restored' and len(result['restore']) == 9
    assert client.live['runs'] == original_run


def test_native_pin_change_after_natural_drain_restores_without_source(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    client.change_pins = True
    result = execute(journal, client, clock)
    assert client.source_calls == 0 and result['phase'] == 'restored'
    assert any(e['reason'] == 'NATIVE_PINS_CHANGED' for e in result['errors'])


def test_final_recheck_has_only_one_minute_after_early_natural_drain(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    calls = []
    def preflight(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            assert kwargs['seconds'] == w.LAUNCH
            clock.sleep(w.LAUNCH + 1)
        return copy.deepcopy(client.plan['runtime'])
    client.preflight = preflight
    result = execute(journal, client, clock)
    assert result['phase'] == 'restored' and client.source_calls == 0
    assert result['source_start_deadline'] == result['launch_started_monotonic'] + 60
    assert result['source_start_deadline'] < result['launch_deadline']
    assert any(e['reason'] == 'WINDOW_PHASE_EXPIRED' for e in result['errors'])


def test_lost_pause_ack_is_not_guessed_or_replayed(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    task_id = sorted(w.TARGETS)[3]
    client.fail_pause = task_id
    result = execute(journal, client, clock)
    assert result['phase'] == 'recovery_required' and client.source_calls == 0
    assert task_id not in result['confirmed']
    assert sum(i == task_id for i, _, _ in client.calls) == 1
    assert result['unresolved'][0]['action'] == 'pause'
    assert len(result['restore']) == 3


def test_lost_resume_ack_stays_uncertain_even_when_actual_row_is_active(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    task_id = sorted(w.TARGETS)[3]
    client.fail_resume = task_id
    result = execute(journal, client, clock)
    assert result['phase'] == 'recovery_required'
    assert client.task(task_id)['state'] == 'active' and client.task(task_id)['version'] == 6
    assert result['restore'][task_id]['outcome'] == 'API_ACK_LOST'
    before = list(client.calls)
    w.restoring(journal, client, clock=clock.now)
    assert client.calls == before


def test_external_task_edit_is_preserved_and_other_owned_pauses_restore(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    task_id = sorted(w.TARGETS)[3]
    client.external_edit = task_id
    result = execute(journal, client, clock)
    assert client.source_calls == 0 and result['phase'] == 'recovery_required'
    assert result['restore'][task_id]['outcome'] == 'CONCURRENT_CHANGE_PRESERVED'
    assert sum(r['outcome'] == 'restored' for r in result['restore'].values()) == 8
    assert (task_id, 5, 'active') not in client.calls


def test_dead_watchdog_and_deadline_tampering_refuse_before_pause(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    result = w.run_window(journal, client, clock=clock.now, sleep=clock.sleep,
                          liveness=lambda _: False)
    assert client.calls == [] and client.source_calls == 0
    assert result['phase'] == 'restored'
    value = journal.read()
    value['deadline'] += 1
    with pytest.raises(w.Refused, match='WINDOW_DEADLINE_CHANGED'):
        w.validate_state(value)


def test_partial_receipt_recovery_uses_one_exclusive_restorer(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    with journal.lock('.restore.lock', blocking=False):
        with pytest.raises(w.Refused, match='RESTORER_BUSY'):
            w.restoring(journal, client, clock=clock.now)
    assert client.calls == []


def test_unsupported_pidfd_refuses_before_arming_or_pausing(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    monkeypatch.setattr(w.os, 'pidfd_open', None, raising=False)
    with pytest.raises(w.Refused, match='PIDFD_REQUIRED_BEFORE_PAUSE'):
        w.arm_watchdog(journal)
    assert client.calls == [] and client.source_calls == 0
    assert not journal.path.with_name(journal.path.name + '.watchdog.jsonl').exists()


def test_source_fence_inspect_stop_and_exit_share_one_ten_second_budget(sample, monkeypatch):
    from types import SimpleNamespace
    clock = Clock()
    monkeypatch.setattr(w, 'time', SimpleNamespace(monotonic=clock.now))
    client = w.DockerClient(sample[0]['manifest'])
    budgets = []
    def inspect(value, *, seconds, fencing):
        assert fencing is True
        budgets.append(seconds)
        clock.sleep(2)
        return {'id': 'a' * 64, 'status': 'running' if len(budgets) == 1 else 'exited',
                'running': len(budgets) == 1, 'exit_code': 0}
    def command(argv, *, seconds):
        budgets.append(seconds)
        assert argv == ['docker', 'stop', '--time', '5', 'a' * 64]
        clock.sleep(5)
        return 'a' * 64
    monkeypatch.setattr(client, 'source_state', inspect)
    monkeypatch.setattr(client, 'command', command)
    assert client.stop_source({})['status'] == 'exited'
    assert budgets == [10, 8, 3] and clock.now() == 109


def test_source_creation_overrides_only_oneoff_restart_and_never_replays(sample, monkeypatch):
    plan, root = sample
    client = w.DockerClient(plan['manifest'])
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        return 'a' * 64
    monkeypatch.setattr(client, 'command', command)
    episode = 'e' * 32
    assert client.source(episode, w.digest(plan), control_directory=root) == 'a' * 64
    path = root / ('source-' + episode + '.compose.json')
    assert w.read_private(path) == w.SOURCE_OVERRIDE
    assert set(w.SOURCE_OVERRIDE['services']) == {'runner'}
    assert w.SOURCE_OVERRIDE['services']['runner'] == {
        'restart': 'no', 'pull_policy': 'never',
        'deploy': {'restart_policy': {'condition': 'none'}}}
    assert calls[0][:len(client.compose_prefix())+2] == client.compose_prefix() + ['-f', str(path)]
    assert '--rm' not in calls[0]  # Keep the positive exit-code receipt available.
    with pytest.raises(FileExistsError):
        client.source(episode, w.digest(plan), control_directory=root)
    assert len(calls) == 1


@pytest.mark.parametrize('user', ['999', 'app'])
@pytest.mark.parametrize('policy,restarts', [('always', 0), ('no', 1), ('no', 0)])
def test_actual_source_restart_policy_is_verified_but_owned_fencing_remains_possible(sample, monkeypatch, policy, restarts, user):
    client = w.DockerClient(sample[0]['manifest'])
    episode, token = 'e' * 32, 'b' * 64
    item = {'Id': 'a' * 64, 'Image': w.APP_IMAGE, 'RestartCount': restarts,
        'Config': {'Labels': {'com.docker.compose.project': w.PROJECT,
            'qf.r01.window': episode, 'qf.r01.plan': token}, 'Env': [], 'User': user},
        'HostConfig': {'NetworkMode': 'synthetic-net', 'RestartPolicy': {'Name': policy}},
        'Mounts': [], 'State': {'Running': True, 'ExitCode': 0, 'Status': 'running'}}
    state = {'episode': episode, 'plan_sha256': token, 'source_launch_intent': True,
        'source_id': 'a' * 64, 'plan': {'runtime': {'configured_user': user, 'network_mode': 'synthetic-net',
            'env_sha256': w.digest({}), 'mounts_sha256': w.digest([])}}}
    monkeypatch.setattr(client, 'inspect', lambda *a, **kw: copy.deepcopy(item))
    if policy != 'no' or restarts:
        with pytest.raises(w.Refused, match='SOURCE_RESTART_POLICY_UNVERIFIED'):
            client.source_state(state)
    else:
        assert client.source_state(state)['running']
    assert client.source_state(state, fencing=True)['id'] == 'a' * 64


@pytest.mark.parametrize('user', ['app', 'app:app', '999', '999:999'])
def test_named_application_user_requires_actual_numeric_identity(user):
    w.verify_application_user(user, user, dict(uid=999, euid=999, gid=999, egid=999))


@pytest.mark.parametrize('uid', [999, 0])
def test_preflight_resolves_image_user_app_and_checks_the_live_process(sample, monkeypatch, uid):
    client = w.DockerClient(sample[0]['manifest'])
    image = {'Id': w.APP_IMAGE, 'Config': {'User': 'app', 'Env': ['QF_ENVIRONMENT=test'],
        'Labels': {'org.opencontainers.image.revision': w.APP_HEAD}}}
    runner = {'Id': sample[0]['manifest']['runner_id'], 'Image': w.APP_IMAGE,
        'State': {'Running': True}, 'Config': {'User': 'app', 'Env': ['QF_ENVIRONMENT=test'],
            'Labels': {'com.docker.compose.project': w.PROJECT,
                       'com.docker.compose.service': 'runner'}},
        'HostConfig': {'NetworkMode': 'synthetic-net'}, 'Mounts': []}
    config = {'services': {'runner': {'image': w.APP_IMAGE, 'volumes': []}}}
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ['docker', 'ps']: return ''
        if argv[:3] == ['docker', 'image', 'inspect']: return json.dumps([image])
        if argv[:2] == ['docker', 'compose']: return json.dumps(config)
        assert argv[:3] == ['docker', 'exec', runner['Id']]
        assert 'os.getuid()' in argv[-1] and 'os.getegid()' in argv[-1]
        return json.dumps(dict(uid=uid, euid=uid, gid=999, egid=999))
    monkeypatch.setattr(client, 'backend', lambda **kw: sample[0]['manifest']['backend_id'])
    monkeypatch.setattr(client, 'inspect', lambda *a, **kw: runner)
    monkeypatch.setattr(client, 'command', command)
    if uid == 0:
        with pytest.raises(w.Refused, match='APP_UID_CHANGED'): client.preflight()
    else:
        runtime = client.preflight()
        assert runtime['configured_user'] == 'app' and runtime['application_identity']['uid'] == 999
    assert len([a for a in calls if a[:2] == ['docker', 'exec']]) == 1
    assert all('run' not in a and 'up' not in a for a in calls)


@pytest.mark.parametrize('field,value', [(field, value)
    for field in ('uid', 'euid', 'gid', 'egid') for value in (0, 1000, None, '999', True)])
def test_named_application_user_never_accepts_wrong_or_unproved_rights(field, value):
    identity = dict(uid=999, euid=999, gid=999, egid=999)
    identity[field] = value
    with pytest.raises(w.Refused, match='APP_UID_CHANGED'):
        w.verify_application_user('app', 'app', identity)


@pytest.mark.parametrize('expected,configured', [('root', 'root'), ('app', 'root'),
    ('app', '999'), ('unexpected', 'unexpected'), (None, '')])
def test_application_user_spelling_must_match_the_reviewed_compose_user(expected, configured):
    with pytest.raises(w.Refused, match='APP_UID_CHANGED'):
        w.verify_application_user(expected, configured,
            dict(uid=999, euid=999, gid=999, egid=999))


@pytest.mark.parametrize('proof', [None, [], '999'])
def test_application_user_missing_identity_proof_is_refused(proof):
    with pytest.raises(w.Refused, match='APP_UID_CHANGED'):
        w.verify_application_user('app', 'app', proof)


@pytest.mark.parametrize('stopped_status', ['exited', 'restarting'])
def test_restarting_source_is_fenced_once_and_requires_a_terminal_exit(sample, monkeypatch, stopped_status):
    client = w.DockerClient(sample[0]['manifest'])
    observed, commands = [], []
    def inspect(value, *, seconds, fencing):
        assert fencing is True
        observed.append(seconds)
        return {'id': 'a' * 64, 'running': False, 'exit_code': 2,
            'status': 'restarting' if len(observed) == 1 else stopped_status}
    def command(argv, *, seconds):
        commands.append(argv)
        return 'a' * 64
    monkeypatch.setattr(client, 'source_state', inspect)
    monkeypatch.setattr(client, 'command', command)
    # Docker can report Running=False during an automatic restart. Recovery
    # must stop the owned source once and cannot treat that state as released.
    if stopped_status == 'restarting':
        with pytest.raises(w.Refused, match='SOURCE_EXIT_UNVERIFIED'):
            client.stop_source({})
    else:
        assert client.stop_source({})['status'] == 'exited'
    assert commands == [['docker', 'stop', '--time', '5', 'a' * 64]]
    assert len(observed) == 2


def test_watchdog_uses_first_recovery_deadline_without_waiting_for_55minutes(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    task_id = sorted(w.TARGETS)[0]
    receipt = client.change(task_id, 4, 'paused')
    journal.update(lambda v: (v['confirmed'].update({task_id: receipt}),
        v.update(watchdog=None, watchdog_ready=False)))
    live, signals = [True], []
    def sleep(seconds):
        clock.sleep(seconds)
        if journal.read()['phase'] == 'arming':
            journal.update(lambda v: v.update(phase='restoring',
                recovery_started_monotonic=clock.now(),
                recovery_deadline=clock.now() + w.RECOVERY))
    def stop(identity, signum):
        signals.append((clock.now(), signum))
        live[0] = False
    result = w.watchdog(journal, client, clock=clock.now, sleep=sleep,
        liveness=lambda _: live[0], identity=lambda: journal.read()['worker'], stop_worker=stop)
    assert result['phase'] == 'restored'
    assert signals == [(result['recovery_deadline'] - 20, signal.SIGTERM)]
    assert signals[0][0] < result['restore_deadline']
    assert result['stop_requested'] == 'RECOVERY_CUTOFF'
    assert result['recovery_deadline'] == result['recovery_started_monotonic'] + w.RECOVERY
    changed = copy.deepcopy(result)
    changed['recovery_deadline'] += 1
    with pytest.raises(w.Refused, match='RECOVERY_DEADLINE_CHANGED'):
        w.validate_state(changed)


@pytest.mark.parametrize('change', ['scopes', 'missing_worker', 'unknown_publication',
    'unverified_publication', 'counts', 'over_scope_cap', 'tagged_session', 'missing_result'])
def test_partial_or_inconsistent_source_receipts_are_not_declared_completed(sample, change):
    client = FakeClient(sample[0], Clock())
    receipt = client.receipt()
    if change == 'scopes': receipt['batch']['scopes'] = list(reversed(receipt['batch']['scopes']))
    elif change == 'missing_worker': receipt['workers'].pop()
    elif change == 'unknown_publication': receipt['workers'][0]['unknown_publication'] = True
    elif change == 'unverified_publication': receipt['workers'][0]['publication']['status'] = 'unknown'
    elif change == 'counts': receipt['batch']['counts']['http_attempts'] = 4
    elif change == 'over_scope_cap': receipt['workers'][0]['counts']['http_attempts'] = 2
    elif change == 'tagged_session': receipt['tagged_activity'] = [{'n': 1}]
    else: receipt['batch']['results'].pop()
    with pytest.raises(w.Refused):
        w.verify_source_receipt(sample[0]['snapshot'], receipt, client.source_state())


def test_resume_restoration_does_not_claim_schedule_recovery_after_runner_restart(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    def changed(**kwargs): raise w.Refused('RUNNER_SCHEDULE_UNVERIFIED')
    client.verify_runner = changed
    result = execute(journal, client, clock)
    assert len(result['restore']) == 9
    assert all(r['outcome'] == 'restored' for r in result['restore'].values())
    assert result['phase'] == 'recovery_required'


def test_api_worker_checks_exact_ack_identity_and_never_exports_error_body(sample, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    import threading
    from pydantic import SecretStr
    task = copy.deepcopy(next(t for t in sample[0]['snapshot']['tasks'] if t['id'] in w.TARGETS))
    task['name'] = '测试采集'
    token = 'synthetic-window-token-at-least-thirty-two'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            assert self.path == f"/api/admin/tasks/{task['id']}/pause?version=4"
            assert self.headers['Authorization'] == 'Bearer ' + token
            self.send_response(self.server.response_code);self.end_headers()
            self.wfile.write(json.dumps(self.server.response).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.response_code = 200
    server.response = {**task, 'state': 'paused', 'version': 5}
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr('app.core.config.get_settings', lambda: SimpleNamespace(
        server_port=server.server_port, api_token=SecretStr(token)))
    request = {'action': 'change', 'id': task['id'], 'version': 4, 'target': 'paused',
               'fields': list(w.FIELDS)}
    def invoke():
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
        output = io.StringIO()
        with redirect_stdout(output): exec(compile(w.CONTAINER_CODE, '<window-worker>', 'exec'), {})
        raw = output.getvalue()
        assert token not in raw
        return json.loads(raw)
    try:
        result = invoke()
        assert result['ok'] and result['value']['definition_sha256'] == w.digest(w.definition(task))
        server.response['id'] = str(UUID(int=100))
        assert invoke()['uncertain'] is True
        server.response_code = 401
        server.response = {'secret': 'never export this server error body'}
        denied = invoke()
        assert denied == {'ok': False, 'reason': 'API_HTTP_401', 'uncertain': False}
    finally:
        server.shutdown();server.server_close();worker.join(2)


def test_journal_checksum_and_symlinks_are_not_accepted(sample):
    _, root = sample
    journal = w.Journal(root / 'state.json')
    state = {'plan': {}, 'plan_sha256': w.digest({})}
    journal.create(state)
    (root / 'state.json').write_text('{"payload":{},"sha256":"wrong"}')
    with pytest.raises((w.Refused, KeyError)): journal.read()
    link = root / 'symlink.json'
    link.symlink_to(root / 'state.json')
    with pytest.raises(OSError): w.read_private(link)


@pytest.mark.parametrize('remaining', [0, 1, 19])
def test_no_resume_api_is_started_without_its_recovery_reserve(sample, monkeypatch, remaining):
    journal, client, clock = unit_window(sample, monkeypatch)
    task_id = sorted(w.TARGETS)[0]
    receipt = client.change(task_id, 4, 'paused')
    journal.update(lambda v: v['confirmed'].update({task_id: receipt}))
    clock.value = journal.read()['deadline'] - remaining
    result = w.restoring(journal, client, clock=clock.now)
    assert len(client.calls) == 1 and result['phase'] == 'recovery_required'
    assert result['restore'][task_id]['outcome'] == 'DEADLINE_EXPIRED'


def test_late_final_verification_does_not_claim_the_window_met_its_deadline(sample, monkeypatch):
    journal, client, clock = unit_window(sample, monkeypatch)
    task_id = sorted(w.TARGETS)[0]
    receipt = client.change(task_id, 4, 'paused')
    journal.update(lambda v: v['confirmed'].update({task_id: receipt}))
    clock.value = journal.read()['deadline'] - 30
    def slow_final(**kwargs):
        clock.sleep(31)
        return True
    client.verify_runner = slow_final
    result = w.restoring(journal, client, clock=clock.now)
    assert result['restore'][task_id]['outcome'] == 'restored'
    assert result['deadline_met'] is False and result['phase'] == 'recovery_required'


class FileClient:
    """Cross-process synthetic API state; no Docker or network access."""
    def __init__(self, path): self.path = Path(path)

    def snapshot(self): return w.read_private(self.path)

    def task(self, task_id, **kwargs):
        return next(t for t in self.snapshot()['tasks'] if t['id'] == task_id)

    def call(self, action, **kwargs):
        assert action == 'snapshot'
        return self.snapshot()

    def change(self, task_id, version, target, **kwargs):
        state = self.snapshot()
        task = next(t for t in state['tasks'] if t['id'] == task_id)
        assert task['version'] == version
        task.update(state=target, version=version+1)
        state.setdefault('api_calls', []).append([task_id, version, target])
        w.write_private(self.path, state)
        return {'id': task_id, 'state': target, 'version': version+1,
                'definition_sha256': w.digest(w.definition(task))}

    def stop_source(self, value, **kwargs):
        assert not value['source_launch_intent']
        return None

    def verify_runner(self, **kwargs): return True


def pause_producer(journal_path, api_path, connection):
    connection.send(w.process_identity())
    connection.recv()
    task_id = sorted(w.TARGETS)[0]
    receipt = FileClient(api_path).change(task_id, 4, 'paused')
    journal = w.Journal(journal_path)
    journal.update(lambda v: v['confirmed'].update({task_id: receipt}))
    connection.send('paused')
    while True: time.sleep(1)


def detached_watch(journal_path, api_path):
    os.setsid()
    with open(os.devnull, 'rb') as devnull: os.dup2(devnull.fileno(), 0)
    w.watchdog(w.Journal(journal_path), FileClient(api_path), stop_worker=w.stop_owned_worker)


@pytest.mark.skipif(sys.platform != 'linux', reason='real Linux /proc ownership required')
@pytest.mark.parametrize('cause', ['operator_SIGKILL', '55minute_cutoff'])
def test_real_detached_watchdog_restores_after_operator_loss_or_expiry(sample, cause):
    plan, root = sample
    api = root / 'synthetic-api.json'
    w.write_private(api, plan['snapshot'])
    ctx = multiprocessing.get_context('fork')
    parent, child = ctx.Pipe()
    operator = ctx.Process(target=pause_producer, args=(root/'state.json', api, child))
    operator.start()
    assert parent.poll(5)
    identity = parent.recv()
    journal = w.Journal(root / 'state.json')
    value = w.new_state(plan, identity)
    if cause == '55minute_cutoff':
        value['started_monotonic'] -= w.TOTAL - w.RECOVERY
        for name in ('drain_deadline', 'launch_deadline', 'restore_deadline', 'deadline'):
            value[name] -= w.TOTAL - w.RECOVERY
    journal.create(value)
    parent.send('go')
    assert parent.poll(5) and parent.recv() == 'paused'
    guard = ctx.Process(target=detached_watch, args=(root/'state.json', api))
    guard.start()
    try:
        until = time.monotonic() + 5
        while time.monotonic() < until and not journal.read()['watchdog_ready']: time.sleep(.05)
        assert journal.read()['watchdog_ready']
        assert os.getsid(guard.pid) == guard.pid
        if cause == 'operator_SIGKILL':
            os.kill(operator.pid, signal.SIGKILL)
        operator.join(5)
        guard.join(8)
        assert not operator.is_alive() and not guard.is_alive()
        result = journal.read()
        assert result['phase'] == 'restored'
        assert result['source_launch_intent'] is False
        restored = FileClient(api).snapshot()
        assert restored['api_calls'] == [[sorted(w.TARGETS)[0], 4, 'paused'],
                                          [sorted(w.TARGETS)[0], 5, 'active']]
        assert restored['runs'] == plan['snapshot']['runs']
    finally:
        for process in (operator, guard):
            if process.is_alive(): process.kill()
            process.join(3)


@pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PostgreSQL required')
def test_real_postgresql_paused_task_still_claims_accepted_queue(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from tests.test_data_store_kernel import test_url
    from app.scheduling.models import ScheduledTask, TaskRun
    from app.scheduling.repository import SchedulerRepository
    from app.scheduling.service import SchedulerService, TaskConflictError
    from app.scheduling.registry import TaskRegistry
    from app.scheduling.schemas import TaskState, TriggerType, RunStatus
    from app.data_sources.models import DataSourceConfig
    from uuid import uuid4
    schema = 'r01_window_' + uuid4().hex
    admin = create_engine(test_url())
    with admin.begin() as c: c.execute(text('CREATE SCHEMA ' + schema))
    engine = create_engine(test_url(), connect_args={'options': '-csearch_path=' + schema})
    try:
        with engine.begin() as c:
            ScheduledTask.__table__.create(c)
            TaskRun.__table__.create(c)
            DataSourceConfig.__table__.create(c)
        with Session(engine) as s:
            # Chinese operational messages must retain the existing service
            # switch fingerprint. An empty config table cannot expose a
            # mistaken UTF-8/ASCII serialization contract.
            s.add(DataSourceConfig(key='synthetic-config', initialized=True,
                enabled=True, values={'label': '合成配置'}, version=1,
                encrypted_secrets='synthetic-encrypted-placeholder',
                check_status='success', check_message='合成检查'))
            task = ScheduledTask(name='Synthetic accepted collector', task_type='test.noop',
                parameters={}, parameter_version=1, schedule={'type':'cron','expression':'*/10 * * * *',
                'timezone':'Asia/Shanghai'}, state='active', version=1, concurrency_limit=1,
                overlap_policy='skip', queue_limit=1, priority=0)
            s.add(task);s.flush()
            repository = SchedulerRepository(s)
            accepted = repository.add_run(task, trigger_type=TriggerType.SCHEDULED, status=RunStatus.QUEUED)
            service = SchedulerService(s, TaskRegistry())
            service.change_state(task.id, expected_version=1, target=TaskState.PAUSED)
            monkeypatch.setattr('app.scheduling.service.lock_source_gates', lambda *a: {})
            with pytest.raises(TaskConflictError, match='does not allow'):
                service.enqueue_run(task.id, trigger_type=TriggerType.SCHEDULED, max_queued_runs=100)
            assert repository.claim_queued_runs(1) == [accepted.id]
            assert task.state == 'paused' and task.version == 2
            assert accepted.status == 'running' and accepted.task_version == 1
            assert accepted.parameters == {}
            s.commit()
            configs = s.execute(text('SELECT row_to_json(t) FROM '
                '(SELECT * FROM data_source_configs ORDER BY key) t')).scalars().all()
            from tests.test_r01_service_switch import switcher
            legacy_config_sha = switcher.digest(configs)
            assert legacy_config_sha != w.digest(configs)
        # Run the shipped metadata worker against this isolated schema. This
        # checks real SQL, serialization and the read-only transaction boundary.
        monkeypatch.setattr('app.db.session.get_engine', lambda: engine)
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
            'action':'snapshot','fields':list(w.FIELDS),'accepted':[]})))
        output = io.StringIO()
        with redirect_stdout(output): exec(compile(w.CONTAINER_CODE, '<window-worker>', 'exec'), {})
        response = json.loads(output.getvalue())
        assert response['ok'] and response['value']['tasks'][0]['state'] == 'paused'
        assert response['value']['source_configs_sha256'] == legacy_config_sha
    finally:
        engine.dispose()
        with admin.begin() as c: c.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        admin.dispose()
