"""Tests for the versioned slippage models and their registry identities."""

import unittest
from decimal import Decimal

from app.backtesting.reason_codes import SlippageReasonCode
from app.backtesting.registry import (
    SLIPPAGE_MODEL_KEY_BPS,
    SLIPPAGE_MODEL_KEY_NONE,
    build_default_component_registry,
)
from app.backtesting.slippage import (
    BpsSlippageModel,
    SLIPPAGE_MODEL_VERSION,
    SlippageError,
)


class BpsSlippageCalculationTests(unittest.TestCase):
    def test_reference_10_15bps_tick_01_gives_buy_10_02_sell_9_98(self) -> None:
        model = BpsSlippageModel(slippage_bps="15", price_tick="0.01")

        buy = model.apply("10.00", "buy")
        sell = model.apply("10.00", "sell")

        self.assertEqual(buy.execution_price, Decimal("10.02"))
        self.assertEqual(sell.execution_price, Decimal("9.98"))
        # The audit trail reports the tick actually applied.
        self.assertEqual(buy.price_tick, Decimal("0.01"))
        self.assertEqual(
            buy.parameters["slippage_bps"], Decimal("15")
        )

    def test_none_model_records_zero_slippage_but_still_rounds_adversely(self) -> None:
        model = BpsSlippageModel.none(price_tick="0.01")

        buy = model.apply("10.003", "buy")
        sell = model.apply("10.003", "sell")

        self.assertEqual(model.model_key, "none")
        self.assertEqual(model.slippage_bps, Decimal("0"))
        self.assertEqual(buy.slippage_bps, Decimal("0"))
        self.assertEqual(buy.price_delta, Decimal("0.007"))
        # Tick rounding still applies with zero bps: adverse direction.
        self.assertEqual(buy.execution_price, Decimal("10.01"))
        self.assertEqual(sell.execution_price, Decimal("10.00"))

    def test_instrument_tick_overrides_the_fixture_default(self) -> None:
        model = BpsSlippageModel(slippage_bps="0", price_tick="0.01")

        result = model.apply("10.00", "buy", price_tick="0.05")

        self.assertEqual(result.execution_price, Decimal("10.00"))
        self.assertEqual(result.parameters["price_tick"], Decimal("0.05"))

    def test_identical_inputs_reproduce_identical_results(self) -> None:
        model = BpsSlippageModel(slippage_bps="15")
        first = model.apply("10.00", "sell")
        second = model.apply("10.00", "sell")
        self.assertEqual(first, second)

    def test_failures_carry_structured_reason_codes(self) -> None:
        model = BpsSlippageModel(slippage_bps="15")

        with self.assertRaises(SlippageError) as bad_reference:
            model.apply("0", "buy")
        self.assertEqual(
            bad_reference.exception.reason_code,
            SlippageReasonCode.INVALID_REFERENCE_PRICE,
        )

        with self.assertRaises(SlippageError) as bad_tick:
            model.apply("10.00", "buy", price_tick="0")
        self.assertEqual(
            bad_tick.exception.reason_code,
            SlippageReasonCode.INVALID_PRICE_TICK,
        )

        tiny_tick_model = BpsSlippageModel(slippage_bps="0", price_tick="0.01")
        with self.assertRaises(SlippageError) as non_positive:
            tiny_tick_model.apply("-1", "sell")
        self.assertEqual(
            non_positive.exception.reason_code,
            SlippageReasonCode.INVALID_REFERENCE_PRICE,
        )

    def test_negative_configuration_is_rejected_as_invalid_configuration(
        self,
    ) -> None:
        with self.assertRaises(SlippageError) as ctx:
            BpsSlippageModel(slippage_bps="-1")
        self.assertEqual(
            ctx.exception.reason_code,
            SlippageReasonCode.INVALID_SLIPPAGE_CONFIGURATION,
        )

    def test_construction_failures_carry_structured_reason_codes(self) -> None:
        with self.assertRaises(SlippageError) as bad_bps:
            BpsSlippageModel(slippage_bps="abc")
        self.assertEqual(
            bad_bps.exception.reason_code,
            SlippageReasonCode.INVALID_SLIPPAGE_CONFIGURATION,
        )

        with self.assertRaises(SlippageError) as bad_tick:
            BpsSlippageModel(slippage_bps="5", price_tick="zero")
        self.assertEqual(
            bad_tick.exception.reason_code,
            SlippageReasonCode.INVALID_PRICE_TICK,
        )
        self.assertEqual(bad_tick.exception.details["price_tick"], "zero")


class SlippageRegistryTests(unittest.TestCase):
    def test_bps_and_none_are_registered_at_version_one(self) -> None:
        registry = build_default_component_registry()

        bps_entry = registry.resolve(SLIPPAGE_MODEL_KEY_BPS, 1)
        none_entry = registry.resolve(SLIPPAGE_MODEL_KEY_NONE, 1)

        self.assertEqual(bps_entry.name_zh, "基点滑点")
        self.assertIn("基点滑点（", bps_entry.display_name)
        self.assertEqual(none_entry.name_en, "No Slippage")
        self.assertEqual(SLIPPAGE_MODEL_VERSION, 1)

    def test_registered_factories_construct_working_models(self) -> None:
        registry = build_default_component_registry()

        bps_model = registry.resolve(SLIPPAGE_MODEL_KEY_BPS, 1).construct(
            {"slippage_bps": "15"}
        )
        none_model = registry.resolve(SLIPPAGE_MODEL_KEY_NONE, 1).construct({})

        self.assertIsInstance(bps_model, BpsSlippageModel)
        self.assertIsInstance(none_model, BpsSlippageModel)
        self.assertEqual(bps_model.model_key, "bps")
        self.assertEqual(none_model.model_key, "none")

    def test_bps_requires_explicit_nonzero_slippage_and_none_rejects_nonzero(
        self,
    ) -> None:
        from app.backtesting.registry import RegistryError

        registry = build_default_component_registry()
        with self.assertRaises(RegistryError):
            registry.resolve(SLIPPAGE_MODEL_KEY_BPS, 1).construct({})
        with self.assertRaises(RegistryError):
            registry.resolve(SLIPPAGE_MODEL_KEY_BPS, 1).construct(
                {"slippage_bps": "0"}
            )
        with self.assertRaises(RegistryError):
            registry.resolve(SLIPPAGE_MODEL_KEY_NONE, 1).construct(
                {"slippage_bps": "5"}
            )

    def test_malformed_decimal_parameters_fail_with_registry_error(self) -> None:
        from decimal import InvalidOperation

        from app.backtesting.registry import RegistryError

        registry = build_default_component_registry()
        for key in (SLIPPAGE_MODEL_KEY_BPS, SLIPPAGE_MODEL_KEY_NONE):
            with self.subTest(key=key):
                with self.assertRaises(RegistryError) as ctx:
                    registry.resolve(key, 1).construct(
                        {"slippage_bps": "abc"}
                    )
                self.assertNotIsInstance(
                    ctx.exception, InvalidOperation
                )
                self.assertIn("not a valid decimal", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
