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


def test_C05_conversion_rule_failure_is_not_completion_and_can_retry(ready, monkeypatch):
    from app.data_store import pipeline

    entry = BY_ID['E41']
    source = company('rule-repair', 1)
    original = pipeline.normalize

    def incompatible_rule(candidate, raw):
        # Model a parsable source reaching a broken domain conversion rule.
        # The real pipeline still records the issue and guards publication.
        for unit in original(candidate, raw):
            yield replace(unit, rows=(), failure='CORE_VALUE_INVALID')

    monkeypatch.setattr(pipeline, 'normalize', incompatible_rule)
    failed = run_entry(ready, entry, Inputs(source))
    assert failed['complete'] and not failed['qualified']
    assert failed['input_failures'] == 1
    assert ready.catalog.issues(entry.spec.name, limit=10)
    failed_unit = next(original(entry, source))
    with pytest.raises(DataStoreError) as error:
        rows(ready, entry, failed_unit)
    assert error.value.code == 'DATA_RESTRICTED'

    # The same retained native input is reprocessed after the converter fix;
    # no new provider request or historical formal snapshot is required.
    monkeypatch.setattr(pipeline, 'normalize', original)
    corrected = run_entry(ready, entry, Inputs(source),
                          options=PipelineOptions(mode='retry'))
    assert corrected['complete'] and corrected['qualified']
    unit = next(normalize(entry, source))
    assert rows(ready, entry, unit)[0]['f0_name'] == 'rule-repair'
    assert ready.catalog.issues(entry.spec.name, limit=10) == []


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
    assert result['complete'] and result['normalized_units']==3
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


def calendar_rows(ready, entry):
    with ready.catalog.transaction() as c:
        partitions=tuple(c.execute(text('SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:d'),
                                   {'d':entry.spec.name}).scalars())
    return ready.read(entry.spec,Query(partitions=partitions or ('default',),page_size=100)).rows


def calendar_table(ready, dates):
    with ready.catalog.engine.begin() as c:
        c.execute(text('CREATE TABLE trading_calendar_days (exchange text,calendar_date date,is_open boolean,PRIMARY KEY(exchange,calendar_date))'))
        c.execute(text("INSERT INTO trading_calendar_days VALUES ('SSE',:day,true)"),
                  [{'day': day} for day in dates])


def test_native_full_scan_over_256_partitions_resumes_after_store_restart(ready):
    from app.data_store.local_sources import NativeSources
    from app.data_store.storage import CurrentStore
    from tests.test_data_store_kernel import KEY
    entry = BY_ID['E68']
    calendar_table(ready, [f'{2000+i//12}-{1+i%12:02}-01' for i in range(300)])
    first = run_entry(ready, entry, NativeSources(ready.catalog.engine))
    assert not first['complete'] and first['committed_partitions'] == 256
    assert first['source_rows'] == first['normalized_units'] == 300
    with ready.catalog.transaction() as c:
        before = dict(c.execute(text('SELECT path,content_hash FROM data_store_files')).all())
    # Recovery must preserve the charged sealed scan after all process locks
    # have gone away. A newly constructed store models a different invocation.
    ready.budget.sweep()
    with CurrentStore(ready.catalog.engine, ready.files.root, cursor_key=KEY, limits=ready.limits) as reopened:
        second = run_entry(reopened, entry, NativeSources(ready.catalog.engine))
        assert second['complete'] and second['resumed']
        assert second['committed_partitions'] == 44
        assert second['source_rows'] == second['normalized_units'] == 0
        assert second['last_partition'] > first['last_partition']
        with reopened.catalog.transaction() as c:
            after = dict(c.execute(text('SELECT path,content_hash FROM data_store_files')).all())
        assert len(after) == 300 and before.items() <= after.items()
    assert not list(ready.files.root.glob('.scratch/*/spill/*.sqlite'))


def test_native_unchanged_scan_noop_and_one_changed_partition(ready):
    from app.data_store.local_sources import NativeSources
    entry = BY_ID['E68']
    calendar_table(ready, ['2025-01-01','2025-02-01'])
    run_entry(ready, entry, NativeSources(ready.catalog.engine))
    generation = ready.catalog.dataset(entry.spec.name)['generation']
    repeat = run_entry(ready, entry, NativeSources(ready.catalog.engine))
    assert repeat['metrics']['files_written'] == 0
    assert ready.catalog.dataset(entry.spec.name)['generation'] == generation
    with ready.catalog.engine.begin() as c:
        c.execute(text("UPDATE trading_calendar_days SET is_open=false WHERE calendar_date='2025-02-01'"))
    changed = run_entry(ready, entry, NativeSources(ready.catalog.engine))
    assert changed['metrics']['files_written'] == 1
    assert ready.catalog.dataset(entry.spec.name)['generation'] == generation+1


def test_native_complete_snapshot_deletes_missing_keys_including_empty_partition(ready):
    from app.data_store.local_sources import NativeSources
    entry = BY_ID['E68']
    calendar_table(ready, ['2025-01-01','2025-01-02','2025-02-01'])
    run_entry(ready, entry, NativeSources(ready.catalog.engine))
    with ready.catalog.engine.begin() as c:
        c.execute(text("DELETE FROM trading_calendar_days WHERE calendar_date <> '2025-01-01'"))
    result = run_entry(ready, entry, NativeSources(ready.catalog.engine))
    assert result['complete']
    assert len(calendar_rows(ready, entry)) == 1
    with ready.catalog.engine.begin() as c:
        c.execute(text('DELETE FROM trading_calendar_days'))
    result = run_entry(ready, entry, NativeSources(ready.catalog.engine))
    assert result['complete'] and calendar_rows(ready, entry) == []


@pytest.mark.parametrize('failure', ['incomplete','exception'])
def test_native_unfinished_scan_never_deletes_current(ready, failure):
    from app.data_store.local_sources import NativeSources
    from app.data_store.adapters.canonical import NativeInputError
    entry = BY_ID['E68']
    calendar_table(ready, ['2025-01-01','2025-01-02'])
    run_entry(ready, entry, NativeSources(ready.catalog.engine))
    generation = ready.catalog.dataset(entry.spec.name)['generation']
    class Truncated(NativeSources):
        def iter_entry(self, entry):
            source = super().iter_entry(entry)
            try:
                yield next(source)
                if failure == 'exception':
                    raise NativeInputError('SOURCE_BUDGET_EXCEEDED', '测试超时。')
            finally:
                source.close()
    with pytest.raises(NativeInputError):
        run_entry(ready, entry, Truncated(ready.catalog.engine))
    assert ready.catalog.dataset(entry.spec.name)['generation'] == generation
    assert len(calendar_rows(ready, entry)) == 2
    assert run_entry(ready, entry, NativeSources(ready.catalog.engine))['complete']


def test_resume_catalog_commit_before_spool_cursor_never_merges_twice(ready, monkeypatch):
    from app.data_store import pipeline
    from app.data_store.local_sources import NativeSources
    entry = BY_ID['E68']
    calendar_table(ready, ['2025-01-01','2025-02-01'])
    original = pipeline._commit_partition
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('process stopped after catalog commit')
    with monkeypatch.context() as patch:
        patch.setattr(pipeline, '_commit_partition', interrupted)
        with pytest.raises(RuntimeError):
            run_entry(ready,entry,NativeSources(ready.catalog.engine))
    result = run_entry(ready,entry,NativeSources(ready.catalog.engine))
    assert result['complete'] and result['resumed']
    assert result['source_rows'] == 0 and result['committed_partitions'] == 1
    assert len(calendar_rows(ready, entry)) == 2


def _crash_pipeline_after_catalog_commit(schema, root, policy):
    import os
    from app.data_store import pipeline
    from app.data_store.local_sources import NativeSources
    from app.data_store.storage import CurrentStore
    from tests.test_data_store_kernel import KEY, make_engine, probe
    # A fresh interpreter gets its own connection pool and no inherited locks.
    # Only the isolated schema and real shared files survive the process exit.
    engine=make_engine(schema)
    original = pipeline._commit_partition
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    pipeline._commit_partition = crash
    with probe(), CurrentStore(engine,root,cursor_key=KEY,limits=policy) as child:
        pipeline.run_entry(child,BY_ID['E68'],NativeSources(engine))


def test_real_process_death_after_commit_resumes_without_source_scan(ready, database):
    import multiprocessing
    from app.data_store.local_sources import NativeSources
    calendar_table(ready,['2025-01-01','2025-02-01'])
    child=multiprocessing.get_context('spawn').Process(
        target=_crash_pipeline_after_catalog_commit,
        args=(database[1],ready.files.root,ready.limits))
    child.start()
    try:
        child.join(20)
        assert child.exitcode == 73
    finally:
        if child.is_alive():
            child.kill();child.join()
    result=run_entry(ready,BY_ID['E68'],NativeSources(ready.catalog.engine))
    assert result['complete'] and result['resumed']
    assert result['source_rows']==0 and result['committed_partitions']==1
    assert len(calendar_rows(ready,BY_ID['E68']))==2


def test_idle_continuations_remain_charged_and_are_reclaimed_only_when_done(ready):
    from app.data_store.budget import Budget
    from app.data_store.limits import MiB
    budget=Budget(ready.files,ready.locks,replace(ready.limits,scratch_bytes=64*MiB,
        duckdb_memory_bytes=16*MiB,query_bytes=8*MiB,batch_bytes=16*MiB,commit_bytes=MiB,
        operation_slots=3))
    for key in ('pipeline.E68','pipeline.E70'):
        with budget.reserve('read',pending=key) as reservation:
            (reservation.spill/'pending.sqlite').write_bytes(b'pending current work')
    budget.sweep()
    with pytest.raises(DataStoreError) as error:
        with budget.reserve('read',pending='pipeline.E69'):
            pass
    assert error.value.code == 'SCRATCH_BUDGET_EXCEEDED'
    # At capacity a writer still fits, so the retained continuations can drain.
    with budget.reserve('write'):
        pass
    with budget.reserve('read',pending='pipeline.E68') as reservation:
        assert (reservation.spill/'pending.sqlite').read_bytes()==b'pending current work'
        reservation.discard=True
    with budget.reserve('read'):
        pass
    with budget.reserve('read',pending='pipeline.E70') as reservation:
        assert (reservation.spill/'pending.sqlite').exists()
        reservation.discard=True


def test_mutable_trading_status_snapshot_noop_and_deletion(ready):
    from app.data_store.local_sources import NativeSources
    entry=BY_ID['E64']
    with ready.catalog.engine.begin() as c:
        c.execute(text('CREATE TABLE trading_status_facts (ts_code text,trade_date date,dimension text,status text,quality_status text,source text)'))
        c.execute(text("INSERT INTO trading_status_facts VALUES ('A.SH','2025-01-01','suspend','normal','complete','tushare'),('B.SH','2025-01-01','suspend','normal','complete','tushare')"))
    initial=run_entry(ready,entry,NativeSources(ready.catalog.engine))
    assert initial['qualified']
    generation=ready.catalog.dataset(entry.spec.name)['generation']
    repeated=run_entry(ready,entry,NativeSources(ready.catalog.engine))
    assert repeated['metrics']['files_written']==0
    assert ready.catalog.dataset(entry.spec.name)['generation']==generation
    with ready.catalog.engine.begin() as c:
        c.execute(text("DELETE FROM trading_status_facts WHERE ts_code='B.SH'"))
    assert run_entry(ready,entry,NativeSources(ready.catalog.engine))['complete']
    assert len(calendar_rows(ready,entry))==1


def test_run_local_drains_continuations_and_does_not_starve_later_entries(ready):
    from app.data_store.budget import Budget
    from app.data_store.limits import MiB
    from app.data_store.pipeline import run_local
    policy=replace(ready.limits,scratch_bytes=64*MiB,duckdb_memory_bytes=16*MiB,
                   query_bytes=8*MiB,batch_bytes=16*MiB,commit_bytes=MiB,operation_slots=3)
    ready.limits=policy
    ready.budget=Budget(ready.files,ready.locks,policy)
    entries=[]
    for number in range(41,47):
        entry=replace(BY_ID['E41'],id=f'E{number}')
        object.__setattr__(entry,'spec',replace(entry.spec,name=f'resume.e{number}'))
        entries.append(entry)
    class Counted(Inputs):
        def __init__(self, *rows):
            super().__init__(*rows)
            self.calls={}
        def iter_entry(self, entry):
            self.calls[entry.id]=self.calls.get(entry.id,0)+1
            yield from super().iter_entry(entry)
    source=Counted(company(subject='C1'),company(subject='C2'))
    assert len({entries[0].spec.partitioner((*next(normalize(entries[0],r)).key,'root'))
                for r in source.rows}) == 2
    for _ in range(8):
        try:
            results=run_local(ready,source,entries=entries,options=PipelineOptions(maximum_passes=1))
        except DataStoreError as error:
            assert error.code=='SCRATCH_BUDGET_EXCEEDED'
            continue
        if all(result['complete'] for result in results):
            break
    else:
        pytest.fail('A batch never completes because finished entries restart while siblings are pending')
    assert source.calls=={entry.id:1 for entry in entries}
    with ready.catalog.transaction() as c:
        counts=dict(c.execute(text('SELECT dataset,sum(row_count) FROM data_store_files GROUP BY dataset')).all())
    assert len(counts)==6 and all(value==2 for value in counts.values())

    # Finishing a sweep must not freeze subsequent updates. The next invocation
    # starts exactly one fresh acquisition per entry and can finish naturally.
    for _ in range(8):
        try:
            results=run_local(ready,source,entries=entries,options=PipelineOptions(maximum_passes=1))
        except DataStoreError as error:
            assert error.code=='SCRATCH_BUDGET_EXCEEDED'
            continue
        if all(result['complete'] for result in results):
            break
    else:
        pytest.fail('The next complete batch cannot finish')
    assert source.calls == {entry.id:2 for entry in entries}


@pytest.mark.parametrize('entry_id', ['E41', 'B05'])
def test_D06_persisted_schema_and_capability_exclude_recomputable_fields(ready, entry_id):
    import pyarrow.parquet as pq
    from scripts.benchmark_local_ticks import event
    entry = BY_ID['E41'] if entry_id == 'E41' else SYNTHETIC[0]
    raw = company() if entry_id == 'E41' else event(1)
    assert run_entry(ready, entry, Inputs(raw))['complete']
    unit = next(normalize(entry, raw))
    partition = entry.spec.partitioner((*unit.key, 'root'))
    for ref in ready.catalog.files(entry.spec.name, partition):
        schema = pq.read_schema(ready.files.root / ref['path'])
        assert not {'value_hash', 'basis_valid'} & set(schema.names)
        assert 'basis_state' in schema.names
    capability = ready.describe_capability(entry.spec)
    assert capability['semantics']['row_layout'] == 'typed-object-nodes-v2'
    assert not {'value_hash', 'basis_valid'} & {f['name'] for f in capability['fields']}
    assert not list(ready.files.root.glob('.scratch/*/spill/*.sqlite'))


def test_D06_invalid_and_withdrawn_remain_debug_only(ready):
    entry = BY_ID['E41']
    raw = company()
    unit = next(normalize(entry, raw))
    run_entry(ready, entry, Inputs(raw))
    assert rows(ready, entry, unit)[0]['basis_state'] == 'valid'
    run_entry(ready, entry, Inputs(company('bad', 2, failure='CORE_VALUE_INVALID')))
    with pytest.raises(DataStoreError) as error:
        rows(ready, entry, unit)
    assert error.value.code == 'DATA_RESTRICTED'
    assert rows(ready, entry, unit, qualified=False)[0]['basis_state'] == 'invalid'
    # The row-state predicate must independently exclude invalid rows, even
    # when there is no blocking issue left to reject the entire query.
    with ready.catalog.transaction() as connection:
        connection.execute(text('DELETE FROM data_store_issues WHERE dataset=:d'), {'d': entry.spec.name})
    assert rows(ready, entry, unit) == []
    assert rows(ready, entry, unit, qualified=False)[0]['basis_state'] == 'invalid'
    withdrawal = replace(company('X', 3), content={'item': []}, withdrawals=('current',))
    run_entry(ready, entry, Inputs(withdrawal))
    assert rows(ready, entry, unit) == []
    assert rows(ready, entry, unit, qualified=False)[0]['basis_state'] == 'withdrawn'


def test_D06_v1_file_requires_explicit_rebuild_without_reinterpreting_bytes(ready):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from app.data_store.merge import value_hash
    from tests.test_data_store_kernel import source
    entry = BY_ID['E41']
    raw = company()
    unit = next(normalize(entry, raw))
    fields = list(entry.spec.schema)
    index = entry.spec.schema.get_field_index('basis_state')
    fields[index:index] = [
        pa.field('value_hash', pa.string(), nullable=False, metadata={b'max_utf8_bytes': b'64'}),
        pa.field('basis_valid', pa.bool_(), nullable=False),
    ]
    old = replace(entry.spec, schema=pa.schema(fields),
                  semantics={**entry.spec.semantics, 'row_layout': 'typed-object-nodes-v1'})
    assert old.schema_id != entry.spec.schema_id and not entry.spec.accepts(old)
    ready.register(old)
    partition = old.partitioner((*unit.key, 'root'))
    payload = [{**row, 'representation': unit.representation, 'subject': unit.subject,
                'object_key': unit.object_key, 'basis_group': unit.group, 'basis_ns': unit.order,
                'basis_token': unit.token, 'basis_state': 'valid', 'basis_valid': True,
                'value_hash': value_hash(unit.rows)} for row in unit.rows]
    ready.replace_partition(old, partition, [pa.RecordBatch.from_pylist(payload, schema=old.schema)],
                            source(ready, old.name), complete=True, source_check=lambda _: True)
    before = ready.catalog.files(old.name, partition)
    for operation in (lambda: run_entry(ready, entry, Inputs(raw)),
                      lambda: rows(ready, entry, unit)):
        with pytest.raises(DataStoreError) as error:
            operation()
        assert error.value.code == 'REBUILD_REQUIRED'
        assert ready.catalog.files(old.name, partition) == before
    # Preparing a new descriptor still must not let union_by_name reinterpret
    # the old physical shard. Only a successful explicit source rebuild replaces it.
    ready.register(entry.spec, prepare_rebuild=True)
    with pytest.raises(DataStoreError) as error:
        rows(ready, entry, unit)
    assert error.value.code == 'REBUILD_REQUIRED'
    result = run_entry(ready, entry, Inputs(raw),
                       options=PipelineOptions(mode='rebuild', allow_incompatible_rebuild=True))
    assert result['complete'] and result['qualified']
    assert rows(ready, entry, unit)[0]['f0_name'] == 'X'
    for ref in ready.catalog.files(entry.spec.name, partition):
        assert ref['schema_id'] == entry.spec.schema_id
        assert not {'value_hash', 'basis_valid'} & set(pq.read_schema(ready.files.root/ref['path']).names)
