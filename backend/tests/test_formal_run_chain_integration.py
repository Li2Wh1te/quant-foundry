"""Integration test: the unified formal run chain (task 04-03, section 8).

Chains the three delivered capability areas over one scenario:

    instrument_id
      → PIT mapping resolution and trading sessions
      → per-segment bar reads with strict coverage validation
      → calendar-resolved T+1 settlement plan for the buy fill
      → pre-open release of the due batch
      → normalized suspension/opening/price-limit facts
      → explicit (default-free) MarketState
      → opening match and accounting application

Scenario: an ETF changed its source code after 2026-08-19; the strategy
buys on Friday 2026-08-21; the exchange is closed on the weekend; on
Monday 2026-08-24 the T+1 batch is released before the open match, but
the instrument is suspended, so the sell order expires with a stable
reason while the release itself still happens on schedule.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.accounting import (
    AccountingPolicy,
    AccountState,
    Fill,
    OrderSide,
    PortfolioState,
    SettlementPolicy,
)
from app.backtesting.calendar_axis import (
    CalendarDefinition,
    CalendarSessionFact,
    InMemoryCalendarAxisDataProvider,
)
from app.backtesting.data.facts import Bar, FactEvidence, TradingStatus
from app.backtesting.data.pit_history import (
    read_segmented_history,
    resolve_pit_mappings,
)
from app.backtesting.data.requests import PriceBasis, QualityStatus
from app.backtesting.execution import (
    BarMarketExecutionModel,
    MatchContext,
    MarketState,
    Order,
    OrderStatus,
)
from app.backtesting.execution_facts import (
    CAPABILITY_DIMENSION_OPENING_AVAILABILITY as OPENING,
)
from app.backtesting.execution_facts import (
    CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY as LIMIT,
)
from app.backtesting.execution_facts import (
    CAPABILITY_DIMENSION_SUSPENSION as SUSPENSION,
)
from app.backtesting.execution_facts import (
    evaluate_execution_facts,
    market_state_from_execution_facts,
)
from app.backtesting.fees import FeeCalculator, FeeSchedule
from app.backtesting.settlement import (
    CalendarAxisSettlementGateway,
    UnsupportedSettlementRuleError,
    require_formal_settlement_policy,
    settlement_plan_for_fill,
)
from app.backtesting.slippage import BpsSlippageModel
from app.instruments.domain import InstrumentCodeMapping
from tests.test_backtesting_settlement import calendar_provider

INSTRUMENT_ID = uuid4()
SOURCE = "tushare"
CALENDAR_ID = "SSE"
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)
KNOWN_AT = datetime(2025, 12, 1, tzinfo=UTC)

CODE_CHANGE_DAY = date(2026, 8, 20)
SESSIONS = [
    date(2026, 8, 18),
    date(2026, 8, 19),
    date(2026, 8, 20),
    date(2026, 8, 21),
    date(2026, 8, 24),
]
BUY_SESSION = date(2026, 8, 21)


class _SegmentReader:
    """Serves bars keyed by (source_code, trade_date)."""

    def __init__(self, bars):
        self._bars = bars

    def read_bars(self, source_code, start_date, end_date):
        return [
            bar
            for (code, day), bar in sorted(self._bars.items())
            if code == source_code and start_date <= day <= end_date
        ]


def _bar(day: date) -> Bar:
    return Bar(
        instrument_id=INSTRUMENT_ID,
        trade_date=day,
        frequency="1d",
        open="1.000",
        high="1.010",
        low="0.990",
        close="1.005",
        volume="1000",
        amount="1000",
        price_basis=PriceBasis.RAW,
        evidence=FactEvidence(
            source=SOURCE,
            observed_at=datetime(2026, 8, 21, tzinfo=UTC),
            quality_status=QualityStatus.COMPLETE,
            known_at=datetime(2026, 8, 21, tzinfo=UTC),
        ),
    )


def _mappings():
    return [
        InstrumentCodeMapping(
            instrument_id=INSTRUMENT_ID,
            source=SOURCE,
            source_code="OLD.CODE",
            trading_code="510300",
            valid_from=date(2026, 1, 1),
            valid_to=CODE_CHANGE_DAY,
            mapping_source="exchange_announcement",
            evidence="announcement 2026-001",
            known_at=KNOWN_AT,
            observed_at=KNOWN_AT,
        ),
        InstrumentCodeMapping(
            instrument_id=INSTRUMENT_ID,
            source=SOURCE,
            source_code="NEW.CODE",
            trading_code="510300",
            valid_from=CODE_CHANGE_DAY,
            valid_to=None,
            mapping_source="exchange_announcement",
            evidence="announcement 2026-002",
            known_at=KNOWN_AT,
            observed_at=KNOWN_AT,
        ),
    ]


def _status_fact(dimension, status, **kwargs):
    attributes = {"dimension": dimension}
    attributes.update(kwargs.pop("attributes", {}))
    return TradingStatus(
        instrument_id=INSTRUMENT_ID,
        status=status,
        valid_from=date(2026, 8, 24),
        evidence=FactEvidence(
            source="exchange_status_feed",
            observed_at=datetime(2026, 8, 24, tzinfo=UTC),
            quality_status=QualityStatus.COMPLETE,
            known_at=datetime(2026, 8, 24, tzinfo=UTC),
        ),
        attributes=attributes,
        **kwargs,
    )


class UnifiedRunChainTestCase(unittest.TestCase):
    """One deterministic pass over the full formal chain."""

    def test_chain_from_identity_to_match_and_accounting(self) -> None:
        # --- 1. PIT mapping resolution over the calendar sessions -------
        resolution = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=SESSIONS,
            mappings=_mappings(),
            data_cutoff=CUTOFF,
        )
        self.assertEqual(resolution.session_bindings[BUY_SESSION], "NEW.CODE")
        self.assertEqual(len(resolution.segments), 2)
        summary = resolution.evidence_summary
        self.assertEqual(summary["segment_count"], 2)

        # --- 2. Segmented bar read with strict coverage -----------------
        bars = {
            ("OLD.CODE", day): _bar(day)
            for day in SESSIONS
            if day < CODE_CHANGE_DAY
        }
        bars.update(
            {("NEW.CODE", day): _bar(day) for day in SESSIONS if day >= CODE_CHANGE_DAY}
        )
        history = read_segmented_history(resolution, _SegmentReader(bars))
        self.assertEqual(
            [bar.trade_date for bar in history.bars], SESSIONS
        )
        self.assertTrue(
            all(bar.instrument_id == INSTRUMENT_ID for bar in history.bars)
        )

        # --- 3. Calendar-resolved settlement plan for the buy -----------
        provider = calendar_provider(set(SESSIONS))
        gateway = CalendarAxisSettlementGateway(provider)
        plan = settlement_plan_for_fill(
            gateway, calendar_id=CALENDAR_ID, trade_session=BUY_SESSION
        )
        # Weekend 08-22/23 is skipped by the exchange calendar.
        self.assertEqual(plan.settlement_session, date(2026, 8, 24))

        # --- 4. Buy fill: quantity moves, availability does not ---------
        portfolio = PortfolioState(
            account=AccountState(
                cash_balances={"CNY": "100000"},
                available_cash="100000",
                frozen_cash="0",
                margin_used="0",
                margin_available="0",
                equity="100000",
            ),
            as_of=datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
        )
        accounting = AccountingPolicy()
        buy_fill = Fill(
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            timestamp=datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
            side=OrderSide.BUY,
            price="1.005",
            quantity="1000",
            fees="0",
        )
        accounting.apply_fill(portfolio, buy_fill, settlement_plan=plan)
        position = portfolio.positions[INSTRUMENT_ID]
        self.assertEqual(position.quantity, Decimal("1000"))
        self.assertEqual(position.available_quantity, Decimal("0"))

        # --- 5. Pre-open release on Monday, then suspended matching -----
        released = accounting.settle_pending_before_open_match(
            portfolio, calendar_id=CALENDAR_ID, session_date=date(2026, 8, 24)
        )
        self.assertEqual(released, (INSTRUMENT_ID,))
        self.assertEqual(position.available_quantity, Decimal("1000"))
        batch = accounting.settled_batches()[0]
        self.assertEqual(batch.trade_session, BUY_SESSION)
        self.assertEqual(batch.settlement_session, date(2026, 8, 24))
        self.assertEqual(batch.source_fill_id, buy_fill.fill_id)

        resolved, issues = evaluate_execution_facts(
            INSTRUMENT_ID,
            calendar_id=CALENDAR_ID,
            session_date=date(2026, 8, 24),
            applicability={
                SUSPENSION: "required",
                OPENING: "required",
                LIMIT: "not_applicable",
            },
            status_facts=[
                _status_fact(SUSPENSION, "suspended"),
                _status_fact(OPENING, "available"),
            ],
            data_cutoff=datetime(2026, 8, 25, tzinfo=UTC),
            rule_package_reference="china_listed_etf_rules@1",
        )
        self.assertEqual(issues, ())
        assert resolved is not None
        self.assertEqual(resolved.suspension_state.value, "suspended")
        # The declared-not-applicable limit dimension is recorded, not guessed.
        self.assertEqual(
            resolved.evidence[LIMIT]["applicability"], "not_applicable"
        )

        state = market_state_from_execution_facts(
            resolved,
            open_price="1.005",
            price_tick="0.001",
            timestamp=datetime(2026, 8, 24, 1, 30, tzinfo=timezone.utc),
        )
        self.assertTrue(state.is_suspended)
        self.assertIn(SUSPENSION, state.facts_basis)

        model = BarMarketExecutionModel(
            slippage_model=BpsSlippageModel(slippage_bps="0", price_tick="0.001"),
            fee_calculator=FeeCalculator(schedule=FeeSchedule(key="test", fee_rules=())),
        )
        sell = Order(
            order_id=uuid4(),
            intent_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            side=OrderSide.SELL,
            quantity="1000",
            submitted_at=datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc),
        )
        result = model.match(
            [sell],
            {INSTRUMENT_ID: state},
            MatchContext.from_portfolio(portfolio),
        )
        self.assertEqual(result.fills, ())
        self.assertEqual(sell.status, OrderStatus.EXPIRED)
        self.assertEqual(sell.status_reason, "instrument_suspended")

    def test_blocked_preflight_reports_all_missing_dimensions(self) -> None:
        resolved, issues = evaluate_execution_facts(
            INSTRUMENT_ID,
            calendar_id=CALENDAR_ID,
            session_date=date(2026, 8, 24),
            applicability={
                SUSPENSION: "required",
                OPENING: "required",
                LIMIT: "required",
            },
            status_facts=[],
            data_cutoff=CUTOFF,
            rule_package_reference="china_listed_etf_rules@1",
        )
        self.assertIsNone(resolved)
        codes = {issue.code for issue in issues}
        self.assertEqual(
            codes,
            {
                "trading_status_fact_missing",
                "opening_availability_fact_missing",
                "price_limit_tradability_fact_missing",
            },
        )
        # Every issue is locatable to instrument, session, and dimension.
        for issue in issues:
            self.assertEqual(
                issue.details["instrument_id"], str(INSTRUMENT_ID)
            )
            self.assertEqual(issue.details["session_date"], "2026-08-24")

    def test_formal_settlement_gate_blocks_same_day(self) -> None:
        with self.assertRaises(UnsupportedSettlementRuleError):
            require_formal_settlement_policy(SettlementPolicy.SAME_DAY)


if __name__ == "__main__":
    unittest.main()
