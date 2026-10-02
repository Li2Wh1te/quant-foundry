"""Native range consumer shared by the CLI and ordinary scheduled updates.

Only one source object/month is decoded into a charged spool at a time. The
source claim survives process exits; its cursor advances only after the current
commit. Concurrent writes merge into a separate pending range, so an acknowledgement
cannot erase a late commit or r+1. No completed observation ledger is retained.
"""
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import text

from .adapters.canonical import NativeInputError
from .adapters.contracts import LocalInput, digest, native_json, instant_ns
from .local_sources import (NativeSources, TABLES, _json, _requests, _scope,
                            materialize_observation, EffectiveBasis, _confirmed, _MERGED)
from .errors import DataStoreError

IDS = frozenset(('E23','E44','E50','E51','E52','E69','E70'))
WHERE = 'source=:source AND dataset=:dataset AND subject=:subject AND variant=:variant AND range_key=:range_key'
ZERO = '00000000-0000-0000-0000-000000000000'


def enabled(sources, entry, options):
    # Rescue/combined sources retain their explicit complete-scan semantics.
    return type(sources) is NativeSources and entry.id in IDS and not options.partitions


def _params(entry):return {'source':entry.source,'dataset':entry.native}


def available(engine,entry=None):
    with engine.connect() as c:
        if not c.execute(text("SELECT to_regclass('data_store_capture_version') IS NOT NULL")).scalar_one():return False
        if entry is not None and entry.id in ('E23','E44') and c.execute(
                text('SELECT version FROM data_store_capture_version WHERE singleton=1')).scalar_one()<2:
            return False
        # Disabled/bypassed capture is not a proven caught-up boundary. Ordinary
        # INSERT/UPDATE/DELETE/COPY execute these triggers; TRUNCATE is refused.
        return c.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname='data_store_capture_range' AND tgenabled IN ('O','A') AND tgrelid IN (to_regclass('tonghuashun_observations'),to_regclass('tonghuashun_collection_states'),to_regclass('etf_daily_bars'),to_regclass('etf_adjustment_factors'))")).scalar_one()==4


def claim(sources,entry):
    """Capture an active source boundary without holding locks during file IO."""
    with sources.engine.begin() as c:
        row=c.execute(text('SELECT source,dataset,subject,variant,range_key,pending,bootstrap_pending,active,lower_at::text AS lower_text FROM data_store_source_ranges '
            "WHERE source=:source AND dataset=:dataset AND (active->'blocked' IS NULL OR EXISTS(SELECT 1 FROM tonghuashun_collection_states s WHERE s.dataset=data_store_source_ranges.dataset AND s.subject=data_store_source_ranges.subject AND s.variant=data_store_source_ranges.variant AND s.status='succeeded')) "
            'ORDER BY (active IS NOT NULL) DESC,enqueued_at,subject,variant,range_key LIMIT 1 FOR UPDATE'),_params(entry)).mappings().first()
        if row is None:return None
        row=dict(row)
        if row['active'] is not None and row['active'].get('blocked'):
            row['active']=dict(row['active']);row['active'].pop('blocked')
            c.execute(text('UPDATE data_store_source_ranges SET active=CAST(:a AS jsonb) WHERE '+WHERE),{**row,'a':json.dumps(row['active'])})
        if row['active'] is None:
            # +infinity is a state-only event. UUID order is used solely inside
            # this captured boundary; later commits are independently dirtied.
            stop=None
            if entry.source=='tonghuashun':
                stop=c.execute(text('SELECT observed_at::text AS at,id::text AS id FROM tonghuashun_observations '
                    'WHERE dataset=:dataset AND subject=:subject AND variant=:variant '
                    'AND observed_at>=CAST(:lower_text AS timestamptz) ORDER BY observed_at DESC,id DESC LIMIT 1'),row).mappings().first()
            active={'id':uuid4().hex,'bootstrap':row['bootstrap_pending'],'when':datetime.now(timezone.utc).isoformat(),
                    'lower':row['lower_text'],'after':None,'stop':dict(stop) if stop else None}
            row['active']=active
            c.execute(text('UPDATE data_store_source_ranges SET active=CAST(:a AS jsonb),pending=false,bootstrap_pending=false '
                           'WHERE '+WHERE),{**row,'a':json.dumps(active)})
        return row


def claim_funds(sources,entry,limit):
    """Claim a bounded set of fund scopes so small observations share file IO.

    Existing active claims always resume as the same ordered group. Only native
    identity/boundary metadata is held here; payloads remain in their original
    tables and the merger still owns one finite, reusable working file.
    """
    with sources.engine.begin() as c:
        active=c.execute(text('SELECT EXISTS(SELECT 1 FROM data_store_source_ranges '
            "WHERE source=:source AND dataset=:dataset AND active IS NOT NULL AND (active->'blocked' IS NULL OR EXISTS(SELECT 1 FROM tonghuashun_collection_states s WHERE s.dataset=data_store_source_ranges.dataset AND s.subject=data_store_source_ranges.subject AND s.variant=data_store_source_ranges.variant AND s.status='succeeded')))"),_params(entry)).scalar_one()
        rows=c.execute(text('SELECT source,dataset,subject,variant,range_key,pending,bootstrap_pending,active,lower_at::text AS lower_text '
            "FROM data_store_source_ranges WHERE source=:source AND dataset=:dataset AND (active->'blocked' IS NULL OR EXISTS(SELECT 1 FROM tonghuashun_collection_states s WHERE s.dataset=data_store_source_ranges.dataset AND s.subject=data_store_source_ranges.subject AND s.variant=data_store_source_ranges.variant AND s.status='succeeded')) "
            'AND (:active=false OR active IS NOT NULL) '
            "ORDER BY CASE WHEN :active THEN '-infinity'::timestamptz ELSE enqueued_at END,subject,variant LIMIT :n FOR UPDATE"),
            {**_params(entry),'active':active,'n':64 if active else limit}).mappings().all()
        jobs=[]
        for value in rows:
            job=dict(value)
            if job['active'] is not None and job['active'].get('blocked'):
                job['active']=dict(job['active']);job['active'].pop('blocked')
                c.execute(text('UPDATE data_store_source_ranges SET active=CAST(:a AS jsonb) WHERE '+WHERE),{**job,'a':json.dumps(job['active'])})
            if job['active'] is None:
                stop=c.execute(text('SELECT observed_at::text AS at,id::text AS id FROM tonghuashun_observations '
                    'WHERE dataset=:dataset AND subject=:subject AND variant=:variant '
                    'AND observed_at>=CAST(:lower_text AS timestamptz) ORDER BY observed_at DESC,id DESC LIMIT 1'),job).mappings().first()
                job['active']={'id':uuid4().hex,'bootstrap':job['bootstrap_pending'],'when':datetime.now(timezone.utc).isoformat(),
                    'lower':job['lower_text'],'after':None,'stop':dict(stop) if stop else None}
                c.execute(text('UPDATE data_store_source_ranges SET active=CAST(:a AS jsonb),pending=false,bootstrap_pending=false WHERE '+WHERE),
                          {**job,'a':json.dumps(job['active'])})
            jobs.append(job)
        return sorted(jobs,key=lambda j:(j['subject'],j['variant']))


def claim_tables(sources,entry,limit):
    """Batch adjacent monthly ranges into one bounded merge, not one file per symbol.

    Active rows sort first so a sealed merge resumes the identical claim set.
    A fixed maximum of 64 months bounds acquisition independently of history.
    """
    with sources.engine.begin() as c:
        has_active=c.execute(text('SELECT EXISTS(SELECT 1 FROM data_store_source_ranges WHERE source=:source AND dataset=:dataset AND active IS NOT NULL)'),_params(entry)).scalar_one()
        rows=c.execute(text('SELECT source,dataset,subject,variant,range_key,pending,bootstrap_pending,active '
            "FROM data_store_source_ranges WHERE source=:source AND dataset=:dataset AND (active->'blocked' IS NULL OR EXISTS(SELECT 1 FROM tonghuashun_collection_states s WHERE s.dataset=data_store_source_ranges.dataset AND s.subject=data_store_source_ranges.subject AND s.variant=data_store_source_ranges.variant AND s.status='succeeded')) "
            'AND (:active=false OR active IS NOT NULL) '
            "ORDER BY CASE WHEN :active THEN '-infinity'::timestamptz ELSE enqueued_at END,range_key,subject,variant LIMIT :n FOR UPDATE"),
            {**_params(entry),'n':64 if has_active else limit,'active':has_active}).mappings().all()
        jobs=[]
        for record in rows:
            job=dict(record)
            if job['active'] is None:
                job['active']={'id':uuid4().hex,'bootstrap':job['bootstrap_pending'],'when':datetime.now(timezone.utc).isoformat()}
                c.execute(text('UPDATE data_store_source_ranges SET active=CAST(:a AS jsonb),pending=false,bootstrap_pending=false WHERE '+WHERE),
                          {**job,'a':json.dumps(job['active'])})
            jobs.append(job)
        return sorted(jobs,key=lambda j:(j['range_key'],j['subject'],j['variant']))


def existing_claims(sources,entry):
    """Read the ordinary active batch without claiming a producer's pending work.

    Exact source-selection validation detects any different eligible claim set.
    Failed ranges keep their existing blocked state, and recovered ranges are
    not silently unblocked just to make an operator continuation pass its fence.
    """
    batch=entry.id in ('E23','E44') or entry.source=='tushare'
    order=('range_key,subject,variant' if entry.source=='tushare' else
           'subject,variant' if batch else 'enqueued_at,subject,variant,range_key')
    with sources.engine.connect() as c:
        rows=c.execute(text('SELECT source,dataset,subject,variant,range_key,pending,bootstrap_pending,active,lower_at::text AS lower_text '
            'FROM data_store_source_ranges WHERE source=:source AND dataset=:dataset AND active IS NOT NULL '
            "AND (active->'blocked' IS NULL OR EXISTS(SELECT 1 FROM tonghuashun_collection_states s WHERE s.dataset=data_store_source_ranges.dataset AND s.subject=data_store_source_ranges.subject AND s.variant=data_store_source_ranges.variant AND s.status='succeeded')) "
            'ORDER BY '+order+' LIMIT :n'),{**_params(entry),'n':64 if batch else 1}).mappings().all()
    return [dict(row) for row in rows]


def acknowledge(sources,job,*,after=None):
    """Never clear a producer's pending bit, even when no newer head exists."""
    with sources.engine.begin() as c:
        current=c.execute(text('SELECT active,pending FROM data_store_source_ranges WHERE '+WHERE+' FOR UPDATE'),job).mappings().one()
        if current['active']['id']!=job['active']['id']:raise DataStoreError('SOURCE_CONFLICT')
        if after is not None:
            active=dict(current['active'],after=after)
            c.execute(text('UPDATE data_store_source_ranges SET active=CAST(:a AS jsonb) WHERE '+WHERE),
                      {**job,'a':json.dumps(active)})
        elif current['pending']:
            c.execute(text('UPDATE data_store_source_ranges SET active=NULL WHERE '+WHERE),job)
        else:
            c.execute(text('DELETE FROM data_store_source_ranges WHERE '+WHERE),job)


class RangeSources:
    """A stable, bounded input for the existing current merger, never full proof."""
    incremental_slice=True

    def __init__(self,native,entry,job,observation=None,jobs=None):
        self.native,self.entry,self.job,self.observation=native,entry,job,observation
        self.jobs=jobs or [job]
        self.summary={};self.snapshot_scope=None
        self.metrics={'metadata_rows':1,'payload_rows':0,'payload_bytes':0,
                      'dependency_rows':0,'dependency_bytes':0,'decoded_rows':0,'decode_calls':0}
        self.boundary=digest([[j['active']['id'] for j in self.jobs],observation])
        if entry.source=='tushare':
            self.snapshot_scope=[{'subject':j['subject'],'month':j['range_key']} for j in self.jobs]

    def selection_key(self):return digest([self.native.selection_key(),self.boundary])

    def iter_entry(self,entry):
        self.summary={'complete':False,'source_rows':0,'scan_policy':'transactional_native_range'}
        with self.native._snapshot() as (c,_):
            if entry.source=='tushare':
                table=TABLES[entry.native][0]
                for job in self.jobs:
                    start=date.fromisoformat(job['range_key']+'-01')
                    end=date(start.year+int(start.month==12),1 if start.month==12 else start.month+1,1)
                    # The existing PK (source,ts_code,trade_date) bounds this read.
                    for row in self.native._rows(c,table,
                        'WHERE source=:source AND ts_code=:subject AND trade_date>=:start AND trade_date<:end',
                        {**job,'start':start.isoformat(),'end':end.isoformat()},'ORDER BY trade_date'):
                        self.metrics['payload_rows']+=1;self.metrics['decoded_rows']+=1;self.metrics['decode_calls']+=1
                        self.metrics['payload_bytes']+=len(native_json(row).encode())
                        self.summary['source_rows']+=1
                        yield LocalInput('tushare',entry.native,job['subject'],'default',job['active']['when'],
                                         row,digest(row),order_kind='current_table_snapshot',representation='native_table')
                self.summary['snapshot_representation']='tushare:'+digest([entry.native,'default'])[:24]
            else:
                cache={}
                def lookup(identity,dependency=True):
                    if identity in cache:return cache[identity]
                    encoded=c.execute(text('SELECT CASE WHEN octet_length(row_to_json(t)::text)<=:n '
                         'THEN row_to_json(t)::text ELSE NULL END FROM tonghuashun_observations t WHERE id=:id'),
                         {'id':UUID(identity),'n':self.native.limits.payload_bytes}).scalar_one_or_none()
                    if encoded is None:raise NativeInputError('SOURCE_DEPENDENCY_MISSING','原生观察或必要依赖缺失。')
                    value=_json(encoded,self.native.limits.payload_bytes);cache[identity]=value
                    prefix='dependency' if dependency else 'payload'
                    self.metrics[prefix+'_rows']+=1;self.metrics[prefix+'_bytes']+=len(encoded.encode())
                    return value
                row=lookup(self.observation['id'],False)
                body,basis,field=materialize_observation(row,lookup,self.native.limits,metrics=self.metrics)
                tracker=EffectiveBasis()
                # One immediate predecessor establishes sparse equal-value
                # confirmation across full re-anchors. Necessary delta ancestors
                # are fetched by primary key and hash checked, never a scope replay.
                requests=_requests(row,self.native.limits)
                previous=None
                # Complete returned-key evidence grants its own confirmation;
                # an unrelated preceding full anchor is not a dependency.
                if not all(_confirmed(field,r.get(field),requests) for r in body.get('item',[])):
                    previous=c.execute(text('SELECT id::text FROM tonghuashun_observations '
                        'WHERE dataset=:dataset AND subject=:subject AND variant=:variant '
                        'AND (observed_at,id)<(CAST(:at AS timestamptz),CAST(:id AS uuid)) '
                        'ORDER BY observed_at DESC,id DESC LIMIT 1'),{**self.job,**self.observation}).scalar_one_or_none()
                    self.metrics['metadata_rows']+=1
                old_items={}
                if previous:
                    old=lookup(previous);oldbody,oldbasis,oldfield=materialize_observation(old,lookup,self.native.limits,metrics=self.metrics)
                    tracker.apply(old,oldbody,oldbasis,oldfield,_requests(old,self.native.limits))
                    old_items={str(r.get(field)):r for r in oldbody.get('item',[])}
                requests=_requests(row,self.native.limits)
                basis,uncertain=tracker.apply(row,body,basis,field,requests)
                # Inherited equal values that were not returned need no new
                # normalization. Ambiguous legacy confirmation stays restricted.
                items=[r for r in body.get('item',[]) if entry.native not in _MERGED or str(r.get(field)) in uncertain or
                       old_items.get(str(r.get(field)))!=r or _confirmed(field,r.get(field),requests)]
                body=dict(body,item=items)
                self.summary['source_rows']=1
                yield LocalInput('tonghuashun',entry.native,row['subject'],row['variant'],row['observed_at'],body,
                    digest([_scope(row),row['content_hash'],row['observed_at'],requests]),
                    row_basis=basis,basis_field=field,unconfirmed_keys=uncertain)
        self.summary['complete']=True


class FundBatchSources:
    """Reuse complete source objects in a finite batch, never physical row slices."""
    incremental_slice=True
    snapshot_scope=None

    def __init__(self,native,entry,pairs):
        self.native,self.entry,self.pairs=native,entry,pairs
        self.summary={}
        self.metrics={'metadata_rows':0,'payload_rows':0,'payload_bytes':0,
                      'dependency_rows':0,'dependency_bytes':0,'decoded_rows':0,'decode_calls':0}

    def selection_key(self):
        return digest([self.native.selection_key(),[(j['active']['id'],o) for j,o in self.pairs]])

    def iter_entry(self,entry):
        self.summary={'complete':False,'source_rows':0,'scan_policy':'transactional_native_object_batch'}
        for job,observation in self.pairs:
            segment=RangeSources(self.native,entry,job,observation)
            try:
                for raw in segment.iter_entry(entry):
                    self.summary['source_rows']+=1
                    yield raw
            finally:
                for key,value in segment.metrics.items():self.metrics[key]+=value
            if not segment.summary.get('complete'):raise NativeInputError('LOCAL_SCAN_INCOMPLETE','基金原生范围尚未完整读取。')
        self.summary['complete']=True


def next_observation(sources,entry,job):
    a=job['active']
    if not a['stop']:return None
    with sources.engine.connect() as c:
        row=c.execute(text('SELECT observed_at::text AS at,id::text AS id FROM tonghuashun_observations '
            'WHERE dataset=:dataset AND subject=:subject AND variant=:variant '
            'AND observed_at>=CAST(:lower AS timestamptz) '
            'AND (observed_at,id)>(CAST(:after_at AS timestamptz),CAST(:after_id AS uuid)) '
            'AND (observed_at,id)<=(CAST(:stop_at AS timestamptz),CAST(:stop_id AS uuid)) '
            'ORDER BY observed_at,id LIMIT 1'),{**job,'lower':a['lower'],
                'after_at':a['after']['at'] if a['after'] else '-infinity',
                'after_id':a['after']['id'] if a['after'] else ZERO,
                'stop_at':a['stop']['at'],'stop_id':a['stop']['id']}).mappings().first()
        return dict(row) if row else None


def check_state(sources,entry,job):
    with sources.engine.begin() as c:
        state=c.execute(text("SELECT status,to_jsonb(t)->>'revision' AS revision FROM tonghuashun_collection_states t "
            'WHERE dataset=:dataset AND subject=:subject AND variant=:variant'),job).mappings().first()
        if state and state['status'] in ('failed','partial'):
            current=c.execute(text('SELECT active FROM data_store_source_ranges WHERE '+WHERE+' FOR UPDATE'),job).scalar_one()
            if current['id']!=job['active']['id']:raise DataStoreError('SOURCE_CONFLICT')
            active=dict(current,blocked={'code':'SOURCE_REFRESH_FAILED','source_revision':state['revision']})
            c.execute(text('UPDATE data_store_source_ranges SET active=CAST(:a AS jsonb) WHERE '+WHERE),
                      {**job,'a':json.dumps(active)})
            return False
    return True


def next_batch(sources,entry,job,limit=32):
    """Bound captured observation metadata; values are streamed one at a time.

    Reducing one source's remaining observations before the partition commit
    avoids rewriting every monthly file once per historical observation. It
    does not infer returned keys, reorder source authority or replay other scopes.
    """
    a=job['active']
    if not a['stop']:return []
    with sources.engine.connect() as c:
        rows=c.execute(text('SELECT observed_at::text AS at,id::text AS id FROM tonghuashun_observations '
            'WHERE dataset=:dataset AND subject=:subject AND variant=:variant '
            'AND observed_at>=CAST(:lower AS timestamptz) '
            'AND (observed_at,id)>(CAST(:after_at AS timestamptz),CAST(:after_id AS uuid)) '
            'AND (observed_at,id)<=(CAST(:stop_at AS timestamptz),CAST(:stop_id AS uuid)) '
            'ORDER BY observed_at,id LIMIT :limit'),{**job,'lower':a['lower'],
                'after_at':a['after']['at'] if a['after'] else '-infinity',
                'after_id':a['after']['id'] if a['after'] else ZERO,
                'stop_at':a['stop']['at'],'stop_id':a['stop']['id'],'limit':limit}).mappings().all()
    return [dict(r) for r in rows]


def seed_current(store,entry,sources):
    """Only explicit/bootstrap reconciliation reads current range identities.

    Include empty native months retained in current after unsupported manual
    writes. Ordinary update never walks these files. Pagination uses the normal
    pinned, checked current reader; a concurrent direct commit invalidates its
    cursor instead of turning an inconsistent enumeration into deletion proof.
    """
    if entry.source!='tushare':return
    from .readers import Query
    with store.catalog.transaction() as c:
        parts=[r[0] for r in c.execute(text('SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:d'),{'d':entry.spec.name})]
    for part in parts:
        cursor=None
        while True:
            page=store.read(entry.spec,Query(partitions=(part,),columns=('representation','subject','object_key'),
                                           page_size=1000,cursor=cursor,require_qualified=False))
            scopes={(r['subject'],r['object_key'][:7]) for r in page.rows
                    if r['representation']=='tushare:'+digest([entry.native,'default'])[:24]}
            with sources.engine.begin() as c:
                for subject,month in scopes:
                    c.execute(text("INSERT INTO data_store_source_ranges(source,dataset,subject,variant,range_key,bootstrap_pending) "
                        "VALUES (:source,:dataset,:subject,'default',:month,true) "
                        "ON CONFLICT(source,dataset,subject,variant,range_key) DO UPDATE SET pending=true,bootstrap_pending=true,lower_at='-infinity'"),
                        {**_params(entry),'subject':subject,'month':month})
            if not page.next_cursor:break
            cursor=page.next_cursor


def _accumulate(totals,result,segment):
    """Include work before budget/cancel exits, not only the final small slice."""
    for key in ('source_rows','normalized_units','input_failures','passes','committed_partitions','new_issues','resolved'):
        totals[key]+=result.get(key,0)
    for key,value in result['metrics'].items():
        totals['metrics'][key]=(max(totals['metrics'].get(key,0),value) if key.endswith('peak_bytes')
                                 else totals['metrics'].get(key,0)+value)
    for key,value in segment.metrics.items():totals['source_metrics'][key]+=value
    totals['source_scanned']|=result.get('source_scanned',False)
    totals['work_admitted']=result.get('work_admitted',False)


def run(store,entry,sources,*,options,cancelled=None):
    from .pipeline import (_run_entry_locked,read_entry_status,_status,
                           continuation_refused,validate_sealed_continuation)
    from .change_capture import seed
    if not available(sources.engine,entry):
        raise NativeInputError('SOURCE_INCREMENTAL_NOT_INITIALIZED','请先运行正常数据库迁移以启用本地行情变化捕获。')
    old=read_entry_status(store,entry.id)
    contract=digest(entry.spec.descriptor())
    if options.resume_sealed_only and (old.get('incremental',{}).get('contract')!=contract or
            'pipeline.'+entry.id not in store.budget.pending_keys()):
        # A fenced continuation may not seed a fresh queue or register a new
        # source contract. It starts only from the already captured work.
        raise continuation_refused('SEALED_CONTINUATION_REQUIRED')
    store.register(entry.spec,prepare_rebuild=options.allow_incompatible_rebuild)
    if old.get('incremental',{}).get('contract')!=contract:
        # A new store/rule must enumerate source metadata even if a previous
        # consumer exhausted the shared queue. No business payload is copied.
        with sources.engine.begin() as c:seed(c,entry.source,entry.native)
        seed_current(store,entry,sources)
        old['incremental']={'version':1,'contract':contract,'bootstrap_boundary':uuid4().hex}
        _status(store,entry,old)
    # Rebuild is an explicit metadata reconciliation. Continuations retain the
    # marker until all ranges are exhausted, including across process restarts.
    explicit_reconcile=(options.mode=='rebuild' or
                        options.mode=='retry' and bool(store.catalog.issues(entry.spec.name,limit=1)))
    if explicit_reconcile and not old.get('incremental',{}).get('reconciling'):
        with sources.engine.begin() as c:seed(c,entry.source,entry.native)
        seed_current(store,entry,sources)
        old['incremental']={'reconciling':True,'contract':contract,'bootstrap_boundary':uuid4().hex};_status(store,entry,old)
    store.register(entry.spec,prepare_rebuild=options.allow_incompatible_rebuild)
    totals={'entry_id':entry.id,'mode':options.mode,'scope':'native_incremental','state':'running',
        'complete':False,'qualified':False,'source_rows':0,'normalized_units':0,'input_failures':0,
        'passes':0,'committed_partitions':0,'new_issues':0,'resolved':0,'metrics':{'files_written':0},
        'source_metrics':{'metadata_rows':0,'payload_rows':0,'payload_bytes':0,'dependency_rows':0,
                          'dependency_bytes':0,'decoded_rows':0,'decode_calls':0},'source_scanned':False}
    try:
        for _ in range(options.claim_batches):
            if cancelled and cancelled():raise DataStoreError('OPERATION_CANCELLED')
            fund_batch=entry.id in ('E23','E44')
            jobs=(existing_claims(sources,entry) if options.resume_sealed_only else
                  claim_funds(sources,entry,min(64,options.claim_batches)) if fund_batch else
                  claim_tables(sources,entry,min(64,options.claim_batches)) if entry.source=='tushare' else [claim(sources,entry)])
            totals['source_metrics']['metadata_rows']+=len(jobs)
            if not jobs or jobs[0] is None:
                if options.resume_sealed_only:raise continuation_refused('SOURCE_CONFLICT')
                break
            job=jobs[0]
            if fund_batch:
                pairs=[]
                for candidate in jobs:
                    item=next_observation(sources,entry,candidate)
                    if item is None:
                        if options.resume_sealed_only:raise continuation_refused('SOURCE_CONFLICT')
                        if check_state(sources,entry,candidate):acknowledge(sources,candidate)
                    else:pairs.append((candidate,item))
                if not pairs:continue
                jobs=[j for j,_ in pairs];job=jobs[0]
                observation={'batch':[o for _,o in pairs]}
                segment=FundBatchSources(sources,entry,pairs)
            else:
                observation=next_observation(sources,entry,job) if entry.source=='tonghuashun' else None
                if entry.source=='tonghuashun' and observation is None:
                    if options.resume_sealed_only:raise continuation_refused('SOURCE_CONFLICT')
                    if check_state(sources,entry,job):acknowledge(sources,job)
                    continue
                marker=read_entry_status(store,entry.id)
                legacy=('pipeline.'+entry.id in store.budget.pending_keys() and
                        isinstance(marker.get('incremental',{}).get('active_observation'),dict) and
                        'id' in marker['incremental']['active_observation'])
                if entry.source=='tonghuashun' and not legacy:
                    pairs=[(job,o) for o in next_batch(sources,entry,job)]
                    observation={'batch':[o for _,o in pairs]}
                    segment=FundBatchSources(sources,entry,pairs)
                else:segment=RangeSources(sources,entry,job,observation,jobs=jobs)
            if options.resume_sealed_only:
                # Validate the seal before changing the active-observation
                # marker. The ordinary merger repeats this guard under quota.
                sealed_progress=validate_sealed_continuation(store,entry,segment,options,cancelled=cancelled)
            marker=read_entry_status(store,entry.id)
            marker['incremental']=dict(old.get('incremental',{}),version=1,contract=contract,
                active_range={k:job[k] for k in ('source','dataset','subject','variant','range_key')},
                active_observation=observation)
            _status(store,entry,marker)
            try:
                result=_run_entry_locked(store,entry,segment,options=options,cancelled=cancelled)
            except BaseException:
                _accumulate(totals,read_entry_status(store,entry.id),segment)
                raise
            _accumulate(totals,result,segment)
            if options.resume_sealed_only:
                totals['sealed_continuation']={
                    'complete':bool(result.get('complete')),
                    'input_identity':options.expected_input_identity,
                    'source_selection':options.expected_source_selection,
                    'claim_batches':1,'partition_passes':result.get('passes',0),
                    'last_partition':result.get('last_partition'),
                    # This attempt decodes zero input. Report the sealed
                    # batch's original disposition counts separately rather
                    # than presenting zero new failures as a clean input.
                    'source_rows':sealed_progress.get('source_rows',0),
                    'normalized_units':sealed_progress.get('normalized_units',0),
                    'input_failures':sealed_progress.get('input_failures',0),
                }
            if not result.get('complete'):break
            # This is intentionally after the file/catalog commit. A process exit
            # before this acknowledgement repeats only this unit, idempotently.
            if fund_batch or isinstance(segment,FundBatchSources):
                # Each scope advances once to the last actually committed item.
                final={}
                for done,item in pairs:final[(done['subject'],done['variant'])]=(done,item)
                for done,item in final.values():acknowledge(sources,done,after=item)
            else:
                for done in jobs:acknowledge(sources,done,after=observation)
            if options.resume_sealed_only:
                # Completing this sealed batch does not authorize another
                # claim, even if new producer commits arrived during the run.
                break
    except BaseException as error:
        # A finite pass may have committed many independent ranges. Preserve
        # their measured work even when the final range is interrupted.
        try:
            latest=read_entry_status(store,entry.id)
            for key in ('full_coverage','last_complete_coverage','coverage_pending','incremental','overflow_restriction'):
                if key in latest:totals[key]=latest[key]
            totals.update(state='incomplete',complete=False,qualified=False,reason=getattr(error,'code','INTERRUPTED'))
            _status(store,entry,totals)
        except Exception:
            pass  # A catalog outage must not replace a process exit signal.
        raise
    with sources.engine.connect() as c:
        remaining,bootstrap_remaining,blocked=c.execute(text("SELECT count(*),count(*) FILTER(WHERE bootstrap_pending OR coalesce((active->>'bootstrap')::boolean,false)),count(*) FILTER(WHERE active->'blocked' IS NOT NULL) FROM data_store_source_ranges WHERE source=:source AND dataset=:dataset"),_params(entry)).one()
    latest=read_entry_status(store,entry.id)
    totals['incremental']={'version':1,'contract':contract,'remaining_ranges':remaining,
                           'reconciling':bool(old.get('incremental',{}).get('reconciling') and remaining),
                           'bootstrap_complete':bootstrap_remaining==0,
                           'bootstrap_boundary':old.get('incremental',{}).get('bootstrap_boundary'),
                           'bootstrap_remaining_ranges':bootstrap_remaining,'blocked_ranges':blocked}
    for key in ('full_coverage','last_complete_coverage','coverage_pending','overflow_restriction'):
        if key in latest:totals[key]=latest[key]
    totals['complete']=remaining==0
    totals['qualified']=not blocked and not store.catalog.issues(entry.spec.name,limit=1)
    totals['state']='processed' if totals['complete'] else 'incomplete'
    if not totals['complete']:totals['reason']='SOURCE_RANGES_BLOCKED' if remaining==blocked else 'NATIVE_RANGES_PENDING'
    if not remaining or (not old.get('incremental',{}).get('bootstrap_complete') and not bootstrap_remaining):
        # Every seeded native range and transactionally captured change up to
        # this boundary was actually exhausted. Output metadata describes the
        # completed range, but is never the source of completion evidence.
        with store.catalog.transaction() as c:
            parts=[r[0] for r in c.execute(text('SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:d ORDER BY partition_key'),{'d':entry.spec.name})]
        h=hashlib.sha256()
        for part in parts:h.update(digest(part).encode())
        previous=totals.get('full_coverage',{})
        proof={'version':1,'entry_id':entry.id,'contract':digest(entry.spec.descriptor()),
            'source_selection':sources.selection_key(),'source_kind':digest([type(sources).__module__,type(sources).__qualname__]),
            'boundary':digest(['native-ranges-v1',totals['incremental']['bootstrap_boundary'],previous.get('boundary'),totals['source_rows']]),
            'boundary_kind':'exhausted_transactional_ranges','range_digest':h.hexdigest(),
            'expected':len(parts),'end':parts[-1] if parts else '', 'after':parts[-1] if parts else '',
            'scan_complete':True,'complete':True,'remaining':0,
            'generation':store.catalog.dataset(entry.spec.name)['generation']}
        pending=[] if (not old.get('incremental',{}).get('bootstrap_complete') or
                       old.get('incremental',{}).get('reconciling')) else [
                       p for p in totals.get('coverage_pending',[]) if p!='native_ranges']
        if remaining:pending=sorted(set(pending)|{'native_ranges'})
        totals.update(full_coverage=proof,last_complete_coverage=proof,coverage_pending=pending)
    sources.summary=dict(totals)
    _status(store,entry,totals)
    return totals
