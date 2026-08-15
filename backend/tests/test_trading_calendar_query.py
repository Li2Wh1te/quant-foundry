from datetime import date, datetime, timezone
import unittest
from unittest.mock import Mock

from app.data_ingestion.models.sync_checkpoint import DataSyncCheckpoint
from app.data_ingestion.models.trading_calendar import TradingCalendarDay
from app.data_ingestion.repositories.trading_calendar_query import (
    TradingCalendarQueryRepository,
)


class TradingCalendarQueryRepositoryTestCase(unittest.TestCase):
    def test_lists_filtered_days_with_a_matching_total(self) -> None:
        day = TradingCalendarDay(
            exchange="SSE",
            calendar_date=date(2026, 8, 14),
            is_open=True,
            previous_trading_date=date(2026, 8, 13),
        )
        session = Mock()
        session.scalars.return_value.all.return_value = [day]
        session.scalar.return_value = 1

        days, total = TradingCalendarQueryRepository(session).list_days(
            exchange="SSE",
            is_open=True,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            limit=50,
            offset=0,
        )

        self.assertEqual(days, [day])
        self.assertEqual(total, 1)
        page_query = str(session.scalars.call_args.args[0])
        count_query = str(session.scalar.call_args.args[0])
        self.assertIn("trading_calendar_days.exchange", page_query)
        self.assertIn("trading_calendar_days.is_open IS true", page_query)
        self.assertIn("trading_calendar_days.exchange", count_query)
        self.assertIn("trading_calendar_days.is_open IS true", count_query)

    def test_overview_ignores_malformed_checkpoint_dates(self) -> None:
        session = Mock()
        session.execute.return_value.one.return_value = (
            3,
            2,
            2,
            date(2026, 8, 12),
            date(2026, 8, 14),
            datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc),
        )
        valid = DataSyncCheckpoint(
            sync_key="tushare.trade_calendar",
            scope_key="exchange=SSE",
            cursor={"synced_through_date": "2026-08-14"},
        )
        malformed = DataSyncCheckpoint(
            sync_key="tushare.trade_calendar",
            scope_key="exchange=SZSE",
            cursor={"synced_through_date": "not-a-date"},
        )
        session.scalars.return_value.all.return_value = [valid, malformed]

        overview = TradingCalendarQueryRepository(session).overview()

        self.assertEqual(overview.total_records, 3)
        self.assertEqual(overview.exchange_count, 2)
        self.assertEqual(overview.open_day_count, 2)
        self.assertEqual(overview.start_date, date(2026, 8, 12))
        self.assertEqual(overview.end_date, date(2026, 8, 14))
        self.assertEqual(overview.checkpoints, {"SSE": date(2026, 8, 14)})


if __name__ == "__main__":
    unittest.main()
