import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.data_ingestion.schemas.trading_calendar import (
    DataSyncCheckpointState,
    TradeCalendarUpsertResult,
    TradingCalendarDayInput,
)
from app.data_ingestion.services.trade_calendar import (
    _commit_trade_calendar_range,
    fetch_trade_calendar,
    normalize_trade_calendar,
    plan_trade_calendar_year_ranges,
    sync_trade_calendar,
)


class FetchTradeCalendarTestCase(unittest.TestCase):
    def test_delegates_to_tushare_trade_calendar_api(self) -> None:
        client = Mock()

        result = fetch_trade_calendar(
            client,
            exchange="SSE",
            start_date="20180101",
            end_date="20181231",
        )

        self.assertIs(result, client.pro.trade_cal.return_value)
        client.pro.trade_cal.assert_called_once_with(
            exchange="SSE",
            start_date="20180101",
            end_date="20181231",
        )

    def test_normalizes_tushare_trade_calendar_rows(self) -> None:
        dataframe = Mock()
        dataframe.to_dict.return_value = [
            {
                "exchange": "SSE",
                "cal_date": "20180101",
                "is_open": "0",
                "pretrade_date": "20171229",
            },
            {
                "exchange": "SSE",
                "cal_date": "20180102",
                "is_open": "1",
                "pretrade_date": "20171229",
            },
        ]

        days = normalize_trade_calendar(dataframe)

        self.assertEqual(
            [(day.calendar_date, day.is_open, day.previous_trading_date) for day in days],
            [
                (date(2018, 1, 1), False, date(2017, 12, 29)),
                (date(2018, 1, 2), True, date(2017, 12, 29)),
            ],
        )

    def test_plans_full_calendar_years_and_partial_boundaries(self) -> None:
        ranges = plan_trade_calendar_year_ranges(
            checkpoint=None,
            initial_start_date=date(2018, 7, 15),
            as_of_date=date(2020, 3, 20),
        )

        self.assertEqual(
            [(item.start_date, item.end_date) for item in ranges],
            [
                (date(2018, 7, 15), date(2018, 12, 31)),
                (date(2019, 1, 1), date(2019, 12, 31)),
                (date(2020, 1, 1), date(2020, 3, 20)),
            ],
        )

    def test_resumes_after_the_checkpoint_date(self) -> None:
        checkpoint = DataSyncCheckpointState(
            sync_key="tushare.trade_calendar",
            scope_key="exchange=SSE",
            cursor={"synced_through_date": "2025-09-08"},
            cursor_version=1,
            version=3,
        )

        ranges = plan_trade_calendar_year_ranges(
            checkpoint=checkpoint,
            initial_start_date=date(1990, 12, 19),
            as_of_date=date(2026, 8, 15),
        )

        self.assertEqual(
            [(item.start_date, item.end_date) for item in ranges],
            [
                (date(2025, 9, 9), date(2025, 12, 31)),
                (date(2026, 1, 1), date(2026, 8, 15)),
            ],
        )

    @patch("app.data_ingestion.services.trade_calendar.DataSyncCheckpointRepository")
    @patch("app.data_ingestion.services.trade_calendar.TradingCalendarRepository")
    @patch("app.data_ingestion.services.trade_calendar.get_engine")
    @patch("app.data_ingestion.services.trade_calendar.Session")
    def test_commits_data_and_checkpoint_in_one_transaction(
        self,
        session_class,
        get_engine_mock,
        trading_repository_class,
        checkpoint_repository_class,
    ) -> None:
        session = session_class.return_value.__enter__.return_value
        expected = TradeCalendarUpsertResult(received=1, changed=1, unchanged=0)
        trading_repository_class.return_value.upsert_days.return_value = expected
        advanced_checkpoint = DataSyncCheckpointState(
            sync_key="tushare.trade_calendar",
            scope_key="exchange=SSE",
            cursor={"synced_through_date": "2018-01-01"},
            cursor_version=1,
            version=1,
        )
        checkpoint_repository_class.return_value.advance.return_value = advanced_checkpoint
        day = TradingCalendarDayInput(
            exchange="SSE",
            calendar_date=date(2018, 1, 1),
            is_open=False,
            previous_trading_date=date(2017, 12, 29),
        )

        result, checkpoint = _commit_trade_calendar_range(
            days=[day],
            sync_key="tushare.trade_calendar",
            scope_key="exchange=SSE",
            expected_checkpoint=None,
            synced_through_date=date(2018, 1, 1),
        )

        self.assertIs(result, expected)
        self.assertIs(checkpoint, advanced_checkpoint)
        get_engine_mock.assert_called_once_with()
        session_class.assert_called_once_with(get_engine_mock.return_value)
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()

    @patch("app.data_ingestion.services.trade_calendar.DataSyncCheckpointRepository")
    @patch("app.data_ingestion.services.trade_calendar.TradingCalendarRepository")
    @patch("app.data_ingestion.services.trade_calendar.get_engine")
    @patch("app.data_ingestion.services.trade_calendar.Session")
    def test_rolls_back_data_when_checkpoint_advance_fails(
        self,
        session_class,
        get_engine_mock,
        trading_repository_class,
        checkpoint_repository_class,
    ) -> None:
        session = session_class.return_value.__enter__.return_value
        trading_repository_class.return_value.upsert_days.return_value = TradeCalendarUpsertResult(
            received=1, changed=1, unchanged=0
        )
        checkpoint_repository_class.return_value.advance.side_effect = RuntimeError(
            "checkpoint error"
        )
        day = TradingCalendarDayInput(
            exchange="SSE",
            calendar_date=date(2018, 1, 1),
            is_open=False,
            previous_trading_date=date(2017, 12, 29),
        )

        with self.assertRaisesRegex(RuntimeError, "checkpoint error"):
            _commit_trade_calendar_range(
                days=[day],
                sync_key="tushare.trade_calendar",
                scope_key="exchange=SSE",
                expected_checkpoint=None,
                synced_through_date=date(2018, 1, 1),
            )

        session.commit.assert_not_called()
        session.rollback.assert_called_once_with()

    @patch("app.data_ingestion.services.trade_calendar._commit_trade_calendar_range")
    @patch("app.data_ingestion.services.trade_calendar.normalize_trade_calendar")
    @patch("app.data_ingestion.services.trade_calendar.fetch_trade_calendar")
    @patch("app.data_ingestion.services.trade_calendar.tushare_request_pacer")
    @patch("app.data_ingestion.services.trade_calendar._load_checkpoint")
    @patch("app.data_ingestion.services.trade_calendar.get_settings")
    def test_uses_the_larger_task_request_interval(
        self,
        get_settings_mock,
        load_checkpoint_mock,
        pacer_mock,
        fetch_mock,
        normalize_mock,
        commit_mock,
    ) -> None:
        get_settings_mock.return_value = SimpleNamespace(
            ingestion_request_interval_ms=1_000
        )
        load_checkpoint_mock.return_value = None
        day = TradingCalendarDayInput(
            exchange="SSE",
            calendar_date=date(2018, 1, 1),
            is_open=False,
            previous_trading_date=date(2017, 12, 29),
        )
        normalize_mock.return_value = [day]
        updated_checkpoint = DataSyncCheckpointState(
            sync_key="tushare.trade_calendar",
            scope_key="exchange=SSE",
            cursor={"synced_through_date": "2018-01-01"},
            cursor_version=1,
            version=1,
        )
        commit_mock.return_value = (
            TradeCalendarUpsertResult(received=1, changed=1, unchanged=0),
            updated_checkpoint,
        )

        client = Mock()
        result = sync_trade_calendar(
            client,
            exchange="SSE",
            initial_start_date=date(2018, 1, 1),
            as_of_date=date(2018, 1, 1),
            request_interval_ms=1_500,
        )

        self.assertEqual(result.changed, 1)
        pacer_mock.wait_for_turn.assert_called_once_with(1_500)
        fetch_mock.assert_called_once_with(
            client,
            exchange="SSE",
            start_date="20180101",
            end_date="20180101",
        )
