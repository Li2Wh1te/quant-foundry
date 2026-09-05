import unittest

from app.backtesting.strategy_process import StrategyProcessInput, check_and_create_adapter


class StrategyProcessTests(unittest.TestCase):
    def test_contract_gate_runs_before_formal_process_port_returns(self):
        result = check_and_create_adapter(
            StrategyProcessInput(
                source_code="def run(context, parameters):\n    return {'mode': 'hold'}\n",
                parameter_schema={"type": "object"},
                parameters={},
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.evidence["mode"], "hold")


if __name__ == "__main__":
    unittest.main()
