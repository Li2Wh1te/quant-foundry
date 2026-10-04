"""Real PostgreSQL/Parquet continuation boundaries; no supplier or production IO."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest
from sqlalchemy import text

from tests.test_data_store_kernel import database,limits,store,KEY
from tests.test_data_store_incremental import ready,publish,update
from tests.test_data_store_domain_samples import sample
from app.data_ingestion.tonghuashun.confirmation import returned_keys
from app.data_store import incremental,pipeline
from app.data_store.adapters.registry import BY_ID
from app.data_store.errors import DataStoreError
from app.data_store.merge import MergeSpool
from app.data_store.pipeline import PipelineOptions,read_entry_status,run_local
from app.data_store.local_sources import NativeSources
from app.data_store.coverage import check_coverage


def nav_body(months,*,invalid=False,subject=None):
    """One real native object spans enough monthly partitions to seal a batch."""
    body=deepcopy(sample('E44').content)
    first=body['item'][0]
    body['item']=[]
    for i in range(months):
        row=dict(first,nav_date=f'{1999+i//12:04d}{i%12+1:02d}02')
        if subject is not None:row['thscode']=subject
        if invalid and i==0:row['unit_nav']='invalid decimal'
        body['item'].append(row)
    return body


def pending_path(st):
    paths=list(st.files.root.glob('.scratch/*/spill/lfd02-merge.sqlite'))
    assert len(paths)==1
    return paths[0]


def seal(st,months=6,*,invalid=False):
    body=nav_body(months,invalid=invalid)
    publish(st.catalog.engine,'E44',body=body,
            requests=[{'parameters':{},**returned_keys(body)}])
    result=update(st,'E44',options=PipelineOptions(maximum_passes=1))
    assert not result['complete']
    with st.budget.reserve('read',pending='pipeline.E44') as space:
        progress=MergeSpool.read_sealed_progress(space)
    return progress


def test_cancel_obsolete_seal_preserves_files_issues_and_unacknowledged_source_cursor(ready):
    from app.data_store.cancel_sealed import cancel_sealed
    progress = seal(ready, invalid=True)
    entry = BY_ID['E44']
    before_ranges = ranges(ready)
    with ready.catalog.transaction() as connection:
        before_files = connection.execute(text('SELECT * FROM data_store_files')).mappings().all()
        before_issues = connection.execute(text('SELECT * FROM data_store_issues')).mappings().all()
    path = pending_path(ready)
    before_bytes = path.read_bytes()
    with pytest.raises(DataStoreError):
        cancel_sealed(ready, entry, expected_identity='0' * 64,
                      expected_selection=progress['source_selection'])
    assert path.read_bytes() == before_bytes and ranges(ready) == before_ranges
    result = cancel_sealed(ready, entry, expected_identity=progress['identity'],
                           expected_selection=progress['source_selection'])
    assert result['cancelled'] and result['scratch_released'] and not result['input_acknowledged']
    assert not path.exists() and not ready.budget.pending_keys()
    assert ranges(ready) == before_ranges
    with ready.catalog.transaction() as connection:
        assert connection.execute(text('SELECT * FROM data_store_files')).mappings().all() == before_files
        assert connection.execute(text('SELECT * FROM data_store_issues')).mappings().all() == before_issues
    status = read_entry_status(ready, 'E44')
    assert not status['complete'] and not status['qualified'] and status['coverage_pending']
    assert cancel_sealed(ready, entry, expected_identity=progress['identity'],
                         expected_selection=progress['source_selection'])['idempotent']


def continuation(progress,*,partitions=4096):
    return PipelineOptions(resume_sealed_only=True,maximum_claim_batches=1,
        maximum_partition_passes=partitions,pass_seconds=840,
        expected_input_identity=progress['identity'],
        expected_source_selection=progress['source_selection'])


def ranges(st):
    with st.catalog.transaction() as c:
        return [dict(r) for r in c.execute(text(
            'SELECT subject,pending,active FROM data_store_source_ranges ORDER BY subject'
        )).mappings()]


def no_new_claim(*args,**kwargs):
    raise AssertionError('A sealed continuation must not claim or decode new native work')


def test_one_sealed_batch_finishes_321_partitions_without_claiming_next(ready,monkeypatch):
    progress=seal(ready,322)
    with sqlite3.connect(pending_path(ready)) as db:
        assert db.execute('SELECT count(*) FROM partitions WHERE p>?',
                          (progress['after'],)).fetchone()[0]==321
    next_body=nav_body(2,subject='NEXT.FUND')
    publish(ready.catalog.engine,'E44',subject='NEXT.FUND',body=next_body,
            requests=[{'parameters':{},**returned_keys(next_body)}])
    before=next(r for r in ranges(ready) if r['subject']=='NEXT.FUND')
    monkeypatch.setattr(incremental,'claim_funds',no_new_claim)
    monkeypatch.setattr(incremental.FundBatchSources,'iter_entry',no_new_claim)
    result=update(ready,'E44',options=continuation(progress,partitions=321))
    assert result['sealed_continuation']['complete']
    assert result['sealed_continuation']['partition_passes']==321
    assert result['source_metrics']['payload_rows']==0
    assert result['source_metrics']['decode_calls']==0
    assert result['committed_partitions']==321
    assert next(r for r in ranges(ready) if r['subject']=='NEXT.FUND')==before
    assert not result['complete']
    with ready.catalog.transaction() as c:
        assert not check_coverage(c,BY_ID['E44'],result)['satisfied']
    assert 'pipeline.E44' not in ready.budget.pending_keys()
    assert not list(ready.files.root.glob('.scratch/*/spill/*.sqlite'))


@pytest.mark.parametrize('changed',['input_identity','source_selection','active_claim'])
def test_changed_fence_refuses_before_claim_or_rewrite(ready,monkeypatch,changed):
    progress=seal(ready)
    path=pending_path(ready)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    options=continuation(progress)
    if changed=='active_claim':
        with ready.catalog.engine.begin() as c:
            c.execute(text("UPDATE data_store_source_ranges SET active=jsonb_set(active,'{id}',to_jsonb('changed'::text)) WHERE active IS NOT NULL"))
    else:options=replace(options,**{'expected_'+changed:'0'*64})
    source_before=ranges(ready)
    monkeypatch.setattr(incremental,'claim_funds',no_new_claim)
    monkeypatch.setattr(incremental.FundBatchSources,'iter_entry',no_new_claim)
    with pytest.raises(DataStoreError) as error:update(ready,'E44',options=options)
    assert error.value.code=='SOURCE_CONFLICT'
    assert error.value.preserve_continuation
    assert ranges(ready)==source_before
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    assert 'pipeline.E44' in ready.budget.pending_keys()


def test_time_budget_retains_cursor_and_can_resume_without_decode(ready,monkeypatch):
    progress=seal(ready)
    options=continuation(progress)
    elapsed=[0.0]
    original=pipeline._commit_partition
    def expire_after_commit(*args,**kwargs):
        result=original(*args,**kwargs)
        elapsed[0]=841.0
        return result
    monkeypatch.setattr(pipeline,'_commit_partition',expire_after_commit)
    result=run_local(ready,NativeSources(ready.catalog.engine),entries=[BY_ID['E44']],
                     options=options,monotonic=lambda:elapsed[0])[0]
    assert result['reason']=='LOCAL_UPDATE_BUDGET_EXCEEDED'
    assert not result['complete'] and not result['qualified']
    with ready.budget.reserve('read',pending='pipeline.E44') as space:
        checkpoint=MergeSpool.read_sealed_progress(space)
    assert checkpoint['after']>progress['after']
    assert checkpoint['identity']==progress['identity']
    assert checkpoint['source_selection']==progress['source_selection']
    monkeypatch.setattr(pipeline,'_commit_partition',original)
    monkeypatch.setattr(incremental.FundBatchSources,'iter_entry',no_new_claim)
    resumed=update(ready,'E44',options=options)
    assert resumed['sealed_continuation']['complete']
    assert 'pipeline.E44' not in ready.budget.pending_keys()


def test_cancellation_releases_locks_and_preserves_the_committed_cursor(ready,monkeypatch):
    progress=seal(ready)
    cancelled=[False]
    original=pipeline._commit_partition
    def cancel_after_commit(*args,**kwargs):
        result=original(*args,**kwargs)
        cancelled[0]=True
        return result
    monkeypatch.setattr(pipeline,'_commit_partition',cancel_after_commit)
    with pytest.raises(DataStoreError) as error:
        run_local(ready,NativeSources(ready.catalog.engine),entries=[BY_ID['E44']],
                  options=continuation(progress),cancelled=lambda:cancelled[0])
    assert error.value.code=='OPERATION_CANCELLED'
    assert not error.value.results[0]['complete']
    # Reacquiring this quota and the same pipeline in the next call proves the
    # interrupted call released its locks without discarding the sealed input.
    with ready.budget.reserve('read',pending='pipeline.E44') as space:
        checkpoint=MergeSpool.read_sealed_progress(space)
    assert checkpoint['after']>progress['after']
    monkeypatch.setattr(pipeline,'_commit_partition',original)
    monkeypatch.setattr(incremental.FundBatchSources,'iter_entry',no_new_claim)
    result=update(ready,'E44',options=continuation(progress))
    assert result['sealed_continuation']['complete']
    assert 'pipeline.E44' not in ready.budget.pending_keys()


@pytest.mark.parametrize('unsealed',['missing','incomplete'])
def test_unsealed_input_is_not_acquired_or_called_complete(ready,monkeypatch,unsealed):
    if unsealed=='incomplete':
        progress=seal(ready)
        path=pending_path(ready)
        progress['scan_complete']=False
        with sqlite3.connect(path) as db:
            db.execute('UPDATE progress SET body=? WHERE id=1',(json.dumps(progress),))
        before=hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        body=nav_body(2)
        publish(ready.catalog.engine,'E44',body=body,
                requests=[{'parameters':{},**returned_keys(body)}])
        progress={'identity':'0'*64,'source_selection':'0'*64}
    source_before=ranges(ready)
    status_before=read_entry_status(ready,'E44')
    monkeypatch.setattr(incremental,'claim_funds',no_new_claim)
    monkeypatch.setattr(incremental.FundBatchSources,'iter_entry',no_new_claim)
    with pytest.raises(DataStoreError) as error:
        update(ready,'E44',options=continuation(progress))
    assert error.value.code=='SEALED_CONTINUATION_REQUIRED'
    assert ranges(ready)==source_before
    # Rejection occurs before refresh/quality mutation. Preserve the actual
    # previous status; an incomplete entry's domain-quality bit is not proof
    # that its captured input or full coverage completed.
    assert read_entry_status(ready,'E44')==status_before
    assert not status_before.get('complete',False)
    if unsealed=='incomplete':assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    else:assert not ready.budget.pending_keys()


def test_failed_sealed_input_is_disposed_without_qualification(ready):
    progress=seal(ready,invalid=True)
    assert progress['input_failures']>0
    result=update(ready,'E44',options=continuation(progress))
    assert result['sealed_continuation']['complete']
    assert result['sealed_continuation']['input_failures']>0
    assert not result['qualified']
    assert ready.catalog.issues(BY_ID['E44'].spec.name,limit=10)


def test_independent_partition_limit_does_not_expand_claim_batches(ready):
    for subject in ('FIRST.FUND','SECOND.FUND'):
        body=nav_body(4,subject=subject)
        publish(ready.catalog.engine,'E44',body=body,subject=subject,
                requests=[{'parameters':{},**returned_keys(body)}])
    result=update(ready,'E44',options=PipelineOptions(
        maximum_claim_batches=1,maximum_partition_passes=8))
    assert result['source_metrics']['payload_rows']==1
    assert result['committed_partitions']==4
    untouched=next(r for r in ranges(ready) if r['subject']=='SECOND.FUND')
    assert untouched['pending'] and untouched['active'] is None


def test_fenced_dispatch_rejects_multiple_entries_before_processing(ready):
    progress=seal(ready)
    path=pending_path(ready)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(DataStoreError) as error:
        run_local(ready,NativeSources(ready.catalog.engine),
                  entries=[BY_ID['E44'],BY_ID['E50']],options=continuation(progress))
    assert error.value.code=='INVALID_CONFIGURATION'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_legacy_limit_still_bounds_claims_and_partition_passes(ready):
    options=PipelineOptions(maximum_passes=1)
    assert options.claim_batches==options.partition_passes==1
    seal(ready)
    result=update(ready,'E44',options=options)
    assert result['committed_partitions']==1
    assert not result['complete']


def test_cli_runs_the_fenced_existing_batch(ready,monkeypatch,tmp_path,capsys):
    from app.data_store.__main__ import main
    from app.db import session
    progress=seal(ready)
    monkeypatch.setattr(session,'get_engine',lambda:ready.catalog.engine)
    monkeypatch.setenv('QF_CURSOR_SIGNING_KEY',KEY.decode())
    output=tmp_path/'continuation-result.json'
    code=main(['update','--entry','E44','--root',str(ready.files.root),
        '--resume-sealed-only','--max-claim-batches','1','--max-partition-passes','5',
        '--pass-seconds','840','--expect-input-identity',progress['identity'],
        '--expect-source-selection',progress['source_selection'],'--output',str(output)])
    result=json.loads(output.read_text())
    assert result['entries'][0]['sealed_continuation']['complete']
    assert not result['complete'] and code==2
    assert result['supplier_network_used'] is False
