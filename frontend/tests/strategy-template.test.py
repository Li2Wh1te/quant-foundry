"""Exercise the shipped template against the real immutable strategy protocol."""
import unittest
from pathlib import Path
from datetime import date, datetime, timezone
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from uuid import UUID
from app.strategy_protocol.adapter import FunctionStrategyAdapter
from app.strategy_protocol.synthetic import ContractCheckParameters, build_synthetic_context
from app.strategies.validation import validate_strategy_draft

SOURCE = (Path(__file__).parents[1] / 'src/pages/strategies/rotation.py').read_text()

class RotationTemplateTest(unittest.TestCase):
    def setUp(self):
        self.module = ModuleType('reviewed_rotation_template')
        exec(compile(SOURCE, 'rotation.py', 'exec'), self.module.__dict__)

    def test_real_protocol_accepts_published_template(self):
        now = datetime(2026, 9, 12, 15, tzinfo=timezone.utc)
        context, _ = build_synthetic_context(ContractCheckParameters(
            session_date=date(2026, 9, 12), decision_time=now, data_cutoff=now,
            static_instrument_ids=(), initial_positions=()))
        decision = FunctionStrategyAdapter(self.module, parameters={'lookback': 2, 'holding_size': 3}).on_step(context)
        self.assertEqual(decision.mode, 'target_weights')
        self.assertGreater(len(decision.targets), 0)
        self.assertLessEqual(sum(decision.targets.values()), Decimal(1))
        self.assertTrue(validate_strategy_draft(SOURCE, parameter_schema={}, default_parameters={}).valid)

    def context(self, windows):
        candidates=[SimpleNamespace(instrument_id=UUID(int=i+1)) for i in range(len(windows))]
        def bars(instrument_id, *, lookback_sessions):
            return [SimpleNamespace(values={'close':v}) for v in windows[instrument_id.int-1]]
        return SimpleNamespace(universe=SimpleNamespace(query=lambda **kw: candidates), data=SimpleNamespace(bars=bars))

    def test_empty_and_bad_windows_hold(self):
        for windows in [[], [[]], [[Decimal('NaN'),Decimal(2)]], [[Decimal(0),Decimal(2)]], [[None,Decimal(2)]], [[Decimal(1)]]]:
            with self.subTest(windows=windows):
                self.assertEqual(self.module.run(self.context(windows), {'lookback':2}), {'mode':'hold'})

    def test_rank_ties_and_decimal_weights_are_deterministic(self):
        windows=[[Decimal(1),Decimal(2)]]*3
        result=self.module.run(self.context(windows), {'lookback':2,'holding_size':3})
        self.assertEqual(list(result['targets']), [str(UUID(int=i)) for i in range(1,4)])
        self.assertTrue(all(isinstance(v,str) for v in result['targets'].values()))
        self.assertLessEqual(sum(map(Decimal,result['targets'].values())), Decimal(1))

    def test_data_failures_are_not_suppressed(self):
        context=self.context([[Decimal(1),Decimal(2)]])
        def fail(*a,**kw): raise RuntimeError('missing PIT evidence')
        context.data.bars=fail
        with self.assertRaisesRegex(RuntimeError,'missing PIT'):
            self.module.run(context,{'lookback':2})

    def test_invalid_parameters_are_rejected(self):
        for params in [{'lookback':True},{'lookback':1},{'holding_size':0},{'holding_size':1.5}]:
            with self.assertRaises(ValueError): self.module.run(self.context([]),params)

if __name__=='__main__': unittest.main()
