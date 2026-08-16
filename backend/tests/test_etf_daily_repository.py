"""Tests for idempotent ETF daily-bar persistence statements."""

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from sqlalchemy.dialects import postgresql

from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
from app.data_ingestion.schemas.etf_daily import EtfDailyBarInput


def make_bar() -> EtfDailyBarInput:
    return EtfDailyBarInput(
        ts_code="510330.SH",
        trade_date=date(2026, 8, 14),
        open=Decimal("3.71"),
        high=Decimal("3.75"),
        low=Decimal("3.70"),
        close=Decimal("3.74"),
        vol=Decimal("12345"),
        amount=Decimal("46000.5"),
    )


class EtfDailyBarRepositoryTestCase(unittest.TestCase):
    def test_upsert_updates_only_changed_source_values(self) -> None:
        session = Mock()
        session.execute.return_value.all.return_value = [("510330.SH",)]

        result = EtfDailyBarRepository(session).upsert_bars(
            [make_bar()], source="tushare"
        )

        self.assertEqual((result.received, result.changed, result.unchanged), (1, 1, 0))
        statement = session.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn(
            "ON CONFLICT (source, ts_code, trade_date) DO UPDATE", sql
        )
        self.assertIn("IS DISTINCT FROM excluded.open", sql)
        self.assertIn("updated_at = now()", sql)

    def test_rejects_duplicate_source_bar_keys_before_writing(self) -> None:
        session = Mock()

        with self.assertRaisesRegex(ValueError, "duplicate ts_code and trade_date"):
            EtfDailyBarRepository(session).upsert_bars(
                [make_bar(), make_bar()], source="tushare"
            )

        session.execute.assert_not_called()
