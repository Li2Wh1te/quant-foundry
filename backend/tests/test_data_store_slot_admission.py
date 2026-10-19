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
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_local
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
            assert check_coverage(c,entry,status)['satisfied']
    with current.budget.reserve('read',pending='pipeline.E68') as space:
        assert (space.spill/'retained.sqlite').read_bytes()==b'pipeline.E68'
        space.discard=True
    recovered=run_local(current,Inputs(company('new',2)),entries=[entry],clock=lambda:1040)[0]
    assert recovered['complete'] and recovered['qualified'] and recovered['outcome']=='updated'
    assert recovered['refresh']['last_success_at']==1040
    assert current.budget.pending_keys()=={'pipeline.E70'}


def test_capacity_wait_does_not_clear_existing_quality_restriction(current):
    entry = BY_ID['E41']
    run_local(current, Inputs(company(failure='SOURCE_REFRESH_FAILED')),
              entries=[entry], clock=lambda: 1000)
    issues = current.catalog.issues(entry.spec.name, limit=10)
    for owner in ('pipeline.E68', 'pipeline.E70'):
        with current.budget.reserve('read', pending=owner):
            pass

    class MustNotScan(Inputs):
        def iter_entry(self, entry):
            raise AssertionError('capacity waiting must not inspect new input')

    result = run_local(current, MustNotScan(), entries=[entry], clock=lambda: 1040)[0]
    assert result['outcome'] == 'deferred' and not result['attempted']
    status = read_entry_status(current, entry.id)
    assert not status['qualified']
    assert current.catalog.issues(entry.spec.name, limit=10) == issues
    with current.catalog.transaction() as c:
        assert check_coverage(c, entry, status)['reason'] == 'CURRENT_QUALITY_UNRESOLVED'


def test_capacity_wait_does_not_promote_an_existing_partial_scan(current):
    entry = BY_ID['E41']
    inputs = Inputs(*(company(subject=f'C{i:03}') for i in range(40)))
    first = run_local(current, inputs, entries=[entry],
                      options=PipelineOptions(maximum_passes=1), clock=lambda: 1000)[0]
    assert not first['complete'] and first['full_coverage']['remaining'] > 0
    with current.budget.reserve('read', pending='pipeline.E68'):
        pass

    class MustNotScan(Inputs):
        def iter_entry(self, entry):
            raise AssertionError('a blocked continuation must retain its seal')

    with current.budget.reserve('write'):
        result = run_local(current, MustNotScan(), entries=[entry], clock=lambda: 1010)[0]
    assert result['outcome'] == 'deferred' and not result['attempted']
    status = read_entry_status(current, entry.id)
    assert not status['complete']
    assert status['full_coverage'] == first['full_coverage']
    assert current.budget.pending_keys() == {'pipeline.E41', 'pipeline.E68'}
    with current.catalog.transaction() as c:
        assert not check_coverage(c, entry, status)['satisfied']


def test_fixed_three_slots_advance_and_release_across_bounded_scheduler_cycles(current,monkeypatch):
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
    # One invocation owns one total unit. Bound the fixture's drain by its
    # original source-object count plus final entry checks, not 24 calls that
    # assumed a separate allowance for each sibling.
    # Retire completed test selections so an unrelated fresh scan does not
    # masquerade as continuation of the original sealed input.
    maximum_cycles=sum(len(values) for values in rows.values())+len(entries)
    for cycle in range(maximum_cycles):
        selected=[e for e in entries if e.id not in completed]
        result=tasks.update_local(TaskContext(task_id=uuid4(),run_id=uuid4()),
            tasks.LocalUpdateParameters(datasets=[e.spec.name for e in selected],maximum_passes=1))
        assert result['budget_units']<=1
        assert result['complete'] or result['pending']>0
        total_committed+=result['committed_partitions']
        pending=current.budget.pending_keys();assert len(pending)<=2
        actual={row['entry_id']:row for row in result['entries_results']}
        for e in selected:
            status=read_entry_status(current,e.id);refresh=status['refresh']
            # A call-budget deferral preserves the last attempted refresh,
            # which can be absent on a never-served entry. Classify this turn
            # by its explicit result rather than rewriting attempt history.
            row=actual[e.id]
            assert row['outcome'] in ('updated','unchanged','continued','deferred')
            assert refresh.get('consecutive_failures',0)==0
            if row['outcome']=='deferred':
                deferred.add(e.id)
                assert row['source_rows']==row['committed_partitions']==0
                assert refresh['deferred_since']>0
            if row['source_rows']:
                source_scans[e.id]+=1
            if status.get('complete'):
                completed.add(e.id);assert 'pipeline.'+e.id not in pending
        # The reserved writer headroom remains usable in every real cycle.
        with current.budget.reserve('write'):pass
        if completed=={e.id for e in entries}:break
    assert completed=={e.id for e in entries} and deferred
    assert 0<total_committed<=maximum_cycles
    assert all(calls[e.id]==1 for e in entries)
    assert calls==source_scans
    assert not current.budget.pending_keys()
    assert asdict(Catalog.existing_limits(current.catalog.engine))==policy
    assert phase(current.catalog.engine)=='reset_done'
    with current.catalog.transaction() as c:
        assert c.scalar(text('SELECT count(*) FROM data_store_issues'))==0
        for e in entries:
            assert check_coverage(c,e,read_entry_status(current,e.id))['satisfied']
            assert current.catalog.dataset(e.spec.name)['row_count']==len(rows[e.id])


@pytest.mark.parametrize('source_seconds,complete', [(272, True), (301, False)])
def test_configured_scan_bound_can_finish_manager_input_and_still_stops_at_deadline(
        current, monkeypatch, source_seconds, complete):
    # The real manager input needs more than180 seconds. Virtual elapsed time
    # exercises the actual dispatcher cancellation guard without a long test.
    entry = BY_ID['E54']
    raw = sample(entry.id)
    inputs = [replace(raw, subject=f'bounded-manager-{i:03}',
                      token=digest([raw.token, i])) for i in range(40)]
    elapsed = [0.0]

    class ManagerInputs:
        def __init__(self, *a, **k):
            self.summary = {}

        def iter_entry(self, entry):
            self.summary = {'complete': False}
            elapsed[0] = source_seconds
            yield from inputs
            self.summary = {'complete': True, 'source_rows': len(inputs)}

    monkeypatch.setattr(tasks, 'get_engine', lambda: current.catalog.engine)
    monkeypatch.setattr(tasks, 'get_settings', lambda: SimpleNamespace(
        data_store_root=current.files.root, cursor_signing_key=SecretStr(KEY.decode())))
    monkeypatch.setattr(tasks, 'NativeSources', ManagerInputs)
    dispatch = tasks.run_local
    monkeypatch.setattr(tasks, 'run_local', lambda *a, **k:
                        dispatch(*a, **k, monotonic=lambda: elapsed[0]))
    policy = asdict(Catalog.existing_limits(current.catalog.engine))
    result = tasks.update_local(TaskContext(task_id=uuid4(), run_id=uuid4()),
        tasks.LocalUpdateParameters(datasets=[entry.spec.name],
                                    maximum_passes=16, pass_seconds=300))
    status = read_entry_status(current, entry.id)
    assert result['complete'] is complete
    if complete:
        assert result['committed_partitions'] > 1
        assert status['qualified'] and status['refresh']['outcome'] == 'updated'
        with current.catalog.transaction() as c:
            assert check_coverage(c, entry, status)['satisfied']
    else:
        assert not result['committed_partitions'] and result['pending'] == 1
        assert status['refresh']['reason'] == 'LOCAL_UPDATE_BUDGET_EXCEEDED'
        assert status['refresh']['outcome'] == 'continued'
        assert current.catalog.dataset(entry.spec.name)['row_count'] == 0
    assert asdict(Catalog.existing_limits(current.catalog.engine)) == policy
    assert phase(current.catalog.engine) == 'reset_done'


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
def test_budget_continuation_preserves_quality_and_rejects_new_failures(current,monkeypatch,patch):
    from contextlib import nullcontext
    monkeypatch.setattr(tasks,'get_engine',lambda:current.catalog.engine)
    monkeypatch.setattr(tasks,'get_settings',lambda:SimpleNamespace(
        data_store_root=current.files.root,cursor_signing_key=SecretStr(KEY.decode())))
    monkeypatch.setattr(tasks,'CurrentStore',lambda *a,**k:nullcontext(current))
    monkeypatch.setattr(tasks,'NativeSources',lambda *a,**k:object())
    row={'entry_id':'E41','outcome':'continued','reason':'PASS_BUDGET_EXCEEDED',
         'complete':False,'qualified':False,**patch}
    monkeypatch.setattr(tasks,'run_local',lambda *a,**k:[row])
    if patch=={'unresolved_issues':1}:
        # Existing domain issues do not make a valid bounded turn a new
        # execution error. Its completion and quality gates must stay closed.
        result=tasks.update_local(TaskContext(task_id=uuid4(),run_id=uuid4()),
            tasks.LocalUpdateParameters(datasets=[BY_ID['E41'].spec.name]))
        assert result['complete'] is False and result['pending']==1
        assert result['entries_results'][0]['qualified'] is False
        return
    with pytest.raises(RuntimeError,match='DATA_STORE_UPDATE_INCOMPLETE'):
        tasks.update_local(TaskContext(task_id=uuid4(),run_id=uuid4()),
            tasks.LocalUpdateParameters(datasets=[BY_ID['E41'].spec.name]))
