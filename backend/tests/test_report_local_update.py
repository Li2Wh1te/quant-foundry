"""F03 isolated source-shaped checks of unchanged default production code."""
# Initialize registrations through the same import direction as app.main.
import app.main
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import time
import pytest
from sqlalchemy.orm import Session
from tests.test_data_store_kernel import database, store
from tests.test_data_store_incremental import ready
from tests.test_data_store_domain_samples import sample
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_store.adapters.registry import BY_ID
from app.data_store.domain_reader import read_object
from app.data_store.limits import StoreLimits, MiB
from app.data_store.local_sources import NativeSources, SourceLimits
from app.data_store.pipeline import PipelineOptions, run_entry, run_local, read_entry_status
from app.data_store.updates import RetryPolicy

pytestmark=pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED')!='1', reason='isolated PostgreSQL required')

IDS=('E17','E19','E20')
NOW=datetime(2026,1,2,tzinfo=timezone.utc)
FIELDS={'E17':('stock_ratio_pct','reported_stock_ratio_pct','81'),
        'E19':('holder_amount','reported_holder_amount',11),
        'E20':('hold_share','reported_hold_share','101')}

@pytest.fixture
def limits():
    return replace(StoreLimits(),file_rows=10,lock_timeout_ms=500,
                   pipeline_spill_bytes=512*MiB,duckdb_memory_bytes=128*MiB,log_bytes=1*MiB)

def publish(st,entry_id,body=None,*,subject='FUND.SH',when=NOW):
    entry=BY_ID[entry_id]
    with Session(st.catalog.engine) as db,db.begin():
        repo=CollectionRepository(db);old=repo.read(entry.native,subject,'default',with_data=False)
        return repo.publish(entry.native,subject,'default',expected=old.revision,
                            data=deepcopy(sample(entry_id).content if body is None else body),requests=[],now=when)

def invoke(st,entry_id,clock,*,passes=64):
    return run_entry(st,BY_ID[entry_id],NativeSources(st.catalog.engine,limits=SourceLimits(pass_seconds=120)),
                     options=PipelineOptions(maximum_passes=passes,pass_seconds=120,pipeline_spill_bytes=512*MiB),
                     _policy=RetryPolicy(),clock=lambda:clock[0])

@pytest.mark.parametrize('entry_id',IDS)
def test_default_entry_seals_resumes_and_preserves_source_semantics(ready,entry_id):
    started=time.monotonic();clock=[int(time.time())];entry=BY_ID[entry_id]
    for n in range(8):publish(ready,entry_id,subject=f'FUND{n}.SH')
    first=invoke(ready,entry_id,clock,passes=1)
    assert not first['complete'] and first['source_rows']==8
    assert first['refresh']['continuation']['eligible'] is True
    assert 'pipeline.'+entry_id in ready.budget.pending_keys()
    second=invoke(ready,entry_id,clock)
    assert second['complete'] and second['qualified'] and second['resumed']
    assert second['source_rows']==0 and not second.get('source_scanned',False)
    assert 'pipeline.'+entry_id not in ready.budget.pending_keys()
    representation=sample(entry_id).representation_key
    row=read_object(ready,entry_id,representation,'FUND0.SH','2026-01-02')
    assert row['found'] and len(row['data']['members'])==1
    unchanged=invoke(ready,entry_id,clock)
    assert unchanged['complete'] and unchanged['metrics']['files_written']==0

    # A new observation may carry a late business report or correct the same
    # report. Its source order, rather than its business date, owns authority.
    late=deepcopy(sample(entry_id).content);late['item'][0]['report_date_ms']-=86400000
    publish(ready,entry_id,late,subject='FUND0.SH',when=NOW+timedelta(seconds=1))
    clock[0]+=31;assert invoke(ready,entry_id,clock)['complete']
    assert read_object(ready,entry_id,representation,'FUND0.SH','2026-01-01')['found']
    corrected=deepcopy(sample(entry_id).content);source_field,result_field,value=FIELDS[entry_id]
    corrected['item'][0][source_field]=value
    publish(ready,entry_id,corrected,subject='FUND0.SH',when=NOW+timedelta(seconds=2))
    clock[0]+=31;assert invoke(ready,entry_id,clock)['complete']
    before=read_object(ready,entry_id,representation,'FUND0.SH','2026-01-02')
    assert str(before['data']['members'][0][result_field])==str(value)
    publish(ready,entry_id,corrected,subject='FUND0.SH',when=NOW+timedelta(seconds=3))
    clock[0]+=31;assert invoke(ready,entry_id,clock)['complete']
    after=read_object(ready,entry_id,representation,'FUND0.SH','2026-01-02')
    assert after['data']==before['data']
    assert int(after['confirmation']['order'])>int(before['confirmation']['order'])

    with Session(ready.catalog.engine) as db,db.begin():
        repo=CollectionRepository(db);old=repo.read(entry.native,'FUND0.SH','default',with_data=False)
        repo.fail(entry.native,'FUND0.SH','default',expected=old.revision,kind='temporary',now=NOW+timedelta(seconds=4))
    clock[0]+=31;failed=invoke(ready,entry_id,clock)
    assert failed['complete'] and failed['qualified'] is False and failed['input_failures']>0
    backoff=invoke(ready,entry_id,clock)
    assert backoff['reason']=='RETRY_BACKOFF' and backoff['attempted'] is False
    clock[0]+=3600;same=invoke(ready,entry_id,clock)
    assert same['reason']=='SOURCE_INPUT_UNCHANGED' and same['source_rows']==same['committed_partitions']==0
    assert same['refresh']['continuation']['eligible'] is False
    assert read_object(ready,entry_id,representation,'FUND1.SH','2026-01-02')['found']
    print(json.dumps({'entry':entry_id,'scope':'isolated_source_shaped_inputs','elapsed_seconds':time.monotonic()-started,
                      'first':{k:first.get(k) for k in ('source_rows','committed_partitions','reason','metrics')},
                      'resume':{k:second.get(k) for k in ('source_rows','committed_partitions','resumed','metrics')},
                      'same_source_disposition':same['reason'],'production_writes':0,'supplier_requests':0},default=str))

def test_existing_E19_exception_summary_does_not_retire_unfinished_sealed_work(ready):
    from dataclasses import replace
    from app.data_store.adapters.normalize import normalize
    from tests.test_data_store_overflow_read_scope import install_overflow
    entry=BY_ID['E19'];ready.register(entry.spec)
    unit=next(normalize(entry,sample('E19')))
    bad_subject=next('BAD'+str(n)+'.SH' for n in range(256)
                     if entry.spec.partitioner((unit.representation,'BAD'+str(n)+'.SH',unit.object_key,'root')).endswith('b15'))
    proof=install_overflow(ready,entry,replace(unit,subject=bad_subject))
    for n in range(8):publish(ready,'E19',subject=f'FUND{n}.SH')
    with Session(ready.catalog.engine) as db,db.begin():
        repo=CollectionRepository(db);old=repo.read(entry.native,bad_subject,'default',with_data=False)
        repo.fail(entry.native,bad_subject,'default',expected=old.revision,kind='temporary',now=NOW)
    clock=[int(time.time())];first=invoke(ready,'E19',clock,passes=1)
    assert not first['complete'] and first['committed_partitions']>0
    assert 'pipeline.E19' in ready.budget.pending_keys()
    assert first.get('qualified',False) is False and first['overflow_restriction']['sha256']==proof['sha256']
    assert first['refresh']['continuation']['eligible'] is True, first['refresh']
    clock[0]+=31;second=invoke(ready,'E19',clock)
    assert second['resumed'] and second['source_rows']==0 and second['complete']
    assert second['qualified'] is False


@pytest.mark.parametrize('sealed,progress,reason,complete',[(False,1,'PASS_BUDGET_EXCEEDED',False),(True,0,'PASS_BUDGET_EXCEEDED',False),(True,1,'SOURCE_INPUT_UNCHANGED',False),(True,1,'SOURCE_RANGES_BLOCKED',False),(True,1,'PASS_BUDGET_EXCEEDED',True)])
def test_overflow_without_unfinished_forward_seal_stays_blocked(sealed,progress,reason,complete):
    from app.data_store.continuation import disposition
    row={'reason':reason,'complete':complete,'qualified':False,'committed_partitions':progress,'overflow_restriction':{'blocking_objects':1}}
    assert not disposition(row,now=1000,sealed=sealed)['eligible']
