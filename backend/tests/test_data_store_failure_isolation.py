"""AR-01: real scheduler/CLI, native PostgreSQL inputs and current readback."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.data_store.adapters.registry import BY_ID
from app.data_store.errors import DataStoreError
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_entry, run_local
from app.data_store.updates import RetryPolicy
from app.scheduling.registry import TaskContext
from app.data_store import scheduler_tasks as tasks
from tests.test_data_store_kernel import database, limits, store, KEY
from tests.test_data_store_domain_samples import ready, sample, NOW, add_observation, current_object
from app.data_store.readers import Query
from tests.test_legacy_reset import DDL

pytestmark=pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED')!='1',reason='isolated PostgreSQL required')
A,B,C=(BY_ID[i] for i in ('E41','E68','E42'))


@pytest.fixture
def clock(monkeypatch):
    from app.data_store import updates
    value=[1_800_000_000]
    monkeypatch.setattr(updates.time,'time',lambda:value[0])
    return value


def changed(ready,round):
    for entry,field in ((A,'company_name'),(C,'manager_name')):
        body=deepcopy(sample(entry.id).content)
        body['item'][0][field]=f'value-{round}'
        add_observation(ready.catalog.engine,entry.id,body,NOW+timedelta(seconds=round+1))


def values(ready,round):
    for entry in (A,C):assert current_object(ready,entry.id)['data']['name']==f'value-{round}'


def many_calendar(ready,count=300):
    with ready.catalog.engine.begin() as c:
        c.execute(text('DELETE FROM trading_calendar_days'))
        c.execute(text("INSERT INTO trading_calendar_days(exchange,calendar_date,is_open) VALUES ('SSE',:day,true)"),
                  [{'day':f'{2000+i//12}-{1+i%12:02}-01'} for i in range(count)])


def calendar_rows(ready,entry):
    with ready.catalog.transaction() as c:
        partitions=list(c.execute(text('SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:d ORDER BY partition_key'),
                                  {'d':entry.spec.name}).scalars())
    return [row for start in range(0,len(partitions),64) for row in
            ready.read(entry.spec,Query(partitions=tuple(partitions[start:start+64]),page_size=100)).rows]


def scheduler(ready,monkeypatch):
    with ready.catalog.engine.begin() as c:
        c.exec_driver_sql(DDL[0])
        c.exec_driver_sql("INSERT INTO data_store_legacy_maintenance(singleton,phase) VALUES (1,'ready')")
    monkeypatch.setattr(tasks,'get_engine',lambda:ready.catalog.engine)
    monkeypatch.setattr(tasks,'get_settings',lambda:SimpleNamespace(data_store_root=ready.files.root,
        cursor_signing_key=SecretStr(KEY.decode())))
    return TaskContext(task_id=uuid4(),run_id=uuid4())


@pytest.mark.parametrize('selection',[(B,A,C),(A,B,C),(A,C,B)],ids=['first','middle','last'])
def test_A01_A02_default_scheduler_four_failed_rounds_refresh_healthy(ready,monkeypatch,clock,selection):
    context=scheduler(ready,monkeypatch)
    with ready.catalog.engine.begin() as c:
        c.exec_driver_sql('ALTER TABLE trading_calendar_days RENAME TO ar01_saved_calendar')
    calls=[];original=NativeSources.iter_entry
    def counted(self,entry):
        calls.append(entry.id)
        yield from original(self,entry)
    monkeypatch.setattr(NativeSources,'iter_entry',counted)
    for round in range(4):
        changed(ready,round);clock[0]+=3600;calls.clear()
        with pytest.raises(RuntimeError,match='DATA_STORE_UPDATE_INCOMPLETE'):
            tasks.update_local(context,tasks.LocalUpdateParameters(datasets=[e.spec.name for e in selection]))
        assert sorted(calls)==sorted(e.id for e in selection)
        values(ready,round)
        assert read_entry_status(ready,B.id)['refresh']['outcome']=='failed'
        assert all(read_entry_status(ready,e.id)['refresh']['last_success_at']==clock[0] for e in (A,C))
    with ready.catalog.engine.begin() as c:
        c.exec_driver_sql('ALTER TABLE ar01_saved_calendar RENAME TO trading_calendar_days')
    for round in (4,5):
        changed(ready,round);clock[0]+=3600;calls.clear()
        if round==5:
            with ready.catalog.engine.begin() as c:
                c.exec_driver_sql('UPDATE trading_calendar_days SET is_open=false')
        result=tasks.update_local(context,tasks.LocalUpdateParameters(datasets=[e.spec.name for e in selection]))
        assert result['complete'] and sorted(calls)==sorted(e.id for e in selection)
        values(ready,round)
        assert read_entry_status(ready,B.id)['refresh']['consecutive_failures']==0
    assert current_object(ready,B.id)['data']['is_open'] is False


def test_A03_sealed_300_partition_work_does_not_freeze_siblings(ready,monkeypatch,clock):
    many_calendar(ready);native=NativeSources(ready.catalog.engine)
    calls=[];original=NativeSources.iter_entry
    def counted(self,entry):
        calls.append(entry.id);yield from original(self,entry)
    monkeypatch.setattr(NativeSources,'iter_entry',counted)
    for round in range(5):
        changed(ready,round);clock[0]+=60
        result=run_local(ready,native,entries=[A,B,C],options=PipelineOptions(maximum_passes=64))
        values(ready,round)
        b=next(r for r in result if r['entry_id']==B.id)
        assert b['full_coverage']['remaining']==max(0,300-64*(round+1))
        assert b['outcome']==('continued' if round<4 else 'updated')
    assert calls.count(B.id)==1 and calls.count(A.id)==calls.count(C.id)==5
    assert len(calendar_rows(ready,B))==300
    run_local(ready,native,entries=[B])
    assert calls.count(B.id)==2


def test_A04_A06_backoff_recovery_and_manual_retry_preserve_clocks(ready,clock):
    native=NativeSources(ready.catalog.engine)
    good=run_local(ready,native,entries=[B])[0]
    with ready.catalog.engine.begin() as c:c.exec_driver_sql('ALTER TABLE trading_calendar_days RENAME TO saved_calendar')
    clock[0]+=10
    bad=run_local(ready,native,entries=[B])[0]
    assert bad['outcome']=='failed' and bad['refresh']['next_retry_at']==clock[0]+30
    last_attempt=bad['refresh']['last_attempt_at']
    clock[0]+=29
    skipped=run_local(ready,native,entries=[B])[0]
    assert skipped['outcome']=='backoff' and not skipped['attempted'] and skipped['source_rows']==0
    assert skipped['refresh']['last_attempt_at']==last_attempt
    assert skipped['refresh']['last_success_at']==good['refresh']['last_success_at']
    assert skipped['refresh']['last_source_scan_at']==good['refresh']['last_source_scan_at']
    assert len(calendar_rows(ready,B))==1
    with ready.catalog.engine.begin() as c:c.exec_driver_sql('ALTER TABLE saved_calendar RENAME TO trading_calendar_days')
    manual=run_local(ready,native,entries=[B],options=PipelineOptions(mode='retry'))[0]
    assert manual['outcome']=='unchanged' and manual['attempted']
    assert manual['refresh']['last_success_at']==clock[0] and manual['refresh']['consecutive_failures']==0
    assert len(calendar_rows(ready,B))==1


def test_A04_sealed_failure_resumes_without_read_and_retry_bypasses_backoff(ready,monkeypatch,clock):
    from app.data_store import pipeline
    many_calendar(ready,3);native=NativeSources(ready.catalog.engine)
    first=run_local(ready,native,entries=[B],options=PipelineOptions(maximum_passes=1))[0]
    source_time=first['refresh']['last_source_scan_at']
    original=pipeline._commit_partition
    def failed(*args,**kwargs):raise DataStoreError('SOURCE_CONFLICT')
    with monkeypatch.context() as patch:
        patch.setattr(pipeline,'_commit_partition',failed)
        clock[0]+=5
        assert run_local(ready,native,entries=[B])[0]['outcome']=='failed'
    assert 'pipeline.E68' in ready.budget.pending_keys()
    clock[0]+=1
    result=run_local(ready,native,entries=[B],options=PipelineOptions(mode='retry'))[0]
    assert result['resumed'] and result['source_rows']==0 and result['complete']
    assert result['refresh']['last_source_scan_at']==source_time
    assert len(calendar_rows(ready,B))==3


def test_A04_source_file_error_is_entry_local_not_store_failure(ready):
    class Files(NativeSources):
        def iter_entry(self,entry):
            if entry==B:raise FileNotFoundError('private-source-path')
            yield from super().iter_entry(entry)
    changed(ready,3)
    results=run_local(ready,Files(ready.catalog.engine),entries=[B,A,C])
    assert results[0]['reason']=='LOCAL_SOURCE_UNAVAILABLE'
    assert 'private-source-path' not in json.dumps(results)
    values(ready,3)


@pytest.mark.parametrize('code',['DISK_PRESSURE','CATALOG_MISMATCH','CATALOG_UNAVAILABLE','OPERATION_CANCELLED'])
def test_A07_shared_fatal_conditions_stop_after_committed_work(ready,monkeypatch,code):
    from app.data_store import pipeline
    original=pipeline._commit_partition
    def injected(store,entry,*args,**kwargs):
        if entry==B:raise DataStoreError(code)
        return original(store,entry,*args,**kwargs)
    monkeypatch.setattr(pipeline,'_commit_partition',injected)
    changed(ready,0)
    with pytest.raises(DataStoreError) as raised:
        run_local(ready,NativeSources(ready.catalog.engine),entries=[A,B,C])
    assert raised.value.code==code and len(raised.value.results)==2
    assert current_object(ready,A.id)['data']['name']=='value-0'
    assert 'refresh' not in read_entry_status(ready,C.id)


@pytest.mark.parametrize('error',[KeyboardInterrupt,SystemExit,RuntimeError])
def test_A07_signals_and_unexpected_errors_are_not_swallowed(ready,monkeypatch,error):
    from app.data_store import pipeline
    def broken(*args,**kwargs):raise error('do-not-persist-this-private-payload')
    monkeypatch.setattr(pipeline,'normalize',broken)
    with pytest.raises(error):run_local(ready,NativeSources(ready.catalog.engine),entries=[A,C])
    status=read_entry_status(ready,A.id)
    assert status['refresh']['error_type']==error.__name__
    assert 'private-payload' not in json.dumps(status)
    assert 'refresh' not in read_entry_status(ready,C.id)


def test_A07_cancellation_callback_preserves_completed_entry(ready,monkeypatch):
    from app.data_store import pipeline
    original=pipeline.run_entry;cancelled=[False]
    def completed(*args,**kwargs):
        result=original(*args,**kwargs);cancelled[0]=True;return result
    monkeypatch.setattr(pipeline,'run_entry',completed)
    changed(ready,2)
    with pytest.raises(DataStoreError) as raised:
        run_local(ready,NativeSources(ready.catalog.engine),entries=[A,B,C],cancelled=lambda:cancelled[0])
    assert raised.value.code=='OPERATION_CANCELLED'
    assert current_object(ready,A.id)['data']['name']=='value-2'
    assert 'refresh' not in read_entry_status(ready,C.id)


def test_A08_cli_isolates_failures_and_deduplicates_import_target(ready,monkeypatch,clock,capsys):
    from app.data_store.__main__ import main
    from app.db import session
    monkeypatch.setattr(session,'get_engine',lambda:ready.catalog.engine)
    monkeypatch.setenv('QF_CURSOR_SIGNING_KEY',KEY.decode())
    with ready.catalog.engine.begin() as c:c.exec_driver_sql('DROP TABLE trading_calendar_days')
    calls=[];original=NativeSources.iter_entry
    def counted(self,entry):calls.append(entry.id);yield from original(self,entry)
    monkeypatch.setattr(NativeSources,'iter_entry',counted)
    changed(ready,1)
    target=BY_ID['E01'].target
    args=['update','--root',str(ready.files.root)]
    for id in (B.id,A.id,C.id,'E01',target):args+=['--entry',id]
    assert main(args)==2
    output=json.loads(capsys.readouterr().out)
    assert not output['complete'] and not output['supplier_network_used']
    assert calls.count(target)==1 and len(calls)==len(set(calls))
    values(ready,1)


def test_A05_stalled_continuations_release_only_scratch_after_bounded_failures(ready,monkeypatch,clock):
    from app.data_store import pipeline
    from app.data_store.budget import Budget
    from app.data_store.limits import MiB
    limits=replace(ready.limits,scratch_bytes=64*MiB,duckdb_memory_bytes=16*MiB,pipeline_spill_bytes=16*MiB,
                   query_bytes=8*MiB,batch_bytes=16*MiB,commit_bytes=MiB,operation_slots=3)
    ready.limits=limits;ready.budget=Budget(ready.files,ready.locks,limits)
    many_calendar(ready,3)
    original=pipeline._commit_partition
    def failed(store,entry,*args,**kwargs):
        if entry in (B,C):raise DataStoreError('SOURCE_CONFLICT')
        return original(store,entry,*args,**kwargs)
    monkeypatch.setattr(pipeline,'_commit_partition',failed)
    # Two sealed failures fill the read slots; the third slot remains reserved
    # for a writer even while the failed entries are in backoff.
    run_local(ready,NativeSources(ready.catalog.engine),entries=[B,C])
    assert len(ready.budget.pending_keys())==2
    with ready.budget.reserve('write'):pass
    charges=[ready.budget._quota(i)[1] for i in range(3)]
    assert sum(charges)<limits.scratch_bytes
    changed(ready,1)
    for _ in range(5):
        clock[0]+=3600
        results=run_local(ready,NativeSources(ready.catalog.engine),entries=[B,C,A])
        if any(r['entry_id']==A.id and r['complete'] for r in results):break
    else:pytest.fail('stalled continuations permanently starved the healthy entry')
    assert current_object(ready,A.id)['data']['name']=='value-1'
    assert any(read_entry_status(ready,e.id)['refresh'].get('continuation_aborted') for e in (B,C))


def test_A05_finite_call_budget_defers_without_forging_attempt(ready,monkeypatch,clock):
    from app.data_store import pipeline
    timer=[0.0];original=pipeline.run_entry
    def charged(*args,**kwargs):
        result=original(*args,**kwargs);timer[0]+=10;return result
    monkeypatch.setattr(pipeline,'run_entry',charged)
    entries=[A,B,C];native=NativeSources(ready.catalog.engine)
    result=run_local(ready,native,entries=entries,policy=RetryPolicy(call_seconds=5),monotonic=lambda:timer[0])
    assert result[0]['complete'] and all(r['outcome']=='deferred' for r in result[1:])
    assert 'last_attempt_at' not in read_entry_status(ready,B.id).get('refresh',{})
    timer[0]=0;clock[0]+=60
    second=run_local(ready,native,entries=entries,policy=RetryPolicy(call_seconds=5),monotonic=lambda:timer[0])
    assert second[0]['entry_id']==B.id and second[0]['complete']


def test_A09_retry_update_and_full_work_share_entry_lock_not_batch_state(ready,monkeypatch,clock):
    from app.data_store import pipeline
    many_calendar(ready,3);changed(ready,0)
    entered=threading.Event();release=threading.Event();original=pipeline._commit_partition
    def held(store,entry,*args,**kwargs):
        result=original(store,entry,*args,**kwargs)
        if entry==B and not entered.is_set():
            entered.set();assert release.wait(5)
        return result
    monkeypatch.setattr(pipeline,'_commit_partition',held)
    native=NativeSources(ready.catalog.engine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(run_local,ready,native,entries=[B],options=PipelineOptions(maximum_passes=1))
        try:
            assert entered.wait(5)
            retry=run_local(ready,NativeSources(ready.catalog.engine),entries=[B],
                            options=PipelineOptions(mode='retry',partitions=('absent',)))[0]
            assert retry['reason']=='LOCK_TIMEOUT'
            assert run_local(ready,NativeSources(ready.catalog.engine),entries=[A])[0]['complete']
            assert current_object(ready,A.id)['data']['name']=='value-0'
        finally:release.set()
        result=first.result(timeout=5)[0]
    proof=result['full_coverage']
    clock[0]+=60
    partial=run_local(ready,native,entries=[B],options=PipelineOptions(mode='retry',
                      partitions=(result['last_partition'],)))[0]
    assert partial['complete'] and partial['full_coverage']==proof and not proof['complete']
    resumed=run_local(ready,native,entries=[B])[0]
    assert resumed['resumed'] and resumed['complete'] and len(calendar_rows(ready,B))==3
    changed(ready,1);clock[0]+=60
    assert run_local(ready,native,entries=[A])[0]['complete']
    assert current_object(ready,A.id)['data']['name']=='value-1'
