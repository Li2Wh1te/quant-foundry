import unittest
from decimal import Decimal
import numpy as np
import pandas as pd
from quantfoundry import ContractError
from quantfoundry.indicators import sma, ema, rsi, atr, macd, rolling_std, rank


class IndicatorTests(unittest.TestCase):
    def test_decimal_original_and_float_output_stay_distinct(self):
        original = pd.Series([Decimal('1.000000000000000000001'), Decimal('2'), Decimal('3')], index=['a', 'b', 'c'])
        result = sma(original, 2)
        self.assertEqual(result.index.tolist(), ['a', 'b', 'c'])
        self.assertEqual(result['value'].dtype, np.dtype('float64'))
        self.assertEqual(original.iloc[0], Decimal('1.000000000000000000001'))
        self.assertEqual(result.iloc[0]['status'], 'warmup')
        self.assertEqual(result.iloc[-1]['value'], 2.5)
        self.assertEqual(result.attrs['qf']['precision'], 'float64')

    def test_wilder_and_macd_values_use_rust_seed_policy(self):
        result = rsi([10, 12, 11, 13, 12], 2)
        self.assertAlmostEqual(result.iloc[2]['value'], 200/3)
        result = atr([11, 13, 12, 14], [9, 11, 10, 12], [10, 12, 11, 13], 2)
        self.assertEqual(result['value'].tolist()[1:], [2.5, 2.25, 2.625])
        result = macd(np.arange(1, 6), 2, 3, 2)
        self.assertEqual(result.iloc[2]['signal_status'], 'warmup')
        self.assertEqual(result.iloc[3]['signal'], .5)
        self.assertEqual(rolling_std([1, 2, 3], 3).iloc[-1]['value'], 1)

    def test_missing_and_warmup_are_distinct_and_no_fill(self):
        result = ema([1, 2, None, 4, 5, 6], 3)
        self.assertEqual(result['status'].tolist(), ['warmup', 'warmup', 'missing', 'missing', 'missing', 'available'])
        self.assertEqual(result.iloc[-1]['value'], 5)
        self.assertTrue(sma([1, np.nan, 3], 2)['value'].isna().all())
        self.assertEqual(rank({'B': 1, 'A': 1, 'C': None})['value'].tolist()[:2], [1.5, 1.5])
        self.assertEqual(rank({})['value'].dtype, np.dtype('float64'))

    def test_input_resource_and_finite_checks(self):
        for value in (float('inf'), Decimal('NaN'), True, '1'):
            with self.assertRaises(ContractError):
                sma([value], 1)
        for period in (0, True, 10001):
            with self.assertRaises(ContractError):
                ema([1], period)
        with self.assertRaises(ContractError):
            rolling_std([1], 1)
        with self.assertRaises(ContractError):
            macd([1, 2], 3, 2)


if __name__ == '__main__':
    unittest.main()
