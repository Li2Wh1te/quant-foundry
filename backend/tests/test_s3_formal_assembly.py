"""Isolated final-assembly DTO oracles and real PG/node/IPC/Rust consumption.

These narrow DTOs are test inputs, not new production producers. The default
SQL resolver remains unchanged and refuses the same rows before/after assembly;
no AcceptedRunPlan or success provider is installed into either default factory.
"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4
import socket

import pytest
from sqlalchemy.orm import Session

from tests.test_s3_sql_resolver import (database, limits, store, formal, formal_facts,
    seed, calendar_month, work, DAY, KNOWN)
from tests.test_data_store_local_pipeline import Inputs
from app.backtest_service.formal_assembly import ExecutionSessionInput, assemble_run_plan
from app.backtest_service.formal_runtime import REQUIRED_EXECUTION_FACTS, daily_node_binding
from app.backtest_service.formal_resolver import SqlFormalPlanResolver
from app.backtest_service.service import FormalProvider
from app.backtest_service.data_gateway import serve
from app.backtest_service.runtime_contracts import RuntimeFault
from app.backtest_service.serialization import encoded
from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry
from app.data_ingestion.models.corporate_action import CorporateActionCoverageFact
from quantfoundry import _native


def oracle(formal_facts):
    symbol = '510001.SH'
    iid = seed(formal_facts)
    calendar_month(formal_facts)
    with Session(formal_facts.catalog.engine) as session:
        for family in ('share_distribution', 'split', 'rights_issue'):
            session.add(CorporateActionCoverageFact(id=uuid4(), instrument_id=iid,
                start_date=DAY, end_date=DAY, status='complete', event_count=0, action_type=family,
                source='isolated', source_revision='synthetic-full-zero', known_at=KNOWN,
                observed_at=KNOWN, evidence=dict(reference='isolated-zero'), validation_rule='isolated@1'))
        session.commit()
    current = work()
    current.config.update(api_schema='qf.backtest.v2', strategy_revision_id='isolated-strategy',
        account_id='isolated-account', initial_cash='10000', execution_model='bar_next_interval_v1', parameters={})
    current.config.update(json.loads(_native.validate_run_config_json(encoded(current.config).decode())))
    current.specification.update(account_snapshot=dict(fee_schedule=dict(key='isolated-commission',
        metadata=dict(currency='CNY', s3_commission=dict(included_components=[])),
        fee_rules=[dict(key='commission', category='commission', side='both', rate='0.0003',
            minimum='5', rounding_mode='half_up', rounding_precision='0.01')])))
    resolver = SqlFormalPlanResolver(SimpleNamespace(), engine=formal_facts.catalog.engine)
    facts = resolver.resolve_facts(current)
    assert facts.action_projection == ()
    assert next(check for check in facts.checks if check['binding'] == 'execution_corporate_actions')['available']
    entry = BY_ID['E70']
    body = dict(source='tushare', ts_code=symbol, trade_date=DAY.isoformat(),
        open=Decimal('10'), high=Decimal('11'), low=Decimal('9'), close=Decimal('10'), vol=Decimal('500'))
    source = LocalInput(entry.source, entry.native, symbol, 'isolated-assembly-oracle', KNOWN, body, digest(body))
    assert run_entry(formal_facts, entry, Inputs(source))['qualified']
    market = daily_node_binding('E70', representation=next(normalize(entry, source)).representation,
        identities={symbol: (symbol, str(iid))}, calendar=facts.execution_calendar['calendar'],
        shares_per_quantity='2', accepted_facts=REQUIRED_EXECUTION_FACTS,
        evidence=dict(kind='synthetic', reference='isolated-assembly-oracle'))
    payload = dict(instrument=dict(security=symbol, exchange='shanghai', product='equity_etf'),
        day=dict(security=symbol, date=DAY.isoformat(), session=facts.execution_calendar['calendar'][0]['session']['key'],
            listing=dict(first_trading_date='2000-01-01', last_trading_date=None,
                verified_through='2026-12-31', kind='other', session_ordinal=1000),
            status='trading', risk='normal', price_limit=dict(kind='limited', rate='0.1', lower='9', upper='11'),
            fund_price_band='ten_percent', underlying_same_session=None, delisting_session_ordinal=None),
        permissions=dict(buy=True, sell=True, product_buy=True, risk_disclosure=None,
            delisting_buy=None, risk_bought_or_open=None), submission_reference=None,
        investor='resident_individual', sale_proceeds='next_session',
        sale_proceeds_basis='isolated-explicit-cash-timing', disposal_settlement_date='2026-07-07', holding_bands={})
    projected = replace(facts, market=market, execution_inputs=(ExecutionSessionInput(iid, payload),),
                        action_projection=())
    return current, resolver, facts, projected


def assemble(current, resolver, accepted, projected):
    return assemble_run_plan(current, projected, check_facts=lambda run, *, connection=None:
        resolver.check_facts(run, accepted, connection=connection))


def test_final_assembly_oracle_streams_original_session_dtos_and_default_still_refuses(formal_facts):
    current, resolver, accepted, projected = oracle(formal_facts)
    with pytest.raises(RuntimeFault) as refused: resolver(current)
    assert refused.value.code == 'CAPABILITY_UNAVAILABLE'
    plan = assemble(current, resolver, accepted, projected)
    assert plan.validate(current) == accepted.execution_calendar['reference']
    assert plan.spec['following_session']['session']['key'] == plan.spec['next_session']
    assert plan.spec['facts'] == [] and plan.spec['stream_session_facts']
    session = plan.spec['calendar'][0]['session']['key']
    page = plan.session_facts(session, 0, 1)
    assert page['next_offset'] is None and len(page['facts']) == 1
    assert page['facts'][0]['origin']['kind'] == 'official'
    assert page['facts'][0]['buy_fees']['minimum_commission'] == '5'
    page['facts'][0]['permissions']['buy'] = False
    assert plan.session_facts(session, 0, 256)['facts'][0]['permissions']['buy']
    with pytest.raises(RuntimeFault): plan.session_facts('ungranted-session', 0, 1)
    with pytest.raises(RuntimeFault): resolver(current)


def test_missing_optional_adjustment_semantics_only_disables_that_research_route(formal_facts):
    from tests.test_s3_formal_research_runtime import research_nodes
    current, resolver, accepted, projected = oracle(formal_facts)
    bindings = research_nodes(formal_facts, projected.execution_calendar['calendar'], security='510001.SH')
    bindings = tuple(replace(binding, factor_anchor=None) if binding.kind == 'adjustment' else binding
                     for binding in bindings)
    plan = assemble(current, resolver, accepted, replace(projected, research=bindings, reference_indices=('REF',)))
    assert 'adjustment' not in {binding.kind for binding in plan.research}
    assert 'price' in {binding.kind for binding in plan.research}
    assert plan.market is projected.market
    assert plan.validate(current) == projected.execution_calendar['reference']


def test_tied_zero_action_coverage_is_ambiguous_and_cannot_survive_final_fact_check(formal_facts):
    current, resolver, accepted, projected = oracle(formal_facts)
    with Session(formal_facts.catalog.engine) as session:
        session.add(CorporateActionCoverageFact(id=uuid4(), instrument_id=projected.execution_inputs[0].instrument_id,
            start_date=DAY, end_date=DAY, status='complete', event_count=0, action_type='cash_dividend',
            source='isolated', source_revision='tied-zero', known_at=KNOWN, observed_at=KNOWN,
            evidence=dict(reference='conflicting-zero-assertion'), validation_rule='isolated@1'))
        session.commit()
    latest = resolver.resolve_facts(current)
    assert latest.action_projection is None
    assert next(check for check in latest.checks if check['binding'] == 'corporate_actions')['status'] == 'fact_invalid'
    with pytest.raises(RuntimeFault) as changed: resolver.check_facts(current, accepted)
    assert changed.value.code == 'DATA_CHANGED'


@pytest.mark.parametrize('case', ['unknown_actions', 'cash_only_zero', 'identity', 'duplicate', 'session', 'fees', 'rules', 'listing'])
def test_final_assembly_oracle_rejects_incomplete_scope_and_fact_substitution(formal_facts, case):
    current, resolver, accepted, projected = oracle(formal_facts)
    item = projected.execution_inputs[0]
    payload = deepcopy(item.payload)
    if case == 'unknown_actions': projected = replace(projected, action_projection=None)
    elif case == 'cash_only_zero':
        actions = tuple((s, d, dict(value, coverage=tuple(r for r in value['coverage']
            if r['action_type'] == 'cash_dividend'))) for s, d, value in projected.corporate_actions)
        projected = replace(projected, corporate_actions=actions)
    elif case == 'identity': projected = replace(projected, execution_inputs=(replace(item, instrument_id=uuid4()),))
    elif case == 'duplicate': projected = replace(projected, execution_inputs=(item, item))
    elif case == 'rules': projected = replace(projected, official_rules=projected.official_rules[:-1])
    else:
        if case == 'session': payload['day']['session'] = 'ungranted-session'
        elif case == 'listing': payload['day']['listing'] = None
        else: payload['buy_fees'] = {}
        projected = replace(projected, execution_inputs=(replace(item, payload=payload),))
    with pytest.raises(RuntimeFault) as refused: assemble(current, resolver, accepted, projected)
    if case == 'listing':
        assert refused.value.code == 'RULE_UNAVAILABLE'
        assert refused.value.diagnostic()['scope']['field'] == 'listing'
        assert all(type(v) is str for v in refused.value.diagnostic()['scope'].values())


def test_assembly_oracle_actual_pg_node_ipc_and_rust_session_facts(formal_facts, tmp_path):
    current, resolver, accepted, projected = oracle(formal_facts)
    plan = assemble(current, resolver, accepted, projected)
    provider = FormalProvider(SimpleNamespace(environment='test'), store=formal_facts)
    # A direct wiring oracle, never a replacement resolver/default success.
    gateway, context = provider._gateway(current, 'isolated-assembly', Event(), plan)
    packet = plan.packet(current, 'isolated-assembly')
    listener = socket.socket(socket.AF_UNIX)
    endpoint = tmp_path/'data.sock'
    listener.bind(str(endpoint)); listener.listen(1)
    packet['source']['socket_path'] = str(endpoint)
    errors = []
    from quantfoundry._transport import Channel
    def peer():
        try:
            connection, _ = listener.accept()
            serve(Channel(connection), gateway, context, close_context=False, session_facts=plan.session_facts)
        except BaseException as error: errors.append(error)
    thread = Thread(target=peer, daemon=True); thread.start()
    module = SimpleNamespace()
    exec('''from quantfoundry.api import get_price,record
def handle_data(c,data):
    frame=get_price(['510001.SH'],count=1,fields=['close'])
    assert str(frame['close'].iloc[0]) == '10'
    record(assembly_callbacks=1)
''', module.__dict__)
    from quantfoundry._worker import _run_module
    from quantfoundry.data import _MarketRoute
    result = None
    try:
        result = _run_module(module, packet, market_routes={'1d': _MarketRoute(plan.market.name,
            ('open', 'high', 'low', 'close', 'quantity'))})
        summary = json.loads(result.summary_json())
        assert summary['outcome']['status'] == 'succeeded', summary
        assert summary['progress']['completed_events'] == 1
        assert result._callback_metrics['by_kind']['handle_data']['count'] == 1
        assert gateway.finalize(context, lambda connection: connection.in_transaction())
    finally:
        if result is not None: result.close()
        listener.close(); thread.join(10)
        gateway.close(context)
    assert not thread.is_alive() and not errors, errors
