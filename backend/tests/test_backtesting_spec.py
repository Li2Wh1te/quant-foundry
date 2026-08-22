"""Tests for single-run backtest specification validation."""

from datetime import date
from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.domain import DomainValidationError, PositionSide
from app.backtesting.spec import BacktestSpec, InitialPositionInput


def position(
    quantity: Decimal | int | str = "100",
    available_quantity: Decimal | int | str = "100",
    average_price: Decimal | int | str | None = "10.50",
    instrument_id=None,
) -> InitialPositionInput:
    """Build one valid opening position with overridable fields."""

    return InitialPositionInput(
        instrument_id=instrument_id or uuid4(),
        side=PositionSide.LONG,
        quantity=quantity,
        available_quantity=available_quantity,
        average_price=average_price,
    )


class InitialPositionInputTestCase(unittest.TestCase):
    def test_negative_quantity_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            position(quantity="-1")

    def test_available_quantity_bounds_are_enforced(self) -> None:
        with self.assertRaises(DomainValidationError):
            position(available_quantity="-1")
        with self.assertRaises(DomainValidationError):
            position(quantity="100", available_quantity="101")

    def test_non_positive_average_price_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            position(average_price="0")
        with self.assertRaises(DomainValidationError):
            position(average_price="-5")

    def test_non_zero_position_requires_average_price(self) -> None:
        with self.assertRaises(DomainValidationError):
            position(average_price=None)

    def test_missing_available_quantity_is_rejected(self) -> None:
        # available_quantity is a required constructor argument; omitting it
        # must fail instead of defaulting to an implicit value.
        with self.assertRaises(TypeError):
            InitialPositionInput(
                instrument_id=uuid4(),
                side=PositionSide.LONG,
                quantity="100",
                average_price="10.50",
            )

    def test_float_inputs_are_rejected_everywhere(self) -> None:
        with self.assertRaises(TypeError):
            position(quantity=100.0)
        with self.assertRaises(TypeError):
            position(available_quantity=10.0)
        with self.assertRaises(TypeError):
            position(average_price=10.5)

    def test_side_must_match_domain_model(self) -> None:
        with self.assertRaises(DomainValidationError):
            InitialPositionInput(
                instrument_id=uuid4(),
                side="cross",  # type: ignore[arg-type]
                quantity="1",
                available_quantity="1",
                average_price="1",
            )

    def test_instrument_id_must_be_stable_uuid(self) -> None:
        with self.assertRaises(DomainValidationError):
            InitialPositionInput(
                instrument_id="600519.SH",  # type: ignore[arg-type]
                side=PositionSide.LONG,
                quantity="100",
                available_quantity="100",
                average_price="10.50",
            )


class BacktestSpecTestCase(unittest.TestCase):
    def build(
        self,
        start: date = date(2026, 8, 3),
        end: date = date(2026, 8, 21),
        initial_cash: Decimal | int | str = "1000000",
        positions: list[InitialPositionInput] | None = None,
        **kwargs,
    ) -> BacktestSpec:
        return BacktestSpec(
            start_date=start,
            end_date=end,
            initial_cash=initial_cash,
            initial_positions=(
                positions if positions is not None else [position()]
            ),
            **kwargs,
        )

    def test_start_after_end_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.build(start=date(2026, 8, 21), end=date(2026, 8, 3))

    def test_datetime_boundaries_are_rejected(self) -> None:
        from datetime import datetime, timezone

        with self.assertRaises(DomainValidationError):
            self.build(start=datetime(2026, 8, 3, tzinfo=timezone.utc))  # type: ignore[arg-type]

    def test_initial_cash_accepts_decimal_int_and_str(self) -> None:
        for cash in (Decimal("1000.00"), 1000, "1000"):
            with self.subTest(cash=cash):
                spec = self.build(initial_cash=cash)
                self.assertEqual(spec.initial_cash, Decimal("1000"))

    def test_initial_cash_rejects_float_and_illegal_values(self) -> None:
        with self.assertRaises(TypeError):
            self.build(initial_cash=1000000.0)
        with self.assertRaises(DomainValidationError):
            self.build(initial_cash="one million")
        with self.assertRaises(DomainValidationError):
            self.build(initial_cash="-1")

    def test_duplicate_instrument_ids_are_rejected_even_with_zero_row(self) -> None:
        duplicate = uuid4()
        first = position(instrument_id=duplicate)
        second = position(instrument_id=duplicate)
        with self.assertRaises(DomainValidationError):
            self.build(positions=[first, second])

    def test_zero_quantity_positions_are_normalized_away(self) -> None:
        zero_row = position(quantity="0", available_quantity="0", average_price=None)
        active = position()
        spec = self.build(positions=[zero_row, active])

        self.assertEqual(spec.non_zero_initial_positions, (active,))
        self.assertEqual(len(spec.initial_positions), 1)

    def test_positions_are_stored_in_stable_sorted_order(self) -> None:
        ids = [uuid4() for _ in range(4)]
        rows = [position(instrument_id=item) for item in ids]
        shuffled = [rows[2], rows[0], rows[3], rows[1]]

        spec_a = self.build(positions=list(rows))
        spec_b = self.build(positions=shuffled)

        expected = tuple(sorted(ids, key=str))
        self.assertEqual(
            [row.instrument_id for row in spec_a.initial_positions], list(expected)
        )
        self.assertEqual(
            [row.instrument_id for row in spec_b.initial_positions], list(expected)
        )

    def test_dynamic_universe_flag_defaults_to_false(self) -> None:
        self.assertFalse(self.build().dynamic_universe)
        self.assertTrue(self.build(dynamic_universe=True).dynamic_universe)


if __name__ == "__main__":
    unittest.main()
