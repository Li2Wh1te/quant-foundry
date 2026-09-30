"""Lossless issue membership and deferred source ranges use synthetic inputs."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_data_store_kernel import database,limits,store
from tests.test_data_store_incremental import ready,publish,update,obj,NOW,DAY
from app.data_store import incremental
from app.data_store.adapters.registry import BY_ID
from app.data_store.adapters.contracts import digest
from app.data_store.adapters.normalize import normalize
from app.data_store.catalog import Issue
from app.data_store.issue_sets import compact_entry,logical_count,members,fingerprint
from app.data_store.issue_scope import relevant_issue_count
from app.data_store.pipeline import _scope,_partition_issues,PipelineOptions
from app.data_store.merge import MergeSpool
from app.data_store.readers import Query
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from tests.test_data_store_domain_samples import sample


def seed_issues(st,n=12):
    e=BY_ID['E50'];u=next(normalize(e,sample('E50')));st.register(e.spec)
    part=e.spec.partitioner((*u.key,'root'));issues=[]
    for i in range(n):
        target={'scope_version':'object-key-v1','partition':part,'prefix':[*u.key[:2],f'2026-01-{i+2:02d}'],
                'group':u.group,'order':str(u.order-1),'blocking':True}
        issues.append(Issue('local.'+digest(target),_scope(part),'SOURCE_CONFIRMATION_UNPROVEN',digest(['token',i]),target,{'retry':'same_local_pipeline'}))
    with st.catalog.transaction() as c:st.catalog.change_issues(c,e.spec.name,issues,{},check=lambda:None)
    return e,u,part


def expanded(st,e):
    return [v for r in st.catalog.issues(e.spec.name,limit=1000) for v in members(r)]


def test_compaction_preserves_every_original_issue_and_file_generation(ready):
    e,u,part=seed_issues(ready)
    before=expanded(ready,e);state=ready.catalog.dataset(e.spec.name)
    result=compact_entry(ready,e)
    after=expanded(ready,e)
    assert result['affected_objects_before']==result['affected_objects_after']==12
    assert result['physical_records_released']==11
    assert fingerprint(before)==fingerprint(after)
    assert ready.catalog.dataset(e.spec.name)==state
    with ready.catalog.transaction() as c:assert logical_count(c,e.spec.name)==12
    assert len(ready.catalog.issues(e.spec.name,limit=1000))==1
    for day,expected in [('2026-01-02',1),('2026-01-13',1),('2026-01-29',0)]:
        q=Query(partitions=(part,),lower=(*u.key[:2],day),upper=(*u.key[:2],day+'\x00'))
        assert relevant_issue_count(ready,e.spec,q)==expected


def test_member_fix_removes_only_proven_object(ready):
    e,u,part=seed_issues(ready);compact_entry(ready,e)
    with ready.budget.reserve('read',pending='pipeline.test') as space:
        spool=MergeSpool(space,e.spec,partition_count=None)
        spool.offer(u)
        additions,resolved=_partition_issues(ready,e,part,spool)
        assert resolved.resolved_objects==1 and resolved.new_objects==0
        with ready.catalog.transaction() as c:ready.catalog.change_issues(c,e.spec.name,additions,resolved,check=lambda:None)
        spool.close();space.discard=True
    with ready.catalog.transaction() as c:assert logical_count(c,e.spec.name)==11
    assert len(expanded(ready,e))==11
    assert all(json.loads(v['target_json'])['prefix'][2]!='2026-01-02' for v in expanded(ready,e))


def test_compaction_failure_rolls_back_all_original_members(ready,monkeypatch):
    e,u,part=seed_issues(ready);before=expanded(ready,e)
    original=ready.catalog.change_issues
    def fail(*a,**kw):original(*a,**kw);raise RuntimeError('synthetic transaction interruption')
    monkeypatch.setattr(ready.catalog,'change_issues',fail)
    with pytest.raises(RuntimeError):compact_entry(ready,e)
    assert fingerprint(expanded(ready,e))==fingerprint(before)


def test_failed_source_stays_pending_while_sibling_runs_and_recovers(ready):
    engine=ready.catalog.engine;publish(engine)
    with Session(engine) as s,s.begin():
        repo=CollectionRepository(s);old=repo.read('stock_daily','000001.SZ','default',with_data=False)
        repo.fail('stock_daily','000001.SZ','default',expected=old.revision,kind='temporary',now=NOW+timedelta(seconds=1))
    body=deepcopy(sample('E50').content);body['thscode']='000002.SZ';body['item'][0]['thscode']='000002.SZ'
    publish(engine,subject='000002.SZ',body=body)
    first=update(ready)
    assert not first['complete'] and first['reason']=='SOURCE_RANGES_BLOCKED'
    assert first['incremental']['blocked_ranges']==1
    assert obj(ready,subject='000002.SZ')['found']
    again=update(ready)
    assert again['source_metrics']['payload_rows']==again['committed_partitions']==0
    with engine.connect() as c:assert c.execute(text("SELECT count(*) FROM data_store_source_ranges WHERE active->'blocked' IS NOT NULL")).scalar_one()==1
    publish(engine,now=NOW+timedelta(seconds=2))
    assert update(ready)['complete']


def test_one_scope_batches_observations_without_file_write_amplification(ready):
    engine=ready.catalog.engine
    for i in range(4):
        body=deepcopy(sample('E50').content);body['item'][0]['close_price']=f'10.{51+i}'
        publish(engine,body=body,now=NOW+timedelta(seconds=i))
    result=update(ready)
    assert result['complete'] and result['source_rows']==4
    assert result['committed_partitions']==1
    assert obj(ready)['data']['reported_close']=='10.54'


def test_verified_table_snapshot_checks_content_without_fabricating_new_order(ready):
    from tests.test_data_store_incremental import bar
    from app.data_store.verify_coverage import verify_existing
    from app.data_store.local_sources import NativeSources
    bar(ready.catalog.engine);update(ready,'E70')
    e=BY_ID['E70'];before=ready.catalog.dataset(e.spec.name)
    assert verify_existing(ready,e,NativeSources(ready.catalog.engine))['complete']
    assert ready.catalog.dataset(e.spec.name)==before
    bar(ready.catalog.engine,close='6')
    assert not verify_existing(ready,e,NativeSources(ready.catalog.engine))['complete']


def test_verified_bootstrap_does_not_erase_concurrent_pending_change(ready):
    from tests.test_data_store_incremental import bar
    from app.data_store.verify_coverage import verify_existing
    from app.data_store.local_sources import NativeSources
    from app.data_store.change_capture import seed
    from app.data_store.coverage import check_coverage
    bar(ready.catalog.engine);update(ready,'E70')
    with ready.catalog.engine.begin() as c:seed(c,'tushare','etf_daily')
    native=NativeSources(ready.catalog.engine);original=native.iter_entry
    def later(entry):
        yield from original(entry)
        bar(ready.catalog.engine,close='6')
    native.iter_entry=later;e=BY_ID['E70']
    assert verify_existing(ready,e,native)['complete']
    with ready.catalog.transaction() as c:
        assert c.execute(text("SELECT count(*) FROM data_store_source_ranges WHERE source='tushare' AND dataset='etf_daily'")).scalar_one()==1
        assert check_coverage(c,e,__import__('app.data_store.pipeline',fromlist=['read_entry_status']).read_entry_status(ready,e.id))['reason']=='CURRENT_INPUT_PENDING'


def test_empty_revision_fence_can_downgrade_and_upgrade(ready,monkeypatch):
    from importlib import import_module
    migration=import_module('app.db.migrations.versions.20261010_01_current_issue_lookup')
    with ready.catalog.engine.begin() as c:
        monkeypatch.setattr(migration.op,'get_bind',lambda:c)
        migration.downgrade()
        assert c.execute(text('SELECT version FROM data_store_capture_version')).scalar_one()==2
        assert not c.execute(text("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='data_store_source_ranges' AND column_name='change_revision')")).scalar_one()
        migration.install(c)
        assert c.execute(text('SELECT version FROM data_store_capture_version')).scalar_one()==3


@pytest.mark.parametrize('kind',['pending_range','issue_members','verification_receipt'])
def test_revision_fence_refuses_downgrade_of_persisted_evidence(ready,monkeypatch,kind):
    from importlib import import_module
    migration=import_module('app.db.migrations.versions.20261010_01_current_issue_lookup')
    if kind=='pending_range':publish(ready.catalog.engine)
    elif kind=='issue_members':
        e,u,part=seed_issues(ready);compact_entry(ready,e)
    else:
        from tests.test_data_store_incremental import bar
        from app.data_store.verify_coverage import verify_existing
        from app.data_store.local_sources import NativeSources
        bar(ready.catalog.engine);update(ready,'E70')
        assert verify_existing(ready,BY_ID['E70'],NativeSources(ready.catalog.engine))['complete']
    with ready.catalog.engine.begin() as c:
        monkeypatch.setattr(migration.op,'get_bind',lambda:c)
        with pytest.raises(RuntimeError,match='contains persisted evidence'):migration.downgrade()
        assert c.execute(text('SELECT version FROM data_store_capture_version')).scalar_one()==3
