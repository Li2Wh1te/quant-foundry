"""Finite entry-local refresh scheduling; no shared campaign completion gate."""
from __future__ import annotations

from dataclasses import dataclass, replace
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
from .continuation import FATAL, WAITING, disposition, source_revision


@dataclass(frozen=True)
class RetryPolicy:
    initial_seconds: int = 30
    maximum_seconds: int = 1800
    abandon_after: int = 3
    call_seconds: int = 900

    def __post_init__(self):
        if any(type(v) is not int or v<1 for v in vars(self).values()):
            raise ValueError('Positive finite retry limits required')


COUNTERS = ('passes','committed_partitions','source_rows','normalized_units','new_issues',
            'resolved','input_failures','budget_units','range_batches','acknowledged_ranges')


def _read(store, entry):
    with store.catalog.transaction() as c:
        raw=c.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i'),
                      {'i':entry.id}).scalar_one_or_none()
    return json.loads(raw) if raw else {'entry_id':entry.id}


def _refresh(store, entry, patch, *, operation=None, previous=None):
    """Only update our namespace from a freshly locked row, never coverage."""
    with store.catalog.transaction() as c:
        raw=c.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i FOR UPDATE'),
                      {'i':entry.id}).scalar_one_or_none()
        status=json.loads(raw) if raw else {'entry_id':entry.id}
        if operation:status.update(operation)
        state=dict(status.get('refresh',{}) if previous is None else previous)
        state.update(patch);status['refresh']=state
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
        if (_policy is not None and options.mode!='retry' and previous.get('next_retry_at',0)>now):
            skipped=dict(_skipped(old,'backoff','RETRY_BACKOFF'),refresh=previous)
            state=_refresh(store,entry,{'outcome':'backoff','attempted':False,
                'continuation':disposition(skipped,now=now,
                    sealed='pipeline.'+entry.id in store.budget.pending_keys())})
            return dict(skipped,refresh=state)
        revision=source_revision(sources,entry) if _policy is not None else None
        if (options.mode!='retry' and revision is not None and
                previous.get('blocked_input_revision')==revision):
            return dict(_skipped(old,'blocked','SOURCE_INPUT_UNCHANGED'),refresh=previous)
        _refresh(store,entry,{'last_attempt_at':now,'attempted':True,'outcome':'running','deferred_since':0,
            'continuation':{'version':1,'state':'running','eligible':True,'available_at':now+30}})
        error=None
        try:
            from .incremental import enabled, run as incremental_run
            # Drain an already sealed pre-upgrade acquisition before switching
            # to the range queue; its sole retained input must not be discarded.
            legacy_pending=('pipeline.'+entry.id in store.budget.pending_keys() and
                            not old.get('incremental') and old.get('scope')!='native_incremental')
            execute=incremental_run if enabled(sources,entry,options) and not legacy_pending else _run_entry_locked
            # Range batches have their own acknowledgement protocol. Only a
            # normal complete-table acquisition binds this metadata to its
            # durable spool, leaving existing sealed revisions untouched.
            execution_options=(replace(options,acquisition_revision=revision)
                if execute is _run_entry_locked and revision is not None and not options.partitions
                else options)
            result=execute(store,entry,sources,options=execution_options,cancelled=cancelled)
        except BaseException as caught:
            error=caught
            try:
                status=getattr(caught,'entry_result',None)
                if status is None:status=_read(store,entry)
            except Exception:
                # Database failure must not replace a process exit/cancel signal.
                status=_skipped(old,'failed',_code(caught))
            result=dict(status,state='incomplete',complete=False,qualified=False,reason=_code(caught))
        # Complete-table adapters use the same finite pass allowance as native
        # ranges. Publish their actual units too; a real admitted empty scan or
        # acquisition failure costs one unit, while an unadmitted wait costs none.
        result.setdefault('budget_units',max(1,result.get('passes',0))
                          if result.get('work_admitted') else 0)
        if (isinstance(error,DataStoreError) and error.code in WAITING
                and not result.get('work_admitted')):
            # The entry lock was checked, but no input/commit was admitted.
            # Restore its prior attempt/backoff clocks; only this invocation's
            # waiting state changes. No new coverage is claimed; the existing
            # current-data proof and any pending/quality restrictions survive.
            state=_refresh(store,entry,{'outcome':'deferred','attempted':False,
                'deferred_since':previous.get('deferred_since') or now,
                'reason':error.code,'error_type':None,
                'continuation':disposition(dict(result,outcome='deferred',reason=error.code),now=now)},previous=previous)
            return dict(_skipped(result,'deferred',error.code),refresh=state)
        if isinstance(error,DataStoreError) and error.code in WAITING:
            # Earlier ranges in this invocation may already have committed.
            # Their counters/checkpoints survive ordinary capacity waiting.
            error=None
        success=bool(result.get('complete') and result.get('qualified',not entry.business))
        continued=(error is None and not result.get('complete') or
                   result.get('reason')=='LOCAL_UPDATE_BUDGET_EXCEEDED')
        if not success and not continued and error is None:
            result['reason']=result.get('reason') or 'CURRENT_QUALITY_UNRESOLVED'
        outcome=('updated' if result.get('metrics',{}).get('files_written',0) else 'unchanged') if success else (
            'continued' if continued else 'failed')
        failures=0 if success or continued else min(32,previous.get('consecutive_failures',0)+1)
        progressed=(result.get('committed_partitions',0)>0 or result.get('acknowledged_ranges',0)>0 or
                    result.get('last_partition') and result.get('last_partition')!=old.get('last_partition'))
        stalls=min(32,previous.get('stalled_failures',0)+1) if failures and not progressed else 0
        patch={'outcome':outcome,'attempted':True,'consecutive_failures':failures,
               'stalled_failures':stalls,
               'next_retry_at':int(clock())+min(policy.maximum_seconds,policy.initial_seconds*2**(failures-1)) if failures else 0,
               'error_type':type(error).__name__ if error else None,
               'reason':result.get('reason') if not success else None}
        if success:patch['last_success_at']=int(clock())
        if success or continued:patch['continuation_aborted']=None
        if result.get('work_admitted') or progressed:patch['last_work_at']=now
        if result.get('source_scanned'):patch['last_source_scan_at']=now
        candidate=dict(result,outcome=outcome,refresh=patch)
        patch['continuation']=disposition(candidate,now=int(clock()),
            sealed='pipeline.'+entry.id in store.budget.pending_keys())
        if revision is not None:
            patch['input_revision']=revision
            disposed_revision=result.get('captured_input_revision') or (
                revision if result.get('source_scanned') or
                result.get('reason')=='SOURCE_RANGES_BLOCKED' else None)
            if success or result.get('reason') in FATAL:
                # A capacity/storage error is not evidence that this source
                # version was disposed. It must remain eligible after the
                # actual global problem is repaired, including explicit retry.
                patch['blocked_input_revision']=None
            elif (disposed_revision is not None and not patch['continuation']['eligible'] and
                    (result.get('reason')=='SOURCE_RANGES_BLOCKED' or
                     result.get('source_scanned') and (
                         result.get('input_failures') or result.get('new_issues') or
                         result.get('complete') and not result.get('qualified')) or
                     result.get('captured_input_revision') and result.get('complete') and
                     not result.get('qualified'))):
                # A complete narrow report may preserve an older whole-subject
                # restriction even when every fresh unit is valid. Repeating
                # that exact source cannot supply the missing authority. In
                # contrast, failures seen before acquisition reached EOF must
                # not pin an incomplete input and silently skip its remainder.
                # When disposal spans calls, block the seal's captured revision
                # instead of the newer heads observed while draining it. An
                # unknown legacy seal gets one fresh baseline after disposal.
                patch['blocked_input_revision']=disposed_revision
            elif success or progressed:
                patch['blocked_input_revision']=None
        # A repeatedly failing sealed unit must not retain every shared slot
        # forever. Explicitly invalidate its pending coverage before releasing
        # only its idle scratch. Current files and committed checkpoints survive.
        if (_policy is not None and stalls>=policy.abandon_after and
                result.get('reason') not in FATAL and error is not None and
                not getattr(error,'preserve_continuation',False) and
                isinstance(error,(DataStoreError,NativeInputError))):
            owner='pipeline.'+entry.id+('.selected' if options.partitions else '')
            if owner in store.budget.pending_keys():
                _refresh(store,entry,dict(patch,continuation_aborted='REPEATED_ENTRY_FAILURE'))
                store.budget.abandon(owner)
                patch['continuation_aborted']='REPEATED_ENTRY_FAILURE'
        try:
            state=_refresh(store,entry,patch,operation=(
                {'state':'incomplete','complete':False,'qualified':False,'reason':_code(error)} if error else None))
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
    remaining_passes=options.maximum_passes
    for entry in selected:
        try:
            if cancelled and cancelled():raise DataStoreError('OPERATION_CANCELLED')
            if monotonic()>=deadline or remaining_passes<=0:
                result=_skipped(_read(store,entry),'deferred','LOCAL_UPDATE_BUDGET_EXCEEDED')
                result['refresh']=_refresh(store,entry,{'deferred_since':int(clock()),
                    'continuation':disposition(result,now=int(clock()))})
                results.append(result)
                continue
            entry_deadline=min(deadline,monotonic()+options.pass_seconds)
            def stop():
                if cancelled and cancelled():return True
                if monotonic()>=entry_deadline:raise DataStoreError('LOCAL_UPDATE_BUDGET_EXCEEDED')
                return False
            result=attempt(store,entry,sources,options=replace(options,maximum_passes=remaining_passes),
                           cancelled=stop,_policy=policy,clock=clock)
        except Exception as error:
            code=_code(error)
            result=getattr(error,'entry_result',None)
            if result is None:
                result=dict(_skipped(statuses[entry.id],'failed',code),error_type=type(error).__name__)
            results.append(result)
            remaining_passes-=result.get('budget_units',result.get('passes',0))
            if code in FATAL or code=='UNEXPECTED_ERROR':
                error.results=results
                raise
        else:
            results.append(result)
            remaining_passes-=result.get('budget_units',result.get('passes',0))
    return results
