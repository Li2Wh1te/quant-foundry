"""D11 joint-window integration under an explicit TEST mathematical contract.

This assembly is test code, not a production policy registry or HTTP override.
The default factory/real CurrentStore read both synthetic E70 and E69 inputs in
one bounded scope. It proves the seam, not a supplier formula or completed API.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from itertools import groupby

import pytest

from app.data_store.adapters.registry import BY_ID
from app.data_store.errors import DataStoreError
from app.data_store.pipeline import run_entry
from app.market_service.adjustments import adjusted_prices
from app.market_service.bindings import ETF_CATALOG, ETF_DAILY, ETF_FACTORS, EXCHANGE_CALENDAR
from app.market_service.contracts import Anchor, BarPage, BarWindow, Notice, PriceBasis
from app.market_service.errors import MarketError
from app.market_service.series import ONE_DAY, _calendar, aggregate_bucket, bucket_bounds, read_observations
from app.market_service.service import IdentityQuery
from tests.test_s2_market_read import market, HEADERS, database_schema  # noqa: F401
from tests.test_s2_market_series import identity, seed
from tests.test_s2_market_adjustments import seed_factors
from tests.test_data_store_kernel import database, limits, store  # noqa: F401
from tests.test_data_store_local_pipeline import Inputs, ready  # noqa: F401

pytest.importorskip('quantfoundry', reason='joint arithmetic checked with the installed native wheel')

TEST_CONTRACT = {
    'policy': 'isolated-cumulative-exact-date-oracle@1', 'fixture_only': True,
    'formula': 'price*factor/anchor', 'factor_validity': 'exact_business_date',
    'post_anchor': {'date': '2025-01-01', 'factor': '1'},
}
START, END = date(2026, 1, 26), date(2026, 2, 6)


@contextmanager
def joint_test_window(service, key, *, adjustment='pre', period='week', start=START,
                      end=END, as_of=END, limit=1, cursor=None):
    # Never enter this contract into production BINDINGS or an HTTP parameter.
    assert service.settings.environment == 'test'
    assert end <= as_of
    conditions = dict(operation='isolated-joint-window', test_contract=TEST_CONTRACT,
        instrument_key=key, period=period, adjustment=adjustment,
        start=start, end=end, as_of=as_of, limit=limit)
    with service.read_scope((ETF_DAILY, ETF_FACTORS, ETF_CATALOG, EXCHANGE_CALENDAR),
            conditions=conditions, identity=IdentityQuery(instrument_keys=(key,), start=start, end=as_of), cursor=cursor) as scope:
        segments = scope.mapping_segments(key)
        prices, factors = [], []
        for segment in segments:
            low, high = max(start, segment.valid_from), min(as_of, segment.valid_to - ONE_DAY if segment.valid_to else as_of)
            prices.extend(read_observations(scope, ETF_DAILY, segment.source_code, low, min(end, high)))
            factors.extend(read_observations(scope, ETF_FACTORS, segment.source_code, low, high))
        prices.sort(key=lambda row: row['trade_date'])
        by_date = {row['trade_date']: row['factor'] for row in factors}
        sessions = _calendar(scope, segments, start, as_of)
        dates = [start + timedelta(days=i) for i in range((as_of - start).days + 1)]
        if not all(day in sessions for day in dates):
            raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
        if adjustment == 'pre':
            anchor_date = max(day for day in dates if sessions[day])
            if anchor_date not in by_date:
                raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
            anchor = Anchor(date=anchor_date, factor=by_date[anchor_date])
        else:
            # Explicit oracle baseline, never the first available E69 row.
            anchor = Anchor(**TEST_CONTRACT['post_anchor'])
        for row in prices:
            if row['trade_date'] not in by_date:
                raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
            names = ('open', 'high', 'low', 'close')
            row.update(zip(names, adjusted_prices([(row[name], by_date[row['trade_date']], anchor.factor) for name in names])))
        outputs = [aggregate_bucket(list(rows), period, start=start, end=end, sessions=sessions)[0]
                   for _bucket, rows in groupby(prices, lambda row: bucket_bounds(row['trade_date'], period)[0])]
        if scope.position:
            before = date.fromisoformat(scope.position['before'])
            outputs = [row for row in outputs if row.bucket_start < before]
        selected, more = outputs[-limit:], len(outputs) > limit
        result = BarPage(items=selected, series_key=ETF_DAILY, instrument_key=key, period=period,
            next_cursor=scope.make_cursor({'before': selected[0].bucket_start.isoformat()}) if more else None,
            units=service.bindings[ETF_DAILY].declared_units, window=BarWindow(start=start, end=end, as_of=as_of),
            basis=PriceBasis(adjustment=adjustment, policy=TEST_CONTRACT['policy'], as_of=as_of, anchor=anchor,
                note='明确人工测试契约；不证明E69生产语义，不恢复历史当时所知。'),
            meta=scope.meta(scope=f'人工共同窗口 · {key} · {start} 至 {end}', business_as_of=selected[-1].last_trade_date,
                limitations=[Notice(code='TEST_CONTRACT_ONLY', message='该政策仅属于测试，默认E69 pre/post仍暂停。')]))
        scope.response(result)
        yield result, scope.bytes_read


def prepare(current, *, missing=()):
    key = identity(current)
    prices = seed(current, START, END)
    values = [(row.content['trade_date'], '1' if row.content['trade_date'] < '2026-01-28' else '2')
              for row in prices if row.content['trade_date'] not in missing]
    factors = seed_factors(current, [*values, ('2026-02-09', '100')])
    return key, prices, factors


@pytest.mark.parametrize('adjustment', ['pre', 'post'])
def test_real_joint_window_fixed_anchor_three_reads_and_daily_before_week(market, adjustment):
    client, current, service = market
    key, prices, _ = prepare(current)
    with joint_test_window(service, key, adjustment=adjustment) as (first, _):
        pass
    with joint_test_window(service, key, adjustment=adjustment, cursor=first.next_cursor) as (older, _):
        pass
    with joint_test_window(service, key, adjustment=adjustment, limit=2) as (whole, _):
        pass
    assert first.basis == older.basis == whole.basis
    assert older.meta.read_token == first.meta.read_token
    assert [*older.items, *first.items] == whole.items
    assert older.next_cursor is None
    divisor = Decimal('2') if adjustment == 'pre' else Decimal('1')
    with localcontext() as ctx:
        ctx.prec = 80
        expected = (prices[0].content['open'] / divisor).quantize(Decimal('.000000000001'), rounding=ROUND_HALF_EVEN)
    assert Decimal(whole.items[0].open) == expected
    assert whole.items[0].volume == format(sum(row.content['vol'] for row in prices[:5]), 'f')
    assert all(item.last_trade_date <= END for item in whole.items)
    # Default authenticated app still cannot inherit this synthetic policy.
    response = client.get('/api/admin/market/instruments/' + key + '/bars?end=2026-02-06&adjustment=' + adjustment, headers=HEADERS)
    assert response.status_code == 422 and response.json()['detail']['code'] == 'FACTOR_ANCHOR_UNVERIFIED'


def test_joint_replay_excludes_future_factor_and_does_not_reselect_page_anchor(market):
    _client, current, service = market
    key, prices, _ = prepare(current)
    with joint_test_window(service, key, end=date(2026, 1, 27), as_of=date(2026, 1, 27), period='day', limit=10) as (early, _):
        assert early.basis.anchor == Anchor(date=date(2026, 1, 27), factor='1')
        assert Decimal(early.items[0].open) == prices[0].content['open']
    with joint_test_window(service, key, limit=2) as (late, _):
        assert late.basis.anchor.factor == '2'  # future 100 is excluded
        assert Decimal(late.items[0].open) == prices[0].content['open'] / 2


def test_missing_exact_factor_refuses_joint_contract_without_forward_fill(market):
    client, current, service = market
    key, _prices, _factors = prepare(current, missing=('2026-01-28',))
    with pytest.raises(MarketError):
        with joint_test_window(service, key):
            pass
    assert client.get('/api/admin/market/instruments/' + key + '/bars?end=2026-02-06', headers=HEADERS).status_code == 200


def test_factor_only_change_invalidates_cursor_and_final_joint_submission(market):
    _client, current, service = market
    key, _prices, factors = prepare(current)
    with joint_test_window(service, key) as (old, _):
        pass
    changed = replace(factors[0], content={**factors[0].content, 'adj_factor': Decimal('3')}, observed_at=factors[0].observed_at + ONE_DAY)
    with pytest.raises(DataStoreError) as error:
        with joint_test_window(service, key):
            assert run_entry(current, BY_ID['E69'], Inputs(changed))['qualified']
    assert error.value.code == 'DATA_CHANGED'
    with pytest.raises(DataStoreError) as error:
        with joint_test_window(service, key, cursor=old.next_cursor):
            pass
    assert error.value.code == 'DATA_CHANGED'


def test_new_price_cannot_commit_with_old_factor_window_and_joint_budget_is_cumulative(market, monkeypatch):
    _client, current, service = market
    key, prices, _factors = prepare(current)
    changed = replace(prices[0], content={**prices[0].content, 'open': Decimal('4.2')}, observed_at=prices[0].observed_at + ONE_DAY)
    with pytest.raises(DataStoreError) as error:
        with joint_test_window(service, key) as (_page, _size):
            assert run_entry(current, BY_ID['E70'], Inputs(changed))['qualified']
    assert error.value.code == 'DATA_CHANGED'
    with joint_test_window(service, key) as (_page, current_size):
        pass
    from app.market_service import service as shared
    monkeypatch.setattr(shared, 'MAX_RESPONSE_BYTES', current_size - 1)
    with pytest.raises(DataStoreError) as error:
        with joint_test_window(service, key):
            pass
    assert error.value.code == 'QUERY_BUDGET_EXCEEDED'
