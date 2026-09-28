"""AR-03 real PostgreSQL producer/consumer acceptance; no supplier access."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_domain_samples import sample
from app.data_ingestion.models.tonghuashun import (TonghuashunObservation as Observation,
    TonghuashunCollectionState as State, TonghuashunDumpImport as Import, TonghuashunDumpStage as Stage)
from app.data_ingestion.models.etf_daily import EtfDailyBar, EtfDailyBarRevisionAudit
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_store.adapters.registry import BY_ID
from app.data_store.adapters.contracts import native_json,digest
from app.data_store.adapters.canonical import NativeInputError
from app.data_store.change_capture import install
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import run_entry,run_local,PipelineOptions,read_entry_status
from app.data_store.domain_reader import read_object
from app.data_store.tables import entry_status
from app.data_store import incremental
from app.data_store.coverage import check_coverage

NOW=datetime(2026,1,2,tzinfo=timezone.utc)
DAY=1767283200000


@pytest.fixture
def ready(store):
    with store.catalog.engine.begin() as c:
        for model in (Observation,State,Import,Stage,EtfDailyBar,EtfDailyBarRevisionAudit,EtfAdjustmentFactor):
            model.__table__.create(c)
        entry_status.create(c)
        install(c)
    return store


def publish(engine,entry='E50',*,body=None,now=NOW,subject=None,requests=None):
    e=BY_ID[entry];raw=sample(entry);body=deepcopy(raw.content if body is None else body)
    subject=subject or raw.subject
    if requests is None:
        requests=[{'key_receipt':'actual_returned_keys_v1','returned_keys':{'date_ms':[r['date_ms'] for r in body['item']]},'parameters':{}}]
    with Session(engine) as s,s.begin():
        repo=CollectionRepository(s);old=repo.read(e.native,subject,'default',with_data=False)
        return repo.publish(e.native,subject,'default',expected=old.revision,data=body,requests=requests,now=now)


def bar(engine,*,day='2026-01-02',close='4.15',subject='510300.SH'):
    # Exercise the production repository's real transaction, including revision
    # audit writes. Direct DML tests below independently verify trigger coverage.
    from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
    from app.data_ingestion.schemas.etf_daily import EtfDailyBarInput
    with Session(engine) as s,s.begin():
        EtfDailyBarRepository(s).upsert_bars([EtfDailyBarInput(ts_code=subject,trade_date=date.fromisoformat(day),
            open=Decimal('4'),high=Decimal('9'),low=Decimal('1'),close=Decimal(close),vol=Decimal('100'),amount=Decimal('200'))],source='tushare')


def factor(engine,value='1'):
    from app.data_ingestion.repositories.etf_adjustment import EtfAdjustmentFactorRepository
    from app.data_ingestion.schemas.etf_adjustment import EtfAdjustmentFactorInput
    with Session(engine) as s,s.begin():
        EtfAdjustmentFactorRepository(s).upsert_factors([EtfAdjustmentFactorInput(ts_code='510300.SH',
            trade_date=date(2026,1,2),adj_factor=Decimal(value))],source='tushare')


def update(st,entry='E50',**kwargs):return run_entry(st,BY_ID[entry],NativeSources(st.catalog.engine),**kwargs)


def obj(st,entry='E50',day='2026-01-02',subject=None):
    raw=sample(entry)
    return read_object(st,entry,raw.representation_key,subject or (raw.content.get('ts_code') if entry in ('E69','E70') else raw.subject),day)


def files(st,entry):
    with st.catalog.transaction() as c:
        return {r['path']:r['content_hash'] for r in c.execute(text('SELECT path,content_hash FROM data_store_files WHERE dataset=:d'),{'d':BY_ID[entry].spec.name}).mappings()}


@pytest.mark.parametrize('entry',['E50','E51','E52','E69','E70'])
def test_I01_all_native_paths_and_two_zero_payload_updates(ready,entry):
    if entry=='E70':bar(ready.catalog.engine)
    elif entry=='E69':factor(ready.catalog.engine)
    else:publish(ready.catalog.engine,entry)
    first=update(ready,entry);assert first['complete'] and first['qualified']
    before=files(ready,entry);generation=ready.catalog.dataset(BY_ID[entry].spec.name)['generation']
    for _ in range(2):
        r=update(ready,entry)
        assert r['normalized_units']==r['source_metrics']['payload_rows']==r['metrics']['files_written']==0
        assert r['source_metrics']['dependency_rows']==0
        assert files(ready,entry)==before
        assert ready.catalog.dataset(BY_ID[entry].spec.name)['generation']==generation


def test_I02_I03_late_historical_correction_and_factor(ready):
    bar(ready.catalog.engine);bar(ready.catalog.engine,day='2025-01-02');factor(ready.catalog.engine)
    update(ready,'E70');update(ready,'E69')
    before=files(ready,'E70')
    bar(ready.catalog.engine,day='2026-02-01');update(ready,'E70')
    bar(ready.catalog.engine,day='2020-01-01',close='5');r=update(ready,'E70')
    assert r['source_metrics']['payload_rows']==1
    assert obj(ready,'E70','2020-01-01')['data']['close']=='5'
    bar(ready.catalog.engine,day='2025-01-02',close='6');update(ready,'E70')
    assert obj(ready,'E70','2025-01-02')['data']['close']=='6'
    assert set(before)&set(files(ready,'E70'))  # cold January 2026 survives
    factor(ready.catalog.engine,'2');update(ready,'E69')
    assert obj(ready,'E69')['data']['factor']=='2'


def test_I04_scoped_delete_sparse_empty_and_failed_source(ready):
    bar(ready.catalog.engine);bar(ready.catalog.engine,subject='510500.SH')
    update(ready,'E70')
    with ready.catalog.engine.begin() as c:c.execute(text("DELETE FROM etf_daily_bars WHERE ts_code='510300.SH'"))
    update(ready,'E70')
    assert not obj(ready,'E70')['found']
    assert obj(ready,'E70',subject='510500.SH')['found']
    publish(ready.catalog.engine);update(ready)
    body=deepcopy(sample('E50').content);body['item']=[]
    publish(ready.catalog.engine,body=body,now=NOW+timedelta(seconds=1));assert update(ready)['complete']
    assert obj(ready)['found']  # archive removed is not business withdrawal
    with Session(ready.catalog.engine) as s,s.begin():
        repo=CollectionRepository(s);old=repo.read('stock_daily','000001.SZ','default',with_data=False)
        repo.fail('stock_daily','000001.SZ','default',expected=old.revision,kind='temporary',now=NOW+timedelta(seconds=2))
    with pytest.raises(NativeInputError,match='最近采集'):update(ready)
    assert obj(ready)['found']
    publish(ready.catalog.engine,now=NOW+timedelta(seconds=3));assert update(ready)['complete']


def test_I05_identifier_allocated_before_other_commit_and_rollback(ready):
    update(ready)  # bootstrap empty first
    body=deepcopy(sample('E50').content)
    body['thscode']='000002.SZ';body['item'][0]['thscode']='000002.SZ'
    identity=uuid4()
    # T1 allocates and writes before T2, then commits after T2 is consumed.
    # Separate subjects avoid source-range writer serialization obscuring it.
    with ready.catalog.engine.connect() as t1:
        tx=t1.begin()
        t1.execute(Observation.__table__.insert().values(id=identity,dataset='stock_daily',subject='000002.SZ',variant='default',
            observed_at=NOW,data_json=native_json(body),request_json='[]',content_hash=digest(body),row_count=1,chain_depth=0))
        publish(ready.catalog.engine);update(ready)
        tx.commit()
    assert update(ready)['normalized_units']==1
    assert obj(ready,subject='000002.SZ')['found']
    with ready.catalog.engine.connect() as c:
        tx=c.begin();c.execute(text("DELETE FROM tonghuashun_observations WHERE id=:id"),{'id':identity});tx.rollback()
    assert update(ready)['source_metrics']['payload_rows']==0


def test_I06_new_revision_during_processing_survives_ack(ready,monkeypatch):
    bar(ready.catalog.engine)
    original=incremental.acknowledge;changed=False
    def concurrent(sources,job,**kw):
        nonlocal changed
        if not changed:
            changed=True;bar(ready.catalog.engine,close='7')
        return original(sources,job,**kw)
    monkeypatch.setattr(incremental,'acknowledge',concurrent)
    first=update(ready,'E70',options=PipelineOptions(maximum_passes=1))
    assert not first['complete'] and first['incremental']['remaining_ranges']==1
    assert obj(ready,'E70')['data']['close']=='4.15'
    assert update(ready,'E70')['complete']
    assert obj(ready,'E70')['data']['close']=='7'


@pytest.mark.parametrize('point',['before_commit','after_commit'])
def test_I07_exit_replays_only_active_range_without_new_generation(ready,monkeypatch,point):
    from app.data_store import pipeline
    bar(ready.catalog.engine)
    target=incremental if point=='after_commit' else pipeline
    name='acknowledge' if point=='after_commit' else '_commit_partition'
    original=getattr(target,name)
    def exit_now(*args,**kwargs):raise SystemExit(73)
    monkeypatch.setattr(target,name,exit_now)
    with pytest.raises(SystemExit):update(ready,'E70')
    before=files(ready,'E70');generation=ready.catalog.dataset(BY_ID['E70'].spec.name)['generation']
    monkeypatch.setattr(target,name,original)
    assert update(ready,'E70')['complete']
    assert obj(ready,'E70')['found']
    if point=='after_commit':
        assert files(ready,'E70')==before
        assert ready.catalog.dataset(BY_ID['E70'].spec.name)['generation']==generation


def test_I08_bounded_bootstrap_continues_with_new_input(ready):
    for n in range(4):bar(ready.catalog.engine,day=f'2025-0{n+1}-01')
    options=PipelineOptions(maximum_passes=1)
    first=update(ready,'E70',options=options)
    assert not first['complete'] and first['source_metrics']['payload_rows']==1
    with ready.catalog.transaction() as c:
        assert not check_coverage(c,BY_ID['E70'],read_entry_status(ready,'E70'))['satisfied']
    bar(ready.catalog.engine,day='2020-01-01')
    counts=first['source_metrics']['payload_rows']
    for _ in range(8):
        r=update(ready,'E70',options=options);counts+=r['source_metrics']['payload_rows']
        if r['complete']:break
    assert r['complete'] and counts==5
    assert obj(ready,'E70','2020-01-01')['found']


def test_I09_intermediate_unique_key_equal_confirmation_and_bad_dependency(ready):
    body=deepcopy(sample('E50').content)
    publish(ready.catalog.engine,body=body);update(ready)
    before=obj(ready)['basis'] if 'basis' in obj(ready) else None
    # A middle observation contains a unique older date; the latest head omits
    # it. Both observations must be consumed, never just the latest head.
    middle=deepcopy(body);middle['item'][0]['date_ms']=DAY-86400000
    publish(ready.catalog.engine,body=middle,now=NOW+timedelta(seconds=1))
    publish(ready.catalog.engine,body=body,now=NOW+timedelta(seconds=2))
    r=update(ready);assert r['complete'] and obj(ready,day='2026-01-01')['found']
    assert r['source_metrics']['payload_rows']==2
    # Repeated equal returned values are a new confirmation, not a cold no-op.
    generation=ready.catalog.dataset(BY_ID['E50'].spec.name)['generation']
    publish(ready.catalog.engine,body=body,now=NOW+timedelta(seconds=3));update(ready)
    assert ready.catalog.dataset(BY_ID['E50'].spec.name)['generation']>generation
    with ready.catalog.engine.begin() as c:
        c.execute(text("UPDATE tonghuashun_observations SET content_hash=repeat('a',64) WHERE observed_at=:t"),{'t':NOW})
    with pytest.raises(NativeInputError):update(ready)
    assert not read_entry_status(ready,'E50')['complete']


def test_I10_entry_failure_keeps_other_market_updates_and_full_proof(ready):
    publish(ready.catalog.engine);bar(ready.catalog.engine)
    update(ready);update(ready,'E70')
    old=read_entry_status(ready,'E50')['full_coverage']
    with ready.catalog.engine.begin() as c:c.execute(text("UPDATE tonghuashun_observations SET content_hash=repeat('b',64)"))
    for n in range(4):
        bar(ready.catalog.engine,close=str(5+n))
        results=run_local(ready,NativeSources(ready.catalog.engine),entries=[BY_ID['E50'],BY_ID['E70']],options=PipelineOptions(mode='retry'))
        assert results[0]['outcome']=='failed' and results[1]['complete']
        assert obj(ready,'E70')['data']['close']==str(5+n)
        assert read_entry_status(ready,'E50')['full_coverage']==old


def test_I12_bulk_import_real_publish_path(ready):
    from app.data_ingestion.tonghuashun.dump_service import publish_batch
    generation=uuid4();body=sample('E50').content
    with Session(ready.catalog.engine) as s,s.begin():
        s.add(Import(dataset='stock_daily_dump',generation=generation,status='ready',started_at=NOW,
            lease_until=NOW+timedelta(hours=1),digest='a'*64,metadata_json=json.dumps({'observed_start':DAY,'observed_end':DAY}),
            total_subjects=1,imported_subjects=0,superseded_subjects=0))
        s.flush();s.add(Stage(dataset='stock_daily_dump',subject='000001.SZ',data_json=native_json(body)))
    result=publish_batch('stock_daily_dump',generation,SimpleNamespace(batch_size=10,mode='reconcile'),ready.catalog.engine,NOW)
    assert result['imported']==1
    assert update(ready)['complete'] and obj(ready)['found']


def test_I07_capacity_failure_then_same_budget_continuation_preserves_ranges(ready,monkeypatch):
    from app.data_store import pipeline
    from app.data_store.errors import DataStoreError
    for month in range(1,4):bar(ready.catalog.engine,day=f'2025-{month:02}-01')
    assert not update(ready,'E70',options=PipelineOptions(maximum_passes=1))['complete']
    before=files(ready,'E70');original=pipeline._commit_partition
    def pressure(*args,**kwargs):raise DataStoreError('SCRATCH_BUDGET_EXCEEDED')
    monkeypatch.setattr(pipeline,'_commit_partition',pressure)
    with pytest.raises(DataStoreError):update(ready,'E70',options=PipelineOptions(maximum_passes=1))
    assert files(ready,'E70')==before
    monkeypatch.setattr(pipeline,'_commit_partition',original)
    r=update(ready,'E70',options=PipelineOptions(maximum_passes=1))
    assert r['source_metrics']['payload_rows']==0  # sealed month was not reread
    assert update(ready,'E70')['complete']
    for month in range(1,4):assert obj(ready,'E70',f'2025-{month:02}-01')['found']


def test_I11_small_calendar_delete_and_big_table_ranges_coexist(ready):
    with ready.catalog.engine.begin() as c:
        c.execute(text('CREATE TABLE trading_calendar_days(exchange text,calendar_date date,is_open boolean)'))
        c.execute(text("INSERT INTO trading_calendar_days VALUES ('SSE','2026-01-02',true)"))
    bar(ready.catalog.engine);update(ready,'E70');update(ready,'E68')
    with ready.catalog.engine.begin() as c:c.execute(text('DELETE FROM trading_calendar_days'))
    results=run_local(ready,NativeSources(ready.catalog.engine),entries=[BY_ID['E68'],BY_ID['E70']])
    assert all(r['complete'] for r in results)
    assert next(r for r in results if r['entry_id']=='E70')['source_metrics']['payload_rows']==0
    assert not files(ready,'E68') and obj(ready,'E70')['found']


def test_I12_disabled_capture_refused_and_explicit_reconcile_repairs_manual_delete(ready):
    bar(ready.catalog.engine);update(ready,'E70')
    with ready.catalog.engine.begin() as c:
        c.execute(text('ALTER TABLE etf_daily_bars DISABLE TRIGGER data_store_capture_range'))
        c.execute(text('DELETE FROM etf_daily_bars'))
    with pytest.raises(NativeInputError) as error:update(ready,'E70')
    assert error.value.code=='SOURCE_INCREMENTAL_NOT_INITIALIZED'
    with ready.catalog.engine.begin() as c:c.execute(text('ALTER TABLE etf_daily_bars ENABLE TRIGGER data_store_capture_range'))
    assert update(ready,'E70',options=PipelineOptions(mode='rebuild'))['complete']
    assert not obj(ready,'E70')['found']


def test_default_scheduler_and_cli_use_incremental_path(ready,monkeypatch,capsys):
    from tests.test_data_store_failure_isolation import scheduler
    from app.data_store import scheduler_tasks as tasks
    from app.data_store.__main__ import main
    from app.db import session
    from tests.test_data_store_kernel import KEY
    bar(ready.catalog.engine);publish(ready.catalog.engine)
    ctx=scheduler(ready,monkeypatch)
    params=tasks.LocalUpdateParameters(datasets=[BY_ID['E50'].spec.name,BY_ID['E70'].spec.name])
    tasks.update_local(ctx,params)
    for _ in range(2):
        result=tasks.update_local(ctx,params)
        assert result['entries']==2
        assert all(read_entry_status(ready,e)['source_metrics']['payload_rows']==0 for e in ('E50','E70'))
    monkeypatch.setattr(session,'get_engine',lambda:ready.catalog.engine)
    monkeypatch.setenv('QF_CURSOR_SIGNING_KEY',KEY.decode())
    capsys.readouterr()
    assert main(['update','--root',str(ready.files.root),'--entry','E50','--entry','E70'])==0
    output=json.loads(capsys.readouterr().out)
    assert all(e['source_metrics']['payload_rows']==0 for e in output['entries'])


def test_capture_migration_rolls_back_atomically_and_metadata_matches(ready):
    from sqlalchemy import MetaData
    from alembic.migration import MigrationContext
    from alembic.autogenerate import compare_metadata
    from app.data_store.source_range_tables import metadata
    with ready.catalog.engine.connect() as c:
        context=MigrationContext.configure(c,opts={'compare_type':True,'compare_server_default':True,
            'include_object':lambda obj,name,type_,reflected,compare_to:type_!='table' or name in metadata.tables})
        assert compare_metadata(context,metadata)==[]
    with ready.catalog.engine.begin() as c:
        with pytest.raises(Exception):
            # TRUNCATE is explicitly outside captured DML and must not silently
            # erase a table without creating native pending ranges.
            with c.begin_nested():c.execute(text('TRUNCATE etf_daily_bars'))


def test_source_receipts_are_decoded_once_per_observation(ready,monkeypatch):
    from app.data_store import local_sources
    body=deepcopy(sample('E50').content)
    body['item']=[dict(body['item'][0],date_ms=DAY+n*86400000) for n in range(1000)]
    publish(ready.catalog.engine,body=body)
    with ready.catalog.engine.connect() as c:
        encoded=c.execute(text('SELECT row_to_json(t)::text FROM tonghuashun_observations t')).scalar_one()
    row=local_sources._json(encoded,32*1024**2);calls=[];original=local_sources._json
    def counted(value,limit):
        if value==row['request_json']:calls.append(value)
        return original(value,limit)
    monkeypatch.setattr(local_sources,'_json',counted)
    local_sources.materialize_observation(row,lambda _:None)
    assert len(calls)==1


def test_additive_capture_installation_rolls_back_with_source_intact(store):
    for model in (Observation,State,EtfDailyBar,EtfAdjustmentFactor):
        model.__table__.create(store.catalog.engine)
    with store.catalog.engine.begin() as c:
        c.execute(EtfDailyBar.__table__.insert().values(source='tushare',ts_code='510300.SH',
            trade_date=date(2026,1,2),open=1,high=2,low=1,close=2,vol=1,amount=1))
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    from importlib import import_module
    migration=import_module('app.db.migrations.versions.20261008_01_current_source_ranges')
    with pytest.raises(RuntimeError,match='rollback'):
        with store.catalog.engine.begin() as c:
            with Operations.context(MigrationContext.configure(c)):migration.upgrade()
            raise RuntimeError('rollback')
    with store.catalog.engine.connect() as c:
        assert c.execute(text("SELECT to_regclass('data_store_capture_version')")).scalar_one() is None
        assert c.execute(text('SELECT count(*) FROM etf_daily_bars')).scalar_one()==1
    with store.catalog.engine.begin() as c:
        with Operations.context(MigrationContext.configure(c)):migration.upgrade()
        assert c.execute(text('SELECT count(*) FROM data_store_source_ranges')).scalar_one()==1


def test_bad_month_does_not_hide_valid_sibling_month_deletion(ready):
    bar(ready.catalog.engine);bar(ready.catalog.engine,day='2025-01-01');update(ready,'E70')
    with ready.catalog.engine.begin() as c:
        c.execute(text("UPDATE etf_daily_bars SET close=100 WHERE trade_date='2026-01-02'"))
        c.execute(text("DELETE FROM etf_daily_bars WHERE trade_date='2025-01-01'"))
    r=update(ready,'E70')
    assert r['complete'] and not r['qualified']
    assert not obj(ready,'E70','2025-01-01')['found']


def test_I08_continuous_hot_writes_do_not_block_initial_boundary(ready,monkeypatch):
    bar(ready.catalog.engine,day='2025-01-01');bar(ready.catalog.engine,day='2025-02-01')
    original=incremental.acknowledge;number=[0]
    def producer(sources,job,**kwargs):
        number[0]+=1
        bar(ready.catalog.engine,day='2025-01-01',close=str(5+number[0]%2))
        return original(sources,job,**kwargs)
    monkeypatch.setattr(incremental,'acknowledge',producer)
    options=PipelineOptions(maximum_passes=1)
    first=update(ready,'E70',options=options)
    assert not first['incremental']['bootstrap_complete']
    second=update(ready,'E70',options=options)
    assert not second['complete']  # producer is still adding new dirty work
    assert second['incremental']['bootstrap_complete']
    assert second['full_coverage']['complete']
    assert obj(ready,'E70','2025-02-01')['found']
    with ready.catalog.transaction() as c:
        assert check_coverage(c,BY_ID['E70'],second)['reason']=='CURRENT_INPUT_PENDING'
    monkeypatch.setattr(incremental,'acknowledge',original)
    assert update(ready,'E70')['complete']


def test_multi_month_claim_resumes_same_order_after_partial_commit(ready,monkeypatch):
    from app.data_store import pipeline
    for month in (3,1,2):bar(ready.catalog.engine,day=f'2025-{month:02}-01')
    original=pipeline._commit_partition;calls=[0]
    def interrupted(*args,**kwargs):
        calls[0]+=1
        if calls[0]==2:raise SystemExit(74)
        return original(*args,**kwargs)
    monkeypatch.setattr(pipeline,'_commit_partition',interrupted)
    with pytest.raises(SystemExit):update(ready,'E70')
    before=files(ready,'E70')
    monkeypatch.setattr(pipeline,'_commit_partition',original)
    r=update(ready,'E70')
    assert r['complete'] and r['source_metrics']['payload_rows']==0
    assert set(before)<=set(files(ready,'E70'))
    for month in (1,2,3):assert obj(ready,'E70',f'2025-{month:02}-01')['found']
