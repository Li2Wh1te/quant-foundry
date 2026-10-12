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
from support_sdk import ipc_spec
from s3_runservice_suite import account_payload
from quantfoundry._transport import Channel
from quantfoundry._worker import _run_module
from quantfoundry.data import _MarketRoute

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')


def test_b1_actual_currentstore_full_5000_scope_diagnostic(formal, tmp_path):
    from benchmark import Provider
    load = Load('B1-admission-diagnostic', 5000, 2)
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
    work = AcceptedWork('isolated-owner', config, MOMENTUM, WorkerLimits(),
        dict(account_snapshot=dict(fee_schedule=account_payload())))
    assert provider.check(work) == packet['calendar_reference']
    bound = provider.binding(work, Claim('b1-scope-diagnostic','token',1), Event())
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
    source = MOMENTUM.replace("    ordered=frame.sort_values", "    assert len(frame)==c.state.calls*5000\n    ordered=frame.sort_values")
    module = SimpleNamespace();exec(compile(source,'diagnostic_strategy.py','exec'),module.__dict__)
    result = None
    try:
        result = _run_module(module,bound.spec,market_routes={'1d':_MarketRoute('market',('open','high','low','close','quantity'))})
        summary = json.loads(result.summary_json())
        assert summary['outcome']['status'] == 'succeeded', (summary['outcome'],[(type(e).__name__,str(e)[:500]) for e in errors])
        assert summary['progress']['completed_events'] == 10000
        assert result._callback_metrics['by_kind']['handle_data']['count'] == 2
        assert 0 < bound.gateway.stats['cache_rows'] <= 10000
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
