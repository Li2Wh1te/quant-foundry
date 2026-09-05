"""Real Memory Provider/session/chunk acceptance through deterministic phases."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.backtesting.accounting import AccountState, AccountingPolicy, PortfolioState, SettlementPolicy
from app.backtesting.data.memory import MemoryDataProvider
from app.backtesting.data.runtime_bridge import ChunkBacktestViewFactory
from app.backtesting.execution import BarMarketExecutionModel
from app.backtesting.fees import FeeCalculator, FeeRule, FeeSchedule
from app.backtesting.runtime import DeterministicBacktestRunner, TargetWeightsInterpreter, run_data_session
from app.backtesting.slippage import BpsSlippageModel
from app.backtesting.timing import AfterCloseToNextOpenV1
from app.backtesting.time_axis import TradingDayAxis
from tests.backtest_runtime_fixture import ScriptedStrategy, SessionListSettlementCalendar, universe_query, make_candidate
from tests.test_backtesting_memory_provider import build_dataset, make_intent, admit, IID_A, CAL_ID, weekdays


def run_memory_acceptance(*, expire_after_first_chunk=False, with_analysis=False, with_dividend=False, analysis_session_factory=None, sharpe_mode="sharpe_simple"):
    days = weekdays(date(2026, 1, 2), date(2026, 2, 5))
    dataset = build_dataset(facts_start=date(2026, 1, 1), facts_end=date(2026, 2, 5), open_days=set(days))
    # These are named synthetic facts, known before the fixture window; they
    # are never used as fallbacks for a production source's missing evidence.
    known = datetime(2026, 1, 1, tzinfo=timezone.utc)
    dataset = replace(dataset, bars=tuple(replace(bar, evidence=replace(bar.evidence, known_at=known, observed_at=known)) for bar in dataset.bars))
    provider = MemoryDataProvider(dataset)
    intent = make_intent(start=days[0], end=days[-1])
    intent = replace(intent, query_boundary=replace(intent.query_boundary, data_cutoff=dataset.clock))
    request = admit(provider, intent)
    with provider.open_session(request) as session:
        report = session.preflight()
        axis = TradingDayAxis(session.resolved_sessions)
        views = ChunkBacktestViewFactory(request=request,
            universe_query=universe_query([make_candidate(IID_A)]),
            trading_status_resolver=lambda *_: {"suspended": False, "buy_allowed": True, "sell_allowed": True})
        strategy = ScriptedStrategy({19: {str(IID_A): "1"}, 20: {str(IID_A): "0"}})
        schedule = FeeSchedule(key="memory_acceptance", version=1, fee_rules=(FeeRule(key="commission", category="commission", rate="0", minimum="5", rounding_level="fee_item", rounding_scope="commission", rounding_mode="half_up", rounding_precision="0.01"),))
        portfolio = PortfolioState(account=AccountState(cash_balances={"CNY": "10000"}, available_cash="10000", frozen_cash="0", margin_used="0", margin_available="0", equity="10000"), as_of=axis.at(0).start_time)
        analysis = None
        if with_analysis:
            from app.backtesting.production_runtime import _admit_formal_analysis, default_components, _initial_portfolio
            from app.backtesting.spec import BacktestSpec, ComponentSelection
            spec = BacktestSpec(days[0], days[-1], "10000", (), analyzer_selections=tuple(ComponentSelection(key, 1) for key in (sharpe_mode, "performance", "turnover", "fee_summary")))
            analysis = _admit_formal_analysis(spec, default_components(analyzers=spec.analyzer_selections), report, provider, "12345678-1234-4234-8234-123456789012")
            portfolio = _initial_portfolio(SimpleNamespace(spec=spec), analysis.initial_equity_snapshot)
        corporate_actions = None
        if with_dividend:
            # Memory Provider v1 does not serve action chunks. The separate
            # named action fixture is explicit, while market reads still use
            # the full Provider protocol and its genuine chunk boundaries.
            from tests.test_backtesting_dividend_events import StaticDividends, dividend_event
            corporate_actions = StaticDividends([dividend_event(event_id=UUID("98765432-1234-4234-8234-123456789012"), instrument_id=IID_A, record_date=days[20], effective_session=days[21])])
        runner = DeterministicBacktestRunner(
            run_id="12345678-1234-4234-8234-123456789012", axis=axis,
            timing_policy=AfterCloseToNextOpenV1(), view_factory=views,
            strategy=strategy, interpreter=TargetWeightsInterpreter(),
            execution_model=BarMarketExecutionModel(slippage_model=BpsSlippageModel.none(price_tick="0.01"), fee_calculator=FeeCalculator(schedule)),
            accounting=AccountingPolicy(currency="CNY", settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH),
            initial_portfolio=portfolio,
            analysis_admission=analysis,
            pit_data_gateway=SimpleNamespace(data_cutoff_at=lambda *, session_date, as_of: as_of),
            corporate_actions=corporate_actions,
            settlement_calendar=SessionListSettlementCalendar({CAL_ID: days}),
            fixed_authorized_instrument_ids=(IID_A,),
        )
        evidence = []
        def record(item):
            evidence.append(item)

        if expire_after_first_chunk:
            original_open = session.open_chunk
            def open_stale(query):
                chunk = original_open(query)
                provider.invalidate_revision()
                return chunk
            session.open_chunk = open_stale
        from app.backtesting.analysis_finalization import AnalysisFinalizationCoordinator
        result = run_data_session(
            session, runner, view_factory=views, evidence_sink=record,
            analysis_coordinator=AnalysisFinalizationCoordinator() if analysis_session_factory else None,
            analysis_session_factory=analysis_session_factory,
        )
        return result, strategy, evidence


def test_memory_provider_cross_chunk_orders_settlement_and_fees_are_deterministic():
    left, strategy, evidence = run_memory_acceptance()
    right, _, _ = run_memory_acceptance()
    assert len(evidence) == 2
    assert len(strategy.observed_contexts) == 24
    assert left.equity_curve == right.equity_curve
    assert left.equity_curve[-1].cumulative_fees == Decimal("10")
    assert left.equity_curve[-1].market_value == 0
    assert left.equity_curve[-1].cash == Decimal("9990")


def test_expired_memory_token_cannot_execute_a_business_phase():
    from app.backtesting.runtime import PhaseExecutionError
    with pytest.raises(PhaseExecutionError, match="revision|consistency|expired"):
        run_memory_acceptance(expire_after_first_chunk=True)


def test_production_analysis_admission_runs_through_memory_engine():
    result, _, _ = run_memory_acceptance(with_analysis=True)
    metrics = {row.metric_key: row for row in result.analysis_metrics}
    assert metrics["total_return"].value == Decimal("-0.001")
    assert metrics["cumulative_fees"].value == Decimal("10")
    assert metrics["volatility"].sample_count == 25


def test_memory_engine_credits_record_date_dividend_after_open_sale():
    result, _, _ = run_memory_acceptance(with_dividend=True)
    assert result.equity_curve[-1].cash == Decimal("10080")
    dividend, = [event for event in result.events if event.event_type == "cash_dividend_applied"]
    assert dividend.payload["entitlement_quantity"] == Decimal("900")
    assert dividend.payload["cash_delta"] == Decimal("90")
    assert dividend.phase_key == "cash_actions"


def test_disabled_pit_sharpe_does_not_prevent_other_metrics_from_finalizing():
    from sqlalchemy import select
    from app.backtesting.result_records import BacktestMetricRecord, BacktestAnalysisSummaryRecord
    from tests.test_backtesting_analysis_finalization import SqliteHarness
    harness = SqliteHarness()
    try:
        result, _, _ = run_memory_acceptance(
            with_analysis=True, sharpe_mode="sharpe_pit_rf",
            analysis_session_factory=harness.session_factory,
        )
        assert result.analysis_status == "final"
        with harness.session_factory() as session:
            summary = session.scalars(select(BacktestAnalysisSummaryRecord)).one()
            metrics = {row.metric_key: row for row in session.scalars(select(BacktestMetricRecord))}
            assert summary.status == "final"
            assert summary.completed_through_session == date(2026, 2, 5)
            assert "sharpe" not in metrics
            assert len(metrics) == 7
            assert metrics["total_return"].value == Decimal("-0.001")
            assert metrics["cumulative_fees"].value == Decimal("10")
    finally:
        harness.engine.dispose()
