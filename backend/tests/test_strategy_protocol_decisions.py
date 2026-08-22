"""Tests for strategy protocol decisions, modes, and the read-only context."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from uuid import uuid4

from app.strategy_protocol.contract import (
    STRATEGY_CONTRACT_VERSION,
    InvalidDecisionPayloadError,
    MissingDecisionModeError,
    UnknownDecisionModeError,
    UnknownInstrumentError,
)
from app.strategy_protocol.context import (
    DecisionContext,
    DeterministicClockDTO,
    PortfolioDTO,
    PositionDTO,
    PreviousStepDTO,
)
from app.strategy_protocol.decisions import (
    HOLD_MODE,
    TARGET_WEIGHTS_MODE,
    StrategyDecision,
    build_default_registry,
)

AWARE_TIME = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone(timedelta(hours=8)))
SESSION_DAY = date(2026, 8, 21)


def _make_position(instrument_id=None):
    return PositionDTO(
        instrument_id=instrument_id or uuid4(),
        trading_code="SYN.A",
        name="合成标的 A",
        display_name="Synthetic A",
        side="long",
        quantity=Decimal("100"),
        available_quantity=Decimal("100"),
        average_price=Decimal("10"),
        mark_price=Decimal("11"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("100"),
    )


def _make_context(positions=(), candidates=()):
    """Build a minimal context with stub query facades."""

    class _StubUniverse:
        def __init__(self, rows):
            self._rows = tuple(rows)

        def query(self, *, exchanges=None, asset_classes=None):
            return self._rows

    class _StubData:
        pass

    clock = DeterministicClockDTO(decision_time=AWARE_TIME, session_date=SESSION_DAY)
    portfolio = PortfolioDTO(
        cash_balances={"CNY": Decimal("100000")},
        available_cash=Decimal("100000"),
        frozen_cash=Decimal("0"),
        margin_used=Decimal("0"),
        margin_available=Decimal("100000"),
        equity=Decimal("100000"),
        positions=tuple(positions),
    )
    return DecisionContext(
        step_sequence=1,
        session_date=SESSION_DAY,
        decision_time=AWARE_TIME,
        data_cutoff=AWARE_TIME,
        timezone="Asia/Shanghai",
        clock=clock,
        portfolio=portfolio,
        previous_step=PreviousStepDTO(step_sequence=0),
        data=_StubData(),
        universe=_StubUniverse(candidates),
    )


class DecisionObjectTestCase(unittest.TestCase):
    """Cover immutability and numeric conversion rules."""

    def test_decision_is_immutable_value_object(self) -> None:
        decision = StrategyDecision(
            step_sequence=1,
            decision_time=AWARE_TIME,
            mode=HOLD_MODE,
        )
        with self.assertRaises(FrozenInstanceError):
            decision.mode = TARGET_WEIGHTS_MODE

    def test_decimal_strings_and_decimals_are_converted(self) -> None:
        decision = StrategyDecision(
            step_sequence=1,
            decision_time=AWARE_TIME,
            mode=TARGET_WEIGHTS_MODE,
            targets={"a": "0.60", "b": Decimal("0.40"), "c": 1},
        )
        self.assertEqual(decision.targets["a"], Decimal("0.60"))
        self.assertEqual(decision.targets["b"], Decimal("0.40"))
        self.assertEqual(decision.targets["c"], Decimal("1"))
        # The exposed mapping cannot be mutated by callers.
        with self.assertRaises(TypeError):
            decision.targets["a"] = Decimal("1")

    def test_float_bool_and_invalid_values_are_rejected(self) -> None:
        for value in (0.6, True, False, "abc", "NaN", "Infinity", None, ["0.5"]):
            with self.assertRaises(Exception, msg=f"{value!r} must be rejected"):
                StrategyDecision(
                    step_sequence=1,
                    decision_time=AWARE_TIME,
                    mode=TARGET_WEIGHTS_MODE,
                    targets={"x": value},
                )

    def test_contract_version_is_pinned(self) -> None:
        decision = StrategyDecision(
            step_sequence=1, decision_time=AWARE_TIME, mode=HOLD_MODE
        )
        self.assertEqual(decision.contract_version, STRATEGY_CONTRACT_VERSION)
        with self.assertRaises(InvalidDecisionPayloadError):
            StrategyDecision(
                step_sequence=1,
                decision_time=AWARE_TIME,
                mode=HOLD_MODE,
                contract_version=2,
            )

    def test_naive_decision_time_is_rejected(self) -> None:
        with self.assertRaises(InvalidDecisionPayloadError):
            StrategyDecision(
                step_sequence=1,
                decision_time=datetime(2026, 8, 21, 15, 0, 0),
                mode=HOLD_MODE,
            )


class DecisionModeRegistryTestCase(unittest.TestCase):
    """Cover the first-version registry semantics."""

    def setUp(self) -> None:
        self.known = {uuid4()}
        self.registry = build_default_registry()

    def test_only_two_modes_are_registered_but_registry_is_extensible(self) -> None:
        self.assertEqual(
            self.registry.modes(), (HOLD_MODE, TARGET_WEIGHTS_MODE)
        )
        # The public object is not hard-coded to this subset.
        self.registry.register("target_positions", lambda targets, ids: {})
        self.assertIn("target_positions", self.registry.modes())

    def test_target_weights_accepts_partial_known_targets(self) -> None:
        mode, targets = self.registry.validate(
            {
                "mode": "target_weights",
                "targets": {str(next(iter(self.known))): "0.60"},
            },
            known_instrument_ids=self.known,
        )
        self.assertEqual(mode, TARGET_WEIGHTS_MODE)
        self.assertEqual(targets[str(next(iter(self.known)))], Decimal("0.60"))

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(UnknownDecisionModeError):
            self.registry.validate(
                {"mode": "order_intents"}, known_instrument_ids=self.known
            )

    def test_missing_mode_is_rejected(self) -> None:
        with self.assertRaises(MissingDecisionModeError):
            self.registry.validate({}, known_instrument_ids=self.known)

    def test_non_object_payloads_are_rejected(self) -> None:
        for payload in (0.5, "hold", [], None, 3):
            with self.assertRaises(InvalidDecisionPayloadError):
                self.registry.validate(payload, known_instrument_ids=self.known)

    def test_unknown_instrument_id_is_rejected(self) -> None:
        with self.assertRaises(UnknownInstrumentError):
            self.registry.validate(
                {
                    "mode": "target_weights",
                    "targets": {str(uuid4()): "1"},
                },
                known_instrument_ids=self.known,
            )
        with self.assertRaises(UnknownInstrumentError):
            self.registry.validate(
                {
                    "mode": "target_weights",
                    "targets": {"instrument-a": "1"},
                },
                known_instrument_ids=self.known,
            )

    def test_hold_must_not_carry_targets(self) -> None:
        mode, targets = self.registry.validate(
            {"mode": "hold"}, known_instrument_ids=self.known
        )
        self.assertEqual((mode, targets), (HOLD_MODE, {}))
        mode, targets = self.registry.validate(
            {"mode": "hold", "targets": {}}, known_instrument_ids=self.known
        )
        self.assertEqual(targets, {})
        with self.assertRaises(InvalidDecisionPayloadError):
            self.registry.validate(
                {
                    "mode": "hold",
                    "targets": {str(next(iter(self.known))): "1"},
                },
                known_instrument_ids=self.known,
            )

    def test_legacy_intents_protocol_has_no_compatibility_branch(self) -> None:
        with self.assertRaises(MissingDecisionModeError):
            self.registry.validate({"intents": []}, known_instrument_ids=self.known)
        with self.assertRaises(UnknownDecisionModeError):
            self.registry.validate(
                {"mode": "intents", "intents": []},
                known_instrument_ids=self.known,
            )


class ContextDtoTestCase(unittest.TestCase):
    """Cover deterministic clock and read-only nested DTOs."""

    def test_clock_is_stable_within_one_step(self) -> None:
        clock = DeterministicClockDTO(decision_time=AWARE_TIME, session_date=SESSION_DAY)
        for _ in range(3):
            self.assertEqual(clock.now(), AWARE_TIME)
            self.assertEqual(clock.today(), SESSION_DAY)

    def test_real_startup_time_does_not_change_clock_results(self) -> None:
        first = DeterministicClockDTO(
            decision_time=AWARE_TIME, session_date=SESSION_DAY
        )
        second = DeterministicClockDTO(
            decision_time=AWARE_TIME, session_date=SESSION_DAY
        )
        self.assertEqual(first.now(), second.now())
        self.assertEqual(first.today(), second.today())

    def test_portfolio_and_positions_are_immutable(self) -> None:
        position = _make_position()
        portfolio = PortfolioDTO(
            cash_balances={"CNY": Decimal("1")},
            available_cash=Decimal("1"),
            frozen_cash=Decimal("0"),
            margin_used=Decimal("0"),
            margin_available=Decimal("1"),
            equity=Decimal("1"),
            positions=(position,),
        )
        with self.assertRaises(TypeError):
            portfolio.cash_balances["USD"] = Decimal("1")
        with self.assertRaises(FrozenInstanceError):
            position.quantity = Decimal("2")
        self.assertIsInstance(portfolio.positions, tuple)

    def test_portfolio_rejects_duplicate_instrument_ids(self) -> None:
        same_id = uuid4()
        with self.assertRaises(InvalidDecisionPayloadError):
            PortfolioDTO(
                cash_balances={},
                available_cash=Decimal("0"),
                frozen_cash=Decimal("0"),
                margin_used=Decimal("0"),
                margin_available=Decimal("0"),
                equity=Decimal("0"),
                positions=(_make_position(same_id), _make_position(same_id)),
            )

    def test_portfolio_rejects_float_and_invalid_amounts(self) -> None:
        base = dict(
            cash_balances={"CNY": Decimal("1")},
            available_cash=Decimal("1"),
            frozen_cash=Decimal("0"),
            margin_used=Decimal("0"),
            margin_available=Decimal("1"),
            equity=Decimal("1"),
        )
        # The engine's Decimal normalization rejects binary floats with a
        # TypeError and invalid decimal values with a DomainValidationError.
        numeric_error = (TypeError, ValueError)
        for field in ("available_cash", "frozen_cash", "margin_used",
                      "margin_available", "equity"):
            with self.assertRaises(numeric_error, msg=field):
                PortfolioDTO(**{**base, field: 1.5})
        with self.assertRaises(numeric_error):
            PortfolioDTO(**{**base, "cash_balances": {"CNY": 2.5}})
        # Negative non-cash amounts are rejected as well.
        with self.assertRaises(ValueError):
            PortfolioDTO(**{**base, "frozen_cash": Decimal("-1")})

    def test_position_rejects_float_and_inconsistent_quantities(self) -> None:
        def position(**overrides):
            values = dict(
                instrument_id=uuid4(),
                trading_code="SYN.A",
                name="合成标的 A",
                display_name="Synthetic A",
                side="long",
                quantity=Decimal("100"),
                available_quantity=Decimal("100"),
                average_price=Decimal("10"),
                mark_price=Decimal("11"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("100"),
            )
            values.update(overrides)
            return PositionDTO(**values)

        numeric_error = (TypeError, ValueError)
        for field in ("quantity", "average_price", "mark_price", "realized_pnl"):
            with self.assertRaises(numeric_error, msg=field):
                position(**{field: 1.5})
        with self.assertRaises(ValueError):  # available above owned quantity
            position(available_quantity=Decimal("200"))
        with self.assertRaises(ValueError):  # zero-quantity positions rejected
            position(quantity=Decimal("0"), average_price=None)

    def test_context_binds_clock_to_step_and_is_immutable(self) -> None:
        context = _make_context()
        self.assertEqual(context.clock.now(), context.decision_time)
        self.assertEqual(context.clock.today(), context.session_date)
        with self.assertRaises(FrozenInstanceError):
            context.step_sequence = 99

    def test_context_rejects_mismatched_or_naive_clock_fields(self) -> None:
        clock = DeterministicClockDTO(
            decision_time=AWARE_TIME, session_date=date(1999, 1, 1)
        )
        with self.assertRaises(InvalidDecisionPayloadError):
            _make_context().__class__(
                step_sequence=1,
                session_date=SESSION_DAY,
                decision_time=AWARE_TIME,
                data_cutoff=AWARE_TIME,
                timezone="Asia/Shanghai",
                clock=clock,
                portfolio=_make_context().portfolio,
                previous_step=PreviousStepDTO(step_sequence=0),
                data=_make_context().data,
                universe=_make_context().universe,
            )


if __name__ == "__main__":
    unittest.main()
