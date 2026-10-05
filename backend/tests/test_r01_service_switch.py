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
from types import SimpleNamespace
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


def kernel_clock(namespace='time:[4026531834]'):
    return {'namespace': namespace, 'offset_namespace': namespace,
            'boot_id': '6f430381-ff78-4273-989b-73f1cac33934',
            'offsets': 'monotonic           0         0\nboottime            0         0\n'}


def protocol_gate(plan, monkeypatch, *, remote=None, host=None, deadline=250.0):
    """Exercise real protocol/deadline checks with only the child pipe replaced."""
    clock = [100.0]
    remote = kernel_clock('time:[4026532836]') if remote is None else remote
    host = kernel_clock() if host is None else host
    monkeypatch.setattr(switcher.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(switcher, 'clock_domain', lambda: host)
    ready = {'stage': 'drained', 'monotonic_namespace': remote.get('namespace'),
             'clock_domain': remote, 'release_deadline_monotonic': deadline}
    child = SimpleNamespace(buffer=b'', process=SimpleNamespace(poll=lambda: None))
    lines = [json.dumps(ready).encode() + b'\n']
    def read(wait):
        if lines:
            child.buffer += lines.pop()
    child.read = read
    return switcher.Gate(child, plan), ready, clock


@pytest.mark.parametrize('namespace', ['time:[4026531834]', 'time:[4026532836]'])
def test_same_boot_zero_offsets_accept_distinct_namespaces_without_renewing_deadline(plan, monkeypatch, namespace):
    gate, ready, clock = protocol_gate(plan, monkeypatch, remote=kernel_clock(namespace))
    # The emitted deadline is 250, and 60 seconds of transport latency have
    # already elapsed. The controller must retain only its remaining 90 seconds.
    clock[0] = 160.0
    assert gate.drained() == ready
    assert gate.deadline == 250.0 and gate.host_clock_domain == kernel_clock()
    gate.check(89)
    clock[0] = 250.0
    with pytest.raises(switcher.Refused, match='RELEASE_WINDOW_INSUFFICIENT'):
        gate.check(0)
    assert gate.deadline == 250.0


@pytest.mark.parametrize('side', ['host', 'remote'])
@pytest.mark.parametrize('change', ['missing_boot', 'invalid_boot', 'missing_namespace', 'invalid_namespace',
    'missing_offset_namespace', 'different_offset_namespace',
    'missing_offsets', 'non_string_offsets', 'monotonic_seconds', 'monotonic_nanoseconds',
    'boottime_seconds', 'duplicate_clock', 'missing_clock', 'extra_clock', 'extra_field'])
def test_unknown_or_nonzero_clock_proof_is_refused_even_when_namespaces_match(plan, monkeypatch, side, change):
    host, remote = kernel_clock(), kernel_clock()
    altered = host if side == 'host' else remote
    if change.startswith('missing_') and change != 'missing_clock':
        del altered[{'missing_boot': 'boot_id', 'missing_namespace': 'namespace',
                     'missing_offsets': 'offsets', 'missing_offset_namespace': 'offset_namespace'}[change]]
    elif change == 'invalid_boot':
        altered['boot_id'] = 'unknown'
    elif change == 'invalid_namespace':
        altered['namespace'] = 'time:[unknown]'
    elif change == 'non_string_offsets':
        altered['offsets'] = None
    elif change == 'different_offset_namespace':
        altered['offset_namespace'] = 'time:[4026531835]'
    elif change == 'extra_field':
        altered['unproved'] = '0'
    else:
        altered['offsets'] = {'monotonic_seconds': 'monotonic 1 0\nboottime 0 0\n',
            'monotonic_nanoseconds': 'monotonic 0 1\nboottime 0 0\n',
            'boottime_seconds': 'monotonic 0 0\nboottime 1 0\n',
            'duplicate_clock': 'monotonic 0 0\nmonotonic 0 0\n',
            'missing_clock': 'monotonic 0 0\n',
            'extra_clock': 'monotonic 0 0\nboottime 0 0\nrealtime 0 0\n'}[change]
    gate, _, _ = protocol_gate(plan, monkeypatch, host=host, remote=remote)
    with pytest.raises(switcher.Refused, match='GATE_CLOCK_OR_DEADLINE_INVALID'):
        gate.drained()
    assert gate.deadline is None


def test_same_namespace_and_offsets_cannot_hide_different_boot_id(plan, monkeypatch):
    remote = kernel_clock()
    remote['boot_id'] = '7f430381-ff78-4273-989b-73f1cac33934'
    gate, _, _ = protocol_gate(plan, monkeypatch, remote=remote)
    with pytest.raises(switcher.Refused, match='GATE_CLOCK_OR_DEADLINE_INVALID'):
        gate.drained()
    assert gate.deadline is None


@pytest.mark.parametrize('change', ['missing_proof', 'namespace_disagrees'])
def test_gate_protocol_requires_new_kernel_proof_and_consistent_namespace(plan, monkeypatch, change):
    gate, ready, _ = protocol_gate(plan, monkeypatch)
    if change == 'missing_proof':
        del ready['clock_domain']
    else:
        ready['monotonic_namespace'] = 'time:[4026532837]'
    gate.child.read = lambda wait: None
    gate.child.buffer = json.dumps(ready).encode() + b'\n'
    with pytest.raises(switcher.Refused, match='GATE_CLOCK_OR_DEADLINE_INVALID'):
        gate.drained()
    assert gate.deadline is None


@pytest.mark.parametrize('deadline', [99.0, 100.0, 282.0, float('inf'), float('nan'), True, '250', None])
def test_clock_equivalence_never_accepts_expired_nonfinite_or_extended_deadline(plan, monkeypatch, deadline):
    gate, _, _ = protocol_gate(plan, monkeypatch, deadline=deadline)
    with pytest.raises(switcher.Refused, match='GATE_CLOCK_OR_DEADLINE_INVALID'):
        gate.drained()
    assert gate.deadline is None


def test_kernel_proof_read_cannot_cross_the_original_deadline(plan, monkeypatch):
    gate, _, clock = protocol_gate(plan, monkeypatch)
    def delayed_kernel_read():
        clock[0] = 251.0
        return kernel_clock()
    monkeypatch.setattr(switcher, 'clock_domain', delayed_kernel_read)
    with pytest.raises(switcher.Refused, match='GATE_CLOCK_OR_DEADLINE_INVALID'):
        gate.drained()
    assert gate.deadline is None


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


@pytest.mark.parametrize('expiry,missing', [(3, ('backend',)), (4, ('backend',)),
                                         (5, ('backend', 'runner')), (6, ('backend', 'runner'))])
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
@pytest.mark.parametrize('probe_number', [3, 4])
def test_fenced_record_or_seal_change_after_stop_causes_one_old_restart(plan, change, probe_number):
    host = FakeHost(plan)
    def mutate(n, s):
        if n == probe_number:
            if change == 'record_delete':
                s['records'] = []
            elif change == 'record_change':
                s['records'][0]['record_hash'] = 'f' * 64
            else:
                s['resources']['retained_seals'] = []
    host.change = mutate
    code, events = execute(plan, host)
    assert code == 2 and events[-1]['restored_old'] is True
    missing = ('backend',) if probe_number == 3 else switcher.SERVICES
    assert [c for c in host.calls if c[0] == 'start'] == [('start', plan.old_image, missing)]


def test_captured_queue_can_become_running_after_release_without_losing_identity(plan):
    host = FakeHost(plan)
    def accepted_after_release(n, s):
        if n == 5:
            s['activity']['accepted_runs'] = 1
            s['records'][0]['record_hash'] = 'f' * 64
    host.change = accepted_after_release
    assert execute(plan, host)[0] == 0


@pytest.mark.parametrize('phase', ['normal', 'recovery'])
def test_post_backend_idle_probe_cannot_renew_original_runner_stop_deadline(plan, phase, monkeypatch):
    """Use the real Gate.check after a slow, otherwise idle SQL probe."""
    clock = [0.0]
    monkeypatch.setattr(switcher.time, 'monotonic', lambda: clock[0])
    gates = []

    class DeadlineGate(FakeGate):
        def __init__(self, host, number):
            super().__init__(host, number)
            self.deadline = clock[0] + plan.hold_seconds
            gates.append(self)

        def check(self, seconds):
            super().check(seconds)
            # Only child liveness/output are synthetic. The actual original-
            # deadline comparison is the committed Gate.check implementation.
            gate = object.__new__(switcher.Gate)
            gate.deadline = self.deadline
            gate.collect = lambda: None
            gate.child = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
            switcher.Gate.check(gate, seconds)

    class SlowProbeHost(FakeHost):
        def gate(self):
            super().gate()
            return DeadlineGate(self, self.gates)

        def probe(self, captured, resources):
            value = super().probe(captured, resources)
            target_gate = 1 if phase == 'normal' else 2
            if self.gates == target_gate and self.current['backend']['stopped'] and self.current['runner']['running']:
                self.preserved_runner = copy.deepcopy(self.current['runner'])
                clock[0] = gates[-1].deadline + 1
            return value

    host = SlowProbeHost(plan)
    host.fail_health = phase == 'recovery'
    code, events = execute(plan, host)
    assert code == 2 and not any(e['stage'] == 'completed' for e in events)
    expected_stops = [('stop', 'backend')] if phase == 'normal' else [
        ('stop', 'backend'), ('stop', 'runner'), ('stop', 'backend')]
    assert [c for c in host.calls if c[0] == 'stop'] == expected_stops
    assert host.states()['runner'] == host.preserved_runner
    assert gates[-1].deadline == plan.hold_seconds
    assert [c for c in host.calls if c[0] == 'start' and c[1] == plan.old_image] == [
        ('start', plan.old_image, ('backend',))]
    assert host.gates == (1 if phase == 'normal' else 2)


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
    host.image_revisions = {plan.old_image: '1' * 40}
    inspections = [0]
    def containers(deadline=None):
        inspections[0] += 1
        return {s: {'Id': ('a' if s == 'backend' else 'b') * 64,
                    'State': {'Running': inspections[0] == 1, 'Status': 'running' if inspections[0] == 1 else 'exited'},
                    'Config': {'Labels': {switcher.OCI_REVISION: '1' * 40}, 'Env': []},
                    'HostConfig': {}, 'Mounts': [], 'Image': plan.old_image} for s in switcher.SERVICES}
    host.containers = containers
    host.stop('backend')
    host.start(plan.candidate_image, ('backend', 'runner'))
    assert calls[0][-4:] == ['kill', '--signal', 'SIGTERM', 'a' * 64]
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


def mounted_runtime():
    # These two declarations model the real P01 current-store/log volume pair.
    # No application data, credentials or production volume is mounted by tests.
    return {'Id': 'a' * 64, 'Config': {'Hostname': 'a' * 12, 'Labels': {}, 'Env': []},
            'HostConfig': {'Binds': ['logs:/app/data/logs:rw', 'current:/app/data/current-store:rw'],
                           'Init': True},
            'Mounts': [{'Type': 'volume', 'Name': name, 'Source': '/volumes/' + name,
                        'Destination': target, 'Mode': 'rw', 'RW': True, 'Propagation': ''}
                       for name, target in [('logs', '/app/data/logs'),
                                            ('current', '/app/data/current-store')]]}


def test_runtime_disjoint_bind_reordering_preserves_exact_mount_configuration():
    old = mounted_runtime()
    new = copy.deepcopy(old)
    new['HostConfig']['Binds'].reverse()
    new['Mounts'].reverse()
    assert switcher.service_spec(new) == switcher.service_spec(old)


@pytest.mark.parametrize('defect', ['source', 'target', 'mode', 'observed_source',
                                  'observed_permission', 'host_limit', 'application_label'])
def test_runtime_bind_reordering_does_not_hide_a_configuration_change(defect):
    old = mounted_runtime()
    new = copy.deepcopy(old)
    new['HostConfig']['Binds'].reverse()
    if defect == 'source':
        new['HostConfig']['Binds'][0] = 'other:/app/data/current-store:rw'
    elif defect == 'target':
        new['HostConfig']['Binds'][0] = 'current:/other:rw'
    elif defect == 'mode':
        new['HostConfig']['Binds'][0] = 'current:/app/data/current-store:ro'
    elif defect == 'observed_source':
        new['Mounts'][1]['Source'] = '/other/current'
    elif defect == 'observed_permission':
        new['Mounts'][1]['RW'] = False
    elif defect == 'host_limit':
        new['HostConfig']['Memory'] = 1024
    else:
        new['Config']['Labels']['application'] = 'changed'
    assert switcher.service_spec(new) != switcher.service_spec(old)


@pytest.mark.parametrize('unproven', ['duplicate_target', 'overlapping_target', 'ambiguous_source',
                                    'missing_mount', 'mode_disagreement'])
def test_runtime_unproven_bind_declarations_keep_strict_order(unproven):
    old = mounted_runtime()
    if unproven == 'duplicate_target':
        old['HostConfig']['Binds'][1] = 'current:/app/data/logs:rw'
        old['Mounts'][1]['Destination'] = '/app/data/logs'
    elif unproven == 'overlapping_target':
        old['HostConfig']['Binds'][1] = 'current:/app/data/logs/nested:rw'
        old['Mounts'][1]['Destination'] = '/app/data/logs/nested'
    elif unproven == 'ambiguous_source':
        old['HostConfig']['Binds'][1] = 'current:ambiguous:/app/data/current-store:rw'
        old['Mounts'][1]['Name'] = 'current:ambiguous'
    elif unproven == 'missing_mount':
        old['Mounts'].pop()
    else:
        old['Mounts'][1]['Mode'] = 'ro'
    new = copy.deepcopy(old)
    new['HostConfig']['Binds'].reverse()
    assert switcher.service_spec(new) != switcher.service_spec(old)


def revision_container(plan, service, *, candidate=False, legacy=None):
    identity = ('c' if service == 'backend' else 'd') * 64 if candidate else ('a' if service == 'backend' else 'b') * 64
    predecessor = ('a' if service == 'backend' else 'b') * 64
    labels = {switcher.OCI_REVISION: plan.candidate_head if candidate else '1' * 40,
              'qf_application': 'preserved', 'com.docker.compose.project': plan.project,
              'com.docker.compose.service': service, 'com.docker.compose.oneoff': 'False'}
    if candidate:
        labels[switcher.COMPOSE_REPLACE] = predecessor
    elif legacy is not None:
        labels[switcher.COMPOSE_REPLACE] = legacy
    return {'Id': identity, 'Image': plan.candidate_image if candidate else plan.old_image,
            'Config': {'Image': plan.candidate_image if candidate else plan.old_image,
                       'Hostname': identity[:12], 'Labels': labels, 'Env': ['synthetic=preserved']},
            'HostConfig': {'Init': True}, 'Mounts': [],
            'State': {'Running': True, 'Status': 'running', 'Health': {'Status': 'healthy'}}}


def revision_preflight_host(plan, monkeypatch, *, candidate_revision=None, candidate_identity=None):
    from types import SimpleNamespace
    monkeypatch.setattr(switcher, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(switcher, 'clock_domain', kernel_clock)
    files = [{'path': 'app/__init__.py', 'sha256': 'a' * 64}]
    Path(plan.manifest_file).write_text(json.dumps({'head': plan.candidate_head,
        'image': plan.candidate_image, 'files': files}))
    host = switcher.DockerHost(plan)
    old, new = compose_config(), compose_config()
    for service in switcher.SERVICES:
        new['services'][service]['image'] = plan.candidate_image
    host.config = lambda candidate: new if candidate else old
    calls = []
    def run(args, seconds=10):
        calls.append(args)
        if args[:3] == ['docker', 'image', 'inspect']:
            candidate = args[3] == plan.candidate_image
            return json.dumps([{'Id': (candidate_identity or plan.candidate_image) if candidate else plan.old_image,
                'Config': {'Labels': {switcher.OCI_REVISION: (candidate_revision or plan.candidate_head)
                                     if candidate else '1' * 40}}}])
        assert args[:2] == ['docker', 'run']
        return json.dumps(files)
    host.run = run
    host.containers = lambda deadline=None: {s: revision_container(plan, s, legacy=s + '-1') for s in switcher.SERVICES}
    return host, calls


def test_preflight_pins_exact_candidate_image_revision_and_legacy_runtime_labels(plan, monkeypatch):
    host, calls = revision_preflight_host(plan, monkeypatch)
    host.preflight()
    assert host.image_revisions == {plan.old_image: '1' * 40, plan.candidate_image: plan.candidate_head}
    assert host.replacement_proofs['a' * 64] == 'backend-1'
    assert host.replacement_proofs['b' * 64] == 'runner-1'
    assert any(c[:3] == ['docker', 'image', 'inspect'] and c[3] == plan.candidate_image for c in calls)


@pytest.mark.parametrize('defect', ['image_identity', 'image_revision'])
def test_preflight_refuses_forged_candidate_image_identity_or_revision(plan, monkeypatch, defect):
    kwargs = {'candidate_identity': 'sha256:' + 'f' * 64} if defect == 'image_identity' else {'candidate_revision': 'f' * 40}
    host, calls = revision_preflight_host(plan, monkeypatch, **kwargs)
    with pytest.raises(switcher.Refused, match='IMAGE_IDENTITY_MISMATCH|CANDIDATE_IMAGE_REVISION_MISMATCH'):
        host.preflight()
    assert not any(c[:2] == ['docker', 'run'] for c in calls)


def test_verified_legacy_old_revision_to_real_stopped_predecessor_replacement_is_allowed(plan, monkeypatch):
    host, _ = revision_preflight_host(plan, monkeypatch)
    host.preflight()
    host.stopped_ids.update(host.service_ids.values())
    new = {s: revision_container(plan, s, candidate=True) for s in switcher.SERVICES}
    old = {s: revision_container(plan, s, legacy=s + '-1') for s in switcher.SERVICES}
    # Without daemon image/predecessor proof, no revision label is omitted.
    assert switcher.service_spec(old['backend']) != switcher.service_spec(new['backend'])
    host.containers = lambda deadline=None: copy.deepcopy(new)
    host.verify_candidate()
    assert host.verified_spec(new['backend'], 'backend') == host.baseline['backend']
    assert host.verified_spec(new['runner'], 'runner') == host.baseline['runner']
    # The same bounded restoration may replace the actually stopped candidate;
    # its predecessor proof follows that candidate ID, never an old alias.
    host.stopped_ids.update(c['Id'] for c in new.values())
    restored = revision_container(plan, 'backend')
    restored['Id'] = 'e' * 64
    restored['Config']['Hostname'] = 'e' * 12
    restored['Config']['Labels'][switcher.COMPOSE_REPLACE] = new['backend']['Id']
    assert host.verified_spec(restored, 'backend') == host.baseline['backend']


@pytest.mark.parametrize('defect', ['runtime_revision', 'other_app_label', 'forged_predecessor',
                                 'legacy_alias', 'not_stopped', 'unknown_image'])
def test_candidate_runtime_provenance_and_all_other_labels_remain_strict(plan, monkeypatch, defect):
    host, _ = revision_preflight_host(plan, monkeypatch)
    host.preflight()
    host.stopped_ids.update(host.service_ids.values())
    new = {s: revision_container(plan, s, candidate=True) for s in switcher.SERVICES}
    labels = new['backend']['Config']['Labels']
    if defect == 'runtime_revision':
        labels[switcher.OCI_REVISION] = 'f' * 40
    elif defect == 'other_app_label':
        labels['qf_application'] = 'changed'
    elif defect == 'forged_predecessor':
        labels[switcher.COMPOSE_REPLACE] = 'f' * 64
    elif defect == 'legacy_alias':
        labels[switcher.COMPOSE_REPLACE] = 'backend-1'
    elif defect == 'not_stopped':
        host.stopped_ids.clear()
    else:
        new['backend']['Image'] = 'sha256:' + 'f' * 64
    host.containers = lambda deadline=None: copy.deepcopy(new)
    with pytest.raises(switcher.Refused):
        host.verify_candidate()


def test_original_legacy_replace_value_is_pinned_to_original_container_id(plan, monkeypatch):
    host, _ = revision_preflight_host(plan, monkeypatch)
    host.preflight()
    changed = revision_container(plan, 'backend', legacy='runner-1')
    with pytest.raises(switcher.Refused, match='CONTAINER_REPLACEMENT_MISMATCH'):
        host.verified_spec(changed, 'backend')


def logical_replacement_start(plan, monkeypatch, *, fail_health=False, partial=False):
    """Only start's owned before/after observations establish the new ID receipt."""
    host, _ = revision_preflight_host(plan, monkeypatch)
    host.preflight()
    before = {s: revision_container(plan, s, legacy=s + '-1') for s in switcher.SERVICES}
    for container in before.values():
        container['State'].update(Running=False, Status='exited')
    after = {s: revision_container(plan, s, candidate=True) for s in switcher.SERVICES}
    for service, container in after.items():
        container['Config']['Labels'][switcher.COMPOSE_REPLACE] = service + '-1'
    if partial:
        after['runner'] = before['runner']
    current = [before]
    host.containers = lambda deadline=None: copy.deepcopy(current[0])
    def up(args, seconds=10):
        assert 'up' in args
        current[0] = after
        if fail_health:
            raise switcher.Refused('COMMAND_FAILED')
        return ''
    host.run = up
    return host, before, after


def test_owned_start_binds_real_logical_labels_to_each_exact_stopped_predecessor(plan, monkeypatch):
    host, before, after = logical_replacement_start(plan, monkeypatch)
    host.start(plan.candidate_image, switcher.SERVICES)
    for service in switcher.SERVICES:
        assert host.created_predecessors[after[service]['Id']] == (
            service, before[service]['Id'], plan.candidate_image)
    host.verify_candidate()
    assert host.service_ids == {s: c['Id'] for s, c in after.items()}


@pytest.mark.parametrize('partial', [False, True])
def test_failed_health_start_keeps_only_the_observed_new_ID_receipts_for_recovery(plan, monkeypatch, partial):
    host, before, after = logical_replacement_start(plan, monkeypatch, fail_health=True, partial=partial)
    with pytest.raises(switcher.Refused, match='COMMAND_FAILED'):
        host.start(plan.candidate_image, switcher.SERVICES)
    assert host.created_predecessors[after['backend']['Id']] == (
        'backend', before['backend']['Id'], plan.candidate_image)
    assert host.verified_spec(after['backend'], 'backend') == host.baseline['backend']
    if partial:
        assert before['runner']['Id'] not in host.created_predecessors


def test_owned_restoration_logical_label_binds_to_stopped_candidate_not_original_ID(plan, monkeypatch):
    host, _, after = logical_replacement_start(plan, monkeypatch)
    host.start(plan.candidate_image, switcher.SERVICES)
    host.verify_candidate()
    candidate = copy.deepcopy(after)
    for c in candidate.values():
        c['State'].update(Running=False, Status='exited')
    restored = {s: revision_container(plan, s, legacy=s + '-1') for s in switcher.SERVICES}
    for service, c in restored.items():
        c['Id'] = ('e' if service == 'backend' else 'f') * 64
        c['Config']['Hostname'] = c['Id'][:12]
    current = [candidate]
    host.containers = lambda deadline=None: copy.deepcopy(current[0])
    host.run = lambda args, seconds=10: current.__setitem__(0, restored) or ''
    host.start(plan.old_image, switcher.SERVICES)
    for service in switcher.SERVICES:
        assert host.created_predecessors[restored[service]['Id']] == (
            service, candidate[service]['Id'], plan.old_image)
        assert host.verified_spec(restored[service], service) == host.baseline[service]


@pytest.mark.parametrize('defect', ['missing', 'wrong_service', 'wrong_predecessor', 'wrong_image',
                                 'predecessor_not_stopped', 'different_new_ID', 'other_alias', 'other_app_label'])
def test_logical_label_without_exact_owned_creation_receipt_stays_refused(plan, monkeypatch, defect):
    host, before, after = logical_replacement_start(plan, monkeypatch)
    host.start(plan.candidate_image, switcher.SERVICES)
    new = copy.deepcopy(after)
    identity = new['backend']['Id']
    receipt = ('backend', before['backend']['Id'], plan.candidate_image)
    if defect == 'missing':
        del host.created_predecessors[identity]
    elif defect == 'wrong_service':
        host.created_predecessors[identity] = ('runner', receipt[1], receipt[2])
    elif defect == 'wrong_predecessor':
        host.created_predecessors[identity] = ('backend', 'f' * 64, receipt[2])
    elif defect == 'wrong_image':
        host.created_predecessors[identity] = ('backend', receipt[1], plan.old_image)
    elif defect == 'predecessor_not_stopped':
        host.stopped_ids.clear()
    elif defect == 'different_new_ID':
        new['backend']['Id'] = 'e' * 64
        new['backend']['Config']['Hostname'] = 'e' * 12
    elif defect == 'other_alias':
        new['backend']['Config']['Labels'][switcher.COMPOSE_REPLACE] = 'backend-2'
    else:
        new['backend']['Config']['Labels']['qf_application'] = 'changed'
    host.containers = lambda deadline=None: copy.deepcopy(new)
    with pytest.raises(switcher.Refused):
        host.verify_candidate()


def test_post_start_observation_cannot_renew_its_original_finite_budget(plan, monkeypatch):
    host, _, after = logical_replacement_start(plan, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(switcher.time, 'monotonic', lambda: clock[0])
    containers = host.containers
    def observe(deadline=None):
        if deadline is not None and clock[0] >= deadline:
            raise switcher.Refused('COMMAND_TIMEOUT')
        return containers(deadline)
    host.containers = observe
    up = host.run
    def delayed_up(args, seconds=10):
        result = up(args, seconds)
        clock[0] = seconds + 1
        return result
    host.run = delayed_up
    with pytest.raises(switcher.Refused, match='COMMAND_TIMEOUT'):
        host.start(plan.candidate_image, switcher.SERVICES)
    assert not host.created_predecessors
    with pytest.raises(switcher.Refused, match='CONTAINER_REPLACEMENT_MISMATCH'):
        host.verified_spec(after['backend'], 'backend')


@pytest.mark.parametrize('exit_kind', ['self_exit', 'late_SIGTERM_exit'])
def test_recovery_start_observes_actual_stopped_candidate_predecessor(plan, monkeypatch, exit_kind):
    host, _ = revision_preflight_host(plan, monkeypatch)
    host.preflight()
    host.stopped_ids.update(host.service_ids.values())
    candidate = revision_container(plan, 'backend', candidate=True)
    candidate['State'].update(Running=False, Status='exited')
    if exit_kind == 'late_SIGTERM_exit':
        # stop's earlier timeout did not register this candidate ID. The
        # current inspection, not a successful stop return, supplies proof.
        host.verified_spec(candidate, 'backend')
    assert candidate['Id'] not in host.stopped_ids
    host.containers = lambda deadline=None: {'backend': copy.deepcopy(candidate)}
    calls = []
    host.run = lambda args, seconds=10: calls.append(args) or ''
    host.start(plan.old_image, ('backend',))
    assert candidate['Id'] in host.stopped_ids
    assert len(calls) == 1 and 'up' in calls[0] and calls[0][-1] == 'backend'
    restored = revision_container(plan, 'backend')
    restored['Id'] = 'e' * 64
    restored['Config']['Hostname'] = 'e' * 12
    restored['Config']['Labels'][switcher.COMPOSE_REPLACE] = candidate['Id']
    assert host.verified_spec(restored, 'backend') == host.baseline['backend']


@pytest.mark.parametrize('defect', ['running', 'forged_revision'])
def test_recovery_start_never_proves_running_or_forged_predecessor(plan, monkeypatch, defect):
    host, _ = revision_preflight_host(plan, monkeypatch)
    host.preflight()
    host.stopped_ids.update(host.service_ids.values())
    candidate = revision_container(plan, 'backend', candidate=True)
    candidate['State'].update(Running=defect == 'running', Status='running' if defect == 'running' else 'exited')
    if defect == 'forged_revision':
        candidate['Config']['Labels'][switcher.OCI_REVISION] = 'f' * 40
    host.containers = lambda deadline=None: {'backend': copy.deepcopy(candidate)}
    calls = []
    host.run = lambda args, seconds=10: calls.append(args) or ''
    with pytest.raises(switcher.Refused):
        host.start(plan.old_image, ('backend',))
    assert candidate['Id'] not in host.stopped_ids and not calls


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
@pytest.mark.parametrize('phase', ['normal', 'recovery'])
def test_new_pg_backtest_after_backend_exit_preserves_runner(database, plan, phase, monkeypatch):
    """A source-row drain does not fence a newly claimed backtest.

    Insert the unfinished backtest into real PostgreSQL after the relevant
    backend has exited. The production database probe, rather than a mocked
    activity count, must stop the runner SIGTERM and preserve its work.
    """
    engine, _ = database
    queued = scheduler_tables(engine)
    before = switcher.database_snapshot(engine, switcher.OWNED_TASK, [queued])
    # Match the private fixture's real 177-task baseline without bypassing
    # the production scope guard or changing any production task definition.
    monkeypatch.setattr(switcher, 'ORIGINAL_177', before['original_fingerprint'])
    actual_plan = replace(plan, task_fingerprint=before['task_fingerprint'],
        original_fingerprint=before['original_fingerprint'],
        owned_definition_hash=before['owned_definition_hash'],
        definitions_hash=before['definitions_hash'])
    backtest = str(uuid4())

    class BacktestHost(FakeHost):
        def probe(self, captured, resources):
            value = super().probe(captured, resources)
            value.update(switcher.database_snapshot(engine, switcher.OWNED_TASK, captured))
            return value

        def stop(self, service):
            super().stop(service)
            target_gate = 1 if phase == 'normal' else 2
            if service == 'backend' and self.gates == target_gate:
                self.preserved_runner = copy.deepcopy(self.current['runner'])
                with engine.begin() as connection:
                    connection.execute(text('INSERT INTO backtest_runs (id) VALUES (:id)'),
                                       {'id': backtest})

    host = BacktestHost(actual_plan)
    host.fail_health = phase == 'recovery'
    code, events = execute(actual_plan, host)
    assert code == 2 and not any(e['stage'] == 'completed' for e in events)
    expected_stops = [('stop', 'backend')] if phase == 'normal' else [
        ('stop', 'backend'), ('stop', 'runner'), ('stop', 'backend')]
    assert [c for c in host.calls if c[0] == 'stop'] == expected_stops
    assert host.states()['runner'] == host.preserved_runner
    assert [c for c in host.calls if c[0] == 'start' and c[1] == actual_plan.old_image] == [
        ('start', actual_plan.old_image, ('backend',))]
    assert host.gates == (1 if phase == 'normal' else 2)
    assert events[-1]['guarded'] == (phase == 'normal')
    after = switcher.database_snapshot(engine, switcher.OWNED_TASK, [queued])
    assert after['activity']['backtests'] == 1
    assert after['definitions_hash'] == before['definitions_hash']
    assert after['records'] == before['records']
    with engine.connect() as connection:
        assert connection.execute(text('SELECT id::text FROM backtest_runs WHERE finished_at IS NULL')).scalar_one() == backtest


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
        assert ready['clock_domain'] == switcher.clock_domain()
        assert switcher.zero_offset_clock(ready['clock_domain']) == gate.host_clock_domain
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
