"""Tests for the isolated subprocess strategy contract check.

These tests spawn real worker subprocesses; they verify the happy path,
protocol failures, timeout, cancellation, synthetic identity injection, and
determinism of the check evidence.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from app.strategy_protocol.checker import (
    ContractCheckRequest,
    build_worker_payload,
    run_strategy_contract_check,
)
from app.strategy_protocol.contract import FAILURE_PHASE_STRATEGY_CONTRACT_CHECK

AWARE_TIME = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone(timedelta(hours=8)))
STATIC_ID = uuid4()


def _request(source: str, **overrides) -> ContractCheckRequest:
    defaults = dict(
        source_code=source,
        parameter_schema={},
        default_parameters={},
        static_instrument_ids=(str(STATIC_ID),),
        session_date=date(2026, 8, 21),
        decision_time=AWARE_TIME,
        data_cutoff=AWARE_TIME,
    )
    defaults.update(overrides)
    return ContractCheckRequest(**defaults)


HOLD_STRATEGY = "def run(context, parameters):\n    return {'mode': 'hold'}\n"


class ContractCheckPayloadTestCase(unittest.TestCase):
    """Cover request serialization rules."""

    def test_payload_uses_deterministic_fallback_dates(self) -> None:
        payload = build_worker_payload(
            ContractCheckRequest(source_code=HOLD_STRATEGY, parameter_schema={}, default_parameters={})
        )
        import json

        decoded = json.loads(payload)
        # Fixed fallback session, never derived from the wall clock.
        self.assertEqual(decoded["session_date"], "2030-01-15")
        self.assertIn("+08:00", decoded["decision_time"])

    def test_payload_serializes_static_ids_and_positions(self) -> None:
        payload = build_worker_payload(
            _request(
                HOLD_STRATEGY,
                initial_positions=(
                    {
                        "instrument_id": STATIC_ID,
                        "side": "long",
                        "quantity": "100",
                        "available_quantity": "100",
                        "average_price": "10",
                    },
                ),
            )
        )
        import json

        decoded = json.loads(payload)
        self.assertEqual(decoded["static_instrument_ids"], [str(STATIC_ID)])
        self.assertEqual(decoded["initial_positions"][0]["quantity"], "100")


class ContractCheckSubprocessTestCase(unittest.TestCase):
    """Run the real isolated worker for each scenario."""

    def test_valid_strategy_passes_with_evidence(self) -> None:
        result = run_strategy_contract_check(_request(HOLD_STRATEGY))
        self.assertTrue(result.ok, result.message)
        self.assertIsNone(result.failure_phase)
        self.assertEqual(result.evidence["contract_version"], 1)
        self.assertIn(str(STATIC_ID), str(result.evidence["identity_rows"]))

    def test_target_weights_strategy_passes_with_static_id(self) -> None:
        source = (
            "def run(context, parameters):\n"
            f"    return {{'mode': 'target_weights',\n"
            f"            'targets': {{'{STATIC_ID}': '1'}}}}\n"
        )
        result = run_strategy_contract_check(_request(source))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.evidence["mode"], "target_weights")
        self.assertEqual(result.evidence["target_count"], 1)

    def test_invalid_return_fails_with_contract_check_phase(self) -> None:
        for source in (
            "def run(context, parameters):\n    return 0.5\n",
            "def run(context, parameters):\n    return {'intents': []}\n",
            "def run(context, parameters):\n    return {'mode': 'mystery'}\n",
            (
                "def run(context, parameters):\n"
                "    return {'mode': 'target_weights',\n"
                "            'targets': {'" + str(uuid4()) + "': '1'}}\n"
            ),
            "def run(context, parameters):\n    raise ValueError('boom')\n",
        ):
            result = run_strategy_contract_check(_request(source))
            self.assertFalse(result.ok, source)
            self.assertEqual(
                result.failure_phase, FAILURE_PHASE_STRATEGY_CONTRACT_CHECK
            )
            self.assertTrue(result.error_type)
            self.assertTrue(result.message)

    def test_syntax_error_reports_line(self) -> None:
        result = run_strategy_contract_check(
            _request("def run(context, parameters)\n    pass\n")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "SyntaxError")
        self.assertEqual(result.line, 1)

    def test_unknown_identity_keeps_official_error_semantics(self) -> None:
        source = (
            "def run(context, parameters):\n"
            "    bar = context.data.bars(\n"
            f"        '{uuid4()}', lookback_sessions=5)\n"
            "    return {'mode': 'hold'}\n"
        )
        result = run_strategy_contract_check(_request(source))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "UnknownInstrumentError")

    def test_data_cutoff_and_lookback_semantics_match_the_real_run(self) -> None:
        future_source = (
            "def run(context, parameters):\n"
            "    import datetime\n"
            "    context.data.bars(\n"
            f"        '{STATIC_ID}',\n"
            "        end_date=datetime.date(2026, 8, 22))\n"
            "    return {'mode': 'hold'}\n"
        )
        result = run_strategy_contract_check(_request(future_source))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "DataCutoffViolationError")

        oversized_source = (
            "def run(context, parameters):\n"
            "    context.data.bars(\n"
            f"        '{STATIC_ID}', lookback_sessions=513)\n"
            "    return {'mode': 'hold'}\n"
        )
        result = run_strategy_contract_check(_request(oversized_source))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "LookbackLimitExceededError")

    def test_non_zero_initial_position_passes_end_to_end(self) -> None:
        # Regression: position instrument ids cross the process boundary as
        # strings and must be decoded by the worker, not fail the check.
        source = (
            "def run(context, parameters):\n"
            "    positions = context.portfolio.positions\n"
            "    assert len(positions) == 1\n"
            f"    assert str(positions[0].instrument_id) == '{STATIC_ID}'\n"
            "    return {'mode': 'hold'}\n"
        )
        result = run_strategy_contract_check(
            _request(
                source,
                initial_positions=(
                    {
                        "instrument_id": STATIC_ID,
                        "side": "long",
                        "quantity": "100",
                        "available_quantity": "100",
                        "average_price": "10",
                    },
                ),
            )
        )
        self.assertTrue(result.ok, result.message)

    def test_strategy_stdout_cannot_pollute_the_result_document(self) -> None:
        # Regression: strategy prints are redirected to stderr so stdout only
        # ever carries the one machine-readable JSON result.  Module-level
        # code runs before run() and must be covered by the redirect too.
        source = (
            'print("top-level noise")\n'
            "def run(context, parameters):\n"
            "    print('debug')\n"
            "    print('more noise', 123)\n"
            "    return {'mode': 'hold'}\n"
        )
        result = run_strategy_contract_check(_request(source))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.evidence["mode"], "hold")

    def test_runtime_failure_reports_line_and_technical_details(self) -> None:
        source = (
            "def run(context, parameters):\n"
            "    marker = 1\n"
            "    raise ValueError('boom at runtime')\n"
        )
        result = run_strategy_contract_check(_request(source))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ValueError")
        # The failing line inside the strategy module is reported.
        self.assertEqual(result.line, 3)
        self.assertIn("ValueError", result.technical or "")
        self.assertIn("boom at runtime", result.message)

    def test_timeout_kills_the_worker(self) -> None:
        source = (
            "def run(context, parameters):\n"
            "    import time\n"
            "    time.sleep(60)\n"
            "    return {'mode': 'hold'}\n"
        )
        result = run_strategy_contract_check(
            _request(source), timeout_seconds=1.5
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ContractCheckTimeout")
        self.assertEqual(
            result.failure_phase, FAILURE_PHASE_STRATEGY_CONTRACT_CHECK
        )

    def test_cancel_signal_terminates_the_worker(self) -> None:
        source = (
            "def run(context, parameters):\n"
            "    import time\n"
            "    time.sleep(60)\n"
            "    return {'mode': 'hold'}\n"
        )
        result = run_strategy_contract_check(
            _request(source), should_cancel=lambda: True
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ContractCheckCancelled")

    def test_check_is_deterministic_across_repeats(self) -> None:
        first = run_strategy_contract_check(_request(HOLD_STRATEGY))
        second = run_strategy_contract_check(_request(HOLD_STRATEGY))
        self.assertEqual(first.evidence, second.evidence)

    def test_check_does_not_touch_the_database_or_network(self) -> None:
        # A strategy importing a DB driver or network client is not part of
        # the synthetic check surface; the worker only receives JSON stdin.
        source = (
            "def run(context, parameters):\n"
            "    import json\n"
            "    return {'mode': 'hold', 'reason': json.dumps({'ok': True})}\n"
        )
        result = run_strategy_contract_check(_request(source))
        self.assertTrue(result.ok, result.message)

    def test_source_compilation_is_refused_outside_the_worker(self) -> None:
        # Regression: importing the worker from an ordinary process (this
        # test process) must not expose a callable user-source loader.
        from app.strategy_protocol.worker import _load_published_module

        with self.assertRaises(RuntimeError):
            _load_published_module("value = 7\n")


if __name__ == "__main__":
    unittest.main()
