"""Offline protocol tests; synthetic participants, no process signals or data.

The isolated flock tests inject only the filesystem probe, as in the existing
locking tests. Real flock, guarded paths, process identities and fsync calls
remain active. This does not establish power-loss durability on overlayfs.
"""
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.data_store import handoff, locking
from app.data_store.closeout import main, run
from app.data_store.errors import DataStoreError

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux /proc and flock protocol')
EPOCH = '10000000-0000-4000-8000-000000000001'
RUN_ID = '10000000-0000-4000-8000-000000000002'
SPEC = {'entries': ['E50', 'E51'], 'deadline': 1000, 'max_attempts': 2,
        'pass_seconds': 840, 'max_passes': 1}
DONE = {'complete': True, 'qualified': True}


@contextmanager
def gate_at(root):
    with patch.object(locking, '_filesystem_name', return_value='ext4'):
        with handoff.HandoffGate(root) as gate:
            yield gate


@pytest.fixture
def root(tmp_path):
    root = tmp_path / 'store'
    root.mkdir(mode=0o700)
    (root / '.locks').mkdir(mode=0o700)
    (root / '.store-id').write_text(str(uuid4()))
    with gate_at(root) as gate:
        gate.adopt(EPOCH)
    return root


def journal(root):
    value = json.loads((root / '.locks' / handoff.JOURNAL).read_text())
    value.pop('digest')
    return value


def drive(session, *, spec=SPEC, attempt=lambda *_: DONE, cancelled=lambda: False, now=100):
    return run(spec['entries'], attempt, deadline=spec['deadline'],
               max_attempts=spec['max_attempts'], pass_seconds=spec['pass_seconds'],
               state=session.checkpoint, save=session.save, cancelled=cancelled,
               clock=lambda: now)


def code(expected, call):
    with pytest.raises(DataStoreError) as error:
        call()
    assert error.value.code == expected


def admit(gate, epoch=EPOCH):
    with gate.scheduler(epoch):
        pass


def abandon(gate, *, spec=SPEC, stage='attempt'):
    def work(session):
        if stage == 'claim':
            raise RuntimeError('synthetic interruption')
        def failure(*_):
            raise RuntimeError('synthetic interruption')
        drive(session, spec=spec, attempt=failure)
    with pytest.raises(RuntimeError):
        gate.execute(EPOCH, RUN_ID, spec, work)


def test_adoption_is_explicit_once_and_has_no_store_or_quota_side_effects(root):
    with gate_at(root) as gate:
        code('HANDOFF_ALREADY_ADOPTED', lambda: gate.adopt(EPOCH))
        code('HANDOFF_UNSUPPORTED_OWNER', lambda: gate.adopt(EPOCH, execution_model='legacy'))
    assert set(p.name for p in root.iterdir()) == {'.store-id', '.locks'}
    assert journal(root)['phase'] == 'scheduler'


@pytest.mark.parametrize('model', ['legacy', 'signal-paused', 'subprocess', None])
def test_uncooperative_and_child_owners_rejected_before_any_work(root, model):
    calls = []
    with gate_at(root) as gate:
        code('HANDOFF_UNSUPPORTED_OWNER', lambda: gate.execute(
            EPOCH, RUN_ID, SPEC, lambda _: calls.append('work'), execution_model=model))
        code('HANDOFF_UNSUPPORTED_OWNER', lambda: gate.scheduler(EPOCH, execution_model=model).__enter__())
    assert not calls and journal(root)['run'] is None


def test_missing_adoption_never_creates_a_ticket_or_calls_work(root):
    (root / '.locks' / handoff.JOURNAL).unlink()
    (root / '.locks' / handoff.ADOPTION).unlink()
    with gate_at(root) as gate:
        code('HANDOFF_ADOPTION_REQUIRED', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive))
    assert not (root / '.locks' / handoff.JOURNAL).exists()


def test_missing_journal_after_adoption_never_falls_back_or_readopts(root):
    (root / '.locks' / handoff.JOURNAL).unlink()
    with gate_at(root) as gate:
        code('HANDOFF_STATE_INVALID', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive))
        code('HANDOFF_ALREADY_ADOPTED', lambda: gate.adopt(EPOCH))
    code('HANDOFF_EPOCH_REQUIRED', lambda: handoff.scheduler_admission(root).__enter__())


def test_adoption_interrupted_after_marker_remains_blocked(root):
    (root / '.locks' / handoff.JOURNAL).unlink()
    (root / '.locks' / handoff.ADOPTION).unlink()
    with gate_at(root) as gate:
        with patch.object(gate, '_write', side_effect=OSError('synthetic interruption')):
            with pytest.raises(OSError):
                gate.adopt(EPOCH)
        code('HANDOFF_STATE_INVALID', lambda: admit(gate))
        code('HANDOFF_ALREADY_ADOPTED', lambda: gate.adopt(EPOCH))
    code('HANDOFF_EPOCH_REQUIRED', lambda: handoff.scheduler_admission(root).__enter__())


def test_natural_scheduler_drain_and_next_admission_are_serialized(root):
    with gate_at(root) as first, gate_at(root) as second:
        with first.scheduler(EPOCH):
            code('LOCK_TIMEOUT', lambda: second.execute(EPOCH, RUN_ID, SPEC, drive))
        events = []
        @contextmanager
        def resources():
            events.append('acquired')
            yield
            code('LOCK_TIMEOUT', lambda: admit(second))
            assert journal(root)['phase'] == 'closeout'
            events.append('closed')
        def work(session):
            with resources():
                result = drive(session)
                code('LOCK_TIMEOUT', lambda: admit(second))
            return result
        result = first.execute(EPOCH, RUN_ID, SPEC, work)
        assert events == ['acquired', 'closed']
        assert result['processing_finished'] and not result['acceptance_complete']
        admit(second)
    assert journal(root)['phase'] == 'scheduler'


@pytest.mark.parametrize('stage', ['claim', 'attempt'])
def test_failure_leaves_fence_and_live_owner_cannot_be_recovered(root, stage):
    with gate_at(root) as gate:
        abandon(gate, stage=stage)
        saved = journal(root)
        code('HANDOFF_RECOVERY_REQUIRED', lambda: admit(gate))
        code('HANDOFF_OWNER_STILL_LIVE', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True))
    assert journal(root) == saved


def test_owner_observation_failure_never_releases_fence(root):
    with gate_at(root) as gate:
        abandon(gate)
        with patch.object(handoff, '_start_ticks', side_effect=PermissionError):
            code('HANDOFF_OWNER_UNVERIFIED', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True))
        code('HANDOFF_RECOVERY_REQUIRED', lambda: admit(gate))


def test_recovery_preserves_origin_scope_and_consumed_attempts(root):
    with gate_at(root) as gate:
        abandon(gate)
        original = journal(root)['run']
        assert original['checkpoint']['jobs']['E50']['attempts'] == 1
        with patch.object(handoff, 'process_dead', return_value=True):
            result = gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True)
        recovered = journal(root)['run']
        assert recovered['origin'] == original['origin']
        assert recovered['recoveries'] == 1
        assert recovered['spec'] == SPEC
        assert result['jobs']['E50']['attempts'] == 2
        assert result['jobs']['E51']['attempts'] == 1
        assert not result['acceptance_complete'] and not result['supplier_collection_executed']


@pytest.mark.parametrize('change', [
    {'entries': ['E51', 'E50']}, {'entries': ['E50']}, {'deadline': 1001},
    {'max_attempts': 3}, {'pass_seconds': 839}, {'max_passes': 2},
])
def test_recovery_cannot_change_original_scope_or_expand_budgets(root, change):
    with gate_at(root) as gate:
        abandon(gate)
        saved = journal(root)
        code('HANDOFF_RUN_MISMATCH', lambda: gate.execute(
            EPOCH, RUN_ID, dict(SPEC, **change), drive, resume=True))
        assert journal(root) == saved


def test_wrong_epoch_run_and_new_start_cannot_replace_existing_state(root):
    with gate_at(root) as gate:
        abandon(gate)
        saved = journal(root)
        code('HANDOFF_EPOCH_MISMATCH', lambda: gate.execute(str(uuid4()), RUN_ID, SPEC, drive, resume=True))
        code('HANDOFF_RUN_MISMATCH', lambda: gate.execute(EPOCH, str(uuid4()), SPEC, drive, resume=True))
        code('HANDOFF_RUN_MISMATCH', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive))
        assert journal(root) == saved


def test_terminal_run_never_replays_done_or_cancelled_attempts(root):
    with gate_at(root) as gate:
        result = gate.execute(EPOCH, RUN_ID, SPEC, lambda session: drive(session, cancelled=lambda: True))
        saved = journal(root)
        resumed = gate.execute(EPOCH, RUN_ID, SPEC, lambda _: pytest.fail('replayed terminal run'), resume=True)
        assert result == resumed
        assert result['stop_reason'] == 'OPERATION_CANCELLED'
        assert all(j['attempts'] == 0 for j in result['jobs'].values())
        assert journal(root) == saved


def test_cancellation_during_work_closes_resources_before_scheduler_admission(root):
    stopping = [False]
    with gate_at(root) as gate, gate_at(root) as other:
        def work(session):
            def attempt(*_):
                stopping[0] = True
                code('LOCK_TIMEOUT', lambda: admit(other))
                return DONE
            return drive(session, attempt=attempt, cancelled=lambda: stopping[0])
        result = gate.execute(EPOCH, RUN_ID, SPEC, work)
        assert result['stop_reason'] == 'OPERATION_CANCELLED'
        assert result['jobs']['E50']['attempts'] == 1 and result['jobs']['E51']['attempts'] == 0
        admit(other)


def test_recovery_limit_cannot_be_reset_or_replaced(root):
    with gate_at(root) as gate:
        abandon(gate)
        saved = journal(root)
        with patch.object(handoff, 'MAX_RECOVERIES', 0), \
                patch.object(handoff, 'process_dead', return_value=True):
            code('HANDOFF_RECOVERY_LIMIT', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True))
        assert journal(root) == saved


@pytest.mark.parametrize('cancel', [False, True])
def test_abandoned_recovery_after_deadline_or_cancel_never_calls_attempt(root, cancel):
    with gate_at(root) as gate:
        abandon(gate)
        with patch.object(handoff, 'process_dead', return_value=True):
            result = gate.execute(EPOCH, RUN_ID, SPEC, lambda session: drive(
                session, now=1001, cancelled=lambda: cancel,
                attempt=lambda *_: pytest.fail('expired/cancelled work')), resume=True)
        assert result['deadline'] == SPEC['deadline']
        assert result['jobs']['E50']['attempts'] == 1
        assert result['stop_reason'] == ('OPERATION_CANCELLED' if cancel else 'CLOSEOUT_DEADLINE_REACHED')
        admit(gate)


def test_deadline_is_not_a_lease_and_does_not_admit_scheduler_during_live_work(root):
    expired = dict(SPEC, deadline=99)
    with gate_at(root) as gate, gate_at(root) as other:
        def work(session):
            code('LOCK_TIMEOUT', lambda: admit(other))
            assert journal(root)['phase'] == 'closeout'
            return drive(session, spec=expired)
        result = gate.execute(EPOCH, RUN_ID, expired, work)
        assert result['stop_reason'] == 'CLOSEOUT_DEADLINE_REACHED'
        admit(other)


def test_child_failure_and_fatal_update_do_not_claim_acceptance(root):
    with gate_at(root) as gate:
        result = gate.execute(EPOCH, RUN_ID, SPEC, lambda session: drive(
            session, attempt=lambda *_: {'reason': 'COMMIT_UNKNOWN'}))
        assert result['stop_reason'] == 'COMMIT_UNKNOWN'
        assert not result['processing_finished'] and not result['acceptance_complete']
        assert result['jobs']['E51']['attempts'] == 0
        # A returned ordinary-update failure is finalized only after resources
        # unwind. Detached child failures have no supported admission model.
        admit(gate)


def test_checkpoint_before_attempt_can_survive_without_reusing_attempt_number(root):
    with gate_at(root) as gate:
        def interrupted(session):
            def save(state):
                session.save(state)
                if any(j['status'] == 'running' for j in state['jobs'].values()):
                    raise RuntimeError('before callback')
            run(SPEC['entries'], lambda *_: pytest.fail('attempt was not admitted'),
                deadline=SPEC['deadline'], max_attempts=2, state=session.checkpoint,
                save=save, clock=lambda: 100)
        with pytest.raises(RuntimeError):
            gate.execute(EPOCH, RUN_ID, SPEC, interrupted)
        assert journal(root)['run']['checkpoint']['jobs']['E50']['status'] == 'running'
        with patch.object(handoff, 'process_dead', return_value=True):
            result = gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True)
        assert result['jobs']['E50']['attempts'] == 2


def test_no_replay_after_committed_checkpoint_before_release(root):
    with gate_at(root) as gate:
        def interrupted(session):
            drive(session)
            raise RuntimeError('before release')
        with pytest.raises(RuntimeError):
            gate.execute(EPOCH, RUN_ID, SPEC, interrupted)
        with patch.object(handoff, 'process_dead', return_value=True):
            result = gate.execute(EPOCH, RUN_ID, SPEC, lambda session: drive(
                session, attempt=lambda *_: pytest.fail('committed checkpoint replayed')), resume=True)
        assert all(j['attempts'] == 1 for j in result['jobs'].values())


@pytest.mark.parametrize('outcome', ['cancelled', 'fatal', 'expired', 'finished'])
def test_terminal_decision_survives_crash_before_release_without_reentering_work(root, outcome):
    with gate_at(root) as gate:
        def interrupted(session):
            drive(session, cancelled=lambda: outcome == 'cancelled',
                  attempt=lambda *_: {'reason': 'COMMIT_UNKNOWN'} if outcome == 'fatal' else DONE,
                  now=1001 if outcome == 'expired' else 100)
            raise RuntimeError('after terminal checkpoint')
        with pytest.raises(RuntimeError):
            gate.execute(EPOCH, RUN_ID, SPEC, interrupted)
        saved = journal(root)['run']['checkpoint']
        with patch.object(handoff, 'process_dead', return_value=True):
            result = gate.execute(EPOCH, RUN_ID, SPEC,
                                  lambda _: pytest.fail('terminal work re-entered'), resume=True)
        assert result == saved
        assert not result['acceptance_complete']
        admit(gate)


def test_fatal_result_interrupted_before_stop_reason_never_retries(root):
    with gate_at(root) as gate:
        def interrupted(session):
            def save(state):
                session.save(state)
                if state['jobs']['E50'].get('reason') == 'COMMIT_UNKNOWN':
                    raise RuntimeError('before final stop_reason')
            run(SPEC['entries'], lambda *_: {'reason': 'COMMIT_UNKNOWN'},
                deadline=1000, max_attempts=2, save=save, clock=lambda: 100)
        with pytest.raises(RuntimeError):
            gate.execute(EPOCH, RUN_ID, SPEC, interrupted)
        saved = journal(root)['run']['checkpoint']
        assert 'stop_reason' not in saved
        with patch.object(handoff, 'process_dead', return_value=True):
            result = gate.execute(EPOCH, RUN_ID, SPEC,
                                  lambda _: pytest.fail('fatal attempt reissued'), resume=True)
        assert result['stop_reason'] == 'COMMIT_UNKNOWN'
        assert result['jobs']['E50']['attempts'] == 1 and result['jobs']['E51']['attempts'] == 0


def test_stale_checkpoint_and_escaped_session_cannot_advance_or_release(root):
    escaped = []
    with gate_at(root) as gate:
        def work(session):
            escaped.append(session)
            old = run(SPEC['entries'], lambda *_: DONE, deadline=SPEC['deadline'],
                      max_attempts=2, cancelled=lambda: True, clock=lambda: 100)
            result = drive(session)
            code('HANDOFF_CHECKPOINT_MISMATCH', lambda: session.save(old))
            return result
        gate.execute(EPOCH, RUN_ID, SPEC, work)
        code('LOCK_CONTEXT_EXPIRED', lambda: escaped[0].save(escaped[0].value['run']['checkpoint']))


def test_other_thread_cannot_use_active_ownership_session(root):
    with gate_at(root) as gate:
        failures = []
        def work(session):
            def other_thread():
                try:
                    session.check()
                except DataStoreError as error:
                    failures.append(error.code)
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            return drive(session)
        gate.execute(EPOCH, RUN_ID, SPEC, work)
        assert failures == ['LOCK_CONTEXT_EXPIRED']


def test_failed_write_before_attempt_fails_closed_and_next_recovery_uses_durable_state(root):
    with gate_at(root) as gate:
        original = gate._write
        def write(value, **kwargs):
            checkpoint = value['run']['checkpoint'] if value['run'] else None
            if checkpoint and any(j['status'] == 'running' for j in checkpoint['jobs'].values()):
                raise OSError('synthetic disk error')
            original(value, **kwargs)
        with patch.object(gate, '_write', side_effect=write), pytest.raises(OSError):
            gate.execute(EPOCH, RUN_ID, SPEC, lambda session: drive(
                session, attempt=lambda *_: pytest.fail('unsaved attempt executed')))
        assert journal(root)['phase'] == 'closeout'
        assert journal(root)['run']['checkpoint']['jobs']['E50']['attempts'] == 0
        code('HANDOFF_RECOVERY_REQUIRED', lambda: admit(gate))


def test_release_write_interruption_leaves_resources_closed_and_fence(root):
    with gate_at(root) as gate:
        original = gate._write
        def write(value, **kwargs):
            if value['phase'] == 'scheduler':
                raise OSError('synthetic release failure')
            original(value, **kwargs)
        with patch.object(gate, '_write', side_effect=write), pytest.raises(OSError):
            gate.execute(EPOCH, RUN_ID, SPEC, drive)
        assert journal(root)['phase'] == 'closeout'
        assert journal(root)['run']['checkpoint']['processing_finished']
        code('HANDOFF_RECOVERY_REQUIRED', lambda: admit(gate))


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(version=True),
    lambda value: value.update(protocol='legacy'),
    lambda value: value.update(phase='scheduler'),
    lambda value: value['root'].update(token=str(uuid4())),
    lambda value: value['run'].update(owner={'pid': 1}),
    lambda value: value['run'].update(recoveries=-1),
    lambda value: value['run']['checkpoint'].update(acceptance_complete=True),
    lambda value: value['run']['checkpoint'].update(supplier_collection_executed=True),
    lambda value: value['run']['checkpoint']['jobs']['E50'].update(attempts=3),
    lambda value: value['run']['checkpoint']['jobs']['E50'].update(due=float('nan')),
    lambda value: value['run']['checkpoint']['jobs']['E50'].update(status='done'),
])
def test_corrupt_or_inconsistent_state_never_admits_scheduler_or_work(root, mutation):
    with gate_at(root) as gate:
        abandon(gate)
        value = journal(root)
        mutation(value)
        try:
            from hashlib import sha256
            value['digest'] = sha256(handoff._encode(value)).hexdigest()
        except ValueError:
            value['digest'] = 'invalid-nonfinite-checksum'
        gate.state_path.write_text(json.dumps(value))
        code('HANDOFF_STATE_INVALID', lambda: admit(gate))
        code('HANDOFF_STATE_INVALID', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True))


@pytest.mark.parametrize('data', [b'{', b'[]', b'{"version":1,"version":1}', b'\xff', b'x' * 262_145],
                         ids=['torn', 'wrong-type', 'duplicate', 'encoding', 'oversized'])
def test_torn_duplicate_non_json_and_oversized_state_rejected(root, data):
    with gate_at(root) as gate:
        gate.state_path.write_bytes(data)
        code('HANDOFF_STATE_INVALID', lambda: admit(gate))


def test_valid_json_counter_corruption_is_detected_by_checksum(root):
    with gate_at(root) as gate:
        abandon(gate)
        value = json.loads(gate.state_path.read_text())
        value['run']['checkpoint']['jobs']['E50']['attempts'] = 0
        gate.state_path.write_text(json.dumps(value))
        code('HANDOFF_STATE_INVALID', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True))
        code('HANDOFF_STATE_INVALID', lambda: admit(gate))


def test_symlink_hardlink_and_public_permissions_do_not_redirect_journal(root, tmp_path):
    victim = tmp_path / 'victim'
    victim.write_text('must remain unchanged')
    with gate_at(root) as gate:
        gate.state_path.unlink()
        gate.state_path.symlink_to(victim)
        with pytest.raises((DataStoreError, OSError)):
            admit(gate)
        gate.state_path.unlink()
        os.link(victim, gate.state_path)
        code('UNSAFE_STORAGE_PATH', lambda: admit(gate))
        gate.state_path.unlink()
        # The permanent adoption marker prevents reinitialization after loss.
        gate._write({'version': 1, 'protocol': handoff.PROTOCOL, 'epoch': EPOCH,
                     'root': gate.binding, 'phase': 'scheduler', 'run': None}, creating=True)
        gate.state_path.chmod(0o644)
        code('UNSAFE_STORAGE_PATH', lambda: admit(gate))
    assert victim.read_text() == 'must remain unchanged'


def test_changed_root_and_lock_directory_cannot_form_second_namespace(root):
    with gate_at(root) as gate:
        previous = root.with_name('previous-store')
        root.rename(previous)
        root.mkdir(mode=0o700)
        code('UNSAFE_STORAGE_PATH', lambda: admit(gate))


def test_lock_directory_replacement_during_work_cannot_publish_or_release(root):
    with gate_at(root) as gate:
        def work(session):
            (root / '.locks').rename(root / 'previous-locks')
            (root / '.locks').mkdir(mode=0o700)
            code('UNSAFE_STORAGE_PATH', lambda: session.save({}))
            raise RuntimeError('directory replaced')
        with pytest.raises(RuntimeError):
            gate.execute(EPOCH, RUN_ID, SPEC, work)
    value = json.loads((root / 'previous-locks' / handoff.JOURNAL).read_text())
    assert value['phase'] == 'closeout'


@pytest.mark.parametrize('filesystem', ['overlay', 'tmpfs', 'nfs', 'unknown'])
def test_default_guards_reject_unsupported_filesystem(root, filesystem):
    before = journal(root)
    with patch.object(locking, '_filesystem_name', return_value=filesystem):
        code('UNSUPPORTED_FILESYSTEM', lambda: handoff.HandoffGate(root))
    assert journal(root) == before


def test_pid_reuse_and_reboot_are_exact_identity_absence_not_signals():
    owner = {'boot_id': EPOCH, 'pid': 42, 'start_ticks': 123,
             'pid_namespace': handoff._pid_namespace()}
    with patch.object(handoff, '_boot_id', return_value=EPOCH):
        with patch.object(handoff, '_start_ticks', return_value=123):
            assert not handoff.process_dead(owner)
        with patch.object(handoff, '_start_ticks', return_value=124):
            assert handoff.process_dead(owner)
        with patch.object(handoff, '_start_ticks', side_effect=FileNotFoundError):
            assert handoff.process_dead(owner)
        with patch.object(handoff, '_start_ticks', side_effect=ValueError):
            code('HANDOFF_OWNER_UNVERIFIED', lambda: handoff.process_dead(owner))
    with patch.object(handoff, '_boot_id', return_value=str(uuid4())):
        assert handoff.process_dead(owner)


def test_different_pid_namespace_cannot_supply_owner_absence_proof():
    owner = handoff.process_identity()
    with patch.object(handoff, '_pid_namespace', return_value=owner['pid_namespace'] + 1):
        code('HANDOFF_OWNER_UNVERIFIED', lambda: handoff.process_dead(owner))


def test_live_process_identity_is_not_dead():
    owner = handoff.process_identity()
    assert owner['pid'] == os.getpid()
    assert not handoff.process_dead(owner)


def crash_participant(root, stage, ready, finish):
    """Dedicated synthetic worker, exits itself; never signal other processes."""
    with gate_at(root) as gate:
        def work(session):
            if stage == 'attempt':
                def attempt(*_):
                    ready.set()
                    if not finish.wait(15):
                        os._exit(91)
                    os._exit(23)
                return drive(session, attempt=attempt)
            if stage == 'finished':
                drive(session)
            ready.set()
            if not finish.wait(15):
                os._exit(91)
            os._exit(23)
        gate.execute(EPOCH, RUN_ID, SPEC, work)


@pytest.mark.parametrize('stage', ['claim', 'attempt', 'finished'])
def test_worker_death_disconnect_and_missing_coordinator_leave_durable_fence(root, stage):
    ctx = multiprocessing.get_context('spawn')
    ready, finish = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=crash_participant, args=(str(root), stage, ready, finish))
    worker.start()
    try:
        assert ready.wait(15), 'Synthetic worker did not claim admission'
        with gate_at(root) as gate:
            code('LOCK_TIMEOUT', lambda: admit(gate))
            code('LOCK_TIMEOUT', lambda: gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True))
        # There is no connection/coordinator lifetime lease. The worker owns the
        # gate directly until death, and its journal fence outlives that death.
        finish.set()
        worker.join(15)
        assert worker.exitcode == 23
        before = journal(root)
        with gate_at(root) as gate:
            code('HANDOFF_RECOVERY_REQUIRED', lambda: admit(gate))
            result = gate.execute(EPOCH, RUN_ID, SPEC, drive, resume=True)
            assert journal(root)['run']['origin'] == before['run']['origin']
            assert journal(root)['run']['owner'] == handoff.process_identity()
            assert result['jobs']['E50']['attempts'] == (2 if stage == 'attempt' else 1)
            admit(gate)
    finally:
        finish.set()
        worker.join(20)
        assert not worker.is_alive()


def test_scheduler_task_admission_requires_epoch_once_adopted(root):
    with patch.object(locking, '_filesystem_name', return_value='ext4'):
        code('HANDOFF_EPOCH_REQUIRED', lambda: handoff.scheduler_admission(root).__enter__())
        with handoff.scheduler_admission(root, EPOCH):
            pass
        with gate_at(root) as gate:
            abandon(gate)
        code('HANDOFF_RECOVERY_REQUIRED', lambda: handoff.scheduler_admission(root, EPOCH).__enter__())


def test_registered_scheduler_handler_is_inside_gate_and_cannot_bypass_it(root, monkeypatch):
    from types import SimpleNamespace
    # The registry is the application's canonical task-registration entrypoint.
    from app.scheduling import registry
    from app.data_store import scheduler_tasks as tasks
    monkeypatch.setattr(tasks, 'get_settings', lambda: SimpleNamespace(data_store_root=root))
    calls = []
    def handler(*_):
        with gate_at(root) as other:
            code('LOCK_TIMEOUT', lambda: other.execute(EPOCH, RUN_ID, SPEC, drive))
        calls.append('handler')
        return {'complete': True}
    monkeypatch.setattr(tasks, '_update_local', handler)
    parameters = tasks.LocalUpdateParameters(handoff_epoch=EPOCH)
    with patch.object(locking, '_filesystem_name', return_value='ext4'):
        assert tasks.update_local(None, parameters) == {'complete': True}
        code('HANDOFF_EPOCH_REQUIRED', lambda: tasks.update_local(None, tasks.LocalUpdateParameters()))
        with gate_at(root) as gate:
            abandon(gate)
        code('HANDOFF_RECOVERY_REQUIRED', lambda: tasks.update_local(None, parameters))
    assert calls == ['handler']
    with pytest.raises(ValueError):
        tasks.LocalUpdateParameters(handoff_epoch=EPOCH, pass_seconds=841)


def test_cli_expiry_and_cancellation_need_no_database_or_supplier_access(root, monkeypatch, capsys):
    monkeypatch.setenv('QF_CURSOR_SIGNING_KEY', 'synthetic-test-key-with-thirty-two-characters')
    args = ['--root', str(root), '--state', str(root / '.locks' / handoff.JOURNAL),
            '--entry', 'E50', '--entry', 'E51', '--deadline', '1970-01-01T00:16:40+00:00',
            '--max-attempts', '2', '--max-passes', '1', '--handoff-epoch', EPOCH, '--run-id', RUN_ID]
    with patch.object(locking, '_filesystem_name', return_value='ext4'):
        assert main(args) == 2
        report = json.loads(capsys.readouterr().out)
        assert report['stop_reason'] == 'CLOSEOUT_DEADLINE_REACHED'
        saved = journal(root)
        assert main(args + ['--resume']) == 2
        capsys.readouterr()
        assert journal(root) == saved


def test_cli_abandoned_cancellation_releases_fence_without_store_access(root, monkeypatch, capsys):
    monkeypatch.setenv('QF_CURSOR_SIGNING_KEY', 'synthetic-test-key-with-thirty-two-characters')
    with gate_at(root) as gate:
        abandon(gate)
    args = ['--root', str(root), '--state', str(root / '.locks' / handoff.JOURNAL),
            '--entry', 'E50', '--entry', 'E51', '--deadline', '1970-01-01T00:16:40+00:00',
            '--max-attempts', '2', '--max-passes', '1', '--handoff-epoch', EPOCH, '--run-id', RUN_ID,
            '--resume', '--cancel']
    with patch.object(locking, '_filesystem_name', return_value='ext4'), \
            patch.object(handoff, 'process_dead', return_value=True):
        assert main(args) == 130
    assert json.loads(capsys.readouterr().out)['stop_reason'] == 'OPERATION_CANCELLED'
    assert journal(root)['phase'] == 'scheduler'


def test_cli_rejects_detached_or_stale_snapshot_state_paths_before_storage(root):
    with pytest.raises(SystemExit):
        main(['--root', str(root), '--state', str(root / 'old-snapshot.json'),
              '--entry', 'E50', '--deadline', '1970-01-01T00:16:40+00:00',
              '--handoff-epoch', EPOCH, '--run-id', RUN_ID])
