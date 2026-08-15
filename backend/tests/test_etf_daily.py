"""Tests for ETF daily-bar retrieval and atomic incremental synchronization."""

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from app.data_ingestion.schemas.etf_daily import (
    EtfDailyBarInput,
    EtfDailyBarUpsertResult,
)
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.data_ingestion.services.etf_daily import (
    ETF_DAILY_FIELDS,
    ETF_DAILY_MARKET_FIELDS,
    ETF_DAILY_SCOPE_KEY,
    ETF_DAILY_SYNC_KEY,
    MAX_ETF_DAILY_ROWS,
    _commit_etf_daily_date,
    fetch_etf_daily,
    fetch_etf_daily_for_trade_date,
    normalize_etf_daily,
    sync_etf_daily,
)


def make_bar(trade_date: date = date(2026, 8, 14)) -> EtfDailyBarInput:
    return EtfDailyBarInput(
        ts_code="510330.SH",
        trade_date=trade_date,
        open=Decimal("3.710"),
        high=Decimal("3.750"),
        low=Decimal("3.700"),
        close=Decimal("3.740"),
        vol=Decimal("12345.0000"),
        amount=Decimal("46000.5000"),
    )


def make_dataframe(*, trade_date: str = "20260814") -> Mock:
    dataframe = Mock()
    dataframe.to_dict.return_value = [
        {
            "ts_code": "510330.SH",
            "trade_date": trade_date,
            "open": 3.71,
            "high": 3.75,
            "low": 3.70,
            "close": 3.74,
            "vol": 12345,
            "amount": 46000.5,
        }
    ]
    return dataframe


class FetchEtfDailyTestCase(unittest.TestCase):
    def test_delegates_targeted_repair_query_to_tushare(self) -> None:
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

    def test_fetches_one_complete_market_session_by_trade_date(self) -> None:
        client = Mock()

        result = fetch_etf_daily_for_trade_date(client, trade_date="20260814")

        self.assertIs(result, client.pro.fund_daily.return_value)
        client.pro.fund_daily.assert_called_once_with(
            trade_date="20260814", fields=ETF_DAILY_MARKET_FIELDS
        )

    def test_normalizes_a_complete_market_response(self) -> None:
        bars = normalize_etf_daily(
            make_dataframe(), expected_trade_date=date(2026, 8, 14)
        )

        self.assertEqual(bars, [make_bar()])

    def test_rejects_a_response_at_the_provider_row_limit(self) -> None:
        dataframe = Mock()
        dataframe.to_dict.return_value = [{}] * MAX_ETF_DAILY_ROWS

        with self.assertRaisesRegex(ValueError, "may be truncated"):
            normalize_etf_daily(dataframe, expected_trade_date=date(2026, 8, 14))

    @patch("app.data_ingestion.services.etf_daily.logger")
    @patch("app.data_ingestion.services.etf_daily._commit_etf_daily_date")
    @patch("app.data_ingestion.services.etf_daily.fetch_etf_daily_for_trade_date")
    @patch("app.data_ingestion.services.etf_daily._load_open_dates")
    @patch("app.data_ingestion.services.etf_daily._load_checkpoint")
    @patch("app.data_ingestion.services.etf_daily.get_settings")
    @patch("app.data_ingestion.services.etf_daily.tushare_request_pacer")
    def test_commits_each_completed_market_session_before_advancing_cursor(
        self,
        pacer_mock,
        get_settings_mock,
        load_checkpoint_mock,
        load_open_dates_mock,
        fetch_mock,
        commit_mock,
        logger_mock,
    ) -> None:
        first_date, second_date = date(2026, 8, 13), date(2026, 8, 14)
        client = Mock()
        get_settings_mock.return_value = SimpleNamespace(
            ingestion_request_interval_ms=1_000
        )
        load_checkpoint_mock.return_value = None
        load_open_dates_mock.return_value = [first_date, second_date]
        fetch_mock.side_effect = [make_dataframe(trade_date="20260813"), make_dataframe()]
        first_checkpoint = DataSyncCheckpointState(
            sync_key=ETF_DAILY_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={"synced_through_date": "2026-08-13"},
            cursor_version=1,
            version=1,
        )
        second_checkpoint = DataSyncCheckpointState(
            sync_key=ETF_DAILY_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={"synced_through_date": "2026-08-14"},
            cursor_version=1,
            version=2,
        )
        commit_mock.side_effect = [
            (EtfDailyBarUpsertResult(received=1, changed=1, unchanged=0), first_checkpoint),
            (EtfDailyBarUpsertResult(received=1, changed=0, unchanged=1), second_checkpoint),
        ]

        result = sync_etf_daily(
            client,
            calendar_exchange="SSE",
            initial_start_date=first_date,
            as_of_date=second_date,
            request_interval_ms=1_500,
        )

        self.assertEqual(result.days_completed, 2)
        self.assertEqual(result.received, 2)
        self.assertEqual(result.changed, 1)
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.synced_through_date, second_date)
        pacer_mock.wait_for_turn.assert_has_calls([call(1_500), call(1_500)])
        fetch_mock.assert_has_calls(
            [call(client, trade_date="20260813"), call(client, trade_date="20260814")]
        )
        self.assertEqual(commit_mock.call_args_list[0].kwargs["expected_checkpoint"], None)
        self.assertIs(
            commit_mock.call_args_list[1].kwargs["expected_checkpoint"],
            first_checkpoint,
        )
        self.assertEqual(
            [item.args[0] for item in logger_mock.info.call_args_list],
            [
                "etf_daily_sync_planned",
                "etf_daily_sync_started",
                "etf_daily_sync_succeeded",
                "etf_daily_sync_started",
                "etf_daily_sync_succeeded",
            ],
        )

    @patch("app.data_ingestion.services.etf_daily.DataSyncCheckpointRepository")
    @patch("app.data_ingestion.services.etf_daily.EtfDailyBarRepository")
    @patch("app.data_ingestion.services.etf_daily.get_engine")
    @patch("app.data_ingestion.services.etf_daily.Session")
    def test_commits_the_day_and_checkpoint_in_one_transaction(
        self,
        session_class,
        get_engine_mock,
        bar_repository_class,
        checkpoint_repository_class,
    ) -> None:
        session = session_class.return_value.__enter__.return_value
        write_result = EtfDailyBarUpsertResult(received=1, changed=1, unchanged=0)
        bar_repository_class.return_value.upsert_bars.return_value = write_result
        checkpoint = DataSyncCheckpointState(
            sync_key=ETF_DAILY_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={"synced_through_date": "2026-08-14"},
            cursor_version=1,
            version=1,
        )
        checkpoint_repository_class.return_value.advance.return_value = checkpoint

        result, actual_checkpoint = _commit_etf_daily_date(
            bars=[make_bar()],
            expected_checkpoint=None,
            synced_through_date=date(2026, 8, 14),
        )

        self.assertIs(result, write_result)
        self.assertIs(actual_checkpoint, checkpoint)
        bar_repository_class.return_value.upsert_bars.assert_called_once_with(
            [make_bar()], source="tushare"
        )
        checkpoint_repository_class.return_value.advance.assert_called_once_with(
            sync_key=ETF_DAILY_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={"synced_through_date": "2026-08-14"},
            expected_version=None,
        )
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
