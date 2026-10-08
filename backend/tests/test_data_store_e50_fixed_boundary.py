"""Exact finite E50 bootstrap acceptance on isolated PostgreSQL and ext4."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta, timezone
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from tests.test_data_store_incremental import ready, publish, update, obj, files, NOW
from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_domain_samples import sample
from app.data_store import incremental, pipeline
from app.data_store.adapters.registry import BY_ID
from app.data_store.adapters.contracts import digest
from app.data_store.change_capture import seed
from app.data_store.errors import DataStoreError
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import PipelineOptions, read_entry_status


def queue(st):
    with st.catalog.engine.connect() as c:
        return [dict(r) for r in c.execute(text('SELECT source,dataset,subject,variant,range_key,pending,'
            'bootstrap_pending,enqueued_at,lower_at::text AS lower_at,active FROM data_store_source_ranges ORDER BY subject')).mappings()]


def manifest(st,subjects=('000001.SZ',),counts=None):
    entry=BY_ID['E50'];contract=digest(entry.spec.descriptor());boundary=uuid4().hex
    with st.catalog.engine.begin() as c:seed(c,entry.source,entry.native)
    st.register(entry.spec)
    pipeline._status(st,entry,{'entry_id':'E50','incremental':{'version':1,'contract':contract,
        'bootstrap_boundary':boundary,'bootstrap_complete':False}})
    scopes=[]
    with st.catalog.engine.connect() as c:
        for subject in subjects:
            rows=[dict(r) for r in c.execute(text('SELECT observed_at AS at,id::text,content_hash,row_count,'
                "chain_depth,base_observation_id::text,encode(sha256(convert_to(request_json,'UTF8')),'hex') AS request_sha256 "
                'FROM tonghuashun_observations WHERE dataset=:dataset AND subject=:subject ORDER BY observed_at,id'),
                {'dataset':entry.native,'subject':subject}).mappings()]
            if counts:rows=rows[:counts[subject]]
            for row in rows:row['at']=row['at'].astimezone(timezone.utc).isoformat()
            scopes.append({'subject':subject,'variant':'default','range_key':'*','lower':'-infinity',
                'stop':{k:rows[-1][k] for k in ('at','id')},'observation_count':len(rows),'metadata_sha256':digest(rows)})
    return {'version':1,'entry_id':entry.id,'source':entry.source,'dataset':entry.native,
        'contract':contract,'bootstrap_boundary':boundary,'candidates':scopes}


def options(fence,**kwargs):
    return PipelineOptions(e50_input_fence=json.dumps(fence),maximum_claim_batches=2,
        maximum_partition_passes=64,**kwargs)


def publish_many(st,n=22,subject=None):
    body=deepcopy(sample('E50').content)
    if subject:
        body['thscode']=subject
        for row in body['item']:row['thscode']=subject
    for i in range(n):publish(st.catalog.engine,body=body,now=NOW+timedelta(seconds=i),subject=subject)


def test_original22_plus_eof_retains23_then_default_consumes_tail(ready):
    publish_many(ready);fence=manifest(ready)
    later=deepcopy(sample('E50').content);later['item'][0]['close_price']='10.75'
    publish(ready.catalog.engine,body=later,now=NOW+timedelta(seconds=22))
    result=update(ready,options=options(fence))
    assert result['source_metrics']['payload_rows']==22 and result['fixed_input_fence']['complete']
    row=queue(ready)[0]
    assert row['active'] is None and row['pending'] and not row['bootstrap_pending']
    assert incremental.datetime.fromisoformat(row['lower_at'])==NOW+timedelta(seconds=22)
    assert obj(ready)['data']['reported_close']=='10.5'
    before=files(ready,'E50');generation=ready.catalog.dataset(BY_ID['E50'].spec.name)['generation']
    # Reusing a completed bootstrap manifest never claims its ordinary tail.
    repeated=update(ready,options=options(fence))
    assert repeated['source_metrics']['payload_rows']==0
    assert files(ready,'E50')==before and ready.catalog.dataset(BY_ID['E50'].spec.name)['generation']==generation
    ordinary=update(ready)
    assert ordinary['source_metrics']['payload_rows']==1 and not queue(ready)
    assert obj(ready)['data']['reported_close']=='10.75'


def test_concurrent_append_after_claim_and_before_eof_survives(ready,monkeypatch):
    publish_many(ready);fence=manifest(ready)
    original=incremental.acknowledge;changed=False
    def append(sources,job,**kwargs):
        nonlocal changed
        if kwargs.get('after') is not None and not changed:
            changed=True;publish(ready.catalog.engine,now=NOW+timedelta(seconds=22))
        return original(sources,job,**kwargs)
    monkeypatch.setattr(incremental,'acknowledge',append)
    result=update(ready,options=options(fence))
    assert result['source_metrics']['payload_rows']==22 and result['fixed_input_fence']['complete']
    assert queue(ready)[0]['pending'] and queue(ready)[0]['active'] is None
    assert update(ready)['source_metrics']['payload_rows']==1


def test_existing_tail_and_concurrent_earlier_tail_coalesce(ready,monkeypatch):
    publish_many(ready);fence=manifest(ready)
    publish(ready.catalog.engine,now=NOW+timedelta(seconds=24))
    original=incremental.acknowledge;changed=False
    def append(sources,job,**kwargs):
        nonlocal changed
        if kwargs.get('after') is not None and not changed:
            changed=True;publish(ready.catalog.engine,now=NOW+timedelta(seconds=23))
        return original(sources,job,**kwargs)
    monkeypatch.setattr(incremental,'acknowledge',append)
    assert update(ready,options=options(fence))['source_metrics']['payload_rows']==22
    assert incremental.datetime.fromisoformat(queue(ready)[0]['lower_at'])==NOW+timedelta(seconds=23)
    assert update(ready)['source_metrics']['payload_rows']==2


def test_equal_timestamp_uuid_exact_stop_and_pending_tie(ready):
    for _ in range(3):publish(ready.catalog.engine,now=NOW)
    # Exact UUID ordering defines two originals and a same-time tail, rather
    # than widening the frozen boundary to all observations at this timestamp.
    fence=manifest(ready,counts={'000001.SZ':2})
    result=update(ready,options=options(fence))
    assert result['source_metrics']['payload_rows']==2 and result['fixed_input_fence']['complete']
    assert incremental.datetime.fromisoformat(queue(ready)[0]['lower_at'])==NOW and queue(ready)[0]['pending']
    assert update(ready)['source_metrics']['payload_rows']==3
    assert not queue(ready)  # ordinary timestamp overlap remains conservative


@pytest.mark.parametrize('point',['before_commit','after_commit','after_cursor'])
def test_crash_resume_retains_exact_identity_and_post_stop(ready,monkeypatch,point):
    publish_many(ready);fence=manifest(ready)
    publish(ready.catalog.engine,now=NOW+timedelta(seconds=22))
    target=pipeline if point=='before_commit' else incremental
    name='_commit_partition' if point=='before_commit' else 'acknowledge'
    original=getattr(target,name)
    def interrupted(*args,**kwargs):
        if point=='after_cursor' and kwargs.get('after') is not None:original(*args,**kwargs)
        raise SystemExit(73)
    monkeypatch.setattr(target,name,interrupted)
    with pytest.raises(SystemExit):update(ready,options=options(fence))
    row=queue(ready)[0];claim=row['active']['id']
    assert row['pending'] and row['active']['bootstrap'] and row['active']['stop']==fence['candidates'][0]['stop']
    refs=files(ready,'E50');generation=ready.catalog.dataset(BY_ID['E50'].spec.name)['generation']
    monkeypatch.setattr(target,name,original)
    r=update(ready,options=options(fence))
    assert r['fixed_input_fence']['complete'] and queue(ready)[0]['active'] is None
    assert queue(ready)[0]['pending']
    if point!='before_commit':
        assert files(ready,'E50')==refs
        assert ready.catalog.dataset(BY_ID['E50'].spec.name)['generation']==generation


@pytest.mark.parametrize('field',['contract','bootstrap_boundary','metadata_sha256','stop','foreign_active','foreign_entry'])
def test_wrong_identity_refuses_before_queue_status_or_files_change(ready,field):
    publish_many(ready,2);fence=manifest(ready)
    if field=='foreign_active':
        incremental.claim(NativeSources(ready.catalog.engine),BY_ID['E50'])
    elif field in ('contract','bootstrap_boundary'):fence[field]='0'*len(fence[field])
    elif field=='metadata_sha256':fence['candidates'][0][field]='0'*64
    elif field=='stop':fence['candidates'][0]['stop']['id']=str(uuid4())
    before=queue(ready);status=read_entry_status(ready,'E50');refs=files(ready,'E50')
    with pytest.raises(DataStoreError) as caught:
        update(ready,'E51' if field=='foreign_entry' else 'E50',options=options(fence))
    assert caught.value.code=='SOURCE_CONFLICT'
    assert queue(ready)==before and read_entry_status(ready,'E50')==status and files(ready,'E50')==refs


def test_bootstrap_prefix_moves_past_retained_tail_without_claiming_it(ready):
    publish_many(ready,2);publish_many(ready,2,subject='000002.SZ')
    fence=manifest(ready,subjects=('000001.SZ','000002.SZ'))
    publish(ready.catalog.engine,now=NOW+timedelta(seconds=22))
    opts=replace(options(fence),maximum_claim_batches=4)
    result=update(ready,options=opts)
    assert result['source_metrics']['payload_rows']==4 and result['fixed_input_fence']['complete']
    assert [(r['subject'],r['bootstrap_pending']) for r in queue(ready)]==[('000001.SZ',False)]


def test_manifest_cannot_bypass_an_earlier_unfinished_bootstrap(ready):
    publish_many(ready,2);publish_many(ready,2,subject='000002.SZ')
    fence=manifest(ready,subjects=('000002.SZ',));before=queue(ready)
    with pytest.raises(DataStoreError):update(ready,options=options(fence))
    assert queue(ready)==before and not files(ready,'E50')


def test_fixed_business_rows_match_ordinary_frozen_originals(ready):
    # Preserve every finite object's value, current basis and quality/state.
    # A second subject supplies the identical original source body under the
    # ordinary consumer, while a changed future input stays pending on subject1.
    from app.data_store.readers import Query
    publish_many(ready,3)
    assert update(ready)['complete']
    expected=ready.read(BY_ID['E50'].spec,Query(require_qualified=False)).rows
    reference=obj(ready)
    fence=manifest(ready)
    later=deepcopy(sample('E50').content);later['item'][0]['close_price']='9.5'
    publish(ready.catalog.engine,body=later,now=NOW+timedelta(seconds=4))
    update(ready,options=options(fence))
    actual=ready.read(BY_ID['E50'].spec,Query(require_qualified=False)).rows
    assert actual==expected  # includes every value, basis, quality and state field
    result=obj(ready)
    for field in ('data','found','field_quality','confirmation','capability'):
        assert result[field]==reference[field]
    assert queue(ready)[0]['pending']


@pytest.mark.parametrize('argv',[
    ['rebuild','--entry','E50'],['update','--entry','E51'],['update'],
    ['update','--entry','E50','--partition','2026-01'],
    ['update','--entry','E50','--resume-sealed-only']])
def test_cli_refuses_foreign_fence_modes_before_opening_store(tmp_path,argv):
    from app.data_store.__main__ import main
    path=tmp_path/'fence.json';path.write_text('{}')
    with pytest.raises(SystemExit) as caught:main([*argv,'--e50-input-fence',str(path)])
    assert caught.value.code==2
