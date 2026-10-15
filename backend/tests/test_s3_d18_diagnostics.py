"""Real SDK/Rust diagnostics; these small cases never substitute for B1/B4.

B1 retains the full 5000-security admission scope. B4 retains the fixed price
path long enough to hit its unchanged dated per-order rule at session 103.
"""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sys
import time
from threading import Event, Thread
from types import SimpleNamespace

import pytest

if os.getenv('QF_S3_INTEGRATION_REQUIRED') == '1':
    import quantfoundry
else:
    quantfoundry = pytest.importorskip('quantfoundry', reason='installed S3 wheel required')

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT/'tests', ROOT/'sdk/python/tests', ROOT/'ops/s3-d18'):
    sys.path.insert(0, str(path))
from app.backtest_service.data_gateway import serve
from app.backtest_service.runtime_contracts import AcceptedWork, Claim, WorkerLimits
from app.data_store.catalog import SourceUpdate
from app.data_store.schema import fingerprint
from tests.test_s3_data_gateway import database, limits, store, formal
from fixtures import Load, batches, dataset, template, facts
from strategies import MOMENTUM
from support_sdk import ipc_spec, spec
from s3_runservice_suite import account_payload
from quantfoundry._transport import Channel
from quantfoundry._worker import _run_module
from quantfoundry.data import _MarketRoute

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')


def test_hot_probe_budget_starts_at_commit_and_still_rejects_late_terminal():
    from benchmark import _CorrectionProbe
    clock = [0.0]
    release, entered = Event(), Event()
    def correct(committed, stopped):
        entered.set()
        assert release.wait(2)
        committed()
        return {'real_commit_observed': True}
    probe = _CorrectionProbe(correct, clock=lambda: clock[0])
    try:
        assert probe.poll(ready=True, terminal=False) is False
        assert entered.wait(2)
        clock[0] = 20.0
        assert probe.poll(ready=True, terminal=False) is False
        assert not probe.committed.is_set()
        release.set()
        assert probe.done.wait(2)
        clock[0] = 35.0
        assert probe.poll(ready=True, terminal=False) is True
        clock[0] = 35.001
        with pytest.raises(TimeoutError, match='after correction commit'):
            probe.poll(ready=True, terminal=True)
    finally:
        release.set()
        probe.close()


@pytest.mark.parametrize('kind', ['trade_tick', 'quote_tick'])
def test_hot_probe_real_late_commit_keeps_data_changed_and_partial(formal, tmp_path, kind):
    """Delay only synthetic batch arrival; all checks/write/Rust calls are real."""
    from benchmark import Provider, _CorrectionProbe, hot_correction, prepare
    from strategies import HOT_PROBE
    from quantfoundry import _native
    load = Load('late-commit-' + kind, 1, 1, ticks=1000, kind=kind)
    formal.limits = replace(formal.limits, file_rows=1000)
    prepare(formal, load)
    provider = Provider(formal, load, 'tick', None, {})
    work = AcceptedWork('isolated-owner', deepcopy(provider.spec['config']), HOT_PROBE,
        WorkerLimits(cpu_millis=2000, memory_bytes=5*1024**3, wall_seconds=3600),
        dict(account_snapshot=dict(fee_schedule=account_payload())))
    bound = provider.binding(work, Claim('late-commit-probe', 'token', 1), Event())
    endpoint = tmp_path/'late.sock'
    listener = socket.socket(socket.AF_UNIX);listener.bind(str(endpoint));listener.listen(1)
    bound.spec['source']['socket_path'] = str(endpoint)
    marker, staged, arrived, finished = Event(), Event(), Event(), Event()
    peer_errors, worker_errors, output, phases = [], [], {}, {}
    cancellation = _native.CancellationToken()
    def peer():
        try:
            connection, _ = listener.accept()
            serve(Channel(connection, timeout=45), bound.gateway, bound.context,
                  close_context=False, session_facts=bound.session_facts)
        except BaseException as error:
            peer_errors.append(error)
    module = SimpleNamespace()
    exec(compile(HOT_PROBE, 'strategy.py', 'exec'), module.__dict__)
    module.print = lambda *args, **kwargs: marker.set()
    def worker():
        result = None
        try:
            result = _run_module(module, bound.spec,
                market_routes={'tick': _MarketRoute('market', tuple(provider.request(work)['fields']))},
                cancellation=cancellation)
            output['summary'] = json.loads(result.summary_json())
            output['callbacks'] = result._callback_metrics
        except BaseException as error:
            worker_errors.append(error)
        finally:
            if result is not None: result.close()
            finished.set()
    class DelayedBatchStore:
        def __getattr__(self, name):return getattr(formal, name)
        def upsert(self, spec, partition, batches, source, **kwargs):
            def late_batch():
                staged.set()
                # Exceeds the former 15-second callback-relative deadline.
                # It is input staging, before the actual CurrentStore commit.
                if probe.stopped.wait(16):raise RuntimeError('test correction cancelled')
                arrived.set()
                yield from batches
            return formal.upsert(spec, partition, late_batch(), source, **kwargs)
    probe = _CorrectionProbe(lambda committed, stopped: hot_correction(
        DelayedBatchStore(), provider, committed=committed, stopped=stopped, phases=phases))
    peer_thread, worker_thread = Thread(target=peer, daemon=True), Thread(target=worker, daemon=True)
    peer_thread.start();worker_thread.start()
    started = time.monotonic()
    staging_still_running = False
    try:
        while True:
            complete = probe.poll(ready=marker.is_set(), terminal=finished.is_set())
            if staged.is_set() and not arrived.is_set() and time.monotonic()-started > 15.5:
                assert not finished.is_set(), 'probe exhausted its deadline before commit'
                staging_still_running = True
            if finished.is_set() and complete:break
            assert time.monotonic()-started < 45, 'isolated regression exceeded its test budget'
            finished.wait(.01)
        summary = output['summary']
        assert staging_still_running
        assert summary['outcome']['status'] == 'failed', summary
        assert summary['outcome']['error']['code'] == 'DATA_CHANGED', summary
        assert summary['outcome']['partial'] is True, summary
        assert summary['progress']['completed_events'] == 100
        assert output['callbacks']['by_kind']['handle_tick']['count'] == 100
        assert probe.result['generation_after'] > probe.result['generation_before']
        assert probe.result['old_context_terminal'] == 'DATA_CHANGED'
        assert probe.result['corrected_read_verified'] is True
        assert phases['upsert_seconds'] >= 16
        assert 0 <= probe.measurement()['commit_to_terminal_seconds'] <= 15
        assert not worker_errors and not peer_errors, (worker_errors, peer_errors)
    finally:
        cancellation.cancel()
        probe.close()
        listener.close();worker_thread.join(5);peer_thread.join(5)
        bound.gateway.close(bound.context)
    assert not worker_thread.is_alive() and not peer_thread.is_alive()


def test_fact_page_keeps_every_wire_value_and_distinct_side_commissions():
    load=Load('fact-wire-diagnostic',30,3)
    packet=template(load)
    commissions={side:dict(commission_rate='0',minimum_commission='1',settlement_scale=2,rounding='half_up')
                 for side in ('buy','sell')}
    for calendar_row in packet['calendar']:
        page=facts(load,calendar_row,3,11,commissions)
        expected=[]
        for security in load.universe[3:14]:
            row=deepcopy(spec(days=1,securities=('S0000.SH',))['facts'][0])
            row['instrument']['security']=row['day']['security']=security
            row['day'].update(date=calendar_row['date'],session=calendar_row['session']['key'])
            row['day']['listing'].update(first_trading_date='2010-01-01',verified_through='2030-12-31')
            from fixtures import MODEL
            row['origin']['reference']=MODEL
            row['disposal_settlement_date']=calendar_row['date']
            for side in ('buy','sell'):
                row[side+'_fees'].update(synthetic_model=MODEL,**commissions[side])
            expected.append(row)
        assert json.dumps(page,sort_keys=True)==json.dumps(dict(
            session=calendar_row['session']['key'],facts=expected,next_offset=14),sort_keys=True)
        with pytest.raises(TypeError):page['facts'][0]['day']['listing']['first_trading_date']='future'
        with pytest.raises(TypeError):page['facts'][0]['permissions']['buy']=False
    asymmetric=dict(commissions,sell=dict(commissions['sell'],minimum_commission='9'))
    page=facts(load,packet['calendar'][0],0,30,asymmetric)
    assert all(row['buy_fees']['minimum_commission']=='1' and row['sell_fees']['minimum_commission']=='9'
               for row in page['facts'])


def test_b1_actual_currentstore_full_5000_scope_diagnostic(formal, tmp_path):
    from benchmark import Provider
    load = Load('B1', 5000, 2)
    spec = dataset(load)
    packet = template(load)
    formal.limits = replace(formal.limits, file_rows=10000)
    formal.register(spec)
    formal.upsert(spec, 'default', batches(load, packet['calendar'], 0, 2, spec.schema),
        SourceUpdate('test',fingerprint('b1-scope'),fingerprint('isolated-d18-diagnostic'),0),
        lower=(int(packet['calendar'][0]['before_open_ns']),),
        upper=(int(packet['calendar'][-1]['after_close_ns'])+1,),source_check=lambda _:True)
    process = SimpleNamespace(image='explicit-native-diagnostic',daemon=SimpleNamespace(preflight=lambda _:None))
    provider = Provider(formal, load, '1d', process, {})
    config = deepcopy(packet['config'])
    config['parameters'].update(securities=load.universe)
    work = AcceptedWork('isolated-owner', config, MOMENTUM,
        WorkerLimits(cpu_millis=2000,memory_bytes=5*1024**3,wall_seconds=3600),
        dict(account_snapshot=dict(fee_schedule=account_payload())))
    assert provider.check(work) == packet['calendar_reference']
    bound = provider.binding(work, Claim('b1-scope-diagnostic','token',1), Event())
    assert bound.gateway.replay_window_ns == 2*24*60*60*10**9
    endpoint = tmp_path/'data.sock'
    listener = socket.socket(socket.AF_UNIX);listener.bind(str(endpoint));listener.listen(1)
    bound.spec['source']['socket_path'] = str(endpoint)
    errors = []
    def peer():
        try:
            connection,_ = listener.accept()
            serve(Channel(connection),bound.gateway,bound.context,close_context=False,
                  session_facts=bound.session_facts)
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=peer,daemon=True);thread.start()
    source = 'from quantfoundry.data import _ACTIVE as _read_active\n' + MOMENTUM.replace(
        "    ordered=frame.sort_values",
        "    assert len(frame)==c.state.calls*5000\n"
        "    assert int(frame['time_ns'].max())<=c.now_ns\n"
        "    session=_read_active.get().session\n"
        "    assert session.window_size>0\n"
        "    assert session.window_size+session.cache_size<=session.cache_bytes\n"
        "    ordered=frame.sort_values")
    module = SimpleNamespace();exec(compile(source,'diagnostic_strategy.py','exec'),module.__dict__)
    result = None
    try:
        result = _run_module(module,bound.spec,market_routes={'1d':_MarketRoute('market',('open','high','low','close','quantity'))})
        summary = json.loads(result.summary_json())
        assert summary['outcome']['status'] == 'succeeded', (summary['outcome'],[(type(e).__name__,str(e)[:500]) for e in errors])
        assert summary['progress']['completed_events'] == 10000
        assert result._callback_metrics['by_kind']['handle_data']['count'] == 2
        assert 0 <= bound.gateway.stats['cache_rows'] <= 10000
        assert bound.gateway.stats['storage_rows'] >= 10000
    finally:
        if result is not None:result.close()
        listener.close();thread.join(5);bound.gateway.close(bound.context)
    assert not thread.is_alive() and not errors,errors


def test_b4_real_orders_keep_quantity_gate_and_record_refusals():
    load = Load('B4-rule-diagnostic',30,120)
    packet = template(load)
    packet['config']['parameters'].update(securities=load.universe,window=5)
    commission = {side:dict(commission_rate='0',minimum_commission='1',settlement_scale=2,rounding='half_up')
                  for side in ('buy','sell')}
    packet['facts'] = [f for row in packet['calendar'] for f in facts(load,row,0,30,commission)['facts']]
    events = []
    for batch in batches(load,packet['calendar'],0,120,dataset(load).schema):
        for row in batch.to_pylist():
            event = {key:row[key] for key in ('security','session','open','high','low','close','quantity')}
            event.update(interval_start_ns=str(row['interval_start_ns']),interval_end_ns=str(row['interval_end_ns']),
                units=dict(price_currency='CNY',quantity_unit='shares'),identity={
                    key:str(row[key]) if key.endswith('sequence') else row[key]
                    for key in ('source_session','channel','sequence','stable_input_sequence')})
            events.append(dict(kind='bar',event=event))
    packet['source'] = dict(kind='fixture',events=events)
    module = SimpleNamespace();exec(compile(MOMENTUM,'diagnostic_strategy.py','exec'),module.__dict__)
    with ipc_spec(packet,batch_rows=10000) as (wired,peer,routes):
        wired['facts'] = []
        result = _run_module(module,wired,market_routes=routes)
        try:
            summary = json.loads(result.summary_json())
            assert summary['outcome']['status'] == 'succeeded',summary
            assert summary['progress']['completed_events'] == 3600
            assert result._callback_metrics['by_kind']['handle_data']['count'] == 120
            records = json.loads(result.page_json('records',None,10000))['items']
            refused = int(records[-1]['values']['order_limit_rejections']['value'])
            assert refused > 0 and int(records[-1]['values']['callbacks']['value']) == 120
            orders = json.loads(result.page_json('orders',None,10000))['items']
            trades = json.loads(result.page_json('trades',None,10000))['items']
            assert orders and trades
            assert all(order['quantity'] <= packet['rules'][0]['maximum_market'] for order in orders)
            assert sum(int(trade['quantity']) for trade in trades) > 0
            assert all(trade['fee'] != '0' for trade in trades)
            logs = json.loads(result.page_json('logs',None,10000))['items']
            assert len(logs) == refused and all('超过单笔申报数量上限' in row['message'] for row in logs)
        finally:
            result.close()
