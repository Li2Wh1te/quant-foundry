"""Tests for source-scoped ETF detail read queries."""

import unittest
from datetime import date
from unittest.mock import Mock

from app.data_ingestion.repositories.etf_detail import EtfDetailQueryRepository


class EtfDetailQueryRepositoryTestCase(unittest.TestCase):
    """Keep SQL predicates stable for the ETF detail endpoints."""

    def test_get_code_is_scoped_to_the_configured_source(self) -> None:
        session = Mock()

        EtfDetailQueryRepository(session).get_code("510300.SH")

        query = str(session.scalar.call_args.args[0])
        self.assertIn("etf_codes.source", query)
        self.assertIn("etf_codes.ts_code", query)

    def test_daily_bars_filter_the_selected_code_and_date_range(self) -> None:
        session = Mock()
        session.scalars.return_value.all.return_value = []

        result = EtfDetailQueryRepository(session).list_daily_bars(
            ts_code="510300.SH",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 14),
            limit=1_000,
        )

        self.assertEqual(result, [])
        query = str(session.scalars.call_args.args[0])
        self.assertIn("etf_daily_bars.source", query)
        self.assertIn("etf_daily_bars.ts_code", query)
        self.assertIn("etf_daily_bars.trade_date >=", query)
        self.assertIn("etf_daily_bars.trade_date <=", query)
        self.assertIn("ORDER BY etf_daily_bars.trade_date ASC", query)

    def test_adjustment_factors_filter_the_selected_code_and_date_range(self) -> None:
        session = Mock()
        session.scalars.return_value.all.return_value = []

        result = EtfDetailQueryRepository(session).list_adjustment_factors(
            ts_code="510300.SH",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 14),
            limit=1_000,
        )

        self.assertEqual(result, [])
        query = str(session.scalars.call_args.args[0])
        self.assertIn("etf_adjustment_factors.source", query)
        self.assertIn("etf_adjustment_factors.ts_code", query)
        self.assertIn("etf_adjustment_factors.trade_date >=", query)
        self.assertIn("etf_adjustment_factors.trade_date <=", query)
        self.assertIn("ORDER BY etf_adjustment_factors.trade_date ASC", query)


if __name__ == "__main__":
    unittest.main()
