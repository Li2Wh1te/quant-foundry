"""Tests for the minimal ETF adjustment-factor retrieval service."""

import unittest
from unittest.mock import Mock

from app.data_ingestion.services.etf_adjustment import fetch_etf_adjustment_factors


class FetchEtfAdjustmentFactorsTestCase(unittest.TestCase):
    """Verify the service preserves Tushare's documented request contract."""

    def test_forwards_the_documented_tushare_parameters(self) -> None:
        """The service should forward the official fund_adj demo parameters unchanged."""
        client = Mock()

        result = fetch_etf_adjustment_factors(
            client,
            ts_code="513100.SH",
            start_date="20190101",
            end_date="20190926",
        )

        self.assertIs(result, client.pro.fund_adj.return_value)
        client.pro.fund_adj.assert_called_once_with(
            ts_code="513100.SH",
            start_date="20190101",
            end_date="20190926",
        )
