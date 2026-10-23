"""Finite continuation of existing local work; never collect or bless data.

The caller supplies an absolute original deadline. A temporary scheduler backoff
remains pending until its recorded due time, independently of cursor progress.
Actual cancellation and storage-wide failures stop this driver. All source and
quality gates remain owned by the existing update/coverage/audit implementations.
"""
from __future__ import annotations

import math
import time

from .updates import FATAL


CONTINUATIONS = frozenset({
    'PASS_BUDGET_EXCEEDED', 'NATIVE_RANGES_PENDING', 'LOCAL_UPDATE_BUDGET_EXCEEDED',
})
TERMINAL_REASONS = FATAL | frozenset({
    'UNEXPECTED_ERROR', 'CLOSEOUT_DEADLINE_REACHED', 'CLOSEOUT_PROCESSING_FINISHED',
})


def terminal_reason(state):
    """Retain terminal decisions even if the owner died just before release."""
    if state is None:
        return None
    reason = state.get('stop_reason')
    if reason in TERMINAL_REASONS:
        return reason
    # The fatal attempt result is checkpointed immediately before stop_reason.
    # A crash in that interval must not convert failure into another attempt.
    return next((job.get('reason') for job in state['jobs'].values()
                 if job['status'] == 'pending'
                 and job.get('reason') in FATAL | {'UNEXPECTED_ERROR'}), None)


def validate_state(state, *, entries, deadline, max_attempts, pass_seconds):
    """Validate a checkpoint without repairing, resetting or accepting it."""
    if (type(state) is not dict or type(state.get('version')) is not int
            or state['version'] != 1 or state.get('deadline') != deadline
            or type(state.get('deadline')) not in (int, float)
            or state.get('max_attempts') != max_attempts
            or type(state.get('max_attempts')) is not int
            or state.get('pass_seconds') != pass_seconds
            or type(state.get('pass_seconds')) is not int
            or state.get('entries') != list(entries)
            or type(state.get('jobs')) is not dict
            or set(state['jobs']) != set(entries)
            or type(state.get('processing_finished')) is not bool
            or state.get('acceptance_complete') is not False
            or state.get('supplier_collection_executed') is not False):
        raise ValueError('Resume must retain the original scope and budgets')
    for job in state['jobs'].values():
        if (type(job) is not dict
                or job.get('status') not in ('pending', 'running', 'done', 'blocked', 'limited')
                or type(job.get('attempts')) is not int
                or not 0 <= job['attempts'] <= max_attempts
                or type(job.get('due')) not in (int, float)
                or not math.isfinite(job['due']) or job['due'] < 0
                or job['status'] == 'running' and job['attempts'] == 0
                or job['status'] in ('blocked', 'done') and job['attempts'] == 0
                or job['status'] == 'limited' and job['attempts'] != max_attempts
                or job['status'] == 'done' and (job.get('complete') is not True
                                              or job.get('qualified') is not True)
                or any(key in job and type(job[key]) is not bool for key in ('complete', 'qualified'))):
            raise ValueError('Invalid persisted job')
    if state['processing_finished'] and any(
            job['status'] in ('pending', 'running') for job in state['jobs'].values()):
        raise ValueError('Invalid completion checkpoint')


def disposition(result, *, now, progressed):
    """Return a bounded scheduling decision without copying arbitrary payloads."""
    reason = result.get('reason')
    if reason in FATAL or reason == 'UNEXPECTED_ERROR':
        return 'stop', None
    if result.get('complete') and result.get('qualified'):
        return 'done', None
    if reason == 'RETRY_BACKOFF':
        due = result.get('refresh', {}).get('next_retry_at')
        if type(due) not in (int, float) or not math.isfinite(due) or due <= 0:
            return 'blocked', None
        # Due or nearly due clocks still receive an idle interval, rather than
        # exhausting round limits in a tight loop or bypassing native backoff.
        return 'pending', max(now + 1, due)
    if reason in CONTINUATIONS:
        if progressed:
            return 'pending', now
        if reason == 'LOCAL_UPDATE_BUDGET_EXCEEDED':
            return 'pending', now + 1
    return 'blocked', None


def run(entries, attempt, *, deadline, max_attempts=128, pass_seconds=840,
        state=None, save=lambda _: None, cancelled=lambda: False,
        clock=time.time, sleep=time.sleep):
    """Fairly drain selected entries while preserving deadline and retry state.

    ``attempt`` must honor its positive per-call budget and use the ordinary
    update path, not forced retry. Its result may include ``progress`` only when
    backed by a file generation/acknowledged cursor change. ``save`` atomically
    persists progress after every transition. Resuming an interrupted attempt
    consumes its already-recorded attempt, preserving the finite bound.
    """
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or type(max_attempts) is not int or not 1 <= max_attempts <= 128
            or type(pass_seconds) is not int or not 1 <= pass_seconds <= 840
            or not entries or len(entries) != len(set(entries))):
        raise ValueError('Invalid finite closeout configuration')
    if state is None:
        state = {'version': 1, 'deadline': deadline, 'max_attempts': max_attempts,
                 'pass_seconds': pass_seconds, 'entries': list(entries),
                 'jobs': {entry: {'status': 'pending', 'attempts': 0, 'due': 0}
                          for entry in entries},
                 'processing_finished': False, 'acceptance_complete': False,
                 'supplier_collection_executed': False}
    validate_state(state, entries=entries, deadline=deadline, max_attempts=max_attempts,
                   pass_seconds=pass_seconds)
    final_reason = terminal_reason(state)
    if final_reason is not None:
        state['stop_reason'] = final_reason
        save(state)
        return state
    jobs = state['jobs']
    for job in jobs.values():
        if job['status'] == 'running':
            job['status'] = 'pending'
    state.update(processing_finished=False, acceptance_complete=False)
    save(state)
    while True:
        if cancelled():
            state['stop_reason'] = 'OPERATION_CANCELLED'
            break
        now = clock()
        if now >= deadline:
            state['stop_reason'] = 'CLOSEOUT_DEADLINE_REACHED'
            break
        pending = [entry for entry in entries if jobs[entry]['status'] == 'pending']
        if not pending:
            state['processing_finished'] = True
            state['stop_reason'] = 'CLOSEOUT_PROCESSING_FINISHED'
            break
        attempted = False
        for entry in pending:
            if cancelled() or clock() >= deadline:
                break
            job = jobs[entry]
            if job['attempts'] >= max_attempts:
                job.update(status='limited', reason='CLOSEOUT_ATTEMPT_LIMIT')
                save(state)
                continue
            if job['due'] > clock():
                continue
            remaining = int(deadline - clock())
            if remaining < 1:
                continue
            job.update(status='running', attempts=job['attempts'] + 1)
            save(state)
            attempted = True
            try:
                result = attempt(entry, min(pass_seconds, remaining))
            except BaseException:
                # Keep the entry and checkpoint available after process loss,
                # while preserving a truthful incomplete result. Never log the
                # exception text: database errors can contain private DSNs.
                job['status'] = 'pending'
                state['stop_reason'] = 'CLOSEOUT_ATTEMPT_INTERRUPTED'
                save(state)
                raise
            progressed = (result.get('progress') is True
                          or type(result.get('committed_partitions')) is int
                          and result['committed_partitions'] > 0)
            decision, due = disposition(result, now=clock(), progressed=progressed)
            job.update(status='pending' if decision == 'stop' else decision,
                       reason=result.get('reason'), due=due or 0,
                       complete=result.get('complete') is True,
                       qualified=result.get('qualified') is True)
            save(state)
            if decision == 'stop':
                state['stop_reason'] = result.get('reason')
                save(state)
                return state
        if not attempted and any(job['status'] == 'pending' for job in jobs.values()):
            # Short sleeps keep cancellation responsive. Waiting is not an
            # attempt and does not consume the per-entry round budget.
            earliest = min(job['due'] for job in jobs.values() if job['status'] == 'pending')
            sleep(max(0, min(1, deadline - clock(), max(.1, earliest - clock()))))
    save(state)
    return state


def main(argv=None):
    """Local-update driver for an explicitly adopted cooperative store only."""
    import argparse
    from datetime import datetime
    import json
    import os
    from pathlib import Path
    import signal
    from .errors import DataStoreError
    from .handoff import HandoffGate, validate_spec

    parser = argparse.ArgumentParser(description='Continue selected existing local work; no supplier requests')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--entry', action='append', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--handoff-epoch', required=True, help='Previously approved offline adoption UUID')
    parser.add_argument('--run-id', required=True, help='Immutable closeout UUID, retained on recovery')
    parser.add_argument('--deadline', required=True, help='Original absolute ISO-8601 deadline, with timezone')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--cancel', action='store_true', help='Recover and cancel an abandoned run; requires --resume')
    parser.add_argument('--max-attempts', type=int, default=128)
    parser.add_argument('--pass-seconds', type=int, default=840)
    parser.add_argument('--max-passes', type=int, default=4096)
    args = parser.parse_args(argv)
    from .adapters.registry import BY_ID
    from .pipeline import PipelineOptions
    try:
        deadline_date = datetime.fromisoformat(args.deadline)
        if deadline_date.tzinfo is None:
            raise ValueError('Deadline needs a timezone')
        deadline = deadline_date.timestamp()
        if (not args.root.is_absolute() or not args.state.is_absolute()
                or args.state != args.root / '.locks' / 'closeout-handoff.json'
                or len(args.entry) != len(set(args.entry))
                or any(not BY_ID[e].business for e in args.entry)
                or args.cancel and not args.resume
                or not 1 <= args.max_attempts <= 128 or not 1 <= args.pass_seconds <= 840):
            raise ValueError('Invalid scope or paths')
        PipelineOptions(mode='update', maximum_passes=args.max_passes, pass_seconds=args.pass_seconds)
        spec = {'entries': args.entry, 'deadline': deadline, 'max_attempts': args.max_attempts,
                'pass_seconds': args.pass_seconds, 'max_passes': args.max_passes}
        validate_spec(spec)
    except (ValueError, KeyError, OverflowError):
        parser.error('Use unique business entries, absolute existing paths and finite valid limits')
    key = os.environ.get('QF_CURSOR_SIGNING_KEY', '').encode()
    if len(key) < 32:
        parser.error('Provide QF_CURSOR_SIGNING_KEY through the existing environment')
    stopping = [False]
    prior_handlers = {}
    def stop(*_):
        stopping[0] = True
    try:
        with HandoffGate(args.root) as gate:
            for sig in (signal.SIGINT, signal.SIGTERM):
                prior_handlers[sig] = signal.signal(sig, stop)
            def work(session):
                def drive(attempt):
                    return run(args.entry, attempt, deadline=deadline,
                               max_attempts=args.max_attempts, pass_seconds=args.pass_seconds,
                               state=session.checkpoint, save=session.save,
                               cancelled=lambda: stopping[0] or args.cancel)
                # Recovery after expiry/cancellation touches only the journal.
                # It neither initializes the store nor sweeps scratch resources.
                if args.cancel or stopping[0] or time.time() >= deadline:
                    return drive(lambda *_: None)
                from app.db.session import get_engine
                from .storage import CurrentStore
                from .local_sources import NativeSources, SourceLimits
                from .updates import RetryPolicy, run_local
                from .adapters.canonical import NativeInputError
                engine = get_engine()
                with CurrentStore(engine, args.root, cursor_key=key) as store:
                    def attempt(eid, seconds):
                        options = PipelineOptions(mode='update', maximum_passes=args.max_passes,
                                                  pass_seconds=seconds)
                        sources = NativeSources(engine, limits=SourceLimits(pass_seconds=seconds),
                                                cancelled=lambda: stopping[0])
                        try:
                            rows = run_local(store, sources, entries=[BY_ID[eid]], options=options,
                                             cancelled=lambda: stopping[0],
                                             policy=RetryPolicy(call_seconds=seconds))
                        except (DataStoreError, NativeInputError) as error:
                            rows = getattr(error, 'results', [])
                            if not rows:
                                return {'reason': error.code, 'complete': False, 'qualified': False}
                        return next(row for row in rows if row['entry_id'] == eid)
                    return drive(attempt)
                # CurrentStore and all per-attempt contexts are closed before
                # gate.execute records release and permits scheduler admission.
            result = gate.execute(args.handoff_epoch, args.run_id, spec, work, resume=args.resume)
        # A drained queue is never a substitute for independent full coverage,
        # formal reads and audit-export. This driver intentionally claims none.
        print(json.dumps({'processing_finished': result['processing_finished'],
                          'acceptance_complete': False, 'stop_reason': result['stop_reason'],
                          'jobs': result['jobs']}, ensure_ascii=False))
        return (130 if stopping[0] or result['stop_reason'] == 'OPERATION_CANCELLED' else
                0 if all(j['status'] == 'done' for j in result['jobs'].values()) else 2)
    except Exception as error:
        print(json.dumps({'processing_finished': False, 'acceptance_complete': False,
                          'reason': (error.code if isinstance(error, DataStoreError) else
                                     'CLOSEOUT_CONFIGURATION_OR_EXECUTION_ERROR')}))
        return 2
    finally:
        for sig, handler in prior_handlers.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
