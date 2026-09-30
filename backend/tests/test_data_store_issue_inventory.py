"""Exact capacity, evidence preservation and bounded issue regrouping."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import event,text
from app.data_store.adapters.contracts import digest
from app.data_store.catalog import Issue
from app.data_store.errors import DataStoreError
from app.data_store.issue_sets import compact_entry, fingerprint, members, logical_count
from tests.test_data_store_kernel import database,limits,store
from tests.test_data_store_incremental import ready,NOW
from tests.test_data_store_issue_sets import seed_issues,expanded

def totals(st):
    with st.catalog.transaction() as c:
        return int(c.execute(text('SELECT coalesce(sum(physical_records),0) FROM data_store_issue_totals')).scalar_one())

def test_mixed_opaque_and_large_group_preserves_every_observation(ready):
    e,u,part=seed_issues(ready,400)
    opaque=Issue('opaque','local.partition.'+part,'SOURCE_ORDER_UNCOMPARABLE',digest('opaque'),
                 {'prefix':list(u.key[:2])},{'proof':'retained_original'})
    with ready.catalog.transaction() as c:
        ready.catalog.change_issues(c,e.spec.name,(opaque,),{},check=lambda:None)
        c.execute(text("UPDATE data_store_issues SET attempts=19,first_seen=:a,last_seen=:b WHERE dataset=:d AND issue_key='opaque'"),
                  {'d':e.spec.name,'a':NOW-timedelta(days=5),'b':NOW})
    before=expanded(ready,e);generation=ready.catalog.dataset(e.spec.name)['generation']
    result=compact_entry(ready,e);after=expanded(ready,e)
    assert result['affected_objects_before']==result['affected_objects_after']==401
    assert fingerprint(before)==fingerprint(after)
    assert result['physical_records_released']>380
    assert totals(ready)==len(ready.catalog.issues(e.spec.name,limit=1000))
    assert ready.catalog.dataset(e.spec.name)['generation']==generation
    assert next(v for v in after if v['issue_key']=='opaque')['attempts']==19
    assert all(len(r['target_json'].encode())<=65536 for r in ready.catalog.issues(e.spec.name,limit=1000))
    again=compact_entry(ready,e)
    assert again['physical_records_released']==0
    assert fingerprint(expanded(ready,e))==fingerprint(before)

def test_inventory_counts_token_fences_upserts_and_rollback(ready):
    e,u,part=seed_issues(ready,12)
    before=ready.catalog.issues(e.spec.name,limit=1000)
    assert totals(ready)==12
    row=before[0]
    issue=Issue(row['issue_key'],row['scope_key'],row['reason'],row['evidence_token'],
                json.loads(row['target_json']),json.loads(row['resolution_json']))
    with ready.catalog.transaction() as c:
        ready.catalog.change_issues(c,e.spec.name,(issue,),{row['issue_key']:digest('wrong')},check=lambda:None)
    assert totals(ready)==12
    with pytest.raises(RuntimeError):
        with ready.catalog.transaction() as c:
            ready.catalog.change_issues(c,e.spec.name,(),{row['issue_key']:row['evidence_token']},check=lambda:None)
            assert int(c.execute(text('SELECT sum(physical_records) FROM data_store_issue_totals')).scalar_one())==11
            raise RuntimeError('synthetic transaction interruption')
    assert totals(ready)==12
    with ready.catalog.transaction() as c:
        ready.catalog.change_issues(c,e.spec.name,(),{row['issue_key']:row['evidence_token']},check=lambda:None)
    assert totals(ready)==11
    compact_entry(ready,e)
    assert totals(ready)==1
    with ready.catalog.transaction() as c:
        assert logical_count(c,e.spec.name)==logical_count(c,e.spec.name,cached=True)==11

def test_capacity_guard_uses_inventory_and_rejects_exact_overflow(ready,monkeypatch):
    from dataclasses import replace
    e,u,part=seed_issues(ready,12)
    monkeypatch.setattr(ready.catalog,'limits',replace(ready.catalog.limits,issue_count=12))
    statements=[]
    def record(c,cursor,statement,*args):statements.append(statement)
    event.listen(ready.catalog.engine,'before_cursor_execute',record)
    try:
        issue=Issue('extra','local.partition.'+part,'SOURCE_CONFIRMATION_UNPROVEN',digest('extra'),{}, {})
        with pytest.raises(DataStoreError) as error:
            with ready.catalog.transaction() as c:ready.catalog.change_issues(c,e.spec.name,(issue,),{},check=lambda:None)
        assert error.value.code=='ISSUE_BUDGET_EXCEEDED'
    finally:event.remove(ready.catalog.engine,'before_cursor_execute',record)
    assert totals(ready)==12
    assert not any('SELECT count(*) FROM data_store_issues' in s for s in statements)
    assert any('sum(physical_records)' in s for s in statements)

def test_migration_populates_existing_records_under_lock_without_touching_them(ready,monkeypatch):
    from importlib import import_module
    migration=import_module('app.db.migrations.versions.20261011_01_current_issue_totals')
    with ready.catalog.engine.begin() as c:
        monkeypatch.setattr(migration.op,'get_bind',lambda:c);migration.downgrade()
    e,u,part=seed_issues_without_accounting(ready)
    before=fingerprint(expanded(ready,e))
    with ready.catalog.engine.begin() as c:
        monkeypatch.setattr(migration.op,'get_bind',lambda:c);migration.upgrade()
    assert totals(ready)==12
    assert fingerprint(expanded(ready,e))==before

def seed_issues_without_accounting(st):
    # Populate the pre-migration physical table directly. The previous catalog
    # implementation is intentionally not used with the new inventory API.
    from app.data_store.adapters.registry import BY_ID
    e=BY_ID['E50'];st.register(e.spec)
    with st.catalog.transaction() as c:
        c.execute(text("INSERT INTO data_store_issues(dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json) "
            "SELECT :d,'existing.'||n,'legacy','SOURCE_SCHEMA_INVALID',:t,'{}','{}' FROM generate_series(1,12) n"),
            {'d':e.spec.name,'t':digest('legacy')})
    return e,None,None


def seed_queue(st,n=300):
    from tests.test_data_store_incremental import bar,update
    from app.data_store.change_capture import seed
    bar(st.catalog.engine);update(st,'E70')
    with st.catalog.engine.begin() as c:
        seed(c,'tushare','etf_daily')
        c.execute(text("INSERT INTO data_store_source_ranges(source,dataset,subject,variant,range_key,lower_at,change_revision) "
            "SELECT 'tushare','etf_daily','synthetic.'||n,'default','2026-01','2026-01-01'::timestamptz,4 "
            "FROM generate_series(1,:n) n"),{'n':n})

def test_batched_range_acknowledgement_keeps_revisions_and_active_work(ready):
    from app.data_store.adapters.registry import BY_ID
    from app.data_store.local_sources import NativeSources
    from app.data_store.verify_coverage import verify_existing
    seed_queue(ready)
    native=NativeSources(ready.catalog.engine);original=native.iter_entry
    def changed(entry):
        yield from original(entry)
        with ready.catalog.engine.begin() as c:
            c.execute(text("UPDATE data_store_source_ranges SET change_revision=change_revision+1 WHERE subject='synthetic.1'"))
            c.execute(text("UPDATE data_store_source_ranges SET active='{}'::jsonb WHERE subject='synthetic.2'"))
    native.iter_entry=changed
    result=verify_existing(ready,BY_ID['E70'],native)
    assert result['complete'] and result['current_files_changed'] is False
    with ready.catalog.transaction() as c:
        assert c.execute(text('SELECT subject FROM data_store_source_ranges ORDER BY subject')).scalars().all()==['synthetic.1','synthetic.2']

def test_interrupt_after_first_ack_batch_rolls_back_all_ranges_and_receipt(ready):
    from app.data_store.adapters.registry import BY_ID
    from app.data_store.local_sources import NativeSources
    from app.data_store.verify_coverage import verify_existing
    from app.data_store.pipeline import read_entry_status
    seed_queue(ready)
    before=read_entry_status(ready,'E70').get('full_coverage');deletes=[]
    def record(c,cursor,statement,*args):
        if statement.startswith('DELETE FROM data_store_source_ranges q USING'):deletes.append(statement)
    event.listen(ready.catalog.engine,'after_cursor_execute',record)
    try:
        with pytest.raises(DataStoreError) as error:
            verify_existing(ready,BY_ID['E70'],NativeSources(ready.catalog.engine),cancelled=lambda:bool(deletes))
        assert error.value.code=='OPERATION_CANCELLED' and len(deletes)==1
    finally:event.remove(ready.catalog.engine,'after_cursor_execute',record)
    with ready.catalog.transaction() as c:assert c.execute(text('SELECT count(*) FROM data_store_source_ranges')).scalar_one()==301
    status=read_entry_status(ready,'E70')
    assert status.get('full_coverage')==before and status['coverage_pending']


def test_large_native_scalar_verification_keeps_files_and_reports_exact_counts(ready):
    from datetime import date
    from decimal import Decimal
    from sqlalchemy.orm import Session
    from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
    from app.data_ingestion.schemas.etf_daily import EtfDailyBarInput
    from app.data_store.adapters.registry import BY_ID
    from app.data_store.local_sources import NativeSources
    from app.data_store.pipeline import run_entry
    from app.data_store.verify_coverage import verify_existing
    inputs=[EtfDailyBarInput(ts_code='510300.SH',trade_date=date(2020,1,1)+timedelta(days=i),
        open=Decimal('4'),high=Decimal('9'),low=Decimal('1'),close=Decimal('4.15'),
        vol=Decimal('100'),amount=Decimal('200')) for i in range(1000)]
    with Session(ready.catalog.engine) as session,session.begin():
        EtfDailyBarRepository(session).upsert_bars(inputs,source='tushare')
    entry=BY_ID['E70'];native=NativeSources(ready.catalog.engine)
    assert run_entry(ready,entry,native)['complete']
    before=ready.catalog.dataset(entry.spec.name)
    result=verify_existing(ready,entry,NativeSources(ready.catalog.engine))
    assert result['complete'] and result['source_rows']==result['expected_objects']==result['current_objects']==1000
    assert result['current_files_changed'] is False
    assert ready.catalog.dataset(entry.spec.name)==before

def test_failed_verification_reports_counters_and_keeps_old_receipt(ready):
    from app.data_store.adapters.registry import BY_ID
    from app.data_store.local_sources import NativeSources
    from app.data_store.pipeline import read_entry_status
    from app.data_store.verify_coverage import verify_existing
    seed_queue(ready);before=read_entry_status(ready,'E70').get('full_coverage')
    with pytest.raises(DataStoreError) as error:
        verify_existing(ready,BY_ID['E70'],NativeSources(ready.catalog.engine),seconds=0)
    assert error.value.code=='QUERY_TIMEOUT'
    assert error.value.verification['phase']=='capture_ranges'
    assert error.value.verification['source_rows']==0
    assert read_entry_status(ready,'E70').get('full_coverage')==before


def test_disabled_inventory_capture_rejects_cached_reads_and_writes(ready):
    e,u,part=seed_issues(ready,12)
    with ready.catalog.transaction() as c:
        c.execute(text('ALTER TABLE data_store_issues DISABLE TRIGGER data_store_issue_records_row'))
        assert logical_count(c,e.spec.name)==12
        with pytest.raises(DataStoreError) as error:logical_count(c,e.spec.name,cached=True)
        assert error.value.code=='CATALOG_MISMATCH'
        issue=Issue('extra','local.partition.'+part,'SOURCE_SCHEMA_INVALID',digest('extra'),{}, {})
        with pytest.raises(DataStoreError):ready.catalog.change_issues(c,e.spec.name,(issue,),{},check=lambda:None)
    assert totals(ready)==12
