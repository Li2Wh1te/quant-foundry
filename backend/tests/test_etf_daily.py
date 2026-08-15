import unittest
from unittest.mock import Mock

from app.data_ingestion.services.etf_daily import ETF_DAILY_FIELDS, fetch_etf_daily


class FetchEtfDailyTestCase(unittest.TestCase):
    def test_delegates_to_the_official_fund_daily_example_call(self) -> None:
        client = Mock()

        result = fetch_etf_daily(
            client,
            ts_code="510330.SH",
            start_date="20250101",
            end_date="20250618",
        )

        self.assertIs(result, client.pro.fund_daily.return_value)
        client.pro.fund_daily.assert_called_once_with(
            ts_code="510330.SH",
            start_date="20250101",
            end_date="20250618",
            fields=ETF_DAILY_FIELDS,
        )
