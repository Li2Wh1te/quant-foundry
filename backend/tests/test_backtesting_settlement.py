"""Tests for T+1-before-open-match settlement (task 04-03, 7A).

Covers the acceptance matrix of section 9.2: same-session availability,
release before the next open session's match, weekend/holiday deferral
through the trading calendar, suspension not shifting settlement, formal
settlement-class gating, and missed-boundary detection.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.accounting import (
    AccountingPolicy,
    AccountState,
    DeferredSettlementPlan,
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
from app.backtesting.domain import DomainValidationError
from app.backtesting.settlement import (
    FORMAL_SETTLEMENT_RULE_CLASS,
    CalendarAxisSettlementGateway,
    SettlementCalendarUnresolvedError,
    SettlementNextSessionMissingError,
    UnsupportedSettlementRuleError,
    require_formal_settlement_policy,
    settlement_plan_for_fill,
    settlement_policy_for_rule_class,
)

INSTRUMENT_ID = uuid4()
FILL_TS = datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)


def make_portfolio(cash: str = "100000") -> PortfolioState:
    return PortfolioState(
        account=AccountState(
            cash_balances={"CNY": cash},
            available_cash=cash,
            frozen_cash="0",
            margin_used="0",
            margin_available="0",
            equity=cash,
        ),
        as_of=FILL_TS,
    )


def make_buy(*, quantity: Decimal = Decimal("1000")) -> Fill:
    return Fill(
        fill_id=uuid4(),
        order_id=uuid4(),
        instrument_id=INSTRUMENT_ID,
        timestamp=FILL_TS,
        side=OrderSide.BUY,
        price="10",
        quantity=quantity,
        fees="0",
    )


CALENDAR_VERSION = "china_exchange_daily@1"
CHINA_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))


def calendar_provider(
    open_days: set[date],
    *,
    calendar_id: str = "SSE",
    fact_from: date = date(2026, 1, 1),
    fact_to: date = date(2026, 12, 31),
) -> InMemoryCalendarAxisDataProvider:
    """Build an in-memory axis provider over an explicit open-day set."""

    definition = CalendarDefinition(
        calendar_id=calendar_id,
        definition_version=CALENDAR_VERSION,
        timezone="Asia/Shanghai",
        default_sessions=CHINA_SESSIONS,
        source="test",
    )
    facts = []
    day = fact_from
    while day <= fact_to:
        facts.append(
            CalendarSessionFact(
                calendar_id=calendar_id,
                session_date=day,
                is_open=day in open_days,
                definition_version=CALENDAR_VERSION,
                source="test",
            )
        )
        day += timedelta(days=1)
    return InMemoryCalendarAxisDataProvider([definition], facts)


class NextOpenSessionTestCase(unittest.TestCase):
    """Calendar-based resolution of the next settlement session."""

    def test_weekend_defers_settlement_to_next_open_session(self) -> None:
        # Friday buy; Saturday/Sunday closed; Monday open.
        provider = calendar_provider({date(2026, 8, 21), date(2026, 8, 24)})
        gateway = CalendarAxisSettlementGateway(provider)

        plan = settlement_plan_for_fill(
            gateway, calendar_id="SSE", trade_session=date(2026, 8, 21)
        )

        self.assertEqual(plan.trade_session, date(2026, 8, 21))
        self.assertEqual(plan.settlement_session, date(2026, 8, 24))
        self.assertEqual(plan.calendar_id, "SSE")

    def test_extended_holiday_cluster_skips_forward(self) -> None:
        open_days = {date(2026, 8, 21), date(2026, 10, 9)}
        gateway = CalendarAxisSettlementGateway(calendar_provider(open_days))

        plan = settlement_plan_for_fill(
            gateway, calendar_id="SSE", trade_session=date(2026, 8, 21)
        )

        self.assertEqual(plan.settlement_session, date(2026, 10, 9))

    def test_missing_calendar_fact_blocks_as_unresolved(self) -> None:
        # Facts exist only up to 2026-08-25.
        gateway = CalendarAxisSettlementGateway(
            calendar_provider(
                {date(2026, 8, 21)},
                fact_to=date(2026, 8, 25),
            )
        )

        with self.assertRaises(SettlementCalendarUnresolvedError) as ctx:
            settlement_plan_for_fill(
                gateway, calendar_id="SSE", trade_session=date(2026, 8, 25)
            )
        self.assertEqual(ctx.exception.code, "settlement_calendar_unresolved")

    def test_no_open_session_within_horizon_blocks(self) -> None:
        gateway = CalendarAxisSettlementGateway(
            calendar_provider(set(), fact_to=date(2027, 12, 31))
        )

        with self.assertRaises(SettlementNextSessionMissingError) as ctx:
            settlement_plan_for_fill(
                gateway, calendar_id="SSE", trade_session=date(2026, 8, 21)
            )
        self.assertEqual(ctx.exception.code, "settlement_next_session_missing")


class FormalSettlementGateTestCase(unittest.TestCase):
    """Only t1_before_open_match enters formal runs."""

    def test_formal_class_is_accepted(self) -> None:
        require_formal_settlement_policy(
            SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH
        )

    def test_same_day_is_blocked(self) -> None:
        with self.assertRaises(UnsupportedSettlementRuleError) as ctx:
            require_formal_settlement_policy(SettlementPolicy.SAME_DAY)
        self.assertEqual(ctx.exception.code, "settlement_rule_unsupported")

    def test_legacy_t_plus_one_is_blocked(self) -> None:
        with self.assertRaises(UnsupportedSettlementRuleError):
            require_formal_settlement_policy(SettlementPolicy.T_PLUS_ONE)

    def test_rule_class_mapping_rejects_unknown_classes(self) -> None:
        self.assertEqual(
            settlement_policy_for_rule_class(FORMAL_SETTLEMENT_RULE_CLASS),
            SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH,
        )
        self.assertIsNone(settlement_policy_for_rule_class("weekly"))


class SettlementLifecycleTestCase(unittest.TestCase):
    """Buy-then-release lifecycle under the default formal policy."""

    PLAN = DeferredSettlementPlan(
        calendar_id="SSE",
        trade_session=date(2026, 8, 21),
        settlement_session=date(2026, 8, 24),
    )

    def test_buy_increases_quantity_but_not_availability(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()

        policy.apply_fill(portfolio, make_buy(), settlement_plan=self.PLAN)

        position = portfolio.positions[INSTRUMENT_ID]
        self.assertEqual(position.quantity, Decimal("1000"))
        self.assertEqual(position.available_quantity, Decimal("0"))

    def test_release_happens_at_next_open_session_pre_match(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        policy.apply_fill(portfolio, make_buy(), settlement_plan=self.PLAN)

        released = policy.settle_pending_before_open_match(
            portfolio, calendar_id="SSE", session_date=date(2026, 8, 24)
        )

        self.assertEqual(released, (INSTRUMENT_ID,))
        self.assertEqual(
            portfolio.positions[INSTRUMENT_ID].available_quantity,
            Decimal("1000"),
        )

    def test_batches_keep_fill_level_audit_trail(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        fill_a = make_buy(quantity=Decimal("400"))
        fill_b = make_buy(quantity=Decimal("600"))
        policy.apply_fill(portfolio, fill_a, settlement_plan=self.PLAN)
        policy.apply_fill(portfolio, fill_b, settlement_plan=self.PLAN)

        batches = policy.pending_batches()
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].quantity, Decimal("400"))
        self.assertEqual(batches[0].source_fill_id, fill_a.fill_id)
        self.assertEqual(batches[1].source_fill_id, fill_b.fill_id)

    def test_batches_from_different_sessions_stay_separate(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        fill_a = make_buy(quantity=Decimal("400"))
        policy.apply_fill(portfolio, fill_a, settlement_plan=self.PLAN)

        # A second buy on a later session keeps its own batch and audit trail.
        later_ts = datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc)
        later_plan = DeferredSettlementPlan(
            calendar_id="SSE",
            trade_session=date(2026, 8, 24),
            settlement_session=date(2026, 8, 25),
        )
        later_fill = Fill(
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            timestamp=later_ts,
            side=OrderSide.BUY,
            price="10",
            quantity="600",
            fees="0",
        )
        policy.apply_fill(portfolio, later_fill, settlement_plan=later_plan)

        batches = policy.pending_batches()
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].trade_session, date(2026, 8, 21))
        self.assertEqual(batches[0].settlement_session, date(2026, 8, 24))
        self.assertEqual(batches[1].trade_session, date(2026, 8, 24))
        self.assertEqual(batches[1].settlement_session, date(2026, 8, 25))

        # Releasing on 2026-08-24 frees only the first batch.
        released = policy.settle_pending_before_open_match(
            portfolio, calendar_id="SSE", session_date=date(2026, 8, 24)
        )
        self.assertEqual(released, (INSTRUMENT_ID,))
        self.assertEqual(
            portfolio.positions[INSTRUMENT_ID].available_quantity,
            Decimal("400"),
        )
        self.assertEqual(len(policy.pending_batches()), 1)

    def test_missed_boundary_blocks_instead_of_catching_up(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        # A fill whose scheduled release session was already passed by the
        # runner (it first calls settle on 2026-08-25).
        early_ts = datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc)
        early_plan = DeferredSettlementPlan(
            calendar_id="SSE",
            trade_session=date(2026, 8, 20),
            settlement_session=date(2026, 8, 21),
        )
        early_fill = Fill(
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            timestamp=early_ts,
            side=OrderSide.BUY,
            price="10",
            quantity="1000",
            fees="0",
        )
        portfolio.as_of = early_ts
        policy.apply_fill(portfolio, early_fill, settlement_plan=early_plan)

        with self.assertRaises(DomainValidationError) as ctx:
            policy.settle_pending_before_open_match(
                portfolio, calendar_id="SSE", session_date=date(2026, 8, 25)
            )
        self.assertEqual(ctx.exception.code, "settlement_boundary_missed")
        # Nothing was released by the failed call.
        self.assertEqual(
            portfolio.positions[INSTRUMENT_ID].available_quantity,
            Decimal("0"),
        )

    def test_foreign_calendar_batch_is_never_released_by_wrong_calendar(self) -> None:
        # A batch bound to SZSE must not be releasable through an SSE call,
        # even on the same calendar date.
        szse_plan = DeferredSettlementPlan(
            calendar_id="SZSE",
            trade_session=date(2026, 8, 21),
            settlement_session=date(2026, 8, 24),
        )
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        policy.apply_fill(portfolio, make_buy(), settlement_plan=szse_plan)

        released = policy.settle_pending_before_open_match(
            portfolio, calendar_id="SSE", session_date=date(2026, 8, 24)
        )

        self.assertEqual(released, ())
        self.assertEqual(
            portfolio.positions[INSTRUMENT_ID].available_quantity,
            Decimal("0"),
        )
        self.assertEqual(len(policy.pending_batches()), 1)

        # The batch still releases normally under its own calendar.
        released = policy.settle_pending_before_open_match(
            portfolio, calendar_id="SZSE", session_date=date(2026, 8, 24)
        )
        self.assertEqual(released, (INSTRUMENT_ID,))

    def test_multi_batch_release_is_atomic_on_failure(self) -> None:
        # Two due batches; the second refers to a position that vanished.
        # The failure must leave the first position and both batches
        # untouched so a retry cannot double-release.
        other = uuid4()
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        plan_a = DeferredSettlementPlan(
            calendar_id="SSE",
            trade_session=date(2026, 8, 21),
            settlement_session=date(2026, 8, 24),
        )
        fill_a = Fill(
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            timestamp=datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
            side=OrderSide.BUY,
            price="10",
            quantity="400",
            fees="0",
        )
        policy.apply_fill(portfolio, fill_a, settlement_plan=plan_a)

        later_ts = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
        plan_b = DeferredSettlementPlan(
            calendar_id="SSE",
            trade_session=date(2026, 8, 21),
            settlement_session=date(2026, 8, 24),
        )
        fill_b = Fill(
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=other,
            timestamp=later_ts,
            side=OrderSide.BUY,
            price="10",
            quantity="600",
            fees="0",
        )
        portfolio.as_of = later_ts
        policy.apply_fill(portfolio, fill_b, settlement_plan=plan_b)
        # Simulate the loss of the second position after both batches are
        # pending but before the release call.
        del portfolio.positions[other]

        from app.backtesting.accounting import AccountingError

        with self.assertRaises(AccountingError):
            policy.settle_pending_before_open_match(
                portfolio, calendar_id="SSE", session_date=date(2026, 8, 24)
            )

        # Atomic rollback: the first position is unchanged and both
        # batches remain pending exactly once.
        self.assertEqual(
            portfolio.positions[INSTRUMENT_ID].available_quantity,
            Decimal("0"),
        )
        self.assertEqual(len(policy.pending_batches()), 2)


if __name__ == "__main__":
    unittest.main()
