"""Shared S2/S3 arithmetic, independent Decimal oracle; no source admission."""
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
import json
import unittest
from quantfoundry import _native


class AdjustmentMathTests(unittest.TestCase):
    def test_contract_example_and_independent_decimal_rounding(self):
        values = [('100', '1', '2'), ('60', '2', '2'), ('100', '1', '1'), ('60', '2', '1'),
                  ('1', '1', '3'), ('1.0000000000005', '1', '1'), ('1.0000000000015', '1', '1'),
                  ('1234.123456', '12.345678901234', '9.876543210987')]
        with localcontext() as context:
            context.prec = 80
            expected = [(Decimal(price) * Decimal(factor) / Decimal(anchor)).quantize(
                Decimal('.000000000001'), rounding=ROUND_HALF_EVEN) for price, factor, anchor in values]
        actual = json.loads(_native.adjust_prices_json(json.dumps(values)))
        self.assertEqual([Decimal(value) for value in actual], expected)

    def test_range_zero_anchor_and_batch_limits_preserve_native_refusal(self):
        for values, expected in [([['1', '1', '0']], 'NUMERIC_RANGE_UNSUPPORTED'),
                                 ([['79228162514264337593543950335', '2', '1']], 'NUMERIC_RANGE_UNSUPPORTED'),
                                 ([['1', '1', '1']] * 8001, 'RESOURCE_LIMIT')]:
            with self.assertRaises(_native.ContractError) as error:
                _native.adjust_prices_json(json.dumps(values))
            self.assertEqual(error.exception.code, expected)
