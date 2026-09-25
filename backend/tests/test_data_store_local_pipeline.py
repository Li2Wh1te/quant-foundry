"""Small LF-D02 critical-path checks; synthetic data, never supplier/production IO."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_data_store_kernel import database, limits, store  # explicit isolated PG fixture
from app.data_store.tables import entry_status
from app.data_store.adapters.contracts import LocalInput, digest, native_json
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID, BY_NATIVE, SYNTHETIC
from app.data_store.pipeline import run_entry, PipelineOptions
from app.data_store.readers import Query
from app.data_store.errors import DataStoreError

NOW=datetime(2026,1,1,tzinfo=timezone.utc)


class Inputs:
    def __init__(self,*rows):self.rows=rows;self.summary={}
    def iter_entry(self,entry):
        self.summary={'complete':False}
        yield from self.rows
        self.summary={'complete':True,'source_rows':len(self.rows),'state':'present' if self.rows else 'empty'}


def company(name='X',n=1,*,subject='C1',failure=None):
    body={'item':[{'company_id':subject,'company_name':name,'fund_count':1}]}
    return LocalInput('tonghuashun','fund_company',subject,'default',NOW+timedelta(seconds=n),
                      body,digest([body,n]),failure=failure)


@pytest.fixture
def ready(store):
    with store.catalog.engine.begin() as c:entry_status.create(c)
    return store


def rows(store,entry,unit,qualified=True):
    p=entry.spec.partitioner((*unit.key,'root'))
    return store.read(entry.spec,Query(partitions=(p,),page_size=100,require_qualified=qualified)).rows


def test_C02_C07_new_confirmation_old_conflicting_value_and_missing_key(ready):
    e=BY_ID['E41']
    for value in (company('X',1),company('X',3),company('Y',2),company('old',0)):
        result=run_entry(ready,e,Inputs(value))
        assert result['complete']
    u=list(normalize(e,company()))[0]
    result=rows(ready,e,u)
    assert result[0]['f0_name']=='X'
    assert int(result[0]['basis_ns'])==int((NOW+timedelta(seconds=3)).timestamp())*10**9
    run_entry(ready,e,Inputs(company('Z',4),company('historic',0,subject='C2')))
    assert rows(ready,e,u)[0]['f0_name']=='Z'
    missing=list(normalize(e,company('historic',0,subject='C2')))[0]
    assert rows(ready,e,missing)[0]['f0_name']=='historic'


def test_C03_C04_old_bad_does_not_block_new_good_and_new_bad_is_scoped(ready):
    e=BY_ID['E41'];good=company('good',3)
    run_entry(ready,e,Inputs(good,company('other',1,subject='C2')))
    old=company('old',1,failure='DUPLICATE_BUSINESS_KEY')
    run_entry(ready,e,Inputs(old))
    u=list(normalize(e,good))[0]
    assert rows(ready,e,u)[0]['f0_name']=='good'
    run_entry(ready,e,Inputs(company('bad',4,failure='DUPLICATE_BUSINESS_KEY')))
    with pytest.raises(DataStoreError) as error: rows(ready,e,u)
    assert error.value.code=='DATA_RESTRICTED'
    other=list(normalize(e,company('other',1,subject='C2')))[0]
    p=e.spec.partitioner((*other.key,'root'))
    safe=ready.read(e.spec,Query(partitions=(p,),lower=other.key,upper=(*other.key[:2],other.key[2]+'\x00')))
    assert safe.rows[0]['f0_name']=='other'
    run_entry(ready,e,Inputs(company('fixed',5)))
    assert rows(ready,e,u)[0]['f0_name']=='fixed'


def test_C08_explicit_withdrawal_never_resurrected_by_old_input(ready):
    e=BY_ID['E41'];raw=company('X',1)
    run_entry(ready,e,Inputs(raw))
    withdrawal=replace(company('X',3),content={'item':[]},withdrawals=('current',))
    run_entry(ready,e,Inputs(withdrawal))
    run_entry(ready,e,Inputs(raw))
    u=list(normalize(e,raw))[0]
    assert rows(ready,e,u)==[]


def test_repeat_is_idempotent_and_keeps_current_files(ready):
    e=BY_ID['E41'];raw=company()
    run_entry(ready,e,Inputs(raw))
    before=ready.catalog.dataset(e.spec.name)
    result=run_entry(ready,e,Inputs(raw))
    assert result['metrics']['files_written']==0
    assert ready.catalog.dataset(e.spec.name)['generation']==before['generation']


def test_C09_real_tick_file_query_precision_and_distinct_sequence(ready):
    e=SYNTHETIC[0]
    def one(sequence):
        body=dict(source_code='TEST.SH',trading_date='2026-01-01',session='auction',channel='A',sequence=sequence,
                  event_ns=1767225600000000123,price=Decimal('12345678901234567890.123456789012345678'),quantity=1,
                  currency='CNY',price_basis='raw')
        return LocalInput('synthetic','tick','TEST.SH','default',NOW,body,digest(body))
    run_entry(ready,e,Inputs(one(1),one(2)))
    units=list(normalize(e,one(1)))
    result=rows(ready,e,units[0]);assert len(result)==2
    assert result[0]['f0_event_ns']=='1767225600000000123'
    assert result[0]['f0_price']=='12345678901234567890.123456789012345678'


def test_C14_native_disabled_source_rescan_sees_late_commits_without_supplier(ready,monkeypatch):
    from app.data_store.local_sources import NativeSources
    import requests
    def forbidden(*args,**kwargs):raise AssertionError('Supplier call is forbidden')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    e=BY_ID['E68'];engine=ready.catalog.engine
    with engine.begin() as c:
        c.execute(text('CREATE TABLE trading_calendar_days (exchange text,calendar_date date,is_open boolean,PRIMARY KEY(exchange,calendar_date))'))
        c.execute(text("CREATE TABLE data_sources (key text PRIMARY KEY,enabled boolean)"))
        c.execute(text("INSERT INTO data_sources VALUES ('tushare',false)"))
        c.execute(text("INSERT INTO trading_calendar_days VALUES ('SSE','2026-01-01',false),('SSE','2026-01-02',true)"))
    native=NativeSources(engine)
    scan=native.iter_entry(e);first=next(scan)
    with engine.begin() as c:
        c.execute(text("INSERT INTO trading_calendar_days VALUES ('SSE','2025-12-31',true)"))
        c.execute(text("UPDATE trading_calendar_days SET is_open=false WHERE calendar_date='2026-01-02'"))
    remaining=list(scan)
    assert len(remaining)==1  # fixed repeatable-read snapshot, not a moving cursor
    result=run_entry(ready,e,native)
    assert result['complete'] and result['normalized_units']==3*result['passes']
    with engine.connect() as c:
        assert c.execute(text("SELECT enabled FROM data_sources WHERE key='tushare'")).scalar_one() is False
    for raw in native.iter_entry(e):
        if str(raw.content['calendar_date'])=='2026-01-02':
            unit=list(normalize(e,raw))[0];p=e.spec.partitioner((*unit.key,'root'))
            found=ready.read(e.spec,Query(partitions=(p,),lower=unit.key,upper=(*unit.key[:2],unit.key[2]+'\x00')))
            assert found.rows[0]['f0_is_open'] is False


def test_C06_complete_report_bad_member_stays_blocked_when_only_sibling_changes(ready):
    e=BY_ID['E57']
    def report(rows,n):
        body={'item':rows};return LocalInput('tonghuashun','index_constituents','INDEX.SH','default',
            NOW+timedelta(seconds=n),body,digest([body,n]))
    good=report([{'thscode':'A.SH','name':'A'},{'thscode':'B.SH','name':'B'}],1)
    unit=list(normalize(e,good))[0]
    run_entry(ready,e,Inputs(good))
    run_entry(ready,e,Inputs(report([{'thscode':None,'name':'A'},{'thscode':'B.SH','name':'new B'}],2)))
    run_entry(ready,e,Inputs(report([{'thscode':'B.SH','name':'newer B'},{'thscode':None,'name':'A'}],3)))
    with pytest.raises(DataStoreError):rows(ready,e,unit)
    run_entry(ready,e,Inputs(report([{'thscode':'A.SH','name':'fixed A'},{'thscode':'B.SH','name':'newer B'}],4)))
    from app.data_store.domain_reader import read_object
    obj=read_object(ready,e.id,*unit.key)
    assert obj['found'] and len(obj['data']['members'])==2


def test_B05_harness_only_tiny_development_smoke_not_capacity_acceptance(database,tmp_path,limits):
    import importlib.util
    path=Path(__file__).parents[1]/'scripts/benchmark_local_ticks.py'
    spec=importlib.util.spec_from_file_location('local_b05_smoke',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    # B05 uses a 512 MiB default query engine. The generic D01 fixture has
    # an intentionally tiny 64 MiB budget; this smoke keeps a finite 128 MiB.
    from app.data_store.storage import CurrentStore
    from tests.test_data_store_kernel import probe,KEY
    with database[0].begin() as c:entry_status.create(c)
    root=tmp_path/'b05';root.mkdir(mode=0o700)
    with probe(),CurrentStore(database[0],root,cursor_key=KEY,initialize=True,
                      limits=replace(limits,duckdb_memory_bytes=128*1024**2)) as bench:
        result=module.execute(bench,32,2)
    assert result['complete'] and not result['full_B05_executed']
    assert result['current_rows']==34 and result['exact_event_samples']


def test_C10_report_crosses_physical_files_before_any_member_is_exposed(ready):
    e=BY_ID['E57']
    def source(bad=False):
        members=[{'thscode':f'{i:06}.SH','name':str(i)} for i in range(25)]
        if bad:members[23]['thscode']=None
        body={'item':members}
        return LocalInput('tonghuashun',e.native,'LARGE.SH','default',NOW+timedelta(seconds=2 if bad else 1),body,digest(body))
    raw=source();unit=next(normalize(e,raw))
    run_entry(ready,e,Inputs(raw))
    p=e.spec.partitioner((*unit.key,'root'));assert len(ready.catalog.files(e.spec.name,p))>=3
    from app.data_store.domain_reader import read_object
    assert len(read_object(ready,e.id,*unit.key)['data']['members'])==25
    run_entry(ready,e,Inputs(source(True)))
    with pytest.raises(DataStoreError):read_object(ready,e.id,*unit.key)


def test_C16_changed_rule_requires_explicit_bounded_rebuild(ready):
    e=BY_ID['E41'];raw=company('same',1)
    run_entry(ready,e,Inputs(raw))
    updated=replace(e)
    object.__setattr__(updated,'spec',replace(e.spec,rule='lfd02-v2-test'))
    with pytest.raises(DataStoreError) as err:run_entry(ready,updated,Inputs(raw))
    assert err.value.code=='REBUILD_REQUIRED'
    result=run_entry(ready,updated,Inputs(raw),options=PipelineOptions(mode='rebuild',allow_incompatible_rebuild=True))
    assert result['complete'] and result['qualified']


def test_additive_D02_migration_executes_without_touching_existing_kernel(store,monkeypatch):
    import importlib.util
    from sqlalchemy import inspect
    path=Path(__file__).parents[1]/'app/db/migrations/versions/20261006_01_local_entry_status.py'
    spec=importlib.util.spec_from_file_location('d02_migration',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    with store.catalog.engine.begin() as c:
        monkeypatch.setattr(mod.op,'execute',lambda statement:c.exec_driver_sql(statement))
        mod.upgrade()
        assert len(inspect(c).get_table_names())==7
        c.execute(text("INSERT INTO data_store_entry_status (entry_id,summary_json) VALUES ('E01','{}')"))
        monkeypatch.setattr(mod.op,'get_bind',lambda:c)
        with pytest.raises(RuntimeError):mod.downgrade()
