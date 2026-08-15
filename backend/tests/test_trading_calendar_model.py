import unittest

from app.data_ingestion.models.trading_calendar import TradingCalendarDay


class TradingCalendarDayModelTestCase(unittest.TestCase):
    def test_uses_exchange_and_calendar_date_as_the_primary_key(self) -> None:
        self.assertEqual(
            [column.name for column in TradingCalendarDay.__table__.primary_key.columns],
            ["exchange", "calendar_date"],
        )

    def test_has_an_open_day_partial_index(self) -> None:
        indexes = {index.name: index for index in TradingCalendarDay.__table__.indexes}

        self.assertIn("ix_trading_calendar_days_open_date", indexes)
        self.assertEqual(
            str(indexes["ix_trading_calendar_days_open_date"].dialect_options["postgresql"]["where"]),
            "is_open",
        )
