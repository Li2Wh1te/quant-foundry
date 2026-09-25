"""LF-D01 C01/C09/C11/C12/C13/C15/C16: real PG, Parquet, DuckDB and processes.

Synthetic inputs only. The optional LF_D01_ALLOW_OVERLAY_TEST=1 probe injection
allows mechanics tests in a restricted development container; CI and container
acceptance MUST run on a real supported bind mount without that injection.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace, asdict
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time
from urllib.request import urlopen
from uuid import uuid4
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, text, URL
from sqlalchemy.exc import OperationalError

from app.data_store import locking
from app.data_store.budget import Metrics, QuotaFile, rss_bytes
from app.data_store.catalog import DDL, SourceUpdate, Issue
from app.data_store.errors import DataStoreError
from app.data_store.limits import StoreLimits, MiB
from app.data_store.maintenance import write_summary, prune_completed_runs
from app.data_store.readers import Query
from app.data_store.schema import DatasetSpec, fingerprint
from app.data_store.storage import CurrentStore
from app.data_store.values import epoch_ns

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PostgreSQL required')
KEY = b'isolated-d01-cursor-key-not-a-production-key'


def test_url():
    if os.getenv('QF_ENVIRONMENT') != 'test':
        pytest.fail('LF-D01 tests require QF_ENVIRONMENT=test')
    host = os.getenv('QF_DATABASE_HOST', '127.0.0.1')
    if host not in ('127.0.0.1', 'localhost', 'postgres'):
        pytest.fail('LF-D01 tests never connect to a production/remote database')
    return URL.create('postgresql+psycopg', username=os.getenv('QF_DATABASE_USER', 'postgres'),
        password=os.getenv('QF_DATABASE_PASSWORD', ''), host=host,
        port=int(os.getenv('QF_DATABASE_PORT', '5432')),
        database=os.getenv('QF_DATABASE_NAME', 'quant_foundry_test'))


# This helper is not itself a test.
test_url.__test__ = False


def make_engine(schema):
    return create_engine(test_url(), connect_args={'options': f'-csearch_path={schema}', 'connect_timeout': 3})


@pytest.fixture
def database():
    schema = 'lfd01_test_' + uuid4().hex
    admin = create_engine(test_url())
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA {schema}'))
        c.execute(text(f'SET LOCAL search_path={schema}'))
        for statement in DDL.split(';'):
            if statement.strip():
                c.exec_driver_sql(statement)
    engine = make_engine(schema)
    yield engine, schema
    engine.dispose()
    with admin.begin() as c:
        # Only the random schema created by this fixture, never shared/public.
        c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    admin.dispose()


@contextmanager
def probe():
    if os.getenv('LF_D01_ALLOW_OVERLAY_TEST') == '1':
        with patch.object(locking, '_filesystem_name', return_value='ext4'):
            yield
    else:
        yield


@pytest.fixture
def limits():
    return replace(StoreLimits(), file_rows=10, lock_timeout_ms=100, orphan_grace_seconds=1,
                   duckdb_memory_bytes=64*MiB, log_bytes=4096)


@pytest.fixture
def store(database, tmp_path, limits):
    root = tmp_path / 'current'
    root.mkdir(mode=0o700)
    with probe(), CurrentStore(database[0], root, cursor_key=KEY, initialize=True, limits=limits) as instance:
        yield instance


def spec(name='bars', **kwargs):
    return DatasetSpec(name, pa.schema([pa.field('id', pa.int64(), False),
                                       pa.field('price', pa.decimal128(38,8), False)]),
                       ('id',), 'r1', {'unit': 'CNY', 'representation': 'unadjusted'}, **kwargs)


def source(st, dataset='bars', scope='range', token='one', **kwargs):
    state = st.source_state(dataset, scope)
    return SourceUpdate(scope, fingerprint(token), fingerprint('ctx'), state['revision'] if state else 0,
                        **kwargs)


def batches(contract, start=0, count=20, add=0, batch_size=10):
    for begin in range(start, start+count, batch_size):
        ids = list(range(begin, min(start+count, begin+batch_size)))
        yield pa.RecordBatch.from_pydict({'id': ids, 'price': [Decimal(i+add) for i in ids]},
                                        schema=contract.schema)


def put(st, contract=None, *, start=0, count=20, add=0, token='one', partition='default', scope='range'):
    contract = contract or spec()
    return st.upsert(contract, partition, batches(contract, start, count, add),
                     source(st, contract.name, scope, token), lower=(start,), upper=(start+count,),
                     source_check=lambda current: True)


def query_all(st, contract=None):
    return st.read(contract or spec(), Query(page_size=1000)).rows


def counts(st):
    with st.catalog.transaction() as c:
        return {name: c.execute(text('SELECT count(*) FROM '+name)).scalar_one()
                for name in ('data_store_datasets','data_store_files','data_store_scopes',
                             'data_store_issues','data_store_garbage')}


def assert_error(code, call):
    with pytest.raises(DataStoreError) as caught:
        call()
    assert caught.value.code == code


def test_C01_replay_100_current_only_no_row_ledger(store):
    contract = spec(); store.register(contract)
    result = put(store)
    original = store.catalog.files('bars','default')
    original_rows = query_all(store)
    fixed = source(store)
    for _ in range(100):
        def not_read():
            raise AssertionError('unchanged verified input must not be reloaded')
            yield
        replay = store.upsert(contract,'default',not_read(),fixed,lower=(0,),upper=(20,),source_check=lambda s:True)
        assert replay.idempotent and not replay.changed
        assert replay.generation == result.generation
    assert store.catalog.files('bars','default') == original
    assert query_all(store) == original_rows
    assert counts(store) == {'data_store_datasets':1,'data_store_files':2,'data_store_scopes':1,
                             'data_store_issues':0,'data_store_garbage':0}
    assert sum(p.stat().st_size for p in (store.files.root/'.logs').iterdir()) <= store.limits.log_bytes
    assert len(list((store.files.root/'objects').rglob('*.parquet'))) == 2
    assert not list((store.files.root/'.scratch').rglob('*.parquet'))


def test_same_values_new_confirmation_does_not_rewrite_or_forget_scope(store):
    store.register(spec()); put(store)
    refs = store.catalog.files('bars','default')
    src = source(store,token='new',confirmation={'observed_ns':9007199254740993})
    result = store.upsert(spec(),'default',batches(spec()),src,lower=(0,),upper=(20,),source_check=lambda s:True)
    assert not result.changed and not result.idempotent and result.generation == 2
    assert store.catalog.files('bars','default') == refs
    assert store.source_state('bars','range')['confirmation'] == {'observed_ns':9007199254740993}
    assert store.source_state('bars','range')['revision'] == 2


def test_scope_CAS_and_current_basis_invalidate_old_replay(store):
    store.register(spec()); put(store)
    stale = source(store)
    put(store, start=3,count=1,add=100,token='other',scope='correction')
    # The prior scope token is not a permanent success receipt: basis changed.
    assert_error('SOURCE_CONFLICT', lambda: store.upsert(spec(),'default',batches(spec()),
        replace(stale, expected_revision=0), lower=(0,),upper=(20,),source_check=lambda s:True))
    assert query_all(store)[3]['price'] == '103'


def test_C09_events_decimal_ns_timezone_and_HTTP_transport(store):
    schema = pa.schema([
        pa.field('trade_day',pa.date32(),False),pa.field('channel',pa.string(),False),
        pa.field('event_ns',pa.int64(),False,metadata={b'unit':b'epoch_ns'}),
        pa.field('sequence',pa.uint64(),False),pa.field('price',pa.decimal128(38,8),False),
        pa.field('observed_at',pa.timestamp('us','UTC'),False)])
    contract = DatasetSpec('ticks',schema,('trade_day','channel','event_ns','sequence'),'r1',
                           {'frequency':'tick','session':'exchange','timezone':'UTC'})
    store.register(contract)
    observed = datetime(2026,9,24,23,59,59,999999,tzinfo=timezone(timedelta(hours=9)))
    ns = epoch_ns(observed)+777
    price = Decimal('123456789012345678901234567890.12345678')
    rows = [{'trade_day':date(2026,9,24),'channel':'c1','event_ns':ns,'sequence':1,'price':price,'observed_at':observed},
            {'trade_day':date(2026,9,24),'channel':'c1','event_ns':ns,'sequence':2,'price':price,'observed_at':observed},
            {'trade_day':date(2026,9,24),'channel':'c1','event_ns':ns+1,'sequence':3,'price':price,'observed_at':observed},
            {'trade_day':date(2026,9,25),'channel':'c2','event_ns':ns+1000,'sequence':2**64-1,'price':price,'observed_at':observed}]
    store.upsert(contract,'default',[pa.RecordBatch.from_pylist(rows,schema=schema)],source(store,'ticks'),
                 lower=(date(2026,9,24),),upper=(date(2026,9,26),),source_check=lambda s:True)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            result = store.read(contract,Query()).to_dict()
            body = json.dumps(result).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker = threading.Thread(target=server.serve_forever);worker.start()
    try:
        with urlopen(f'http://127.0.0.1:{server.server_port}/current',timeout=5) as response:
            result=json.load(response)
    finally:
        server.shutdown();worker.join();server.server_close()
    assert len(result['rows']) == 4
    assert all(r['price'] == str(price) for r in result['rows'])
    assert [r['event_ns'] for r in result['rows'][:3]] == [str(ns),str(ns),str(ns+1)]
    assert result['rows'][-1]['sequence'] == str(2**64-1)
    assert result['rows'][0]['observed_at'] == observed.astimezone(timezone.utc).isoformat()


def test_C11_signed_cursor_continuation_then_changed_and_no_history(store):
    store.register(spec());put(store)
    first = store.read(spec(),Query(page_size=3))
    second = store.read(spec(),Query(page_size=3,cursor=first.next_cursor))
    assert [r['id'] for r in second.rows] == [3,4,5]
    assert_error('INVALID_CURSOR',lambda:store.read(spec(),Query(page_size=4,cursor=first.next_cursor)))
    assert_error('INVALID_CURSOR',lambda:store.read(spec(),Query(page_size=3,cursor='x'+first.next_cursor[1:])))
    put(store,start=1,count=1,add=100,token='correction',scope='correction')
    assert_error('DATA_CHANGED',lambda:store.read(spec(),Query(page_size=3,cursor=first.next_cursor)))
    assert_error('HISTORY_UNSUPPORTED',lambda:Query(release='old'))
    assert_error('HISTORY_UNSUPPORTED',lambda:Query(snapshot='1'))
    assert_error('QUERY_BUDGET_EXCEEDED',lambda:store.read(spec(),Query(columns=('price";DROP TABLE x',))))


def test_multidependency_generations_prevent_price_factor_mix(store):
    prices,factors=spec(),spec('factors')
    store.register(prices);store.register(factors);put(store);put(store,factors)
    first=store.read_many([(prices,Query(page_size=3)),(factors,Query(page_size=3))])
    put(store,factors,add=100,token='next')
    assert_error('DATA_CHANGED',lambda:store.read_many(
        [(prices,Query(page_size=3,cursor=first[0].next_cursor)),(factors,Query(page_size=3))]))
    assert_error('DATA_CHANGED',lambda:store.read_many([(prices,Query()),(factors,Query())],
                                                       expected_generations=first[0].generations))


def test_C16_nullable_addition_and_explicit_partition_rebuild(store):
    old=spec();store.register(old);put(store)
    added=replace(old,schema=old.schema.append(pa.field('optional',pa.string(),True)))
    store.register(added)
    assert all(r['optional'] is None for r in query_all(store,added))
    changed=replace(old,rule='r2',semantics={'unit':'USD','representation':'unadjusted'})
    assert_error('REBUILD_REQUIRED',lambda:store.register(changed))
    store.register(changed,prepare_rebuild=True)
    assert_error('REBUILD_REQUIRED',lambda:query_all(store,changed))
    store.replace_partition(changed,'default',batches(changed),source(store,token='one'),
                            complete=True,source_check=lambda s:True)
    assert len(query_all(store,changed))==20
    assert query_all(store,changed)[10]['price']=='10'


@pytest.mark.parametrize('dtype',[pa.decimal256(50,10),pa.decimal128(20,-2),pa.float64(),pa.timestamp('ns','UTC')])
def test_C16_unsupported_exact_types_rejected(dtype):
    assert_error('SCHEMA_UNSUPPORTED',lambda:DatasetSpec('invalid',pa.schema([
        pa.field('id',pa.int64(),False),pa.field('price',dtype)]),('id',),'r1'))


def test_reports_are_one_atomic_unit_empty_is_explicit(store):
    contract=DatasetSpec('holdings',pa.schema([pa.field('report',pa.string(),False),
        pa.field('member',pa.int64(),False),pa.field('weight',pa.decimal128(18,8),False)]),
        ('report','member'),'r1',{'unit':'ratio'},report_prefix=1)
    store.register(contract)
    def members(n):
        for start in range(0,n,10):
            yield pa.RecordBatch.from_pylist([{'report':'r1','member':i,'weight':Decimal('0.01')}
                for i in range(start,min(start+10,n))],schema=contract.schema)
    def publish(n,complete=True,token='one'):
        return store.replace_report(contract,'default',('r1',),members(n),source(store,'holdings',token=token),
                                    complete=complete,source_check=lambda s:True)
    publish(25)
    assert len(query_all(store,contract))==25
    assert_error('REPORT_INCOMPLETE',lambda:publish(2,False,'bad'))
    assert len(query_all(store,contract))==25
    publish(2,token='two')
    assert len(query_all(store,contract))==2
    publish(0,token='empty-complete')
    assert query_all(store,contract)==[]
    assert counts(store)['data_store_files']==0


def test_rows_cannot_escape_declared_range_partition_or_duplicate_keys(store):
    store.register(spec())
    assert_error('KEY_ORDER_INVALID',lambda:store.upsert(spec(),'default',batches(spec(),0,10),
        source(store),lower=(2,),upper=(11,),source_check=lambda s:True))
    assert_error('INVALID_VALUE',lambda:put(store,partition='other'))
    duplicate=pa.RecordBatch.from_pydict({'id':[1,1],'price':[Decimal(1),Decimal(2)]},schema=spec().schema)
    assert_error('KEY_ORDER_INVALID',lambda:store.upsert(spec(),'default',[duplicate],source(store),
        lower=(0,),upper=(3,),source_check=lambda s:True))
    assert counts(store)['data_store_files']==0


def test_changed_shards_only_and_unrelated_partition_files_stable(store):
    contract=spec(partitioning='tens',partitioner=lambda key:'p'+str(key[0]//100))
    store.register(contract)
    for i in range(3):put(store,contract,start=i*100,count=20,partition=f'p{i}',scope=f's{i}',token=f't{i}')
    unrelated=store.catalog.files('bars','p1')+store.catalog.files('bars','p2')
    result=put(store,contract,start=1,count=1,add=100,token='fix',partition='p0',scope='fix')
    assert result.metrics.files_read==1
    assert result.metrics.files_retired==1
    assert unrelated==store.catalog.files('bars','p1')+store.catalog.files('bars','p2')
    assert_error('KEY_ORDER_INVALID',lambda:put(store,contract,start=1,count=1,partition='p1'))


def crash_child(schema,root,limits,point):
    def fault(p):
        if p==point:os._exit(77)
    engine=make_engine(schema)
    with probe(),CurrentStore(engine,root,cursor_key=KEY,limits=StoreLimits(**limits),_fault=fault) as st:
        put(st,start=1,count=1,add=100,token='crash',scope='crash')


@pytest.mark.parametrize('point',['after_write','after_file_fsync','before_promote','after_link',
    'after_directory_fsync','before_catalog_commit','after_catalog_commit','before_delete','after_delete'])
def test_C13_process_crashes_all_durability_boundaries(store,database,point):
    store.register(spec());put(store)
    oldgen=store.catalog.dataset('bars')['generation']
    ctx=multiprocessing.get_context('spawn')
    child=ctx.Process(target=crash_child,args=(database[1],str(store.files.root),asdict(store.limits),point))
    child.start();child.join(20)
    try:
        assert not child.is_alive()
        assert child.exitcode==77
    finally:
        if child.is_alive():child.kill();child.join()
    after=point in ('after_catalog_commit','before_delete','after_delete')
    rows=query_all(store)
    assert rows[1]['price']==('101' if after else '1')
    assert store.catalog.dataset('bars')['generation']==oldgen+int(after)
    state=store.source_state('bars','crash')
    assert (state is not None)==after
    # No referenced file is a half-write, even after a killed move/commit.
    for f in store.catalog.files('bars','default'):
        assert (store.files.root/f['path']).is_file()
        with pq.ParquetFile(store.files.root/f['path']) as p:
            assert p.read().num_rows==f['row_count']
    time.sleep(1.05)
    store.recover('bars');store.recover('bars')
    assert counts(store)['data_store_garbage']==0
    assert len(list((store.files.root/'objects').rglob('*.parquet')))==len(store.catalog.files('bars','default'))


def test_C13_commit_acknowledgement_loss_confirmed_not_replayed(store):
    store.register(spec());put(store)
    def fault(p):
        if p=='after_catalog_commit':raise OperationalError('commit',None,ConnectionError('synthetic lost ack'))
    store.fault=fault
    result=put(store,add=100,token='two')
    assert result.generation==2 and query_all(store)[1]['price']=='101'
    assert counts(store)['data_store_garbage']==0


def test_C13_unreachable_confirmation_is_unknown_never_delete_new_current(store,monkeypatch):
    store.register(spec());put(store)
    original=store.catalog.dataset
    def unavailable(*args,**kwargs):raise OperationalError('probe',None,ConnectionError('synthetic'))
    def fault(p):
        if p=='after_catalog_commit':
            monkeypatch.setattr(store.catalog,'dataset',unavailable)
            raise OperationalError('commit',None,ConnectionError('synthetic'))
    store.fault=fault
    assert_error('COMMIT_UNKNOWN',lambda:put(store,add=100,token='two'))
    monkeypatch.setattr(store.catalog,'dataset',original);store.fault=lambda _:None
    assert query_all(store)[1]['price']=='101'
    store.recover('bars')
    assert counts(store)['data_store_files']==2


def test_C12_active_reader_and_writer_protect_files_not_unrelated_dataset(store,database):
    store.register(spec());store.register(spec('other'));put(store);put(store,spec('other'))
    original=store.catalog.files('bars','default')
    with store.locks.read('bars'):
        assert_error('LOCK_TIMEOUT',lambda:put(store,add=100,token='new'))
        # Prepared new files are not current; the original reader remains valid.
        assert all((store.files.root/r['path']).exists() for r in original)
        assert len(query_all(store))==20
        assert len(query_all(store,spec('other')))==20
    time.sleep(1.05);store.recover('bars')
    assert counts(store)['data_store_garbage']==0


def test_C12_two_writers_one_dataset_and_independent_reader(store,database):
    store.register(spec());put(store)
    with store.locks.writer('bars'):
        assert_error('LOCK_TIMEOUT',lambda:put(store,add=100,token='new'))
        assert query_all(store)[0]['price']=='0'
        with store.locks.writer('unrelated',timeout_ms=0) as writer:
            with writer.commit(timeout_ms=0):pass


def test_C15_shared_scratch_reservations_and_actual_file_growth(store):
    with store.budget.reserve('write') as space:
        assert_error('LOCK_TIMEOUT',lambda:_reserve_write(store))
        file=space.new_file()
        with QuotaFile(file,space) as f:
            assert_error('SCRATCH_BUDGET_EXCEEDED',lambda:f.write(b'x'*(space.stage_limit+1)))
        assert space.usage()[0]==0
    assert not list((store.files.root/'.scratch').rglob('*.parquet'))


def _reserve_write(store):
    with store.budget.reserve('write'):pass


def test_C15_real_memory_disk_timeout_output_and_cancellation(store,monkeypatch):
    store.register(spec());put(store)
    original=store.limits
    for changed,expected in [({'minimum_free_bytes':2**62},'DISK_PRESSURE'),
                             ({'process_memory_bytes':1},'MEMORY_PRESSURE'),
                             ({'query_timeout_ms':1},'QUERY_TIMEOUT'),
                             ({'query_bytes':1},'QUERY_BUDGET_EXCEEDED')]:
        limited=replace(original,**changed)
        monkeypatch.setattr(store,'limits',limited);monkeypatch.setattr(store.budget,'limits',limited)
        assert_error(expected,lambda:store.read(spec(),Query(page_size=2)))
    monkeypatch.setattr(store,'limits',original);monkeypatch.setattr(store.budget,'limits',original)
    assert_error('OPERATION_CANCELLED',lambda:store.read_many([(spec(),Query())],cancelled=lambda:True))
    assert store.catalog.dataset('bars')['generation']==1


def test_C15_limits_reject_batch_before_directory_or_checkpoint(store,monkeypatch):
    store.register(spec())
    small=replace(store.limits,commit_rows=1)
    monkeypatch.setattr(store,'limits',small);monkeypatch.setattr(store.budget,'limits',small)
    assert_error('BATCH_BUDGET_EXCEEDED',lambda:put(store))
    assert counts(store)['data_store_files']==0
    assert store.source_state('bars','range') is None
    assert store.catalog.dataset('bars')['generation']==0


def test_C13_C15_delete_failures_retry_bounded_queue_and_backpressure(store,monkeypatch):
    store.register(spec());put(store)
    original=store.files.delete_object
    def fail(*_):raise OSError('synthetic delete denial')
    monkeypatch.setattr(store.files,'delete_object',fail)
    result=put(store,add=100,token='two')
    assert result.cleanup_pending
    limited=replace(store.limits,garbage_count=2)
    monkeypatch.setattr(store,'limits',limited);monkeypatch.setattr(store.catalog,'limits',limited)
    assert_error('GARBAGE_BUDGET_EXCEEDED',lambda:put(store,add=200,token='three'))
    assert store.catalog.dataset('bars')['generation']==2
    monkeypatch.setattr(store.files,'delete_object',original)
    time.sleep(1.1);store.cleanup('bars')
    assert counts(store)['data_store_garbage']==0
    assert query_all(store)[1]['price']=='101'


def test_issues_aggregate_retry_without_success_ledger_or_accidental_resolution(store):
    store.register(spec());put(store)
    issue=Issue('report.field','bad_range','INVALID_VALUE',fingerprint('evidence'),
                {'report':'r','member':'a','field':'price'},{'requires':'correct_same_target'})
    def report():
        return store.record_problems(spec(),'default',source(store,scope='bad_range',token='bad',qualified=False),
                    (issue,),lower=(0,),upper=(20,),source_check=lambda s:True)
    for _ in range(5):report()
    current=store.catalog.issues('bars')
    assert len(current)==1 and current[0]['attempts']==5
    assert not store.source_state('bars','bad_range')['qualified']
    assert store.describe_capability(spec())['status']=='restricted'
    store.upsert(spec(),'default',[],source(store,token='unrelated'),lower=(0,),upper=(20,),
                 source_check=lambda s:True,resolved={'report.field':fingerprint('wrong-target')})
    assert len(store.catalog.issues('bars'))==1
    store.upsert(spec(),'default',batches(spec()),source(store,token='fixed'),lower=(0,),upper=(20,),
                 source_check=lambda s:True,resolved={'report.field':issue.evidence_token})
    assert store.catalog.issues('bars')==[]


def test_task_retention_does_not_touch_other_types_or_running(database,store):
    # Minimal shared-table fixture exercises the actual retention SQL in PostgreSQL.
    with store.catalog.transaction() as c:
        c.execute(text('CREATE TABLE task_runs(id uuid PRIMARY KEY, task_id uuid, task_type text, status text, finished_at timestamptz)'))
        task=uuid4()
        rows=[{'id':uuid4(),'task':task} for _ in range(1005)]
        c.execute(text("INSERT INTO task_runs VALUES (:id,:task,'data_store.update','succeeded',clock_timestamp())"),rows)
        c.execute(text("INSERT INTO task_runs VALUES (:id,:task,'other.task','succeeded',clock_timestamp()-interval '60 days')"),
                  {'id':uuid4(),'task':task})
        c.execute(text("INSERT INTO task_runs VALUES (:id,:task,'data_store.update','running',NULL)"),{'id':uuid4(),'task':task})
        c.execute(text("INSERT INTO task_runs VALUES (:id,:task,'data_store.retry','failed',clock_timestamp()-interval '60 days')"),
                  {'id':uuid4(),'task':uuid4()})
    assert prune_completed_runs(store.catalog)==6
    with store.catalog.transaction() as c:
        assert c.execute(text('SELECT count(*) FROM task_runs')).scalar_one()==1002
        assert c.execute(text("SELECT count(*) FROM task_runs WHERE status='running'")).scalar_one()==1
        assert c.execute(text("SELECT count(*) FROM task_runs WHERE task_type='other.task'")).scalar_one()==1


def test_current_metadata_cannot_bind_another_root_or_different_policy(store,database,tmp_path):
    another=tmp_path/'another';another.mkdir()
    assert_error('CATALOG_MISMATCH',lambda:CurrentStore(database[0],another,initialize=True,
        cursor_key=KEY,limits=store.limits))
    assert_error('CATALOG_MISMATCH',lambda:CurrentStore(database[0],store.files.root,
        cursor_key=KEY,limits=replace(store.limits,parallel_writers=2)))


def test_current_query_never_reads_unlisted_garbage_or_arbitrary_paths(store):
    store.register(spec());put(store)
    rogue=store.files.root/'objects'/'rogue.parquet'
    pq.write_table(pa.table({'id':[999],'price':[999]}),rogue)
    assert len(query_all(store))==20
    assert_error('INVALID_VALUE',lambda:Query(partitions=('../objects',)))
    assert rogue.exists()


def test_logs_both_error_and_success_share_byte_and_age_budget(store):
    old=store.files.root/'.logs'/f'{time.time_ns()-8*86400*10**9:020}-{uuid4().hex}.jsonl'
    old.write_text('{"old":true}\n')
    for _ in range(100):
        write_summary(store,'bars','INVALID_VALUE',Metrics())
        write_summary(store,'bars','committed',Metrics())
    assert not old.exists()
    assert sum(p.stat().st_size for p in (store.files.root/'.logs').iterdir())<=store.limits.log_bytes
    assert all(len(line)<2048 for p in (store.files.root/'.logs').iterdir() for line in p.read_bytes().splitlines())


def test_confirmation_and_issue_payloads_captured_before_lazy_input(store):
    contract = spec()
    store.register(contract)
    cp = {'nested': {'offset': 1}}
    co = {'source': ['original']}
    update = source(store, confirmation=co, checkpoint=cp)

    def mutate():
        cp['nested']['offset'] = 99
        co['source'].append('changed-during-read')
        yield from batches(contract)

    store.upsert(contract, 'default', mutate(), update, lower=(0,), upper=(20,), source_check=lambda _: True)
    state = store.source_state('bars', 'range')
    assert state['checkpoint'] == {'nested': {'offset': 1}}
    assert state['confirmation'] == {'source': ['original']}
    target = {'member': ['exact']}
    issue = Issue('bad', 'range', 'invalid', fingerprint('evidence'), target, {})
    target['member'].append('unrelated')
    store.record_problems(contract, 'default', source(store, token='bad', qualified=False), (issue,),
                          lower=(0,), upper=(20,), source_check=lambda _: True)
    assert json.loads(store.catalog.issues('bars')[0]['target_json']) == {'member': ['exact']}


def test_same_rule_unit_change_requires_explicit_rebuild(store):
    contract = spec()
    store.register(contract)
    store.upsert(contract, 'default', batches(contract), source(store),
                 lower=(0,), upper=(20,), source_check=lambda _: True)
    changed = replace(contract, semantics={'unit': 'USD', 'representation': 'unadjusted'})
    with pytest.raises(DataStoreError) as result:
        store.register(changed)
    assert result.value.code == 'REBUILD_REQUIRED'
    store.register(changed, prepare_rebuild=True)
    assert store.describe_capability(changed)['status'] == 'rebuild_required'
    with pytest.raises(DataStoreError) as result:
        store.read(changed, Query(('default',)))
    assert result.value.code == 'REBUILD_REQUIRED'


def test_actual_duckdb_interrupt_and_native_memory_cap(database, tmp_path, limits):
    for mode in ('timeout', 'native_memory'):
        root = tmp_path / mode
        root.mkdir(mode=0o700)
        tight = replace(limits, query_timeout_ms=20 if mode == 'timeout' else 5000,
                        duckdb_memory_bytes=16*MiB, query_bytes=4*MiB)
        with probe(), CurrentStore(database[0] if mode == 'timeout' else make_engine(database[1]),
                                    root, cursor_key=KEY, initialize=True, limits=tight) as st:
            with pytest.raises(DataStoreError) as result:
                with st.budget.reserve('read') as space:
                    with space.connection() as con:
                        if mode == 'timeout':
                            con.execute('SELECT sum(hash(i)) FROM range(500000000) t(i)').fetchone()
                        else:
                            con.execute('SELECT list(i) FROM range(10000000) t(i)').fetchone()
            assert result.value.code == ('QUERY_TIMEOUT' if mode == 'timeout' else 'MEMORY_PRESSURE')
        if mode == 'timeout':
            # This fixture owns the isolated schema. Remove only the empty root
            # binding between two disjoint native-resource test configurations.
            with database[0].begin() as c:
                c.execute(text('DELETE FROM data_store_runtime'))


def test_catalog_metadata_matches_frozen_ddl(database):
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.autogenerate import compare_metadata
    from alembic.operations import Operations
    from sqlalchemy import MetaData
    from app.data_store.tables import metadata

    # The fixture deliberately creates D01's frozen six-table schema. Check it
    # without rewriting the historical DDL to include D02's independent table.
    frozen_metadata = MetaData()
    for table in metadata.sorted_tables:
        if table.name != 'data_store_entry_status':
            table.to_metadata(frozen_metadata)
    assert len(frozen_metadata.tables) == 6
    assert len(metadata.tables) == 7
    migration = import_module('app.db.migrations.versions.20261006_01_local_entry_status')
    assert migration.down_revision == '20261005_01'
    with database[0].begin() as c:
        # Both checks use the fixture's private schema, never shared/public.
        options = {'compare_type': True, 'compare_server_default': True}
        context = MigrationContext.configure(c, opts=options)
        assert compare_metadata(context, frozen_metadata) == []
        # Run the actual additive migration rather than metadata.create_all(),
        # which would hide a mismatch between deployed DDL and current models.
        with Operations.context(context):
            migration.upgrade()
        context = MigrationContext.configure(c, opts=options)
        assert compare_metadata(context, metadata) == []


def test_idle_cleanup_applies_log_age_without_new_summary(store):
    from app.data_store.maintenance import prune_summary_logs
    logdir = store.files.root / '.logs'
    expired = logdir / ('00000000000000000001-' + 'f'*32 + '.jsonl')
    expired.write_text('old summary\n')
    assert prune_summary_logs(store) == {'retained_segments':0,'retained_bytes':0}
    assert not expired.exists()


def test_scratch_cleanup_failure_never_relabels_committed_data(store, monkeypatch):
    contract = spec()
    store.register(contract)
    clean = store.budget._clean_slot
    def fault(point):
        if point == 'after_catalog_commit':
            def unavailable(*args):
                raise OSError('synthetic scratch cleanup failure')
            monkeypatch.setattr(store.budget, '_clean_slot', unavailable)
    store.fault = fault
    result = store.upsert(contract,'default',batches(contract),source(store),
                          lower=(0,),upper=(20,),source_check=lambda _:True)
    assert result.changed and result.cleanup_pending and result.generation == 1
    assert store.source_state('bars','range')['qualified']
    monkeypatch.setattr(store.budget, '_clean_slot', clean)
    store.fault = lambda _:None
    assert store.recover('bars')['row_count'] == 20
    assert len(store.read(contract, Query()).rows) == 20


def test_catalog_and_native_paths_are_not_exposed(store, monkeypatch):
    def bad(*_, **__):
        raise OperationalError('SELECT private_original',{'token':'secret'},Exception('private path'))
    monkeypatch.setattr(store.catalog, 'dataset', bad)
    with pytest.raises(DataStoreError) as error:
        store.describe_capability(spec())
    assert error.value.code == 'CATALOG_UNAVAILABLE'
    assert 'secret' not in str(error.value) and 'private' not in str(error.value)


def test_cancel_at_last_commit_boundary_keeps_current_and_checkpoint(store):
    contract = spec()
    store.register(contract)
    put(store)
    previous = store.source_state('bars','range')
    cancelled = threading.Event()
    store.fault = lambda point: cancelled.set() if point == 'before_catalog_commit' else None
    with pytest.raises(DataStoreError) as error:
        store.upsert(contract,'default',batches(contract,add=100),source(store,token='new'),
                     lower=(0,),upper=(20,),source_check=lambda _:True,cancelled=cancelled.is_set)
    assert error.value.code == 'OPERATION_CANCELLED'
    assert store.catalog.dataset('bars')['generation'] == 1
    assert store.source_state('bars','range')['revision'] == previous['revision']
    store.fault = lambda _:None
    assert query_all(store)[0]['price'] == '0'


def test_problem_commit_exceeding_budget_rolls_back_all_state(store,monkeypatch):
    contract=spec(); store.register(contract)
    monkeypatch.setattr(store,'limits',replace(store.limits,write_timeout_ms=10))
    def delay(point):
        if point == 'before_catalog_commit': time.sleep(.02)
    store.fault=delay
    issue=Issue('bad','range','INVALID_VALUE',fingerprint('bad'),{'member':'exact'},{})
    with pytest.raises(DataStoreError) as error:
        store.record_problems(contract,'default',source(store,token='bad',qualified=False),(issue,),
                              lower=(0,),upper=(20,),source_check=lambda _:True)
    assert error.value.code == 'QUERY_TIMEOUT'
    assert store.source_state('bars','range') is None
    assert store.catalog.issues('bars') == []
    assert store.catalog.dataset('bars')['generation'] == 0


def test_wide_column_small_response_budget_fails_without_large_arrow_batch(store):
    contract = DatasetSpec('wide', pa.schema([pa.field('id',pa.int64(),False),
                          pa.field('body',pa.string(),False)]),('id',),'r1')
    store.register(contract)
    batch = pa.RecordBatch.from_pydict({'id':list(range(10)),'body':['x'*65536]*10},schema=contract.schema)
    store.upsert(contract,'default',[batch],source(store,'wide'),lower=(0,),upper=(10,),source_check=lambda _:True)
    old = store.limits
    store.limits = replace(old,query_bytes=1024)
    try:
        with pytest.raises(DataStoreError) as error:
            store.read(contract, Query(page_size=1000))
        assert error.value.code == 'QUERY_BUDGET_EXCEEDED'
        assert store.catalog.dataset('wide')['generation'] == 1
    finally:
        store.limits = old


def test_new_migration_only_downgrades_completely_unused_tables(database):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    path=Path(__file__).parents[1]/'app/db/migrations/versions/20261005_01_current_data_store.py'
    loader=importlib.util.spec_from_file_location('lfd01_additive_migration',path)
    migration=importlib.util.module_from_spec(loader); loader.loader.exec_module(migration)
    engine=database[0]
    with engine.begin() as c:
        migration.op=Operations(MigrationContext.configure(c))
        migration.downgrade()
        assert not any(t.startswith('data_store_') for t in inspect(c).get_table_names())
        migration.upgrade()
    with engine.begin() as c:
        c.execute(text("INSERT INTO data_store_runtime VALUES (1,:id,'{}')"),{'id':uuid4()})
    with engine.begin() as c:
        migration.op=Operations(MigrationContext.configure(c))
        with pytest.raises(RuntimeError,match='contains persisted evidence'):
            migration.downgrade()
        assert len([t for t in inspect(c).get_table_names() if t.startswith('data_store_')])==6


def test_unresolved_problem_never_exposes_old_value_as_qualified_current(store):
    contract=spec(); store.register(contract); put(store)
    issue=Issue('bad','range','INVALID_VALUE',fingerprint('bad'),{'member':'exact'},{})
    store.record_problems(contract,'default',source(store,token='bad',qualified=False),(issue,),
                          lower=(0,),upper=(20,),source_check=lambda _:True)
    assert_error('DATA_RESTRICTED',lambda:store.read(contract,Query()))
    page=store.read(contract,Query(require_qualified=False))
    assert page.to_dict()['quality_status']=='restricted'
    assert page.to_dict()['unresolved_issues']==1
    assert page.rows[0]['price']=='0'
