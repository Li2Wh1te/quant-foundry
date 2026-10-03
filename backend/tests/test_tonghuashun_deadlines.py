"""Isolated cancellation boundaries on the registered source collector path."""
from datetime import date
from decimal import Decimal
import hashlib
import os
import signal
import subprocess
import sys
import time
from uuid import uuid4
from unittest.mock import Mock

import pytest
import requests
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.scheduling.registry import task_registry
from app.data_ingestion.clients import tonghuashun as transport
from app.data_ingestion.models.tonghuashun import TonghuashunObservation, TonghuashunWorkUnit
from app.data_ingestion.scheduler_tasks import tonghuashun as scheduler
from app.data_ingestion.tonghuashun.acquisition import Acquisition
from app.data_ingestion.tonghuashun.contracts import exact_json
from app.data_ingestion.tonghuashun.control import (
    CollectionYield, ExecutionDeadline, check_execution, execution_deadline, note_response, wait_for_pacing,
)
from tests.test_tonghuashun_collections import engine, bar, reply, seed, ticker


@pytest.fixture
def owned_limit():
    cancelled = [False]
    limit = ExecutionDeadline(deadline=time.monotonic() + 180, cancelled=lambda: cancelled[0])
    token = execution_deadline.set(limit)
    try:
        yield limit, cancelled
    finally:
        execution_deadline.reset(token)


def formal_handler(engine, monkeypatch, client):
    TonghuashunWorkUnit.__table__.create(engine)
    seed(engine, [ticker()])
    monkeypatch.setattr(scheduler, 'get_engine', lambda: engine)
    monkeypatch.setattr(scheduler, 'get_settings', lambda: Mock())
    monkeypatch.setattr(transport.TonghuashunClient, 'from_settings', lambda settings: client)
    definition = task_registry.require('data.ths.etf_daily')
    parameters = definition.parameters_model.model_validate({
        'subjects': ['510300.SH'], 'asset_types': ['fund-etf'], 'mode': 'incremental',
        'start_date': '2026-09-28', 'end_date': '2026-09-30', 'batch_size': 1,
        'max_requests': 1, 'max_seconds': 180,
    })
    return lambda: definition.handler(Mock(run_id=None), parameters)


def test_last_allowed_request_can_publish_without_request_budget_recheck(engine, monkeypatch, owned_limit):
    limit, _ = owned_limit
    client = Mock(interval_ms=0)
    returned = reply([bar(date(2026, 9, 29))])
    client.request.return_value = returned
    result = formal_handler(engine, monkeypatch, client)()
    assert result['succeeded'] == 1 and client.request.call_count == 1
    assert limit.counts == {'logical_requests': 1, 'http_attempts': 0, 'reused': 0}
    assert limit.response_evidence['sha256'] == hashlib.sha256(exact_json(returned.data).encode()).hexdigest()
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TonghuashunObservation)) == 1
        assert session.scalar(select(func.count()).select_from(TonghuashunWorkUnit)) == 0


def test_response_after_cancellation_is_not_journaled_or_published(engine, monkeypatch, owned_limit):
    limit, cancelled = owned_limit
    client = Mock(interval_ms=0)
    def delayed_response(*args):
        cancelled[0] = True
        return reply([bar(date(2026, 9, 29))])
    client.request.side_effect = delayed_response
    result = formal_handler(engine, monkeypatch, client)()
    assert result['yield_reason'] == 'stopped'
    assert limit.counts['logical_requests'] == 1
    assert limit.response_evidence['row_count'] == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TonghuashunObservation)) == 0
        assert session.scalar(select(func.count()).select_from(TonghuashunWorkUnit)) == 0


def test_cancellation_after_validation_blocks_publication_but_keeps_checkpoint(engine, monkeypatch, owned_limit):
    _, cancelled = owned_limit
    original = Acquisition.fetch
    def validated_then_cancelled(self, *args):
        data = original(self, *args)
        cancelled[0] = True
        return data
    monkeypatch.setattr(Acquisition, 'fetch', validated_then_cancelled)
    client = Mock(interval_ms=0)
    client.request.return_value = reply([bar(date(2026, 9, 29))])
    result = formal_handler(engine, monkeypatch, client)()
    assert result['yield_reason'] == 'stopped'
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TonghuashunObservation)) == 0
        assert session.scalar(select(func.count()).select_from(TonghuashunWorkUnit)) == 1


def test_expired_execution_blocks_formal_request_before_network(engine, monkeypatch, owned_limit):
    limit, _ = owned_limit
    monkeypatch.setattr(time, 'monotonic', lambda: limit.deadline)
    client = Mock(interval_ms=0)
    result = formal_handler(engine, monkeypatch, client)()
    assert result['yield_reason'] == 'budget'
    assert client.request.call_count == 0
    assert limit.counts == {'logical_requests': 0, 'http_attempts': 0, 'reused': 0}


def test_provider_pacing_wait_can_be_cancelled_without_waiting_full_cooldown(monkeypatch, owned_limit):
    _, cancelled = owned_limit
    sleeps = []
    def interrupt(seconds):
        sleeps.append(seconds)
        cancelled[0] = True
    monkeypatch.setattr(time, 'sleep', interrupt)
    with pytest.raises(CollectionYield) as error:
        wait_for_pacing(60)
    assert error.value.reason == 'stopped' and sleeps == [.1]


def test_transport_cancel_between_retries_preserves_exact_http_count(monkeypatch, owned_limit):
    limit, cancelled = owned_limit
    client = transport.TonghuashunClient('https://provider.invalid', 'fictional-test-secret', interval_ms=0)
    monkeypatch.setattr(transport, '_gate', Mock(next_start=0))
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    def first_attempt(*args, **kwargs):
        cancelled[0] = True
        raise requests.Timeout('fictional-reflected-secret')
    get = Mock(side_effect=first_attempt)
    monkeypatch.setattr(requests, 'get', get)
    with pytest.raises(CollectionYield) as error:
        client.request('a-share-index.prices.historical', {'thscode': '000300.SH'})
    assert error.value.reason == 'stopped'
    assert get.call_count == limit.counts['http_attempts'] == 1


def test_true_numeric_response_projection_is_bounded_and_excludes_reflected_strings(owned_limit):
    limit, _ = owned_limit
    secret = 'fictional-reflected-credential'
    row = {**bar(date(2026, 9, 29)), 'volume': secret, 'turnover': Decimal('-1.2300'),
           'thscode': secret, 'api_key': secret}
    data = {'item': [row] * 10, 'api_key': secret, 'url': secret}
    note_response(data, {'thscode': '510300.SH'})
    evidence = limit.response_evidence
    assert evidence['row_count'] == 10 and len(evidence['numeric_samples']) == 5
    assert evidence['full_payload_exported'] is False and secret not in exact_json(evidence)
    sample = evidence['numeric_samples'][0]
    assert sample['numeric_fields']['volume'] == {'type': 'other', 'value_omitted': True}
    assert sample['numeric_fields']['turnover'] == {'type': 'decimal', 'value_text': '-1.2300'}
    assert sample['subject_matches_request'] is False
    assert len(exact_json(evidence).encode()) < 4096


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -float('inf')])
def test_non_finite_execution_deadline_is_rejected(invalid):
    with pytest.raises(ValueError):
        ExecutionDeadline(invalid, lambda: False)


def test_unbounded_scheduler_path_does_not_acquire_new_deadline_semantics():
    token = execution_deadline.set(None)
    try:
        check_execution()
    finally:
        execution_deadline.reset(token)


@pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL")
def test_postgresql_admission_releases_source_gate_but_retains_only_owned_scope_lease(engine):
    from app.data_ingestion.tonghuashun import bounded
    from app.data_sources.models import DataSourceConfig
    # The inherited engine fixture owns a fresh schema; no deployed data,
    # configuration or account-wide vendor pacing is involved in this check.
    DataSourceConfig.__table__.create(engine)
    TonghuashunWorkUnit.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE task_runs (task_type TEXT, status TEXT, parameters JSONB)"))
    scope = bounded.Scope('index_daily', '900000CNY01.SH', date(2026, 9, 1), date(2026, 9, 3))
    seed(engine, [ticker(scope.subject, scope.asset_type)])
    with Session(engine) as session:
        session.add(DataSourceConfig(key='tonghuashun', initialized=True, enabled=True,
            encrypted_secrets='invented-test-ciphertext', values={}, version=7))
        session.commit()
    # New process-owned connections must carry their finite SQL bounds/tag.
    engine.dispose()
    operation_id = str(uuid4())
    bounded._tag_engine(engine, operation_id)
    key = int.from_bytes(hashlib.sha256(('ths-bounded:' + bounded._scope_hash(scope)).encode()).digest()[:8], 'big') & ((1 << 63) - 1)
    with bounded.admission(engine, scope):
        with engine.begin() as other:
            assert other.scalar(text("SELECT current_setting('application_name')")) == 'ths-bounded:' + operation_id
            assert other.scalar(text("SELECT current_setting('statement_timeout')")) == '5s'
            assert other.scalar(text("SELECT current_setting('lock_timeout')")) == '5s'
            assert other.scalar(text("SELECT pg_try_advisory_lock(:key)"), {'key': key}) is False
            # The source configuration gate is not held over a network call.
            assert other.scalar(text("SELECT version FROM data_source_configs WHERE key='tonghuashun' FOR UPDATE NOWAIT")) == 7
    with engine.begin() as other:
        assert other.scalar(text("SELECT pg_try_advisory_lock(:key)"), {'key': key}) is True
        assert other.scalar(text("SELECT pg_advisory_unlock(:key)"), {'key': key}) is True


@pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL")
def test_hard_supervisor_kill_releases_its_live_postgresql_session_and_lease(tmp_path):
    from app.core.config import get_settings
    from app.data_ingestion.tonghuashun import bounded
    operation_id = str(uuid4())
    tag = 'ths-bounded:' + operation_id
    marker = tmp_path / 'connected'
    scope = bounded.Scope('index_daily', '900000CNY01.SH', date(2026, 9, 1), date(2026, 9, 3))
    script = """import os,signal
from pathlib import Path
from sqlalchemy import create_engine,text
from app.data_ingestion.tonghuashun.bounded import _tag_engine
signal.signal(signal.SIGTERM,signal.SIG_IGN)
engine=create_engine(os.environ['QF_BOUNDED_FIXTURE_DB_URL'])
_tag_engine(engine,os.environ['QF_BOUNDED_FIXTURE_OPERATION'])
with engine.begin() as connection:
    connection.execute(text('SELECT pg_advisory_xact_lock(912735468)'))
    Path(os.environ['QF_BOUNDED_FIXTURE_MARKER']).write_text('connected')
    connection.execute(text('SELECT pg_sleep(30)'))
"""
    children = []
    def launcher(_command, **options):
        # This is the disposable test database's invented password, conveyed
        # only to our owned child; no deployed credential is read or logged.
        fixture_env = {**os.environ, 'QF_BOUNDED_FIXTURE_DB_URL':get_settings().database_url.render_as_string(hide_password=False),
            'QF_BOUNDED_FIXTURE_OPERATION':operation_id, 'QF_BOUNDED_FIXTURE_MARKER':str(marker)}
        child = subprocess.Popen([sys.executable, '-c', script], env=fixture_env, **options)
        children.append(child)
        return child
    admin = create_engine(get_settings().database_url)
    try:
        record = bounded.supervise(scope, tmp_path / 'killed-sql', _budget=3, _popen=launcher)
        assert marker.read_text() == 'connected'
        assert record['process_exited'] and record['kill_sent']
        assert record['exit_code'] == -signal.SIGKILL
        assert record['unknown_publication'] and not record['counts_complete']
        # Process exit is necessary but a DB read supplies the cleanup proof.
        with admin.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout='3s'"))
            # An active server query may survive its killed TCP client until
            # the owned five-second statement bound fires. A finite readback
            # is required; process exit alone cannot establish lock release.
            for _ in range(140):
                remaining = connection.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE application_name=:tag'), {'tag':tag})
                if remaining == 0:
                    break
                time.sleep(.05)
                connection.execute(text('SELECT pg_stat_clear_snapshot()'))
            assert remaining == 0
            assert connection.scalar(text('SELECT pg_try_advisory_xact_lock(912735468)')) is True
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        admin.dispose()
