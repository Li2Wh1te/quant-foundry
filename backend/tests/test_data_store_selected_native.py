"""Real PostgreSQL selected-input tests; all observations are synthetic."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_store.adapters.contracts import digest
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.coverage import check_coverage
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_entry
from tests.test_data_store_domain_samples import sample
from tests.test_data_store_incremental import ready  # noqa: F401
from tests.test_data_store_kernel import database, limits, store  # noqa: F401

pytestmark=pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED')!='1',
                             reason='isolated PostgreSQL required')
ENTRY=BY_ID['E22']
NOW=datetime(2026,1,2,tzinfo=timezone.utc)
OLD_DAY=1634486400000  # 2021-10-18 00:00 Asia/Shanghai; before every observation.
PARTITION='2021-10.b09'


def partition(subject,variant='default'):
    representation='tonghuashun:'+digest(['stock_actions',variant])[:24]
    return ENTRY.spec.partitioner((representation,subject,'2021-10-18'))


def scopes(variant='default',selected=True):
    values=[]
    for i in range(1000,1200):
        subject=f'{i:06}.SZ'
        if (partition(subject,variant)==PARTITION)==selected:
            values.append((subject,variant))
            if len(values)==2:return values
    raise AssertionError('synthetic identity bucket search failed')


def publish(engine,subject,variant='default',*,revision=0):
    body=deepcopy(sample('E22').content)
    body['ticker']=subject.split('.')[0]
    body['item'][0].update(thscode=subject,ticker=body['ticker'],ex_date_ms=OLD_DAY,
                           dividend_per_share=Decimal('0.2')+Decimal(revision)/100)
    requests=[{'key_receipt':'actual_returned_keys_v1',
               'returned_keys':{'ex_date_ms':[OLD_DAY]},'parameters':{}}]
    with Session(engine) as db,db.begin():
        repo=CollectionRepository(db)
        old=repo.read('stock_actions',subject,variant,with_data=False)
        repo.publish('stock_actions',subject,variant,expected=old.revision,
                     data=body,requests=requests,now=NOW+timedelta(days=revision))


def units(inputs):
    return sorted((unit for raw in inputs for unit in normalize(ENTRY,raw)
                   if ENTRY.spec.partitioner((*unit.key,'root'))==PARTITION),
                  key=lambda unit:(unit.key,unit.order,unit.token))


def test_selected_input_skips_other_payloads_but_keeps_all_history_and_variants(ready,monkeypatch):
    selected=scopes()+scopes('alternative')
    outside=scopes(selected=False)+scopes('alternative',selected=False)
    for subject,variant in selected+outside:
        for revision in (0,1):publish(ready.catalog.engine,subject,variant,revision=revision)
    full=NativeSources(ready.catalog.engine)
    expected=units(list(full.iter_entry(ENTRY)))
    assert len(expected)==len(selected)*2
    source=NativeSources(ready.catalog.engine);original=source._rows;decoded=[]

    def observed(connection,table,*args,**kwargs):
        for row in original(connection,table,*args,**kwargs):
            if table in ('tonghuashun_observations','tonghuashun_collection_states'):
                assert (row['subject'],row['variant']) in selected
                decoded.append((row['subject'],row['variant']))
            yield row

    monkeypatch.setattr(source,'_rows',observed)
    actual=list(source.iter_partitions(ENTRY,(PARTITION,)))
    assert len(actual)==len(selected)*2 and len(decoded)==len(actual)
    assert units(actual)==expected
    assert all(not unit.failure for unit in expected)
    assert source.summary['complete'] and source.summary['source_rows']==len(actual)
    assert source.selection_key()==full.selection_key()


def test_selected_input_retains_failed_refresh_and_state_only_scope(ready):
    selected=scopes();outside=scopes(selected=False)
    publish(ready.catalog.engine,*selected[0])
    for subject,variant in selected+outside:
        with Session(ready.catalog.engine) as db,db.begin():
            repo=CollectionRepository(db);old=repo.read('stock_actions',subject,variant,with_data=False)
            repo.fail('stock_actions',subject,variant,expected=old.revision,
                      kind='synthetic_failure',now=NOW+timedelta(days=2))
    full=list(NativeSources(ready.catalog.engine).iter_entry(ENTRY))
    source=NativeSources(ready.catalog.engine)
    result=list(source.iter_partitions(ENTRY,(PARTITION,)))
    assert len(result)==3 and {raw.subject for raw in result}=={s for s,_ in selected}
    assert sum(raw.failure=='SOURCE_REFRESH_FAILED' for raw in result)==2
    assert result==[raw for raw in full if (raw.subject,raw.variant) in selected]
    assert source.summary['source_failures']==2


@pytest.mark.parametrize('selector', ['not-a-native-partition','2021-13.b09','2021-10.b16'])
def test_unknown_selector_keeps_existing_full_read_behavior(ready,selector):
    for subject,variant in scopes()+scopes(selected=False):
        publish(ready.catalog.engine,subject,variant)
    full=list(NativeSources(ready.catalog.engine).iter_entry(ENTRY))
    assert list(NativeSources(ready.catalog.engine).iter_partitions(ENTRY,(selector,)))==full


def test_selected_native_retry_matches_current_keys_and_preserves_other_files(ready):
    for subject,variant in scopes()+scopes(selected=False):
        publish(ready.catalog.engine,subject,variant)
    raw=list(NativeSources(ready.catalog.engine).iter_entry(ENTRY))
    assert not any(r.failure for r in raw), [(r.subject,r.failure) for r in raw]
    normalized=[u for r in raw for u in normalize(ENTRY,r)]
    assert not any(u.failure for u in normalized), [(u.subject,u.failure) for u in normalized]
    first=run_entry(ready,ENTRY,NativeSources(ready.catalog.engine))
    assert first['complete'] and first['qualified'], (first,ready.catalog.issues(ENTRY.spec.name,limit=10))
    with ready.catalog.transaction() as c:
        before=[tuple(r) for r in c.execute(text('SELECT path,content_hash FROM data_store_files '
            'WHERE dataset=:d AND partition_key<>:p ORDER BY path'),
            {'d':ENTRY.spec.name,'p':PARTITION})]
    publish(ready.catalog.engine,*scopes()[0],revision=1)
    result=run_entry(ready,ENTRY,NativeSources(ready.catalog.engine),
                     options=PipelineOptions(mode='retry',partitions=(PARTITION,)))
    assert result['complete'] and result['qualified'] and result['committed_partitions']==1
    assert result['source_rows']==3  # selected scopes only; all their revisions.
    with ready.catalog.transaction() as c:
        after=[tuple(r) for r in c.execute(text('SELECT path,content_hash FROM data_store_files '
            'WHERE dataset=:d AND partition_key<>:p ORDER BY path'),
            {'d':ENTRY.spec.name,'p':PARTITION})]
        assert after==before
        assert check_coverage(c,ENTRY,read_entry_status(ready,ENTRY.id))['satisfied']


def test_selected_empty_input_does_not_claim_whole_domain_coverage(ready):
    result=run_entry(ready,ENTRY,NativeSources(ready.catalog.engine),
                     options=PipelineOptions(mode='retry',partitions=(PARTITION,)))
    assert result['complete'] and result['scope']=='selected_partitions'
    with ready.catalog.transaction() as c:
        assert not check_coverage(c,ENTRY,read_entry_status(ready,ENTRY.id))['satisfied']
