"""LF-D02 local rebuild/update/retry through one bounded current-value pipeline.

Every pass first reduces source history into target partitions. It never creates
an official copy for each observation. Finite partition passes may rescan native
inputs; this trades bounded storage for extra reads, not for an unsafe permanent
ID/time watermark. A next invocation always sweeps early/late commits again.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import json
import sqlite3
import time

import pyarrow.parquet as pq
from sqlalchemy import text

from .adapters.contracts import Unit, digest
from .adapters.normalize import normalize
from .adapters.registry import BY_ID, ENTRIES, Entry
from .adapters.canonical import NativeInputError
from .catalog import SourceUpdate, Issue
from .errors import DataStoreError
from .locking import _deadline
from .merge import MergeSpool, restore_row
from .schema import DatasetSpec
from .values import control_json


@dataclass(frozen=True)
class PipelineOptions:
    mode: str = 'update'
    partitions: tuple[str,...] = ()
    partitions_per_pass: int = 1
    maximum_passes: int = 256
    pass_seconds: int = 300
    allow_incompatible_rebuild: bool = False

    def __post_init__(self):
        if (self.mode not in ('rebuild','update','retry') or
                not isinstance(self.partitions,tuple) or len(self.partitions)>8 or
                len(set(self.partitions))!=len(self.partitions) or
                type(self.partitions_per_pass) is not int or not 1<=self.partitions_per_pass<=8 or
                type(self.maximum_passes) is not int or not 1<=self.maximum_passes<=4096 or
                type(self.pass_seconds) is not int or not 1<=self.pass_seconds<=3600 or
                self.allow_incompatible_rebuild and self.mode!='rebuild'):
            raise ValueError('Invalid bounded pipeline options')
        from .schema import identifier
        for p in self.partitions: identifier(p)


def _scope(partition):
    return 'local.partition.'+partition


def _status(store, entry, summary):
    """One current operational state per static entry, never a pseudo dataset."""
    with store.catalog.transaction() as c:
        c.execute(text('INSERT INTO data_store_entry_status (entry_id,summary_json) VALUES (:i,:j) '
                       'ON CONFLICT (entry_id) DO UPDATE SET summary_json=EXCLUDED.summary_json, '
                       'updated_at=clock_timestamp()'), {'i':entry.id,'j':control_json(summary)})


def read_entry_status(store, entry_id):
    if entry_id not in BY_ID: raise ValueError('Unknown static entry')
    with store.catalog.transaction() as c:
        raw=c.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i'),
                      {'i':entry_id}).scalar_one_or_none()
    return json.loads(raw) if raw else {'entry_id':entry_id,'state':'not_checked','complete':False}


def _load_current(store, entry, partition, spool, *, rebuild):
    spec=entry.spec
    with store.locks.read(spec.name, timeout_ms=store.limits.lock_timeout_ms):
        state=store.catalog.dataset(spec.name)
        refs=store.catalog.files(spec.name,partition)
        incompatible=any(not spec.accepts(DatasetSpec.from_descriptor(json.loads(r['contract_json']))) for r in refs)
        if incompatible:
            if not rebuild: raise DataStoreError('REBUILD_REQUIRED')
            # The caller explicitly requested rebuilding from the complete local
            # source pass. Do not decode old files using the new descriptor.
            return state['generation'],True
        if sum(r['byte_count'] for r in refs)>store.limits.commit_bytes:
            raise DataStoreError('BATCH_BUDGET_EXCEEDED')
        current_key, rows = None, []
        def offer():
            if not rows: return
            first=rows[0]
            if any(any(r[k]!=first[k] for k in ('basis_group','basis_ns','basis_token','basis_state')) for r in rows):
                raise DataStoreError('FILE_INVALID')
            spool.offer(Unit(first['subject'],first['representation'],first['object_key'],
                             first['basis_group'],first['basis_ns'],first['basis_token'],tuple(rows),
                             failure='CURRENT_INPUT_INVALID' if first['basis_state']=='invalid' else None,
                             withdrawn=first['basis_state']=='withdrawn'),current=True)
        with ExitStack() as stack:
            paths=store._pin_files(spec,refs,stack,spool.space)
            for path in paths:
                with pq.ParquetFile(path,page_checksum_verification=True) as file:
                    for batch in file.iter_batches(batch_size=spec.bounded_rows(1024,store.limits.batch_bytes)):
                        spool.check()
                        for row in batch.to_pylist():
                            key=tuple(row[k] for k in spec.key[:3])
                            if current_key is not None and key!=current_key:
                                offer(); rows=[]
                            current_key=key;rows.append(row)
                            if len(rows)>100000: raise DataStoreError('BATCH_BUDGET_EXCEEDED')
            offer()
        return state['generation'],False


def _issue_from(record, partition, blocking):
    target={'scope_version':'object-key-v1','partition':None if record['o']=='unlocated' else partition,
            'prefix':[record['r'],record['s']]+([] if record['o']=='unlocated' else [record['o']]),
            'group':record['g'],'order':str(record['n']),'blocking':blocking}
    key='local.'+hashlib.sha256(bytes(record['k'])).hexdigest()
    return Issue(key,_scope(partition),record['reason'],record['t'],target,
                 {'retry':'same_local_pipeline','proof':'validate_exact_complete_target',
                  'classification':'blocking' if blocking else 'older_invalid_explained'})


def _partition_issues(store,entry,partition,spool):
    additions=[];resolved={};proofs={}
    for rec,blocking,fixed in spool.problem_records(partition):
        issue=_issue_from(rec,partition,blocking)
        proofs[issue.key]=(rec,fixed)
        if not fixed: additions.append(issue)
    # Bound every read by the kernel's issue policy. Paging does not silently
    # truncate a large unresolved set and unrelated targets are never cleared.
    after=''; old_by_key={}
    while True:
        page=store.catalog.issues(entry.spec.name,scope=_scope(partition),limit=500,after=after)
        for old in page:
            old_by_key[old['issue_key']]=old
            if len(old_by_key)>store.limits.issue_count: raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
            target=json.loads(old['target_json'])
            if target.get('scope_version')!='object-key-v1': continue
            prefix=target.get('prefix',[])
            if len(prefix)==3:
                winner=spool.db.execute('SELECT * FROM objects WHERE k=?',
                                       (entry.spec.key_bytes(tuple(prefix)),)).fetchone()
                if (winner and winner['validated'] and winner['state']!='invalid' and
                        winner['g']==target.get('group') and winner['n']>=int(target['order'])):
                    resolved[old['issue_key']]=old['evidence_token']
            elif len(prefix)==2:
                winner=spool.db.execute('SELECT * FROM scopes WHERE r=? AND s=?',tuple(prefix)).fetchone()
                if (winner and winner['g']==target.get('group') and winner['n']>=int(target['order'])):
                    resolved[old['issue_key']]=old['evidence_token']
        if len(page)<500: break
        after=page[-1]['issue_key']
    # A concurrently changed evidence token is never cleared: the kernel checks it.
    for issue in additions:
        resolved.pop(issue.key,None)
    if len(additions)+len(resolved)>1000:
        raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
    return tuple(additions),resolved


def _commit_partition(store,entry,partition,spool,options,cancelled):
    generation,incompatible=_load_current(store,entry,partition,spool,rebuild=options.allow_incompatible_rebuild)
    # Incompatible partitions require real replacement evidence. Absence from a
    # partial rescue/local range cannot authorize deleting their current values.
    if incompatible and not spool.db.execute('SELECT 1 FROM objects WHERE p=? LIMIT 1',(partition,)).fetchone():
        raise NativeInputError('REBUILD_SOURCE_INCOMPLETE','缺少可验证的当前分区重建来源。')
    issues,resolved=_partition_issues(store,entry,partition,spool)
    scope_key=_scope(partition)
    previous=store.source_state(entry.spec.name,scope_key)
    token=spool.partition_token(partition)
    source=SourceUpdate(scope_key,token,digest([entry.spec.schema_id,entry.spec.rule,partition]),
                        previous['revision'] if previous else 0,
                        confirmation={'basis':'inline_sparse_object_order','range':partition},
                        checkpoint={'policy':'rescan_native_inputs','partition':partition,'input':token},
                        qualified=not issues,expected_generation=generation)
    revision=source.expected_revision
    result=store.replace_partition(entry.spec,partition,
                spool.batches(partition,rows=store.limits.batch_rows,nbytes=store.limits.batch_bytes),source,
                complete=True,source_check=lambda current:(current['revision'] if current else 0)==revision,
                cancelled=cancelled,issues=issues,resolved=resolved)
    return {'partition':partition,'generation':result.generation,'changed':result.changed,
            'idempotent':result.idempotent,'new_issues':len(issues),'resolved':len(resolved),
            'current_blocked':sum(json.loads(i.target_json).get('blocking',True) for i in issues),
            'cleanup_pending':result.cleanup_pending,'metrics':asdict(result.metrics)}


def run_entry(store, entry: Entry, sources, *, options=PipelineOptions(), cancelled=None):
    """Process one registered entry; a failure never becomes complete/empty.

    `sources.iter_entry(entry)` must return a *new full read* on each call.
    Retry uses the same normalizer/order rules, optionally a bounded partition
    selector. No campaign/execution/runtime ID or supplier access is needed.
    """
    summary={'entry_id':entry.id,'mode':options.mode,'state':'running','complete':False,
             'passes':0,'committed_partitions':0,'source_rows':0,'normalized_units':0,
             'new_issues':0,'resolved':0,'input_failures':0,'metrics':{},'scope':'selected_partitions' if options.partitions else 'entry'}
    # Serializes snapshot acquisition + merge for this entry on the shared mount.
    # Kernel generation fencing separately rejects writes by other integrations.
    dataset=entry.spec.name if entry.business else 'local.entry.'+entry.id.lower()
    with store.locks._hold(dataset,'pipeline',fcntl.LOCK_EX,_deadline(store.limits.lock_timeout_ms),cancelled):
        old_status=read_entry_status(store,entry.id) if entry.id in BY_ID else {}
        if old_status.get('overflow_restriction'):summary['overflow_restriction']=old_status['overflow_restriction']
        _status(store,entry,summary)
        try:
            if not entry.business:
                for _ in sources.iter_entry(entry): pass
                summary.update(sources.summary)
                summary['state']=entry.disposition if summary.get('complete') else 'incomplete'
                _status(store,entry,summary)
                if entry.disposition=='ingestion_channel':
                    # Do not publish import progress. The actual validated native
                    # prices/events take exactly their ordinary target pipeline.
                    summary['target_entry']=entry.target
                return summary
            store.register(entry.spec,prepare_rebuild=options.allow_incompatible_rebuild)
            after=''
            for number in range(options.maximum_passes):
                summary['passes']=number+1
                with store.budget.reserve('read',cancelled=cancelled) as space:
                    # This is an explicitly finite transformation budget, not a
                    # query. It still shares measured native/RSS/disk accounting.
                    space.deadline=time.monotonic()+options.pass_seconds
                    spool=MergeSpool(space,entry.spec,after=after,partitions=options.partitions or None,
                                     partition_count=options.partitions_per_pass)
                    try:
                        for raw in sources.iter_entry(entry):
                            summary['source_rows']+=1
                            count=0; failed=False
                            for unit in normalize(entry,raw):
                                count+=1;failed|=bool(unit.failure)
                                summary['normalized_units']+=1;summary['input_failures']+=bool(unit.failure)
                                spool.offer(unit)
                            # Only whole current-object inputs can prove the end
                            # of a previously unlocated scope restriction. A
                            # sparse price/report request never clears it globally.
                            if count and not failed and entry.projection[0] in ('snapshot','reference'):
                                spool.validate_scope(raw)
                            spool.check()
                        if not sources.summary.get('complete',False):
                            raise NativeInputError('LOCAL_SCAN_INCOMPLETE','本地来源尚未完整扫描。')
                        spool.db.commit();spool.check()
                        selected=sorted(spool.parts)
                        for partition in selected:
                            try:
                                result=_commit_partition(store,entry,partition,spool,options,cancelled)
                            except DataStoreError as error:
                                if error.code=='ISSUE_BUDGET_EXCEEDED':
                                    from .problem_samples import write_overflow
                                    summary['overflow_restriction']={'partition':partition,'blocking_objects':1,
                                        'complete_key_list':False,'reason':'ISSUE_BUDGET_EXCEEDED'}
                                    _status(store,entry,summary)
                                    summary['overflow_restriction']=write_overflow(store,entry,partition,spool)
                                    _status(store,entry,summary)
                                raise
                            if summary.get('overflow_restriction'):
                                from .problem_samples import resolved_overflow
                                if resolved_overflow(store,entry,partition,spool,summary['overflow_restriction']):
                                    summary.pop('overflow_restriction')
                            summary['committed_partitions']+=1
                            summary['new_issues']+=result['new_issues'];summary['resolved']+=result['resolved']
                            summary['last_partition']=partition
                            summary['last_generation']=result['generation']
                            summary['cleanup_pending']=summary.get('cleanup_pending',False) or result['cleanup_pending']
                            for k,v in result['metrics'].items():
                                summary['metrics'][k]=(max(summary['metrics'].get(k,0),v) if k.endswith('peak_bytes')
                                                       else summary['metrics'].get(k,0)+v)
                            _status(store,entry,summary)
                        more=spool.discarded
                    finally:
                        spool.close()
                if options.partitions or not more or not selected:
                    summary['complete']=True
                    break
                after=selected[-1]
            if not summary['complete']:
                summary['state']='incomplete';summary['reason']='PASS_BUDGET_EXCEEDED'
            else:
                with store.catalog.transaction() as c:
                    issue_count=c.execute(text('SELECT count(*) FROM data_store_issues WHERE dataset=:d'),
                                          {'d':entry.spec.name}).scalar_one()
                summary['unresolved_issues']=issue_count
                summary['state']='processed_with_issues' if issue_count else 'empty' if not summary['normalized_units'] else 'processed'
                summary['qualified']=not issue_count and not summary.get('overflow_restriction',{}).get('blocking_objects')
            _status(store,entry,summary)
            return summary
        except (NativeInputError,DataStoreError,sqlite3.Error) as error:
            summary.update(state='incomplete',complete=False,qualified=False,
                           reason=error.code if isinstance(error,(NativeInputError,DataStoreError)) else 'SCRATCH_BUDGET_EXCEEDED')
            # Previously committed independent partitions remain authoritative.
            # Never mark a truncated pass as an empty or completed source range.
            _status(store,entry,summary)
            raise


def run_local(store,sources,*,entries=None,options=PipelineOptions(),cancelled=None):
    """All baseline entries have a real disposition; imports share target writes."""
    selected=list(entries or ENTRIES)
    if len(selected)>len(ENTRIES): raise ValueError('Too many source entries')
    seen=set();results=[]
    for entry in selected:
        if entry.id in seen: continue
        seen.add(entry.id)
        results.append(run_entry(store,entry,sources,options=options,cancelled=cancelled))
        if entry.target and entry.disposition=='ingestion_channel' and entry.target not in seen:
            target=BY_ID[entry.target];seen.add(target.id)
            results.append(run_entry(store,target,sources,options=options,cancelled=cancelled))
    return results
