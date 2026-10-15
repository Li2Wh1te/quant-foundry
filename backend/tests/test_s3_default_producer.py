"""Development regressions for the default producer; no live-market acceptance.

Only existing private PostgreSQL/CurrentStore fixtures are used here. Source
facts enter through the real immutable repository, never a plan/provider seam.
Execution-machine verification remains a separate operational step.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_s3_sql_resolver import (database, limits, store, formal, formal_facts,
    seed, calendar_month, DAY, KNOWN, TOKEN, CURSOR, http_api, statuses)
from tests.test_instrument_rule_facts import make_candidate
from tests.test_data_store_local_pipeline import Inputs
from app.backtest_service.formal_execution_producer import PACKAGE, REQUIRED_FIELDS, captured_commission_schedule
from app.backtest_service.formal_resolver import SqlFormalPlanResolver, FormalResolutionFault
from app.backtest_service.accounts.service import AccountProfileService
from app.backtest_service.accounts.types import AccountProfileStatus
from app.backtest_service.service import FormalProvider, RunService, normalize
from app.backtest_service.repository import DatabaseRunRepository
from app.backtest_service.models import Run, RunResult
from app.backtest_service.runtime_contracts import RuntimeFault
from app.core.config import Settings
from app.main import create_app
from app.db.session import get_db_session
from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.normalize import normalize as normalize_source
from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry
from app.data_ingestion.models.corporate_action import CorporateActionCoverageFact
from app.instruments.domain import VersionedReference
from app.instruments.rule_facts_repository import RuleFactsRepository


@pytest.fixture
def producer_setup(formal_facts, tmp_path):
    symbol = '510001.SH'
    iid = seed(formal_facts)
    calendar_month(formal_facts)
    with Session(formal_facts.catalog.engine) as session:
        for family in ('share_distribution', 'split', 'rights_issue'):
            session.add(CorporateActionCoverageFact(id=uuid4(), instrument_id=iid,
                start_date=DAY, end_date=DAY, status='complete', event_count=0, action_type=family,
                source='isolated', source_revision='development-zero', known_at=KNOWN, observed_at=KNOWN,
                evidence=dict(reference='development-zero'), validation_rule='development@1'))
        account = AccountProfileService(session).create(name='producer-' + uuid4().hex,
            status=AccountProfileStatus.ACTIVE, metadata={}, fee_schedule=dict(key='producer-commission',
                metadata=dict(currency='CNY', s3_commission='{"included_components":[]}'),
                fee_rules=[dict(key='commission', category='commission', side='both', rate='0.0003',
                    minimum='5', rounding_level='fee_item', rounding_scope='on_fill',
                    rounding_mode='half_up', rounding_precision='0.01')]))
        from app.strategies.service import StrategyStorageService
        strategies = StrategyStorageService(session)
        strategy = strategies.create_strategy(name='producer-' + uuid4().hex,
            source_code='def initialize(c): pass', parameter_schema={'type': 'object'}, default_parameters={})
        revision = strategies.publish_revision(strategy.id, expected_draft_version=1)
        session.commit()
        config = normalize(dict(api_schema='qf.backtest.v2', account_id=str(account.id),
            strategy_revision_id=str(revision.id), initial_cash='10000', universe=[symbol],
            frequency='1d', start=DAY.isoformat(), end=DAY.isoformat(), parameters={},
            execution_model='bar_next_interval_v1', participation_rate='1', slippage_bps='0', seed=23))
    entry = BY_ID['E70']
    body = dict(source='tushare', ts_code=symbol, trade_date=DAY.isoformat(),
        open=Decimal('10'), high=Decimal('11'), low=Decimal('9'), close=Decimal('10'), vol=Decimal('500'))
    source = LocalInput(entry.source, entry.native, symbol, 'development-producer', KNOWN, body, digest(body))
    assert run_entry(formal_facts, entry, Inputs(source))['qualified']
    representation = next(normalize_source(entry, source)).representation
    settings = Settings(api_token=TOKEN, cursor_signing_key=CURSOR, database_password='isolated-test',
        s3_result_root=tmp_path/'results', data_store_root=formal_facts.files.root, environment='test',
        scheduler_enabled=False, s3_work_root=tmp_path/'work', s3_daemon_work_root=tmp_path/'work',
        s3_worker_image='sha256:' + 'a'*64, _env_file=None)
    (tmp_path/'work').mkdir(mode=0o700)
    provider = FormalProvider(settings, store=formal_facts)
    service = RunService(DatabaseRunRepository(formal_facts.catalog.engine), provider)
    current = service._capture('development-producer', config)
    fields = dict(date=DAY.isoformat(), product='equity_etf',
        day=dict(listing=dict(first_trading_date='2000-01-01', last_trading_date=None,
            verified_through='2026-12-31', kind='other', session_ordinal=1000),
            status='trading', risk='normal', price_limit=dict(kind='limited', rate='0.1', lower='9', upper='11'),
            fund_price_band='ten_percent', underlying_same_session=None, delisting_session_ordinal=None,
            submission_reference=None, evidence='development-explicit-trade-day'),
        account=dict(account_id=str(account.id), account_version=int(account.version),
            permissions=dict(buy=True, sell=True, product_buy=True, risk_disclosure=None,
                delisting_buy=None, risk_bought_or_open=None), investor='resident_individual',
            sale_proceeds='next_session', sale_proceeds_basis='development-cash-timing',
            disposal_settlement_date=None, holding_bands={}, evidence='development-backtest-configuration'),
        market=dict(entry_id='E70', representation=representation, source_subject=symbol,
            price_currency='CNY', price_basis='raw', quantity_unit='lots', shares_per_lot='100',
            completed_bar_visibility='at_verified_session_close',
            evidence='development-explicit-source-semantics'))
    terms = fields.pop('account')
    terms.pop('account_id')
    terms.pop('account_version')
    terms['scope'] = 'backtest_configuration'
    with Session(formal_facts.catalog.engine) as session:
        AccountProfileService(session).update(account.id, expected_version=1,
            metadata=dict(s3_execution=json.dumps(terms)))
        session.commit()
    current = service._capture('development-producer', config)
    return SimpleNamespace(store=formal_facts, iid=iid, fields=fields, work=current,
        provider=provider, service=service, settings=settings, key='producer-' + uuid4().hex)


def append_fact(setup, fields=None, **overrides):
    values = dict(instrument_id=setup.iid, package_reference=PACKAGE,
        fact_reference=VersionedReference(key=setup.key, version=1), fields=fields or setup.fields,
        valid_from=DAY, valid_to=DAY + timedelta(days=1), known_at=KNOWN, observed_at=KNOWN,
        source='development-explicit-inputs', source_revision='development-v1', content_hash=None)
    values.update(overrides)
    with Session(setup.store.catalog.engine) as session:
        RuleFactsRepository(session).append_fact(make_candidate(**values))
        session.commit()


def test_default_provider_uses_real_sql_producer_and_original_native_composer(producer_setup):
    setup = producer_setup
    append_fact(setup)
    # The existing account identity port also accepts the same UUID in hex.
    setup.work.config['account_id'] = setup.work.config['account_id'].replace('-', '')
    assert type(setup.provider.plan_resolver) is SqlFormalPlanResolver
    plan = setup.provider._plan(setup.work)
    key = plan.spec['calendar'][0]['session']['key']
    fact = plan.session_facts(key, 0, 1)['facts'][0]
    assert fact['instrument']['product'] == 'equity_etf'
    assert fact['origin']['kind'] == 'official'
    assert fact['buy_fees']['minimum_commission'] == '5'
    assert fact['permissions']['buy'] is True
    assert not plan.spec.get('synthetic_model') and not plan.spec['facts']
    accepted = setup.provider.plan_resolver.resolve_facts(setup.work)
    assert not accepted.execution_inputs.unavailable_reason
    assert not any((accepted.identities, accepted.commissions, accepted.rule_packages))
    assert statuses(accepted)['execution_plan_assembly']['missing'] == []
    legacy = setup.provider.plan_resolver.resolve_facts(setup.work, paged=False)
    from app.backtest_service.formal_assembly import assemble_run_plan
    legacy_plan = assemble_run_plan(setup.work, legacy, check_facts=lambda work, *, connection=None:
        setup.provider.plan_resolver.check_facts(work, legacy, connection=connection))
    assert plan.session_facts(key, 0, 1) == legacy_plan.session_facts(key, 0, 1)
    from threading import Event
    gateway, context = setup.provider._gateway(setup.work, 'development-default-producer', Event(), plan)
    try:
        query = plan.packet(setup.work, 'development-default-producer')['source']['request']
        rows = [row for batch in gateway.read(plan.market.name, query, context) for row in batch.table.to_pylist()]
        assert len(rows) == 1 and rows[0]['quantity'] == 50000
    finally:
        gateway.close(context)


@pytest.mark.parametrize('case', ['absent', 'raw', 'conversion', 'account', 'permissions', 'fixture',
    'future', 'incomplete', 'date', 'plan', 'visibility', 'missing_terms', 'duplicate_terms'])
def test_default_producer_refuses_missing_or_substituted_source_facts(producer_setup, case):
    setup = producer_setup
    fields, options = deepcopy(setup.fields), {}
    if case == 'raw': fields['market']['price_basis'] = 'provider_reported'
    elif case == 'conversion': fields['market']['shares_per_lot'] = '0'
    elif case == 'account': setup.work.specification['account_snapshot']['id'] = str(uuid4())
    elif case == 'permissions':
        terms = json.loads(setup.work.specification['account_snapshot']['metadata']['s3_execution'])
        terms['permissions']['buy'] = None
        setup.work.specification['account_snapshot']['metadata']['s3_execution'] = json.dumps(terms)
    elif case == 'missing_terms':
        setup.work.specification['account_snapshot']['metadata'] = dict(buy=True, raw=True)
    elif case == 'duplicate_terms':
        metadata = setup.work.specification['account_snapshot']['metadata']
        metadata['s3_execution'] = '{"scope":"backtest_configuration",' + metadata['s3_execution'][1:]
    elif case == 'fixture': options['fixture_only'] = True
    elif case == 'future': options['known_at'] = KNOWN.replace(year=2027)
    elif case == 'incomplete':
        from app.instruments.rules.contracts import FactQualityStatus
        options['quality_status'] = FactQualityStatus.INCOMPLETE
    elif case == 'date': fields['date'] = (DAY + timedelta(days=1)).isoformat()
    elif case == 'plan': fields['buy_fees'] = {}
    elif case == 'visibility': fields['market'].pop('completed_bar_visibility')
    if case != 'absent': append_fact(setup, fields, **options)
    with pytest.raises(FormalResolutionFault) as refused:
        setup.provider._plan(setup.work)
    check = next(item for item in refused.value.checks if item['binding'] == 'daily_execution_inputs')
    assert not check['available']
    assert 'resolved_execution_inputs' in refused.value.checks[-1]['missing']
    if case == 'absent':
        assert check['scope']['missing_scope']['fields'] == list(REQUIRED_FIELDS)


def test_new_visible_fact_revision_invalidates_ordinary_and_final_checks(producer_setup):
    setup = producer_setup
    append_fact(setup)
    accepted = setup.provider.plan_resolver.resolve_facts(setup.work)
    changed = deepcopy(setup.fields)
    changed['market']['evidence'] = 'development-corrected-source-evidence'
    append_fact(setup, changed, fact_reference=VersionedReference(key=setup.key, version=2))
    with pytest.raises(RuntimeFault) as error:
        setup.provider.plan_resolver.check_facts(setup.work, accepted)
    assert error.value.code == 'DATA_CHANGED'
    with setup.store.catalog.engine.begin() as connection, pytest.raises(RuntimeFault) as error:
        setup.provider.plan_resolver.check_facts(setup.work, accepted, connection=connection)
    assert error.value.code == 'DATA_CHANGED'
    plan = setup.provider._plan(setup.work)
    key = plan.spec['calendar'][0]['session']['key']
    terms = json.loads(setup.work.specification['account_snapshot']['metadata']['s3_execution'])
    terms['permissions']['buy'] = False
    from uuid import UUID
    with Session(setup.store.catalog.engine) as session:
        AccountProfileService(session).update(UUID(setup.work.config['account_id']),
            expected_version=setup.work.specification['account_snapshot']['version'],
            metadata=dict(s3_execution=json.dumps(terms)))
        session.commit()
    current = setup.service._capture('development-producer', setup.work.config)
    fresh = setup.provider._plan(current)
    assert fresh.session_facts(key, 0, 1)['facts'][0]['permissions']['buy'] is False
    assert plan.session_facts(key, 0, 1)['facts'][0]['permissions']['buy'] is True


def test_source_observation_audit_does_not_backdate_saved_configuration(producer_setup):
    setup = producer_setup
    # Same existing RuleFactCandidate contract: known_at is visibility;
    # observed_at is audit/display and may follow the simulated date.
    append_fact(setup, observed_at=KNOWN.replace(month=8))
    assert setup.provider._plan(setup.work)


def test_revision_removing_effective_window_cannot_resurrect_old_fact(producer_setup):
    setup = producer_setup
    append_fact(setup)
    accepted = setup.provider.plan_resolver.resolve_facts(setup.work)
    append_fact(setup, fact_reference=VersionedReference(key=setup.key, version=2),
        valid_from=DAY + timedelta(days=2), valid_to=DAY + timedelta(days=3))
    with pytest.raises(RuntimeFault) as error:
        setup.provider.plan_resolver.check_facts(setup.work, accepted)
    assert error.value.code == 'DATA_CHANGED'
    with pytest.raises(FormalResolutionFault):
        setup.provider._plan(setup.work)


def test_default_market_projection_refuses_a_conversion_change_inside_the_run(producer_setup):
    setup = producer_setup
    append_fact(setup)
    following = DAY + timedelta(days=1)
    setup.work.config['end'] = following.isoformat()
    from app.data_ingestion.models.trading_calendar import TradingStatusCoverageFact
    with Session(setup.store.catalog.engine) as session:
        for family in ('cash_dividend', 'share_distribution', 'split', 'rights_issue'):
            session.add(CorporateActionCoverageFact(id=uuid4(), instrument_id=setup.iid,
                start_date=following, end_date=following, status='complete', event_count=0, action_type=family,
                source='isolated', source_revision='development-zero', known_at=KNOWN, observed_at=KNOWN,
                evidence=dict(reference='development-zero'), validation_rule='development@1'))
        session.add(TradingStatusCoverageFact(id=uuid4(), instrument_id=setup.iid, dimension='suspension',
            start_date=following, end_date=following, status='complete', event_count=0,
            source='isolated', source_revision='development-zero', known_at=KNOWN, observed_at=KNOWN,
            evidence=dict(reference='development-zero'), validation_rule='development@1'))
        session.commit()
    fields = deepcopy(setup.fields)
    fields['date'] = following.isoformat()
    fields['day']['listing']['session_ordinal'] += 1
    fields['market']['shares_per_lot'] = '200'
    append_fact(setup, fields, fact_reference=VersionedReference(key=setup.key + '-following', version=1),
        valid_from=following, valid_to=following + timedelta(days=1))
    with pytest.raises(FormalResolutionFault) as error:
        setup.provider._plan(setup.work)
    check = next(item for item in error.value.checks if item['binding'] == 'daily_market_assembly')
    assert not check['available'] and check['missing'] == ['daily_market_contract_or_identity_changed']


def test_default_http_reaches_actual_runner_installation_gate_without_injected_provider(producer_setup):
    setup = producer_setup
    append_fact(setup)
    app = create_app(setup.settings)
    def db():
        with Session(setup.store.catalog.engine) as session:
            yield session
    app.dependency_overrides[get_db_session] = db
    with patch('app.backtest_service.router.get_engine', return_value=setup.store.catalog.engine), http_api(app) as request:
        response = request('POST', '/api/admin/backtests/preflight', setup.work.config)
        assert response.status_code == 200, response.text
        error = response.json()['errors'][0]
        assert error['code'] == 'CAPABILITY_UNAVAILABLE' and 'runner' in error['message']
        assert error['scope'] == {}  # producer passed; no fake runtime-ready marker
    with Session(setup.store.catalog.engine) as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(RunResult)) == 0


@pytest.mark.parametrize('agreement', ['{"included_components":[]}',
    '{"included_components":[],"settlement_scale":2,"rounding":"half_up"}'])
def test_account_http_string_metadata_can_express_explicit_commission_agreement(agreement):
    from app.backtest_service.accounts.schemas import FeeScheduleRequest
    from app.backtest_service.account_config import commission_from_account_fee_schedule
    schedule = FeeScheduleRequest.model_validate(dict(key='saved-backtest-configuration',
        metadata=dict(currency='CNY', s3_commission=agreement), fee_rules=[dict(key='commission',
            category='commission', side='both', rate='0.0003', minimum='5',
            rounding_mode='half_up', rounding_precision='0.01')])).model_dump(mode='json')
    result = commission_from_account_fee_schedule(captured_commission_schedule(schedule), side='buy')
    assert result['commission_rate'] == '0.0003' and result['included_components'] == []


@pytest.mark.parametrize('agreement', ['{}', '[]', 'invalid-json',
    '{"included_components":[],"included_components":[]}',
    '{"included_components":[],"permissions":true}', 'x' * 4097])
def test_string_commission_agreement_cannot_fill_unknown_or_ambiguous_terms(agreement):
    from app.backtest_service.account_config import commission_from_account_fee_schedule
    schedule = dict(key='saved-backtest-configuration', metadata=dict(currency='CNY', s3_commission=agreement),
        fee_rules=[dict(key='commission', category='commission', side='both', rate='0.0003',
            minimum='5', rounding_mode='half_up', rounding_precision='0.01')])
    with pytest.raises(ValueError):
        commission_from_account_fee_schedule(captured_commission_schedule(schedule), side='buy')
