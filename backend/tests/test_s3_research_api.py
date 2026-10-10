"""Actual CurrentStore -> D04 IPC -> installed wheel -> Rust D05 public API.

Explicit synthetic accepted facts, random isolated PostgreSQL schemas; never
supplier/production data. Absence of the wheel fails in S3 CI, skips in Validate.
"""
from contextlib import contextmanager
from dataclasses import replace, FrozenInstanceError
from decimal import Decimal
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import socket
import threading
import subprocess
import sys

import pyarrow as pa
import pytest

if os.getenv('QF_S3_INTEGRATION_REQUIRED') == '1':
    import quantfoundry
else:
    quantfoundry = pytest.importorskip('quantfoundry', reason='installed S3 wheel required')

from quantfoundry import ContractError, _native
from quantfoundry.data import _DataSession, _MarketRoute, _callback
from quantfoundry._transport import Client, Channel, TransportError
from quantfoundry.api import (get_price, get_current_data, get_fundamentals, get_valuation,
                             get_industry, get_instruments, get_index_stocks, Filter)
from app.core.auth import AuthenticatedPrincipal
from app.backtest_service.data_gateway import AuthorizedRun, MARKET_SCHEMA, DataBatch, serve
from app.backtest_service.research_gateway import (ResearchDataGateway, ResearchBinding, SCHEMA,
                                                  current_research_bindings, FIELDS)
from app.data_store.schema import DatasetSpec, fingerprint
from app.data_store.catalog import SourceUpdate
from app.data_store.readers import Query
from tests.test_s3_data_gateway import (database, store, limits, formal, contract, binding, quality,
                                      BASE, PRICE, rows)

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')


def put(formal, spec, data, token='fixture'):
    state = formal.source_state(spec.name, 'd05')
    source = SourceUpdate('d05', fingerprint(token), fingerprint('synthetic-d05'), state['revision'] if state else 0)
    data = sorted(data, key=lambda r: spec.key_bytes(tuple(r[k] for k in spec.key)))
    securities = sorted({r['security'] for r in data})
    return formal.upsert(spec, 'default', [pa.RecordBatch.from_pylist(data, schema=spec.schema)],
        source, lower=(securities[0],), upper=(securities[-1]+'\0',), source_check=lambda _: True)


def research_spec(kind):
    fields = [pa.field(f.name, f.type, nullable=f.name in ('value', 'report_period', 'effective_until_ns'),
              metadata={b'max_utf8_bytes': b'128'} if pa.types.is_string(f.type) else {b'unit': b'epoch_ns'}) for f in SCHEMA]
    return DatasetSpec('d05_'+kind, pa.schema(fields, metadata=SCHEMA.metadata),
        ('security', 'record_id', 'field'), 'synthetic-d05-v1', {'knowledge_basis': 'synthetic_verified_public_time'})


def research_binding(spec, kind):
    types = {f: ('text', 'synthetic_text') if f in ('member', 'name', 'exchange', 'asset_type', 'currency',
        'listing_date', 'end_date', 'industry_code', 'industry_name', 'taxonomy') else
        ('boolean', 'known_status') if f == 'halted' else ('decimal', 'verified_cumulative_factor') if f == 'factor'
        else ('decimal', 'index_points') if kind == 'price' else ('decimal', 'CNY') for f in FIELDS.get(kind, ('close',))}
    if kind == 'price':
        types = {'close': ('decimal', 'index_points')}
    return ResearchBinding(kind, spec, kind, {n: n for n in SCHEMA.names}, {},
        lambda s, *_: ((s,), (s+'\0',)), 'time_ns', types,
        ('verified_public_time', 'stable_identity', 'verified_effective_dates', 'verified_cumulative_anchor', 'verified_complete_sets'),
        limitations=('synthetic_business_oracle_not_production_data',),
        factor_anchor=dict(factor='1', effective_ns=str(BASE-1000), public_ns=str(BASE-1000)) if kind == 'adjustment' else None)


def fact(security='A.SH', field='revenue', value='123456789012345678901234567890.1234567890123456789',
         published=BASE-10, effective=BASE-100, until=None, record='report-1', period='2025-12-31',
         kind='decimal', unit='CNY', entity=None):
    return dict(security=security, instrument_id=entity or 'stable-'+security, record_id=record,
                field=field, value_kind=kind, value=value, unit=unit, report_period=period,
                time_ns=published, effective_from_ns=effective, effective_until_ns=until)


def market_data(securities=('A.SH',), count=4):
    data = []
    for s in securities:
        for i, row in enumerate(rows(count)):
            row = dict(row, security=s, time_ns=BASE-20+i, sequence=i+1, stable_input_sequence=i+1)
            data.append(row)
    return data


def gateway(formal, bindings, securities=('A.SH',), **kwargs):
    grant = AuthorizedRun('isolated-d05', 'isolated-owner', tuple(securities), BASE-1000, BASE+10**14)
    return ResearchDataGateway(formal, AuthenticatedPrincipal('isolated-owner'), grant, tuple(bindings),
                              batch_rows=512, **kwargs)


@contextmanager
def session(gateway, *, market_routes=None, research_routes=None, frequency='tick', calendar=(), **kwargs):
    context = gateway.open(dict(run_id=gateway.grant.run_id, universe=list(gateway.grant.universe)))
    parent, worker = socket.socketpair()
    failures = []
    def run():
        try:
            serve(Channel(parent, timeout=5), gateway, context)
        except TransportError as error:
            if error.code != 'CANCELLED':
                failures.append(error)
    thread = threading.Thread(target=run, daemon=True); thread.start()
    client = Client(Channel(worker, timeout=5), gateway.grant.run_id)
    data = _DataSession(client, universe=gateway.grant.universe, frequency=frequency,
        market_routes=market_routes or {}, research_routes=research_routes or {}, calendar=calendar, **kwargs)
    try:
        yield data, context
    finally:
        if not client.channel.closed:
            try:
                client.call('close', {})
            except TransportError:
                pass
        client.channel.close(); thread.join(5)
        assert not thread.is_alive() and not failures


def boundary(now=BASE, sequence=None, security='A.SH'):
    key = None if sequence is None else dict(time_ns=str(now), phase='market', security=security,
        identity=dict(source_session='2026-01-01:auction', channel='A', sequence=str(sequence), stable_input_sequence=str(sequence)))
    return dict(now_ns=str(now), market_through=key)


def test_public_price_shape_exact_values_default_simulation_and_empty(formal):
    spec = contract('d05_market'); formal.register(spec); put(formal, spec, market_data())
    g = gateway(formal, [binding(spec)])
    with session(g, market_routes={'tick': _MarketRoute('market', ('price', 'quantity'))}) as (data, _):
        with _callback(data, boundary()):
            single = get_price('A.SH', count=2, fields=['price', 'quantity'])
            multiple = get_price(['A.SH'], count=2, fields=['price', 'quantity'])
            empty = get_price([], count=2, fields=['price', 'quantity'])
            assert single.equals(multiple) and list(empty.columns) == list(single.columns)
            assert str(empty['time_ns'].dtype) == str(single['time_ns'].dtype) == 'int64'
            assert single['time_ns'].tolist() == [BASE-18, BASE-17]
            assert single['price'].tolist() == [Decimal(PRICE)]*2
            assert all(type(v) is int for v in single['quantity'])
            assert single.attrs['qf']['actual_scope']['end_ns'] == str(BASE-17)
            assert single.attrs['qf']['requested_scope']['end_ns'] == str(BASE)
            assert not empty.attrs['qf']['actual_scope']['securities']
            assert get_price('A.SH', count=10, fields=['price']).attrs['qf']['short_window'] == {'A.SH': 4}
            example = runpy.run_path(str(Path(__file__).resolve().parents[2]/'sdk/python/examples/research.py'))
            originals, stats = example['price_research'](['A.SH'], count=2, field='price')
            assert originals['price'].tolist() == [Decimal(PRICE)]*2
            assert stats['A.SH']['status'] == 'available' and stats['A.SH']['above_exact_mean'] is False
            assert type(stats['A.SH']['sma_float']) is float


def test_same_nanosecond_unpublished_ticks_cannot_evict_count_or_leak(formal):
    spec = contract('d05_same_ns'); formal.register(spec)
    samples = rows(4)
    for i, r in enumerate(samples):
        r['time_ns'] = BASE
        r['price'] = str(10+i)
    put(formal, spec, samples)
    g = gateway(formal, [binding(spec)])
    with session(g, market_routes={'tick': _MarketRoute('market', ('price', 'quantity'))}) as (data, _):
        with _callback(data, boundary(sequence=2)) as callback:
            frame = get_price('A.SH', count=1, fields=['price', 'sequence'])
            assert frame['price'].tolist() == [Decimal('11')] and frame['sequence'].tolist() == [2]
            quote = get_current_data()['A.SH']
            assert quote.last_price == Decimal('11') and quote.price_time_ns == BASE
            assert quote.sequence == 2
        with pytest.raises(ContractError, match='过期'):
            callback.view.current_json('["A.SH"]')
        with _callback(data, boundary(sequence=3)):
            assert get_price('A.SH', count=1, fields=['price'])['price'].tolist() == [Decimal('12')]
        with _callback(data, boundary()):
            assert get_price('A.SH', count=1, fields=['price']).empty


def test_configured_cross_section_window_separates_total_budget_from_arrow_batch():
    # Bounded window smoke only, not the 12.6M-row end-to-end D18 benchmark.
    securities = [f'A{i:04d}.SH' for i in range(5000)]
    view = _native.ReadView(json.dumps(boundary(sequence=105000, security=securities[-1])),
                            105000, 1024*1024*1024)
    template = rows(1)[0]
    for offset in range(0, 100000, 10000):
        samples = [dict(template, security=securities[i//20], time_ns=BASE-20+i%20,
                        sequence=i+1, stable_input_sequence=i+1) for i in range(offset, offset+10000)]
        batch = DataBatch(pa.Table.from_pylist(samples, schema=MARKET_SCHEMA),
                         dict(schema_id='qf.market.v1', rows=len(samples)))
        view.push_market(json.dumps(batch.metadata), batch.ipc(64*1024*1024))
    samples = [dict(template, security=s, time_ns=BASE, sequence=100001+i,
                    stable_input_sequence=100001+i) for i, s in enumerate(securities)]
    batch = DataBatch(pa.Table.from_pylist(samples, schema=MARKET_SCHEMA),
                     dict(schema_id='qf.market.v1', rows=len(samples)))
    view.push_market(json.dumps(batch.metadata), batch.ipc(64*1024*1024))
    request = dict(securities=securities, fields=['price'], frequency='tick', start_ns=None,
                   end_ns=str(BASE), count_per_security=20, adjustment='none')
    result = json.loads(view.prices_json(json.dumps(request), 'tick'))
    assert len(result) == 100000
    assert result[0]['time_ns'] == str(BASE-19) and result[19]['time_ns'] == str(BASE)
    assert result[0]['price'] == PRICE and result[-1]['security'] == securities[-1]
    resources = json.loads(view.resources_json())
    assert resources['working_set_bytes'] <= resources['max_bytes']
    view.reset()
    assert json.loads(view.resources_json()) == dict(working_set_bytes=0, max_bytes=1024*1024*1024,
        price_rows=0, research_rows=0, price_capacity=0, research_capacity=0)
    view.expire()
    oversized = [dict(template, sequence=i+1, stable_input_sequence=i+1) for i in range(10001)]
    batch = DataBatch(pa.Table.from_pylist(oversized, schema=MARKET_SCHEMA),
                     dict(schema_id='qf.market.v1', rows=len(oversized)))
    view = _native.ReadView(json.dumps(boundary()), 105000, 1024*1024*1024)
    with pytest.raises(ContractError) as error:
        view.push_market(json.dumps(batch.metadata), batch.ipc(64*1024*1024))
    assert error.value.code == 'RESOURCE_LIMIT'
    view.expire()


def test_native_prefetch_filter_and_view_expiry_are_cross_language(formal):
    samples = market_data(count=3)
    samples[2]['time_ns'] = BASE+10
    table = pa.Table.from_pylist(samples, schema=MARKET_SCHEMA)
    batch = DataBatch(table, dict(schema_id='qf.market.v1', rows=3))
    view = _native.ReadView(json.dumps(boundary()))
    view.push_market(json.dumps(batch.metadata), batch.ipc(1024*1024))
    request = dict(securities=['A.SH'], fields=['price'], frequency='tick', start_ns=None,
                   end_ns=str(BASE), count_per_security=10, adjustment='none')
    result = json.loads(view.prices_json(json.dumps(request), 'tick'))
    assert [int(r['time_ns']) for r in result] == [BASE-20, BASE-19]
    view.expire()
    for action in (view.reset, lambda: view.push_market(json.dumps(batch.metadata), batch.ipc(1024*1024))):
        with pytest.raises(ContractError):
            action()


def test_price_parameters_time_like_and_no_tick_close_default(formal):
    spec = contract('d05_params'); formal.register(spec); put(formal, spec, market_data())
    with session(gateway(formal, [binding(spec)]), market_routes={'tick': _MarketRoute('market', ('price', 'quantity'))}) as (data, _):
        with _callback(data, boundary()):
            for end in (BASE+1, '2026-01-01T00:00:00.000000124Z'):
                with pytest.raises(ContractError) as error:
                    get_price('A.SH', count=1, end=end, fields=['price'])
                assert error.value.code == 'LOOKAHEAD_FORBIDDEN'
            iso = '2026-01-01T00:00:00.000000123Z'
            assert get_price('A.SH', count=1, end=iso, fields=['price']).attrs['qf']['requested_scope']['end_ns'] == str(BASE)
            for kwargs in ({}, {'count': True}, {'start': BASE-10, 'count': 1}, {'count': 1, 'end': datetime(2026, 1, 1)},
                           {'count': 1, 'end': True}, {'count': 1, 'fields': ['close']},
                           {'count': 1, 'end': '2026/01/01 00:00:00+00:00'}):
                with pytest.raises(ContractError):
                    get_price('A.SH', **kwargs)
            with pytest.raises(ContractError) as error:
                get_price('A.SH', count=1, fields=['price'], end='2026-01-01T00:00:00.0000001234Z')
            assert error.value.code == 'NUMERIC_RANGE_UNSUPPORTED'


def test_cross_section_one_bulk_storage_scan_and_bounded_cache(formal):
    securities = tuple(f'A{i:04d}.SH' for i in range(200))
    spec = contract('d05_bulk'); formal.register(spec); put(formal, spec, market_data(securities, count=2))
    g = gateway(formal, [binding(spec)], securities)
    with session(g, market_routes={'tick': _MarketRoute('market', ('price', 'quantity'))}) as (data, _):
        with _callback(data, boundary()):
            frame = get_price(securities, count=1, fields=['price'])
            assert len(frame) == 200 and set(frame['security']) == set(securities)
            assert g.stats['storage_reads'] <= 2
            reads = g.stats['storage_reads']
            frame.iloc[0, frame.columns.get_loc('price')] = Decimal('999')
            assert get_price(securities, count=1, fields=['price']).iloc[0]['price'] == Decimal(PRICE)
            assert g.stats['storage_reads'] == reads
            with pytest.raises(ContractError) as error:
                get_price(securities, count=1000, fields=['price'])
            assert error.value.code == 'RESOURCE_LIMIT'


def test_financial_report_period_is_not_public_time_and_high_decimal_is_preserved(formal):
    spec = research_spec('fundamentals'); formal.register(spec)
    put(formal, spec, [fact(), fact(record='future', period='2026-03-31', value='999', published=BASE+20, effective=BASE-100)])
    g = gateway(formal, [research_binding(spec, 'fundamentals')])
    with session(g, research_routes={'fundamentals': 'fundamentals'}) as (data, _):
        with _callback(data, boundary()):
            frame = get_fundamentals(['A.SH'], fields=['revenue'])
            assert frame['report_period'].tolist() == ['2025-12-31']
            assert frame.iloc[0]['revenue'] == Decimal(fact()['value'])
            assert frame.iloc[0]['time_ns'] == BASE-10
            reads = g.stats['storage_reads']
            assert get_fundamentals(['A.SH'], fields=['revenue']).equals(frame)
            assert g.stats['storage_reads'] == reads
            assert list(get_fundamentals([], fields=['revenue']).columns) == list(frame.columns)
            assert get_fundamentals(['A.SH'], fields=['revenue'], as_of=BASE-11).empty
            with pytest.raises(ContractError) as e:
                get_fundamentals(['A.SH'], fields=['revenue'], as_of=BASE+1)
            assert e.value.code == 'LOOKAHEAD_FORBIDDEN'


def test_research_structured_filters_sort_limit_bulk_and_dynamic_dependency(formal):
    market = contract('d05_dynamic'); formal.register(market); put(formal, market, market_data())
    spec = research_spec('fundamentals'); formal.register(spec)
    put(formal, spec, [fact(s, value=v) for s, v in [('A.SH', '10'), ('B.SH', '2'), ('C.SH', '10')]])
    g = gateway(formal, [binding(market), research_binding(spec, 'fundamentals')], ('A.SH', 'B.SH', 'C.SH'))
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))}, research_routes={'fundamentals': 'fundamentals'}) as (data, context):
        with _callback(data, boundary()):
            get_price('A.SH', count=1, fields=['price'])
            assert set(context.dependencies) == {'market'}
            frame = get_fundamentals(['C.SH', 'B.SH', 'A.SH'], fields=['revenue'], filters=[Filter('revenue', 'gt', Decimal('2'))],
                                     order_by=['-revenue'], limit=1)
            assert frame['security'].tolist() == ['A.SH']
            assert set(context.dependencies) == {'market', 'fundamentals'}
            assert get_fundamentals(['B.SH'], fields=['revenue'], filters=[Filter('revenue', 'in', ['2', '3'])]).shape[0] == 1
            for kwargs in ({'fields': ['SQL']}, {'fields': ['revenue'], 'filters': ['revenue > 0']},
                           {'fields': ['revenue'], 'order_by': ['eval(x)']}, {'fields': ['revenue'], 'limit': True}):
                with pytest.raises(ContractError):
                    get_fundamentals(['A.SH'], **kwargs)


def test_valuation_industry_identity_validity_and_historical_members(formal):
    kinds = ('valuation', 'industry', 'instruments', 'index_stocks')
    bindings = []
    for kind in kinds:
        spec = research_spec(kind); formal.register(spec); bindings.append(research_binding(spec, kind))
        if kind == 'valuation':
            payload = [fact(field='pe_ratio', value='12.1234567890123456789')]
        elif kind == 'industry':
            payload = [fact(field=f, value=v, kind='text', unit='synthetic_text') for f, v in
                       [('industry_code', 'I1'), ('industry_name', '旧行业'), ('taxonomy', 'synthetic')]]
        elif kind == 'instruments':
            payload = [fact(field='name', value='历史名称', kind='text', unit='synthetic_text', until=BASE+1, entity='OLD')]
            payload += [fact(field='name', value='新代码身份', kind='text', unit='synthetic_text', effective=BASE+1,
                             published=BASE+1, record='new', entity='NEW')]
        else:
            payload = [fact(security='I.SH', field='member', value='A.SH', kind='text', unit='synthetic_text', until=BASE+1),
                fact(security='I.SH', field='member', value='B.SH', kind='text', unit='synthetic_text', effective=BASE+1,
                     published=BASE+1, record='new')]
        put(formal, spec, payload)
    g = gateway(formal, bindings, reference_indices=('I.SH',))
    with session(g, research_routes={kind: kind for kind in kinds}) as (data, _):
        with _callback(data, boundary()):
            assert get_valuation(['A.SH'], fields=['pe_ratio']).iloc[0]['pe_ratio'] == Decimal('12.1234567890123456789')
            assert get_industry(['A.SH']).iloc[0]['industry_name'] == '旧行业'
            assert get_instruments().iloc[0]['name'] == '历史名称'
            assert get_index_stocks('I.SH') == ('A.SH',)
        with _callback(data, boundary(BASE+1)):
            assert get_instruments().iloc[0]['name'] == '新代码身份'
            assert get_index_stocks('I.SH') == ('B.SH',)


def test_formal_unknown_capabilities_stay_refused_without_native_source(formal):
    bindings = current_research_bindings()
    g = gateway(formal, bindings, reference_indices=('I.SH',))
    cases = [('fundamentals', 'research_E14', lambda: get_fundamentals(['A.SH'], fields=['revenue'])),
             ('valuation', 'research_E39', lambda: get_valuation(['A.SH'], fields=['pe_ratio'])),
             ('index_stocks', 'research_E57', lambda: get_index_stocks('I.SH')),
             ('industry', 'research_E56', lambda: get_industry(['A.SH'])),
             ('instruments', 'research_E40', lambda: get_instruments()),
             ('price', 'research_E52', lambda: get_price('A.SH', count=1, frequency='1d'))]
    for kind, name, action in cases:
        with session(g, research_routes={kind: name}) as (data, _):
            with _callback(data, boundary()):
                with pytest.raises(ContractError) as e:
                    action()
                assert e.value.code == 'CAPABILITY_UNAVAILABLE' and e.value.scope['capability_gap']
        g.context = None
    assert g.stats['storage_reads'] == 0


def test_research_quality_change_cannot_be_hidden_by_cache_or_new_dependency(formal):
    market = contract('d05_changed'); formal.register(market); put(formal, market, market_data())
    spec = research_spec('fundamentals'); formal.register(spec); put(formal, spec, [fact()])
    g = gateway(formal, [binding(market), research_binding(spec, 'fundamentals')])
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))}, research_routes={'fundamentals': 'fundamentals'}) as (data, _):
        with _callback(data, boundary()):
            get_price('A.SH', count=1, fields=['price'])
            quality(formal, market)
            with pytest.raises(ContractError) as e:
                get_fundamentals(['A.SH'], fields=['revenue'])
            assert e.value.code == 'DATA_CHANGED'


def test_known_halt_missing_quote_and_stale_time_are_not_conflated(formal):
    spec = contract('d05_quote'); formal.register(spec); put(formal, spec, market_data())
    status = research_spec('status'); formal.register(status)
    put(formal, status, [fact(field='halted', value='true', kind='boolean', unit='known_status')])
    g = gateway(formal, [binding(spec), research_binding(status, 'status')], ('A.SH', 'B.SH'))
    with session(g, market_routes={'tick': _MarketRoute('market', ('price', 'quantity'))}, research_routes={'status': 'status'}) as (data, _):
        with _callback(data, boundary()):
            quotes = get_current_data()
            assert quotes['A.SH'].halted is True and quotes['A.SH'].is_stale and quotes['A.SH'].price_time_ns == BASE-17
            assert quotes['B.SH'].halted is None and quotes['B.SH'].last_price is None
            with pytest.raises(TypeError):
                quotes['A.SH'] = quotes['B.SH']
            with pytest.raises(FrozenInstanceError):
                quotes['A.SH'].last_price = Decimal('0')


def test_quote_bid_ask_quantities_and_unknown_last_are_preserved(formal):
    spec = contract('d05_both_sides'); formal.register(spec)
    payload = rows(1, kind='quote_tick'); put(formal, spec, payload)
    g = gateway(formal, [binding(spec)])
    route = _MarketRoute('market', ('bid', 'ask', 'bid_quantity', 'ask_quantity'))
    with session(g, market_routes={'tick': route}) as (data, _):
        with _callback(data, boundary(sequence=1)):
            quote = get_current_data()['A.SH']
            assert quote.bid_quantity == 3 and quote.ask_quantity == 5
            assert quote.last_price is None and quote.price_time_ns is None
            assert quote.bid == Decimal('10.01') and quote.ask == Decimal('10.02')
            assert not quote.is_stale


def test_d10_published_quote_seam_uses_rust_key_and_no_parent_ipc():
    from quantfoundry.data import Tick
    data = _DataSession(None, universe=('A.SH',), frequency='tick', market_routes={})
    key = boundary(sequence=1)['market_through']
    future = boundary(sequence=2)['market_through']
    quote = Tick('A.SH', BASE, None, None, False, None, 'A', 1, 'quote', Decimal('10'), Decimal('11'), None, 3, 5)
    with _callback(data, boundary(sequence=1), published_quotes={'A.SH': (quote, key)}) as active:
        result = get_current_data()['A.SH']
        assert result.bid_quantity == 3 and result.ask_quantity == 5 and not result.is_stale
        assert result.price_time_ns is None and result.last_price is None
    with pytest.raises(ContractError):
        active.view.visible_key_json(json.dumps(key))
    with _callback(data, boundary(sequence=1), published_quotes={'A.SH': (quote, future)}):
        assert get_current_data()['A.SH'].last_price is None
        assert getattr(get_current_data()['A.SH'], 'bid', None) is None
    with _callback(data, boundary(BASE+1), published_quotes={'A.SH': (quote, key)}):
        assert get_current_data()['A.SH'].is_stale
    with _callback(data, boundary(sequence=1), published_quotes={'A.SH': (replace(quote,sequence=2), key)}):
        with pytest.raises(ContractError) as error:
            get_current_data()
        assert error.value.code == 'INVALID_CONTRACT'


def test_ambiguous_historical_identity_and_missing_factor_facts_refuse(formal):
    spec = research_spec('instruments'); formal.register(spec)
    put(formal, spec, [fact(field='name', value='A', kind='text', unit='synthetic_text', entity='ONE'),
                      fact(field='name', value='B', kind='text', unit='synthetic_text', entity='TWO', record='other')])
    g = gateway(formal, [research_binding(spec, 'instruments')])
    with session(g, research_routes={'instruments': 'instruments'}) as (data, _):
        with _callback(data, boundary()):
            with pytest.raises(ContractError) as e:
                get_instruments()
            assert e.value.code == 'CAPABILITY_UNAVAILABLE'
    spec = research_spec('adjustment'); formal.register(spec)
    put(formal, spec, [fact(field='factor', value='2', unit='verified_cumulative_factor')])
    binding_without_anchor = replace(research_binding(spec, 'adjustment'), accepted_facts=('verified_public_time', 'stable_identity', 'verified_effective_dates'))
    market = contract('d05_unanchored_raw'); formal.register(market); put(formal, market, market_data())
    g = gateway(formal, [binding(market), binding_without_anchor])
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))}, research_routes={'adjustment': 'adjustment'}) as (data, _):
        with _callback(data, boundary()):
            with pytest.raises(ContractError) as e:
                get_price('A.SH', count=1, fields=['price'], adjustment='pre')
            assert e.value.code == 'CAPABILITY_UNAVAILABLE', (e.value.message, e.value.operation)


def test_adjustment_uses_only_published_factor_and_anchor_before_sim_time(formal):
    market = contract('d05_adjusted'); formal.register(market)
    samples = market_data(count=2)
    for row in samples:
        row['price'] = '10'
    put(formal, market, samples)
    spec = research_spec('adjustment'); formal.register(spec)
    put(formal, spec, [fact(field='factor', value='1', published=BASE-100, effective=BASE-100, unit='verified_cumulative_factor'),
        fact(field='factor', value='2', published=BASE-10, effective=BASE-10, record='factor-2', unit='verified_cumulative_factor'),
        fact(field='factor', value='100', published=BASE+1, effective=BASE+1, record='future', unit='verified_cumulative_factor')])
    g = gateway(formal, [binding(market), research_binding(spec, 'adjustment')])
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))}, research_routes={'adjustment': 'adjustment'}) as (data, _):
        with _callback(data, boundary()):
            assert get_price('A.SH', count=2, fields=['price'], adjustment='pre')['price'].tolist() == [Decimal('5')]*2
            assert get_price('A.SH', count=2, fields=['price'], adjustment='post')['price'].tolist() == [Decimal('10')]*2
            assert get_price('A.SH', count=2, fields=['price'])['price'].tolist() == [Decimal('10')]*2


def calendar(now=BASE):
    minute = 60_000_000_000
    return [dict(session=dict(key='S', exchange_timezone='Asia/Shanghai', open_ns=str(now), close_ns=str(now+180*minute)),
        date='2026-01-01', local_midnight_ns=str(now-60*minute), before_open_ns=str(now-1), after_close_ns=str(now+180*minute),
        market_windows=[dict(start_ns=str(now), end_ns=str(now+70*minute)), dict(start_ns=str(now+120*minute), end_ns=str(now+180*minute))],
        bar_windows=[dict(start_ns=str(now), end_ns=str(now+70*minute)), dict(start_ns=str(now+120*minute), end_ns=str(now+180*minute))],
        week=dict(id='W', trading_day=1, total_trading_days=1), month=dict(id='2026-01', trading_day=1, total_trading_days=1))]


def test_minute_hour_no_lunch_join_partial_missing_and_no_upsampling(formal):
    spec = contract('d05_minute'); formal.register(spec)
    minute = 60_000_000_000
    samples = []
    for i in list(range(70))+list(range(120, 180)):
        r = market_data(count=1)[0]
        r.update(kind='bar', session='S', source_session='S', channel='1m', time_ns=BASE+(i+1)*minute,
                 interval_start_ns=BASE+i*minute, interval_end_ns=BASE+(i+1)*minute,
                 open='10', high='12', low='9', close='11', price=None, quantity=1, sequence=i+1, stable_input_sequence=i+1)
        samples.append(r)
    put(formal, spec, samples)
    b = replace(binding(spec), frequency='1m')
    g = gateway(formal, [b])
    route = _MarketRoute('market', ('open', 'high', 'low', 'close', 'quantity'))
    with session(g, market_routes={'1m': route}, frequency='1m', calendar=calendar()) as (data, _):
        with _callback(data, boundary(BASE+181*minute)):
            frame = get_price('A.SH', start=BASE, frequency='60m', fields=['close', 'quantity', 'is_partial', 'status'])
            assert frame['time_ns'].tolist() == [BASE+60*minute, BASE+70*minute, BASE+180*minute]
            assert frame['quantity'].tolist() == [60, 10, 60]
            assert frame['is_partial'].tolist() == [False, True, False]
            assert frame['status'].tolist() == ['observed']*3
            by_date = get_price('A.SH', start='2026-01-01', count=None, fields=['close'])
            assert len(by_date) == 130
        with _callback(data, boundary(BASE+30*minute)):
            assert get_price('A.SH', start=BASE, frequency='60m', fields=['close']).empty
        with _callback(data, boundary(BASE+60*minute)):
            assert get_price('A.SH', start=BASE, frequency='60m', fields=['close', 'status']).empty
        published = boundary(BASE+60*minute, sequence=60)
        published['market_through']['identity'].update(source_session='S', channel='1m')
        with _callback(data, published):
            assert get_price('A.SH', start=BASE, frequency='60m', fields=['close'])['close'].tolist() == [Decimal('11')]
    # A separately declared synthetic input contains a known hole. Upsert
    # would preserve old rows, so it cannot represent this independent oracle.
    spec = contract('d05_minute_hole'); formal.register(spec)
    put(formal, spec, [r for r in samples if r['sequence'] != 2])
    b = replace(binding(spec), frequency='1m')
    g = gateway(formal, [b])
    with session(g, market_routes={'1m': route}, frequency='1m', calendar=calendar()) as (data, _):
        with _callback(data, boundary(BASE+181*minute)):
            frame = get_price('A.SH', start=BASE, frequency='60m', fields=['close', 'status'])
            assert frame.iloc[0]['status'] == 'missing' and frame.iloc[0]['close'] is None
    g = gateway(formal, [replace(b, frequency='60m')])
    with session(g, market_routes={'60m': route}, frequency='60m') as (data, _):
        with _callback(data, boundary(BASE+181*minute)):
            with pytest.raises(ContractError) as e:
                get_price('A.SH', count=1, frequency='1m')
            assert e.value.code == 'CAPABILITY_UNAVAILABLE'


def test_research_index_points_do_not_enter_cny_share_market_schema(formal):
    spec = research_spec('price'); formal.register(spec)
    put(formal, spec, [fact(field='close', value='3000.123456789012345678901234567890', effective=BASE-100,
                           published=BASE-99, unit='index_points')])
    g = gateway(formal, [research_binding(spec, 'price')])
    with session(g, frequency='1d', research_routes={'price': 'price'}) as (data, _):
        with _callback(data, boundary()):
            frame = get_price('A.SH', count=1)
            assert frame.iloc[0]['close'] == Decimal('3000.123456789012345678901234567890')
            assert frame.attrs['qf']['units']['close'] == 'index_points'
            assert 'research_prices_are_not_matching_prices' in frame.attrs['qf']['limitations']


def test_cancel_and_resource_budget_are_terminal_for_actual_chain(formal):
    spec = contract('d05_cancel'); formal.register(spec); put(formal, spec, market_data())
    cancelled = threading.Event()
    g = gateway(formal, [binding(spec)], cancelled=cancelled.is_set)
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))}) as (data, _):
        with _callback(data, boundary()):
            cancelled.set()
            with pytest.raises(ContractError) as e:
                get_price('A.SH', count=1, fields=['price'])
            assert e.value.code == 'CANCELLED'
    g = gateway(formal, [binding(spec)])
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))}, max_rows=1) as (data, _):
        with _callback(data, boundary()):
            with pytest.raises(ContractError) as e:
                get_price('A.SH', start=BASE-100, fields=['price'])
            assert e.value.code == 'RESOURCE_LIMIT'


def test_currentstore_group_count_cursor_cannot_admit_extra_rows(formal):
    spec = contract('d05_group_cursor'); formal.register(spec); put(formal, spec, market_data(('A.SH', 'B.SH'), count=4))
    q = Query(columns=('security', 'time_ns'), page_size=1, descending=True,
              filters=(('security', 'in', ('A.SH', 'B.SH')),), per_group=('security', 2))
    collected = []
    while True:
        page = formal.read_arrow(spec, q)
        collected.extend(page.table.to_pylist())
        if not page.next_cursor:
            break
        q = replace(q, cursor=page.next_cursor)
    assert len(collected) == 4
    assert {s: sorted(r['time_ns'] for r in collected if r['security'] == s) for s in ('A.SH', 'B.SH')} == {
        'A.SH': [BASE-18, BASE-17], 'B.SH': [BASE-18, BASE-17]}


def test_historical_count_uses_complete_event_order_before_source_key_paging(formal):
    spec = contract('d05_complete_count'); formal.register(spec)
    samples = []
    for security in ('A.SH', 'B.SH'):
        samples += [dict(rows(1)[0], security=security, time_ns=BASE-1, channel='Z', price='11'),
                    dict(rows(2)[1], security=security, time_ns=BASE-1, channel='A', price='12')]
    put(formal, spec, samples)
    g = gateway(formal, [binding(spec)], ('A.SH', 'B.SH'))
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))}) as (data, _):
        with _callback(data, boundary()):
            for securities in (['A.SH'], ['A.SH', 'B.SH']):
                frame = get_price(securities, count=1, fields=['price', 'channel'])
                assert frame['price'].tolist() == [Decimal('11')]*len(securities)
                assert frame['channel'].tolist() == ['Z']*len(securities)
    q = Query(columns=('security', 'time_ns', 'channel'), page_size=1, descending=True,
              filters=(('security', 'in', ('A.SH', 'B.SH')),), per_group=('security', 1),
              group_order=('time_ns', 'source_session', 'channel', 'sequence', 'stable_input_sequence'))
    collected = []
    while True:
        page = formal.read_arrow(spec, q); collected.extend(page.table.to_pylist())
        if not page.next_cursor:
            break
        q = replace(q, cursor=page.next_cursor)
    assert len(collected) == 2 and {r['channel'] for r in collected} == {'Z'}


@pytest.mark.parametrize('kind', ['price', 'current', 'research'])
def test_same_query_cache_hit_rechecks_quality_without_content_generation_change(formal, kind):
    market = contract('d05_cached_quality'); formal.register(market); put(formal, market, market_data())
    fundamental = research_spec('fundamentals'); formal.register(fundamental); put(formal, fundamental, [fact()])
    g = gateway(formal, [binding(market), research_binding(fundamental, 'fundamentals')])
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))},
                 research_routes={'fundamentals': 'fundamentals'}) as (data, _):
        with _callback(data, boundary()):
            operation = {'price': lambda: get_price('A.SH', count=1, fields=['price']),
                         'current': get_current_data,
                         'research': lambda: get_fundamentals(['A.SH'], fields=['revenue'])}[kind]
            operation(); reads = g.stats['storage_reads']; operation()
            assert g.stats['storage_reads'] == reads and data.cache
            spec = fundamental if kind == 'research' else market
            generation = formal.catalog.dataset(spec.name)['generation']; quality(formal, spec)
            assert formal.catalog.dataset(spec.name)['generation'] == generation
            with pytest.raises(ContractError) as error:
                operation()
            assert error.value.code == 'DATA_CHANGED'


def test_explicit_original_anchor_is_independent_of_first_available_factor(formal):
    market = contract('d05_truncated_anchor'); formal.register(market)
    put(formal, market, [dict(r, price='10') for r in market_data(count=2)])
    spec = research_spec('adjustment'); formal.register(spec)
    put(formal, spec, [fact(field='factor', value='2', published=BASE-100, effective=BASE-100,
                           unit='verified_cumulative_factor')])
    base = research_binding(spec, 'adjustment')
    for anchor, expected in ((base.factor_anchor, Decimal('20')),
                             (dict(factor='4', effective_ns=str(BASE-1000), public_ns=str(BASE-1000)), Decimal('5'))):
        g = gateway(formal, [binding(market), replace(base, factor_anchor=anchor)])
        with session(g, market_routes={'tick': _MarketRoute('market', ('price',))},
                     research_routes={'adjustment': 'adjustment'}) as (data, _):
            with _callback(data, boundary()):
                assert get_price('A.SH', count=2, fields=['price'], adjustment='post')['price'].tolist() == [expected]*2
                assert get_price('A.SH', count=2, fields=['price'], adjustment='pre')['price'].tolist() == [Decimal('10')]*2
    for anchor in (None, dict(factor='1', effective_ns=str(BASE-1000), public_ns=str(BASE+1))):
        g = gateway(formal, [binding(market), replace(base, factor_anchor=anchor)])
        with session(g, market_routes={'tick': _MarketRoute('market', ('price',))},
                     research_routes={'adjustment': 'adjustment'}) as (data, _):
            with _callback(data, boundary()):
                with pytest.raises(ContractError) as error:
                    get_price('A.SH', count=2, fields=['price'], adjustment='post')
                assert error.value.code == 'CAPABILITY_UNAVAILABLE'


def test_pre_anchor_requires_a_factor_effective_at_the_requested_endpoint(formal):
    market = contract('d05_expired_factor'); formal.register(market)
    put(formal, market, [dict(r, price='10') for r in market_data(count=2)])
    spec = research_spec('adjustment'); formal.register(spec)
    put(formal, spec, [fact(field='factor', value='2', published=BASE-100, effective=BASE-100,
                           until=BASE-10, unit='verified_cumulative_factor')])
    g = gateway(formal, [binding(market), research_binding(spec, 'adjustment')])
    with session(g, market_routes={'tick': _MarketRoute('market', ('price',))},
                 research_routes={'adjustment': 'adjustment'}) as (data, _):
        with _callback(data, boundary()):
            with pytest.raises(ContractError) as error:
                get_price('A.SH', count=2, fields=['price'], adjustment='pre')
            assert error.value.code == 'CAPABILITY_UNAVAILABLE'
            assert get_price('A.SH', count=2, fields=['price'], adjustment='post')['price'].tolist() == [Decimal('20')]*2


def test_unknown_historical_members_are_not_a_verified_empty_set(formal):
    spec = research_spec('index_stocks'); formal.register(spec)
    put(formal, spec, [fact(security='I.SH', field='member', value=None, kind='text', unit='synthetic_text',
                           published=BASE-5, effective=BASE-5)])
    g = gateway(formal, [research_binding(spec, 'index_stocks')], reference_indices=('I.SH',))
    with session(g, research_routes={'index_stocks': 'index_stocks'}) as (data, _):
        with _callback(data, boundary()):
            assert get_index_stocks('I.SH') == ()
            with pytest.raises(ContractError) as error:
                get_index_stocks('I.SH', as_of=BASE-10)
            assert error.value.code == 'CAPABILITY_UNAVAILABLE'


def test_complete_membership_is_never_silently_truncated():
    view = _native.ReadView(json.dumps(boundary()), 20000, 128*1024*1024)
    for offset in (0, 10000):
        payload = [fact(security='I.SH', field='member', value=f'A{i:05d}.SH', kind='text', unit='synthetic_text',
                        record=f'member-{i:05d}') for i in range(offset, min(offset+10000, 10001))]
        batch = DataBatch(pa.Table.from_pylist(payload, schema=SCHEMA), dict(schema_id='qf.research.v1', rows=len(payload)))
        view.push_research(json.dumps(batch.metadata), batch.ipc(64*1024*1024))
    request = dict(kind='index_stocks', securities=['I.SH'], fields=['member'], as_of_ns=str(BASE), limit=10000)
    with pytest.raises(ContractError) as error:
        view.research_json(json.dumps(request))
    assert error.value.code == 'RESOURCE_LIMIT'
    view.expire()


def test_native_and_sdk_conversion_peak_fits_explicit_run_data_budget():
    result = subprocess.run([sys.executable, str(Path(__file__).parent/'fixtures'/'research_memory.py')],
                            check=True, capture_output=True, text=True, timeout=90,
                            env=dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1'))
    report = json.loads(result.stdout)
    assert report['points'] == 100000 and report['window_input_rows'] == 105000
    assert report['replayed_rows'] == 210000
    assert report['peak_rss_bytes'] < report['allocated_data_bytes']
    assert report['reserved_working_set_bytes'] <= report['view_bytes']
    assert report['reset_capacities'] == [0, 0]
    print(json.dumps(report, sort_keys=True))


def test_adjust_fine_bars_before_resampling_and_refuse_intrabar_factor_changes(formal):
    minute = 60_000_000_000
    market = contract('d05_factor_bucket'); formal.register(market)
    samples = [dict(rows(1)[0], kind='bar', session='S', source_session='S', channel='1m',
        time_ns=BASE+(i+1)*minute, interval_start_ns=BASE+i*minute, interval_end_ns=BASE+(i+1)*minute,
        open='10', high='10', low='10', close='10', price=None, quantity=1,
        sequence=i+1, stable_input_sequence=i+1) for i in range(5)]
    put(formal, market, samples)
    factor = research_spec('adjustment'); formal.register(factor)
    transition = BASE+3*minute
    put(formal, factor, [fact(field='factor', value='1', effective=BASE-100, published=BASE-100,
                             until=transition, unit='verified_cumulative_factor'),
        fact(field='factor', value='2', effective=transition, published=BASE-100, record='factor-2',
             unit='verified_cumulative_factor')])
    route = _MarketRoute('market', ('open', 'high', 'low', 'close', 'quantity'))
    g = gateway(formal, [replace(binding(market), frequency='1m'), research_binding(factor, 'adjustment')])
    with session(g, market_routes={'1m': route}, research_routes={'adjustment': 'adjustment'},
                 frequency='1m', calendar=calendar()) as (data, _):
        with _callback(data, boundary(BASE+6*minute)):
            frame = get_price('A.SH', start=BASE, frequency='5m', fields=['open', 'high', 'low', 'close'], adjustment='pre')
            assert frame.iloc[0][['open', 'high', 'low', 'close']].tolist() == [Decimal('5'), Decimal('10'), Decimal('5'), Decimal('10')]
    # A transition inside the available one-minute bar has no known OHLC path.
    changed = [fact(field='factor', value='1', effective=BASE-100, published=BASE-100,
                    until=transition-1, unit='verified_cumulative_factor'),
        fact(field='factor', value='2', effective=transition-1, published=BASE-100, record='factor-2',
             unit='verified_cumulative_factor')]
    put(formal, factor, changed, token='intrabar')
    g = gateway(formal, [replace(binding(market), frequency='1m'), research_binding(factor, 'adjustment')])
    with session(g, market_routes={'1m': route}, research_routes={'adjustment': 'adjustment'},
                 frequency='1m', calendar=calendar()) as (data, _):
        with _callback(data, boundary(BASE+6*minute)):
            with pytest.raises(ContractError) as error:
                get_price('A.SH', start=BASE, fields=['close'], adjustment='pre')
            assert error.value.code == 'CAPABILITY_UNAVAILABLE'


def test_ambiguous_same_public_time_financial_revision_is_not_arbitrarily_selected(formal):
    spec = research_spec('fundamentals'); formal.register(spec)
    put(formal, spec, [fact(value='10'), fact(record='same-time-revision', value='20')])
    g = gateway(formal, [research_binding(spec, 'fundamentals')])
    with session(g, research_routes={'fundamentals': 'fundamentals'}) as (data, _):
        with _callback(data, boundary()):
            with pytest.raises(ContractError) as error:
                get_fundamentals(['A.SH'], fields=['revenue'])
            assert error.value.code == 'CAPABILITY_UNAVAILABLE'
