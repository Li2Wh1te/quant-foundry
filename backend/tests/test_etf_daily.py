"""Tests for ETF daily-bar retrieval and atomic incremental synchronization."""

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from app.data_ingestion.schemas.etf_daily import (
    EtfDailyBarInput,
    EtfDailyBarUpsertResult,
)
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.backtesting.data.errors import InstrumentCalendarUnresolvedError
from app.data_ingestion.services.etf_daily import (
    ETF_DAILY_FIELDS,
    ETF_DAILY_FULL_SYNC_KEY,
    ETF_DAILY_INCREMENTAL_SYNC_KEY,
    ETF_DAILY_MARKET_FIELDS,
    ETF_DAILY_SCOPE_KEY,
    MAX_ETF_DAILY_ROWS,
    _commit_etf_daily_date,
    _initialize_full_cycle,
    _load_earliest_etf_list_date,
    fetch_etf_daily,
    fetch_etf_daily_for_trade_date,
    normalize_etf_daily,
    sync_etf_daily_full,
    sync_etf_daily_incremental,
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
            sync_key=ETF_DAILY_INCREMENTAL_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={"synced_through_date": "2026-08-13"},
            cursor_version=1,
            version=1,
        )
        second_checkpoint = DataSyncCheckpointState(
            sync_key=ETF_DAILY_INCREMENTAL_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={"synced_through_date": "2026-08-14"},
            cursor_version=1,
            version=2,
        )
        commit_mock.side_effect = [
            (EtfDailyBarUpsertResult(received=1, changed=1, unchanged=0), first_checkpoint),
            (EtfDailyBarUpsertResult(received=1, changed=0, unchanged=1), second_checkpoint),
        ]

        result = sync_etf_daily_incremental(
            client,
            as_of_date=second_date,
            request_interval_ms=1_500,
            calendar_ids=("SSE",),
            calendar_for_code=lambda _ts_code: "SSE",
            data_cutoff=datetime(2026, 8, 15, tzinfo=UTC),
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
                "etf_daily_calendar_planned",
                "etf_daily_calendar_started",
                "etf_daily_calendar_succeeded",
                "etf_daily_calendar_started",
                "etf_daily_calendar_succeeded",
            ],
        )
        required_fields = {
            "data_type",
            "calendar_id",
            "start_date",
            "end_date",
            "fetched_count",
            "changed_count",
            "unchanged_count",
            "failed_count",
            "checkpoint_scope",
            "checkpoint_before",
            "checkpoint_after",
            "checkpoint_advanced",
        }
        for event_call in logger_mock.info.call_args_list:
            self.assertTrue(required_fields.issubset(event_call.kwargs))
            message = event_call.kwargs["message"]
            for fragment in ("ETF 日线", "条", "checkpoint"):
                self.assertIn(fragment, message)

    @patch("app.data_ingestion.services.etf_daily.logger")
    @patch("app.data_ingestion.services.etf_daily.fetch_etf_daily_for_trade_date")
    @patch("app.data_ingestion.services.etf_daily._load_open_dates")
    @patch("app.data_ingestion.services.etf_daily._load_checkpoint")
    @patch("app.data_ingestion.services.etf_daily.get_settings")
    @patch("app.data_ingestion.services.etf_daily.tushare_request_pacer")
    def test_failed_market_session_emits_complete_failure_event(
        self,
        pacer_mock,
        get_settings_mock,
        load_checkpoint_mock,
        load_open_dates_mock,
        fetch_mock,
        logger_mock,
    ) -> None:
        get_settings_mock.return_value = SimpleNamespace(
            ingestion_request_interval_ms=1_000
        )
        load_checkpoint_mock.return_value = None
        failed_date = date(2026, 8, 14)
        load_open_dates_mock.return_value = [failed_date]
        fetch_mock.side_effect = RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            sync_etf_daily_incremental(
                Mock(),
                as_of_date=failed_date,
                calendar_ids=("SSE",),
                calendar_for_code=lambda _ts_code: "SSE",
                data_cutoff=datetime(2026, 8, 15, tzinfo=UTC),
            )

        self.assertEqual(
            [item.args[0] for item in logger_mock.info.call_args_list],
            ["etf_daily_calendar_planned", "etf_daily_calendar_started"],
        )
        failure = logger_mock.exception.call_args
        self.assertEqual(failure.args[0], "etf_daily_calendar_failed")
        self.assertEqual(failure.kwargs["failed_count"], 1)
        self.assertFalse(failure.kwargs["checkpoint_advanced"])
        self.assertIn("ETF 日线", failure.kwargs["message"])
        self.assertIn("checkpoint 未推进", failure.kwargs["message"])

    @patch("app.data_ingestion.services.etf_daily._commit_etf_daily_date")
    @patch("app.data_ingestion.services.etf_daily.fetch_etf_daily_for_trade_date")
    @patch("app.data_ingestion.services.etf_daily._load_open_dates")
    @patch("app.data_ingestion.services.etf_daily._load_checkpoint")
    @patch("app.data_ingestion.services.etf_daily.get_settings")
    @patch("app.data_ingestion.services.etf_daily.tushare_request_pacer")
    def test_full_sync_resumes_the_frozen_cycle_target(
        self,
        pacer_mock,
        get_settings_mock,
        load_checkpoint_mock,
        load_open_dates_mock,
        fetch_mock,
        commit_mock,
    ) -> None:
        initial_date = date(2020, 1, 1)
        resumed_date = date(2026, 8, 14)
        frozen_target = date(2026, 8, 15)
        get_settings_mock.return_value = SimpleNamespace(
            ingestion_request_interval_ms=1_000
        )
        checkpoint = DataSyncCheckpointState(
            sync_key=ETF_DAILY_FULL_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={
                "synced_through_date": "2026-08-13",
                "target_through_date": "2026-08-15",
            },
            cursor_version=1,
            version=4,
        )
        load_checkpoint_mock.return_value = checkpoint
        load_open_dates_mock.return_value = [resumed_date]
        fetch_mock.return_value = make_dataframe()
        completed_checkpoint = DataSyncCheckpointState(
            sync_key=ETF_DAILY_FULL_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={
                "synced_through_date": "2026-08-15",
                "target_through_date": "2026-08-15",
            },
            cursor_version=1,
            version=5,
        )
        commit_mock.return_value = (
            EtfDailyBarUpsertResult(received=1, changed=0, unchanged=1),
            completed_checkpoint,
        )

        with self.assertRaises(InstrumentCalendarUnresolvedError):
            sync_etf_daily_full(
                Mock(),
                as_of_date=date(2026, 8, 20),
            )

        # The legacy entry point is blocked before it can read or advance the
        # former market-wide checkpoint.
        load_checkpoint_mock.assert_not_called()
        load_open_dates_mock.assert_not_called()
        fetch_mock.assert_not_called()
        commit_mock.assert_not_called()

    @patch("app.data_ingestion.services.etf_daily.EtfCodeRepository")
    @patch("app.data_ingestion.services.etf_daily.get_engine")
    @patch("app.data_ingestion.services.etf_daily.Session")
    def test_full_sync_start_date_comes_from_etf_reference_data(
        self,
        session_class,
        get_engine_mock,
        code_repository_class,
    ) -> None:
        code_repository_class.return_value.earliest_list_date.return_value = date(
            2005, 2, 23
        )

        result = _load_earliest_etf_list_date()

        self.assertEqual(result, date(2005, 2, 23))
        code_repository_class.return_value.earliest_list_date.assert_called_once_with(
            source="tushare"
        )

    @patch("app.data_ingestion.services.etf_daily.EtfCodeRepository")
    @patch("app.data_ingestion.services.etf_daily.get_engine")
    @patch("app.data_ingestion.services.etf_daily.Session")
    def test_full_sync_requires_etf_reference_data(
        self,
        session_class,
        get_engine_mock,
        code_repository_class,
    ) -> None:
        code_repository_class.return_value.earliest_list_date.return_value = None

        with self.assertRaisesRegex(ValueError, "ETF basic data is required"):
            _load_earliest_etf_list_date()

    @patch("app.data_ingestion.services.etf_daily.DataSyncCheckpointRepository")
    @patch("app.data_ingestion.services.etf_daily.get_engine")
    @patch("app.data_ingestion.services.etf_daily.Session")
    def test_initializes_a_full_cycle_before_the_first_request(
        self,
        session_class,
        get_engine_mock,
        checkpoint_repository_class,
    ) -> None:
        session = session_class.return_value.__enter__.return_value
        expected = DataSyncCheckpointState(
            sync_key=ETF_DAILY_FULL_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={
                "synced_through_date": "2026-08-15",
                "target_through_date": "2026-08-15",
            },
            cursor_version=1,
            version=8,
        )
        checkpoint_repository_class.return_value.advance.return_value = expected

        actual = _initialize_full_cycle(
            expected_checkpoint=None,
            initial_start_date=date(2005, 1, 1),
            target_through_date=date(2026, 8, 15),
        )

        self.assertIs(actual, expected)
        checkpoint_repository_class.return_value.advance.assert_called_once_with(
            sync_key=ETF_DAILY_FULL_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={
                "synced_through_date": "2004-12-31",
                "target_through_date": "2026-08-15",
            },
            expected_version=None,
        )
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()

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
            sync_key=ETF_DAILY_INCREMENTAL_SYNC_KEY,
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
            sync_key=ETF_DAILY_INCREMENTAL_SYNC_KEY,
        )

        self.assertIs(result, write_result)
        self.assertIs(actual_checkpoint, checkpoint)
        bar_repository_class.return_value.upsert_bars.assert_called_once_with(
            [make_bar()], source="tushare"
        )
        checkpoint_repository_class.return_value.advance.assert_called_once_with(
            sync_key=ETF_DAILY_INCREMENTAL_SYNC_KEY,
            scope_key=ETF_DAILY_SCOPE_KEY,
            cursor={"synced_through_date": "2026-08-14"},
            expected_version=None,
        )
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
