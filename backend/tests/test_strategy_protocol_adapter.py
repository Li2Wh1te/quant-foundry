"""Tests for the FunctionStrategyAdapter and its decision conversion."""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.strategy_protocol.adapter import FunctionStrategyAdapter, known_instrument_ids
from app.strategy_protocol.contract import (
    STRATEGY_CONTRACT_VERSION,
    InvalidDecisionPayloadError,
    StrategyProtocolError,
)
from app.strategy_protocol.decisions import StrategyDecision
from app.strategy_protocol.synthetic import (
    ContractCheckParameters,
    build_synthetic_context,
)

AWARE_TIME = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone(timedelta(hours=8)))


def _context(static_id=None):
    parameters = ContractCheckParameters(
        session_date=date(2026, 8, 21),
        decision_time=AWARE_TIME,
        data_cutoff=AWARE_TIME,
        static_instrument_ids=(static_id,) if static_id else (),
        initial_positions=(),
    )
    context, _ = build_synthetic_context(parameters)
    return context


class FunctionStrategyAdapterTestCase(unittest.TestCase):
    """Cover module loading, lifecycle defaults, and payload validation."""

    def test_target_weights_decision_is_built_from_return_value(self) -> None:
        known = next(iter(known_instrument_ids(_context())))
        source = (
            "def run(context, parameters):\n"
            "    return {\n"
            f"        'mode': 'target_weights',\n"
            f"        'targets': {{'{known}': '0.60'}},\n"
            "        'reason': '测试原因',\n"
            "    }\n"
        )
        adapter = FunctionStrategyAdapter.from_source(source, parameters={})
        context = _context()
        decision = adapter.on_step(context)
        self.assertIsInstance(decision, StrategyDecision)
        self.assertEqual(decision.mode, "target_weights")
        self.assertEqual(decision.targets[str(known)], Decimal("0.60"))
        self.assertEqual(decision.reason, "测试原因")
        self.assertEqual(decision.step_sequence, 1)
        self.assertEqual(decision.decision_time, AWARE_TIME)
        self.assertEqual(decision.contract_version, STRATEGY_CONTRACT_VERSION)

    def test_hold_decision_is_a_valid_noop(self) -> None:
        adapter = FunctionStrategyAdapter.from_source(
            "def run(context, parameters):\n    return {'mode': 'hold'}\n",
            parameters={},
        )
        decision = adapter.on_step(_context())
        self.assertEqual(decision.mode, "hold")
        self.assertEqual(dict(decision.targets), {})

    def test_float_unknown_mode_and_unknown_instrument_fail(self) -> None:
        for source in (
            "def run(context, parameters):\n    return 0.5\n",
            "def run(context, parameters):\n    return {'mode': 'mystery'}\n",
            (
                "def run(context, parameters):\n"
                "    return {'mode': 'target_weights',\n"
                "            'targets': {'" + str(uuid4()) + "': '1'}}\n"
            ),
            "def run(context, parameters):\n    return {'intents': []}\n",
        ):
            adapter = FunctionStrategyAdapter.from_source(source, parameters={})
            with self.assertRaises(StrategyProtocolError):
                adapter.on_step(_context())

    def test_hold_with_targets_is_rejected(self) -> None:
        known = next(iter(known_instrument_ids(_context())))
        source = (
            "def run(context, parameters):\n"
            "    return {'mode': 'hold',\n"
            f"            'targets': {{'{known}': '1'}}}}\n"
        )
        adapter = FunctionStrategyAdapter.from_source(source, parameters={})
        with self.assertRaises(InvalidDecisionPayloadError):
            adapter.on_step(_context())

    def test_parameters_are_passed_to_the_entry_point(self) -> None:
        source = (
            "def run(context, parameters):\n"
            "    assert parameters['window'] == 20\n"
            "    return {'mode': 'hold'}\n"
        )
        adapter = FunctionStrategyAdapter.from_source(
            source, parameters={"window": 20}
        )
        decision = adapter.on_step(_context())
        self.assertEqual(decision.mode, "hold")

    def test_module_state_persists_within_one_run(self) -> None:
        source = (
            "counter = {'calls': 0}\n"
            "def run(context, parameters):\n"
            "    counter['calls'] += 1\n"
            "    return {'mode': 'hold', 'reason': str(counter['calls'])}\n"
        )
        adapter = FunctionStrategyAdapter.from_source(source, parameters={})
        context = _context()
        first = adapter.on_step(context)
        second = adapter.on_step(context)
        # One run reuses a single loaded module; in-memory strategy state
        # survives across steps of the same run.
        self.assertEqual(first.reason, "1")
        self.assertEqual(second.reason, "2")

    def test_entry_point_shape_errors_are_locatable(self) -> None:
        for source in (
            "",
            "run = 42\n",
            "def run(context):\n    return {}\n",
            "def run(context, parameters, extra):\n    return {}\n",
            "async def run(context, parameters):\n    return {}\n",
        ):
            with self.assertRaises(
                StrategyProtocolError,
                msg=f"entry point {source!r} must be rejected",
            ):
                FunctionStrategyAdapter.from_source(source, parameters={})

    def test_lifecycle_defaults_are_noop(self) -> None:
        adapter = FunctionStrategyAdapter.from_source(
            "def run(context, parameters):\n    return {'mode': 'hold'}\n",
            parameters={},
        )
        self.assertIsNone(adapter.on_start(None))
        self.assertIsNone(adapter.on_order_update(None))
        self.assertIsNone(adapter.on_fill(None))
        self.assertIsNone(adapter.on_finish(None))


if __name__ == "__main__":
    unittest.main()
