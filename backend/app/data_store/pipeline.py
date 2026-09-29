"""LF-D02 local rebuild/update/retry through one bounded current-value pipeline.

One bounded full scan reduces local inputs into a charged continuation spool.
Only a complete scan can be published. Pending partitions survive invocation and
process boundaries; the current spool is removed when the sweep completes.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

import pyarrow.parquet as pq
from sqlalchemy import text

from .adapters.contracts import Unit, digest
from .adapters.normalize import normalize
from .adapters.registry import BY_ID, ENTRIES, Entry
from .adapters.canonical import NativeInputError
from .catalog import SourceUpdate, Issue, basis_hash, validate_issue_changes, MAX_ISSUE_CHANGES, MAX_ISSUE_CHANGE_BYTES
from .errors import DataStoreError
from .merge import MergeSpool, restore_row
from .schema import DatasetSpec
from .values import control_json
from .coverage import check_coverage


@dataclass(frozen=True)
class PipelineOptions:
    mode: str = 'update'
    partitions: tuple[str,...] = ()
    partitions_per_pass: int = 1
    maximum_passes: int = 256
    pass_seconds: int = 300
    allow_incompatible_rebuild: bool = False
    pipeline_spill_bytes: int | None = None

    def __post_init__(self):
        if (self.mode not in ('rebuild','update','retry') or
                not isinstance(self.partitions,tuple) or len(self.partitions)>8 or
                len(set(self.partitions))!=len(self.partitions) or
                type(self.partitions_per_pass) is not int or not 1<=self.partitions_per_pass<=8 or
                type(self.maximum_passes) is not int or not 1<=self.maximum_passes<=4096 or
                type(self.pass_seconds) is not int or not 1<=self.pass_seconds<=3600 or
                self.allow_incompatible_rebuild and self.mode!='rebuild' or
                self.pipeline_spill_bytes is not None and
                (type(self.pipeline_spill_bytes) is not int or
                 not 1 <= self.pipeline_spill_bytes <= 64*1024**3)):
            raise ValueError('Invalid bounded pipeline options')
        from .schema import identifier
        for p in self.partitions: identifier(p)


def _scope(partition):
    return 'local.partition.'+partition


def _pending_operations(status, scope):
    pending=set(status.get('coverage_pending', []))|{scope}
    return sorted(pending if len(pending)<=16 else {'entry',scope})


def _operation_scope(options):
    return 'partitions:'+','.join(sorted(options.partitions)) if options.partitions else 'entry'


def _source_rows(sources, entry):
    """Classify source-file IO at its boundary, separately from store IO."""
    try:
        yield from sources.iter_entry(entry)
    except OSError as error:
        if error.errno in (errno.ENOSPC,errno.EDQUOT):
            raise DataStoreError('DISK_PRESSURE') from None
        raise NativeInputError('LOCAL_SOURCE_UNAVAILABLE','该入口的本地来源无法读取。') from None


def _status(store, entry, summary):
    """One current operational state per static entry, never a pseudo dataset."""
    with store.catalog.transaction() as c:
        current=c.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i FOR UPDATE'),
                          {'i':entry.id}).scalar_one_or_none()
        if current and 'refresh' in json.loads(current):
            summary['refresh']=json.loads(current)['refresh']
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
    additions=[];resolved={};size=0
    for rec,blocking,fixed in spool.problem_records(partition):
        issue=_issue_from(rec,partition,blocking)
        if not fixed:
            size+=len(issue.target_json.encode())+len(issue.resolution_json.encode())+len(issue.key)+len(issue.scope)+len(issue.reason)+len(issue.evidence_token)
            if len(additions)>=MAX_ISSUE_CHANGES or size>MAX_ISSUE_CHANGE_BYTES:
                raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
            additions.append(issue)
    # Bound every read by the kernel's issue policy. Paging does not silently
    # truncate a large unresolved set and unrelated targets are never cleared.
    after=''; old_count=0
    while True:
        page=store.catalog.issues(entry.spec.name,scope=_scope(partition),limit=500,after=after)
        for old in page:
            old_count+=1
            if old_count>store.limits.issue_count: raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
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
        # Bound the retained delta while paging old metadata, rather than
        # materializing an entire unresolved partition before checking it.
        validate_issue_changes(additions,resolved)
        if len(page)<500: break
        after=page[-1]['issue_key']
    # A concurrently changed evidence token is never cleared: the kernel checks it.
    for issue in additions:
        resolved.pop(issue.key,None)
    validate_issue_changes(additions,resolved)
    return tuple(additions),resolved


def _commit_partition(store,entry,partition,spool,options,cancelled):
    with store.locks.read(entry.spec.name,timeout_ms=store.limits.lock_timeout_ms):
        previous=store.source_state(entry.spec.name,_scope(partition))
        if previous and previous.get('checkpoint',{}).get('cycle') == spool.cycle:
            # The catalog commit may survive a crash before the SQLite cursor.
            # Only its unchanged file basis proves that this partition is still
            # committed; a different integration must not be silently ignored.
            if previous['basis_hash'] != basis_hash(store.catalog.files(entry.spec.name,partition)):
                raise DataStoreError('SOURCE_CONFLICT')
            return None
    generation,incompatible=_load_current(store,entry,partition,spool,rebuild=options.allow_incompatible_rebuild)
    # Incompatible partitions require real replacement evidence. Absence from a
    # partial rescue/local range cannot authorize deleting their current values.
    if incompatible and not spool.db.execute('SELECT 1 FROM objects WHERE p=? LIMIT 1',(partition,)).fetchone():
        raise NativeInputError('REBUILD_SOURCE_INCOMPLETE','缺少可验证的当前分区重建来源。')
    issues,resolved=_partition_issues(store,entry,partition,spool)
    scope_key=_scope(partition)
    previous=store.source_state(entry.spec.name,scope_key)
    token=spool.partition_token(partition)
    checkpoint={'policy':'complete_scan_continuation','partition':partition,'input':token,'cycle':spool.cycle}
    if previous and previous['input_token']==token and not issues and not resolved:
        # Reuse equal confirmation metadata; the kernel still fences generation
        # and verifies the current file basis before taking its no-op path.
        checkpoint=previous['checkpoint']
    source=SourceUpdate(scope_key,token,digest([entry.spec.schema_id,entry.spec.rule,partition]),
                        previous['revision'] if previous else 0,
                        confirmation={'basis':'inline_sparse_object_order','range':partition},
                        checkpoint=checkpoint,
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


def _run_entry_locked(store, entry: Entry, sources, *, options=PipelineOptions(), cancelled=None):
    """Process one registered entry; a failure never becomes complete/empty.

    `sources.iter_entry(entry)` must return a *new full read* on each call.
    Retry uses the same normalizer/order rules, optionally a bounded partition
    selector. No campaign/execution/runtime ID or supplier access is needed.
    """
    full_scan=not options.partitions and not getattr(sources,'incremental_slice',False)
    summary={'entry_id':entry.id,'mode':options.mode,'state':'running','complete':False,
             'partitions':list(options.partitions),
             'passes':0,'committed_partitions':0,'source_rows':0,'normalized_units':0,
             'new_issues':0,'resolved':0,'input_failures':0,'metrics':{},'scope':'selected_partitions' if options.partitions else 'entry'}
    old_status=read_entry_status(store,entry.id) if entry.id in BY_ID else {}
    # Preserve coverage independently of the most recent operation. A
    # bounded set of unresolved selectors prevents a later unrelated retry
    # from hiding a failed update. Overflow requires a complete scan.
    operation_scope='native_ranges' if getattr(sources,'incremental_slice',False) else _operation_scope(options)
    summary['coverage_pending']=_pending_operations(old_status,operation_scope)
    for key in ('full_coverage','last_complete_coverage','incremental'):
        if key in old_status: summary[key]=old_status[key]
    maintain_coverage=False
    if entry.business and options.partitions:
        with store.catalog.transaction() as c:
            # A failed selected correction can be repaired without losing
            # the preceding full receipt; quality is checked again below.
            candidate=dict(old_status,coverage_pending=[])
            judgment=check_coverage(c,entry,candidate)
            maintain_coverage=judgment['satisfied'] or judgment['reason']=='CURRENT_QUALITY_UNRESOLVED'
    if old_status.get('overflow_restriction'):summary['overflow_restriction']=old_status['overflow_restriction']
    _status(store,entry,summary)
    try:
        source_selection=sources.selection_key() if hasattr(sources,'selection_key') else None
        source_kind=digest([type(sources).__module__,type(sources).__qualname__])
        prior_proof=summary.get('full_coverage',{})
        if options.partitions and prior_proof and (
                prior_proof.get('source_selection')!=source_selection or
                prior_proof.get('source_kind')!=source_kind):
            summary['coverage_pending']=sorted(set(summary['coverage_pending'])|{'entry'})
        maintain_coverage=(maintain_coverage and
                           summary['full_coverage'].get('source_selection')==source_selection and
                           summary['full_coverage'].get('source_kind')==source_kind)
        if not entry.business:
            summary['work_admitted']=True
            for _ in _source_rows(sources,entry): pass
            summary.update(sources.summary)
            summary['source_scanned']=bool(sources.summary.get('complete'))
            summary['state']=entry.disposition if summary.get('complete') else 'incomplete'
            _status(store,entry,summary)
            if entry.disposition=='ingestion_channel':
                # Do not publish import progress. The actual validated native
                # prices/events take exactly their ordinary target pipeline.
                summary['target_entry']=entry.target
            return summary
        store.register(entry.spec,prepare_rebuild=options.allow_incompatible_rebuild)
        with store.budget.reserve('read',cancelled=cancelled,
                                  pending='pipeline.'+entry.id+('.selected' if options.partitions else ''),
                                  quota_bytes=options.pipeline_spill_bytes) as space:
            summary['work_admitted']=True
            space.deadline=time.monotonic()+options.pass_seconds
            spool=MergeSpool(space,entry.spec,partitions=options.partitions or None,
                             partition_count=None)
            try:
                identity=digest([entry.spec.descriptor(),type(sources).__module__,
                                 type(sources).__qualname__,options.mode,options.partitions,
                                 options.allow_incompatible_rebuild])
                record=spool.db.execute('SELECT body FROM progress WHERE id=1').fetchone()
                progress=json.loads(record[0]) if record else {}
                compatible_identity=identity
                if options.mode in ('update','retry'):
                    compatible_identity=digest([entry.spec.descriptor(),type(sources).__module__,
                        type(sources).__qualname__,'retry' if options.mode=='update' else 'update',
                        options.partitions,options.allow_incompatible_rebuild])
                if (progress.get('scan_complete') and progress.get('identity') not in (identity,compatible_identity)):
                    # A different selector/mode must never erase the only
                    # sealed input boundary of an unfinished invocation.
                    conflict=DataStoreError('SOURCE_CONFLICT');conflict.preserve_continuation=True
                    raise conflict
                if (progress.get('scan_complete') and 'source_selection' in progress and
                        progress['source_selection']!=source_selection):
                    conflict=DataStoreError('SOURCE_CONFLICT');conflict.preserve_continuation=True
                    raise conflict
                if progress.get('identity') not in (identity,compatible_identity) or not progress.get('scan_complete'):
                    # A failed/incomplete scan provides no deletion evidence.
                    # It is safe to restart acquisition because none of it was
                    # published. A sealed scan instead resumes without IO.
                    for table in ('objects','problems','scopes','partitions','progress'):
                        spool.db.execute('DELETE FROM '+table)
                    input_hash=hashlib.sha256()
                    invalid_snapshot_scopes=set()
                    for raw in _source_rows(sources,entry):
                        input_hash.update(digest([raw.source,raw.dataset,raw.subject,
                                                  raw.variant,raw.token,raw.order_ns]).encode())
                        summary['source_rows']+=1
                        count=0; failed=False
                        for unit in normalize(entry,raw):
                            count+=1;failed|=bool(unit.failure)
                            summary['normalized_units']+=1;summary['input_failures']+=bool(unit.failure)
                            if unit.failure and getattr(sources,'snapshot_scope',None):
                                invalid_snapshot_scopes.add((raw.subject,str(raw.content.get('trade_date',''))[:7]))
                            spool.offer(unit)
                        if count and not failed and entry.projection[0] in ('snapshot','reference'):
                            spool.validate_scope(raw)
                        spool.check()
                    if not sources.summary.get('complete',False):
                        raise NativeInputError('LOCAL_SCAN_INCOMPLETE','本地来源尚未完整扫描。')
                    summary['source_scanned']=True
                    snapshot_scopes=getattr(sources,'snapshot_scope',None)
                    if snapshot_scopes is not None:
                        # Each month is authoritative only if all its rows were
                        # valid. A bad sibling month must not prevent deletion
                        # in an independently complete good month in this batch.
                        snapshot_scopes=[scope for scope in snapshot_scopes
                            if (scope['subject'],scope['month']) not in invalid_snapshot_scopes]
                    representation=(sources.summary.get('snapshot_representation')
                                    if entry.complete_table_snapshot and
                                    (not summary['input_failures'] or snapshot_scopes is not None) else None)
                    if representation:
                        # Include partitions that became entirely empty. The
                        # catalog is current metadata, never a source ledger.
                        if snapshot_scopes is not None:
                            for scope in snapshot_scopes:
                                spool._select(entry.spec.partitioner((representation,scope['subject'],scope['month']+'-01','root')))
                        else:
                            with store.catalog.transaction() as c:
                                for (partition,) in c.execute(text(
                                    'SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:d'),
                                    {'d':entry.spec.name}):
                                    spool._select(partition)
                    progress={'identity':identity,'scan_complete':True,'after':'',
                              'cycle':uuid4().hex,'representation':representation,
                              'snapshot_scope':snapshot_scopes,
                              'source_rows':summary['source_rows'],
                              'normalized_units':summary['normalized_units'],
                              'input_failures':summary['input_failures'],
                              'boundary':input_hash.hexdigest(),'source_selection':source_selection}
                    spool.db.execute('INSERT OR REPLACE INTO progress VALUES (1,?)',
                                     (json.dumps(progress),))
                    spool.db.commit();spool.check()
                else:
                    summary['resumed']=True
                if full_scan:
                    # Upgrade a legacy sealed continuation from its actual
                    # ordered range and cursor, never from old booleans.
                    # Completed-prefix checkpoints are verified below.
                    range_hash=hashlib.sha256()
                    end=''; expected=0
                    for (part,) in spool.db.execute('SELECT p FROM partitions ORDER BY p'):
                        spool.check()
                        range_hash.update(digest(part).encode());end=part;expected+=1
                        if part<=progress['after']:
                            previous=store.source_state(entry.spec.name,_scope(part))
                            checkpoint=(previous or {}).get('checkpoint',{})
                            if (not previous or checkpoint.get('policy')!='complete_scan_continuation'
                                    or checkpoint.get('partition')!=part
                                    or previous['context_token']!=digest([entry.spec.schema_id,entry.spec.rule,part])
                                    or previous['basis_hash']!=basis_hash(store.catalog.files(entry.spec.name,part))):
                                raise DataStoreError('SOURCE_CONFLICT')
                    if progress['after'] and not spool.db.execute(
                            'SELECT 1 FROM partitions WHERE p=?',(progress['after'],)).fetchone():
                        raise DataStoreError('SOURCE_CONFLICT')
                    summary['full_coverage']={
                        'version':1,'entry_id':entry.id,'contract':digest(entry.spec.descriptor()),
                        'source_selection':source_selection,'source_kind':source_kind,
                        'boundary':progress.get('boundary') or digest([identity,progress['cycle'],range_hash.hexdigest()]),
                        'boundary_kind':'source_scan' if progress.get('boundary') else 'legacy_sealed_continuation',
                        'range_digest':range_hash.hexdigest(),'expected':expected,'end':end,
                        'scan_complete':True,'complete':False,'after':progress['after'],
                        'remaining':spool.db.execute('SELECT count(*) FROM partitions WHERE p>?',
                                                    (progress['after'],)).fetchone()[0],
                        'cycle':progress['cycle']}
                    _status(store,entry,summary)
                summary['scan_source_rows']=progress['source_rows']
                summary['scan_normalized_units']=progress['normalized_units']
                spool.cycle=progress['cycle']
                spool.snapshot_representation=progress['representation']
                spool.snapshot_scope=progress.get('snapshot_scope')
                overflow=summary.get('overflow_restriction',{})
                if (summary.get('resumed') and overflow and not overflow.get('file')
                        and overflow.get('partition','')>progress['after']):
                    # Earlier versions could fail while writing the bounded
                    # overflow receipt. Recreate it only from this unchanged
                    # sealed continuation, never infer missing problem keys
                    # from a later source scan or clear the placeholder.
                    from .problem_samples import write_overflow
                    summary['overflow_restriction']=write_overflow(
                        store,entry,overflow['partition'],spool)
                    _status(store,entry,summary)
                for number in range(options.maximum_passes):
                    summary['passes']=number+1
                    space.deadline=time.monotonic()+options.pass_seconds
                    selected=[r[0] for r in spool.db.execute(
                        'SELECT p FROM partitions WHERE p>? ORDER BY p LIMIT ?',
                        (progress['after'],options.partitions_per_pass))]
                    if not selected:
                        summary['complete']=True
                        break
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
                        if result is not None:
                            summary['committed_partitions']+=1
                            summary['new_issues']+=result['new_issues'];summary['resolved']+=result['resolved']
                            summary['last_generation']=result['generation']
                            summary['cleanup_pending']=summary.get('cleanup_pending',False) or result['cleanup_pending']
                            for k,v in result['metrics'].items():
                                summary['metrics'][k]=(max(summary['metrics'].get(k,0),v) if k.endswith('peak_bytes')
                                                       else summary['metrics'].get(k,0)+v)
                        summary['last_partition']=partition
                        progress['after']=partition
                        spool.db.execute('UPDATE progress SET body=? WHERE id=1',(json.dumps(progress),))
                        # Only pending working values are retained; completed
                        # partitions leave no duplicate per-row success log.
                        spool.db.execute('DELETE FROM objects WHERE p=?',(partition,))
                        spool.db.execute('DELETE FROM problems WHERE p=?',(partition,))
                        spool.db.commit()
                        if full_scan:
                            summary['full_coverage'].update(after=partition,remaining=spool.db.execute(
                                'SELECT count(*) FROM partitions WHERE p>?',(partition,)).fetchone()[0])
                        _status(store,entry,summary)
                summary['complete']=not spool.db.execute(
                    'SELECT 1 FROM partitions WHERE p>? LIMIT 1',(progress['after'],)).fetchone()
                if summary['complete']:
                    # Persist the receipt before reclaiming sealed work.
                    # A crash before this write resumes the cursor; a crash
                    # afterwards leaves a valid compact proof, not a gap.
                    with store.locks.read(entry.spec.name,timeout_ms=store.limits.lock_timeout_ms):
                        generation=store.catalog.dataset(entry.spec.name)['generation']
                        if full_scan:
                            summary['full_coverage'].update(complete=True,remaining=0,generation=generation)
                            summary['last_complete_coverage']=dict(summary['full_coverage'])
                            summary['coverage_pending']=[]
                        else:
                            summary['coverage_pending']=[p for p in summary['coverage_pending'] if p!=operation_scope]
                            if maintain_coverage:
                                summary['full_coverage']=dict(summary['full_coverage'],generation=generation)
                        _status(store,entry,summary)
                    space.discard=True
            except BaseException as scan_error:
                # SQLite's progress callback can interrupt SQL for timeout,
                # cancellation or memory pressure. Preserve that exact guard.
                if isinstance(scan_error,sqlite3.Error) and spool._interrupt is not None:
                    scan_error=spool._interrupt
                # Unsealed acquisition cannot resume a repeatable-read DB
                # transaction. Reclaim it immediately; sealed work survives.
                if not spool.db.execute('SELECT 1 FROM progress WHERE id=1').fetchone():
                    space.discard=True
                raise scan_error
            finally:
                # Include acquisition/merge scratch, not only the writer's
                # staging metric. These are sampled per-reservation peaks.
                for key in ('scratch_peak_bytes','rss_peak_bytes'):
                    summary['metrics'][key]=max(summary['metrics'].get(key,0),getattr(space.metrics,key))
                spool.close()
        if not summary['complete']:
            summary['state']='incomplete';summary['reason']='PASS_BUDGET_EXCEEDED'
        else:
            with store.catalog.transaction() as c:
                issue_count=c.execute(text('SELECT count(*) FROM data_store_issues WHERE dataset=:d'),
                                      {'d':entry.spec.name}).scalar_one()
            summary['unresolved_issues']=issue_count
            summary['state']='processed_with_issues' if issue_count else 'empty' if not summary['scan_normalized_units'] else 'processed'
            summary['qualified']=not issue_count and not summary.get('overflow_restriction',{}).get('blocking_objects')
        _status(store,entry,summary)
        return summary
    except (NativeInputError,DataStoreError,sqlite3.Error) as error:
        summary.update(state='incomplete',complete=False,qualified=False,
                       reason=error.code if isinstance(error,(NativeInputError,DataStoreError)) else ('SCRATCH_BUDGET_EXCEEDED' if getattr(error,'sqlite_errorcode',None)==sqlite3.SQLITE_FULL or 'database or disk is full' in str(error) else 'FILE_INVALID'))
        # Previously committed independent partitions remain authoritative.
        # Never mark a truncated pass as an empty or completed source range.
        _status(store,entry,summary)
        if isinstance(error, sqlite3.Error):
            # SQLite's bounded working file reports SQLITE_FULL as a native
            # exception. Keep the same safe public reason as the persisted
            # status, so the CLI can continue with independent entries.
            raise DataStoreError(summary['reason']) from None
        raise



def run_entry(store, entry, sources, *, options=PipelineOptions(), cancelled=None, **kwargs):
    """One entry lock covers processing and its current retry metadata."""
    from .updates import run_entry as attempt
    return attempt(store,entry,sources,options=options,cancelled=cancelled,**kwargs)


def run_local(store, sources, *, entries=None, options=PipelineOptions(), cancelled=None, **kwargs):
    """CLI and scheduler share the same finite, entry-isolated dispatcher."""
    from .updates import run_local as dispatch
    return dispatch(store,sources,entries=entries,options=options,cancelled=cancelled,**kwargs)
