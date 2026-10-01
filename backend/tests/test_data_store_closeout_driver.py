"""Offline closeout scheduling regressions; no production paths or inputs."""
from copy import deepcopy

import pytest

from app.data_store.closeout import disposition, run


class Clock:
    def __init__(self):
        self.now = 100.
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 1
        self.sleeps.append(seconds)
        self.now += seconds


DONE = {'complete': True, 'qualified': True}


def test_backoff_keeps_job_until_due_and_does_not_starve_sibling():
    clock = Clock()
    calls = []
    saved = []
    def attempt(entry, seconds):
        calls.append((entry, clock(), seconds))
        if entry == 'A' and sum(c[0] == 'A' for c in calls) == 1:
            return {'reason': 'RETRY_BACKOFF', 'progress': False,
                    'refresh': {'next_retry_at': 105}}
        return DONE
    result = run(['A', 'B'], attempt, deadline=110, clock=clock, sleep=clock.sleep,
                 save=lambda state: saved.append(deepcopy(state)))
    assert [c[0] for c in calls] == ['A', 'B', 'A']
    assert calls[1][1] == 100 and calls[2][1] == 105
    assert result['jobs']['A']['attempts'] == 2
    assert all(j['status'] == 'done' for j in result['jobs'].values())
    assert result['processing_finished'] and not result['acceptance_complete']
    assert any(s['jobs']['A'].get('reason') == 'RETRY_BACKOFF' for s in saved)


def test_backoff_beyond_deadline_stays_pending_without_unbounded_wait():
    clock = Clock()
    calls = []
    def attempt(entry, seconds):
        calls.append(entry)
        return {'reason': 'RETRY_BACKOFF', 'refresh': {'next_retry_at': 10000}}
    result = run(['A'], attempt, deadline=103, clock=clock, sleep=clock.sleep)
    assert calls == ['A'] and clock() == 103
    assert result['jobs']['A']['status'] == 'pending'
    assert result['stop_reason'] == 'CLOSEOUT_DEADLINE_REACHED'


def test_due_backoff_does_not_spin_and_wait_does_not_consume_attempts():
    clock = Clock()
    calls = []
    def attempt(entry, seconds):
        calls.append(clock())
        return {'reason': 'RETRY_BACKOFF', 'refresh': {'next_retry_at': 99}}
    result = run(['A'], attempt, deadline=120, max_attempts=3, clock=clock, sleep=clock.sleep)
    assert calls == [100, 101, 102]
    assert result['jobs']['A']['status'] == 'limited'


@pytest.mark.parametrize('reason', ['OPERATION_CANCELLED', 'DISK_PRESSURE',
                                   'COMMIT_UNKNOWN', 'UNEXPECTED_ERROR'])
def test_real_cancel_or_global_failure_stops_even_after_progress(reason):
    calls = []
    def attempt(entry, seconds):
        calls.append(entry)
        return {'reason': reason, 'committed_partitions': 12, 'wall_budget_reached': True}
    result = run(['A', 'B'], attempt, deadline=110, clock=lambda: 100)
    assert calls == ['A'] and result['stop_reason'] == reason
    assert not result['processing_finished'] and not result['acceptance_complete']


def test_cooperative_budget_is_continuation_not_failed_retirement():
    clock = Clock()
    calls = []
    def attempt(entry, seconds):
        calls.append(entry)
        return {'reason': 'LOCAL_UPDATE_BUDGET_EXCEEDED'} if len(calls) == 1 else DONE
    result = run(['A'], attempt, deadline=110, clock=clock, sleep=clock.sleep)
    assert calls == ['A', 'A'] and result['jobs']['A']['status'] == 'done'


def test_entry_failure_is_retained_without_blind_retry_and_sibling_runs():
    calls = []
    def attempt(entry, seconds):
        calls.append(entry)
        return {'reason': 'SOURCE_CONFLICT'} if entry == 'A' else DONE
    result = run(['A', 'B'], attempt, deadline=110, clock=lambda: 100)
    assert calls == ['A', 'B']
    assert result['jobs']['A']['status'] == 'blocked'
    assert not result['acceptance_complete']


@pytest.mark.parametrize('value', [None, True, '123', float('inf'), -1])
def test_malformed_backoff_does_not_schedule(value):
    assert disposition({'reason': 'RETRY_BACKOFF', 'refresh': {'next_retry_at': value}},
                       now=100, progressed=False) == ('blocked', None)


def test_unqualified_completion_is_not_done():
    assert disposition({'complete': True, 'qualified': False}, now=100,
                       progressed=True) == ('blocked', None)


def test_progress_continuations_remain_fair_and_finitely_bounded():
    calls = []
    def attempt(entry, seconds):
        calls.append(entry)
        return {'reason': 'NATIVE_RANGES_PENDING', 'committed_partitions': 1}
    result = run(['A', 'B'], attempt, deadline=200, max_attempts=2, clock=lambda: 100)
    assert calls == ['A', 'B', 'A', 'B']
    assert all(j['status'] == 'limited' for j in result['jobs'].values())


def test_resume_keeps_original_deadline_scope_attempts_and_due_time():
    saved = []
    def interrupted(entry, seconds):
        raise RuntimeError('private-payload')
    with pytest.raises(RuntimeError):
        run(['A'], interrupted, deadline=120, max_attempts=2, clock=lambda: 100,
            save=lambda state: saved.append(deepcopy(state)))
    state = saved[-1]
    assert state['jobs']['A']['attempts'] == 1
    for kwargs in ({'deadline': 121}, {'entries': ['B']}, {'max_attempts': 3}, {'pass_seconds': 800}):
        config = dict(entries=['A'], deadline=120, max_attempts=2)
        config.update(kwargs)
        with pytest.raises(ValueError):
            run(attempt=lambda *_: DONE, state=deepcopy(state), clock=lambda: 101, **config)
    resumed = run(['A'], lambda *_: DONE, deadline=120, max_attempts=2,
                  state=state, clock=lambda: 101)
    assert resumed['jobs']['A']['attempts'] == 2 and resumed['deadline'] == 120
    assert 'private-payload' not in str(saved)


def test_budget_passed_to_attempt_is_less_than_original_remaining_time():
    values = []
    result = run(['A'], lambda _, seconds: values.append(seconds) or DONE,
                 deadline=107.9, clock=lambda: 100)
    assert values == [7] and result['processing_finished']


def test_cancel_during_backoff_is_responsive_and_never_invokes_sibling():
    clock = Clock()
    result = run(['A'], lambda *_: {'reason': 'RETRY_BACKOFF', 'refresh': {'next_retry_at': 10000}},
                 deadline=200, clock=clock, sleep=clock.sleep, cancelled=lambda: clock() >= 102)
    assert result['stop_reason'] == 'OPERATION_CANCELLED' and clock() == 102


def test_cli_rejects_unbounded_deadline_before_database_access(tmp_path):
    from app.data_store.closeout import main
    with pytest.raises(SystemExit):
        main(['--root', str(tmp_path), '--state', str(tmp_path / 'state.json'),
              '--entry', 'E50', '--deadline', '2026-10-01T10:00:00'])
