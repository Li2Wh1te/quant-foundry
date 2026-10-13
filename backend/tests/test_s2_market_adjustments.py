"""D11 ordinary app -> real isolated formal factor inputs, no success mocks."""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from sqlalchemy import event

from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry
from tests.test_s2_market_read import market, publish, HEADERS, database_schema  # noqa: F401
from tests.test_s2_market_series import identity, seed
from tests.test_data_store_kernel import database, limits, store  # noqa: F401
from tests.test_data_store_local_pipeline import Inputs, ready  # noqa: F401
from tests.test_data_store_domain_samples import ts


def seed_factors(current, values):
    rows = [ts('E69', '510300.SH', dict(ts_code='510300.SH', trade_date=day, adj_factor=Decimal(value)))
            for day, value in values]
    result = run_entry(current, BY_ID['E69'], Inputs(*rows))
    assert result['qualified'], result
    return rows


def get(client, key, **kwargs):
    return client.get('/api/admin/market/instruments/' + key + '/factors?' + urlencode(
        dict(start='2026-01-01', end='2026-02-06', **kwargs)), headers=HEADERS)


def test_default_factor_route_exact_pages_asof_and_no_collection_or_execution(market):
    client, current, _ = market
    key = identity(current)
    values = [('2026-01-02', '1.000000000001'), ('2026-01-30', '2'), ('2026-02-02', '3'),
              ('2026-02-06', '4.123456789123'), ('2026-02-09', '999')]
    seed_factors(current, values)
    forbidden = []
    def check(_conn, _cursor, statement, _params, _context, _many):
        if any(name in statement.lower() for name in ('etf_adjustment_factors', 'etf_daily_bars', 'backtesting_accounts')):
            forbidden.append(statement)
            raise AssertionError('legacy/execution read')
    event.listen(current.catalog.engine, 'before_cursor_execute', check)
    try:
        a = get(client, key, limit=2, as_of='2026-02-06')
        assert a.status_code == 200, a.text
        first = a.json()
        b = get(client, key, limit=2, as_of='2026-02-06', cursor=first['next_cursor'])
    finally:
        event.remove(current.catalog.engine, 'before_cursor_execute', check)
    assert not forbidden and b.status_code == 200
    second = b.json()
    assert first['items'] == [{'trade_date': day, 'factor': factor} for day, factor in values[2:4]]
    assert second['items'] == [{'trade_date': day, 'factor': factor} for day, factor in values[:2]]
    assert first['meta']['read_token'] == second['meta']['read_token']
    assert second['next_cursor'] is None and first['basis']['anchor'] is None
    assert first['basis']['adjustment'] == 'none' and first['basis']['as_of'] == '2026-02-06'
    assert first['meta']['coverage']['missing_dates'] is None
    assert 'f0_' not in a.text and 'member_key' not in a.text
    assert get(client, key, limit=2, cursor=first['next_cursor']).json()['items'] == second['items']
    assert get(client, key, limit=2, as_of='2026-02-05').status_code == 422
    assert get(client, key, series_key='E51').status_code == 422
    assert get(client, key, representation='supplier-other').status_code == 422


def test_missing_factors_do_not_close_raw_and_bad_factor_is_not_hidden(market):
    client, current, _ = market
    key = identity(current)
    seed(current, date(2026, 1, 26), date(2026, 2, 6))
    raw_url = '/api/admin/market/instruments/' + key + '/bars?end=2026-02-06'
    assert get(client, key).status_code != 200
    assert client.get(raw_url, headers=HEADERS).status_code == 200
    old = seed_factors(current, [('2026-02-06', '2')])[0]
    assert get(client, key).status_code == 200
    broken = replace(old, content={**old.content, 'adj_factor': Decimal('0')}, observed_at=old.observed_at + timedelta(seconds=1))
    assert not run_entry(current, BY_ID['E69'], Inputs(broken))['qualified']
    denied = get(client, key)
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'DATA_RESTRICTED'
    assert client.get(raw_url, headers=HEADERS).status_code == 200


def test_factor_correction_invalidates_old_page_but_never_relabels_e69_as_normalized(market):
    client, current, _ = market
    key = identity(current)
    values = [('2026-01-02', '1'), ('2026-01-30', '2'), ('2026-02-02', '3')]
    rows = seed_factors(current, values)
    first = get(client, key, limit=1).json()
    changed = replace(rows[-1], content={**rows[-1].content, 'adj_factor': Decimal('4')}, observed_at=rows[-1].observed_at + timedelta(seconds=1))
    assert run_entry(current, BY_ID['E69'], Inputs(changed))['qualified']
    continued = get(client, key, limit=1, cursor=first['next_cursor'])
    assert continued.status_code == 409 and continued.json()['detail']['code'] == 'DATA_CHANGED'
    latest = get(client, key, limit=1).json()
    assert latest['items'][0]['factor'] == '4' and latest['basis']['anchor'] is None
    # No formula/coverage/anchor fact is admitted by these original E69 rows.
    seed(current, date(2026, 1, 26), date(2026, 2, 6))
    for mode in ('pre', 'post'):
        response = client.get('/api/admin/market/instruments/' + key + '/bars?end=2026-02-06&adjustment=' + mode, headers=HEADERS)
        assert response.status_code == 422 and response.json()['detail']['code'] == 'FACTOR_ANCHOR_UNVERIFIED'


def test_factor_query_budget_and_invalid_cursor_are_not_empty_success(market, monkeypatch):
    client, current, _ = market
    key = identity(current)
    seed_factors(current, [('2026-01-02', '1'), ('2026-01-30', '2')])
    page = get(client, key, limit=1).json()
    assert get(client, key, cursor=page['next_cursor'][:-1] + '!').status_code == 422
    assert get(client, key, limit=2, cursor=page['next_cursor']).status_code == 422
    from app.market_service import service
    # Keep identity metadata within budget while exceeding the output budget.
    response_size = len(get(client, key).text.encode())
    monkeypatch.setattr(service, 'MAX_RESPONSE_BYTES', response_size - 1)
    denied = get(client, key)
    assert denied.status_code == 422 and denied.json()['detail']['code'] == 'QUERY_BUDGET_EXCEEDED'


def test_s2_uses_installed_shared_native_arithmetic_without_claiming_factor_admission():
    pytest.importorskip('quantfoundry', reason='installed native wheel checked separately and by S3 CI')
    from app.market_service.adjustments import adjusted_prices
    assert [Decimal(value) for value in adjusted_prices([('100', '1', '2'), ('60', '2', '2'),
                                                       ('100', '1', '1'), ('60', '2', '1')])] == [Decimal('50'), Decimal('60'), Decimal('100'), Decimal('120')]


def test_same_code_other_variant_cannot_replace_selected_formal_factors(market):
    client, current, _ = market
    key = identity(current)
    selected = seed_factors(current, [('2026-01-02', '2')])[0]
    other = replace(selected, variant='other', content={**selected.content, 'adj_factor': Decimal('999')})
    assert run_entry(current, BY_ID['E69'], Inputs(other))['qualified']
    response = get(client, key)
    assert response.status_code == 200 and response.json()['items'] == [{'trade_date': '2026-01-02', 'factor': '2'}]


def test_explicit_math_oracle_adjusts_daily_before_bucket_and_preserves_quantities():
    pytest.importorskip('quantfoundry', reason='native wheel is required for the shared arithmetic')
    from app.market_service.adjustments import adjusted_prices
    from app.market_service.series import aggregate_bucket
    # Given mathematical assumptions only, never inferred E69 supplier meaning.
    daily = [dict(trade_date=date(2026, 1, 29), open='100', high='110', low='90', close='100', volume_lots='10', turnover_thousand_yuan='30'),
             dict(trade_date=date(2026, 1, 30), open='60', high='65', low='55', close='60', volume_lots='20', turnover_thousand_yuan='40')]
    for row, factor in zip(daily, ('1', '2')):
        fields = ('open', 'high', 'low', 'close')
        row.update(zip(fields, adjusted_prices([(row[name], factor, '2') for name in fields])))
    bar, missing = aggregate_bucket(daily, 'month', start=date(2026, 1, 1), end=date(2026, 1, 31), sessions=None)
    assert (Decimal(bar.open), Decimal(bar.high), Decimal(bar.low), Decimal(bar.close)) == (Decimal('50'), Decimal('65'), Decimal('45'), Decimal('60'))
    assert (bar.volume, bar.amount) == ('30', '70') and missing is None


def test_server_fixture_contract_cannot_be_enabled_outside_test_or_via_unpaired_anchor():
    from app.core.config import Settings
    base = dict(api_token='a' * 64, cursor_signing_key='b' * 64, database_password='isolated-only')
    for environment in ('local', 'production'):
        with pytest.raises(ValueError):
            Settings(**base, environment=environment, market_test_adjustment_contract='cumulative-exact-date-fixture@1')
    with pytest.raises(ValueError):
        Settings(**base, environment='test', market_test_post_anchor_factor='1')
    with pytest.raises(ValueError):
        Settings(**base, environment='test', market_test_adjustment_contract='cumulative-exact-date-fixture@1',
            market_test_post_anchor_factor='0', market_test_post_anchor_date='2025-01-01')
