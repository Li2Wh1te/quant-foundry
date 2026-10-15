"""Actual default SQL dependency pages; no execution producer or success plan."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import os
import weakref

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from tests.test_s3_sql_resolver import (database, limits, store, formal, formal_facts,
    seed, calendar_month, work, DAY, KNOWN, statuses, CalendarFactRepository,
    CalendarSessionFact, CalendarSessionFactRecord, common_provenance, REGISTRY_FACT_IDS, DEFINITION_FACT_IDS)
from app.backtest_service.formal_resolver import SqlFormalPlanResolver, FactReadBudget
from app.backtest_service.formal_sql_pages import SqlSessionInputs
from app.backtest_service.runtime_contracts import RuntimeFault
from app.backtest_service.session_fact_pages import BRIDGES
from app.data_ingestion.models.corporate_action import CorporateActionCoverageFact
from app.data_ingestion.models.trading_calendar import TradingStatusCoverageFact
from app.instruments.models import InstrumentIdentityFactRecord
from types import SimpleNamespace
from uuid import UUID, uuid4

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')


@pytest.fixture
def sql_pages(formal_facts, request):
    count, days = getattr(request, 'param', (4, 2))
    symbols = tuple(f'{510000 + i:06}.SH' for i in range(1, count + 1))
    ids = [seed(formal_facts, symbol=symbol, calendar=i == 0) for i, symbol in enumerate(symbols)]
    calendar_month(formal_facts)
    with Session(formal_facts.catalog.engine) as session:
        for row in session.scalars(select(CorporateActionCoverageFact)):
            row.end_date = DAY + timedelta(days=days - 1)
        for row in session.scalars(select(TradingStatusCoverageFact)):
            row.end_date = DAY + timedelta(days=days - 1)
        for iid in ids:
            for family in ('share_distribution', 'split', 'rights_issue'):
                session.add(CorporateActionCoverageFact(id=uuid4(), instrument_id=iid,
                    start_date=DAY, end_date=DAY + timedelta(days=days - 1), status='complete', event_count=0,
                    action_type=family, source='isolated', source_revision='synthetic-sql-pages',
                    known_at=KNOWN, observed_at=KNOWN, evidence=dict(reference='isolated-zero'),
                    validation_rule='isolated@1'))
        session.commit()
    current = work()
    current.config.update(universe=list(symbols), end=(DAY + timedelta(days=days - 1)).isoformat())
    current.specification.update(account_snapshot=dict(fee_schedule=dict(key='isolated-captured-commission',
        metadata=dict(currency='CNY', s3_commission=dict(included_components=[])),
        fee_rules=[dict(key='commission', category='commission', side='both', rate='0.0003',
            minimum='5', rounding_mode='half_up', rounding_precision='0.01')])))
    return current, SqlFormalPlanResolver(SimpleNamespace(), engine=formal_facts.catalog.engine), tuple(ids)


@pytest.mark.parametrize('sql_pages', [(100, 1)], indirect=True)
def test_one_hundred_supported_sql_symbols_prepare_inside_unchanged_budget(sql_pages):
    current, resolver, ids = sql_pages
    facts = resolver.resolve_facts(current)
    assert not any((facts.identities, facts.rule_packages, facts.trading_status,
        facts.corporate_actions, facts.commissions))
    for name in BRIDGES:
        check = statuses(facts)[name]
        assert check['available'] and check['scope']['checked_cells'] == '100', check
    assert facts.action_projection == ()
    key = facts.execution_calendar['calendar'][0]['session']['key']
    tail = facts.execution_inputs.read(key, 99, 1)
    assert tail.identities[0][0] == '510100.SH' and tail.identities[0][2].instrument_id == ids[-1]
    assert tail.commissions[0][2][0]['minimum_commission'] == '5'
    assert facts.execution_inputs.unavailable_reason  # dependency scale only


def test_default_resolver_visits_real_sql_pages_without_retaining_full_period(sql_pages, monkeypatch):
    current, resolver, _ = sql_pages
    seen, live = [], []
    original = resolver._read_dependency_page
    def read(*args, **kwargs):
        assert all(ref() is None for ref in live)
        page = original(*args, **kwargs)
        seen.append((args[3], len(args[2])))
        live.append(weakref.ref(page))
        return page
    monkeypatch.setattr(resolver, '_read_dependency_page', read)
    facts = resolver.resolve_facts(current)
    assert seen == [(DAY, 4), (DAY + timedelta(days=1), 4)]
    assert all(ref() is None for ref in live)
    assert not any((facts.identities, facts.calendars, facts.rule_packages,
        facts.trading_status, facts.corporate_actions, facts.commissions))
    assert isinstance(facts.execution_inputs, SqlSessionInputs)
    checks = statuses(facts)
    for name in BRIDGES:
        assert checks[name]['available'], checks[name]
        assert checks[name]['scope']['checked_cells'] == '8'
    assert facts.action_projection == ()
    assert checks['execution_plan_assembly']['missing'] == ['resolved_market_projection', 'resolved_execution_inputs']
    with pytest.raises(RuntimeFault) as unavailable:
        resolver(current)  # actual provider/default entry, not a custom plan
    assert unavailable.value.code == 'CAPABILITY_UNAVAILABLE'
    assert seen == [(DAY, 4), (DAY + timedelta(days=1), 4)] * 2
    legacy = resolver.resolve_facts(current, paged=False)
    assert facts.token == legacy.token and facts.execution_calendar == legacy.execution_calendar
    assert facts.action_projection == legacy.action_projection


@pytest.mark.parametrize('valid_at_tail', [False, True])
def test_real_256_boundary_is_not_truncated_by_compact_diagnostics(sql_pages, monkeypatch, valid_at_tail):
    current, resolver, _ = sql_pages
    missing = [f'S{i:04}.SZ' for i in range(256)]
    valid = [current.config['universe'][0]]
    current.config.update(universe=missing + valid if valid_at_tail else valid + missing, end=DAY.isoformat())
    sizes = []
    original = resolver._read_dependency_page
    def read(*args, **kwargs):
        sizes.append(len(args[2]))
        return original(*args, **kwargs)
    monkeypatch.setattr(resolver, '_read_dependency_page', read)
    with pytest.raises(RuntimeFault) as unavailable:
        resolver(current)
    assert sizes == [256, 1]
    assert unavailable.value.code == 'CAPABILITY_UNAVAILABLE'
    checks = {row['binding']: row for row in json.loads(unavailable.value.diagnostic()['scope']['facts'])}
    assert checks['identity']['scope']['checked_cells'] == '257'
    assert not checks['identity']['available']
    assert checks['identity']['scope']['security'] == 'S0000.SZ'


def test_sql_reader_returns_only_the_requested_slice_and_captured_dependencies(sql_pages):
    current, resolver, ids = sql_pages
    facts = resolver.resolve_facts(current)
    key = facts.execution_calendar['calendar'][1]['session']['key']
    # Mutating the caller after preparation cannot change this reader's grant
    # or frozen account commission; no REST/account metadata selects the port.
    current.config['universe'] = ['foreign']
    current.specification['account_snapshot']['fee_schedule']['fee_rules'][0]['minimum'] = '999'
    statements = []
    def observed(connection, cursor, statement, parameters, context, executemany):
        if 'sha256' not in statement and 'instrument_code_mappings.source_code IN' in statement:
            statements.append(parameters)
    engine = resolver.engine
    event.listen(engine, 'before_cursor_execute', observed)
    try:
        page = facts.execution_inputs.read(key, 2, 1)
    finally:
        event.remove(engine, 'before_cursor_execute', observed)
    assert len(statements) == 1 and '510003.SH' in statements[0].values()
    assert len(page.identities) == len(page.commissions) == len(page.corporate_actions) == 1
    assert page.identities[0][:2] == ('510003.SH', DAY + timedelta(days=1))
    assert page.identities[0][2].instrument_id == ids[2]
    assert page.commissions[0][2][0]['minimum_commission'] == '5'
    assert page.execution_inputs == () and facts.execution_inputs.unavailable_reason
    original = deepcopy(page)
    page.corporate_actions[0][2]['coverage'][0]['status'] = 'incomplete'
    page.commissions[0][2][0]['minimum_commission'] = '777'
    fresh = facts.execution_inputs.read(key, 2, 1)
    assert fresh.corporate_actions == original.corporate_actions
    assert fresh.commissions == original.commissions


def test_invalid_page_grants_never_open_a_database_connection(sql_pages):
    current, resolver, _ = sql_pages
    facts = resolver.resolve_facts(current)
    key = facts.execution_calendar['calendar'][0]['session']['key']
    def connected(connection):
        pytest.fail('invalid SQL page credit must be rejected before connection')
    event.listen(resolver.engine, 'engine_connect', connected)
    try:
        for session, offset, limit in [('foreign', 0, 1), (facts.execution_calendar['next_session'], 0, 1),
                (key, -1, 1), (key, 4, 1), (key, False, 1), (key, 0, 257), (key, 0, True)]:
            with pytest.raises(RuntimeFault):
                facts.execution_inputs.read(session, offset, limit)
    finally:
        event.remove(resolver.engine, 'engine_connect', connected)


def test_zero_action_pages_follow_the_original_reference_calendar_cutoff(sql_pages):
    current, resolver, _ = sql_pages
    day = DAY + timedelta(days=1)
    with Session(resolver.engine) as session:
        previous = session.scalar(select(CalendarSessionFactRecord).where(CalendarSessionFactRecord.session_date == day))
        known = datetime(2026, 7, 6, 12, tzinfo=UTC)
        provenance = common_provenance(content_hash=None)
        provenance.update(known_at=known, observed_at=known, knowledge_from=known, knowledge_as_of=known)
        CalendarFactRepository(session).append_session_fact(CalendarSessionFact(
            calendar_id='SSE', session_date=day, is_open=False, definition_version='sse-bootstrap-v1',
            fact_version=2, supersedes_fact_id=previous.fact_id,
            registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']), registry_version=1,
            definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']), **provenance))
        for row in session.scalars(select(CorporateActionCoverageFact)):
            row.end_date = DAY
        session.commit()
    facts = resolver.resolve_facts(current)
    assert day.isoformat() in {row['date'] for row in facts.execution_calendar['calendar']}
    assert facts.action_projection is None
    assert not statuses(facts)['execution_corporate_actions']['available']
    assert statuses(facts)['execution_corporate_actions']['scope']['date'] == day.isoformat()
    assert resolver.resolve_facts(current, paged=False).action_projection is None


@pytest.mark.parametrize('phase', ['before', 'during', 'outside_slice'])
def test_sql_page_rechecks_the_entire_run_token(sql_pages, monkeypatch, phase):
    current, resolver, ids = sql_pages
    facts = resolver.resolve_facts(current)
    key = facts.execution_calendar['calendar'][0]['session']['key']
    reads = []
    def change():
        with Session(resolver.engine) as session:
            row = session.scalar(select(InstrumentIdentityFactRecord).where(
                InstrumentIdentityFactRecord.instrument_id == (ids[-1] if phase == 'outside_slice' else ids[0])))
            row.currency = 'USD'
            session.commit()
    original = resolver._read_dependency_page
    def read(*args, **kwargs):
        page = original(*args, **kwargs)
        reads.append(1)
        if phase == 'during':
            change()
        return page
    monkeypatch.setattr(resolver, '_read_dependency_page', read)
    if phase != 'during':
        change()
    with pytest.raises(RuntimeFault) as changed:
        facts.execution_inputs.read(key, 0, 1)
    assert changed.value.code == 'DATA_CHANGED'
    assert len(reads) == (1 if phase == 'during' else 0)


@pytest.mark.parametrize('budget', ['FACT_ROWS', 'FACT_BYTES', 'FACT_SECONDS'])
def test_admission_budget_is_bounded_per_batch_and_cannot_hide_exhaustion(sql_pages, monkeypatch, budget):
    current, resolver, _ = sql_pages
    active, completed, static = [], [], []
    original_budget, original_page = FactReadBudget.read, resolver._read_dependency_page
    def budgeted(self, call):
        before = (self.rows, self.bytes)
        result = original_budget(self, call)
        active.append(self)
        sql = str(call.statement)
        if 'FROM instrument_rule_facts' in sql and 'sha256' not in sql:
            static.append((before, (self.rows, self.bytes)))
        return result
    def read(*args, **kwargs):
        page = original_page(*args, **kwargs)
        completed.append((active[-1].rows, active[-1].bytes))
        if budget == 'FACT_SECONDS' and len(completed) == 1 and exhaust[0]:
            active[-1].deadline = 0
        return page
    exhaust = [False]
    monkeypatch.setattr(FactReadBudget, 'read', budgeted)
    monkeypatch.setattr(resolver, '_read_dependency_page', read)
    resolver.resolve_facts(current)
    assert len(completed) == 2 and completed[1][0] < completed[0][0] and completed[1][1] < completed[0][1]
    before, full = static[0]
    active.clear()
    completed.clear()
    if budget == 'FACT_SECONDS':
        exhaust[0] = True
    else:
        index = 0 if budget == 'FACT_ROWS' else 1
        limit = full[index]-1
        assert before[index] <= limit and full[index]-before[index] <= limit
        monkeypatch.setattr('app.backtest_service.formal_resolver.' + budget, limit)
    with pytest.raises(RuntimeFault) as limited:
        resolver.resolve_facts(current)
    assert limited.value.code == 'RESOURCE_LIMIT'
    assert len(completed) == (1 if budget == 'FACT_SECONDS' else 0)


@pytest.mark.parametrize('kind', ['identity', 'mapping', 'rule', 'status', 'actions'])
def test_batched_candidates_match_original_pit_repositories_across_visibility_changes(sql_pages, monkeypatch, kind):
    from app.instruments.domain import InstrumentIdentityFact, InstrumentCodeMapping, VersionedReference
    from app.instruments.identity_repository import InstrumentIdentityFactRepository
    from app.instruments.repository import InstrumentCodeMappingRepository
    from app.instruments.models import InstrumentCodeMappingRecord
    from app.instruments.rule_facts_repository import RuleFactsRepository
    from tests.test_instrument_rule_facts import make_candidate
    current, resolver, ids = sql_pages
    iid = ids[0]
    later = datetime(2026, 7, 6, 12, tzinfo=UTC)
    with Session(resolver.engine) as session:
        if kind == 'identity':
            previous = session.scalar(select(InstrumentIdentityFactRecord).where(InstrumentIdentityFactRecord.instrument_id == iid))
            InstrumentIdentityFactRepository(session).append_fact(InstrumentIdentityFact(
                instrument_id=iid, fact_version=2, logical_fact_key=previous.logical_fact_key,
                supersedes_fact_id=previous.id, asset_class='etf', exchange='SSE', currency='USD',
                calendar_id='SSE', valid_from=previous.valid_from, known_at=later,
                observed_at=later, evidence='isolated-later-identity'))
        elif kind == 'mapping':
            previous = session.scalar(select(InstrumentCodeMappingRecord).where(InstrumentCodeMappingRecord.instrument_id == iid))
            InstrumentCodeMappingRepository(session).add_mapping(InstrumentCodeMapping(instrument_id=iid,
                source=previous.source, source_code=previous.source_code, trading_code=previous.trading_code,
                mapping_source='isolated-correction', valid_from=DAY+timedelta(days=2),
                known_at=later, observed_at=later, evidence='isolated-removed-window',
                logical_fact_key=previous.logical_fact_key, fact_version=2, supersedes_fact_id=previous.id))
        elif kind == 'rule':
            RuleFactsRepository(session).append_fact(make_candidate(instrument_id=iid,
                fact_reference=VersionedReference(key='isolated-conflict-'+uuid4().hex, version=1),
                content_hash=None, known_at=later, observed_at=later))
        else:
            model = TradingStatusCoverageFact if kind == 'status' else CorporateActionCoverageFact
            fields = dict(dimension='suspension') if kind == 'status' else dict(action_type='cash_dividend')
            session.add(model(id=uuid4(), instrument_id=iid, start_date=DAY, end_date=DAY+timedelta(days=1),
                status='incomplete', event_count=0, source='isolated', source_revision='synthetic-later-coverage',
                known_at=later, observed_at=later, evidence=dict(reference='isolated-later'),
                validation_rule='isolated@1', **fields))
        session.commit()
    pages, recording = [], [True]
    original = resolver._read_dependency_page
    def read(*args, **kwargs):
        page = original(*args, **kwargs)
        if recording[0]: pages.append((args[3], page))
        return page
    monkeypatch.setattr(resolver, '_read_dependency_page', read)
    actual = resolver.resolve_facts(current)
    recording[0] = False
    legacy = resolver.resolve_facts(current, paged=False)
    assert actual.token == legacy.token and actual.execution_calendar == legacy.execution_calendar
    assert actual.action_projection == legacy.action_projection
    for day, page in pages:
        for name in ('identities', 'calendars', 'rule_packages', 'trading_status', 'corporate_actions', 'commissions'):
            assert getattr(page, name) == tuple(row for row in getattr(legacy, name) if row[1] == day), (kind, name, day)
    for name in BRIDGES:
        expected = [check for check in legacy.checks if check['binding'] == name]
        if expected:
            assert statuses(actual)[name]['available'] == (len(expected) == 8 and all(row['available'] for row in expected)), name


def test_security_page_order_preserves_the_first_calendar_failure():
    from app.backtest_service.formal_sql_pages import ScopedChecks
    from app.backtest_service.formal_resolver import state
    checks = ScopedChecks(('Z.SH', 'A.SH'))
    for symbol, day in [('Z.SH', '2026-07-07'), ('A.SH', '2026-07-06')]:
        checks.append(state('identity', 'fact_missing', missing=('identity',), scope=dict(security=symbol, date=day)))
    assert checks[0]['scope'] == dict(security='A.SH', date='2026-07-06', checked_cells='2')


def test_default_resolver_carries_research_descriptors_without_acceptance_flags(sql_pages):
    from app.backtest_service.research_gateway import current_research_bindings
    current, resolver, _ = sql_pages
    facts = resolver.resolve_facts(current)
    describe = lambda bindings: [(item.name, item.kind, item.unavailable_reason) for item in bindings]
    assert describe(facts.research) == describe(current_research_bindings())
    assert facts.research and all(item.unavailable_reason and not item.accepted_facts for item in facts.research)
    for binding in facts.research:
        assert statuses(facts)[binding.name]['scope']['requirement'] == 'optional_per_capability'


@pytest.mark.parametrize('sql_pages', [(1, 10)], indirect=True)
@pytest.mark.parametrize('missing_monday', [False, True])
def test_calendar_closed_failures_cannot_hide_required_open_session_failures(sql_pages, missing_monday):
    from app.instruments.domain import InstrumentIdentityFact
    from app.instruments.identity_repository import InstrumentIdentityFactRepository
    current, resolver, ids = sql_pages
    with Session(resolver.engine) as session:
        previous = session.scalar(select(InstrumentIdentityFactRecord).where(
            InstrumentIdentityFactRecord.instrument_id == ids[0]))
        # Build the two initial non-overlapping facts in this private fixture;
        # an old unbounded logical chain cannot coexist with a second chain.
        valid_from, logical_key = previous.valid_from, previous.logical_fact_key
        session.delete(previous)
        session.flush()
        repository = InstrumentIdentityFactRepository(session)
        repository.append_fact(InstrumentIdentityFact(instrument_id=ids[0], fact_version=1,
            logical_fact_key=logical_key,
            asset_class='etf', exchange='SSE', currency='CNY', calendar_id='SSE',
            valid_from=valid_from, valid_to=DAY+timedelta(days=5), known_at=KNOWN,
            observed_at=KNOWN, evidence='isolated-friday-window'))
        repository.append_fact(InstrumentIdentityFact(instrument_id=ids[0], fact_version=1,
            logical_fact_key='isolated-returning-identity', asset_class='etf', exchange='SSE', currency='CNY',
            calendar_id='SSE', valid_from=DAY+timedelta(days=8 if missing_monday else 7),
            known_at=KNOWN, observed_at=KNOWN, evidence='isolated-returning-window'))
        session.commit()
    facts = resolver.resolve_facts(current)
    legacy = resolver.resolve_facts(current, paged=False)
    assert facts.token == legacy.token and facts.action_projection == legacy.action_projection
    required = {row['date'] for row in facts.execution_calendar['calendar']}
    for name in BRIDGES:
        legacy_required = [check for check in legacy.checks if check['binding'] == name
            and check['scope']['date'] in required]
        expected = len(legacy_required) == len(required) and all(check['available'] for check in legacy_required)
        assert statuses(facts)[name]['available'] == expected, name
        if not expected:
            assert statuses(facts)[name]['scope']['date'] == (DAY+timedelta(days=7)).isoformat()
    assert facts.action_projection == (None if missing_monday else ())
