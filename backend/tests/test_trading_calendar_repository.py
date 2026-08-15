import unittest
from datetime import date
from unittest.mock import Mock

from sqlalchemy.dialects import postgresql

from app.data_ingestion.repositories.trading_calendar import TradingCalendarRepository
from app.data_ingestion.schemas.trading_calendar import TradingCalendarDayInput


class TradingCalendarRepositoryTestCase(unittest.TestCase):
    def test_upsert_updates_only_changed_calendar_rows(self) -> None:
        session = Mock()
        session.execute.return_value.all.return_value = [("SSE", date(2018, 1, 1))]
        repository = TradingCalendarRepository(session)

        result = repository.upsert_days(
            [
                TradingCalendarDayInput(
                    exchange="SSE",
                    calendar_date=date(2018, 1, 1),
                    is_open=False,
                    previous_trading_date=date(2017, 12, 29),
                ),
                TradingCalendarDayInput(
                    exchange="SSE",
                    calendar_date=date(2018, 1, 2),
                    is_open=True,
                    previous_trading_date=date(2017, 12, 29),
                ),
            ]
        )

        self.assertEqual(result.received, 2)
        self.assertEqual(result.changed, 1)
        self.assertEqual(result.unchanged, 1)
        statement = session.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT (exchange, calendar_date) DO UPDATE", sql)
        self.assertIn("IS DISTINCT FROM", sql)

    def test_rejects_duplicate_keys_before_executing_sql(self) -> None:
        session = Mock()
        repository = TradingCalendarRepository(session)
        day = TradingCalendarDayInput(
            exchange="SSE",
            calendar_date=date(2018, 1, 1),
            is_open=False,
            previous_trading_date=date(2017, 12, 29),
        )

        with self.assertRaisesRegex(ValueError, "duplicate exchange"):
            repository.upsert_days([day, day])

        session.execute.assert_not_called()
