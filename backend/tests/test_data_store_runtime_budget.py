"""Real bounded-file regressions using synthetic inputs and isolated PostgreSQL."""
from dataclasses import replace
import json
import os
import sqlite3

import pytest

from tests.test_data_store_kernel import database, limits, store, KEY
from tests.test_data_store_local_pipeline import ready, Inputs, company
from app.data_store.adapters.registry import BY_ID
from app.data_store.budget import Budget
from app.data_store.errors import DataStoreError
from app.data_store.limits import MiB, StoreLimits, local_operation_limits
from app.data_store.pipeline import PipelineOptions, run_entry, read_entry_status
from app.data_store.storage import CurrentStore
from app.data_store.tables import entry_status

pytestmark=pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED')!='1',reason='isolated PostgreSQL required')


def test_full_sqlite_scan_reports_public_budget_and_retries_with_disk_only_budget(ready):
    entry=BY_ID['E41']
    source=Inputs(*(company('Synthetic company '+str(i),subject='C'+str(i)) for i in range(300)))
    with pytest.raises(DataStoreError) as error:
        run_entry(ready,entry,source,options=PipelineOptions(pipeline_spill_bytes=128*1024))
    assert error.value.code=='SCRATCH_BUDGET_EXCEEDED'
    assert read_entry_status(ready,entry.id)['reason']=='SCRATCH_BUDGET_EXCEEDED'
    assert ready.budget.pending_keys()==set()
    memory=ready.limits.process_memory_bytes
    result=run_entry(ready,entry,source,options=PipelineOptions(pipeline_spill_bytes=4*MiB))
    assert result['complete'] and result['qualified']
    assert result['scan_source_rows']==300
    assert ready.catalog.dataset(entry.spec.name)['row_count']>=300
    assert ready.limits.process_memory_bytes==memory
    assert ready.budget.pending_keys()==set()


def test_issue_overflow_uses_control_budget_and_can_drain_without_losing_issues(database,tmp_path):
    entry=BY_ID['E41']
    source=Inputs(*(company(subject='C'+str(i),failure='SOURCE_REFRESH_FAILED') for i in range(20)))
    root=tmp_path/'current';root.mkdir()
    with database[0].begin() as c:entry_status.create(c)
    with CurrentStore(database[0],root,cursor_key=KEY,initialize=True,
                      limits=replace(StoreLimits(),issue_count=1)) as constrained:
        with pytest.raises(DataStoreError) as error:
            run_entry(constrained,entry,source)
        assert error.value.code=='ISSUE_BUDGET_EXCEEDED'
        status=read_entry_status(constrained,entry.id)
        assert status['reason']=='ISSUE_BUDGET_EXCEEDED'
        proof=status['overflow_restriction']
        assert proof['complete_key_list'] and proof['blocking_objects']>0
        sample=root/'.problem_samples'/proof['file']
        assert json.loads(sample.read_text().splitlines()[0])['affected_objects']>0
        assert 'pipeline.E41' in constrained.budget.pending_keys()
        # Reproduce the previous release's failed receipt write: only a scoped
        # placeholder survived. Recovery must use the same sealed input, not
        # silently drop the restriction or require a new provider/native scan.
        from app.data_store.pipeline import _status
        status['overflow_restriction']={'partition':proof['partition'],'blocking_objects':1,
            'complete_key_list':False,'reason':'ISSUE_BUDGET_EXCEEDED'}
        _status(constrained,entry,status)
        sample.unlink()
        changed=constrained.configure_resources(issue_count=100)
        assert changed['current']['issue_count']==100
    # The same sealed scan is drained with a reviewed finite issue capacity.
    # Existing input errors stay visible; accepting capacity is not accepting data.
    with CurrentStore(database[0],root,cursor_key=KEY) as reopened:
        assert reopened.limits.issue_count==100
        def no_scan(entry):raise AssertionError('A sealed continuation must not rescan')
        source.iter_entry=no_scan
        result=run_entry(reopened,entry,source)
        assert result['complete'] and result['resumed'] and not result['qualified']
        assert len(reopened.catalog.issues(entry.spec.name,limit=100))==20
        assert reopened.budget.pending_keys()==set()
        assert result['overflow_restriction']['complete_key_list']
        assert sample.exists()


def test_growing_continuation_preserves_other_owner_and_writer_headroom(ready):
    policy=replace(ready.limits,scratch_bytes=64*MiB,duckdb_memory_bytes=16*MiB,pipeline_spill_bytes=16*MiB,
                   query_bytes=8*MiB,batch_bytes=16*MiB,commit_bytes=MiB,operation_slots=3)
    budget=Budget(ready.files,ready.locks,policy)
    for key in ('pipeline.E68','pipeline.E70'):
        with budget.reserve('read',pending=key) as reservation:
            (reservation.spill/'pending.sqlite').write_bytes(key.encode())
    with pytest.raises(DataStoreError) as error:
        with budget.reserve('read',pending='pipeline.E68',quota_bytes=32*MiB):pass
    assert error.value.code=='SCRATCH_BUDGET_EXCEEDED'
    with budget.reserve('read',pending='pipeline.E68',quota_bytes=24*MiB) as reservation:
        assert reservation.quota==24*MiB
        assert (reservation.spill/'pending.sqlite').read_bytes()==b'pipeline.E68'
        with budget.reserve('write'):pass
        reservation.discard=True
    with budget.reserve('read',pending='pipeline.E70') as reservation:
        assert (reservation.spill/'pending.sqlite').read_bytes()==b'pipeline.E70'
        reservation.discard=True


def test_cli_continues_after_full_spool_with_consistent_public_error(ready,monkeypatch,tmp_path):
    from app.data_store.__main__ import main
    from app.data_store import local_sources
    from app.data_store.merge import MergeSpool
    from app.db import session
    source=Inputs(*(company(subject='C'+str(i)) for i in range(300)))
    class Native:
        def __init__(self,*args,**kwargs):self.summary={}
        def iter_entry(self,entry):
            if entry.id=='E41':yield from source.rows
            self.summary={'complete':True}
    monkeypatch.setattr(local_sources,'NativeSources',Native)
    monkeypatch.setattr(session,'get_engine',lambda:ready.catalog.engine)
    monkeypatch.setenv('QF_CURSOR_SIGNING_KEY',KEY.decode())
    def full(*args,**kwargs):raise sqlite3.OperationalError('database or disk is full')
    monkeypatch.setattr(MergeSpool,'offer',full)
    output=tmp_path/'cli.json'
    result=main(['rebuild','--root',str(ready.files.root),'--entry','E41','--entry','E05',
                 '--output',str(output)])
    assert result==2
    data=json.loads(output.read_text())
    assert [r['entry_id'] for r in data['entries']]==['E41','E05']
    assert data['entries'][0]['reason']=='SCRATCH_BUDGET_EXCEEDED'
    assert data['entries'][1]['complete'] and data['entries'][1]['qualified']


def test_policy_growth_fences_active_slots_and_preserves_root(ready):
    original=ready.limits
    with ready.budget.reserve('read'):
        with pytest.raises(DataStoreError) as error:
            ready.configure_resources(issue_count=original.issue_count+1)
        assert error.value.code=='LOCK_TIMEOUT'
    assert ready.limits==original
    ready.configure_resources(issue_count=original.issue_count+1)
    with CurrentStore(ready.catalog.engine,ready.files.root,cursor_key=KEY) as reopened:
        assert reopened.limits.issue_count==original.issue_count+1
        assert reopened.limits.process_memory_bytes==original.process_memory_bytes
        with pytest.raises(DataStoreError) as error:
            reopened.configure_resources(issue_count=original.issue_count)
        assert error.value.code=='INVALID_CONFIGURATION'
    with pytest.raises(DataStoreError) as error:
        CurrentStore(ready.catalog.engine,ready.files.root,cursor_key=KEY,limits=original)
    assert error.value.code=='CATALOG_MISMATCH'


def test_legacy_policy_defaults_and_explicit_cli_upgrade(ready,monkeypatch,tmp_path):
    from dataclasses import asdict
    from sqlalchemy import text
    from app.data_store.__main__ import main
    from app.db import session
    # Simulate an existing root whose policy predates the optional disk field.
    legacy=asdict(ready.limits);legacy.pop('pipeline_spill_bytes')
    with ready.catalog.transaction() as c:
        c.execute(text('UPDATE data_store_runtime SET policy_json=:p WHERE singleton=1'),
                  {'p':json.dumps(legacy)})
    with CurrentStore(ready.catalog.engine,ready.files.root,cursor_key=KEY) as opened:
        assert opened.limits==ready.limits
    monkeypatch.setattr(session,'get_engine',lambda:ready.catalog.engine)
    monkeypatch.setenv('QF_CURSOR_SIGNING_KEY',KEY.decode())
    result_file=tmp_path/'resources.json'
    assert main(['configure-resources','--root',str(ready.files.root),
                 '--pipeline-spill-bytes',str(128*MiB),'--issue-count','20000',
                 '--output',str(result_file)])==0
    result=json.loads(result_file.read_text())
    assert result['current']['pipeline_spill_bytes']==128*MiB
    with CurrentStore(ready.catalog.engine,ready.files.root,cursor_key=KEY) as opened:
        assert opened.limits.issue_count==20000
        with opened.budget.reserve('read',pending='pipeline.E41') as space:
            assert space.quota==128*MiB
            space.discard=True
    damaged=dict(result['current']);damaged.pop('process_memory_bytes')
    with ready.catalog.transaction() as c:
        c.execute(text('UPDATE data_store_runtime SET policy_json=:p WHERE singleton=1'),
                  {'p':json.dumps(damaged)})
    with pytest.raises(DataStoreError) as error:
        CurrentStore(ready.catalog.engine,ready.files.root,cursor_key=KEY)
    assert error.value.code=='CATALOG_MISMATCH'


@pytest.mark.parametrize('kwargs',[{'scratch_bytes':129*1024**3},{'issue_count':1_000_001},
                                  {'issue_count':True},{'scratch_bytes':0}])
def test_resource_overrides_remain_finite(kwargs):
    with pytest.raises(DataStoreError):local_operation_limits(**kwargs)
