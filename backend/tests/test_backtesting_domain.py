"""Tests for the in-memory backtesting account and portfolio domain objects."""

from datetime import datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.domain import (
    AccountSnapshot,
    AccountState,
    DomainValidationError,
    PositionSide,
    PositionState,
    PortfolioState,
    ValuationStatus,
)


AS_OF = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)


def account() -> AccountState:
    return AccountState(
        cash_balances={"CNY": "1000000"},
        available_cash="900000",
        frozen_cash="100000",
        margin_used="0",
        margin_available="0",
        equity="1000000",
    )


class AccountStateTestCase(unittest.TestCase):
    def test_state_normalizes_values_and_snapshot_copies_cash_mapping(self) -> None:
        cash_balances = {"CNY": "100.10"}
        state = AccountState(
            cash_balances=cash_balances,
            available_cash=100,
            frozen_cash=Decimal("0.10"),
            margin_used=0,
            margin_available=0,
            equity="100.10",
        )

        snapshot = state.snapshot()
        cash_balances["CNY"] = "1"
        state.cash_balances["CNY"] = Decimal("99")

        self.assertEqual(snapshot.cash_balances["CNY"], Decimal("100.10"))
        self.assertEqual(snapshot.available_cash, Decimal("100"))
        self.assertIsInstance(snapshot, AccountSnapshot)

    def test_float_is_rejected_to_keep_decimal_arithmetic_explicit(self) -> None:
        with self.assertRaises(TypeError):
            AccountState(
                cash_balances={"CNY": 1.0},
                available_cash=1,
                frozen_cash=0,
                margin_used=0,
                margin_available=0,
                equity=1,
            )

    def test_negative_cash_control_field_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            AccountState(
                cash_balances={"CNY": "100"},
                available_cash="-1",
                frozen_cash=0,
                margin_used=0,
                margin_available=0,
                equity=100,
            )


class PositionStateTestCase(unittest.TestCase):
    def test_zero_position_is_runtime_only_and_not_snapshotted(self) -> None:
        state = PositionState(uuid4(), PositionSide.LONG)

        self.assertTrue(state.is_zero)
        with self.assertRaises(DomainValidationError):
            state.snapshot()

    def test_available_quantity_cannot_exceed_total_quantity(self) -> None:
        with self.assertRaises(DomainValidationError):
            PositionState(
                uuid4(),
                PositionSide.LONG,
                quantity="10",
                available_quantity="11",
                average_price="10",
            )

    def test_missing_mark_price_is_not_replaced_with_zero(self) -> None:
        state = PositionState(
            uuid4(),
            PositionSide.LONG,
            quantity="10",
            available_quantity="10",
            average_price="10",
        )

        self.assertIsNone(state.mark_price)
        self.assertIsNone(state.snapshot().mark_price)


class PortfolioStateTestCase(unittest.TestCase):
    def test_snapshot_contains_only_non_zero_positions_in_stable_order(self) -> None:
        first_id = uuid4()
        second_id = uuid4()
        positions = {
            second_id: PositionState(
                second_id,
                PositionSide.LONG,
                quantity="2",
                available_quantity="2",
                average_price="20",
                mark_price="21",
            ),
            first_id: PositionState(first_id, PositionSide.LONG),
        }
        portfolio = PortfolioState(account(), AS_OF, positions)

        snapshot = portfolio.snapshot()

        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].instrument_id, second_id)
        self.assertEqual(snapshot.as_of, AS_OF)
        self.assertEqual(snapshot.valuation_status, ValuationStatus.COMPLETE)

    def test_portfolio_rejects_naive_timestamp(self) -> None:
        with self.assertRaises(DomainValidationError):
            PortfolioState(account(), datetime(2026, 8, 22))

    def test_snapshot_is_detached_from_future_runtime_mutations(self) -> None:
        instrument_id = uuid4()
        position = PositionState(
            instrument_id,
            PositionSide.LONG,
            quantity="10",
            available_quantity="10",
            average_price="10",
            mark_price="11",
        )
        portfolio = PortfolioState(account(), AS_OF, {instrument_id: position})

        snapshot = portfolio.snapshot()
        position.quantity = Decimal("5")
        portfolio.account.cash_balances["CNY"] = Decimal("0")

        self.assertEqual(snapshot.positions[0].quantity, Decimal("10"))
        self.assertEqual(snapshot.account.cash_balances["CNY"], Decimal("1000000"))


if __name__ == "__main__":
    unittest.main()
