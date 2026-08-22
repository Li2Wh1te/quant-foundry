"""Tests for the FunctionStrategyAdapter and its decision conversion."""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.strategy_protocol.adapter import (
    ISOLATED_SUBPROCESS_SCOPE,
    FunctionStrategyAdapter,
    known_instrument_ids,
)
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


def load(source: str, parameters: dict | None = None) -> FunctionStrategyAdapter:
    """Load a strategy the way only the isolated worker is allowed to."""

    return FunctionStrategyAdapter.from_source(
        source,
        parameters=parameters or {},
        execution_scope=ISOLATED_SUBPROCESS_SCOPE,
    )


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


class ExecutionScopeTestCase(unittest.TestCase):
    """Cover the guard that keeps user-source execution subprocess-only."""

    def test_from_source_requires_the_isolated_subprocess_scope(self) -> None:
        for wrong_scope in (None, "", "api_process", ISOLATED_SUBPROCESS_SCOPE + "x"):
            with self.assertRaises(StrategyProtocolError):
                FunctionStrategyAdapter.from_source(
                    "def run(context, parameters):\n    return {'mode': 'hold'}\n",
                    parameters={},
                    execution_scope=wrong_scope,
                )


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
        adapter = load(source)
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
        adapter = load("def run(context, parameters):\n    return {'mode': 'hold'}\n")
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
            adapter = load(source)
            with self.assertRaises(StrategyProtocolError):
                adapter.on_step(_context())

    def test_hold_with_targets_is_rejected(self) -> None:
        known = next(iter(known_instrument_ids(_context())))
        source = (
            "def run(context, parameters):\n"
            "    return {'mode': 'hold',\n"
            f"            'targets': {{'{known}': '1'}}}}\n"
        )
        adapter = load(source)
        with self.assertRaises(InvalidDecisionPayloadError):
            adapter.on_step(_context())

    def test_parameters_are_deeply_read_only_across_steps(self) -> None:
        source = (
            "def run(context, parameters):\n"
            "    parameters['window'] = 99\n"
            "    return {'mode': 'hold'}\n"
        )
        adapter = load(source, parameters={"window": 20})
        with self.assertRaises((TypeError, AttributeError)):
            adapter.on_step(_context())

        nested_source = (
            "def run(context, parameters):\n"
            "    parameters['bounds']['low'] = -1\n"
            "    return {'mode': 'hold'}\n"
        )
        nested_adapter = load(
            nested_source, parameters={"bounds": {"low": 0}}
        )
        with self.assertRaises((TypeError, AttributeError)):
            nested_adapter.on_step(_context())

        list_source = (
            "def run(context, parameters):\n"
            "    parameters['items'].append('x')\n"
            "    return {'mode': 'hold'}\n"
        )
        list_adapter = load(list_source, parameters={"items": [1]})
        with self.assertRaises((TypeError, AttributeError)):
            list_adapter.on_step(_context())

    def test_parameter_mutation_attempts_cannot_leak_into_later_steps(self) -> None:
        # Even a strategy that catches the write error cannot corrupt the
        # parameter object seen by later steps.
        source = (
            "state = {'seen': []}\n"
            "def run(context, parameters):\n"
            "    try:\n"
            "        parameters['window'] = 99\n"
            "    except (TypeError, AttributeError):\n"
            "        pass\n"
            "    state['seen'].append(parameters['window'])\n"
            "    return {'mode': 'hold', 'reason': str(parameters['window'])}\n"
        )
        adapter = load(source, parameters={"window": 20})
        first = adapter.on_step(_context())
        second = adapter.on_step(_context())
        self.assertEqual(first.reason, "20")
        self.assertEqual(second.reason, "20")

    def test_module_state_persists_within_one_run(self) -> None:
        source = (
            "counter = {'calls': 0}\n"
            "def run(context, parameters):\n"
            "    counter['calls'] += 1\n"
            "    return {'mode': 'hold', 'reason': str(counter['calls'])}\n"
        )
        adapter = load(source)
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
                load(source)

    def test_lifecycle_defaults_are_noop(self) -> None:
        adapter = load("def run(context, parameters):\n    return {'mode': 'hold'}\n")
        self.assertIsNone(adapter.on_start(None))
        self.assertIsNone(adapter.on_order_update(None))
        self.assertIsNone(adapter.on_fill(None))
        self.assertIsNone(adapter.on_finish(None))


if __name__ == "__main__":
    unittest.main()
