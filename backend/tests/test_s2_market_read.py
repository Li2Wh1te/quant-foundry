"""D08: default app, private PG schema, real formal Parquet/DuckDB reads.

All business inputs/evidence are synthetic. No success provider/HTTP payload is
injected. Only the existing isolated database dependency and overlay test probe
are used; supplier/legacy market access is explicitly blocked during requests.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.data_sources.models import DataSourceConfig
from app.data_store.adapters.registry import BY_ID
from app.data_store.catalog import Issue
from app.data_store.errors import DataStoreError
from app.data_store.pipeline import run_entry
from app.data_store.local_sources import NativeSources
from app.data_store.readers import Query
from app.data_store.storage import CurrentStore
from app.db.session import get_db_session
from app.instruments.models import Instrument, InstrumentCodeMappingRecord
from app.main import create_app
from app.market_service.bindings import ETF_CATALOG, ETF_DAILY, ETF_FACTORS
from app.market_service.service import create_market_service, IdentityQuery
from tests.test_data_store_api import ASGIClient, TOKEN
from tests.test_data_store_domain_samples import sample, seed_native_sources
from tests.test_data_store_kernel import database, limits, store  # noqa: F401
from tests.test_data_store_local_pipeline import Inputs, ready  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PostgreSQL required')
HEADERS = {'Authorization': f'Bearer {TOKEN}'}


@pytest.fixture
def market(ready):
    engine = ready.catalog.engine
    with engine.begin() as c:
        c.execute(text('CREATE TABLE data_store_legacy_maintenance (singleton integer PRIMARY KEY,phase text NOT NULL)'))
        c.execute(text("INSERT INTO data_store_legacy_maintenance VALUES(1,'ready')"))
        c.execute(text('CREATE TABLE data_store_legacy_restrictions (origin_key text PRIMARY KEY,dataset text,scope_key text,captured_at timestamptz)'))
        # Test DB only. Production uses its existing migration/constraints.
        c.execute(text('CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public'))
        c.execute(text('SET LOCAL search_path TO ' + database_schema(engine) + ', public'))
        Instrument.__table__.create(c)
        InstrumentCodeMappingRecord.__table__.create(c)
        DataSourceConfig.__table__.create(c)
    with Session(engine) as session, session.begin():
        session.add(DataSourceConfig(key='tushare', initialized=True, enabled=False, values={}))
    settings = Settings(api_token=TOKEN, cursor_signing_key='b' * 64, database_password='private-d08-test',
        environment='test', scheduler_enabled=False, data_store_root=ready.files.root, s3_worker_image=None)
    app = create_app(settings)

    def isolated_database():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = isolated_database
    service = create_market_service(settings, engine, owner_scope='isolated-d08-owner')
    return ASGIClient(app), ready, service


def database_schema(engine):
    with engine.connect() as c:
        return c.execute(text('SELECT current_schema()')).scalar_one()


def publish(current, entry_id='E67', *, code='510300.SH'):
    value = sample(entry_id)
    if 'ts_code' in value.content:
        value = replace(value, content={**value.content, 'ts_code': code})
    result = run_entry(current, BY_ID[entry_id], Inputs(value))
    assert result['qualified'], result


def mapping(current, *, source_code='510300.SH', trading_code='510300', logical_key=None,
            instrument_id=None, version=1, known_at=None, valid_to=None):
    instrument_id = instrument_id or uuid4()
    known_at = known_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(current.catalog.engine) as session, session.begin():
        if session.get(Instrument, instrument_id) is None:
            session.add(Instrument(id=instrument_id, asset_class='etf', status='active'))
            session.flush()
        row = InstrumentCodeMappingRecord(id=uuid4(), instrument_id=instrument_id,
            source='tushare', source_code=source_code, trading_code=trading_code,
            valid_from=date(2026, 1, 1), valid_to=valid_to, fact_version=version,
            logical_fact_key=logical_key or 'isolated:' + str(instrument_id),
            effective_range=Range(date(2026, 1, 1), valid_to, bounds='[)'),
            knowledge_range=Range(known_at, None, bounds='[)'),
            mapping_source='isolated-reviewed-identity', evidence='synthetic D08 identity oracle',
            known_at=known_at, observed_at=known_at)
        session.add(row)
    return instrument_id


def query(service, key=ETF_CATALOG, code='510300.SH'):
    binding = service.bindings[key]
    object_key = code if key == ETF_CATALOG else '2026-01-02'
    prefix = (binding.representation, code, object_key)
    return Query(partitions=(binding.partition(code, object_key),), lower=prefix,
        upper=(*prefix[:2], prefix[2] + '\x00'), page_size=2,
        filters=(('representation', '=', binding.representation),))


def quality(current, *, prefix=None, blocking=True):
    entry = BY_ID['E67']
    if prefix is None:
        target = {'blocking': blocking}
    else:
        target = {'scope_version': 'object-key-v1', 'prefix': prefix, 'blocking': blocking}
    issue = Issue('d08-quality', 'isolated-d08-quality', 'CORE_VALUE_INVALID', 'c' * 64,
                  target, {'operation': 'synthetic-test-only'})
    with current.catalog.transaction() as c:
        current.catalog.change_issues(c, entry.spec.name, (issue,), {})


def test_default_create_app_resolves_real_formal_nodes_and_disabled_source(market, monkeypatch):
    client, current, service = market
    publish(current)
    instrument_id = mapping(current)
    reads = []
    original = CurrentStore.read_many

    def read_formal(self, queries, **kwargs):
        reads.extend(spec.name for spec, _ in queries)
        return original(self, queries, **kwargs)

    monkeypatch.setattr(CurrentStore, 'read_many', read_formal)
    forbidden = []

    def forbid_native(_connection, _cursor, statement, _params, _context, _many):
        if any(name in statement.lower() for name in ('etf_codes', 'etf_daily_bars', 'etf_adjustment_factors',
                                                      'backtesting_accounts', 'data_source_configs')):
            forbidden.append(statement)
            raise AssertionError('market path must not read collection/execution tables')

    event.listen(current.catalog.engine, 'before_cursor_execute', forbid_native)
    try:
        response = client.get('/api/admin/market/instruments/resolve?symbol=510300&asset_type=etf', headers=HEADERS)
        capabilities = client.get('/api/admin/market/capabilities', headers=HEADERS)
    finally:
        event.remove(current.catalog.engine, 'before_cursor_execute', forbid_native)
    assert response.status_code == capabilities.status_code == 200
    result = response.json()
    assert result['instrument_key'] == 'instrument:' + str(instrument_id)
    assert result['symbol'] == '510300.SH'
    assert result['meta']['business_as_of'] is None
    assert result['meta']['coverage']['verified'] is False
    assert result['meta']['coverage']['missing_dates'] is None
    assert len(result['meta']['read_token']) == 64
    assert not any(k in response.text for k in ('member_key', 'basis_token', 'f0_', 'synthetic D08 identity oracle'))
    assert BY_ID['E67'].spec.name in reads and not forbidden
    catalog = capabilities.json()['catalogs'][0]
    assert catalog['catalog_key'] == ETF_CATALOG
    assert catalog['capability']['availability'] == 'available'
    assert catalog['capability']['mapping_implemented'] and catalog['capability']['metadata_known']
    assert catalog['capability']['request_checked'] is False


def test_independent_price_scope_without_factors_fees_accounts_or_worker(market):
    client, current, service = market
    publish(current, 'E67')
    publish(current, 'E70')
    mapping(current)
    with service.read_scope((ETF_DAILY,), conditions={'instrument_key': 'isolated', 'series': ETF_DAILY,
                            'adjustment': 'none', 'as_of': '2026-01-02'}) as scope:
        body = scope.objects(ETF_DAILY, scope.read(ETF_DAILY, query(service, ETF_DAILY)))[0]
        assert body['open'] == '4.1' and body['price_basis'] == 'provider_reported'
        assert service.bindings[ETF_DAILY].declared_units.model_dump() == {'price': '元', 'volume': '手', 'amount': '千元'}
    capabilities = client.get('/api/admin/market/capabilities', headers=HEADERS).json()
    caps = {c['name']: c for c in capabilities['capabilities']}
    assert caps[ETF_DAILY]['availability'] == 'available'
    assert caps[ETF_FACTORS]['availability'] == 'unavailable'
    assert caps['raw']['availability'] == 'unknown'
    assert caps['raw']['limitations'][0]['code'] == 'PRICE_BASIS_UNVERIFIED'
    assert all('E51' not in c['name'] for c in caps.values())
    assert client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS).status_code == 200


def test_missing_identity_unknown_symbol_unsupported_asset_and_missing_mapping_table(market):
    client, current, _service = market
    publish(current)
    response = client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS)
    assert response.status_code == 422 and response.json()['detail']['code'] == 'IDENTITY_UNAVAILABLE'
    mapping(current, source_code='999999.SH', trading_code='999999')
    response = client.get('/api/admin/market/instruments/resolve?symbol=999999.SH', headers=HEADERS)
    assert response.status_code == 404 and response.json()['detail']['code'] == 'IDENTITY_UNKNOWN'
    assert client.get('/api/admin/market/instruments/resolve?symbol=510300.SH&asset_type=stock', headers=HEADERS).json()['detail']['code'] == 'CAPABILITY_UNAVAILABLE'
    # Drop only this random private test schema's synthetic small-fact table.
    with current.catalog.engine.begin() as c:
        c.execute(text('DROP TABLE instrument_code_mappings'))
    response = client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS)
    assert response.status_code == 503 and response.json()['detail']['code'] == 'IDENTITY_MAPPING_UNAVAILABLE'


def test_ambiguous_display_code_never_selects_first_mapping(market):
    client, current, _service = market
    publish(current)
    mapping(current)
    mapping(current, source_code='510300.SZ', trading_code='510300')
    response = client.get('/api/admin/market/instruments/resolve?symbol=510300', headers=HEADERS)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'IDENTITY_AMBIGUOUS'
    assert client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS).status_code == 200


def test_corrected_mapping_is_folded_before_symbol_and_effective_filter(market):
    client, current, _service = market
    publish(current)
    iid = mapping(current, logical_key='corrected')
    mapping(current, instrument_id=iid, logical_key='corrected', version=2,
            known_at=datetime(2026, 1, 2, tzinfo=timezone.utc), valid_to=date(2026, 1, 3))
    response = client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS)
    assert response.status_code == 422 and response.json()['detail']['code'] == 'IDENTITY_UNAVAILABLE'


def test_request_level_admission_accepts_proven_e67_while_global_phase_is_reset_done(market):
    client, current, _service = market
    # Source-shaped input is processed only by the existing isolated pipeline.
    # The API itself cannot read these tables; it uses the resulting proof/files.
    seed_native_sources(current.catalog.engine)
    result = run_entry(current, BY_ID['E67'], NativeSources(current.catalog.engine))
    assert result['complete'] and result['qualified']
    mapping(current)
    with current.catalog.engine.begin() as c:
        c.execute(text("UPDATE data_store_legacy_maintenance SET phase='reset_done'"))
    response = client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS)
    assert response.status_code == 200
    with current.catalog.engine.connect() as c:
        assert c.execute(text('SELECT phase FROM data_store_legacy_maintenance')).scalar_one() == 'reset_done'


def test_shared_identity_window_and_combined_dependency_cursor(market):
    _client, current, service = market
    for entry in ('E67', 'E68', 'E69', 'E70'):
        publish(current, entry)
    iid = mapping(current)
    key = 'instrument:' + str(iid)
    identity = IdentityQuery(instrument_keys=(key,), start=date(2026, 1, 2), end=date(2026, 1, 3))
    dependencies = tuple(service.bindings)
    with service.read_scope(dependencies, conditions={'instrument_key': key, 'period': 'day',
                            'series_key': ETF_DAILY, 'adjustment': 'pre', 'as_of': '2026-01-03'}, identity=identity) as scope:
        assert scope.mapping_segments(key)[0].source_code == '510300.SH'
        cursor = scope.make_cursor({'before': '2026-01-02'})
        conditions = scope.conditions
    entry = BY_ID['E69']
    generation = current.catalog.dataset(entry.spec.name)['generation']
    with current.catalog.engine.begin() as c:
        current.catalog.change_issues(c, entry.spec.name,
            (Issue('factor-quality', 'isolated-quality', 'CORE_VALUE_INVALID', 'f' * 64, {}, {}),), {})
    assert current.catalog.dataset(entry.spec.name)['generation'] == generation
    with pytest.raises(DataStoreError) as caught:
        with service.read_scope(dependencies, conditions=conditions, identity=identity, cursor=cursor):
            pass
    assert caught.value.code == 'DATA_CHANGED'


def test_historical_mapping_gap_never_falls_back_to_current_display_code(market):
    _client, current, service = market
    publish(current)
    iid = mapping(current, valid_to=date(2026, 1, 3))
    key = 'instrument:' + str(iid)
    with service.read_scope((ETF_CATALOG,), conditions={'instrument_key': key},
        identity=IdentityQuery(instrument_keys=(key,), start=date(2026, 1, 2), end=date(2026, 1, 5))) as scope:
        with pytest.raises(ValueError) as caught:
            scope.mapping_segments(key)
        assert caught.value.code == 'IDENTITY_UNAVAILABLE'


def test_quality_refusal_and_maintenance_use_existing_gate(market):
    client, current, _service = market
    publish(current)
    mapping(current)
    quality(current)
    response = client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'DATA_RESTRICTED'
    with current.catalog.engine.begin() as c:
        c.execute(text("UPDATE data_store_legacy_maintenance SET phase='rebuilding'"))
    response = client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS)
    assert response.status_code == 503 and response.json()['detail']['code'] == 'DATA_STORE_REBUILDING'


def test_quality_on_another_formal_object_does_not_block_qualified_identity(market):
    client, current, service = market
    publish(current)
    mapping(current)
    binding = service.bindings[ETF_CATALOG]
    quality(current, prefix=[binding.representation, 'OTHER.SH', 'OTHER.SH'])
    assert client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS).status_code == 200


def test_stateless_cursors_bind_conditions_owner_dependencies_and_quality_without_generation_change(market):
    _client, current, service = market
    publish(current)
    conditions = {'instrument_key': 'isolated', 'series_key': 'isolated-series', 'q': 'ETF', 'adjustment': 'none',
                  'as_of': '2026-01-02', 'end': '2026-01-02'}
    with service.read_scope((ETF_CATALOG,), conditions=conditions) as scope:
        scope.read(ETF_CATALOG, query(service))
        token = scope.read_token
        cursor = scope.make_cursor({'before': '510300.SH'})
    with service.read_scope((ETF_CATALOG,), conditions=conditions, cursor=cursor, read_token=token) as scope:
        assert scope.position == {'before': '510300.SH'}
    for changed in ('instrument_key', 'series_key', 'q', 'adjustment', 'as_of', 'end'):
        with pytest.raises(DataStoreError, match='游标') as caught:
            with service.read_scope((ETF_CATALOG,), conditions={**conditions, changed: 'changed'}, cursor=cursor):
                pass
        assert caught.value.code == 'INVALID_CURSOR'
    other_owner = create_market_service(service.settings, service.engine, owner_scope='another-owner')
    with pytest.raises(DataStoreError) as caught:
        with other_owner.read_scope((ETF_CATALOG,), conditions=conditions, cursor=cursor):
            pass
    assert caught.value.code == 'INVALID_CURSOR'
    generation = current.catalog.dataset(BY_ID['E67'].spec.name)['generation']
    quality(current, blocking=False)
    assert current.catalog.dataset(BY_ID['E67'].spec.name)['generation'] == generation
    with pytest.raises(DataStoreError) as caught:
        with service.read_scope((ETF_CATALOG,), conditions=conditions, cursor=cursor):
            pass
    assert caught.value.code == 'DATA_CHANGED'


@pytest.mark.parametrize('change', ['quality', 'overflow', 'mapping', 'maintenance', 'coverage'])
def test_output_boundary_discards_late_dependency_changes(market, monkeypatch, change):
    client, current, _service = market
    publish(current)
    mapping(current)
    generation = current.catalog.dataset(BY_ID['E67'].spec.name)['generation']
    original = CurrentStore.read_many

    def change_after_actual_read(self, queries, **kwargs):
        result = original(self, queries, **kwargs)
        if change == 'quality':
            quality(current, blocking=False)
        else:
            with current.catalog.engine.begin() as c:
                if change == 'overflow':
                    c.execute(text("UPDATE data_store_entry_status SET summary_json=:summary WHERE entry_id='E67'"),
                        {'summary': '{"overflow_restriction":{"blocking_objects":1}}'})
                elif change == 'mapping':
                    c.execute(text("UPDATE instrument_code_mappings SET evidence='corrected synthetic oracle'"))
                elif change == 'coverage':
                    c.execute(text("UPDATE data_store_entry_status SET summary_json=:summary WHERE entry_id='E67'"),
                        {'summary': '{"full_coverage":{"version":1,"complete":false}}'})
                else:
                    c.execute(text("UPDATE data_store_legacy_maintenance SET phase='rebuilding'"))
        return result

    monkeypatch.setattr(CurrentStore, 'read_many', change_after_actual_read)
    response = client.get('/api/admin/market/instruments/resolve?symbol=510300.SH', headers=HEADERS)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'DATA_CHANGED'
    assert current.catalog.dataset(BY_ID['E67'].spec.name)['generation'] == generation
    assert 'instrument_key' not in response.json()


def test_read_budget_cancellation_auth_and_no_uninstalled_endpoints(market):
    client, current, service = market
    publish(current)
    assert client.get('/api/admin/market/capabilities').status_code == 401
    assert client.post('/api/admin/market/instruments/resolve', headers=HEADERS, json={}).status_code == 405
    assert client.get('/api/admin/market/instruments', headers=HEADERS).status_code == 404
    assert client.get('/api/admin/market/instruments/resolve?symbol=%2Fetc%2Fpasswd', headers=HEADERS).status_code == 422
    for injected in ('dataset=E51', 'source=tonghuashun', 'allow_partial=true', 'sql=select', 'symbol=OTHER.SH'):
        assert client.get('/api/admin/market/instruments/resolve?symbol=510300.SH&' + injected, headers=HEADERS).status_code == 422
    with service.read_scope((ETF_CATALOG,), conditions={}) as scope:
        with pytest.raises(DataStoreError):
            scope.read(ETF_CATALOG, replace(query(service), require_qualified=False))
        with pytest.raises(DataStoreError) as caught:
            scope.read(ETF_CATALOG, replace(query(service), page_size=current.limits.query_rows + 1))
        assert caught.value.code == 'QUERY_BUDGET_EXCEEDED'
    service.cancelled = lambda: True
    with pytest.raises(DataStoreError) as caught:
        with service.read_scope((ETF_CATALOG,), conditions={}):
            pass
    assert caught.value.code == 'OPERATION_CANCELLED'
