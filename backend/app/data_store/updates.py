"""Finite entry-local refresh scheduling; no shared campaign completion gate."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import json
import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .adapters.canonical import NativeInputError
from .adapters.registry import BY_ID, ENTRIES
from .errors import DataStoreError
from .locking import _deadline
from .values import control_json


@dataclass(frozen=True)
class RetryPolicy:
    initial_seconds: int = 30
    maximum_seconds: int = 1800
    abandon_after: int = 3
    call_seconds: int = 900

    def __post_init__(self):
        if any(type(v) is not int or v<1 for v in vars(self).values()):
            raise ValueError('Positive finite retry limits required')


FATAL = frozenset({'OPERATION_CANCELLED','CATALOG_UNAVAILABLE','CATALOG_MISMATCH',
    'DATA_STORE_NOT_INITIALIZED','DATA_STORE_REBUILDING','DISK_PRESSURE',
    'UNSAFE_STORAGE_PATH','UNSUPPORTED_FILESYSTEM','STORAGE_UNAVAILABLE','MEMORY_PRESSURE',
    'COMMIT_UNKNOWN','GARBAGE_BUDGET_EXCEEDED'})
COUNTERS = ('passes','committed_partitions','source_rows','normalized_units','new_issues',
            'resolved','input_failures')


def _read(store, entry):
    with store.catalog.transaction() as c:
        raw=c.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i'),
                      {'i':entry.id}).scalar_one_or_none()
    return json.loads(raw) if raw else {'entry_id':entry.id}


def _refresh(store, entry, patch, *, operation=None):
    """Only update our namespace from a freshly locked row, never coverage."""
    with store.catalog.transaction() as c:
        raw=c.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i FOR UPDATE'),
                      {'i':entry.id}).scalar_one_or_none()
        status=json.loads(raw) if raw else {'entry_id':entry.id}
        if operation:status.update(operation)
        state=dict(status.get('refresh',{}));state.update(patch);status['refresh']=state
        c.execute(text('INSERT INTO data_store_entry_status(entry_id,summary_json) VALUES (:i,:j) '
                       'ON CONFLICT(entry_id) DO UPDATE SET summary_json=EXCLUDED.summary_json, '
                       'updated_at=clock_timestamp()'), {'i':entry.id,'j':control_json(status)})
    return state


def _skipped(status, outcome, reason):
    # These are this invocation's counts, never a cached successful refresh.
    return dict(status, **{k:0 for k in COUNTERS}, metrics={}, attempted=False,
                outcome=outcome, state=outcome, reason=reason, complete=False, qualified=False,
                source_scanned=False)


def _code(error):
    if isinstance(error,(DataStoreError,NativeInputError)):return error.code
    if isinstance(error,SQLAlchemyError):return 'CATALOG_UNAVAILABLE'
    if isinstance(error,OSError):
        return 'DISK_PRESSURE' if error.errno in (errno.ENOSPC,errno.EDQUOT) else 'STORAGE_UNAVAILABLE'
    if isinstance(error,(KeyboardInterrupt,SystemExit)):return 'OPERATION_CANCELLED'
    return 'UNEXPECTED_ERROR'


def run_entry(store,entry,sources,*,options,cancelled=None,_policy=None,clock=None):
    from .pipeline import _run_entry_locked
    clock=clock or time.time
    policy=_policy or RetryPolicy()
    dataset=entry.spec.name if entry.business else 'local.entry.'+entry.id.lower()
    with store.locks._hold(dataset,'pipeline',fcntl.LOCK_EX,_deadline(store.limits.lock_timeout_ms),cancelled):
        old=_read(store,entry);previous=old.get('refresh',{});now=int(clock())
        admitted=None;restored=None
        if options.e50_input_fence is not None:
            from .incremental import validate_e50_fence
            # Refuse a foreign contract, bootstrap or original before changing
            # refresh status. Claim repeats these checks under its row lock.
            validate_e50_fence(store,entry,sources,options.e50_input_fence)
        if options.admit_active_only:
            from .active_input import active_input_plan
            # Rejection must leave the existing status, queue and scratch
            # untouched, including pending/qualified and refresh counters.
            admitted=active_input_plan(store,entry,sources,options=options,cancelled=cancelled)
        if options.resume_sealed_only and entry.id in ('E23','E44'):
            from .sealed_funds import restore_fund_batch
            # A stale original member cannot mutate refresh/quality counters;
            # unrelated newly eligible claims remain outside this old batch.
            restored=restore_fund_batch(store,entry,sources,options,old,cancelled=cancelled)
        if (_policy is not None and options.mode!='retry' and previous.get('next_retry_at',0)>now):
            state=_refresh(store,entry,{'outcome':'backoff','attempted':False})
            return dict(_skipped(old,'backoff','RETRY_BACKOFF'),refresh=state)
        _refresh(store,entry,{'last_attempt_at':now,'attempted':True,'outcome':'running','deferred_since':0})
        error=None
        try:
            from .incremental import enabled, run as incremental_run
            # Drain an already sealed pre-upgrade acquisition before switching
            # to the range queue; its sole retained input must not be discarded.
            legacy_pending=('pipeline.'+entry.id in store.budget.pending_keys() and
                            not old.get('incremental') and old.get('scope')!='native_incremental')
            execute=incremental_run if enabled(sources,entry,options) and not legacy_pending else _run_entry_locked
            extra={'admitted':admitted} if options.admit_active_only else {}
            if restored is not None:extra['restored']=restored
            result=execute(store,entry,sources,options=options,cancelled=cancelled,**extra)
        except BaseException as caught:
            error=caught
            if getattr(caught,'preserve_current_status',False):
                # The failed invocation has zero admitted work. Its returned
                # counters describe this attempt, while the stored receipt and
                # pre-existing restrictions continue to describe current data.
                status=caught.entry_result
            else:
                try:
                    status=_read(store,entry)
                except Exception:
                    # Database failure must not replace a process exit/cancel signal.
                    status=_skipped(old,'failed',_code(caught))
            result=dict(status,state='incomplete',complete=False,qualified=False,reason=_code(caught))
        success=bool(result.get('complete') and result.get('qualified',not entry.business))
        continued=(error is None and not result.get('complete') or
                   result.get('reason')=='LOCAL_UPDATE_BUDGET_EXCEEDED')
        if not success and not continued and error is None:
            result['reason']=result.get('reason') or 'CURRENT_QUALITY_UNRESOLVED'
        outcome=('updated' if result.get('metrics',{}).get('files_written',0) else 'unchanged') if success else (
            'continued' if continued else 'failed')
        failures=0 if success or continued else min(32,previous.get('consecutive_failures',0)+1)
        progressed=(result.get('committed_partitions',0)>0 or
                    result.get('last_partition') and result.get('last_partition')!=old.get('last_partition'))
        stalls=min(32,previous.get('stalled_failures',0)+1) if failures and not progressed else 0
        patch={'outcome':outcome,'attempted':True,'consecutive_failures':failures,
               'stalled_failures':stalls,
               'next_retry_at':int(clock())+min(policy.maximum_seconds,policy.initial_seconds*2**(failures-1)) if failures else 0,
               'error_type':type(error).__name__ if error else None,
               'reason':result.get('reason') if not success else None}
        if success:patch['last_success_at']=int(clock())
        if success or continued:patch['continuation_aborted']=None
        if result.get('work_admitted'):patch['last_work_at']=now
        if result.get('source_scanned'):patch['last_source_scan_at']=now
        # A repeatedly failing sealed unit must not retain every shared slot
        # forever. Explicitly invalidate its pending coverage before releasing
        # only its idle scratch. Current files and committed checkpoints survive.
        if (_policy is not None and stalls>=policy.abandon_after and
                result.get('reason') not in FATAL and error is not None and
                not getattr(error,'preserve_current_status',False) and
                not getattr(error,'preserve_continuation',False) and
                isinstance(error,(DataStoreError,NativeInputError))):
            owner='pipeline.'+entry.id+('.selected' if options.partitions else '')
            if owner in store.budget.pending_keys():
                _refresh(store,entry,dict(patch,continuation_aborted='REPEATED_ENTRY_FAILURE'))
                store.budget.abandon(owner)
                patch['continuation_aborted']='REPEATED_ENTRY_FAILURE'
        try:
            state=_refresh(store,entry,patch,operation=(
                {'state':'incomplete','complete':False,'qualified':False,'reason':_code(error)}
                if error and not getattr(error,'preserve_current_status',False) else None))
        except Exception:
            if error is not None:
                error.entry_result=dict(result,outcome=outcome,attempted=True,refresh=patch)
                raise error
            raise
        result.update(outcome=outcome,attempted=True,refresh=state)
        if error is not None:
            error.entry_result=result
            raise error
        return result


def run_local(store,sources,*,entries,options,cancelled=None,policy=None,clock=None,monotonic=None):
    from .pipeline import run_entry as attempt
    policy=policy or RetryPolicy();clock=clock or time.time;monotonic=monotonic or time.monotonic
    deadline=monotonic()+policy.call_seconds
    selected=list(ENTRIES if entries is None else entries)
    if options.e50_input_fence is not None and [e.id for e in selected]!=['E50']:
        raise DataStoreError('INVALID_CONFIGURATION')
    if (options.resume_sealed_only or options.admit_active_only) and (len(selected)!=1 or not selected[0].business):
        # A single input fence belongs to one business entry. In particular,
        # ingestion-channel expansion must not admit an unapproved target.
        raise DataStoreError('INVALID_CONFIGURATION')
    if len(selected)>len(ENTRIES):raise ValueError('Too many source entries')
    selected=list({e.id:e for e in selected}.values())
    for entry in list(selected):
        if entry.disposition=='ingestion_channel' and entry.target and entry.target not in {e.id for e in selected}:
            selected.append(BY_ID[entry.target])
    pending=store.budget.pending_keys()
    statuses={entry.id:_read(store,entry) for entry in selected}
    # Drain sealed work before admitting scans, then prefer entries that have
    # never obtained a slot. A prior call-budget deferral takes first priority
    # so a slow continuation cannot consume every subsequent invocation too.
    selected.sort(key=lambda e:(not statuses[e.id].get('refresh',{}).get('deferred_since'),
                                'pipeline.'+e.id not in pending,
                                statuses[e.id].get('refresh',{}).get('last_work_at',0)))
    results=[]
    for entry in selected:
        try:
            if cancelled and cancelled():raise DataStoreError('OPERATION_CANCELLED')
            if monotonic()>=deadline:
                _refresh(store,entry,{'deferred_since':int(clock())})
                results.append(_skipped(_read(store,entry),'deferred','LOCAL_UPDATE_BUDGET_EXCEEDED'))
                continue
            entry_deadline=min(deadline,monotonic()+options.pass_seconds)
            def stop():
                if cancelled and cancelled():return True
                if monotonic()>=entry_deadline:raise DataStoreError('LOCAL_UPDATE_BUDGET_EXCEEDED')
                return False
            result=attempt(store,entry,sources,options=options,cancelled=stop,_policy=policy,clock=clock)
        except Exception as error:
            code=_code(error)
            result=getattr(error,'entry_result',None)
            if result is None:
                result=dict(_skipped(statuses[entry.id],'failed',code),error_type=type(error).__name__)
            results.append(result)
            if code in FATAL or code=='UNEXPECTED_ERROR':
                error.results=results
                raise
        else:
            results.append(result)
    return results
