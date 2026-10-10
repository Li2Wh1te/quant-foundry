"""Installed Rust wheel + selected saved account JSON, without old fee imports."""
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
import unittest

from quantfoundry import ContractError, _native
from quantfoundry.contracts import CommissionConfig, EffectiveRange, FeeConfig, FeeScope

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "d02_account_config", ROOT / "backend/app/backtest_service/account_config.py"
)
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)


def saved_schedule():
    # Shape comes from the existing saved FeeSchedule/fee_rule JSON contract.
    # These commission numbers are user-configured synthetic examples.
    return {
        "key": "account-commission-example",
        "version": 7,
        "metadata": {"currency": "CNY"},
        "fee_rules": [{
            "key": "commission", "category": "commission", "side": "both",
            "rate": "0.0003", "minimum": "5", "fixed_amount": "0",
            "rounding_level": "fee_item", "rounding_scope": "commission",
            "rounding_mode": "half_up", "rounding_precision": "0.01",
            "applicability": {},
        }],
    }


class RulesWheelTests(unittest.TestCase):
    def test_saved_commission_is_validated_by_rust_and_captured_as_a_small_copy(self):
        saved = saved_schedule()
        before = deepcopy(saved)
        adapted = ADAPTER.commission_from_account_fee_schedule(
            saved, side="buy", included_components=[]
        )
        self.assertEqual(adapted, CommissionConfig(
            commission_rate="0.0003", minimum_commission="5", currency="CNY",
            settlement_scale=2, rounding="half_up", included_components=[],
            basis="saved-account:account-commission-example:buy",
        ))
        self.assertEqual(saved, before)
        saved["fee_rules"][0]["rate"] = "0.1"
        self.assertEqual(adapted["commission_rate"], "0.0003")
        self.assertFalse(any(name.startswith("app.backtesting") for name in sys.modules))

    def test_saved_side_and_identity_conditions_select_exactly_one_commission(self):
        saved = saved_schedule()
        saved["metadata"] = {}
        etf = saved["fee_rules"][0]
        etf["applicability"] = {"asset_class": "etf"}
        stock = deepcopy(etf)
        stock.update(key="stock-commission", rate="0.0005", applicability={"asset_class": "stock"})
        sell = deepcopy(stock)
        stock["side"] = "buy"
        sell.update(key="stock-sale", side="sell", rate="0.0006")
        saved["fee_rules"] += [stock, sell]
        for side, asset, expected in [("buy", "etf", "0.0003"), ("buy", "stock", "0.0005"), ("sell", "stock", "0.0006")]:
            result = ADAPTER.commission_from_account_fee_schedule(
                saved, side=side, currency="CNY", applicability_context={"asset_class": asset}, included_components=[]
            )
            self.assertEqual(result["commission_rate"], expected)
        with self.assertRaises(ContractError) as caught:
            ADAPTER.commission_from_account_fee_schedule(saved, side="buy", currency="CNY", included_components=[])
        self.assertEqual(caught.exception.scope["field"], "applicability")

    def test_legacy_rounding_is_mapped_explicitly_without_old_fee_calculation(self):
        for legacy, native in [("half_up", "half_up"), ("down", "toward_zero"), ("up", "away_from_zero")]:
            saved = saved_schedule()
            saved["fee_rules"][0]["rounding_mode"] = legacy
            saved["fee_rules"][0]["rounding_precision"] = Decimal("0.01")
            saved["fee_rules"][0]["rate"] = Decimal("0.0003")
            self.assertEqual(ADAPTER.commission_from_account_fee_schedule(saved, side="buy", included_components=[])["rounding"], native)

    def test_missing_inclusion_minimum_rounding_and_duplicate_items_refuse(self):
        for case in ["missing_inclusion", "missing_minimum", "duplicate_commission", "duplicate_inclusion", "separate_and_included", "currency", "unsupported_rounding", "condition_unknown", "bool_scale", "unknown_component"]:
            with self.subTest(case=case):
                saved = saved_schedule()
                kwargs = {"side": "buy", "included_components": []}
                if case == "missing_inclusion": kwargs.pop("included_components")
                elif case == "missing_minimum": saved["fee_rules"][0].pop("minimum")
                elif case == "duplicate_commission":
                    another = deepcopy(saved["fee_rules"][0]); another["key"] = "another"; saved["fee_rules"].append(another)
                elif case == "duplicate_inclusion": kwargs["included_components"] = ["handling_fee", "handling_fee"]
                elif case == "separate_and_included":
                    kwargs["included_components"] = ["handling_fee"]
                    saved["fee_rules"].append({"key": "separate-handling", "category": "handling_fee"})
                elif case == "currency": saved["metadata"] = {}
                elif case == "unsupported_rounding": saved["fee_rules"][0]["rounding_mode"] = "bank-default"
                elif case == "condition_unknown": saved["fee_rules"][0]["applicability"] = {"asset_class": "etf"}
                elif case == "bool_scale": kwargs["settlement_scale"] = True
                else: saved["fee_rules"].append({"key": "other", "category": "unknown_fee"})
                with self.assertRaises(ContractError) as caught:
                    ADAPTER.commission_from_account_fee_schedule(saved, **kwargs)
                self.assertEqual(caught.exception.code, "RULE_UNAVAILABLE")
                self.assertTrue(caught.exception.scope)

    def test_overrides_change_only_commission_and_use_d01_exact_numeric_validation(self):
        saved = saved_schedule()
        adapted = ADAPTER.commission_from_account_fee_schedule(
            saved, side="sell", included_components=[],
            cost_overrides={"commission_rate": "0.0002", "minimum_commission": "1"},
        )
        self.assertEqual((adapted["commission_rate"], adapted["minimum_commission"]), ("0.0002", "1"))
        self.assertEqual(saved["fee_rules"][0]["rate"], "0.0003")
        for overrides in [{"stamp_duty": "0"}, {"commission_rate": 0.0002}, {"commission_rate": None}, {"minimum_commission": "0.005"}]:
            with self.assertRaises(ContractError):
                ADAPTER.commission_from_account_fee_schedule(saved, side="buy", included_components=[], cost_overrides=overrides)
        for value in [True, 0.0003, float("nan")]:
            saved["fee_rules"][0]["rate"] = value
            with self.assertRaises(TypeError):
                ADAPTER.commission_from_account_fee_schedule(saved, side="buy", included_components=[])

    def test_real_fee_composition_keeps_shared_feeconfig_shape_and_scope_errors(self):
        adapted = ADAPTER.commission_from_account_fee_schedule(saved_schedule(), side="sell", included_components=[])
        # This is the catalog inspection date, not an assertion that this
        # Saturday is a trading session or that production market data exists.
        request = {
            "scope": {"instrument": {"security": "fee-fixture", "exchange": "shanghai", "product": "main_board_stock"},
                      "side": "sell", "investor": "resident_individual", "origin": {"kind": "official", "reference": "verified-fee-catalog"}},
            "effective": {"from": "2026-10-10", "through": "2026-10-10"}, "commission": adapted,
        }
        composed = json.loads(_native.compose_official_fee_config_json(json.dumps(request)))
        self.assertEqual(EffectiveRange(**request["effective"]), request["effective"])
        self.assertEqual(FeeScope(**request["scope"]), request["scope"])
        self.assertEqual(composed["scope"], request["scope"])
        fees = composed["fee_config"]
        self.assertEqual(FeeConfig(**fees), fees)
        self.assertIsNone(fees["synthetic_model"])
        self.assertEqual({c["kind"] for c in fees["components"]}, {"stamp_duty", "transfer_fee", "handling_fee", "regulatory_fee"})
        self.assertTrue(all(isinstance(c["fact"].get("rate", "0"), str) for c in fees["components"]))
        request["scope"]["instrument"]["product"] = "equity_etf"
        with self.assertRaises(ContractError) as caught:
            _native.compose_official_fee_config_json(json.dumps(request))
        self.assertEqual(caught.exception.code, "RULE_UNAVAILABLE")
        self.assertEqual(caught.exception.scope["security"], "fee-fixture")
        self.assertEqual(caught.exception.scope["date"], "2026-10-10")
        self.assertEqual(caught.exception.operation, "fee_catalog")
        request["scope"]["origin"] = {"kind": "synthetic", "reference": "fee-oracle"}
        with self.assertRaises(ContractError):
            _native.compose_official_fee_config_json(json.dumps(request))

    def test_native_fee_json_is_bounded_and_unknown_shapes_are_rejected(self):
        with self.assertRaises(ContractError) as caught:
            _native.validate_commission_config_json(" " * (1024 * 1024 + 1))
        self.assertEqual(caught.exception.code, "RESOURCE_LIMIT")
        for payload in ["null", "[]", '{"commission_rate":true}', '{"credentials":"secret-example"}']:
            with self.assertRaises(ContractError) as caught:
                _native.validate_commission_config_json(payload)
            self.assertNotIn("secret-example", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
