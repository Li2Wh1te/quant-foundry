"""Default factories against private PG, existing SQL facts and CurrentStore.

No success provider, AcceptedRunPlan or accepted-facts flag is installed. The
current source semantics cannot execute a run; these tests prove the existing
bridges and precise default refusal, independently of synthetic worker tests.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import importlib
import os
import json
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import uvicorn
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

if os.getenv('QF_S3_INTEGRATION_REQUIRED') == '1':
    import quantfoundry
else:
    quantfoundry = pytest.importorskip('quantfoundry', reason='installed S3 wheel required')

from app.backtest_service.formal_resolver import SqlFormalPlanResolver
from app.backtest_service.runtime_contracts import AcceptedWork, Claim, RuntimeFault, WorkerLimits
from app.backtest_service.runtime_services import create_services
from app.backtest_service.service import FormalProvider, RunService
from app.backtest_service.repository import DatabaseRunRepository
from app.backtest_service.models import Run
from app.core.config import Settings
from app.main import create_app
from app.db.base import Base
from app.db.session import get_db_session
from app.instruments.domain import InstrumentCodeMapping, InstrumentIdentityFact, VersionedReference
from app.instruments.identity_repository import InstrumentIdentityRepository, InstrumentIdentityFactRepository
from app.instruments.repository import InstrumentCodeMappingRepository
from app.instruments.rule_facts_repository import RuleFactsRepository
from app.instruments.models import InstrumentIdentityFactRecord, InstrumentCodeMappingRecord
from app.instruments.rule_facts_models import InstrumentRuleFactRecord
from app.calendars.facts import CalendarSessionFact
from app.calendars.repository import CalendarFactRepository
from app.calendars.models import CalendarSessionFactRecord
from app.data_ingestion.models.corporate_action import CorporateActionCoverageFact, CorporateActionFact
from app.data_ingestion.models.trading_calendar import TradingStatusCoverageFact
from app.db.migrations.named_calendar_seed import common_provenance, REGISTRY_FACT_IDS, DEFINITION_FACT_IDS
from tests.test_s3_data_gateway import database, limits, store, formal
from tests.test_instrument_rule_facts import make_candidate

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')
DAY = date(2026, 7, 6)
KNOWN = datetime(2026, 1, 1, tzinfo=UTC)
TOKEN = 'isolated-default-api-token-thirty-two-characters'
CURSOR = 'isolated-default-cursor-key-thirty-two-characters'


@pytest.fixture
def formal_facts(formal):
    engine = formal.catalog.engine
    # Only this test's random schema. Reuse the real bootstrap migration;
    # identities, daily sessions, rules and coverage still need explicit rows.
    with engine.begin() as connection:
        schema = connection.scalar(text('SELECT current_schema()'))
        assert schema.startswith('lfd01_test_')
        connection.execute(text('CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public'))
        connection.execute(text(f'SET LOCAL search_path={schema},public'))
        # Public contains the shared test DB migrations; none of those tables
        # may satisfy this random schema's CREATE/has_table checks.
        Base.metadata.create_all(connection, checkfirst=False)
        migration = importlib.import_module('app.db.migrations.versions.20260829_01_add_named_calendar_facts')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
    return formal


def seed(formal, *, identity=True, calendar=True, rules=True, coverage=True,
         suspension=True, known_at=KNOWN, fixture_only=False, symbol='510001.SH'):
    with Session(formal.catalog.engine) as session:
        iid = InstrumentIdentityRepository(session).create_identity(asset_class='etf')
        session.flush()
        InstrumentCodeMappingRepository(session).add_mapping(InstrumentCodeMapping(
            instrument_id=iid, source='etf_ingestion', source_code=symbol,
            trading_code=symbol, mapping_source='isolated-sql-fixture',
            valid_from=date(2020, 1, 1), known_at=known_at, observed_at=known_at,
            evidence='isolated-code-mapping-evidence', logical_fact_key='mapping-' + uuid4().hex))
        if identity:
            InstrumentIdentityFactRepository(session).append_fact(InstrumentIdentityFact(
                instrument_id=iid, fact_version=1, asset_class='etf', exchange='SSE', currency='CNY',
                calendar_id='SSE', valid_from=date(2020, 1, 1), known_at=known_at, observed_at=known_at,
                evidence='isolated-identity-evidence'))
        if calendar:
            provenance = common_provenance(content_hash=None)
            provenance.update(known_at=known_at, observed_at=known_at, knowledge_from=known_at,
                              knowledge_as_of=known_at)
            CalendarFactRepository(session).append_session_fact(CalendarSessionFact(
                calendar_id='SSE', session_date=DAY, is_open=True, definition_version='sse-bootstrap-v1',
                registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']), registry_version=1,
                definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']), **provenance))
        if rules:
            RuleFactsRepository(session).append_fact(make_candidate(instrument_id=iid,
                fact_reference=VersionedReference(key='isolated-' + uuid4().hex, version=1),
                content_hash=None, fixture_only=fixture_only,
                known_at=known_at, observed_at=known_at))
        if coverage:
            session.add(CorporateActionCoverageFact(id=uuid4(), instrument_id=iid,
                start_date=DAY, end_date=DAY, status='complete', event_count=0, action_type='cash_dividend',
                source='isolated', source_revision='synthetic-sql-fixture-v1', known_at=known_at,
                observed_at=known_at, evidence=dict(reference='isolated-complete-zero'),
                validation_rule='isolated-complete-window@1'))
        if suspension:
            session.add(TradingStatusCoverageFact(id=uuid4(), instrument_id=iid, dimension='suspension',
                start_date=DAY, end_date=DAY, status='complete', event_count=0, source='isolated',
                source_revision='synthetic-sql-fixture-v1', known_at=known_at, observed_at=known_at,
                evidence=dict(reference='isolated-zero-suspension'), validation_rule='isolated-status-window@1'))
        session.commit()
    return iid


def work(symbol='510001.SH'):
    return AcceptedWork('isolated-default-owner', dict(universe=[symbol], frequency='1d',
        start=DAY.isoformat(), end=DAY.isoformat()), '', WorkerLimits(), {})


def calendar_month(formal):
    # Explicit small fixture calendar, not a production weekend/holiday rule.
    open_days = {date(2026, 7, d) for d in (1, 2, 3, 6, 7, 8, 9, 10, 13, 14, 15,
                 16, 17, 20, 21, 22, 23, 24, 27, 28, 29, 30, 31)}
    with Session(formal.catalog.engine) as session:
        for i in range(31):
            day = date(2026, 7, 1) + timedelta(days=i)
            if day == DAY:
                continue
            CalendarFactRepository(session).append_session_fact(CalendarSessionFact(
                calendar_id='SSE', session_date=day, is_open=day in open_days,
                definition_version='sse-bootstrap-v1', registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']),
                registry_version=1, definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']),
                **common_provenance(content_hash=None)))
        session.commit()


def statuses(facts):
    return {check['binding']: check for check in facts.checks}


@contextmanager
def http_api(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs=dict(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                pytest.fail('isolated real API failed to start')
            time.sleep(0.01)
        def request(method, path, payload=None):
            req = Request(url + path, method=method, headers=dict(
                Authorization='Bearer ' + TOKEN, **{'Content-Type': 'application/json',
                    'Idempotency-Key': 'isolated-default-' + uuid4().hex}),
                data=json.dumps(payload).encode() if payload is not None else None)
            try:
                response = urlopen(req, timeout=10)
            except HTTPError as response:
                body = response.read().decode()
                return SimpleNamespace(status_code=response.code, text=body, json=lambda: json.loads(body))
            with response:
                body = response.read().decode()
                return SimpleNamespace(status_code=response.status, text=body, json=lambda: json.loads(body))
        yield request
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def test_default_provider_reads_supported_contracts_and_keeps_undefined_semantics_refused(formal_facts):
    iid = seed(formal_facts)
    provider = FormalProvider(SimpleNamespace(environment='test'), store=formal_facts)
    assert isinstance(provider.plan_resolver, SqlFormalPlanResolver)
    facts = provider.plan_resolver.resolve_facts(work())
    for name in ('identity', 'calendar_sessions', 'instrument_rule_package', 'suspension_facts',
                 'corporate_actions', 'official_market_rules'):
        assert statuses(facts)[name]['available'], statuses(facts)
    assert facts.identities[0][2].instrument_id == iid
    assert facts.calendars[0][2]['intervals'][0][0].hour == 9
    assert facts.rule_packages[0][2].normalized_values['lot_size'] == Decimal('100')
    assert facts.corporate_actions[0][2]['coverage'][0]['event_count'] == 0
    assert facts.corporate_actions[0][2]['events'] == ()
    assert facts.official_rules and all(rule['origin']['kind'] == 'official' for rule in facts.official_rules)
    assert statuses(facts)['E70']['status'] == 'field_semantics_missing'
    assert statuses(facts)['execution_trade_days']['status'] == 'field_semantics_missing'
    assembly = statuses(facts)['execution_plan_assembly']
    assert assembly['status'] == 'fact_missing' and assembly['scope']['adapter'] == 'implemented'
    assert 'resolved_market_projection' in assembly['missing']
    assert statuses(facts)['research_E69']['scope']['requirement'] == 'optional_per_capability'
    with pytest.raises(RuntimeFault) as failure:
        provider._plan(work())
    assert json.loads(failure.value.diagnostic()['scope']['facts']) == list(facts.checks)


@pytest.mark.parametrize('missing', ['identity', 'calendar', 'rules', 'coverage', 'suspension'])
def test_each_missing_existing_fact_is_distinct_from_missing_source_semantics(formal_facts, missing):
    seed(formal_facts, **{missing: False})
    facts = FormalProvider(SimpleNamespace(), store=formal_facts).plan_resolver.resolve_facts(work())
    name = dict(identity='identity', calendar='calendar_sessions', rules='instrument_rule_package',
                coverage='corporate_actions', suspension='suspension_facts')[missing]
    assert statuses(facts)[name]['status'] == 'fact_missing', statuses(facts)
    assert statuses(facts)['E70']['status'] == 'field_semantics_missing'


def test_later_known_identity_and_fixture_rule_values_do_not_become_historical_facts(formal_facts):
    seed(formal_facts, known_at=datetime(2026, 7, 7, tzinfo=UTC), calendar=False)
    resolver = FormalProvider(SimpleNamespace(), store=formal_facts).plan_resolver
    assert statuses(resolver.resolve_facts(work()))['identity']['status'] == 'fact_missing'
    # Separate identity prevents the invisible row from masquerading as a
    # conflict. Formal mode rejects fixture_only even in a private test DB.
    seed(formal_facts, fixture_only=True, symbol='510002.SH')
    assert statuses(resolver.resolve_facts(work('510002.SH')))['instrument_rule_package']['status'] == 'fact_invalid'


def test_capabilities_change_with_actual_sql_facts_without_claiming_run_readiness(formal_facts):
    provider = FormalProvider(SimpleNamespace(), store=formal_facts)
    before = {check['binding']: check for check in provider.capabilities()}
    assert not before['identity']['available']
    seed(formal_facts)
    after = {check['binding']: check for check in provider.capabilities()}
    for name in ('identity', 'calendar_sessions', 'instrument_rule_package', 'suspension_facts', 'corporate_actions'):
        assert after[name]['available'], after
        assert after[name]['scope']['knowledge_profile'] == 'bounded_inventory_current_knowledge'
    assert not after['E70']['available'] and not after['execution_trade_days']['available']


def test_default_calendar_projects_actual_month_week_lunch_and_following_sessions_through_rust(formal_facts):
    seed(formal_facts)
    resolver = FormalProvider(SimpleNamespace(), store=formal_facts).plan_resolver
    missing = resolver.resolve_facts(work())
    assert statuses(missing)['execution_calendar_assembly']['status'] == 'fact_missing'
    calendar_month(formal_facts)
    facts = resolver.resolve_facts(work())
    assert statuses(facts)['execution_calendar_assembly']['available'], statuses(facts)
    projected = facts.execution_calendar
    row = projected['calendar'][0]
    assert row['week'] == dict(id='2026-07-06', trading_day=1, total_trading_days=5)
    assert row['month'] == dict(id='2026-07', trading_day=4, total_trading_days=23)
    assert projected['next_session'] == 'SSE:2026-07-07'
    assert projected['following_session']['session']['key'] == projected['next_session']
    assert projected['following_session']['date'] == '2026-07-07'
    assert projected['subsequent_session'] == 'SSE:2026-07-08'
    assert len(row['bar_windows']) == 2
    assert int(row['bar_windows'][0]['end_ns']) < int(row['bar_windows'][1]['start_ns'])
    assert not statuses(facts)['E70']['available']
    assert facts.action_projection is None
    assert statuses(facts)['execution_corporate_actions']['status'] == 'fact_missing'
    assert 'split' in statuses(facts)['execution_corporate_actions']['missing']


def test_corporate_action_bridge_preserves_quantity_cash_phase_currency_and_rejects_missing_timing(formal_facts):
    iid = seed(formal_facts)
    with Session(formal_facts.catalog.engine) as session:
        coverage = session.scalar(select(CorporateActionCoverageFact))
        coverage.event_count = 1
        event = CorporateActionFact(event_id=uuid4(), logical_fact_key='isolated-action', fact_version=1,
            instrument_id=iid, action_type='cash_dividend', record_date=DAY,
            ex_date=DAY + timedelta(days=1), cash_effective_date=DAY + timedelta(days=2),
            cash_effective_phase='after_open_match', cash_amount_per_unit=Decimal('0.123456789012'),
            quantity_ratio=Decimal('1.25'), currency='CNY', entitlement_rule='record_date_entitlement',
            cash_date_rule='tushare_fund_div_cash_date@1', timing_rule='after_open_match@1',
            valid_from=DAY + timedelta(days=1), valid_to=DAY + timedelta(days=2), source='isolated',
            source_revision='synthetic-cash-fact', known_at=KNOWN, observed_at=KNOWN,
            evidence=dict(reference='isolated-action-evidence', raw=dict(cash_amount=0.123456789012)), quality='complete')
        session.add(event)
        session.commit()
    resolver = FormalProvider(SimpleNamespace(), store=formal_facts).plan_resolver
    facts = resolver.resolve_facts(work())
    action = facts.corporate_actions[0][2]['events'][0]
    assert action['cash_effective_phase'] == 'after_open_match' and action['currency'] == 'CNY'
    assert action['cash_amount_per_unit'] == Decimal('0.123456789012')
    assert action['quantity_ratio'] == Decimal('1.25')
    assert action['evidence']['raw']['cash_amount'] == 0.123456789012
    with Session(formal_facts.catalog.engine) as session:
        session.scalar(select(CorporateActionFact)).cash_effective_phase = None
        session.commit()
    assert statuses(resolver.resolve_facts(work()))['corporate_actions']['status'] == 'fact_invalid'
    with pytest.raises(RuntimeFault) as failure:
        resolver.check_facts(work(), facts)
    assert failure.value.code == 'DATA_CHANGED'


def test_next_account_session_requires_whole_following_month_facts_and_correct_dto(formal_facts):
    seed(formal_facts)
    calendar_month(formal_facts)
    run = work()
    run.config.update(start='2026-07-31', end='2026-07-31')
    resolver = FormalProvider(SimpleNamespace(), store=formal_facts).plan_resolver
    # Explicit August fixture coverage. Do not derive holidays from weekdays
    # in the production adapter; every natural day has a real calendar row.
    open_days = {date(2026, 8, d) for d in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14,
        17, 18, 19, 20, 21, 24, 25, 26, 27, 28, 31)}
    def append_day(session, day):
        CalendarFactRepository(session).append_session_fact(CalendarSessionFact(
            calendar_id='SSE', session_date=day, is_open=day in open_days,
            definition_version='sse-bootstrap-v1', registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']),
            registry_version=1, definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']),
            **common_provenance(content_hash=None)))
    with Session(formal_facts.catalog.engine) as session:
        for d in (1, 2, 3, 4): append_day(session, date(2026, 8, d))
        session.commit()
    assert not statuses(resolver.resolve_facts(run))['execution_calendar_assembly']['available']
    with Session(formal_facts.catalog.engine) as session:
        for d in range(5, 32): append_day(session, date(2026, 8, d))
        # The Aug31 session's period is not the admitted following session;
        # its September week does not have to be manufactured or admitted.
        session.commit()
    projection = resolver.resolve_facts(run).execution_calendar
    assert projection['next_session'] == 'SSE:2026-08-03'
    assert projection['following_session']['session']['key'] == projection['next_session']
    assert projection['following_session']['month'] == dict(id='2026-08', trading_day=1, total_trading_days=21)
    assert projection['subsequent_session'] == 'SSE:2026-08-04'


def test_sql_snapshot_rejects_new_fact_or_quality_and_hash_drift(formal_facts):
    iid = seed(formal_facts)
    resolver = FormalProvider(SimpleNamespace(), store=formal_facts).plan_resolver
    accepted = resolver.resolve_facts(work())
    resolver.check_facts(work(), accepted)
    with Session(formal_facts.catalog.engine) as session:
        row = session.scalar(select(InstrumentRuleFactRecord).where(InstrumentRuleFactRecord.instrument_id == iid))
        row.fields = dict(row.fields, lot_size='200')  # corrupt this private schema only
        session.commit()
    assert statuses(resolver.resolve_facts(work()))['instrument_rule_package']['status'] == 'fact_invalid'
    with pytest.raises(RuntimeFault) as failure:
        resolver.check_facts(work(), accepted)
    assert failure.value.code == 'DATA_CHANGED'
    with formal_facts.catalog.engine.begin() as connection:
        with pytest.raises(RuntimeFault) as final_failure:
            resolver.check_facts(work(), accepted, connection=connection)
        assert final_failure.value.code == 'DATA_CHANGED'


def test_new_calendar_version_is_reselected_and_cannot_mix_with_the_accepted_projection(formal_facts):
    seed(formal_facts)
    calendar_month(formal_facts)
    resolver = FormalProvider(SimpleNamespace(), store=formal_facts).plan_resolver
    accepted = resolver.resolve_facts(work())
    with Session(formal_facts.catalog.engine) as session:
        previous = session.scalar(select(CalendarSessionFactRecord).where(CalendarSessionFactRecord.session_date == DAY))
        provenance = common_provenance(content_hash=None)
        known = datetime(2026, 7, 5, tzinfo=UTC)
        provenance.update(known_at=known, observed_at=known, knowledge_from=known, knowledge_as_of=known)
        CalendarFactRepository(session).append_session_fact(CalendarSessionFact(
            calendar_id='SSE', session_date=DAY, is_open=False, definition_version='sse-bootstrap-v1',
            fact_version=2, supersedes_fact_id=previous.fact_id,
            registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']), registry_version=1,
            definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']), **provenance))
        session.commit()
    current = resolver.resolve_facts(work())
    assert current.calendars[0][2]['fact'].fact_version == 2
    assert not statuses(current)['execution_calendar_assembly']['available']
    with pytest.raises(RuntimeFault) as failure:
        resolver.check_facts(work(), accepted)
    assert failure.value.code == 'DATA_CHANGED'


def test_calendar_candidates_are_shared_by_month_but_each_pit_boundary_is_preserved(formal_facts):
    seed(formal_facts)
    calendar_month(formal_facts)
    later = datetime(2026, 7, 7, tzinfo=UTC)
    with Session(formal_facts.catalog.engine) as session:
        previous = session.scalar(select(CalendarSessionFactRecord).where(CalendarSessionFactRecord.session_date == DAY))
        provenance = common_provenance(content_hash=None)
        provenance.update(known_at=later, observed_at=later, knowledge_from=later, knowledge_as_of=later)
        CalendarFactRepository(session).append_session_fact(CalendarSessionFact(
            calendar_id='SSE', session_date=DAY, is_open=False, definition_version='sse-bootstrap-v1',
            fact_version=2, supersedes_fact_id=previous.fact_id,
            registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']), registry_version=1,
            definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']), **provenance))
        session.commit()
    statements = []
    def read(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = formal_facts.catalog.engine
    event.listen(engine, 'before_cursor_execute', read)
    try:
        resolver = SqlFormalPlanResolver(SimpleNamespace(), engine=engine)
        with resolver._session() as (session, _budget):
            original = resolver._calendar(session, 'SSE', DAY, datetime(2026, 7, 6, tzinfo=UTC))
            revised = resolver._calendar(session, 'SSE', DAY, later)
            assert original['fact'].is_open and not revised['fact'].is_open
            for offset in range(20):
                resolver._calendar(session, 'SSE', DAY + timedelta(days=offset), later)
            assert resolver._calendar(session, 'SSE', DAY, datetime(2026, 7, 6, tzinfo=UTC)) == original
            reader = session.info['s3_calendar_reader']
            assert len(reader.months) <= 4 and len(reader.selected) <= 128
    finally:
        event.remove(engine, 'before_cursor_execute', read)
    # All securities/knowledge cutoffs reuse the source candidates, not the
    # selected PIT answer. One SQL month read replaces one read per day.
    assert sum('FROM calendar_session_facts' in query for query in statements) == 1
    assert sum('FROM calendar_source_priorities' in query for query in statements) == 1


def test_fact_check_uses_bounded_sql_metadata_without_rebuilding_domain_facts(formal_facts, monkeypatch):
    seed(formal_facts)
    calendar_month(formal_facts)
    resolver = SqlFormalPlanResolver(SimpleNamespace(), engine=formal_facts.catalog.engine)
    current = work()
    accepted = resolver.resolve_facts(current)
    monkeypatch.setattr(resolver, 'resolve_facts', lambda *a, **kw: pytest.fail('fact check must not resolve the full run'))
    monkeypatch.setattr(resolver, '_resolve', lambda *a, **kw: pytest.fail('fact check must not decode domain history'))
    statements = []
    def read(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = formal_facts.catalog.engine
    event.listen(engine, 'before_cursor_execute', read)
    try:
        resolver.check_facts(current, accepted)
        with engine.begin() as connection:
            resolver.check_facts(current, accepted, connection=connection)
    finally:
        event.remove(engine, 'before_cursor_execute', read)
    selects = [query for query in statements if query.lstrip().startswith('SELECT')]
    assert len(selects) == 22 and all('sha256' in query and 'count(' in query for query in selects)


@pytest.mark.parametrize('kind', ['identity', 'status', 'actions', 'new_mapping'])
def test_sql_content_guard_detects_related_fields_quality_and_new_candidates(formal_facts, kind):
    iid = seed(formal_facts)
    resolver = SqlFormalPlanResolver(SimpleNamespace(), engine=formal_facts.catalog.engine)
    current = work()
    accepted = resolver.resolve_facts(current)
    with Session(formal_facts.catalog.engine) as session:
        if kind == 'identity':
            session.scalar(select(InstrumentIdentityFactRecord)).currency = 'USD'
        elif kind == 'status':
            session.scalar(select(TradingStatusCoverageFact)).status = 'incomplete'
        elif kind == 'actions':
            session.scalar(select(CorporateActionCoverageFact)).evidence = dict(reference='changed')
        else:
            previous = session.scalar(select(InstrumentCodeMappingRecord))
            later = KNOWN + timedelta(days=1)
            InstrumentCodeMappingRepository(session).add_mapping(InstrumentCodeMapping(
                instrument_id=iid, source='etf_ingestion', source_code='510003.SH',
                trading_code='510003.SH', mapping_source='isolated-sql-fixture',
                valid_from=date(2030, 1, 1), known_at=later, observed_at=later,
                evidence='isolated-new-revision', logical_fact_key=previous.logical_fact_key,
                fact_version=2, supersedes_fact_id=previous.id))
        session.commit()
    with pytest.raises(RuntimeFault) as changed:
        resolver.check_facts(current, accepted)
    assert changed.value.code == 'DATA_CHANGED'


def test_unrelated_identity_and_cold_calendar_rows_do_not_invalidate_run(formal_facts):
    seed(formal_facts)
    calendar_month(formal_facts)
    resolver = SqlFormalPlanResolver(SimpleNamespace(), engine=formal_facts.catalog.engine)
    current = work()
    accepted = resolver.resolve_facts(current)
    seed(formal_facts, symbol='510002.SH', calendar=False)
    with Session(formal_facts.catalog.engine) as session:
        CalendarFactRepository(session).append_session_fact(CalendarSessionFact(
            calendar_id='SSE', session_date=DAY - timedelta(days=400), is_open=False,
            definition_version='sse-bootstrap-v1', registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']),
            registry_version=1, definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']),
            **common_provenance(content_hash=None)))
        session.commit()
    resolver.check_facts(current, accepted)


def test_fact_budgets_fail_without_truncating_or_advertising_short_coverage(formal_facts):
    seed(formal_facts)
    provider = FormalProvider(SimpleNamespace(), store=formal_facts)
    for budget, value in [('FACT_ROWS', 1), ('FACT_BYTES', 10), ('FACT_CELLS', 0), ('FACT_SECONDS', 0)]:
        with patch('app.backtest_service.formal_resolver.' + budget, value):
            with pytest.raises(RuntimeFault) as failure:
                provider.plan_resolver.resolve_facts(work())
            assert failure.value.code == 'RESOURCE_LIMIT'


def test_real_pg_statement_timeout_is_reported_as_resource_limit(formal_facts):
    resolver = FormalProvider(SimpleNamespace(), store=formal_facts).plan_resolver
    with resolver._session() as (session, budget):
        session.connection().execute(text("SET LOCAL statement_timeout = '1ms'"))
        with pytest.raises(RuntimeFault) as failure:
            session.execute(select(func.pg_sleep(0.05)))
        assert failure.value.code == 'RESOURCE_LIMIT'


def test_default_app_and_runner_share_sql_diagnostics_and_never_enqueue_unknown_semantics(formal_facts, tmp_path):
    seed(formal_facts)
    settings = Settings(api_token=TOKEN, cursor_signing_key=CURSOR, database_password='isolated-test',
        s3_result_root=tmp_path/'results', data_store_root=formal_facts.files.root, environment='test',
        scheduler_enabled=False, s3_work_root=tmp_path/'work', s3_daemon_work_root=tmp_path/'work',
        s3_worker_image='sha256:' + 'a'*64, _env_file=None)
    (tmp_path/'work').mkdir(mode=0o700)
    # Saved account and revision use their actual SQL services. No accepted
    # flag is supplied, and their arbitrary metadata cannot repair source units.
    from app.backtest_service.accounts.service import AccountProfileService
    from app.backtest_service.accounts.types import AccountProfileStatus
    from app.strategies.service import StrategyStorageService
    from app.backtest_service.service import normalize
    with Session(formal_facts.catalog.engine) as session:
        account = AccountProfileService(session).create(name='isolated-' + uuid4().hex,
            status=AccountProfileStatus.ACTIVE, metadata=dict(raw=True, shares_per_lot=100),
            fee_schedule=dict(key='isolated-saved-commission',
                metadata=dict(currency='CNY', s3_commission=dict(included_components=[])),
                fee_rules=[dict(key='commission', category='commission', side='both', rate='0.0003',
                    minimum='5', rounding_level='fee_item', rounding_scope='on_fill',
                    rounding_mode='half_up', rounding_precision='0.01')]))
        strategy = StrategyStorageService(session).create_strategy(name='isolated-' + uuid4().hex,
            source_code='def initialize(c): pass', parameter_schema={'type': 'object'}, default_parameters={})
        revision = StrategyStorageService(session).publish_revision(strategy.id, expected_draft_version=1)
        session.commit()
        config = normalize(dict(api_schema='qf.backtest.v2', account_id=str(account.id),
            strategy_revision_id=str(revision.id), initial_cash='10000', universe=['510001.SH'],
            frequency='1d', start=DAY.isoformat(), end=DAY.isoformat(), parameters={},
            execution_model='bar_next_interval_v1', participation_rate='1', slippage_bps='0', seed=23))
    # The app uses its normal factory. Only the DB connection points at this
    # random private schema; s3_provider and repository are never installed.
    app = create_app(settings)
    def db():
        with Session(formal_facts.catalog.engine) as session:
            yield session
    app.dependency_overrides[get_db_session] = db
    with patch('app.backtest_service.router.get_engine', return_value=formal_facts.catalog.engine), http_api(app) as request:
        caps = request('GET', '/api/admin/backtests/capabilities')
        assert caps.status_code == 200, caps.text
        assert any(item['binding'] == 'identity' and item['available'] for item in caps.json()['actual_data'])
        preflight = request('POST', '/api/admin/backtests/preflight', config)
        assert preflight.status_code == 200, preflight.text
        diagnostic = preflight.json()['errors'][0]
        assert diagnostic['code'] == 'CAPABILITY_UNAVAILABLE'
        assert all(type(value) is str for value in diagnostic['scope'].values())
        checks = {item['binding']: item for item in json.loads(diagnostic['scope']['facts'])}
        assert checks['account_commission']['available']
        assert checks['identity']['available'] and checks['calendar_sessions']['available']
        assert checks['E70']['status'] == 'field_semantics_missing'
        submission = request('POST', '/api/admin/backtests', config)
        assert submission.status_code == 503, submission.text
        assert submission.json()['detail'] == diagnostic
    class NoWorkerDaemon:
        # This test proves default SQL factory composition. Docker image
        # readiness/real workers are tested by the required runtime suite.
        def preflight(self, image):
            assert image == settings.s3_worker_image
        def start(self, *args, **kwargs):
            pytest.fail('unknown source semantics must never launch a worker')
    repository, process, services = create_services(settings=settings, engine=formal_facts.catalog.engine,
        daemon=NoWorkerDaemon())
    assert isinstance(services.builder.__self__.plan_resolver, SqlFormalPlanResolver)
    captured = RunService(repository, services.builder.__self__)._capture('isolated-default-owner', config)
    from threading import Event
    with pytest.raises(RuntimeFault) as failure:
        services.builder(captured, Claim('isolated-run', 'isolated-token', 1), Event())
    assert failure.value.diagnostic() == diagnostic
    with Session(formal_facts.catalog.engine) as session:
        assert session.scalar(select(Run.id)) is None
