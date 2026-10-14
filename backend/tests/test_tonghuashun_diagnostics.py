"""Synthetic business originals; no real supplier, deployed DB or secrets."""
from contextlib import contextmanager, redirect_stderr
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import importlib.util
import io
import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
import structlog

from app.core.config import Settings
from app.core.logging import configure_logging
from app.data_ingestion.clients import tonghuashun as transport
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from app.data_ingestion.models.tonghuashun import TonghuashunWorkUnit
from app.data_ingestion.tonghuashun import diagnostics as d
from app.data_ingestion.tonghuashun.acquisition import Acquisition
from app.data_ingestion.tonghuashun.contracts import CollectionError, CollectionParameters, DATASETS
from app.data_ingestion.tonghuashun.control import CollectionControl, active_control
from app.data_ingestion.tonghuashun.research_acquisition import Unit, series
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.service import collect
from tests.test_tonghuashun_collections import engine, seed, ticker, NOW

RUN = '11111111-2222-4333-8444-555555555555'
KEY = 'synthetic-api-key-for-unit-tests'
BUSINESS = 'SYNTHETIC.OF'


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv('QF_THS_DIAGNOSTIC_RUN_IDS', raising=False)
    monkeypatch.delenv('QF_THS_DIAGNOSTIC_UNTIL', raising=False)
    monkeypatch.setattr(transport, '_gate', transport._RequestGate())


@pytest.fixture
def events(monkeypatch):
    result = []
    monkeypatch.setattr(d, '_emit', lambda event, fields, warning=False: result.append((event, dict(fields))))
    return result


@contextmanager
def run(monkeypatch, dataset='fund_income', capture=True):
    if capture:
        monkeypatch.setenv('QF_THS_DIAGNOSTIC_RUN_IDS', RUN)
        monkeypatch.setenv('QF_THS_DIAGNOSTIC_UNTIL', (datetime.now(UTC)+timedelta(minutes=5)).isoformat())
    token = d.begin_run(dataset, RUN)
    d.begin_object(dataset, 7)
    try:
        yield
    finally:
        d.end_run(token)


class Response:
    def __init__(self, raw, status=200):
        self.raw, self.status_code = raw, status
        self.headers = {'Set-Cookie': 'header-only-secret'}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def iter_content(self, size): yield self.raw


def request(monkeypatch, raw, status=200):
    get = Mock(return_value=Response(raw, status))
    monkeypatch.setattr(transport.requests, 'get', get)
    client = transport.TonghuashunClient('https://example.test', KEY, interval_ms=0, max_attempts=1)
    return client, get


def test_exact_business_original_and_whitespace_kept_on_validation_failure(monkeypatch, events):
    raw = b'{ "code":0, "data":{"item":[],"business_symbol":"SYNTHETIC.OF","complete":false} }\n'
    client, get = request(monkeypatch, raw)
    with run(monkeypatch), pytest.raises(CollectionError):
        Acquisition(client).fetch(DATASETS['fund_income'], BUSINESS, CollectionParameters(), None, NOW)
    fields = events[0][1]
    assert get.call_count == 1 and fields['raw_response'] == raw.decode()
    assert fields['business_parameters'] == {'thscode': BUSINESS}
    assert fields['http_status'] == 200 and fields['business_code'] == 0
    assert fields['complete'] is False and fields['checkpoint_advanced'] is None


def test_empty_income_preserves_last_good_and_failed_checkpoint(monkeypatch, events, engine):
    TonghuashunWorkUnit.__table__.create(engine, checkfirst=True)
    seed(engine, [ticker(BUSINESS, 'fund-otc')])
    with Session(engine) as session:
        CollectionRepository(session).publish('fund_income', BUSINESS, 'default', expected=0,
            data={'item':[{'total_income':Decimal('12.3')}]}, requests=[], now=NOW)
        session.commit()
        before = CollectionRepository(session).read('fund_income', BUSINESS, 'default')
    client, get = request(monkeypatch, b'{ "code":0, "data":{"item":[]} }\n')
    token = active_control.set(CollectionControl(engine, None))
    try:
        with run(monkeypatch), pytest.raises(CollectionError):
            collect('fund_income', CollectionParameters(subjects=[BUSINESS]), client, engine, now=NOW)
    finally:
        active_control.reset(token)
    assert get.call_count == 1
    with Session(engine) as session:
        after = CollectionRepository(session).read('fund_income', BUSINESS, 'default')
        assert after.status == 'failed' and after.succeeded_at == before.succeeded_at
        assert after.data == before.data
        assert len(session.scalars(select(Observation)).all()) == 1
    outcome = next(f for e,f in events if e == 'tonghuashun_object_diagnostic')
    assert outcome['error_branch'] == 'empty_record' and outcome['checkpoint_advanced'] is False
    assert outcome['source_revision'] == 1 and 'raw_response' not in outcome


def test_success_keeps_one_request_and_normal_publication(monkeypatch, events, engine):
    TonghuashunWorkUnit.__table__.create(engine, checkfirst=True)
    seed(engine, [ticker(BUSINESS, 'fund-otc')])
    raw = b'{"code":0,"data":{"item":[{"total_income":12.3}]}}'
    client, get = request(monkeypatch, raw)
    token = active_control.set(CollectionControl(engine, None))
    try:
        with run(monkeypatch):
            result = collect('fund_income', CollectionParameters(subjects=[BUSINESS]), client, engine, now=NOW)
    finally:
        active_control.reset(token)
    assert get.call_count == 1 and result['succeeded'] == 1
    outcome = next(f for e,f in events if e == 'tonghuashun_object_diagnostic')
    assert outcome['checkpoint_advanced'] is True and outcome['published_version'] is not None
    with Session(engine) as session:
        assert CollectionRepository(session).read('fund_income', BUSINESS, 'default').status == 'succeeded'


def test_history_response_logged_before_all_empty_failure(monkeypatch, events):
    raw = b'{"code":0,"data":{"item":[],"has_more":false}}'
    client, get = request(monkeypatch, raw)
    with run(monkeypatch, 'fund_performance_history'), pytest.raises(CollectionError):
        series(Acquisition(client), DATASETS['fund_performance_history'], Unit(BUSINESS, {}),
            CollectionParameters(start_date=NOW.date()-timedelta(days=2), end_date=NOW.date()-timedelta(days=1)), None, NOW)
    assert get.call_count == 1 and len(events) == 1
    fields = events[0][1]
    assert fields['item_empty'] is True and fields['has_more'] is False
    assert fields['complete'] is None  # not a fabricated coverage guarantee
    assert fields['business_parameters']['thscode'] == BUSINESS


@pytest.mark.parametrize('status,raw,kind', [
    (403, b'{"code":2003,"message":"permission denied","data":null}', 'forbidden'),
    (200, b'{"code":1002,"message":"bad business parameter","data":null}', 'invalid_parameters'),
    (200, b'not json: business data', 'invalid_response'),
])
def test_failed_transport_keeps_original_without_extra_request(monkeypatch, events, status, raw, kind):
    client, get = request(monkeypatch, raw, status)
    with run(monkeypatch), pytest.raises(transport.TonghuashunError) as caught:
        client.request('fund.financials.income-statements', {'thscode':BUSINESS})
    assert caught.value.kind == kind and get.call_count == 1
    fields = events[0][1]
    assert fields['http_status'] == status and fields['error_branch'] == kind
    assert fields['raw_response'] == raw.decode()
    assert fields['raw_response_format'] == ('text' if raw.startswith(b'not') else 'json')


def test_credentials_filtered_while_business_fields_retained(monkeypatch, events):
    raw = json.dumps({'code':0, 'data':{'item':[], 'symbol':BUSINESS,
        'password':'fixture-password', 'access_token':'fixture-token',
        'nested':{'Cookie':'sid=fixture-cookie; other=fixture-cookie2'},
        'note':KEY, 'url':'postgresql://fixture:fixture-pass@example.test/db?token=fixture-query'}}).encode()
    client, get = request(monkeypatch, raw)
    with run(monkeypatch):
        client.request('fund.financials.income-statements', {'thscode':BUSINESS,'token':'fixture-param-secret'})
    payload = json.dumps(events, ensure_ascii=False)
    for secret in [KEY,'fixture-password','fixture-token','fixture-cookie','fixture-pass',
                   'fixture-query','fixture-param-secret','header-only-secret']:
        assert secret not in payload
    fields = events[0][1]
    assert BUSINESS in fields['raw_response'] and fields['business_parameters']['thscode'] == BUSINESS
    assert fields['credential_redactions'] > 0 and get.call_count == 1


@pytest.mark.parametrize('separator', [b':', b'='])
def test_non_json_cookie_and_auth_lines_filtered(monkeypatch, events, separator):
    raw = b'business=SYNTHETIC.OF\nCookie'+separator+b' first=fixture-one; second=fixture-two\nAuthorization'+separator+b' Bearer fixture-auth\n'
    client, _ = request(monkeypatch, raw)
    with run(monkeypatch), pytest.raises(transport.TonghuashunError):
        client.request('fund.financials.income-statements', {'thscode':BUSINESS})
    text = events[0][1]['raw_response']
    assert BUSINESS in text and all(x not in text for x in ['fixture-one','fixture-two','fixture-auth'])


def test_credential_removal_preserves_decimal_business_values(monkeypatch, events):
    raw = b'{"code":0,"data":{"item":[{"total_income":0.123456789012345678901}],"token":"fixture-token"}}'
    client, _ = request(monkeypatch, raw)
    with run(monkeypatch):
        client.request('fund.financials.income-statements', {'thscode':BUSINESS})
    body = events[0][1]['raw_response']
    assert '0.123456789012345678901' in body and 'fixture-token' not in body
    assert json.loads(body, parse_float=Decimal)['data']['item'][0]['total_income'] == Decimal('0.123456789012345678901')


def test_default_metadata_only_and_context_reset(monkeypatch, events):
    client, get = request(monkeypatch, b'{"code":0,"data":{"item":[]}}')
    with run(monkeypatch, capture=False):
        client.request('fund.financials.income-statements', {'thscode':BUSINESS})
    assert get.call_count == 1 and len(events) == 1
    assert events[0][1]['raw_response_state'] == 'disabled'
    assert not d.LOCAL_FIELDS.intersection(events[0][1])
    assert d.begin_request() is None


@pytest.mark.parametrize('configuration', ['expired','too_long','too_many','wrong_run'])
def test_capture_requires_exact_run_and_short_expiry(monkeypatch, events, configuration):
    until, ids = datetime.now(UTC)+timedelta(minutes=5), RUN
    if configuration == 'expired': until -= timedelta(minutes=10)
    if configuration == 'too_long': until += timedelta(hours=1)
    if configuration == 'too_many': ids = ','.join([RUN]*3)
    if configuration == 'wrong_run': ids = '99999999-2222-4333-8444-555555555555'
    monkeypatch.setenv('QF_THS_DIAGNOSTIC_RUN_IDS', ids)
    monkeypatch.setenv('QF_THS_DIAGNOSTIC_UNTIL', until.isoformat())
    client, _ = request(monkeypatch, b'{"code":0,"data":{"item":[]}}')
    with run(monkeypatch, capture=False):
        client.request('fund.financials.income-statements', {'thscode':BUSINESS})
    assert events[0][1]['raw_response_state'] == 'disabled'


@pytest.mark.parametrize('limit_field,state', [('MAX_BODY_BYTES','omitted_size_limit'),('MAX_RUN_BYTES','omitted_run_size_limit')])
def test_omissions_explicit_and_no_silent_truncation(monkeypatch, events, limit_field, state):
    monkeypatch.setattr(d, limit_field, 1)
    client, _ = request(monkeypatch, b'{"code":0,"data":{"item":[]}}')
    with run(monkeypatch):
        client.request('fund.financials.income-statements', {'thscode':BUSINESS})
    fields = events[0][1]
    assert fields['raw_response_state'] == state and 'raw_response' not in fields
    assert fields['raw_response_bytes'] > 1


def test_originals_file_only_and_ambient_context_excluded(monkeypatch, tmp_path):
    console = io.StringIO()
    settings = Settings(api_token='a'*64, database_password='fixture-db-secret', log_dir=tmp_path, _env_file=None)
    structlog.contextvars.bind_contextvars(authorization='ambient-secret', subject='ambient-symbol', raw_response='ambient-body')
    try:
        with redirect_stderr(console):
            runtime = configure_logging(settings)
            client, _ = request(monkeypatch, b'{ "code":0, "data":{"item":[],"business":"SYNTHETIC.OF"} }')
            with run(monkeypatch):
                client.request('fund.financials.income-statements', {'thscode':BUSINESS})
            runtime.stop()
        records = [json.loads(line) for line in runtime.log_file.read_text().splitlines()]
        assert len(records) == 1 and BUSINESS in records[0]['raw_response']
        assert records[0]['business_parameters'] == {'thscode':BUSINESS}
        stderr = json.loads(console.getvalue())
        assert not d.LOCAL_FIELDS.intersection(stderr)
        assert all(x not in runtime.log_file.read_text()+console.getvalue()
                   for x in ['ambient-secret','ambient-symbol','ambient-body'])
    finally:
        structlog.contextvars.clear_contextvars()
        logging.getLogger().handlers.clear()


def test_unconfigured_logging_never_sends_originals_to_arbitrary_handler(monkeypatch):
    from app.core import logging as log_config
    monkeypatch.setattr(log_config, 'get_logging_runtime', lambda: None)
    seen = []
    class Capture(logging.Handler):
        def emit(self, record): seen.append(record._ths_diagnostic)
    logger = logging.getLogger(d.__name__)
    old_handlers, old_propagate, old_level = logger.handlers[:], logger.propagate, logger.level
    logger.handlers = [Capture()]; logger.propagate = False; logger.setLevel(logging.INFO)
    try:
        d._emit('tonghuashun_request_diagnostic', {'raw_response':BUSINESS,
                'business_parameters':{'thscode':BUSINESS}, 'run_id':RUN}, warning=True)
        assert len(seen) == 1 and not d.LOCAL_FIELDS.intersection(seen[0])
        assert seen[0]['raw_response_state'] == 'omitted_local_file_channel_unavailable'
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate
        logger.setLevel(old_level)


def test_r01_overlay_rejects_unverified_base_without_partial_output(tmp_path):
    path = Path(__file__).resolve().parents[2]/'scripts/r01_diagnostic_overlay.py'
    spec = importlib.util.spec_from_file_location('overlay', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    source, output = tmp_path/'source', tmp_path/'output'
    source.mkdir()
    target = source/next(iter(module.BASE_HASHES))
    target.parent.mkdir(parents=True); target.write_text('unapproved source')
    with pytest.raises(ValueError, match='hash mismatch'):
        module.prepare(source, output, 'sha256:'+'1'*64)
    assert not output.exists()
