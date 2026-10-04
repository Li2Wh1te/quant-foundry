"""Invented native inputs and mocked HTTP on the registered default handler.

No test calls a supplier or installs a real sample repair. Immutable originals,
request counts, source-head CAS and the normalizer's basis are exercised together.
"""
from contextlib import contextmanager
from datetime import date, timedelta
import json
import os
import time
from unittest.mock import Mock
from uuid import uuid4

import pytest
import requests
import structlog
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.scheduling.registry import task_registry
from app.data_ingestion.clients import tonghuashun as transport
from app.data_ingestion.models.tonghuashun import (
    TonghuashunTicker, TonghuashunObservation, TonghuashunCollectionState, TonghuashunWorkUnit,
    TonghuashunRequestBudget,
)
from app.data_ingestion.scheduler_tasks import tonghuashun as scheduler
from app.data_ingestion.tonghuashun import bounded, bounded_batch
from app.data_ingestion.tonghuashun.contracts import DATASETS, date_ms, exact_json
from app.data_ingestion.tonghuashun.control import (
    CollectionYield, DefaultRepair, ExecutionDeadline, default_repair, execution_deadline,
)
from app.data_ingestion.tonghuashun.repository import CollectionRepository, materialize
from app.data_sources.models import DataSourceConfig
from app.data_sources.errors import SourceError
from tests.test_tonghuashun_collections import NOW, bar, seed, ticker

STOCK = '000999.SZ'
FUND = 'F77777.OF'
DAY = date(2018, 11, 1)
END = date(2017, 6, 30)


@pytest.fixture
def native_engine(tmp_path):
    admin = None
    schema = 'r01_repair_' + uuid4().hex
    if os.getenv('POSTGRES_TEST_ENABLED') == '1':
        # The existing kernel guard only permits an explicitly selected local
        # test database. Every test owns its random schema; never public or a
        # deployed collection. This also exercises real pacing/CAS transactions.
        from tests.test_data_store_kernel import test_url
        admin = create_engine(test_url(), connect_args={'connect_timeout': 3})
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(test_url(), connect_args={'options': f'-csearch_path={schema}', 'connect_timeout': 3})
    else:
        engine = create_engine('sqlite:///' + str(tmp_path / 'native.sqlite'))
    for table in (DataSourceConfig.__table__, TonghuashunTicker.__table__, TonghuashunObservation.__table__,
                  TonghuashunCollectionState.__table__, TonghuashunWorkUnit.__table__, TonghuashunRequestBudget.__table__):
        table.create(engine)
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE task_runs (task_type TEXT, status TEXT, parameters JSON)'))
    with Session(engine) as session:
        session.add(DataSourceConfig(key='tonghuashun', initialized=True, enabled=True,
                    encrypted_secrets='invented-fixture-ciphertext', values={}, version=1))
        session.commit()
    seed(engine, [ticker(STOCK, 'a-share'), ticker(FUND, 'fund-otc')])
    try:
        yield engine
    finally:
        engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def publish(engine, dataset, data, *, expected=0, subject=None, when=NOW):
    subject = subject or (STOCK if dataset == 'stock_daily' else FUND)
    with Session(engine) as session:
        CollectionRepository(session).publish(dataset, subject, 'default', expected=expected,
                                              data=data, requests=[], now=when)
        session.commit()
        observation = session.get(TonghuashunObservation,
            session.get(TonghuashunCollectionState, (dataset, subject, 'default')).observation_id)
        return observation.id, observation.content_hash


def selection(engine, dataset, data, **target):
    identifier, digest = publish(engine, dataset, data)
    subject, asset = (STOCK, 'a-share') if dataset == 'stock_daily' else (FUND, 'fund-otc')
    return bounded.RepairScope(DefaultRepair(dataset, subject, asset, 1, identifier, digest, **target))


def http_response(data=None, *, status=200):
    result = Mock(status_code=status, headers={})
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [exact_json({'code': 0, 'data': data or {'item': []}}).encode()]
    return result


def worker(engine, scope, tmp_path, monkeypatch, responses):
    from app.db import session as database
    client = transport.TonghuashunClient('https://example.test', 'invented-private-key', interval_ms=0)
    monkeypatch.setattr(transport, '_gate', transport._RequestGate())
    get = Mock(side_effect=responses)
    monkeypatch.setattr(transport.requests, 'get', get)
    monkeypatch.setattr(database, 'get_engine', lambda: engine)
    monkeypatch.setattr(scheduler, 'get_engine', lambda: engine)
    monkeypatch.setattr(scheduler, 'get_settings', lambda: Mock())
    monkeypatch.setattr(transport.TonghuashunClient, 'from_settings', lambda settings: client)
    output = tmp_path / ('worker-' + uuid4().hex)
    output.mkdir()
    code = bounded.run_worker(scope, output, str(uuid4()), time.monotonic() + 20)
    return code, json.loads((output / 'worker.json').read_text()), get


def current(engine, scope):
    with Session(engine) as session:
        state = session.get(TonghuashunCollectionState, (scope.dataset, scope.subject, 'default'))
        return state.observation_id, state.status, materialize(session, session.get(TonghuashunObservation, state.observation_id))


@contextmanager
def execution(scope, allowance):
    limit = ExecutionDeadline(time.monotonic() + 30, lambda: False, max_http_attempts=allowance)
    deadline_token = execution_deadline.set(limit)
    repair_token = default_repair.set(scope.selection)
    try:
        yield limit
    finally:
        default_repair.reset(repair_token)
        execution_deadline.reset(deadline_token)


@pytest.mark.parametrize('price', ['2', '1.234567890123456789'])
def test_single_day_keeps_default_history_metadata_failures_and_unrelated_basis(native_engine, tmp_path, monkeypatch, price):
    interface = DATASETS['stock_daily'].interface
    old = {'item': [bar(DAY - timedelta(days=1)), bar(DAY), bar(DAY + timedelta(days=1))],
           'adjust': 'none', 'requested_start': '2010-01-01', 'requested_end': '2026-09-30',
           'failed_requests': [
               {'interface': interface, 'parameters': {'thscode': STOCK, 'interval': '1d', 'adjust': 'none',
                                                      'start': date_ms(DAY), 'end': date_ms(DAY)}, 'error_kind': 'timeout'},
               {'interface': interface, 'parameters': {'thscode': STOCK, 'interval': '1d', 'adjust': 'none',
                   'start': date_ms(DAY + timedelta(days=1)), 'end': date_ms(DAY + timedelta(days=1))}, 'error_kind': 'timeout'}]}
    scope = selection(native_engine, 'stock_daily', old, day=DAY)
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch,
                               [http_response({'item': [bar(DAY, price)], 'adjust': 'none'})])
    _, status, data = current(native_engine, scope)
    assert code == 0 and record['outcome'] == 'succeeded' and status == 'partial'
    assert get.call_count == 1 and get.call_args.kwargs['params'] == scope.selection.allowed_requests()[0][1]
    assert data['item'][0] == old['item'][0] and data['item'][2] == old['item'][2]
    assert data['item'][1] == bar(DAY, price)
    assert {k: v for k, v in data.items() if k not in ('item', 'failed_requests')} == {
        k: v for k, v in old.items() if k not in ('item', 'failed_requests')}
    assert data['failed_requests'] == [old['failed_requests'][1]]
    assert record['counts'] == {'logical_requests': 1, 'http_attempts': 1, 'reused': 0}
    with Session(native_engine) as session:
        states = list(session.scalars(select(TonghuashunCollectionState)))
        assert len(states) == 1 and states[0].variant == 'default' and states[0].reconciled_at is None
        assert session.scalar(select(TonghuashunWorkUnit.scope)) is None
        newest = session.get(TonghuashunObservation, states[0].observation_id)
        receipts = json.loads(newest.request_json)
    from app.data_store.local_sources import EffectiveBasis, instant_ns
    tracker = EffectiveBasis()
    original = {'dataset': scope.dataset, 'subject': STOCK, 'variant': 'default', 'id': str(scope.selection.observation_id),
                'content_hash': scope.selection.content_sha256, 'observed_at': NOW.isoformat()}
    keys = [str(row['date_ms']) for row in old['item']]
    basis, _ = tracker.apply(original, old, {key: (instant_ns(NOW), 'original') for key in keys}, 'date_ms', [], native=True)
    fresh = {**original, 'id': str(newest.id), 'content_hash': newest.content_hash,
             'observed_at': (NOW + timedelta(days=1)).isoformat()}
    patched, _ = tracker.apply(fresh, data, {key: (instant_ns(NOW + timedelta(days=1)), 'new') for key in keys},
                               'date_ms', receipts, native=True)
    assert patched[keys[0]] == basis[keys[0]] and patched[keys[2]] == basis[keys[2]]
    assert patched[keys[1]] != basis[keys[1]]


@pytest.mark.parametrize('returned', ['empty', 'forward', 'other_native_key'])
def test_stock_empty_or_other_adjustment_cannot_replace_old_head(native_engine, tmp_path, monkeypatch, returned):
    old = {'item': [bar(DAY)], 'adjust': 'none'}
    scope = selection(native_engine, 'stock_daily', old, day=DAY)
    data = {'item': []} if returned == 'empty' else {'item': [bar(DAY, '2')], 'adjust': 'forward'}
    if returned == 'other_native_key':
        data = {'item': [{**bar(DAY, '2'), 'date_ms': date_ms(DAY) + 3600000}]}
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [http_response(data)])
    assert code == 2 and record['outcome'] == 'failed' and get.call_count == 1
    assert current(native_engine, scope)[0] == scope.selection.observation_id


def test_worker_captures_failure_after_application_logger_was_already_cached(native_engine, tmp_path, monkeypatch):
    from app.data_ingestion.tonghuashun import service
    original_logging = structlog.get_config()
    outside_events = []
    def old_processor(_logger, _method, event):
        outside_events.append(dict(event))
        return event
    try:
        # Application startup enables caching. Binding a module logger before
        # run_worker reproduces the full-suite failure independently of test
        # order; configure(processors=...) cannot replace a bound old chain.
        structlog.configure(processors=[old_processor], wrapper_class=structlog.BoundLogger,
                            logger_factory=structlog.ReturnLoggerFactory(), cache_logger_on_first_use=True)
        cached = structlog.get_logger(service.__name__)
        cached.info('invented_prebound_application_event')
        assert len(outside_events) == 1
        outside_events.clear()
        monkeypatch.setattr(service, 'logger', cached)
        scope = selection(native_engine, 'stock_daily', {'item': [bar(DAY)], 'adjust': 'none'}, day=DAY)
        code, record, get = worker(native_engine, scope, tmp_path, monkeypatch,
                                   [http_response({'item': []})])
        assert code == 2 and record['outcome'] == 'failed'
        assert record['diagnostic']['error_kind'] == 'invalid_data'
        assert record['diagnostic']['reason'] == '历史行情为空，尚不能确认该范围覆盖。'
        assert record['publication']['status'] == 'none' and not record['unknown_publication']
        assert get.call_count == 1 and current(native_engine, scope)[0] == scope.selection.observation_id
        # The independent worker boundary must also prevent the old logger's
        # processors from emitting a raw event/exception outside its whitelist.
        assert outside_events == []
    finally:
        structlog.configure(**original_logging)


@pytest.mark.parametrize('old_utc,fresh_same_key', [(True, True), (True, False), (False, True), (False, False)])
def test_stock_uses_pinned_raw_key_instead_of_query_timestamp(native_engine, tmp_path, monkeypatch, old_utc, fresh_same_key):
    # Reproduce the observed 1541001600000 raw key and a synthetic UTC variant.
    # Only the timestamp is reproduced; code, values and HTTP are invented.
    shanghai_key, utc_key = date_ms(DAY), 1541030400000
    assert shanghai_key == 1541001600000
    assert utc_key == shanghai_key + 8 * 3600000
    raw_key = utc_key if old_utc else shanghai_key
    old = {'item': [bar(DAY - timedelta(days=1)), {**bar(DAY), 'date_ms': raw_key},
                    bar(DAY + timedelta(days=1))], 'adjust': 'none'}
    scope = selection(native_engine, 'stock_daily', old, day=DAY)
    returned_key = raw_key if fresh_same_key else shanghai_key if old_utc else utc_key
    fresh = {'item': [{**bar(DAY, '2'), 'date_ms': returned_key}], 'adjust': 'none'}
    code, result, get = worker(native_engine, scope, tmp_path, monkeypatch, [http_response(fresh)])
    if old_utc:
        # Canonical daily windows do not support a Shanghai 08:00 raw key.
        # Preserve that policy even though provider_date yields the same day.
        assert code == 2 and result['problem'] == 'baseline_unverifiable'
        assert get.call_count == 0 and result['counts']['http_attempts'] == 0
        assert current(native_engine, scope)[0] == scope.selection.observation_id
        return
    assert get.call_count == 1
    assert get.call_args.kwargs['params']['start'] == shanghai_key
    assert get.call_args.kwargs['params']['end'] == shanghai_key
    if fresh_same_key:
        assert code == 0 and result['outcome'] == 'succeeded'
        rows = current(native_engine, scope)[2]['item']
        assert rows[1] == fresh['item'][0]
        assert rows[0] == old['item'][0] and rows[2] == old['item'][2]
    else:
        assert code == 2 and result['outcome'] == 'failed'
        assert current(native_engine, scope)[0] == scope.selection.observation_id


def descriptor(end, kind='quarter'):
    return {'start_date_ms': date_ms(date(end.year, 1, 1)), 'end_date_ms': date_ms(end), 'report_type': kind,
            'report_type_name': {'quarter': '季度', 'annual': '年度', 'semiannual': '半年度'}[kind]}


def report_data(end, kind='quarter', *, asset='stock', code='000001.SZ'):
    return {'item': [{'thscode': code, 'asset_type': asset, 'name': 'invented member',
                      'end_date_ms': date_ms(end), 'report_type': {'quarter': '季度', 'annual': '年度'}[kind],
                      'hold_ratio': 1}]}


def old_reports(dataset):
    asset = 'stock' if dataset == 'fund_stock_history' else 'bond'
    target, annual, pending = descriptor(END), descriptor(END, 'annual'), descriptor(date(2019, 12, 31))
    wrapper = lambda raw: {'report_key': f"{END}:{raw['report_type']}", 'report': raw,
                          'data': report_data(END, raw['report_type'], asset=asset)}
    interface = DATASETS[dataset].interface
    return {'item': [wrapper(target), wrapper(annual)], 'report_directory': {'item': [target, annual, pending]},
            'failed_requests': [{'interface': interface, 'parameters': {'thscode': FUND, 'end_date': str(end),
                                 'report_type': 'quarter'}, 'error_kind': 'timeout'} for end in (END, date(2019, 12, 31))]}


@pytest.mark.parametrize('dataset', ['fund_stock_history', 'fund_bond_history'])
def test_fresh_exact_old_quarter_replaces_whole_group_and_keeps_other_failure(native_engine, tmp_path, monkeypatch, dataset):
    old = old_reports(dataset)
    scope = selection(native_engine, dataset, old, end_date=END, report_type='quarter')
    directory = {'item': old['report_directory']['item']}
    data = report_data(END, asset='stock' if dataset == 'fund_stock_history' else 'bond', code='000002.SZ')
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [http_response(directory), http_response(data)])
    _, status, fresh = current(native_engine, scope)
    assert code == 0 and status == 'partial' and record['publication']['status'] == 'verified'
    assert get.call_count == 2 and get.call_args.kwargs['params'] == {
        'thscode': FUND, 'end_date': str(END), 'report_type': 'quarter'}
    wrappers = {row['report_key']: row for row in fresh['item']}
    assert wrappers[f'{END}:quarter']['data'] == data
    assert wrappers[f'{END}:annual'] == old['item'][1]
    assert fresh['failed_requests'] == [old['failed_requests'][1]]
    assert fresh['current_provider_directory'] == directory
    assert record['counts'] == {'logical_requests': 2, 'http_attempts': 2, 'reused': 0}


@pytest.mark.parametrize('problem', ['omitted', 'new_report', 'duplicate', 'changed_other_period'])
def test_fresh_directory_problem_never_falls_back_or_fetches_report(native_engine, tmp_path, monkeypatch, problem):
    old = old_reports('fund_stock_history')
    scope = selection(native_engine, 'fund_stock_history', old, end_date=END, report_type='quarter')
    entries = list(old['report_directory']['item'])
    if problem == 'omitted': entries = entries[1:]
    elif problem == 'new_report': entries.append(descriptor(date(2021, 12, 31)))
    elif problem == 'duplicate': entries.append(entries[0])
    else: entries[1] = {**entries[1], 'start_date_ms': date_ms(date(2017, 2, 1))}
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [http_response({'item': entries})])
    assert code == 2 and record['outcome'] == 'failed' and get.call_count == 1
    assert current(native_engine, scope)[0] == scope.selection.observation_id


@pytest.mark.parametrize('problem', ['pagination', 'bad_member', 'wrong_subject'])
def test_incomplete_fresh_report_cannot_reuse_old_members(native_engine, tmp_path, monkeypatch, problem):
    old = old_reports('fund_stock_history')
    scope = selection(native_engine, 'fund_stock_history', old, end_date=END, report_type='quarter')
    data = report_data(END)
    if problem == 'pagination': data['has_more'] = True
    elif problem == 'bad_member': data['item'][0]['end_date_ms'] = date_ms(END - timedelta(days=1))
    else: data['thscode'] = 'different.OF'
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch,
                               [http_response(old['report_directory']), http_response(data)])
    assert code == 2 and record['outcome'] == 'failed' and get.call_count == 2
    assert current(native_engine, scope)[0] == scope.selection.observation_id


@pytest.mark.parametrize('dataset', ['fund_financial_indicators', 'fund_income', 'fund_balance'])
def test_financial_selector_needs_genuine_original_period_before_any_http(native_engine, tmp_path, monkeypatch, dataset):
    old = {'item': [{'start_date_ms': None, 'end_date_ms': date_ms(END)}]}
    scope = selection(native_engine, dataset, old)
    from dataclasses import replace
    scope = bounded.RepairScope(replace(scope.selection, start_date=date(2017, 1, 1), end_date=END,
        selector_observation_id=scope.selection.observation_id, selector_content_sha256=scope.selection.content_sha256))
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [])
    assert code == 2 and record['problem'] == 'baseline_unverifiable'
    assert record['counts']['http_attempts'] == 0 and get.call_count == 0
    assert current(native_engine, scope)[0] == scope.selection.observation_id
    value = scope.selection.as_dict()
    value['selector']['start_date'] = None
    with pytest.raises((ValueError, TypeError)):
        bounded.RepairScope.from_dict(value)


@pytest.mark.parametrize('problem', ['missing_start', 'missing_end', 'other_period'])
def test_fresh_financial_window_must_actually_hit_both_original_dates(native_engine, tmp_path, monkeypatch, problem):
    old = {'item': [{'start_date_ms': date_ms(date(2017, 1, 1)), 'end_date_ms': date_ms(END), 'net_profit': 1}]}
    scope = selection(native_engine, 'fund_income', old)
    from dataclasses import replace
    scope = bounded.RepairScope(replace(scope.selection, start_date=date(2017, 1, 1), end_date=END,
        selector_observation_id=scope.selection.observation_id, selector_content_sha256=scope.selection.content_sha256))
    row = dict(old['item'][0])
    if problem == 'missing_start': row['start_date_ms'] = None
    elif problem == 'missing_end': row['end_date_ms'] = None
    else: row['end_date_ms'] = date_ms(END + timedelta(days=1))
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [http_response({'item': [row]})])
    assert code == 2 and record['outcome'] == 'failed' and get.call_count == 1
    assert get.call_args.kwargs['params'] == {'thscode': FUND}
    assert current(native_engine, scope)[0] == scope.selection.observation_id


@pytest.mark.parametrize('old_utc,fresh_same_key', [(True, True), (True, False), (False, True), (False, False)])
def test_financial_pair_requires_exact_original_raw_keys_and_existing_adapter_contract(native_engine, tmp_path, monkeypatch, old_utc, fresh_same_key):
    from dataclasses import replace
    start = date(2017, 1, 1)
    raw_start, raw_end = (date_ms(day) + (8 * 3600000 if old_utc else 0) for day in (start, END))
    old = {'item': [{'start_date_ms': raw_start, 'end_date_ms': raw_end, 'net_profit': 1}]}
    scope = selection(native_engine, 'fund_income', old)
    scope = bounded.RepairScope(replace(scope.selection, start_date=start, end_date=END,
        selector_observation_id=scope.selection.observation_id, selector_content_sha256=scope.selection.content_sha256))
    # Both variants represent the same canonical dates, but only the exact
    # immutable raw pair is the approved target. No plan-provided date fills it.
    offset = 0 if fresh_same_key else -8 * 3600000 if old_utc else 8 * 3600000
    fresh = {'item': [{'start_date_ms': raw_start + offset, 'end_date_ms': raw_end + offset, 'net_profit': 2}]}
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [http_response(fresh)])
    if old_utc:
        # The financial adapter uses the same canonical source-date boundary.
        # An unsupported original is rejected before HTTP, without broadening
        # a shared parser merely to make a repair target executable.
        assert code == 2 and record['problem'] == 'baseline_unverifiable'
        assert get.call_count == 0 and record['counts']['http_attempts'] == 0
        assert current(native_engine, scope)[0] == scope.selection.observation_id
        return
    assert get.call_count == 1 and get.call_args.kwargs['params'] == {'thscode': FUND}
    if fresh_same_key:
        assert code == 0 and current(native_engine, scope)[2] == fresh
    else:
        assert code == 2 and record['outcome'] == 'failed'
        assert current(native_engine, scope)[0] == scope.selection.observation_id
        assert record['diagnostic']['reason'] == '财报修复响应未命中原始确切期间。'


@pytest.mark.parametrize('dataset', ['stock_daily', 'fund_income'])
def test_ambiguous_business_date_in_pinned_original_is_rejected_before_http(native_engine, tmp_path, monkeypatch, dataset):
    from dataclasses import replace
    if dataset == 'stock_daily':
        old = {'item': [bar(DAY), {**bar(DAY), 'date_ms': date_ms(DAY) + 8 * 3600000}]}
        scope = selection(native_engine, dataset, old, day=DAY)
    else:
        start = date(2017, 1, 1)
        old = {'item': [{'start_date_ms': date_ms(start) + offset,
                         'end_date_ms': date_ms(END) + offset, 'net_profit': 1} for offset in (0, 8 * 3600000)]}
        scope = selection(native_engine, dataset, old)
        scope = bounded.RepairScope(replace(scope.selection, start_date=start, end_date=END,
            selector_observation_id=scope.selection.observation_id, selector_content_sha256=scope.selection.content_sha256))
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [])
    assert code == 2 and record['problem'] == 'baseline_unverifiable'
    assert get.call_count == 0 and record['counts']['http_attempts'] == 0


def test_financial_real_old_selector_does_not_merge_old_rows_into_new_direct_response(native_engine, tmp_path, monkeypatch):
    from dataclasses import replace
    old = {'item': [{'start_date_ms': date_ms(date(2017, 1, 1)), 'end_date_ms': date_ms(END), 'net_profit': 1}]}
    scope = selection(native_engine, 'fund_income', old)
    newer = {'item': [{'start_date_ms': date_ms(date(2018, 1, 1)), 'end_date_ms': date_ms(date(2018, 6, 30)), 'net_profit': 2}]}
    identifier, digest = publish(native_engine, 'fund_income', newer, expected=1, when=NOW + timedelta(seconds=1))
    scope = bounded.RepairScope(replace(scope.selection, revision=2, observation_id=identifier, content_sha256=digest,
        start_date=date(2017, 1, 1), end_date=END, selector_observation_id=scope.selection.observation_id,
        selector_content_sha256=scope.selection.content_sha256))
    fresh = {'item': [{**old['item'][0], 'net_profit': 3}]}
    code, _, get = worker(native_engine, scope, tmp_path, monkeypatch, [http_response(fresh)])
    assert code == 0 and get.call_count == 1 and current(native_engine, scope)[2] == fresh
    with Session(native_engine) as session:
        originals = list(session.scalars(select(TonghuashunObservation)))
        assert len(originals) == 3
        assert materialize(session, session.get(TonghuashunObservation, scope.selection.selector_observation_id)) == old
        assert materialize(session, session.get(TonghuashunObservation, identifier)) == newer


@pytest.mark.parametrize('dataset,subject,field', [
    ('fund_financial_indicators', 'F77777_2.OF', 'asset_nav'), ('fund_income', 'F77777_1.OF', 'net_profit')])
def test_financial_class_suffix_is_sent_exactly_with_no_invented_period_filter(native_engine, tmp_path, monkeypatch, dataset, subject, field):
    seed(native_engine, [ticker(subject, 'fund-otc')])
    old = {'item': [{'start_date_ms': date_ms(date(2017, 1, 1)), 'end_date_ms': date_ms(END), field: 1}]}
    identifier, digest = publish(native_engine, dataset, old, subject=subject)
    scope = bounded.RepairScope(DefaultRepair(dataset, subject, 'fund-otc', 1, identifier, digest,
        start_date=date(2017, 1, 1), end_date=END, selector_observation_id=identifier, selector_content_sha256=digest))
    code, _, get = worker(native_engine, scope, tmp_path, monkeypatch, [http_response(old)])
    assert code == 0 and get.call_count == 1 and get.call_args.kwargs['params'] == {'thscode': subject}


@pytest.mark.parametrize('failure', ['timeout', 'network', '429'])
def test_repair_has_one_actual_attempt_for_retryable_errors(native_engine, tmp_path, monkeypatch, failure):
    scope = selection(native_engine, 'stock_daily', {'item': [bar(DAY)], 'adjust': 'none'}, day=DAY)
    response = requests.Timeout() if failure == 'timeout' else requests.ConnectionError() if failure == 'network' else http_response(status=429)
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [response])
    assert code == 2 and get.call_count == 1
    assert record['counts']['http_attempts'] == 1
    assert current(native_engine, scope)[0] == scope.selection.observation_id


@pytest.mark.parametrize('allowance', [0, 8])
def test_actual_http_budget_rejects_zero_or_ninth_send(native_engine, monkeypatch, allowance):
    scope = selection(native_engine, 'stock_daily', {'item': [bar(DAY)]}, day=DAY)
    monkeypatch.setattr(transport, '_gate', transport._RequestGate())
    get = Mock(return_value=http_response({'item': [bar(DAY)]}))
    monkeypatch.setattr(transport.requests, 'get', get)
    client = transport.TonghuashunClient('https://example.test', 'invented-private-key', interval_ms=0)
    interface, parameters = scope.selection.allowed_requests()[0]
    with execution(scope, allowance) as limit:
        for _ in range(allowance): client.request(interface, parameters)
        with pytest.raises(CollectionYield):
            client.request(interface, parameters)
        assert limit.counts['http_attempts'] == allowance
    assert get.call_count == allowance
    assert not transport._gate.lock.locked()


def test_cas_conflict_preserves_competing_head_and_never_claims_old_or_other_publication(native_engine, tmp_path, monkeypatch):
    scope = selection(native_engine, 'stock_daily', {'item': [bar(DAY)], 'adjust': 'none'}, day=DAY)
    competing = {'item': [bar(DAY, '9')], 'adjust': 'none'}
    def raced(_url, **_kwargs):
        publish(native_engine, 'stock_daily', competing, expected=1)
        return http_response({'item': [bar(DAY, '2')]})
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, raced)
    assert code == 2 and get.call_count == 1
    assert record['outcome'] == 'blocked' and record['unknown_publication'] is True
    assert current(native_engine, scope)[2] == competing
    with Session(native_engine) as session:
        assert len(list(session.scalars(select(TonghuashunObservation)))) == 2


def test_unexpected_handler_error_keeps_publication_unknown_even_if_old_head_is_visible(native_engine, tmp_path, monkeypatch):
    from dataclasses import replace
    scope = selection(native_engine, 'stock_daily', {'item': [bar(DAY)]}, day=DAY)
    def unknown_commit(_context, _parameters):
        raise RuntimeError('invented reflected secret must not appear')
    definition = task_registry.require(scope.task_type)
    monkeypatch.setitem(task_registry._definitions, scope.task_type, replace(definition, handler=unknown_commit))
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [])
    assert code == 2 and record['outcome'] == 'worker_error' and record['unknown_publication']
    assert get.call_count == 0 and 'reflected secret' not in json.dumps(record)


@pytest.mark.parametrize('active', ['default', 'dated', 'different_subject'])
def test_default_admission_only_blocks_actually_overlapping_registered_work(native_engine, active):
    scope = selection(native_engine, 'stock_daily', {'item': [bar(DAY)]}, day=DAY)
    parameters = {'subjects': [STOCK]}
    if active == 'dated': parameters.update(start_date='2026-09-28', end_date='2026-09-30')
    elif active == 'different_subject': parameters['subjects'] = ['000888.SZ']
    with native_engine.begin() as connection:
        connection.execute(text("INSERT INTO task_runs VALUES (:type, 'running', :parameters)"),
                           {'type': scope.task_type, 'parameters': json.dumps(parameters)})
    if active == 'default':
        with pytest.raises(bounded.AdmissionRejected, match='collector_in_flight'):
            with bounded.admission(native_engine, scope): pytest.fail('overlapping default admitted')
    else:
        with bounded.admission(native_engine, scope): pass


def test_existing_repair_checkpoint_is_not_replayed_as_fresh_confirmation(native_engine, tmp_path, monkeypatch):
    scope = selection(native_engine, 'stock_daily', {'item': [bar(DAY)]}, day=DAY)
    with Session(native_engine) as session:
        session.add(TonghuashunWorkUnit(scope=bounded._scope_hash(scope), request_key='invented', data_json='{}', created_at=NOW))
        session.commit()
    code, record, get = worker(native_engine, scope, tmp_path, monkeypatch, [])
    assert code == 2 and record['problem'] == 'checkpoint_exists' and get.call_count == 0


def test_unknown_financial_cli_plan_and_arbitrary_variant_never_start_child(tmp_path, monkeypatch):
    value = synthetic_scopes()[-1].selection.as_dict()
    value['selector']['start_date'] = None
    path = tmp_path / 'unknown-plan.json'
    path.write_text(json.dumps([value]))
    monkeypatch.setattr(bounded, 'supervise', lambda *_args, **_kwargs: pytest.fail('unknown selector started child'))
    assert bounded_batch.main(['--repair-plan', str(path), '--output', str(tmp_path / 'output')]) == 2
    assert not (tmp_path / 'output').exists()
    value = synthetic_scopes()[0].selection.as_dict()
    value['variant'] = 'different'
    with pytest.raises(ValueError):
        bounded.RepairScope.from_dict(value)


def test_transport_rejects_unapproved_interface_or_parameters_before_http(native_engine, monkeypatch):
    scope = selection(native_engine, 'stock_daily', {'item': [bar(DAY)]}, day=DAY)
    get = Mock(side_effect=AssertionError('unapproved HTTP sent'))
    monkeypatch.setattr(transport.requests, 'get', get)
    client = transport.TonghuashunClient('https://example.test', 'invented-private-key', interval_ms=0)
    interface, parameters = scope.selection.allowed_requests()[0]
    with execution(scope, 1):
        for requested_interface, requested_parameters in (
                ('fund.market.historical', parameters), (interface, {**parameters, 'adjust': 'forward'})):
            with pytest.raises(SourceError):
                client.request(requested_interface, requested_parameters)
    get.assert_not_called()


def synthetic_scopes():
    """Known-period invented plans only; these are not the unknown real funds."""
    values = []
    for index, dataset in enumerate(('stock_daily', 'fund_stock_history', 'fund_bond_history',
                                     'fund_financial_indicators', 'fund_income', 'fund_balance')):
        target = {'day': DAY} if index == 0 else {'end_date': END, 'report_type': 'quarter'} if index < 3 else {
            'start_date': date(2017, 1, 1), 'end_date': END,
            'selector_observation_id': uuid4(), 'selector_content_sha256': 'a' * 64}
        values.append(bounded.RepairScope(DefaultRepair(dataset, STOCK if index == 0 else f'F{index}.OF',
            'a-share' if index == 0 else 'fund-otc', 1, uuid4(), 'b' * 64, **target)))
    return values


def test_six_typed_scopes_share_840_seconds_and_eight_http_allowance(tmp_path):
    deadlines = []
    def attempt(scope, output, *, _deadline):
        deadlines.append(_deadline)
        return {'operation_id': 'synthetic', 'outcome': 'succeeded', 'process_exited': True,
                'unknown_publication': False, 'counts_complete': True, 'elapsed_seconds': 0, 'exit_code': 0,
                'counts': {'logical_requests': scope.max_requests, 'http_attempts': scope.max_http_attempts, 'reused': 0}}
    result = bounded_batch.run(synthetic_scopes(), tmp_path / 'batch', _supervise=attempt)
    assert len(deadlines) == 6 and len(set(deadlines)) == 1
    assert result['budget']['wall_seconds'] == 840 and result['budget']['http_attempts'] == 8
    assert result['budget']['retries'] == 0 and result['counts']['http_attempts'] == 8
    assert result['outcome'] == 'completed' and not result['resources_verified']


def test_repair_batch_stops_after_real_validation_failure_without_second_scope_http(native_engine, tmp_path, monkeypatch):
    first = selection(native_engine, 'stock_daily', {'item': [bar(DAY)], 'adjust': 'none'}, day=DAY)
    second = selection(native_engine, 'fund_stock_history', old_reports('fund_stock_history'),
                       end_date=END, report_type='quarter')
    attempts, sent = [], []
    def attempt(scope, output, *, _deadline):
        attempts.append(scope.dataset)
        # Exercise the registered handler and actual HTTP call site. The old
        # target is present, but this new empty response cannot confirm it.
        code, result, get = worker(native_engine, scope, tmp_path, monkeypatch,
                                   [http_response({'item': []})])
        sent.append(get.call_count)
        return {**result, 'process_exited': True, 'elapsed_seconds': 0, 'exit_code': code}
    result = bounded_batch.run([first, second], tmp_path / 'batch', _supervise=attempt)
    assert attempts == ['stock_daily'] and sent == [1]
    assert result['outcome'] == 'incomplete' and result['stopped_scope_outcome'] == 'failed'
    assert result['attempts_started'] == 1 and result['unattempted_scopes'] == 1
    assert result['results'][0]['counts'] == {'logical_requests': 1, 'http_attempts': 1, 'reused': 0}
    assert current(native_engine, first)[0] == first.selection.observation_id
    assert current(native_engine, second)[0] == second.selection.observation_id


def test_repair_batch_over_allowance_or_unknown_selector_never_starts(tmp_path):
    scopes = synthetic_scopes()
    from dataclasses import replace
    # Replacing a one-request snapshot with a distinct two-request report makes
    # nine; validation must refuse the plan before creating evidence or children.
    scopes[-1] = bounded.RepairScope(replace(scopes[1].selection, subject='F99.OF'))
    with pytest.raises(ValueError):
        bounded_batch.run(scopes, tmp_path / 'batch', _supervise=lambda *_: pytest.fail('child started'))
    assert not (tmp_path / 'batch').exists()
    scopes = synthetic_scopes()
    scopes[-1] = bounded.RepairScope(replace(scopes[-1].selection, start_date=None))
    with pytest.raises(ValueError):
        bounded_batch.run(scopes, tmp_path / 'unknown', _supervise=lambda *_: pytest.fail('child started'))
    assert not (tmp_path / 'unknown').exists()
