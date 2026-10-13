"""D09 acceptance through create_app, real isolated PG/Parquet/DuckDB.

Only source-shaped synthetic formal inputs and reviewed small identity facts are
created. No successful provider, HTTP result or collection reader is injected.
"""
from dataclasses import replace
from datetime import date
import os
from urllib.parse import urlencode

import pytest
from sqlalchemy import event

from app.data_ingestion.repositories.etf_query import EtfQueryRepository
from app.data_store.adapters.registry import BY_ID
from app.data_store.errors import DataStoreError
from app.data_store.limits import StoreLimits, MiB
from app.data_store.pipeline import run_entry
from app.data_store.storage import CurrentStore
from app.market_service import catalog
from app.market_service.service import MarketReadScope
from tests.test_data_store_domain_samples import sample, ts
from tests.test_data_store_local_pipeline import Inputs, ready  # noqa: F401
from tests.test_data_store_kernel import database, store  # noqa: F401
from tests.test_s2_market_read import HEADERS, market, mapping, publish, quality  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PostgreSQL required')
BASE = '/api/admin/market'


@pytest.fixture
def limits(request):
    return replace(StoreLimits(), file_rows=2048, lock_timeout_ms=100,
        duckdb_memory_bytes=64 * MiB, pipeline_spill_bytes=64 * MiB,
        query_bytes=getattr(request, 'param', 16 * MiB))


def directory(current, count=121):
    rows = []
    for index in range(count):
        exchange = 'SSE' if index % 2 == 0 else 'SZSE'
        code = f'{510000 + index}.SH' if exchange == 'SSE' else f'{510000 + index}.SZ'
        rows.append(ts('E67', 'directory', {'source': 'tushare', 'ts_code': code,
            'csname': f'正式夹具ETF{index:04}', 'exchange': exchange,
            'list_date': '20260102' if index else '20200101',
            # Deliberately not admitted by E67: must never leak into the DTO.
            'list_status': 'L', 'index_name': '非正式跟踪指数', 'mgt_fee': '0.15'}))
    result = run_entry(current, BY_ID['E67'], Inputs(*rows))
    assert result['complete'] and result['qualified'], result


def get(client, path, **params):
    return client.get(BASE + path + ('?' + urlencode(params) if params else ''), headers=HEADERS)


def test_full_catalog_search_and_three_pages_use_default_formal_route(market, monkeypatch):
    client, current, _service = market
    directory(current, 1107)
    reads, forbidden = [], []
    original = CurrentStore.read_many

    def observe(self, queries, **kwargs):
        reads.extend(spec.name for spec, _query in queries)
        return original(self, queries, **kwargs)

    def deny(*_args, **_kwargs):
        raise AssertionError('old market repository is blocked')

    monkeypatch.setattr(CurrentStore, 'read_many', observe)
    for repository in (EtfQueryRepository,):
        for name in ('list_codes', 'overview', 'list_days', 'get_day'):
            if hasattr(repository, name):
                monkeypatch.setattr(repository, name, deny)

    def deny_native(_connection, _cursor, statement, _params, _context, _many):
        if any(name in statement.lower() for name in ('etf_codes', 'etf_daily_bars',
                'etf_adjustment_factors', 'trading_calendar_days', 'data_source_configs', 'backtesting_accounts')):
            forbidden.append(statement)
            raise AssertionError('native collection/execution read is blocked')

    event.listen(current.catalog.engine, 'before_cursor_execute', deny_native)
    try:
        pages = []
        cursor = None
        for _ in range(3):
            response = get(client, '/instruments', **({'cursor': cursor} if cursor else {}))
            assert response.status_code == 200, response.text
            pages.append(response.json())
            cursor = pages[-1]['next_cursor']
        assert [page['total'] for page in pages] == [1107] * 3
        assert all(len(page['items']) == 50 for page in pages)
        symbols = [item['symbol'] for page in pages for item in page['items']]
        assert len(set(symbols)) == 150 and symbols == sorted(symbols)
        # Stateless revisiting an earlier page is stable within current data.
        revisited = get(client, '/instruments', cursor=pages[0]['next_cursor']).json()
        for field in ('items', 'total', 'next_cursor'):
            assert revisited[field] == pages[1][field]
        assert revisited['meta']['read_token'] == pages[1]['meta']['read_token']
        result = get(client, '/instruments', q='正式夹具ETF1106').json()
        assert result['total'] == 1 and result['items'][0]['symbol'] == '511106.SH'
        exact = get(client, '/instruments', q='511106').json()
        assert exact['items'] == result['items']
        exchange = get(client, '/instruments', exchange='SZSE', limit=200).json()
        assert exchange['total'] == 553 and len(exchange['items']) == 200
        assert all(item['exchange'] == 'SZSE' for item in exchange['items'])
        summary = get(client, '/overview').json()
        assert summary['total_instruments'] == 1107 and summary['listed_count'] is None
        assert summary['first_list_date'] == '2020-01-01' and summary['latest_list_date'] == '2026-01-02'
        assert summary['meta']['business_as_of'] is None
        assert summary['meta']['coverage']['missing_dates'] is None
    finally:
        event.remove(current.catalog.engine, 'before_cursor_execute', deny_native)
    assert not forbidden and set(reads) == {BY_ID['E67'].spec.name}


def test_source_metadata_and_stable_profile_remain_independent_of_prices_and_identity(market):
    client, current, _service = market
    publish(current)
    item = get(client, '/instruments').json()['items'][0]
    assert item['instrument_key'] == 'etf-current:510300.SH'
    for name in ('list_status', 'index_name', 'manager_name', 'management_fee', 'management_fee_unit'):
        assert item[name] is None
    assert get(client, '/instruments/etf-current:510300.SH').status_code == 200
    assert get(client, '/instruments/resolve', symbol='510300.SH').json()['detail']['code'] == 'IDENTITY_UNAVAILABLE'
    iid = mapping(current)
    stable_key = 'instrument:' + str(iid)
    resolved = get(client, '/instruments/resolve', symbol='510300').json()
    assert resolved['instrument_key'] == stable_key
    profile = get(client, '/instruments/' + stable_key)
    assert profile.status_code == 200, profile.text
    caps = {c['name']: c for c in profile.json()['instrument']['capabilities']}
    assert caps['metadata']['availability'] == caps['identity_resolution']['availability'] == 'available'
    assert caps['etf-daily-local']['request_checked'] is False
    assert caps['etf-factors-local']['availability'] == 'unknown'
    assert 'f0_' not in profile.text and 'member_key' not in profile.text
    mapping(current, source_code='510300.SZ', trading_code='510300')
    assert get(client, '/instruments/resolve', symbol='510300').json()['detail']['code'] == 'IDENTITY_AMBIGUOUS'
    assert get(client, '/instruments/resolve', symbol='510300.SH').status_code == 200


def test_absent_declared_namespace_never_uses_another_variant_or_fabricates_zero(market):
    client, current, _service = market
    result = run_entry(current, BY_ID['E67'], Inputs(replace(sample('E67'), variant='other')))
    assert result['qualified']
    for path in ('/instruments', '/overview'):
        response = get(client, path)
        assert response.status_code == 422
        assert response.json()['detail']['code'] == 'CAPABILITY_UNAVAILABLE'
        assert 'total' not in response.json() and 'total_instruments' not in response.json()
    assert get(client, '/instruments', exchange='SSE').status_code == 422


def test_filter_cursor_auth_and_unsupported_status_are_explicit(market):
    client, current, _service = market
    directory(current)
    first = get(client, '/instruments').json()
    for params in ({'limit': 0}, {'limit': 201}, {'exchange': 'bad'}, {'list_status': 'bad'},
                   {'q': '\x00'}, {'cursor': 'forged'}, {'catalog_key': 'all-stocks'}):
        assert get(client, '/instruments', **params).status_code == 422
    for patch in ({'q': 'ETF'}, {'exchange': 'SSE'}, {'limit': 20}):
        assert get(client, '/instruments', cursor=first['next_cursor'], **patch).json()['detail']['code'] == 'INVALID_CURSOR'
    assert get(client, '/instruments', list_status='L').json()['detail']['code'] == 'CAPABILITY_UNAVAILABLE'
    assert get(client, '/instruments', q='非正式跟踪指数').json()['total'] == 0
    assert client.get(BASE + '/instruments').status_code == 401
    assert client.post(BASE + '/instruments', headers=HEADERS, json={}).status_code == 405
    for extra in ('source=other', 'dataset=E51', 'sql=select', 'limit=50&limit=20', 'allow_partial=true'):
        assert client.get(BASE + '/instruments?' + extra, headers=HEADERS).status_code == 422
    assert get(client, '/overview', catalog_key='all-stocks').json()['detail']['code'] == 'CAPABILITY_UNAVAILABLE'
    assert get(client, '/instruments/etf-current:MISSING.SH').status_code == 404


def test_calendar_cross_year_future_missing_dates_and_neighbors_from_formal_facts(market):
    client, current, _service = market
    facts = [('SSE', '2026-12-31', True), ('SSE', '2027-01-01', False),
             ('SSE', '2027-01-04', True), ('SZSE', '2027-01-01', True)]
    result = run_entry(current, BY_ID['E68'], Inputs(*(ts('E68', 'calendar',
        {'exchange': exchange, 'calendar_date': day, 'is_open': opened}) for exchange, day, opened in facts)))
    assert result['qualified'], result
    response = get(client, '/calendar', exchange='SSE', start='2026-12-31', end='2027-01-04')
    assert response.status_code == 200, response.text
    days = response.json()['items']
    assert [day['state'] for day in days] == ['open', 'closed', 'unknown', 'unknown', 'open']
    assert all(day['previous_trading_date'] is None and day['next_trading_date'] is None for day in days)
    assert response.json()['meta']['business_as_of'] == '2027-01-04'
    assert response.json()['meta']['coverage']['verified'] is False
    assert response.json()['meta']['coverage']['missing_dates'] is None
    missing = get(client, '/calendar/SSE/2027-01-02').json()['items']
    assert len(missing) == 1 and missing[0]['state'] == 'unknown'
    assert get(client, '/calendar/SZSE/2027-01-01').json()['items'][0]['state'] == 'open'
    # Valid exchange without records is still an exchange with unknown dates.
    assert get(client, '/calendar/SZSE/2027-02-01').json()['items'][0]['state'] == 'unknown'
    for params in ({'exchange': 'NOPE', 'start': '2026-01-01', 'end': '2026-01-01'},
                   {'exchange': 'SSE', 'start': '2027-01-02', 'end': '2027-01-01'},
                   {'exchange': 'SSE', 'start': '2026-02-30', 'end': '2026-03-01'}):
        assert get(client, '/calendar', **params).status_code == 422
    assert get(client, '/calendar', exchange='SSE', start='2026-01-01', end='2027-01-02').json()['detail']['code'] == 'QUERY_BUDGET_EXCEEDED'


def test_old_calendar_repository_blocked_and_independent_catalog_failure(market, monkeypatch):
    client, current, _service = market
    publish(current, 'E68')

    def deny(*_args, **_kwargs):
        raise AssertionError('legacy date reader must not run')

    def block(_conn, _cursor, statement, _params, _context, _many):
        if 'trading_calendar_days' in statement.lower():
            deny()
    event.listen(current.catalog.engine, 'before_cursor_execute', block)
    try:
        assert get(client, '/instruments').status_code != 200
        assert get(client, '/calendar/SSE/2026-01-02').json()['items'][0]['state'] == 'open'
    finally:
        event.remove(current.catalog.engine, 'before_cursor_execute', block)


def test_data_change_and_quality_mutation_clear_continuation_without_old_content(market):
    client, current, _service = market
    directory(current)
    cursor = get(client, '/instruments').json()['next_cursor']
    quality(current, blocking=False)
    assert get(client, '/instruments', cursor=cursor).json()['detail']['code'] == 'DATA_CHANGED'


def test_output_boundary_quality_mutation_withdraws_constructed_business_dto(market, monkeypatch):
    client, current, _service = market
    directory(current)
    original = MarketReadScope.response

    def mutate_after_dto(self, dto):
        result = original(self, dto)
        quality(current, blocking=False)
        return result

    monkeypatch.setattr(MarketReadScope, 'response', mutate_after_dto)
    response = get(client, '/instruments')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'DATA_CHANGED'
    assert 'items' not in response.json()


@pytest.mark.parametrize('limits', [1024], indirect=True)
def test_real_read_budget_and_cancellation_never_return_complete_empty(market):
    client, current, service = market
    directory(current, 1107)
    response = get(client, '/instruments')
    assert response.status_code == 422 and response.json()['detail']['code'] == 'QUERY_BUDGET_EXCEEDED'
    assert 'total' not in response.json()
    service.cancelled = lambda: True
    with pytest.raises(DataStoreError) as caught:
        catalog.instruments(service)
    assert caught.value.code == 'OPERATION_CANCELLED'
