"""Large existing-DTO assembly oracles, not default SQL/factory acceptance.

No producer, stored semantic contract, vendor, success resolver or execution
plan is installed into a default application. The input reader constructs only
the requested unit-oracle page, and every row uses the unchanged Rust composer.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
import gc
import json
import os
from pathlib import Path
import resource
import sys
from types import SimpleNamespace
import time as clock
import tracemalloc
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
if os.getenv('QF_S3_INTEGRATION_REQUIRED') == '1':
    from quantfoundry import _native
else:
    _native = pytest.importorskip('quantfoundry._native', reason='installed S3 wheel required')

from app.backtest_service.formal_assembly import assemble_run_plan
from app.backtest_service.account_config import commission_from_account_fee_schedule
from app.backtest_service.formal_resolver import ResolvedFormalFacts
from app.backtest_service.formal_runtime import daily_node_binding, REQUIRED_EXECUTION_FACTS
from app.backtest_service.runtime_contracts import AcceptedWork, RuntimeFault, WorkerLimits
from app.backtest_service.serialization import encoded
from app.backtest_service.session_fact_pages import (
    ACTION_FAMILIES, ExecutionSessionInput, PagedSessionInputs, SessionInputPage)

ZONE = ZoneInfo('Asia/Shanghai')


def dto_oracle(symbol_count, day_count):
    """Explicit fictional all-natural-days-open calendar and original DTOs."""
    # SZSE market rules cover this range; ETF transfer-fee evidence starts
    # 2025-05-01. Do not stretch the original catalogs to earlier dates.
    start = date(2025, 5, 1)
    securities = tuple(f'S{i:04}.SZ' for i in range(symbol_count))
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    def ns(day, hour, minute):
        delta = datetime.combine(day, time(hour, minute), ZONE).astimezone(UTC) - epoch
        return str((delta.days * 86400 + delta.seconds) * 10**9)
    calendar = []
    for offset in range(day_count + 1):
        day = start + timedelta(days=offset)
        windows = [dict(start_ns=ns(day, 9, 30), end_ns=ns(day, 11, 30)),
                   dict(start_ns=ns(day, 13, 0), end_ns=ns(day, 15, 0))]
        key = 'SZSE:' + day.isoformat()
        calendar.append(dict(date=day.isoformat(),
            session=dict(key=key, exchange_timezone='Asia/Shanghai',
                open_ns=windows[0]['start_ns'], close_ns=windows[-1]['end_ns']),
            local_midnight_ns=ns(day, 0, 0),
            before_open_ns=str(int(windows[0]['start_ns']) - 1),
            after_close_ns=str(int(windows[-1]['end_ns']) + 1),
            market_windows=windows, bar_windows=deepcopy(windows),
            week=dict(id=(day - timedelta(days=day.weekday())).isoformat(),
                trading_day=day.weekday() + 1, total_trading_days=7),
            month=dict(id=day.isoformat()[:7], trading_day=day.day,
                total_trading_days=monthrange(day.year, day.month)[1])))
    following = calendar.pop()
    dates = {row['session']['key']: date.fromisoformat(row['date']) for row in calendar}
    end = start + timedelta(days=day_count - 1)
    config = dict(api_schema='qf.backtest.v2', account_id='unit-account',
        strategy_revision_id='unit-strategy', initial_cash='10000', parameters={},
        universe=list(securities), frequency='1d', start=start.isoformat(), end=end.isoformat(),
        execution_model='bar_next_interval_v1', participation_rate='1', slippage_bps='0', seed=23)
    config = json.loads(_native.validate_run_config_json(encoded(config).decode()))
    work = AcceptedWork('unit-page-owner', config, '', WorkerLimits(), {})
    identities = {security: (security, str(UUID(int=index + 1))) for index, security in enumerate(securities)}
    market = daily_node_binding('E70', representation='tushare:unit_page_oracle',
        identities=identities, calendar=calendar, shares_per_quantity='1',
        accepted_facts=REQUIRED_EXECUTION_FACTS,
        evidence=dict(kind='synthetic', reference='existing_dto_paging_oracle'))
    commission = commission_from_account_fee_schedule(dict(key='unit-captured-commission',
        metadata=dict(currency='CNY', s3_commission=dict(included_components=[])),
        fee_rules=[dict(key='commission', category='commission', side='both', rate='0.0003',
            minimum='5', rounding_mode='half_up', rounding_precision='0.01')]),
        side='buy', currency='CNY', applicability_context=dict(asset_class='etf', exchange='SZSE'))
    stats = dict(pages=0, rows=0, max_page_rows=0, max_live_rows=0, checks=0)
    def read(session, offset, limit):
        day = dates[session]
        stats['pages'] += 1
        stats['max_page_rows'] = max(stats['max_page_rows'], limit)
        inputs, identity_rows, commissions, status, actions = [], [], [], [], []
        for index in range(offset, offset + limit):
            security = securities[index]
            iid = UUID(int=index + 1)
            payload = dict(instrument=dict(security=security, exchange='shenzhen', product='equity_etf'),
                day=dict(security=security, date=day.isoformat(), session=session,
                    listing=dict(first_trading_date='2000-01-01', last_trading_date=None,
                        verified_through='2026-12-31', kind='other', session_ordinal=1000 + (day - start).days),
                    status='trading', risk='normal', price_limit=dict(kind='limited', rate='0.1', lower='9', upper='11'),
                    fund_price_band='ten_percent', underlying_same_session=None, delisting_session_ordinal=None),
                permissions=dict(buy=True, sell=True, product_buy=True, risk_disclosure=None,
                    delisting_buy=None, risk_bought_or_open=None), submission_reference=None,
                investor='resident_individual', sale_proceeds='next_session',
                sale_proceeds_basis='isolated-explicit-cash-timing',
                disposal_settlement_date=(day + timedelta(days=1)).isoformat(), holding_bands={})
            inputs.append(ExecutionSessionInput(iid, payload))
            identity_rows.append((security, day, SimpleNamespace(instrument_id=iid,
                currency='CNY', exchange='SZSE', asset_class='etf'), ()))
            commissions.append((security, day, (commission, commission)))
            status.append((security, day, dict(facts=(), coverage=())))
            actions.append((security, day, dict(events=(), coverage=tuple(
                dict(action_type=family, status='complete', event_count=0) for family in sorted(ACTION_FAMILIES)))))
        stats['rows'] += len(inputs)
        stats['max_live_rows'] = max(stats['max_live_rows'], len(inputs))
        return SessionInputPage(tuple(inputs), tuple(identity_rows), tuple(commissions), tuple(status), tuple(actions))
    def check(*_args, **_kwargs):
        stats['checks'] += 1
    facts = ResolvedFormalFacts((), (), (), (), (), (), (),
        tuple(json.loads(_native.official_market_rules_json())),
        dict(calendar=calendar, following_session=following, next_session=following['session']['key'],
            reference='isolated-existing-dto-calendar'), 'unit-oracle-not-a-source-token',
        market=market, execution_inputs=PagedSessionInputs(read), action_projection=())
    return work, facts, check, stats


@pytest.mark.parametrize('symbols,days', [(100, 365), (5000, 5)])
def test_large_existing_dto_assembly_is_page_bounded(symbols, days):
    work, facts, check, stats = dto_oracle(symbols, days)
    tracemalloc.start()
    started = clock.perf_counter()
    plan = assemble_run_plan(work, facts, check_facts=check)
    preparation_seconds = clock.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    admission_pages = stats['pages']
    assert stats['rows'] == symbols * days
    assert admission_pages == days * ((symbols + 255) // 256)
    assert stats['max_page_rows'] <= 256 and stats['max_live_rows'] <= 256
    assert peak < 32 * 1024**2
    assert plan.spec['facts'] == [] and plan.spec['stream_session_facts']
    # Runtime reads only the requested current-session slice, without redoing
    # whole-run validation or retaining the admission's composed rows.
    first = plan.spec['calendar'][0]['session']['key']
    last = plan.spec['calendar'][-1]['session']['key']
    head = plan.session_facts(first, 0, 1)
    tail = plan.session_facts(last, symbols - 1, 256)
    assert stats['pages'] == admission_pages + 2
    assert head['facts'][0]['instrument']['security'] == work.config['universe'][0]
    assert tail['facts'][0]['instrument']['security'] == work.config['universe'][-1]
    assert tail['next_offset'] is None
    assert head['facts'][0]['buy_fees']['minimum_commission'] == '5'
    assert len(encoded(plan.packet(work, 'isolated-page-run'))) < 1024**2
    output = os.getenv('QF_S3_PAGE_EVIDENCE')
    if output:
        target = Path(output)
        assert not target.resolve().is_relative_to(Path(__file__).resolve().parents[2])
        target.mkdir(parents=True, exist_ok=True)
        (target / f'assembly-{symbols}x{days}.json').write_text(json.dumps(dict(
            scope='existing_DTO_assembly_oracle_only', default_factory_success=False,
            symbols=symbols, sessions=days, admission_pages=admission_pages,
            input_rows=symbols * days, max_page_rows=stats['max_page_rows'],
            python_tracemalloc_peak_bytes=peak, preparation_seconds=preparation_seconds,
            process_lifetime_rss_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss *
                (1 if sys.platform == 'darwin' else 1024),
            resource_scope='single_local_test_process_not_worker_cgroup',
            rust_composer='original_compose_official_session_fact_json',
            producer_or_schema_added=False), indent=2) + '\n')
    del plan
    gc.collect()


def test_page_input_shape_and_byte_exhaustion_are_resource_errors():
    with pytest.raises(RuntimeFault) as oversized:
        SessionInputPage((None,) * 257, (), (), (), ())
    assert oversized.value.code == 'RESOURCE_LIMIT'
    work, facts, check, _ = dto_oracle(1, 1)
    source = facts.execution_inputs.read
    def oversized_page(session, offset, limit):
        page = source(session, offset, limit)
        item = page.execution_inputs[0]
        payload = dict(item.payload, sale_proceeds_basis='x' * (4 * 1024**2))
        return SessionInputPage((ExecutionSessionInput(item.instrument_id, payload),),
            page.identities, page.commissions, page.trading_status, page.corporate_actions)
    from dataclasses import replace
    with pytest.raises(RuntimeFault) as exhausted:
        assemble_run_plan(work, replace(facts, execution_inputs=PagedSessionInputs(oversized_page)), check_facts=check)
    assert exhausted.value.code == 'RESOURCE_LIMIT'


def test_original_rust_market_date_coverage_is_not_relaxed_by_paging():
    work, facts, check, _ = dto_oracle(1, 1)
    source = facts.execution_inputs.read
    def uncovered_page(session, offset, limit):
        from dataclasses import replace
        page = source(session, offset, limit)
        item = page.execution_inputs[0]
        payload = deepcopy(item.payload)
        payload['instrument']['exchange'] = 'shanghai'
        s, d, identity, mappings = page.identities[0]
        identity = SimpleNamespace(**dict(vars(identity), exchange='SSE'))
        return replace(page, execution_inputs=(replace(item, payload=payload),),
            identities=((s, d, identity, mappings),))
    from dataclasses import replace
    with pytest.raises(RuntimeFault) as refused:
        assemble_run_plan(work, replace(facts, execution_inputs=PagedSessionInputs(uncovered_page)), check_facts=check)
    assert refused.value.code == 'RULE_UNAVAILABLE'
    assert refused.value.diagnostic()['scope']['date'] == '2025-05-01'


def test_control_budget_failure_is_rejected_before_source_pages_are_read():
    from dataclasses import replace
    work, facts, check, stats = dto_oracle(1, 1)
    projection = dict(facts.execution_calendar, reference='x' * (1024**2))
    with pytest.raises(RuntimeFault) as refused:
        assemble_run_plan(work, replace(facts, execution_calendar=projection), check_facts=check)
    assert refused.value.code == 'RESOURCE_LIMIT'
    assert stats['pages'] == 0
