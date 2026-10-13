"""D10: ordinary create_app -> real private PG/Parquet -> business pages.

Only synthetic formal inputs, existing identity facts and the supported test
storage probe. No successful provider/query response or supplier call injection.
"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from sqlalchemy import event, text

from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry
from app.market_service.series import aggregate_bucket, bars
from tests.test_s2_market_read import (market, mapping, publish, HEADERS, database_schema)  # noqa: F401
from tests.test_data_store_kernel import database, limits, store  # noqa: F401
from tests.test_data_store_local_pipeline import Inputs, ready  # noqa: F401
from tests.test_data_store_domain_samples import ts


def seed(current, start, end, *, missing=(), volume_missing=()):
    daily, calendars = [], []
    day = start
    index = 0
    while day <= end:
        opened = day.weekday() < 5  # Explicit synthetic calendar oracle ONLY.
        calendars.append(ts('E68', 'calendar', {'exchange': 'SSE', 'calendar_date': day.isoformat(), 'is_open': opened}))
        if opened and day not in missing:
            value = Decimal('4.123456') + Decimal(index) / 1000000
            daily.append(ts('E70', 'daily', {'source': 'tushare', 'ts_code': '510300.SH', 'trade_date': day.isoformat(),
                'open': value, 'high': value + 1, 'low': value - 1, 'close': value + Decimal('.1'),
                'vol': None if day in volume_missing else Decimal('9007199254740993.1234'), 'amount': Decimal('4100.1234')}))
            index += 1
        day += timedelta(days=1)
    for entry, rows in (('E70', daily), ('E68', calendars)):
        result = run_entry(current, BY_ID[entry], Inputs(*rows))
        assert result['qualified'], result
    return daily


def get(client, key, **params):
    return client.get('/api/admin/market/instruments/' + key + '/bars?' + urlencode(params), headers=HEADERS)


def identity(current):
    publish(current)
    iid = mapping(current)
    # Synthetic admitted lifespan starts before both calendar-year partitions.
    with current.catalog.engine.begin() as c:
        c.execute(text("UPDATE instrument_code_mappings SET valid_from='2025-01-01', effective_range=daterange('2025-01-01',NULL,'[)')"))
    return 'instrument:' + str(iid)


def test_default_app_series_and_raw_without_factors_or_execution(market):
    client, current, _ = market
    key = identity(current)
    seed(current, date(2026, 1, 26), date(2026, 2, 6), volume_missing=(date(2026, 2, 6),))
    forbidden = []
    def check(_conn, _cursor, statement, _params, _ctx, _many):
        if any(name in statement.lower() for name in ('etf_codes', 'etf_daily_bars', 'etf_adjustment_factors', 'backtesting_accounts', 'data_source_configs')):
            forbidden.append(statement)
            raise AssertionError('legacy or execution table reached by raw')
    event.listen(current.catalog.engine, 'before_cursor_execute', check)
    try:
        resolved = client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS)
        options = client.get('/api/admin/market/instruments/' + key + '/series', headers=HEADERS)
        raw = get(client, key, end='2026-02-06')
    finally:
        event.remove(current.catalog.engine, 'before_cursor_execute', check)
    assert resolved.status_code == options.status_code == raw.status_code == 200
    assert not forbidden
    option = options.json()[0]
    assert option['price_basis'] == 'unadjusted' and option['adjustments'] == ['none']
    assert option['periods'] == ['day', 'week', 'month', 'year']
    page = raw.json()
    assert len(page['items']) == 10
    assert page['items'][0]['open'] == '4.123456'
    assert page['items'][0]['volume'] == '9007199254740993.1234'
    assert page['items'][-1]['volume'] is None
    assert page['basis']['adjustment'] == 'none' and page['basis']['anchor'] is None
    assert page['meta']['coverage']['missing_dates'] is None
    assert page['units'] == {'price': '元', 'volume': '手', 'amount': '千元'}
    assert not any(internal in raw.text for internal in ('f0_', 'member_key', 'basis_token'))
    assert get(client, key, end='2026-02-06', adjustment='pre').json()['detail']['code'] == 'FACTOR_ANCHOR_UNVERIFIED'


@pytest.mark.parametrize('period, expected_count', [('week', 6), ('month', 2), ('year', 1)])
def test_full_buckets_across_partitions_and_three_output_pages(market, period, expected_count):
    client, current, _ = market
    key = identity(current)
    rows = seed(current, date(2026, 1, 1), date(2026, 2, 8))
    pages = []
    cursor = None
    for _ in range(10):
        args = dict(start='2026-01-01', end='2026-02-08', as_of='2026-02-08', period=period, limit=1)
        if cursor:
            args['cursor'] = cursor
        response = get(client, key, **args)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body)
        cursor = body['next_cursor']
        if not cursor:
            break
    actual = sorted([item for page in pages for item in page['items']], key=lambda item: item['bucket_start'])
    assert len(actual) == expected_count
    assert len({item['bucket_start'] for item in actual}) == expected_count
    assert actual[0]['open'] == str(rows[0].content['open'])
    assert actual[-1]['close'] == str(rows[-1].content['close'])
    assert sum(Decimal(item['volume']) for item in actual) == sum(Decimal(row.content['vol']) for row in rows)
    assert all(page['meta']['read_token'] == pages[0]['meta']['read_token'] for page in pages)
    if period == 'week':
        cross = next(item for item in actual if item['bucket_start'] == '2026-01-26')
        assert cross['completion'] == 'complete'
        assert cross['first_trade_date'] == '2026-01-26' and cross['last_trade_date'] == '2026-01-30'


def test_partial_replay_known_missing_and_calendar_unknown_are_distinct(market):
    client, current, _ = market
    key = identity(current)
    seed(current, date(2026, 1, 26), date(2026, 2, 8), missing=(date(2026, 1, 28),))
    page = get(client, key, start='2026-01-26', end='2026-02-08', period='week').json()
    assert page['items'][0]['completion'] == 'partial'
    assert page['items'][0]['volume'] is None and page['items'][0]['amount'] is None
    assert page['meta']['coverage']['missing_dates'] == ['2026-01-28']
    mid = get(client, key, start='2026-01-26', end='2026-02-04', as_of='2026-02-04', period='month').json()
    assert all(item['completion'] == 'partial' for item in mid['items'])
    assert all(item['last_trade_date'] <= '2026-02-04' for item in mid['items'])
    assert get(client, key, end='2026-02-05', as_of='2026-02-04').status_code == 422
    # Calendar exists but lacks this entire requested week: observed != complete.
    more = ts('E70', 'daily', {**ts('E70', 'daily', {}).content, 'source': 'tushare', 'ts_code': '510300.SH',
        'trade_date': '2026-02-09', 'open': Decimal('5'), 'high': Decimal('6'), 'low': Decimal('4'), 'close': Decimal('5')})
    assert run_entry(current, BY_ID['E70'], Inputs(more))['qualified']
    unknown = get(client, key, start='2026-02-09', end='2026-02-15', period='week').json()
    assert unknown['items'][0]['completion'] == 'unknown'
    assert unknown['meta']['coverage']['missing_dates'] is None


def test_cursor_repeat_fixed_default_window_tampering_and_quality_change(market):
    client, current, _ = market
    key = identity(current)
    seed(current, date(2026, 1, 26), date(2026, 2, 8))
    first = get(client, key, limit=3).json()
    token = first['next_cursor']
    args = dict(limit=3, cursor=token)
    a, b = get(client, key, **args), get(client, key, **args)
    assert a.status_code == b.status_code == 200
    assert a.json()['items'] == b.json()['items'] and a.json()['window'] == first['window']
    assert get(client, key, limit=4, cursor=token).json()['detail']['code'] == 'INVALID_CURSOR'
    assert get(client, key, limit=3, cursor=token[:-2] + 'AA').status_code == 422
    # Mutation without generation change is still covered by D08 final/cursor gate.
    with current.catalog.engine.begin() as c:
        c.execute(text("UPDATE instrument_code_mappings SET evidence='new synthetic checked evidence'"))
    changed = get(client, key, **args)
    assert changed.status_code == 409 and changed.json()['detail']['code'] == 'DATA_CHANGED'


def test_bad_price_read_refusal_no_calendar_and_budget_are_not_empty_success(market):
    client, current, service = market
    key = identity(current)
    publish(current, 'E70')
    assert get(client, key, end='2026-01-02').status_code == 200
    assert get(client, key, end='2026-01-02', period='week').status_code != 200
    # Existing pipeline quarantines bad official-shaped prices, no silent drop.
    broken = ts('E70', '510300.SH', {'source': 'tushare', 'ts_code': '510300.SH', 'trade_date': '2026-01-02',
        'open': Decimal('-1'), 'high': Decimal('4'), 'low': Decimal('3'), 'close': Decimal('3.5')})
    broken = replace(broken, observed_at=broken.observed_at + timedelta(seconds=1))
    assert not run_entry(current, BY_ID['E70'], Inputs(broken))['qualified']
    denied = get(client, key, end='2026-01-02')
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'DATA_RESTRICTED'


def test_exact_pure_year_sum_ohlc_and_null_never_zero():
    values = [dict(trade_date=date(2025, 12, 31), open='1.000001', high='3.000001', low='0.000001', close='2.000001',
                   volume_lots='99999999999999999999.9999', turnover_thousand_yuan=None),
              dict(trade_date=date(2026, 1, 1), open='2.000001', high='4.000001', low='1.000001', close='3.000001',
                   volume_lots=None, turnover_thousand_yuan='1')]
    for item in values:
        bar, missing = aggregate_bucket([item], 'day', start=item['trade_date'], end=item['trade_date'], sessions=None)
        assert bar.open == item['open'] and bar.volume == item['volume_lots'] and missing is None
    with pytest.raises(Exception):
        aggregate_bucket([{**values[0], 'high': '0.1'}], 'day', start=date(2025, 12, 31), end=date(2025, 12, 31), sessions=None)


def test_missing_entire_week_does_not_vanish_and_cumulative_input_is_charged(market, monkeypatch):
    client, current, _ = market
    key = identity(current)
    missing = tuple(date(2026, 2, 2) + timedelta(days=i) for i in range(5))
    seed(current, date(2026, 1, 26), date(2026, 2, 15), missing=missing)
    page = get(client, key, start='2026-01-26', end='2026-02-15', period='week').json()
    assert page['meta']['coverage']['missing_dates'] == [day.isoformat() for day in missing]
    assert any(notice['code'] == 'MISSING_DATES' for notice in page['meta']['limitations'])
    from app.market_service import service as shared
    monkeypatch.setattr(shared, 'MAX_RESPONSE_BYTES', 3000)
    denied = get(client, key, start='2026-01-26', end='2026-02-15')
    assert denied.status_code == 422 and denied.json()['detail']['code'] == 'QUERY_BUDGET_EXCEEDED'


def test_years_cross_partitions_keep_exact_extrema_and_no_half_year_pages(market):
    client, current, _ = market
    key = identity(current)
    rows = seed(current, date(2025, 12, 1), date(2026, 2, 28))
    a = get(client, key, start='2025-12-01', end='2026-02-28', period='year', limit=1).json()
    b = get(client, key, start='2025-12-01', end='2026-02-28', period='year', limit=1, cursor=a['next_cursor']).json()
    assert a['items'][0]['bucket_start'] == '2026-01-01' and b['items'][0]['bucket_start'] == '2025-01-01'
    assert b['items'][0]['open'] == format(rows[0].content['open'], 'f')
    assert Decimal(a['items'][0]['close']) == rows[-1].content['close']
    assert Decimal(a['items'][0]['high']) == max(row.content['high'] for row in rows if row.content['trade_date'] >= '2026-01-01')
    assert all(page['items'][0]['completion'] == 'partial' for page in (a, b))
    assert b['next_cursor'] is None
