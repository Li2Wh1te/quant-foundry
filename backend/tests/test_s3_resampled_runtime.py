"""Real CurrentStore and FormalProvider with explicitly synthetic minute facts."""
from copy import deepcopy
from dataclasses import replace
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

from app.backtest_service.data_gateway import GatewayError, MARKET_SCHEMA, MarketBinding, serve
from app.backtest_service.formal_runtime import AcceptedRunPlan
from app.backtest_service.resampling import ResampledBarGateway
from app.backtest_service.runtime_contracts import AcceptedWork, Claim, WorkerLimits
from app.backtest_service.service import FormalProvider
from app.data_store.catalog import SourceUpdate
from app.data_store.schema import fingerprint
from tests.test_s3_data_gateway import database, store, formal, quality, contract
from tests.test_s3_data_gateway import limits as gateway_limits

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'sdk/python/tests'))
from support_sdk import spec

MINUTE = 60_000_000_000


@pytest.fixture
def limits(gateway_limits):
    return replace(gateway_limits, file_rows=1000)


def accepted_minutes(formal, *, target='60m', missing=False, short=False, missing_quantity=False, invalid_calendar=False):
    packet = spec(days=2, frequency=target)
    if invalid_calendar:
        packet['calendar'][0]['week']['trading_day'] = 0
    if short:
        window = packet['calendar'][0]['bar_windows'][0]
        window['end_ns'] = str(int(window['start_ns']) + 35 * MINUTE)
    key = ('time_ns', 'security', 'source_session', 'channel', 'sequence', 'stable_input_sequence')
    base = contract('synthetic_d18_accepted_minutes')
    schema = pa.schema([pa.field(f.name, f.type, nullable=f.name not in key, metadata=f.metadata)
                        for f in base.schema], metadata=base.schema.metadata)
    dataset = replace(base, schema=schema, key=key)
    formal.register(dataset)
    rows = []
    for session in packet['calendar']:
        ordinal = 0
        for window in session['bar_windows']:
            begin, end = int(window['start_ns']), int(window['end_ns'])
            for start in range(begin, end, MINUTE):
                for security in packet['config']['universe']:
                    if missing and ordinal == 20 and security == 'A.SH':
                        continue
                    rows.append(dict(kind='bar', security=security, session=session['session']['key'],
                        source_session=session['session']['key'], channel='synthetic-accepted-1m',
                        sequence=ordinal + 1, stable_input_sequence=start + 2**63,
                        time_ns=start + MINUTE, interval_start_ns=start, interval_end_ns=start + MINUTE,
                        open='10.00', high='11.00', low='9.00', close=f'10.{ordinal%100:02}',
                        quantity=None if missing_quantity and ordinal == 20 else 1000))
                ordinal += 1
    formal.upsert(dataset, 'default', [pa.RecordBatch.from_pylist(rows, schema=schema)],
        SourceUpdate('test', fingerprint('accepted-minute-fixture'), fingerprint('synthetic-d18'), 0),
        lower=(int(packet['calendar'][0]['before_open_ns']),),
        upper=(int(packet['calendar'][-1]['after_close_ns']) + 1,), source_check=lambda _: True)
    market = MarketBinding('accepted-minutes', dataset, '1m', {n: n for n in MARKET_SCHEMA.names}, {},
        lambda security, start, end: ((start,), (end + 1,)), 'time_ns')
    packet['source'] = dict(kind='ipc')
    work = AcceptedWork('isolated-owner', deepcopy(packet['config']), '', WorkerLimits(), {})
    checks = []
    def check_facts(accepted, *, connection):
        assert accepted is work
        checks.append(connection)
    plan = AcceptedRunPlan(market, packet, check_facts=check_facts)
    process = SimpleNamespace(image='isolated-prebuilt-worker', daemon=SimpleNamespace(preflight=lambda _: None))
    provider = FormalProvider(SimpleNamespace(environment='test'), store=formal,
        plan_resolver=lambda _: plan, process=process)
    return provider, work, dataset, checks


@pytest.mark.parametrize('frequency', ['5m', '15m', '30m', '60m'])
@pytest.mark.parametrize('page_rows', [1, 19])
def test_formal_resampling_preserves_calendar_prices_quantities_and_cache(formal, frequency, page_rows):
    provider, work, dataset, checks = accepted_minutes(formal, target=frequency)
    assert provider.check(work) == 'synthetic-d10-calendar'
    actual = provider.binding(work, Claim('isolated-resampled-run', 'token', 1), Event())
    assert isinstance(actual.gateway, ResampledBarGateway)
    assert set(actual.market_routes) == {frequency, '1m'}
    query = actual.spec['source']['request']
    gw, ctx = actual.gateway, actual.context
    try:
        table = pa.concat_tables([batch.table for batch in gw.read('accepted-minutes', query, ctx, page_rows=page_rows)])
        width = int(frequency[:-1])
        assert table.num_rows == 2 * 2 * 240 // width
        assert gw.replay_raw_rows == 2 * 2 * 240
        assert table['quantity'].to_pylist() == [width * 1000] * table.num_rows
        first = table.to_pylist()[0]
        assert first['open'] == '10.00' and first['close'] == f'10.{(width-1)%100:02}'
        # Each bucket is within one declared morning/afternoon interval.
        for row in table.to_pylist():
            session = next(s for s in actual.spec['calendar'] if s['session']['key'] == row['session'])
            assert any(int(w['start_ns']) <= row['interval_start_ns'] < row['interval_end_ns'] <= int(w['end_ns'])
                       for w in session['bar_windows'])
        count_query = dict(query, start_ns=None, count_per_security=2)
        expected = pa.concat_tables([b.table for b in gw.read('accepted-minutes', count_query, ctx)])
        reads = gw.stats['storage_reads']
        assert pa.concat_tables([b.table for b in gw.read('accepted-minutes', count_query, ctx)]).equals(expected)
        assert gw.stats['storage_reads'] == reads
        before = dict(count_query, end_ns=actual.spec['calendar'][0]['before_open_ns'])
        assert sum(b.table.num_rows for b in gw.read('accepted-minutes', before, ctx)) == 0
        assert gw.finalize(ctx, lambda connection: connection is checks[-1])
        quality(formal, dataset)
        with pytest.raises(GatewayError) as changed:
            list(gw.read('accepted-minutes', count_query, ctx))
        assert changed.value.code == 'DATA_CHANGED'
    finally:
        gw.close(ctx)
    assert not gw.resampled_cache and gw.resampled_query is None


@pytest.mark.parametrize('failure', ['missing', 'short', 'missing_quantity'])
def test_missing_fine_bar_or_partial_target_bucket_cannot_become_execution_bar(formal, failure):
    provider, work, _, _ = accepted_minutes(formal, **{failure: True})
    with pytest.raises(GatewayError) as rejected:
        provider.check(work)
    assert rejected.value.code == 'CAPABILITY_UNAVAILABLE'


def test_native_calendar_refusal_keeps_the_unified_error_code(formal):
    provider, work, _, _ = accepted_minutes(formal, invalid_calendar=True)
    with pytest.raises(GatewayError) as rejected:
        provider.check(work)
    assert rejected.value.code == 'RULE_UNAVAILABLE'


def test_formal_hour_execution_runs_actual_python_callbacks_and_fees(formal, tmp_path):
    provider, work, _, _ = accepted_minutes(formal)
    actual = provider.binding(work, Claim('isolated-formal-hours', 'token', 1), Event())
    listener = socket.socket(socket.AF_UNIX)
    endpoint = tmp_path / 'data.sock'
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
    exec('''from quantfoundry.api import get_price, order, record
def initialize(c): c.state.calls = 0
def handle_data(c, data):
    c.state.calls += 1
    frame = get_price(['A.SH', 'B.SH'], count=2, fields=['close'])
    assert all(type(value).__name__ == 'Decimal' for value in frame['close'])
    assert max(frame['time_ns']) <= c.now_ns
    if c.state.calls == 1: assert order('A.SH', 100).accepted
    record(callbacks=c.state.calls)
''', module.__dict__)
    from quantfoundry._worker import _run_module
    from quantfoundry.data import _MarketRoute
    result = None
    try:
        routes = {f: _MarketRoute(row['binding'], tuple(row['fields'])) for f, row in actual.market_routes.items()}
        result = _run_module(module, actual.spec, market_routes=routes)
        summary = json.loads(result.summary_json())
        assert summary['outcome']['status'] == 'succeeded', summary
        assert summary['progress']['completed_events'] == 16
        assert result._callback_metrics['by_kind']['handle_data']['count'] == 8
        trades = json.loads(result.page_json('trades', None, 100))['items']
        assert len(trades) == 1 and trades[0]['quantity'] == 100 and trades[0]['fee'] == '1.00'
    finally:
        if result is not None:
            result.close()
        listener.close(); thread.join(5)
        actual.gateway.close(actual.context)
    assert not thread.is_alive() and not errors, errors
