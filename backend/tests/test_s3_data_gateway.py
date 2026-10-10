"""Isolated real PostgreSQL/CurrentStore/Parquet/DuckDB/Arrow/Rust D04 tests."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import gc
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pyarrow as pa
import pytest
from sqlalchemy import text
from app.core.auth import AuthenticatedPrincipal
from app.backtest_service.data_gateway import (AuthorizedRun, GatewayError, MARKET_SCHEMA,
    MarketBinding, RunDataGateway, serve)
from app.backtest_service.market_projection import current_market_bindings, synthetic_market_binding
from app.data_store.adapters.registry import SYNTHETIC
from app.data_store.catalog import SourceUpdate
from app.data_store.schema import DatasetSpec, fingerprint
from app.data_store.limits import StoreLimits, MiB
from app.data_store.tables import entry_status
from app.legacy_reset.tables import maintenance, restrictions
from tests.test_data_store_kernel import database, store
from tests.test_data_store_local_pipeline import Inputs

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')
ROOT = Path(__file__).resolve().parents[2]
BASE = 1767225600000000123
PRICE = '123456789.123456789012345678'
# Tests load the sole maintained transport directly; no production fallback.
loader = importlib.util.spec_from_file_location('d04_test_transport', ROOT/'sdk/python/quantfoundry/_transport.py')
transport = importlib.util.module_from_spec(loader)
sys.modules[loader.name] = transport
loader.loader.exec_module(transport)

@pytest.fixture
def limits():
    return replace(StoreLimits(),file_rows=10,lock_timeout_ms=1000,orphan_grace_seconds=1,
                   duckdb_memory_bytes=64*MiB,pipeline_spill_bytes=64*MiB,log_bytes=4096)

@pytest.fixture
def formal(store):
    with store.catalog.engine.begin() as c:
        for table in (entry_status, maintenance, restrictions):
            table.create(c)
        c.execute(maintenance.insert().values(singleton=1, phase='ready', completed_json={}, files_started=False))
    return store

def contract(name='gateway_market'):
    fields = [pa.field(f.name, f.type, nullable=f.name not in ('security','time_ns','sequence'),
        metadata={b'max_utf8_bytes':b'128'} if pa.types.is_string(f.type) else
                 {b'unit':b'epoch_ns'} if f.name.endswith('_ns') else None) for f in MARKET_SCHEMA]
    return DatasetSpec(name, pa.schema(fields, metadata=MARKET_SCHEMA.metadata),
        ('security','time_ns','sequence'), 'synthetic-d04-v1',
        {'price_basis':'raw','price_currency':'CNY','quantity_unit':'shares'})

def binding(spec, name='market'):
    return MarketBinding(name,spec,'tick',{n:n for n in spec.schema.names},{},
        lambda s,a,b: ((s,a),(s,b+1)), 'time_ns')

def rows(count=10, price=PRICE, kind='trade_tick'):
    result=[]
    for i in range(count):
        row={n:None for n in MARKET_SCHEMA.names}
        row.update(kind=kind,security='A.SH',session='2026-01-01:auction',source_session='2026-01-01:auction',
            channel='A',sequence=i+1,stable_input_sequence=i+1,time_ns=BASE+i//2,
            price=price if kind=='trade_tick' else None,quantity=7 if kind=='trade_tick' else None)
        if kind=='quote_tick':
            row.update(bid='10.01',ask='10.02',bid_quantity=3,ask_quantity=5)
        result.append(row)
    return result

def put(formal,spec,data,token='one'):
    current=formal.source_state(spec.name,'test')
    source=SourceUpdate('test',fingerprint(token),fingerprint('isolated-d04'),current['revision'] if current else 0)
    return formal.upsert(spec,'default',[pa.RecordBatch.from_pylist(data,schema=spec.schema)],
        source,lower=('A.SH',),upper=('A.SH\0',),source_check=lambda _:True)

def make_gateway(formal,bindings,**kwargs):
    grant=AuthorizedRun('isolated-run','isolated-owner',('A.SH',),BASE-100,BASE+1000)
    return RunDataGateway(formal,AuthenticatedPrincipal('isolated-owner'),grant,bindings,batch_rows=2,**kwargs)

def setup(formal,count=10,kind='trade_tick'):
    spec=contract()
    formal.register(spec)
    if count:
        put(formal,spec,rows(count,kind=kind))
    gateway=make_gateway(formal,(binding(spec),))
    ctx=gateway.open(dict(run_id='isolated-run',universe=['A.SH']))
    return spec,gateway,ctx

def request(start=BASE,end=BASE+100,count=None,fields=('price','quantity'),securities=('A.SH',),frequency='tick'):
    return dict(securities=list(securities),fields=list(fields),frequency=frequency,
        start_ns=str(start) if count is None else None,end_ns=str(end),count_per_security=count,adjustment='none')

def error(code,call,store_code=None):
    with pytest.raises(GatewayError) as e:
        call()
    assert e.value.code==code
    if store_code:
        assert e.value.scope['store_code']==store_code
    return e.value

def quality(formal,spec):
    with formal.catalog.engine.begin() as c:
        c.execute(text("INSERT INTO data_store_issues(dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json) "
            "VALUES(:d,'quality','test','QUALITY_CHANGED',:t,'{}','{}')"),{'d':spec.name,'t':fingerprint('changed')})

def test_real_arrow_exact_selective_projection(formal,monkeypatch):
    _,gateway,ctx=setup(formal,count=40)
    from app.data_store import readers
    monkeypatch.setattr(readers,'api_value',lambda *_:pytest.fail('Arrow cannot become JSON rows'))
    batches=list(gateway.read('market',request(BASE+8,BASE+9,fields=('price',)),ctx))
    table=pa.concat_tables([b.table for b in batches])
    assert table.num_rows==4 and gateway.stats['storage_rows']==4
    assert table['price'].to_pylist()==[PRICE]*4 and table['quantity'].null_count==4
    assert table['time_ns'].to_pylist()==[BASE+8,BASE+8,BASE+9,BASE+9]
    assert all(b.table.num_rows<=2 for b in batches)
    assert not list((formal.files.root/'.scratch').rglob('*.parquet'))

@pytest.mark.parametrize('quality_only',[False,True])
def test_two_blocks_changed_never_commit_mixed_success(formal,quality_only):
    spec,gateway,ctx=setup(formal)
    stream=gateway.read('market',request(),ctx)
    assert next(stream).table.num_rows==2
    generation=formal.catalog.dataset(spec.name)['generation']
    if quality_only:
        quality(formal,spec)
        assert formal.catalog.dataset(spec.name)['generation']==generation
    else:
        put(formal,spec,rows(price='2'),token='changed')
    error('DATA_CHANGED',lambda:next(stream))
    error('DATA_CHANGED',lambda:gateway.finalize(ctx,lambda c:pytest.fail('cannot commit mixed success')))

def test_quality_change_during_real_read_before_return(formal,monkeypatch):
    spec,gateway,ctx=setup(formal)
    original=formal.read_arrow
    def changed(*a,**kw):
        result=original(*a,**kw)
        quality(formal,spec)
        return result
    monkeypatch.setattr(formal,'read_arrow',changed)
    error('DATA_CHANGED',lambda:next(gateway.read('market',request(),ctx)))

@pytest.mark.parametrize('new_dependency_changed',[False,True])
def test_dynamic_dependency_checks_old_and_new(formal,new_dependency_changed):
    spec,gateway,ctx=setup(formal)
    other=contract('gateway_other')
    formal.register(other)
    gateway.bindings['other']=binding(other,'other')
    next(gateway.read('market',request(),ctx))
    if new_dependency_changed:
        gateway.declare('other',ctx)
        quality(formal,other)
        error('DATA_CHANGED',lambda:gateway.check(ctx))
    else:
        quality(formal,spec)
        error('DATA_CHANGED',lambda:gateway.declare('other',ctx))
        assert 'other' not in ctx.dependencies

def test_store_503_missing_empty_restricted_and_limits_are_distinct(formal):
    spec,gateway,ctx=setup(formal,count=0)
    batches=list(gateway.read('market',request(),ctx))
    assert len(batches)==1 and batches[0].metadata['rows']==0
    assert pa.ipc.open_stream(batches[0].ipc(gateway.batch_bytes)).read_all().num_rows==0
    with formal.catalog.engine.begin() as c:
        c.execute(text("UPDATE data_store_legacy_maintenance SET phase='rebuilding'"))
    e=error('CAPABILITY_UNAVAILABLE',lambda:gateway.check(ctx),'DATA_STORE_REBUILDING')
    assert e.scope['http_status']=='503'
    with formal.catalog.engine.begin() as c:
        c.execute(text("UPDATE data_store_legacy_maintenance SET phase='ready'"))
    missing=contract('gateway_missing')
    gateway.bindings['missing']=binding(missing,'missing')
    error('CAPABILITY_UNAVAILABLE',lambda:gateway.declare('missing',ctx),'DATASET_MISSING')
    error('RESOURCE_LIMIT',lambda:list(gateway.read('market',request(count=10001),ctx)))
    quality(formal,spec)
    new=make_gateway(formal,(binding(spec),))
    newctx=new.open(dict(run_id='isolated-run',universe=['A.SH']))
    error('DATA_RESTRICTED',lambda:list(new.read('market',request(),newctx)),'DATA_RESTRICTED')

def test_permissions_untrusted_controls_and_provider_basis(formal):
    spec,gateway,ctx=setup(formal)
    error('DATA_RESTRICTED',lambda:RunDataGateway(formal,AuthenticatedPrincipal('wrong'),gateway.grant,(binding(spec),)))
    for req in (request(securities=('B.SH',)),{**request(),'sql':'select * from native'},request(end=BASE+1001)):
        error('DATA_RESTRICTED',lambda:list(gateway.read('market',req,ctx)))
    error('CAPABILITY_UNAVAILABLE',lambda:list(gateway.read('market',request(frequency='1m'),ctx)))
    protected=make_gateway(formal,current_market_bindings())
    c=protected.open(dict(run_id='isolated-run',universe=['A.SH']))
    for name in ('E50','E51','E52','E70'):
        error('CAPABILITY_UNAVAILABLE',lambda:protected.declare(name,c))

@pytest.mark.parametrize('count_rows',[2,40])
def test_lookback_cache_repeated_and_incremental_quality_checks(formal,count_rows):
    spec,gateway,ctx=setup(formal,count=count_rows)
    first=gateway.lookback('market',request(end=BASE+8,count=4),ctx)
    calls=gateway.stats['storage_reads']; loaded=gateway.stats['storage_rows']
    repeat=gateway.lookback('market',request(end=BASE+8,count=4),ctx)
    assert repeat.table.equals(first.table) and gateway.stats['storage_reads']==calls
    updated=gateway.lookback('market',request(end=BASE+9,count=4),ctx)
    assert gateway.stats['storage_rows']-loaded==(2 if count_rows==40 else 0)
    assert updated.table.num_rows<=4 and gateway.stats['cache_rows']<=4
    quality(formal,spec)
    error('DATA_CHANGED',lambda:gateway.lookback('market',request(end=BASE+9,count=4),ctx))

def test_slow_read_cancel_and_arrow_owner_release_short_locks(formal):
    spec,gateway,ctx=setup(formal)
    cancelled=threading.Event()
    gateway.cancelled=cancelled.is_set
    stream=gateway.read('market',request(),ctx)
    before=pa.total_allocated_bytes()
    batch=next(stream)
    with formal.locks.writer(spec.name,timeout_ms=100) as guard:
        with guard.commit(timeout_ms=100):
            assert batch.table.num_rows==2
    cancelled.set()
    error('CANCELLED',lambda:next(stream))
    del batch
    stream.close(); gateway.close(ctx); gc.collect()
    assert ctx.closed and not gateway.cache and not ctx.dependencies
    assert pa.total_allocated_bytes()<=before+4096
    assert not list((formal.files.root/'.scratch').rglob('*.parquet'))

def test_final_transaction_coordinates_quality_write_and_result_commit(formal):
    spec,gateway,ctx=setup(formal)
    next(gateway.read('market',request(),ctx))
    attempted,changed=threading.Event(),threading.Event()
    def mutate():
        attempted.set(); quality(formal,spec); changed.set()
    worker=None
    def commit(c):
        nonlocal worker
        worker=threading.Thread(target=mutate); worker.start()
        assert attempted.wait(1) and not changed.wait(.1)
        c.execute(text('CREATE TEMP TABLE result_commit_probe(ok boolean)'))
        return 'committed'
    assert gateway.finalize(ctx,commit)=='committed'
    worker.join(2)
    assert changed.is_set()
    error('DATA_CHANGED',lambda:gateway.check(ctx))

def rust_probe(gateway,ctx,tmp_path,req,credit=2,before_next=None):
    executable=ROOT/'engine/target/debug/examples/consume_gateway'
    if not executable.exists():
        if os.getenv('QF_S3_INTEGRATION_REQUIRED')=='1':
            pytest.fail('required real Rust consumer missing')
        pytest.skip('build consume_gateway for real Rust integration')
    endpoint=tmp_path/'run.sock'
    listener=socket.socket(socket.AF_UNIX); listener.bind(str(endpoint)); listener.listen(1)
    failures=[]
    def serve_one():
        sock,_=listener.accept(); channel=transport.Channel(sock,timeout=3)
        if before_next:
            original=channel.receive
            pulls=0
            def receive():
                nonlocal pulls
                frame=original()
                if frame.op=='next':
                    pulls+=1
                    if pulls==2:
                        before_next()
                return frame
            channel.receive=receive
        try:
            serve(channel,gateway,ctx)
        except transport.TransportError:
            pass
        except BaseException as e:
            failures.append(e)
    thread=threading.Thread(target=serve_one,daemon=True); thread.start()
    try:
        # No DSN, supplier/token or source directory in child environment.
        result=subprocess.run([str(executable),str(endpoint),gateway.grant.run_id,'A.SH',str(credit),
            req['start_ns'],req['end_ns'],req['frequency'],','.join(req['fields'])],
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},text=True,capture_output=True,timeout=10)
        thread.join(4)
        assert not thread.is_alive() and not failures, failures
        return result,json.loads(result.stdout)
    finally:
        listener.close(); endpoint.unlink(missing_ok=True)

@pytest.mark.parametrize('credit',[1,2,4])
def test_currentstore_arrow_rust_d03_merge_real_chain(formal,tmp_path,credit):
    _,gateway,ctx=setup(formal,count=6)
    gateway.batch_rows=credit
    result,output=rust_probe(gateway,ctx,tmp_path,request(),credit)
    assert result.returncode==0,output
    assert output['count']==6 and output['peak_buffered_events']<=credit
    assert [e['event']['identity']['sequence'] for e in output['samples']]==list(map(str,range(1,7)))
    assert all(e['event']['price']==PRICE for e in output['samples'])
    assert output['samples'][0]['event']['time_ns']==str(BASE)
    assert gateway.stats['storage_rows']==6 and ctx.closed

def test_quote_double_sided_quantities_to_rust(formal,tmp_path):
    _,gateway,ctx=setup(formal,count=4,kind='quote_tick')
    result,output=rust_probe(gateway,ctx,tmp_path,request(fields=('bid','ask','bid_quantity','ask_quantity')))
    assert result.returncode==0,output
    assert output['samples'][0]['event']['bid_quantity']==3 and output['samples'][0]['event']['ask_quantity']==5

def test_existing_registry_tick_projection_to_rust_no_read_normalizer(formal,tmp_path,monkeypatch):
    from app.data_store.adapters.contracts import LocalInput,digest
    from app.data_store.pipeline import run_entry
    from app.data_store.adapters import normalize
    entry=SYNTHETIC[0]; inputs=[]
    for sequence in (1,2,3):
        body=dict(source_code='A.SH',trading_date='2026-01-01',session='auction',channel='A',sequence=sequence,
            event_ns=BASE,price=Decimal('10.123456789012345678'),quantity=7,currency='CNY',price_basis='raw')
        inputs.append(LocalInput('synthetic','tick','A.SH','default',datetime(2026,1,1,tzinfo=timezone.utc),body,digest(body)))
    assert run_entry(formal,entry,Inputs(*inputs))['qualified']
    projection=replace(synthetic_market_binding(entry,inputs[0].representation_key),name='market')
    gateway=make_gateway(formal,(projection,)); ctx=gateway.open(dict(run_id='isolated-run',universe=['A.SH']))
    monkeypatch.setattr(normalize,'normalize',lambda *_:pytest.fail('read cannot normalize'))
    empty=list(gateway.read('market',request(start=BASE+1),ctx))
    assert len(empty)==1 and empty[0].table.num_rows==0
    result,output=rust_probe(gateway,ctx,tmp_path,request())
    assert result.returncode==0 and output['count']==3,output
    assert output['samples'][0]['event']['price']=='10.123456789012345678'

@pytest.mark.parametrize('quality_only',[False,True])
def test_rust_no_success_after_change_between_blocks(formal,tmp_path,quality_only):
    spec,gateway,ctx=setup(formal,count=6)
    def change():
        quality(formal,spec) if quality_only else put(formal,spec,rows(6,price='2'),token='new')
    result,output=rust_probe(gateway,ctx,tmp_path,request(),before_next=change)
    assert result.returncode==1 and output['code']=='DATA_CHANGED'
    assert ctx.closed

@pytest.mark.parametrize('frequency',['1m','60m'])
def test_existing_registry_bar_endpoints_and_exact_arrow_to_rust(formal,tmp_path,frequency):
    from app.data_store.adapters.contracts import LocalInput,digest
    from app.data_store.pipeline import run_entry
    entry=SYNTHETIC[1]
    duration=int(frequency[:-1])*60*10**9
    body=dict(source_code='A.SH',trading_date='2026-01-01',session='continuous',frequency=frequency,
        start_ns=BASE,open=Decimal('10.01'),high=Decimal('10.02'),low=Decimal('10.00'),
        close=Decimal('10.01'),volume=20,currency='CNY',price_basis='raw')
    source=LocalInput('synthetic',entry.native,'A.SH','default',datetime(2026,1,1,tzinfo=timezone.utc),body,digest(body))
    other_body={**body,'frequency':'60m' if frequency=='1m' else '1m'}
    other_source=replace(source,content=other_body,token=digest(other_body))
    assert run_entry(formal,entry,Inputs(source,other_source))['qualified']
    projection=replace(synthetic_market_binding(entry,source.representation_key,frequency=frequency),name='market')
    grant=AuthorizedRun('isolated-run','isolated-owner',('A.SH',),BASE-100,BASE+duration+100)
    gateway=RunDataGateway(formal,AuthenticatedPrincipal('isolated-owner'),grant,(projection,),batch_rows=2)
    ctx=gateway.open(dict(run_id='isolated-run',universe=['A.SH']))
    req=request(end=BASE+duration,frequency=frequency,fields=('open','high','low','close','quantity'))
    result,output=rust_probe(gateway,ctx,tmp_path,req)
    assert result.returncode==0,output
    assert output['count']==1
    event=output['samples'][0]['event']
    assert event['interval_start_ns']==str(BASE) and event['interval_end_ns']==str(BASE+duration)
    assert event['close']=='10.010000000000000000' and event['quantity']==20

def test_changed_legacy_restriction_same_generation(formal):
    spec,gateway,ctx=setup(formal)
    next(gateway.read('market',request(),ctx))
    with formal.catalog.engine.begin() as c:
        c.execute(restrictions.insert().values(origin_key='isolated',dataset=spec.name,fields_json='[]',
            reason='quality changed',payload_json='{}',located=True))
    error('DATA_CHANGED',lambda:gateway.check(ctx))

@pytest.mark.parametrize('disconnect',[False,True])
def test_actual_ipc_backpressure_cancel_disconnect_frees_read_lock_and_buffers(formal,disconnect):
    spec,gateway,ctx=setup(formal,count=300)
    gateway.batch_rows=300
    cancelled=threading.Event(); sending=threading.Event(); failures=[]
    parent,child=socket.socketpair()
    parent.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4096)
    parent_channel=transport.Channel(parent,cancelled=cancelled.is_set,timeout=2)
    client_channel=transport.Channel(child,timeout=2)
    original_send=parent_channel.send
    def send(frame):
        if frame.status=='batch':
            sending.set()
        return original_send(frame)
    parent_channel.send=send
    def run():
        try:
            serve(parent_channel,gateway,ctx)
        except transport.TransportError:
            pass
        except BaseException as e:
            failures.append(e)
    before=pa.total_allocated_bytes()
    thread=threading.Thread(target=run); thread.start()
    client=transport.Client(client_channel,'isolated-run')
    sid=client.call('open_stream',{'binding':'market','request':request(),'max_rows':300}).body['stream_id']
    client_channel.send(transport.Frame('isolated-run',2,'next','request',{'stream_id':sid}))
    assert sending.wait(2)  # now blocked on Arrow send, consumer has no credit
    with formal.locks.writer(spec.name,timeout_ms=100) as guard:
        with guard.commit(timeout_ms=100):
            pass
    if disconnect:
        client_channel.close()
    else:
        cancelled.set()
    thread.join(3); client_channel.close()
    assert not thread.is_alive() and not failures and ctx.closed
    gc.collect()
    assert pa.total_allocated_bytes()<=before+4096
    assert not list((formal.files.root/'.scratch').rglob('*.parquet'))

def test_quality_state_content_changes_not_only_issue_count(formal):
    spec,gateway,ctx=setup(formal)
    quality(formal,spec)
    gateway.declare('market',ctx)
    with formal.catalog.engine.begin() as c:
        c.execute(text("UPDATE data_store_issues SET reason='CHANGED'"))
    error('DATA_CHANGED',lambda:gateway.check(ctx))

def test_registered_empty_nodes_and_overflow_cannot_become_fake_empty(formal):
    spec=replace(contract(),semantics={'entry_id':'E50','issue_scope':'object-key-v1'})
    formal.register(spec)
    with formal.catalog.engine.begin() as c:
        c.execute(entry_status.insert().values(entry_id='E50',summary_json=json.dumps(
            {'overflow_restriction':{'partition':'blocked','blocking_objects':1}})))
    gateway=make_gateway(formal,(binding(spec),))
    ctx=gateway.open(dict(run_id='isolated-run',universe=['A.SH']))
    error('DATA_RESTRICTED',lambda:list(gateway.read('market',request(),ctx)),'DATA_RESTRICTED')

def test_ipc_lookback_reuses_window_and_reads_delta(formal):
    _,gateway,ctx=setup(formal,count=40)
    parent,child=socket.socketpair()
    failures=[]
    def run():
        try:
            serve(transport.Channel(parent,timeout=3),gateway,ctx)
        except BaseException as e:
            failures.append(e)
    thread=threading.Thread(target=run); thread.start()
    client=transport.Client(transport.Channel(child,timeout=3),'isolated-run')
    def window(end):
        sid=client.call('open_stream',{'binding':'market','request':request(end=end,count=4),'max_rows':2}).body['stream_id']
        frames=[]
        while True:
            frame=client.call('next',{'stream_id':sid})
            if frame.status=='eof':
                break
            assert frame.body['rows']<=2
            frames.append(pa.ipc.open_stream(frame.payload).read_all())
        return pa.concat_tables(frames)
    first=window(BASE+8); reads=gateway.stats['storage_reads']; loaded=gateway.stats['storage_rows']
    assert first['time_ns'].to_pylist()==[BASE+7,BASE+7,BASE+8,BASE+8]
    assert window(BASE+8).equals(first) and gateway.stats['storage_reads']==reads
    assert window(BASE+9)['time_ns'].to_pylist()==[BASE+8,BASE+8,BASE+9,BASE+9]
    assert gateway.stats['storage_rows']-loaded==2
    client.call('close',{}); thread.join(3); client.channel.close()
    assert not thread.is_alive() and not failures and ctx.closed

def test_rust_rejects_source_decimal_outside_exact_execution_range(formal,tmp_path):
    spec,gateway,ctx=setup(formal,count=2)
    put(formal,spec,rows(2,price='12345678901234567890.123456789012345678'),token='exact-but-too-wide')
    result,output=rust_probe(gateway,ctx,tmp_path,request())
    assert result.returncode==1 and output['code']=='NUMERIC_RANGE_UNSUPPORTED'

def test_disconnect_during_actual_currentstore_read_interrupts_lock_and_owners(formal,monkeypatch):
    import time
    spec,gateway,ctx=setup(formal)
    reading=threading.Event(); failures=[]
    original=formal._pin_files
    def slow_pin(spec,refs,stack,space):
        reading.set()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            space.check()
            time.sleep(.005)
        return original(spec,refs,stack,space)
    monkeypatch.setattr(formal,'_pin_files',slow_pin)
    parent,child=socket.socketpair()
    def run():
        try:
            serve(transport.Channel(parent,timeout=3),gateway,ctx)
        except transport.TransportError:
            pass
        except BaseException as e:
            failures.append(e)
    thread=threading.Thread(target=run);thread.start()
    client=transport.Client(transport.Channel(child,timeout=3),'isolated-run')
    sid=client.call('open_stream',{'binding':'market','request':request(),'max_rows':2}).body['stream_id']
    client.channel.send(transport.Frame('isolated-run',2,'next','request',{'stream_id':sid}))
    assert reading.wait(2)
    began=time.monotonic();client.channel.close();thread.join(1)
    assert not thread.is_alive() and time.monotonic()-began<1 and not failures and ctx.closed
    with formal.locks.writer(spec.name,timeout_ms=100) as guard:
        with guard.commit(timeout_ms=100):
            pass
    assert not list((formal.files.root/'.scratch').rglob('*.parquet'))
