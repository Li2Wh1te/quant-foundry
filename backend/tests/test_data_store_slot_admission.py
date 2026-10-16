"""Finite real-kernel cycles under fixed slot pressure; synthetic inputs only."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
import os
from types import SimpleNamespace
from uuid import uuid4

from pydantic import SecretStr
import pytest
from sqlalchemy import text

from app.scheduling.registry import TaskContext
from app.data_store import scheduler_tasks as tasks
from app.data_store.adapters.contracts import digest
from app.data_store.adapters.registry import BY_ID
from app.data_store.catalog import Catalog
from app.data_store.coverage import check_coverage
from app.data_store.errors import DataStoreError
from app.data_store.limits import MiB, StoreLimits
from app.data_store.pipeline import read_entry_status, run_local
from tests.test_data_store_domain_samples import sample
from tests.test_data_store_kernel import database, store, KEY  # noqa: F401
from tests.test_data_store_local_pipeline import Inputs, company
from tests.test_data_store_local_update_admission import install_admission, phase

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                                reason='isolated PostgreSQL required')


@pytest.fixture
def limits():
    return replace(StoreLimits(), operation_slots=3, scratch_bytes=64*MiB,
                   duckdb_memory_bytes=16*MiB, pipeline_spill_bytes=16*MiB,
                   query_bytes=8*MiB, batch_bytes=16*MiB, commit_bytes=MiB,
                   file_rows=10, lock_timeout_ms=100, log_bytes=4096)


@pytest.fixture
def current(store):
    install_admission(store.catalog.engine)
    return store


def test_slot_wait_preserves_attempt_clocks_and_source_until_capacity_released(current):
    entry=BY_ID['E41'];source=Inputs(company('old'))
    first=run_local(current,source,entries=[entry],clock=lambda:1000)[0]
    original=deepcopy(first['refresh'])
    for owner in ('pipeline.E68','pipeline.E70'):
        with current.budget.reserve('read',pending=owner) as space:
            (space.spill/'retained.sqlite').write_bytes(owner.encode())
    class MustNotScan(Inputs):
        def iter_entry(self,entry):
            raise AssertionError('capacity waiting must not reacquire the source')
    for tick in (1010,1020,1030):
        result=run_local(current,MustNotScan(),entries=[entry],clock=lambda:tick)[0]
        assert result['outcome']=='deferred' and not result['attempted']
        assert result['reason']=='SCRATCH_ADMISSION_DEFERRED'
        assert result['source_rows']==result['committed_partitions']==0
        for key in ('last_attempt_at','last_success_at','last_source_scan_at',
                    'consecutive_failures','stalled_failures','next_retry_at'):
            assert result['refresh'].get(key)==original.get(key)
        assert result['refresh']['deferred_since']==1010
        assert current.budget.pending_keys()=={'pipeline.E68','pipeline.E70'}
        with current.budget.reserve('write'):pass
        status=read_entry_status(current,entry.id)
        with current.catalog.transaction() as c:
            assert not check_coverage(c,entry,status)['satisfied']
    with current.budget.reserve('read',pending='pipeline.E68') as space:
        assert (space.spill/'retained.sqlite').read_bytes()==b'pipeline.E68'
        space.discard=True
    recovered=run_local(current,Inputs(company('new',2)),entries=[entry],clock=lambda:1040)[0]
    assert recovered['complete'] and recovered['qualified'] and recovered['outcome']=='updated'
    assert recovered['refresh']['last_success_at']==1040
    assert current.budget.pending_keys()=={'pipeline.E70'}


def test_fixed_three_slots_advance_and_release_across_24_scheduler_cycles(current,monkeypatch):
    entries=[BY_ID[e] for e in ('E41','E42','E68')];calls=Counter();rows={}
    for entry,field,prefix in [(entries[0],'company_id','COMP'),(entries[1],'manager_id','MGR')]:
        rows[entry.id]=[]
        for index in range(40):
            raw=sample(entry.id);content=deepcopy(raw.content)
            content['item'][0][field]=prefix+str(index)
            rows[entry.id].append(replace(raw,subject=prefix+str(index),content=content,token=digest(content)))
    raw=sample('E68');rows['E68']=[]
    for month in range(1,7):
        content=dict(raw.content,calendar_date=f'2026-{month:02}-02')
        rows['E68'].append(replace(raw,content=content,token=digest(content)))
    class LocalInputs:
        def __init__(self,*a,**k):self.summary={}
        def iter_entry(self,entry):
            calls[entry.id]+=1;self.summary={'complete':False}
            yield from rows[entry.id]
            self.summary={'complete':True,'source_rows':len(rows[entry.id])}
    monkeypatch.setattr(tasks,'get_engine',lambda:current.catalog.engine)
    monkeypatch.setattr(tasks,'get_settings',lambda:SimpleNamespace(
        data_store_root=current.files.root,cursor_signing_key=SecretStr(KEY.decode())))
    monkeypatch.setattr(tasks,'NativeSources',LocalInputs)
    policy=asdict(Catalog.existing_limits(current.catalog.engine));completed=set();deferred=set()
    source_scans=Counter();total_committed=0
    for cycle in range(24):
        result=tasks.update_local(TaskContext(task_id=uuid4(),run_id=uuid4()),
            tasks.LocalUpdateParameters(datasets=[e.spec.name for e in entries],maximum_passes=4096))
        assert not result['complete'] and result['pending']>0
        total_committed+=result['committed_partitions']
        pending=current.budget.pending_keys();assert len(pending)<=2
        for e in entries:
            status=read_entry_status(current,e.id);refresh=status['refresh']
            assert refresh['outcome'] in ('updated','unchanged','continued','deferred')
            assert refresh.get('consecutive_failures',0)==0
            if refresh['outcome']=='deferred':
                deferred.add(e.id);assert not refresh['attempted']
            if status.get('source_scanned') and refresh.get('attempted'):
                source_scans[e.id]+=1
            if status.get('complete'):
                completed.add(e.id);assert 'pipeline.'+e.id not in pending
        # The reserved writer headroom remains usable in every real cycle.
        with current.budget.reserve('write'):pass
    assert completed=={e.id for e in entries} and deferred
    assert total_committed>=40 and all(calls[e.id]>=1 for e in entries)
    assert calls==source_scans
    assert asdict(Catalog.existing_limits(current.catalog.engine))==policy
    assert phase(current.catalog.engine)=='reset_done'
    with current.catalog.transaction() as c:
        assert c.scalar(text('SELECT count(*) FROM data_store_issues'))==0


@pytest.mark.parametrize('code',['SCRATCH_BUDGET_EXCEEDED','SOURCE_CONFLICT','COMMIT_UNKNOWN'])
def test_admitted_failures_are_not_reclassified_as_capacity_wait(current,monkeypatch,code):
    from app.data_store import pipeline
    def fail(*a,**k):raise DataStoreError(code)
    monkeypatch.setattr(pipeline,'_commit_partition',fail)
    if code=='COMMIT_UNKNOWN':
        with pytest.raises(DataStoreError) as error:
            run_local(current,Inputs(company()),entries=[BY_ID['E41']],clock=lambda:1000)
        result=error.value.results[0]
    else:result=run_local(current,Inputs(company()),entries=[BY_ID['E41']],clock=lambda:1000)[0]
    assert result['outcome']=='failed' and result['reason']==code
    assert result['refresh']['consecutive_failures']==1
    assert result['refresh']['next_retry_at']==1030


@pytest.mark.parametrize('patch',[{'new_issues':1},{'input_failures':1},
                                  {'unresolved_issues':1},{'overflow_restriction':{'blocking_objects':1}},
                                  {'outcome':'backoff','reason':'RETRY_BACKOFF',
                                   'refresh':{'reason':'COMMIT_UNKNOWN'}}])
def test_budget_continuation_with_quality_failure_still_fails(current,monkeypatch,patch):
    from contextlib import nullcontext
    monkeypatch.setattr(tasks,'get_engine',lambda:current.catalog.engine)
    monkeypatch.setattr(tasks,'get_settings',lambda:SimpleNamespace(
        data_store_root=current.files.root,cursor_signing_key=SecretStr(KEY.decode())))
    monkeypatch.setattr(tasks,'CurrentStore',lambda *a,**k:nullcontext(current))
    monkeypatch.setattr(tasks,'NativeSources',lambda *a,**k:object())
    row={'entry_id':'E41','outcome':'continued','reason':'PASS_BUDGET_EXCEEDED',
         'complete':False,'qualified':False,**patch}
    monkeypatch.setattr(tasks,'run_local',lambda *a,**k:[row])
    with pytest.raises(RuntimeError,match='DATA_STORE_UPDATE_INCOMPLETE'):
        tasks.update_local(TaskContext(task_id=uuid4(),run_id=uuid4()),
            tasks.LocalUpdateParameters(datasets=[BY_ID['E41'].spec.name]))
