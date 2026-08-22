"""Tests for fill application, settlement, and daily mark-to-market rules."""

from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.accounting import (
    AccountingPolicy,
    DeferredSettlementPlan,
    Fill,
    InsufficientCashError,
    OrderSide,
    SettlementPolicy,
)
from app.backtesting.domain import (
    AccountState,
    PortfolioState,
    ValuationStatus,
)


START = datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc)
OPEN = datetime(2026, 8, 22, 1, 30, tzinfo=timezone.utc)
NEXT_OPEN = datetime(2026, 8, 23, 1, 30, tzinfo=timezone.utc)

TRADE_SESSION = date(2026, 8, 22)
SETTLEMENT_SESSION = date(2026, 8, 24)
PLAN = DeferredSettlementPlan(
    calendar_id="SSE",
    trade_session=TRADE_SESSION,
    settlement_session=SETTLEMENT_SESSION,
)


def make_portfolio() -> PortfolioState:
    """Build a single-currency cash portfolio for deterministic fixtures."""

    return PortfolioState(
        account=AccountState(
            cash_balances={"CNY": "100000"},
            available_cash="100000",
            frozen_cash="0",
            margin_used="0",
            margin_available="0",
            equity="100000",
        ),
        as_of=START,
    )


def make_buy(*, fill_id=None, price="21", quantity="1400", fees="10") -> Fill:
    """Create one next-session buy fill at its final execution price."""

    return Fill(
        fill_id=fill_id or uuid4(),
        order_id=uuid4(),
        instrument_id=INSTRUMENT_ID,
        timestamp=OPEN,
        side=OrderSide.BUY,
        reference_price="20.98",
        price=price,
        quantity=quantity,
        fees=fees,
    )


INSTRUMENT_ID = uuid4()


class AccountingPolicyTestCase(unittest.TestCase):
    """Cover the first long-only, single-currency accounting contract."""

    def test_buy_includes_fees_in_cost_and_t_plus_one_holds_units(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()

        result = policy.apply_fill(portfolio, make_buy(), settlement_plan=PLAN)

        self.assertTrue(result.applied)
        self.assertEqual(result.cash_delta, Decimal("-29410"))
        self.assertEqual(portfolio.account.cash_balances["CNY"], Decimal("70590"))
        self.assertEqual(portfolio.account.available_cash, Decimal("70590"))
        position = portfolio.positions[INSTRUMENT_ID]
        self.assertEqual(position.quantity, Decimal("1400"))
        self.assertEqual(position.available_quantity, Decimal("0"))
        self.assertEqual(
            position.average_price,
            Decimal("29410") / Decimal("1400"),
        )

        released = policy.settle_pending_before_open_match(
            portfolio, calendar_id="SSE", session_date=SETTLEMENT_SESSION
        )
        self.assertEqual(released, (INSTRUMENT_ID,))
        self.assertEqual(position.available_quantity, Decimal("1400"))
        self.assertEqual(policy.pending_batches(), ())
        self.assertEqual(len(policy.settled_batches()), 1)

    def test_sell_updates_cash_and_realized_pnl_then_removes_zero_position(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        policy.apply_fill(portfolio, make_buy(), settlement_plan=PLAN)
        policy.settle_pending_before_open_match(
            portfolio, calendar_id="SSE", session_date=SETTLEMENT_SESSION
        )

        result = policy.apply_fill(
            portfolio,
            Fill(
                fill_id=uuid4(),
                order_id=uuid4(),
                instrument_id=INSTRUMENT_ID,
                timestamp=NEXT_OPEN,
                side=OrderSide.SELL,
                price="23",
                quantity="600",
                fees="10",
            ),
        )

        self.assertEqual(result.cash_delta, Decimal("13790"))
        self.assertEqual(result.realized_pnl_delta, Decimal("13790") - (Decimal("29410") / Decimal("1400") * 600))
        self.assertEqual(portfolio.account.cash_balances["CNY"], Decimal("84380"))
        position = portfolio.positions[INSTRUMENT_ID]
        self.assertEqual(position.quantity, Decimal("800"))
        self.assertEqual(position.available_quantity, Decimal("800"))
        self.assertEqual(position.average_price, Decimal("29410") / Decimal("1400"))

        policy.apply_fill(
            portfolio,
            Fill(
                fill_id=uuid4(),
                order_id=uuid4(),
                instrument_id=INSTRUMENT_ID,
                timestamp=NEXT_OPEN,
                side=OrderSide.SELL,
                price="23",
                quantity="800",
                fees="0",
            ),
        )
        self.assertNotIn(INSTRUMENT_ID, portfolio.positions)

    def test_insufficient_cash_rejects_atomically(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        before = portfolio.snapshot()

        with self.assertRaises(InsufficientCashError):
            policy.apply_fill(
                portfolio,
                make_buy(price="25", quantity="4001", fees="1"),
                settlement_plan=PLAN,
            )

        after = portfolio.snapshot()
        self.assertEqual(after.account.cash_balances, before.account.cash_balances)
        self.assertEqual(after.positions, before.positions)
        self.assertEqual(after.as_of, before.as_of)

    def test_duplicate_fill_is_idempotent(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy(settlement_policy=SettlementPolicy.SAME_DAY)
        fill = make_buy()

        first = policy.apply_fill(portfolio, fill)
        second = policy.apply_fill(portfolio, fill)

        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertEqual(portfolio.account.cash_balances["CNY"], Decimal("70590"))
        self.assertEqual(portfolio.positions[INSTRUMENT_ID].quantity, Decimal("1400"))

    def test_sell_before_t_plus_one_settlement_is_rejected(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        policy.apply_fill(portfolio, make_buy(), settlement_plan=PLAN)

        with self.assertRaisesRegex(ValueError, "available_quantity"):
            policy.apply_fill(
                portfolio,
                Fill(
                    fill_id=uuid4(),
                    order_id=uuid4(),
                    instrument_id=INSTRUMENT_ID,
                    timestamp=NEXT_OPEN,
                    side=OrderSide.SELL,
                    price="23",
                    quantity="1",
                ),
            )

    def test_valuation_uses_marks_and_blocks_missing_data_without_zero_price(self) -> None:
        portfolio = make_portfolio()
        policy = AccountingPolicy()
        policy.apply_fill(portfolio, make_buy(), settlement_plan=PLAN)

        complete = policy.value(portfolio, {INSTRUMENT_ID: "22"}, as_of=NEXT_OPEN)

        self.assertEqual(complete.market_value, Decimal("30800"))
        self.assertEqual(complete.snapshot.account.equity, Decimal("101390"))
        self.assertEqual(complete.snapshot.valuation_status, ValuationStatus.COMPLETE)
        self.assertEqual(complete.snapshot.positions[0].mark_price, Decimal("22"))
        expected_unrealized = (
            Decimal("22") - Decimal("29410") / Decimal("1400")
        ) * Decimal("1400")
        self.assertEqual(
            complete.snapshot.positions[0].unrealized_pnl,
            expected_unrealized,
        )

        blocked = policy.value(portfolio, {}, as_of=NEXT_OPEN)

        self.assertIsNone(blocked.market_value)
        self.assertEqual(blocked.snapshot.valuation_status, ValuationStatus.BLOCKED)
        self.assertIsNone(blocked.snapshot.positions[0].mark_price)


if __name__ == "__main__":
    unittest.main()
