"""Tests for idempotent ETF daily-bar persistence statements."""

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy.dialects import postgresql

from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
from app.data_ingestion.schemas.etf_daily import EtfDailyBarInput
from app.data_ingestion.schemas.etf_daily import canonical_row_revision


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
        current = SimpleNamespace(
            source="tushare",
            ts_code="510330.SH",
            trade_date=date(2026, 8, 14),
            open=Decimal("3.70"),  # corrected by the incoming bar
            high=Decimal("3.75"),
            low=Decimal("3.70"),
            close=Decimal("3.74"),
            vol=Decimal("12345"),
            amount=Decimal("46000.5"),
            source_revision="old-revision",
        )
        session.get.return_value = current
        accepted_at = datetime(2026, 8, 15, 1, 2, 3, tzinfo=UTC)

        result = EtfDailyBarRepository(session).upsert_bars(
            [make_bar()], source="tushare", accepted_at=accepted_at
        )

        self.assertEqual((result.received, result.changed, result.unchanged), (1, 1, 0))
        self.assertEqual(
            (result.inserted, result.corrected, result.metadata_backfilled),
            (0, 1, 0),
        )
        self.assertTrue(result.batch_revision)

        # The repository owns no transaction boundary: it emits an UPDATE and
        # append-only audit INSERT, leaving commit/rollback to the service.
        self.assertEqual(session.execute.call_count, 2)
        update_statement, audit_statement = [
            call.args[0] for call in session.execute.call_args_list
        ]
        update_sql = str(update_statement.compile(dialect=postgresql.dialect()))
        self.assertIn("UPDATE etf_daily_bars SET", update_sql)
        self.assertNotIn("ON CONFLICT", update_sql)
        update_params = update_statement.compile(dialect=postgresql.dialect()).params
        self.assertEqual(update_params["open"], make_bar().open)
        expected_revision = canonical_row_revision(make_bar(), source="tushare")
        self.assertEqual(update_params["source_revision"], expected_revision)
        self.assertNotEqual(update_params["source_revision"], current.source_revision)
        self.assertEqual(session.get.call_args.args[1], ("tushare", "510330.SH", date(2026, 8, 14)))

        audit_sql = str(audit_statement.compile(dialect=postgresql.dialect()))
        self.assertIn("INSERT INTO etf_daily_bar_revision_audits", audit_sql)
        audit_params = audit_statement.compile(dialect=postgresql.dialect()).params
        self.assertEqual(audit_params["source"], "tushare")
        self.assertEqual(audit_params["ts_code"], "510330.SH")
        self.assertEqual(audit_params["trade_date"], date(2026, 8, 14))
        self.assertEqual(audit_params["previous_source_revision"], "old-revision")
        self.assertEqual(audit_params["accepted_at"], accepted_at)
        self.assertEqual(audit_params["change_kind"], "correction")
        self.assertEqual(audit_params["changed_fields"], ["open"])
        self.assertEqual(audit_params["batch_revision"], result.batch_revision)
        session.commit.assert_not_called()

    def test_rejects_duplicate_source_bar_keys_before_writing(self) -> None:
        session = Mock()

        with self.assertRaisesRegex(ValueError, "duplicate ts_code and trade_date"):
            EtfDailyBarRepository(session).upsert_bars(
                [make_bar(), make_bar()], source="tushare"
            )

        session.execute.assert_not_called()

    def test_list_bars_is_source_and_code_scoped_in_ascending_date_order(self) -> None:
        session = Mock()
        session.scalars.return_value.all.return_value = []

        result = EtfDailyBarRepository(session).list_bars(
            source="tushare",
            ts_code="510330.SH",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
        )

        self.assertEqual(result, ())
        statement = session.scalars.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("etf_daily_bars.source = %(source_1)s", sql)
        self.assertIn("etf_daily_bars.ts_code = %(ts_code_1)s", sql)
        self.assertIn("etf_daily_bars.trade_date >= %(trade_date_1)s", sql)
        self.assertIn("etf_daily_bars.trade_date <= %(trade_date_2)s", sql)
        self.assertIn("ORDER BY etf_daily_bars.trade_date ASC", sql)
