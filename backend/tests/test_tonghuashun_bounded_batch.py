"""Real subprocess deadlines plus synthetic, supplier-free batch boundaries."""
from datetime import date
import json
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from app.data_ingestion.tonghuashun import bounded, bounded_batch


def scopes(count=3):
    return [bounded.Scope('index_daily', f'90000{i}CNY01.SH', date(2026, 9, 1), date(2026, 9, 3))
            for i in range(count)]


def complete(scope, output, *, _deadline):
    # A validated child-shaped fixture, not a supplier observation or receipt.
    return dict(operation_id='fixture', outcome='failed', process_exited=True,
        unknown_publication=False, counts_complete=True,
        counts={'logical_requests': 1, 'http_attempts': 1, 'reused': 0},
        elapsed_seconds=0, exit_code=2)


@pytest.mark.parametrize('clock_origin', [None, 575.613700261])
def test_every_scope_receives_the_same_absolute_batch_deadline(tmp_path, monkeypatch, clock_origin):
    if clock_origin is not None:
        # This real CI origin crosses a floating-point precision boundary when
        # adding the wall allowance. Keep the clock local to the batch module.
        monkeypatch.setattr(bounded_batch, 'time', SimpleNamespace(monotonic=lambda: clock_origin))
    received = []
    def attempt(*args, **kwargs):
        received.append(kwargs['_deadline'])
        return complete(*args, **kwargs)
    result = bounded_batch.run(scopes(), tmp_path / 'batch', _supervise=attempt)
    assert len(received) == 3 and len(set(received)) == 1
    assert received[0] == result['deadline_monotonic']
    # Compare absolute deadlines; subtracting rounded floats can differ by an
    # ULP even when the fixed allowance and cleanup reserve are both correct.
    assert result['wall_deadline_monotonic'] == result['started_monotonic'] + 540
    assert result['deadline_monotonic'] == result['wall_deadline_monotonic'] - 5
    assert result['outcome'] == 'completed' and result['unattempted_scopes'] == 0
    assert not result['resources_verified']
    assert result['process_wall_budget_met']


def test_inter_scope_gap_consumes_wall_and_prevents_second_attempt(tmp_path):
    calls = []
    def attempt(*args, **kwargs):
        calls.append(1)
        return complete(*args, **kwargs)
    result = bounded_batch.run(scopes(), tmp_path / 'batch', wall_seconds=1,
        gap_seconds=1.2, _supervise=attempt)
    assert calls == [1] and result['outcome'] == 'timed_out'
    assert result['unattempted_scopes'] == 2
    assert .9 <= result['elapsed_seconds'] < 1
    assert result['process_wall_budget_met']


@pytest.mark.parametrize('changed', ['unknown', 'in_flight', 'unknown_counts', 'overspend'])
def test_unknown_resources_or_counts_stop_without_retry_or_next_scope(tmp_path, changed):
    calls = []
    def attempt(*args, **kwargs):
        calls.append(1)
        result = complete(*args, **kwargs)
        if changed == 'unknown': result['unknown_publication'] = True
        elif changed == 'in_flight': result['process_exited'] = False
        elif changed == 'unknown_counts': result['counts_complete'] = False
        else: result['counts']['http_attempts'] = 4
        return result
    result = bounded_batch.run(scopes(), tmp_path / 'batch', _supervise=attempt)
    assert calls == [1] and result['unattempted_scopes'] == 2
    assert result['outcome'] in ('verification_required', 'request_budget_violation')


def test_short_remaining_batch_deadline_kills_and_reaps_owned_process(tmp_path):
    children = []
    def popen(command, **kwargs):
        # Inherit ignored SIGTERM through exec to remove the interpreter-start
        # race. This fixture must exercise owned SIGKILL and verified waitpid.
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
            **kwargs, preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))
        children.append(child)
        return child
    def attempt(scope, output, *, _deadline):
        return bounded.supervise(scope, output, _popen=popen, _deadline=_deadline)
    result = bounded_batch.run(scopes(), tmp_path / 'batch', wall_seconds=1.5, _supervise=attempt)
    assert len(children) == 1 and children[0].poll() == -signal.SIGKILL
    child_result = json.loads((tmp_path / 'batch/scope-01/result.json').read_text())
    assert child_result['process_exited'] and child_result['kill_sent']
    assert child_result['outcome'] == 'timed_out'
    assert result['outcome'] == 'verification_required'  # Publication remains honestly unknown.
    assert result['elapsed_seconds'] < 1.5
    assert result['process_wall_budget_met']


@pytest.mark.parametrize('values', [[], scopes(4), scopes(1) * 2])
def test_invalid_or_duplicate_scopes_never_start_child(tmp_path, values):
    with pytest.raises(ValueError):
        bounded_batch.run(values, tmp_path / 'batch', _supervise=lambda *_: pytest.fail('child started'))
    assert not (tmp_path / 'batch').exists()


def test_expired_containing_deadline_never_starts_one_scope(tmp_path):
    result = bounded.supervise(scopes(1)[0], tmp_path / 'scope', _deadline=time.monotonic() - 1,
        _popen=lambda *args, **kwargs: pytest.fail('expired deadline started child'))
    assert not result['process_started'] and result['outcome'] == 'timed_out'


def test_supervision_exception_keeps_started_attempt_counts_unknown(tmp_path):
    def failure(*args, **kwargs):
        raise OSError('invented fixture IO failure')
    result = bounded_batch.run(scopes(), tmp_path / 'batch', _supervise=failure)
    assert result['outcome'] == 'supervisor_error' and result['attempts_started'] == 1
    assert result['unattempted_scopes'] == 2 and not result['counts_complete']
    assert all(value is None for value in result['counts'].values())
