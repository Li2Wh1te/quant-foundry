"""Real E50/E70 CurrentStore nodes; execution facts are named synthetic oracles.

No formal production contract, source switch or quality exception is published.
The actual FormalProvider and Rust/Python callbacks consume these node layouts.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event, Thread
from types import SimpleNamespace

import pyarrow as pa
import pytest

if os.getenv('QF_S3_INTEGRATION_REQUIRED') == '1':
    import quantfoundry
else:
    quantfoundry = pytest.importorskip('quantfoundry', reason='installed S3 wheel required')

from app.backtest_service.data_gateway import AuthorizedRun, GatewayError, serve
from app.backtest_service.formal_runtime import (
    AcceptedRunPlan, FormalDailyGateway, REQUIRED_EXECUTION_FACTS, daily_node_binding,
)
from app.backtest_service.runtime_contracts import AcceptedWork, Claim, RuntimeFault, WorkerLimits
from app.backtest_service.service import FormalProvider
from app.core.auth import AuthenticatedPrincipal
from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry
from tests.test_s3_data_gateway import database, limits, store, formal, quality

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')
SDK_TESTS = Path(__file__).resolve().parents[2]/'sdk/python/tests'
sys.path.insert(0, str(SDK_TESTS))
from support_sdk import spec

MODEL = 'synthetic_d18_formal_node_oracles'
NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


def nodes(formal, entry_id='E70', *, quantities=None):
    entry = BY_ID[entry_id]
    packet = spec(days=3, securities=('A.SH', 'B.SH'))
    # Native subject order need not equal the public stable run-key order.
    identifiers = {s: ('NATIVE-'+other, 'isolated-stable-'+s) for s,other in
                   zip(packet['config']['universe'],reversed(packet['config']['universe']))}
    inputs = []
    for i, row in enumerate(packet['calendar']):
        for security, (subject, _) in identifiers.items():
            quantity = quantities[i] if quantities is not None else ('10.01' if entry_id=='E70' else '1001')
            if entry_id == 'E70':
                body = dict(source='tushare', ts_code=subject, trade_date=row['date'],
                    open=Decimal('10'), high=Decimal('11'), low=Decimal('9'), close=Decimal('10'),
                    vol=None if quantity is None else Decimal(quantity))
            else:
                body = dict(adjust='none', coverage='observed_rows_only', item=[dict(
                    date_ms=int(row['local_midnight_ns'])//1_000_000,
                    open_price='10', high_price='11', low_price='9', close_price='10',
                    volume=quantity, turnover=None)])
            inputs.append(LocalInput(entry.source, entry.native, subject, MODEL, NOW, body, digest(body)))
    # Normalization and the sole current pipeline publish only this private fixture.
    from tests.test_data_store_local_pipeline import Inputs
    outcome = run_entry(formal, entry, Inputs(*inputs))
    assert outcome['qualified'], outcome
    representation = next(normalize(entry, inputs[0])).representation
    binding = daily_node_binding(entry_id, representation=representation, identities=identifiers,
        calendar=packet['calendar'], shares_per_quantity='100' if entry_id == 'E70' else '1',
        accepted_facts=REQUIRED_EXECUTION_FACTS,
        evidence={'kind':'synthetic','reference':MODEL})
    packet['source'] = dict(kind='ipc', socket_path='/run/qf/ipc/data.sock', binding=binding.name, request={})
    work = AcceptedWork('isolated-owner', deepcopy(packet['config']), '', WorkerLimits(), {})
    return binding, packet, work


def gateway(formal, binding, packet, *, rows=1):
    cal = packet['calendar']
    grant = AuthorizedRun('isolated-formal-node-run', 'isolated-owner', tuple(packet['config']['universe']),
        int(cal[0]['before_open_ns']), int(cal[-1]['after_close_ns']))
    gw = FormalDailyGateway(formal, AuthenticatedPrincipal(grant.owner_scope), grant, (binding,),
        calendar=cal, batch_rows=rows)
    ctx = gw.open(dict(run_id=grant.run_id, universe=list(grant.universe)), dependencies=(binding.name,))
    return gw, ctx


def test_formal_plan_checks_data_credit_against_captured_worker_quota_before_admission(formal):
    binding, packet, work = nodes(formal)
    packet['data_budget_bytes'] = 1024**3
    plan = AcceptedRunPlan(binding, packet, check_facts=lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeFault, match='内存的一半') as failure:
        plan.validate(work)
    assert failure.value.code == 'RESOURCE_LIMIT'
    from dataclasses import replace
    approved = replace(work, limits=WorkerLimits(cpu_millis=2000,memory_bytes=5*1024**3))
    assert plan.validate(approved) == packet['calendar_reference']
    packet['data_budget_bytes'] = True
    with pytest.raises(RuntimeFault) as failure:
        plan.validate(approved)
    assert failure.value.code == 'RESOURCE_LIMIT'


def request(packet, *, end=None, count=None):
    return dict(securities=list(packet['config']['universe']), fields=['open','close','quantity'],
        frequency='1d', start_ns=None if count else packet['calendar'][0]['before_open_ns'],
        end_ns=end or packet['calendar'][-1]['after_close_ns'], count_per_security=count, adjustment='none')


@pytest.mark.parametrize('entry_id', ['E50','E70'])
def test_real_nodes_cross_symbol_replay_count_cache_and_no_future(formal, entry_id):
    binding, packet, _ = nodes(formal, entry_id)
    expected = None
    for rows in (1,2,4):
        gw, ctx = gateway(formal, binding, packet, rows=rows)
        try:
            invisible = list(gw.read(binding.name, request(packet, end=packet['calendar'][0]['before_open_ns']), ctx))
            assert sum(b.table.num_rows for b in invisible) == 0
            result = pa.concat_tables([b.table for b in gw.read(binding.name, request(packet), ctx)])
            assert result.num_rows == 6
            assert result['security'].to_pylist() == ['A.SH','B.SH']*3
            assert result['time_ns'].to_pylist() == [int(c['session']['close_ns']) for c in packet['calendar'] for _ in range(2)]
            assert result['quantity'].to_pylist() == [1001]*6
            if expected is not None:
                assert result.equals(expected)
            expected = result
            window_request = request(packet, count=2)
            frame = pa.concat_tables([b.table for b in gw.read(binding.name, window_request, ctx)])
            assert frame.num_rows == 4
            reads = gw.stats['storage_reads']
            assert pa.concat_tables([b.table for b in gw.read(binding.name, window_request, ctx)]).equals(frame)
            assert gw.stats['storage_reads'] == reads
            # A quality-only change with no content generation still aborts cache reuse.
            quality(formal, binding.spec)
            with pytest.raises(GatewayError, match='发生变化') as changed:
                list(gw.read(binding.name, window_request, ctx))
            assert changed.value.code == 'DATA_CHANGED'
        finally:
            gw.close(ctx)
        # Remove only the quality injection in this fixture's private schema;
        # production CurrentStore and gate state are never changed.
        from sqlalchemy import text
        with formal.catalog.engine.begin() as c:
            c.execute(text('DELETE FROM data_store_issues WHERE dataset=:d AND issue_key=\'quality\''), {'d':binding.spec.name})


@pytest.mark.parametrize('quantity,code', [(None,'CAPABILITY_UNAVAILABLE'),('0.0001','NUMERIC_RANGE_UNSUPPORTED'),
    ('92233720368547758.08','NUMERIC_RANGE_UNSUPPORTED')])
def test_missing_fractional_and_overflow_shares_never_coerced(formal, quantity, code):
    binding, packet, _ = nodes(formal, quantities=[quantity]*3)
    gw, ctx = gateway(formal, binding, packet)
    try:
        with pytest.raises(GatewayError) as rejected:
            list(gw.read(binding.name, request(packet), ctx))
        assert rejected.value.code == code
    finally:
        gw.close(ctx)


def test_default_provider_cannot_accept_user_metadata_as_execution_facts(formal):
    binding, packet, work = nodes(formal)
    work.specification.update(account_snapshot=dict(metadata=dict(raw=True,CNY=True,shares_per_lot=100)))
    settings = SimpleNamespace()
    provider = FormalProvider(settings, store=formal)
    for operation in (lambda: provider.check(work), lambda: provider.binding(work, Claim('private','token',1),Event())):
        with pytest.raises(RuntimeFault) as blocked:
            operation()
        assert blocked.value.code == 'CAPABILITY_UNAVAILABLE'
        assert 'E70:provider_reported_raw_basis_and_lot_to_share_conversion_unverified' in str(blocked.value)
    for missing in REQUIRED_EXECUTION_FACTS:
        with pytest.raises(GatewayError) as rejected:
            daily_node_binding('E70', representation=binding.constants['channel'], identities={'A.SH':('X','stable')},
                calendar=packet['calendar'], shares_per_quantity='100',
                accepted_facts=REQUIRED_EXECUTION_FACTS-{missing}, evidence=MODEL)
        assert rejected.value.code == 'CAPABILITY_UNAVAILABLE'


def test_actual_formal_provider_runs_real_rust_and_python_on_node_store(formal, tmp_path):
    binding, packet, work = nodes(formal)
    checks = []
    def facts_check(accepted, *, connection):
        assert accepted is work and accepted.config['universe'] == ['A.SH','B.SH']
        checks.append('isolated-dated-facts-checked')
    plan = AcceptedRunPlan(binding, packet, check_facts=facts_check)
    preflights = []
    process = SimpleNamespace(image='isolated-built-worker',daemon=SimpleNamespace(
        preflight=lambda image:preflights.append(image)))
    provider = FormalProvider(SimpleNamespace(environment='test'), store=formal, plan_resolver=lambda _:plan, process=process)
    assert provider.check(work) == packet['calendar_reference']
    actual = provider.binding(work, Claim('isolated-formal-node-run','token',1), Event())
    assert checks and len(preflights) == 2
    listener = socket.socket(socket.AF_UNIX)
    endpoint = tmp_path/'data.sock'
    listener.bind(str(endpoint)); listener.listen(1)
    actual.spec['source']['socket_path'] = str(endpoint)
    errors = []
    from quantfoundry._transport import Channel
    def peer():
        try:
            connection, _ = listener.accept()
            serve(Channel(connection), actual.gateway, actual.context, close_context=False)
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=peer, daemon=True); thread.start()
    module = SimpleNamespace()
    exec('''from quantfoundry.api import get_price,order,record
def initialize(c): c.state.calls=0
def handle_data(c,data):
    c.state.calls+=1
    frame=get_price(['A.SH','B.SH'],count=2,fields=['close'])
    assert list(frame.columns)==['security','time_ns','close']
    assert all(type(value).__name__=='Decimal' for value in frame['close'])
    if c.state.calls==1: assert order('A.SH',100).accepted
    record(callbacks=c.state.calls)
''', module.__dict__)
    from quantfoundry._worker import _run_module
    from quantfoundry.data import _MarketRoute
    result = None
    try:
        result = _run_module(module, actual.spec, market_routes={'1d':_MarketRoute(binding.name,('open','high','low','close','quantity'))})
        summary = json.loads(result.summary_json())
        assert summary['outcome']['status'] == 'succeeded', summary
        assert summary['progress']['completed_events'] == 6
        assert result._callback_metrics['by_kind']['handle_data']['count'] == 3
        trades = json.loads(result.page_json('trades', None, 100))['items']
        assert len(trades) == 1 and trades[0]['quantity'] == 100 and trades[0]['fee'] == '1.00'
    finally:
        if result is not None: result.close()
        listener.close(); thread.join(5)
        actual.gateway.close(actual.context)
    assert not thread.is_alive() and not errors, errors


def test_accepted_facts_change_aborts_cache_and_same_connection_finalization(formal):
    binding,packet,work=nodes(formal)
    changed=Event();connections=[]
    def check_facts(accepted, *, connection):
        assert accepted is work
        if changed.is_set():
            raise RuntimeFault('DATA_CHANGED','isolated accepted dated facts changed')
        if connection is not None:
            assert connection.in_transaction()
            connections.append(connection)
    plan=AcceptedRunPlan(binding,packet,check_facts=check_facts)
    process=SimpleNamespace(image='explicit-isolated-preflight',daemon=SimpleNamespace(preflight=lambda _:None))
    provider=FormalProvider(SimpleNamespace(environment='test'),store=formal,
        plan_resolver=lambda _:plan,process=process)
    actual=provider.binding(work,Claim('isolated-facts','token',1),Event())
    try:
        query=request(packet,count=2)
        assert sum(b.table.num_rows for b in actual.gateway.read(binding.name,query,actual.context))==4
        assert actual.gateway.finalize(actual.context,lambda connection: connection is connections[-1])
        changed.set()
        with pytest.raises(RuntimeFault) as rejected:
            list(actual.gateway.read(binding.name,query,actual.context))
        assert rejected.value.code=='DATA_CHANGED'
        with pytest.raises(RuntimeFault) as rejected:
            actual.gateway.finalize(actual.context,lambda _:pytest.fail('stale facts cannot commit'))
        assert rejected.value.code=='DATA_CHANGED'
    finally:
        actual.gateway.close(actual.context)
    production=FormalProvider(SimpleNamespace(environment='production'),store=formal,
        plan_resolver=lambda _:plan,process=process)
    with pytest.raises(RuntimeFault) as refused:
        production.check(work)
    assert refused.value.code=='CAPABILITY_UNAVAILABLE'
