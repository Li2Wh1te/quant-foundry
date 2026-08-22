"""Tests for equity curve points and the curve recorder."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

import unittest

from app.backtesting.domain import DomainValidationError, ValuationStatus
from app.backtesting.equity_curve import (
    EquityCurveError,
    EquityCurvePoint,
    EquityCurveRecorder,
    ValuationPointInput,
)

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 6, 15, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 7, 15, 0, tzinfo=timezone.utc)
T3 = datetime(2026, 1, 8, 15, 0, tzinfo=timezone.utc)


def valid_point(
    as_of: datetime,
    cash: Decimal | int | str = "100",
    market_value: Decimal | int | str = "0",
    equity: Decimal | int | str | None = None,
    status: ValuationStatus = ValuationStatus.COMPLETE,
    cumulative_fees: Decimal | int | str = "0",
    reason: str | None = None,
) -> ValuationPointInput:
    """Build one valid valuation input defaulting to an empty portfolio."""

    if equity is None:
        equity = Decimal(str(cash)) + Decimal(str(market_value))
    return ValuationPointInput(
        as_of=as_of,
        cash=cash,
        market_value=market_value,
        equity=equity,
        cumulative_fees=cumulative_fees,
        valuation_status=status,
        valuation_reason=reason,
    )


def blocked_point(
    as_of: datetime,
    cash: Decimal | int | str = "100",
    cumulative_fees: Decimal | int | str = "0",
) -> ValuationPointInput:
    """Build one blocked valuation input with a locatable reason."""

    return ValuationPointInput(
        as_of=as_of,
        cash=cash,
        cumulative_fees=cumulative_fees,
        valuation_status=ValuationStatus.BLOCKED,
        valuation_reason="missing mark price for instrument",
    )


class RecorderBaselineTestCase(unittest.TestCase):
    """initial_equity validation and the first complete point."""

    def test_initial_equity_must_be_strictly_positive(self) -> None:
        with self.assertRaises(DomainValidationError):
            EquityCurveRecorder("0")
        with self.assertRaises(DomainValidationError):
            EquityCurveRecorder("-100")
        with self.assertRaises(DomainValidationError):
            EquityCurveRecorder(Decimal("0.00"))

    def test_first_complete_point_uses_initial_equity_as_baseline(self) -> None:
        recorder = EquityCurveRecorder("100")
        point = recorder.record(valid_point(T0, cash="100", market_value="0"))

        self.assertEqual(point.sequence, 0)
        self.assertEqual(point.nav, Decimal("1"))
        self.assertEqual(point.period_return, Decimal("0"))
        self.assertEqual(point.cumulative_return, Decimal("0"))
        self.assertEqual(point.drawdown, Decimal("0"))
        self.assertEqual(point.total_pnl, Decimal("0"))
        self.assertEqual(recorder.points, (point,))


class ReturnsAndDrawdownTestCase(unittest.TestCase):
    """Period return, cumulative return, drawdown, and total pnl math."""

    def setUp(self) -> None:
        self.recorder = EquityCurveRecorder("100")
        self.recorder.record(valid_point(T0, cash="100"))

    def test_growth_to_110_yields_ten_percent_returns(self) -> None:
        point = self.recorder.record(valid_point(T1, cash="110"))
        expected = Decimal("110") / Decimal("100") - 1
        self.assertEqual(point.period_return, expected)
        self.assertEqual(point.cumulative_return, expected)
        self.assertEqual(point.total_pnl, Decimal("10"))

    def test_decline_uses_last_valid_point_as_period_baseline(self) -> None:
        self.recorder.record(valid_point(T1, cash="110"))
        point = self.recorder.record(valid_point(T2, cash="105"))

        self.assertEqual(point.period_return, Decimal("105") / Decimal("110") - 1)
        self.assertEqual(point.cumulative_return, Decimal("0.05"))
        # Peak NAV is 1.10 from the previous valid point.
        self.assertEqual(point.drawdown, Decimal("105") / Decimal("110") - 1)
        self.assertLess(point.drawdown, Decimal("0"))

    def test_drawdown_is_negative_not_a_positive_loss(self) -> None:
        self.recorder.record(valid_point(T1, cash="90"))
        point = self.recorder.points[-1]
        self.assertLess(point.drawdown, Decimal("0"))
        self.assertNotEqual(point.drawdown, abs(point.drawdown))

    def test_period_and_cumulative_returns_stay_distinct_fields(self) -> None:
        # From 100 to 110 to 105: period is negative while cumulative stays
        # positive; neither field can substitute for the other.
        self.recorder.record(valid_point(T1, cash="110"))
        point = self.recorder.record(valid_point(T2, cash="105"))
        self.assertLess(point.period_return, Decimal("0"))
        self.assertGreater(point.cumulative_return, Decimal("0"))
        self.assertIsNot(point.period_return, point.cumulative_return)


class BlockedValuationTestCase(unittest.TestCase):
    """Blocked points keep audit fields but never forge current values."""

    def setUp(self) -> None:
        self.recorder = EquityCurveRecorder("100")
        self.recorder.record(valid_point(T0, cash="100"))
        self.recorder.record(valid_point(T1, cash="110", market_value="10", equity="120"))

    def test_blocked_point_is_recorded_with_audit_fields(self) -> None:
        point = self.recorder.record(blocked_point(T2, cash="105", cumulative_fees="3.25"))

        self.assertEqual(point.sequence, 2)
        self.assertEqual(point.as_of, T2)
        self.assertIs(point.valuation_status, ValuationStatus.BLOCKED)
        self.assertEqual(point.valuation_reason, "missing mark price for instrument")

    def test_blocked_point_keeps_cash_and_cumulative_fees(self) -> None:
        point = self.recorder.record(blocked_point(T2, cash="105", cumulative_fees="3.25"))
        self.assertEqual(point.cash, Decimal("105"))
        self.assertEqual(point.cumulative_fees, Decimal("3.25"))

    def test_blocked_point_leaves_equity_derived_fields_empty(self) -> None:
        point = self.recorder.record(blocked_point(T2))
        self.assertIsNone(point.market_value)
        self.assertIsNone(point.equity)
        self.assertIsNone(point.nav)
        self.assertIsNone(point.period_return)
        self.assertIsNone(point.cumulative_return)
        self.assertIsNone(point.drawdown)
        self.assertIsNone(point.total_pnl)

    def test_blocked_input_rejects_supplied_current_values(self) -> None:
        with self.assertRaises(DomainValidationError):
            ValuationPointInput(
                as_of=T2,
                cash="100",
                market_value="10",
                valuation_status=ValuationStatus.BLOCKED,
                valuation_reason="missing mark",
            )
        with self.assertRaises(DomainValidationError):
            ValuationPointInput(
                as_of=T2,
                cash="100",
                equity="120",
                valuation_status=ValuationStatus.BLOCKED,
                valuation_reason="missing mark",
            )

    def test_blocked_point_requires_locatable_reason(self) -> None:
        with self.assertRaises(DomainValidationError):
            ValuationPointInput(
                as_of=T2,
                cash="100",
                valuation_status=ValuationStatus.BLOCKED,
            )
        with self.assertRaises(DomainValidationError):
            ValuationPointInput(
                as_of=T2,
                cash="100",
                valuation_status=ValuationStatus.BLOCKED,
                valuation_reason="   ",
            )

    def test_stale_account_equity_is_never_serialized(self) -> None:
        # The runtime account still holds the last valid equity 120 after a
        # blocked pass; the recorded point must not carry it forward.
        stale_account_equity = Decimal("120")
        point = self.recorder.record(blocked_point(T2))
        self.assertIsNone(point.equity)
        self.assertNotEqual(point.equity, stale_account_equity)
        self.assertNotEqual(point.total_pnl, stale_account_equity - Decimal("100"))

    def test_blocked_point_does_not_update_peak_or_baseline(self) -> None:
        # Peak NAV is 1.20 from equity 120.  A blocked point at a lower
        # equity must not move the peak or become the return baseline.
        self.recorder.record(blocked_point(T2, cash="50"))
        self.assertEqual(self.recorder.last_valid_equity, Decimal("120"))
        self.assertEqual(self.recorder.peak_nav, Decimal("120") / Decimal("100"))
        point = self.recorder.record(valid_point(T3, cash="115"))

        self.assertEqual(point.period_return, Decimal("115") / Decimal("120") - 1)
        self.assertEqual(point.drawdown, Decimal("115") / Decimal("120") - 1)

    def test_first_point_after_leading_blocked_uses_initial_equity(self) -> None:
        recorder = EquityCurveRecorder("100")
        recorder.record(blocked_point(T0))
        point = recorder.record(valid_point(T1, cash="108"))
        self.assertEqual(point.period_return, Decimal("108") / Decimal("100") - 1)


class OrderingAndValidationTestCase(unittest.TestCase):
    """Timeline order, fee monotonicity, and numeric input contracts."""

    def test_time_regression_is_rejected(self) -> None:
        recorder = EquityCurveRecorder("100")
        recorder.record(valid_point(T1))
        with self.assertRaises(EquityCurveError):
            recorder.record(valid_point(T0))

    def test_duplicate_time_point_is_rejected(self) -> None:
        recorder = EquityCurveRecorder("100")
        recorder.record(valid_point(T0))
        with self.assertRaises(EquityCurveError):
            recorder.record(valid_point(T0))

    def test_cumulative_fee_regression_is_rejected(self) -> None:
        recorder = EquityCurveRecorder("100")
        recorder.record(valid_point(T0, cumulative_fees="5"))
        with self.assertRaises(EquityCurveError):
            recorder.record(valid_point(T1, cumulative_fees="4.99"))

    def test_equal_cumulative_fees_are_accepted(self) -> None:
        recorder = EquityCurveRecorder("100")
        recorder.record(valid_point(T0, cumulative_fees="5"))
        point = recorder.record(valid_point(T1, cumulative_fees="5"))
        self.assertEqual(point.cumulative_fees, Decimal("5"))

    def test_float_inputs_are_rejected_everywhere(self) -> None:
        with self.assertRaises(TypeError):
            EquityCurveRecorder(100.0)
        with self.assertRaises(TypeError):
            valid_point(T0, cash=100.0)
        with self.assertRaises(TypeError):
            valid_point(T0, market_value=10.5)
        with self.assertRaises(TypeError):
            valid_point(T0, equity=110.0)
        with self.assertRaises(TypeError):
            valid_point(T0, cumulative_fees=1.5)

    def test_non_finite_decimals_are_rejected(self) -> None:
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=bad):
                with self.assertRaises(DomainValidationError):
                    EquityCurveRecorder(bad)
                with self.assertRaises(DomainValidationError):
                    valid_point(T0, cash=bad)
                with self.assertRaises(DomainValidationError):
                    valid_point(T0, equity=bad)

    def test_naive_timestamps_are_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            valid_point(datetime(2026, 1, 5, 15, 0))

    def test_invalid_valuation_status_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            ValuationPointInput(
                as_of=T0,
                cash="100",
                market_value="0",
                equity="100",
                valuation_status="partial",  # type: ignore[arg-type]
            )

    def test_valid_points_require_positive_equity(self) -> None:
        with self.assertRaises(DomainValidationError):
            valid_point(T0, cash="100", market_value="0", equity="0")
        with self.assertRaises(DomainValidationError):
            valid_point(T0, cash="100", market_value="0", equity="-5")

    def test_empty_portfolio_completes_valuation_with_cash_equity(self) -> None:
        recorder = EquityCurveRecorder("100")
        point = recorder.record(valid_point(T0, cash="100", market_value="0"))
        self.assertIs(point.valuation_status, ValuationStatus.COMPLETE)
        self.assertEqual(point.market_value, Decimal("0"))
        self.assertEqual(point.equity, Decimal("100"))
        self.assertEqual(point.nav, Decimal("1"))

    def test_sequences_are_stable_and_deterministic(self) -> None:
        inputs = [
            valid_point(T0, cash="100"),
            valid_point(T1, cash="110"),
            blocked_point(T2),
            valid_point(T3, cash="105"),
        ]
        first = EquityCurveRecorder("100")
        second = EquityCurveRecorder("100")
        first_points = [first.record(value) for value in inputs]
        second_points = [second.record(value) for value in inputs]

        self.assertEqual([point.sequence for point in first_points], [0, 1, 2, 3])
        self.assertEqual(first.points, tuple(first_points))
        self.assertEqual(first_points, second_points)


class ImmutabilityTestCase(unittest.TestCase):
    """Recorded points never change when later state moves on."""

    def test_curve_point_is_frozen(self) -> None:
        recorder = EquityCurveRecorder("100")
        point = recorder.record(valid_point(T0))
        with self.assertRaises(FrozenInstanceError):
            point.equity = Decimal("999")  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            point.nav = Decimal("99")  # type: ignore[misc]

    def test_later_recording_does_not_mutate_earlier_points(self) -> None:
        recorder = EquityCurveRecorder("100")
        first = recorder.record(valid_point(T0, cash="100"))
        snapshot = (first.nav, first.drawdown, first.period_return)
        recorder.record(valid_point(T1, cash="200"))
        self.assertEqual((first.nav, first.drawdown, first.period_return), snapshot)
        self.assertEqual(first.drawdown, Decimal("0"))


class DegradedValuationTestCase(unittest.TestCase):
    """Degraded points compute full derivations without becoming complete."""

    def test_degraded_point_computes_nav_returns_and_drawdown(self) -> None:
        recorder = EquityCurveRecorder("100")
        recorder.record(valid_point(T0, cash="100"))
        point = recorder.record(
            valid_point(
                T1,
                cash="110",
                market_value="10",
                equity="120",
                status=ValuationStatus.DEGRADED,
                reason="one mark price came from a degraded data source",
            )
        )
        self.assertIs(point.valuation_status, ValuationStatus.DEGRADED)
        self.assertIsNot(point.valuation_status, ValuationStatus.BLOCKED)
        self.assertIsNot(point.valuation_status, ValuationStatus.COMPLETE)
        self.assertEqual(point.nav, Decimal("120") / Decimal("100"))
        self.assertEqual(point.period_return, Decimal("120") / Decimal("100") - 1)
        self.assertEqual(point.cumulative_return, Decimal("120") / Decimal("100") - 1)
        self.assertEqual(point.total_pnl, Decimal("20"))
        self.assertEqual(point.drawdown, Decimal("0"))
        self.assertEqual(
            point.valuation_reason,
            "one mark price came from a degraded data source",
        )

    def test_degraded_point_updates_peak_for_following_drawdown(self) -> None:
        recorder = EquityCurveRecorder("100")
        recorder.record(
            valid_point(T0, cash="120", status=ValuationStatus.DEGRADED)
        )
        point = recorder.record(valid_point(T1, cash="110"))
        self.assertEqual(point.drawdown, Decimal("110") / Decimal("120") - 1)

    def test_degraded_point_without_reason_records_none(self) -> None:
        recorder = EquityCurveRecorder("100")
        point = recorder.record(
            valid_point(T0, status=ValuationStatus.DEGRADED, reason=None)
        )
        self.assertIsNone(point.valuation_reason)


class DirectConstructionTestCase(unittest.TestCase):
    """EquityCurvePoint invariants hold even without the recorder."""

    def test_blocked_point_cannot_carry_derived_values(self) -> None:
        with self.assertRaises(DomainValidationError):
            EquityCurvePoint(
                sequence=0,
                as_of=T0,
                cash="100",
                cumulative_fees="0",
                valuation_status=ValuationStatus.BLOCKED,
                valuation_reason="missing mark",
                market_value=None,
                equity="100",
                nav=None,
                period_return=None,
                cumulative_return=None,
                drawdown=None,
                total_pnl=None,
            )

    def test_valid_point_cannot_omit_derived_values(self) -> None:
        with self.assertRaises(DomainValidationError):
            EquityCurvePoint(
                sequence=0,
                as_of=T0,
                cash="100",
                cumulative_fees="0",
                valuation_status=ValuationStatus.COMPLETE,
                valuation_reason=None,
                market_value="0",
                equity="100",
                nav=None,
                period_return=None,
                cumulative_return=None,
                drawdown=None,
                total_pnl=None,
            )

    def test_valid_point_rejects_non_positive_equity(self) -> None:
        for bad_equity in ("0", "-5", Decimal("0.00")):
            with self.subTest(equity=bad_equity):
                with self.assertRaises(DomainValidationError):
                    EquityCurvePoint(
                        sequence=0,
                        as_of=T0,
                        cash="100",
                        cumulative_fees="0",
                        valuation_status=ValuationStatus.COMPLETE,
                        valuation_reason=None,
                        market_value="0",
                        equity=bad_equity,
                        nav="1",
                        period_return="0",
                        cumulative_return="0",
                        drawdown="0",
                        total_pnl="0",
                    )


if __name__ == "__main__":
    unittest.main()
