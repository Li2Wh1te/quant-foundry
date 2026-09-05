import unittest
from types import SimpleNamespace

from app.backtesting.data.preflight_service import _consistency_summary
from app.backtesting.data.requests import ConsistencyMode, ContractRef
from app.backtesting.data import preflight_service


class ConsistencySummaryTests(unittest.TestCase):
    def test_summary_contains_frozen_runtime_fields(self):
        report = SimpleNamespace(
            provider_key="memory",
            data_contract_version=1,
            consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
            consistency_token_contract=None,
            max_lookback_sessions=512,
            data_chunk_policy=ContractRef("fixed_trading_sessions", 1),
            data_chunk_size_sessions=20,
            resolved_sessions=tuple(range(41)),
            warmup_sessions=tuple(range(3)),
            session_summary={"data_watermark": "2020-01-01T00:00:00Z"},
            source_revisions=None,
        )
        summary = _consistency_summary(report)
        self.assertEqual(summary["consistency_mode"], "transitional_repeatable_read")
        self.assertEqual(summary["max_lookback_sessions"], 512)
        self.assertEqual(summary["chunk_token_summary"]["chunk_count"], 3)
        self.assertEqual(summary["data_watermark"], "2020-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
