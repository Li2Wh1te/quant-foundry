"""D11 default authenticated app + explicit isolated contract + real storage.

Only the existing private DB dependency and trusted test-server settings differ.
No market service/provider, successful query result or HTTP body is injected.
"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from sqlalchemy import event, text

pytest.importorskip('quantfoundry', reason='default adjusted route needs the installed shared native wheel')

from app.core.config import Settings
from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry
from app.market_service.bindings import ETF_DAILY, TEST_ETF_DAILY, TEST_ETF_FACTORS, TEST_VARIANT
from app.market_service.service import MarketReadScope
from tests.test_s2_market_read import market, HEADERS, database_schema  # noqa: F401
from tests.test_s2_market_series import identity, seed
from tests.test_s2_market_adjustments import seed_factors
from tests.test_data_store_kernel import database, limits, store  # noqa: F401
from tests.test_data_store_local_pipeline import Inputs, ready  # noqa: F401

START, END = date(2026, 1, 26), date(2026, 2, 6)


def configure(client, *, baseline='1', baseline_date=date(2025, 1, 1), declared=True):
    old = client.app.state.settings
    client.app.state.settings = Settings(**{**old.model_dump(),
        'market_test_adjustment_contract': 'cumulative-exact-date-fixture@1' if declared else None,
        'market_test_post_anchor_date': baseline_date if declared and baseline else None,
        'market_test_post_anchor_factor': baseline if declared else None})


def seed_contract(current, *, missing=(), start=START, end=END, factor_change='2026-01-28'):
    key = identity(current)
    prices = seed(current, start, end)
    isolated_prices = [replace(row, variant=TEST_VARIANT) for row in prices]
    assert run_entry(current, BY_ID['E70'], Inputs(*isolated_prices))['qualified']
    values = [(row.content['trade_date'], '1' if row.content['trade_date'] < factor_change else '2')
              for row in prices if row.content['trade_date'] not in missing]
    default_factors = seed_factors(current, [*values, ('2026-02-09', '100')])
    isolated_factors = [replace(row, variant=TEST_VARIANT) for row in default_factors]
    assert run_entry(current, BY_ID['E69'], Inputs(*isolated_factors))['qualified']
    return key, isolated_prices, isolated_factors


def get(client, key, **kwargs):
    args = dict(series_key=TEST_ETF_DAILY, period='day', adjustment='pre', start=START.isoformat(),
        end=END.isoformat(), as_of=END.isoformat(), limit=512)
    args.update(kwargs)
    return client.get('/api/admin/market/instruments/' + key + '/bars?' + urlencode(args), headers=HEADERS)


@pytest.mark.parametrize('mode, first_open', [('pre', '2.061728'), ('post', '4.123456')])
def test_default_http_adjusted_positive_keeps_production_e69_restricted(market, mode, first_open):
    client, current, _ = market
    key, prices, _factors = seed_contract(current)
    configure(client)
    forbidden = []
    def block(_conn, _cursor, statement, _params, _context, _many):
        if any(name in statement.lower() for name in ('etf_daily_bars', 'etf_adjustment_factors', 'backtesting_accounts')):
            forbidden.append(statement)
            raise AssertionError('legacy market/execution SQL')
    event.listen(current.catalog.engine, 'before_cursor_execute', block)
    try:
        response = get(client, key, adjustment=mode)
        options = client.get('/api/admin/market/instruments/' + key + '/series?as_of=2026-02-06', headers=HEADERS)
    finally:
        event.remove(current.catalog.engine, 'before_cursor_execute', block)
    assert response.status_code == options.status_code == 200, response.text
    result = response.json()
    assert Decimal(result['items'][0]['open']) == Decimal(first_open)
    assert result['items'][0]['volume'] == format(prices[0].content['vol'], 'f')
    assert result['basis']['adjustment'] == mode and '人工隔离' in result['basis']['note']
    assert result['units'] == {'price': '元', 'volume': '手', 'amount': '千元'}
    test = next(row for row in options.json() if row['series_key'] == TEST_ETF_DAILY)
    prod = next(row for row in options.json() if row['series_key'] == ETF_DAILY)
    assert mode in test['adjustments'] and test['adjustment_capabilities'][mode]['mapping_implemented']
    assert prod['adjustment_capabilities']['pre']['availability'] == 'restricted'
    assert prod['adjustment_capabilities']['pre']['mapping_implemented']
    assert get(client, key, series_key=ETF_DAILY, adjustment=mode).json()['detail']['code'] == 'FACTOR_ANCHOR_UNVERIFIED'
    assert not forbidden


@pytest.mark.parametrize('mode', ['pre', 'post'])
@pytest.mark.parametrize('period', ['day', 'week', 'month', 'year'])
def test_default_http_complete_buckets_pages_and_viewport_share_one_anchor(market, period, mode):
    client, current, _ = market
    start, end = (date(2025, 1, 1), date(2026, 1, 6)) if period == 'year' else (START, END)
    if period == 'year':
        # Compact synthetic monthly files; the default reader budgets stay unchanged.
        current.limits = replace(current.limits, file_rows=100_000)
    key, prices, _factors = seed_contract(current, start=start, end=end,
        factor_change='2026-01-01' if period == 'year' else '2026-01-28')
    configure(client)
    params = dict(period=period, adjustment=mode, start=start.isoformat(), end=end.isoformat(), as_of=end.isoformat())
    a = get(client, key, **params, limit=1)
    assert a.status_code == 200, a.text
    first = a.json()
    assert first['next_cursor']  # Every period exercises an actual earlier bucket.
    older = get(client, key, **params, limit=1, cursor=first['next_cursor']).json()
    expanded = get(client, key, **params, limit=2).json()
    assert older['items'] + first['items'] == expanded['items']
    assert older['basis'] == first['basis'] == expanded['basis']
    assert older['meta']['read_token'] == first['meta']['read_token']
    assert get(client, key, **params, limit=1, cursor=first['next_cursor']).json()['items'] == older['items']
    whole = get(client, key, **params, limit=512).json()
    assert Decimal(whole['items'][0]['open']) == prices[0].content['open'] / (2 if mode == 'pre' else 1)
    assert sum(Decimal(row['volume']) for row in whole['items']) == sum(row.content['vol'] for row in prices)
    assert first['basis']['anchor'] == ({'date': end.isoformat(), 'factor': '2'} if mode == 'pre'
        else {'date': '2025-01-01', 'factor': '1'})
    assert len({row['bucket_start'] for row in whole['items']}) == len(whole['items'])
    if period == 'year':
        assert whole['items'][0]['completion'] == 'complete'
        assert whole['items'][1]['completion'] == 'partial'


def test_pre_does_not_require_post_baseline_and_replay_uses_no_future_factor(market):
    client, current, _ = market
    key, prices, _factors = seed_contract(current)
    configure(client, baseline=None)
    assert get(client, key).status_code == 200
    assert get(client, key, adjustment='post').json()['detail']['code'] == 'FACTOR_ANCHOR_UNVERIFIED'
    option = next(row for row in client.get('/api/admin/market/instruments/' + key + '/series?as_of=2026-02-06', headers=HEADERS).json() if row['series_key'] == TEST_ETF_DAILY)
    assert option['adjustments'] == ['none', 'pre']
    early = get(client, key, end='2026-01-27', as_of='2026-01-27').json()
    assert early['basis']['anchor'] == {'date': '2026-01-27', 'factor': '1'}
    assert Decimal(early['items'][0]['open']) == prices[0].content['open']
    assert all(row['last_trade_date'] <= '2026-01-27' for row in early['items'])
    assert get(client, key).json()['basis']['anchor']['factor'] == '2'  # future 100 is excluded


def test_no_factor_forward_fill_or_implicit_raw_fallback(market):
    client, current, _ = market
    key, _prices, _factors = seed_contract(current, missing=('2026-01-28',))
    configure(client)
    assert get(client, key).json()['detail']['code'] == 'FACTOR_UNAVAILABLE'
    assert get(client, key, limit=1).status_code == 200  # unrelated older gap is not forward-filled or consumed
    assert get(client, key, adjustment='none').status_code == 200
    assert get(client, key, series_key='E51').json()['detail']['code'] == 'CAPABILITY_UNAVAILABLE'
    assert get(client, key, representation='other-source').status_code == 422


def test_factor_change_invalidates_adjusted_cursor_but_not_raw_cursor(market):
    client, current, _ = market
    key, _prices, factors = seed_contract(current)
    configure(client)
    adjusted = get(client, key, limit=1).json()
    raw = get(client, key, limit=1, adjustment='none').json()
    changed = replace(factors[0], content={**factors[0].content, 'adj_factor': Decimal('3')}, observed_at=factors[0].observed_at + timedelta(seconds=1))
    assert run_entry(current, BY_ID['E69'], Inputs(changed))['qualified']
    assert get(client, key, limit=1, cursor=adjusted['next_cursor']).json()['detail']['code'] == 'DATA_CHANGED'
    assert get(client, key, limit=1, adjustment='none', cursor=raw['next_cursor']).status_code == 200


@pytest.mark.parametrize('change', ['factor', 'new_price_after_factor', 'policy'])
def test_real_default_response_never_submits_a_changed_joint_window(market, monkeypatch, change):
    client, current, _ = market
    key, prices, factors = seed_contract(current)
    configure(client)
    done = False
    if change == 'new_price_after_factor':
        original = MarketReadScope.objects
        def interleave(scope, binding, page):
            nonlocal done
            rows = original(scope, binding, page)
            if binding == TEST_ETF_FACTORS and not done:
                done = True
                changed = replace(prices[0], content={**prices[0].content, 'open': Decimal('4.2')}, observed_at=prices[0].observed_at + timedelta(seconds=1))
                assert run_entry(current, BY_ID['E70'], Inputs(changed))['qualified']
            return rows
        monkeypatch.setattr(MarketReadScope, 'objects', interleave)
    else:
        original = MarketReadScope.response
        def interleave(scope, dto):
            nonlocal done
            result = original(scope, dto)
            if TEST_ETF_FACTORS in scope.keys and not done:
                done = True
                if change == 'factor':
                    changed = replace(factors[0], content={**factors[0].content, 'adj_factor': Decimal('3')}, observed_at=factors[0].observed_at + timedelta(seconds=1))
                    assert run_entry(current, BY_ID['E69'], Inputs(changed))['qualified']
                else:
                    configure(client, baseline=None)
            return result
        monkeypatch.setattr(MarketReadScope, 'response', interleave)
    response = get(client, key, adjustment='post' if change == 'policy' else 'pre')
    assert done and response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'DATA_CHANGED' and 'items' not in response.json()


def test_test_contract_revocation_and_distinct_representation_are_not_production_permissions(market):
    client, current, _ = market
    key, _prices, _factors = seed_contract(current)
    configure(client)
    assert get(client, key).status_code == 200
    configure(client, declared=False)
    assert get(client, key).json()['detail']['code'] == 'CAPABILITY_UNAVAILABLE'
    assert get(client, key, series_key=ETF_DAILY, adjustment='pre').json()['detail']['code'] == 'FACTOR_ANCHOR_UNVERIFIED'
    for environment in ('local', 'production'):
        with pytest.raises(ValueError):
            Settings(**{**client.app.state.settings.model_dump(), 'environment': environment,
                'market_test_adjustment_contract': 'cumulative-exact-date-fixture@1'})


def test_default_source_and_variant_cannot_supply_missing_test_factors(market):
    client, current, _ = market
    key = identity(current)
    prices = seed(current, START, END)
    assert run_entry(current, BY_ID['E70'], Inputs(*[replace(row, variant=TEST_VARIANT) for row in prices]))['qualified']
    seed_factors(current, [(row.content['trade_date'], '999') for row in prices])
    configure(client)
    assert get(client, key).json()['detail']['code'] in ('FACTOR_UNAVAILABLE', 'DATASET_MISSING')
    assert get(client, key, adjustment='none').status_code == 200


def test_bad_factor_quality_withdraws_adjusted_scope_and_raw_stays_readable(market):
    client, current, _ = market
    key, _prices, factors = seed_contract(current)
    configure(client)
    assert get(client, key).status_code == 200
    broken = replace(factors[0], content={**factors[0].content, 'adj_factor': Decimal('0')}, observed_at=factors[0].observed_at + timedelta(seconds=1))
    assert not run_entry(current, BY_ID['E69'], Inputs(broken))['qualified']
    denied = get(client, key)
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'DATA_RESTRICTED'
    assert get(client, key, adjustment='none').status_code == 200


def test_server_policy_baseline_change_clears_continuation_and_pre_remains_independent(market):
    client, current, _ = market
    key, _prices, _factors = seed_contract(current)
    configure(client)
    page = get(client, key, adjustment='post', limit=1).json()
    pre = get(client, key, limit=1).json()
    raw = get(client, key, adjustment='none', limit=1).json()
    configure(client, baseline=None)
    continued = get(client, key, adjustment='post', limit=1, cursor=page['next_cursor'])
    assert continued.status_code != 200 and continued.json()['detail']['code'] in ('DATA_CHANGED', 'INVALID_CURSOR', 'FACTOR_ANCHOR_UNVERIFIED')
    assert get(client, key).status_code == 200
    assert get(client, key, limit=1, cursor=pre['next_cursor']).status_code == 200
    assert get(client, key, adjustment='none', limit=1, cursor=raw['next_cursor']).status_code == 200


def test_pre_uses_proven_last_open_session_and_refuses_unknown_closed_days(market):
    client, current, _ = market
    key, _prices, _factors = seed_contract(current)
    configure(client, baseline=None)
    unknown = get(client, key, as_of='2026-02-08')
    assert unknown.status_code != 200 and unknown.json()['detail']['code'] == 'FACTOR_ANCHOR_UNVERIFIED'
    # Publish two explicit synthetic closed-calendar facts; no weekday shortcut in the reader.
    seed(current, START, END + timedelta(days=2))
    known = get(client, key, as_of='2026-02-08')
    assert known.status_code == 200, known.text
    assert known.json()['basis']['anchor'] == {'date': '2026-02-06', 'factor': '2'}


def test_default_http_joint_input_budget_is_enforced_after_both_sources(market, monkeypatch):
    client, current, _ = market
    key, _prices, _factors = seed_contract(current)
    configure(client)
    original, sizes = MarketReadScope.response, []
    def measure(scope, dto):
        result = original(scope, dto)
        if TEST_ETF_FACTORS in scope.keys:
            sizes.append(scope.bytes_read)
        return result
    monkeypatch.setattr(MarketReadScope, 'response', measure)
    assert get(client, key).status_code == 200
    from app.market_service import service
    monkeypatch.setattr(service, 'MAX_RESPONSE_BYTES', sizes[-1] - 1)
    denied = get(client, key)
    assert denied.status_code == 422 and denied.json()['detail']['code'] == 'QUERY_BUDGET_EXCEEDED'


def test_default_recent_512_adjusted_days_are_a_bounded_stream_not_daily_file_rescans(market):
    client, current, _ = market
    key = identity(current)
    # Realistic synthetic monthly files; reader's default scan/file limits stay unchanged.
    current.limits = replace(current.limits, file_rows=100_000)
    with current.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE instrument_code_mappings SET valid_from='2023-01-01', effective_range=daterange('2023-01-01',NULL,'[)')"))
    prices = seed(current, date(2023, 1, 1), END)
    assert run_entry(current, BY_ID['E70'], Inputs(*[replace(row, variant=TEST_VARIANT) for row in prices]))['qualified']
    factors = seed_factors(current, [(row.content['trade_date'], '2') for row in prices])
    assert run_entry(current, BY_ID['E69'], Inputs(*[replace(row, variant=TEST_VARIANT) for row in factors]))['qualified']
    configure(client, baseline=None)
    # Omit start rather than transmit a nullable string as a query date.
    response = client.get('/api/admin/market/instruments/' + key + '/bars?' + urlencode(dict(
        series_key=TEST_ETF_DAILY, end=END.isoformat(), as_of=END.isoformat(), adjustment='pre')), headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body['items']) == 512 and body['next_cursor']
    assert body['basis']['anchor'] == {'date': '2026-02-06', 'factor': '2'}
