"""Real installed-wheel and shared Rust/JSON/Python contract regression."""
import ast
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from random import Random
from types import MappingProxyType
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from quantfoundry import ContractError, RunConfig, BacktestClient, capabilities, decimal_input, nanoseconds
from quantfoundry import _native
from quantfoundry.api import LimitOrder, StrategyError, get_price, get_trades, order
from quantfoundry.contracts import QuoteTick, RunConfigFields

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "contracts/examples"

def base():
    return json.loads((EXAMPLES / "run_config.json").read_text())

class ContractTests(unittest.TestCase):
    def test_shared_valid_invalid_configs_and_schema(self):
        schema = json.loads((ROOT / "contracts/run_request.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for case in json.loads((EXAMPLES / "run_config_cases.json").read_text()):
            with self.subTest(case=case["id"]):
                config = base()
                config.update(deepcopy(case["patch"]))
                if "build" in case:
                    config["parameters"] = {"a":"x" * case["build"]["parameter_string_bytes"]}
                for key in case["drop"]:
                    config.pop(key)
                self.assertEqual(validator.is_valid(config), case["schema_valid"])
                raw = json.dumps(config, ensure_ascii=False, allow_nan=False)
                if case["valid"]:
                    normalized = json.loads(_native.validate_run_config_json(raw))
                    self.assertEqual(RunConfig(**config).to_dict(), normalized)
                    self.assertTrue(validator.is_valid(normalized))
                else:
                    with self.assertRaises(ContractError) as error:
                        _native.validate_run_config_json(raw)
                    self.assertEqual(error.exception.code, "INVALID_RUN_CONFIG")
                    # Client defaults intentionally fill omitted schema/parameters;
                    # required constructor arguments raise TypeError if absent.
                    if case["id"] in {"calendar_null", "fees_null", "parameters_null"}:
                        # SDK None explicitly means omission/default by the
                        # approved constructor contract; JSON null is rejected.
                        normalized = RunConfig(**config).to_dict()
                        self.assertTrue(validator.is_valid(normalized))
                        self.assertNotIn("reference_calendar", normalized)
                        if case["id"] == "fees_null":
                            self.assertNotIn("cost_overrides", normalized)
                    elif not case["drop"]:
                        with self.assertRaises((ContractError, TypeError)):
                            RunConfig(**config)

    def test_normalized_config_is_a_copy_and_strict_json_only(self):
        config = RunConfig(**base())
        snapshot = config.to_dict()
        snapshot["parameters"]["window"] = 900
        self.assertEqual(config.to_dict()["parameters"]["window"], 20)
        self.assertEqual(config.to_dict()["initial_cash"], "10000")
        for value in [float("nan"), float("inf"), Decimal("0.1"), object()]:
            data = base(); data["parameters"] = {"x":value}
            with self.assertRaises(ContractError) as error:
                RunConfig(**data)
            self.assertEqual(error.exception.code, "INVALID_RUN_CONFIG")
        data = base(); data["parameters"] = {"n":18446744073709551617}
        self.assertEqual(RunConfig(**data).to_dict()["parameters"]["n"], 18446744073709551617)
        with self.assertRaises(ContractError):
            _native.validate_run_config_json(json.dumps(data).replace("18446744073709551617", "1e400"))
        data["parameters"] = {"n":10 ** 999 + 1}
        self.assertEqual(RunConfig(**data).to_dict()["parameters"], data["parameters"])

    def test_decimal_range_exact_conversion_and_half_even(self):
        for value in [Decimal("0.1"), 1, "0.01", Decimal("1E-28")]:
            self.assertEqual(decimal_input(value), Decimal(value))
        for value in [True, False, 0.1, float("nan"), float("inf")]:
            with self.assertRaises(TypeError): decimal_input(value)
        for value in ["NaN", "Infinity", Decimal("NaN"), Decimal("Infinity"),
                      "0.00000000000000000000000000001", "79228162514264337593543950336"]:
            with self.assertRaises(ContractError) as error: decimal_input(value)
            self.assertEqual(error.exception.code, "NUMERIC_RANGE_UNSUPPORTED")
        self.assertEqual(_native.round_decimal("0.005", 2), "0.00")
        self.assertEqual(_native.round_decimal("0.015", 2), "0.02")
        self.assertEqual(_native.divide_decimal("1", "3", 12), "0.333333333333")
        self.assertEqual(_native.divide_decimal("2", "3", 12), "0.666666666667")
        self.assertEqual(_native.legal_quantity(3, "0.6666666666666666666666666666", "1", 1), 1)
        self.assertEqual(_native.legal_quantity(301, "1", "3", 100), 100)
        for quantity in [-1, 9223372036854775808, True, 0.1]:
            with self.assertRaises((ContractError, TypeError, OverflowError)):
                _native.legal_quantity(quantity, "1", "1", 1)
        self.assertEqual(LimitOrder(Decimal("0.01")).price, Decimal("0.01"))
        for price in ["0", "-1"]:
            with self.assertRaises(ContractError): LimitOrder(price)

    def test_parameter_mapping_preserves_object_keys_and_numbers(self):
        parameters = {
            "$serde_json::private::Number": "123",
            "nested": [
                {"$serde_json::private::Number": "18446744073709551617"},
                {"$serde_json::private::RawValue": "[1,2]"},
                {"$serde_json::private::Number": "not a number", "other": True}
            ],
            "large_integer": 18446744073709551617,
        }
        data = base(); data["parameters"] = parameters
        normalized = json.loads(_native.validate_run_config_json(json.dumps(data)))
        self.assertEqual(normalized["parameters"], parameters)
        self.assertEqual(RunConfig(**data).to_dict(), normalized)
        data["parameters"] = MappingProxyType(parameters)
        data["cost_overrides"] = MappingProxyType({"commission_rate": "0.001"})
        data["universe"] = tuple(data["universe"])
        self.assertEqual(RunConfig(**data).to_dict()["parameters"], parameters)
        for invalid in [{1: "silently converted key"}, {"nested": {False: "invalid key"}}]:
            data["parameters"] = invalid
            with self.assertRaises(ContractError) as error:
                RunConfig(**data)
            self.assertEqual(error.exception.code, "INVALID_RUN_CONFIG")

    def test_decimal_conversion_bounds_exponent_padding_losslessly(self):
        # These tiny Decimal objects must never expand into a billion bytes.
        for value in [Decimal("1e1000000000"), Decimal("1e-1000000000"), 10 ** 5000]:
            with self.assertRaises(ContractError) as error:
                decimal_input(value)
            self.assertEqual(error.exception.code, "NUMERIC_RANGE_UNSUPPORTED")
        self.assertEqual(decimal_input(Decimal("0e-1000000000")), Decimal(0))
        self.assertEqual(decimal_input(Decimal("-0e1000000000")), Decimal(0))
        self.assertEqual(decimal_input(Decimal("1." + "0" * 1000)), Decimal(1))
        self.assertEqual(decimal_input(Decimal("0.01" + "0" * 1000)), Decimal("0.01"))
        self.assertEqual(decimal_input(Decimal("-1." + "0" * 1000)), Decimal(-1))

    def test_numeric_results_match_independent_decimal_and_rational_oracles(self):
        rng = Random(301)
        with localcontext() as context:
            context.prec = 80
            for index in range(200):
                with self.subTest(index=index):
                    value = Decimal(rng.randrange(-10 ** 14, 10 ** 14)).scaleb(-rng.randrange(13))
                    denominator = Decimal(rng.randrange(2, 20))
                    scale = rng.randrange(13)
                    quantum = Decimal(1).scaleb(-scale)
                    expected = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
                    self.assertEqual(Decimal(_native.round_decimal(format(value, "f"), scale)), expected)
                    expected = (value / denominator).quantize(quantum, rounding=ROUND_HALF_EVEN)
                    self.assertEqual(Decimal(_native.divide_decimal(format(value, "f"), str(denominator), scale)), expected)
                    quantity = rng.randrange(10001)
                    numerator = Decimal(rng.randrange(100001)).scaleb(-rng.randrange(7))
                    step = rng.randrange(1, 1001)
                    ratio = quantity * Fraction(numerator) / Fraction(denominator) / step
                    expected_quantity = (ratio.numerator // ratio.denominator) * step
                    self.assertEqual(_native.legal_quantity(quantity, format(numerator, "f"), str(denominator), step), expected_quantity)

    def test_exact_nanoseconds_across_python_rust_and_json(self):
        for value in [-(1<<63), 1767225600000000123, (1<<63)-1]:
            self.assertEqual(nanoseconds(value), value)
            json_roundtrip = json.loads(json.dumps({"time_ns":str(value)}))["time_ns"]
            self.assertEqual(_native.roundtrip_ns(json_roundtrip), str(value))
        for value in [True, 0.1, (1<<63), "01", "+1", "NaN"]:
            with self.assertRaises((TypeError, ContractError)):
                nanoseconds(value)
        event = json.loads((EXAMPLES / "quote_tick.json").read_text())
        normalized = json.loads(_native.validate_market_event_json(json.dumps(event)))
        self.assertEqual(normalized, event)
        self.assertEqual(int(normalized["event"]["time_ns"]), 1767225600000000123)
        self.assertEqual(normalized["event"]["bid_quantity"], 100)
        self.assertEqual(normalized["event"]["ask_quantity"], 200)
        self.assertEqual(QuoteTick(**event["event"]), event["event"])
        self.assertEqual(RunConfigFields(**base()), base())
        event["event"]["time_ns"] = 1767225600000000123
        with self.assertRaises(ContractError): _native.validate_market_event_json(json.dumps(event))

    def test_honest_capabilities_and_uniform_refusals(self):
        caps = capabilities()
        self.assertEqual(caps["implemented"], ["checked_numeric", "shared_contracts", "research_views", "technical_indicators"])
        self.assertEqual(caps["execution_models"], [])
        self.assertEqual(caps["frequencies"], [])
        self.assertFalse(caps["production_data_available"])
        client = BacktestClient(base_url="https://synthetic.invalid", token="fixture")
        for action in [lambda:client.submit(RunConfig(**base()), idempotency_key="fixture"),
                       lambda:get_price("510300.SH", count=20), lambda:get_trades(limit=10),
                       lambda:order("510300.SH", 100)]:
            with self.assertRaises(StrategyError) as error: action()
            self.assertEqual(error.exception.code, "CAPABILITY_UNAVAILABLE")

    def test_public_signatures_generated_and_schema_matches_rust_fields(self):
        subprocess.run([sys.executable, str(ROOT / "scripts/generate_s3_contracts.py"), "--check"], check=True)
        tree = ast.parse((ROOT / "contracts/client.pyi").read_text())
        constructor = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RunConfig")
        method = next(node for node in constructor.body if isinstance(node, ast.FunctionDef))
        expected = [item.arg for item in method.args.kwonlyargs]
        actual = [key for key, p in inspect.signature(RunConfig).parameters.items() if p.kind == p.KEYWORD_ONLY]
        self.assertEqual(actual, expected)
        wire = ast.parse((ROOT / "contracts/shared_types.pyi").read_text())
        run = next(node for node in wire.body if isinstance(node, ast.ClassDef) and node.name == "RunConfigFields")
        fields = {node.target.id for node in run.body if isinstance(node, ast.AnnAssign)}
        schema = json.loads((ROOT / "contracts/run_request.schema.json").read_text())
        self.assertEqual(fields, set(schema["properties"]))
        for filename in ["api.pyi", "client.pyi", "contracts.pyi", "_native.pyi"]:
            ast.parse((ROOT / "sdk/python/quantfoundry" / filename).read_text())

    def test_wheel_import_in_an_independent_process_without_repo_pythonpath(self):
        with tempfile.TemporaryDirectory() as work:
            result = subprocess.run([sys.executable, "-I", "-c",
                "import sys,json,quantfoundry; from quantfoundry import _native; "
                "assert sys.version_info[:2]==(3,12); "
                "assert _native.roundtrip_ns('1767225600000000123')=='1767225600000000123'; "
                "print(json.dumps({'version':quantfoundry.__version__,'module':_native.__file__,'capabilities':quantfoundry.capabilities()}))"],
                cwd=work, check=True, capture_output=True, text=True)
        print("INDEPENDENT_WHEEL_IMPORT", result.stdout.strip())

if __name__ == "__main__":
    unittest.main()
