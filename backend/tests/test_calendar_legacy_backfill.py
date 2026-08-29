"""Focused SQLite checks for the explicit legacy calendar backfill boundary."""

from datetime import date, datetime, timezone
import importlib
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, Column, Date, DateTime, MetaData, String, Table, create_engine, text
from sqlalchemy.orm import Session

from app.backtesting.calendar_models import CalendarReconciliationRangeRecord, CalendarSessionFactRecord
from app.backtesting.data.calendar_repository import CalendarFactRepository
from app.data_ingestion.services.trade_calendar import backfill_legacy_trading_calendar_days


class LegacyCalendarBackfillTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        with self.engine.begin() as connection:
            migration = importlib.import_module(
                "app.db.migrations.versions.20260829_01_add_named_calendar_facts"
            )
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
            Table(
                "trading_calendar_days",
                MetaData(),
                Column("exchange", String(16), nullable=False),
                Column("calendar_date", Date, nullable=False),
                Column("is_open", Boolean, nullable=False),
                Column("previous_trading_date", Date),
                Column("created_at", DateTime),
                Column("updated_at", DateTime),
            ).create(connection)
            # The production checkpoint table is created by an earlier
            # migration; keep this focused fixture independent of migration order.
            connection.execute(
                text(
                    "CREATE TABLE data_sync_checkpoints ("
                    "sync_key VARCHAR(128) NOT NULL, scope_key VARCHAR(256) NOT NULL, "
                    "cursor JSON NOT NULL, cursor_version INTEGER NOT NULL DEFAULT 1, "
                    "version INTEGER NOT NULL DEFAULT 1, created_at DATETIME, updated_at DATETIME, "
                    "PRIMARY KEY(sync_key, scope_key))"
                )
            )

    def _insert(self, *rows: tuple[str, str, int]) -> None:
        with self.engine.begin() as connection:
            for exchange, calendar_date, is_open in rows:
                connection.execute(
                    text(
                        "INSERT INTO trading_calendar_days(exchange, calendar_date, is_open) "
                        "VALUES (:exchange, :calendar_date, :is_open)"
                    ),
                    {"exchange": exchange, "calendar_date": calendar_date, "is_open": is_open},
                )

    def test_replay_is_idempotent_and_keeps_legacy_rows(self) -> None:
        self._insert(("SSE", "2026-01-02", 1), ("SSE", "2026-01-03", 0))
        kwargs = {
            "source_revision": "legacy-v1",
            "data_cutoff": datetime(2026, 2, 1, tzinfo=timezone.utc),
            "known_at": datetime(2026, 1, 10, tzinfo=timezone.utc),
            "observed_at": datetime(2026, 1, 10, tzinfo=timezone.utc),
            "batch_size": 1,
        }
        with Session(self.engine) as session:
            first = backfill_legacy_trading_calendar_days(session, **kwargs)
            checkpoint_before_replay = session.execute(
                text(
                    "SELECT version, cursor FROM data_sync_checkpoints "
                    "WHERE scope_key = 'calendar_id=SSE'"
                )
            ).one()
            second = backfill_legacy_trading_calendar_days(session, **kwargs)
            self.assertEqual((first.changed, first.unchanged), (2, 0))
            self.assertEqual((second.changed, second.unchanged), (0, 2))
            self.assertTrue(first.checkpoint_advanced)
            self.assertFalse(second.checkpoint_advanced)
            self.assertEqual(session.query(CalendarSessionFactRecord).count(), 2)
            self.assertEqual(
                session.execute(text("SELECT COUNT(*) FROM trading_calendar_days")).scalar_one(),
                2,
            )
            checkpoint_after_replay = session.execute(
                text(
                    "SELECT version, cursor FROM data_sync_checkpoints "
                    "WHERE scope_key = 'calendar_id=SSE'"
                )
            ).one()
            self.assertEqual(checkpoint_after_replay, checkpoint_before_replay)
            self.assertIn("2026-01-03", checkpoint_after_replay._mapping["cursor"])

    def test_missing_natural_day_is_gap_without_closed_fact_or_checkpoint(self) -> None:
        self._insert(("SSE", "2026-01-02", 1), ("SSE", "2026-01-04", 0))
        with Session(self.engine) as session:
            report = backfill_legacy_trading_calendar_days(
                session,
                source_revision="legacy-v1",
                data_cutoff=datetime(2026, 2, 1, tzinfo=timezone.utc),
                known_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                observed_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            )
            self.assertEqual(report.status, "blocked")
            self.assertFalse(report.checkpoint_advanced)
            self.assertEqual(report.gaps[0]["reason"], "missing_natural_day")
            self.assertEqual(session.query(CalendarSessionFactRecord).count(), 1)
            self.assertEqual(session.query(CalendarReconciliationRangeRecord).count(), 1)

    def test_explicit_range_boundaries_are_gaps_without_checkpoint(self) -> None:
        self._insert(("SSE", "2026-01-02", 1))
        with Session(self.engine) as session:
            report = backfill_legacy_trading_calendar_days(
                session,
                source_revision="legacy-v1",
                data_cutoff=datetime(2026, 2, 1, tzinfo=timezone.utc),
                known_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                observed_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 3),
            )
            self.assertEqual(report.status, "blocked")
            self.assertFalse(report.checkpoint_advanced)
            self.assertEqual(
                [(gap["range_start"], gap["range_end"]) for gap in report.gaps],
                [("2026-01-01", "2026-01-02"), ("2026-01-03", "2026-01-04")],
            )
            self.assertEqual(session.query(CalendarSessionFactRecord).count(), 0)
            self.assertEqual(session.query(CalendarReconciliationRangeRecord).count(), 2)

    def test_changed_legacy_content_appends_a_superseding_fact(self) -> None:
        self._insert(("SSE", "2026-01-02", 1))
        kwargs = {
            "source_revision": "legacy-v1",
            "data_cutoff": datetime(2026, 2, 1, tzinfo=timezone.utc),
            "known_at": datetime(2026, 1, 10, tzinfo=timezone.utc),
            "observed_at": datetime(2026, 1, 10, tzinfo=timezone.utc),
        }
        with Session(self.engine) as session:
            backfill_legacy_trading_calendar_days(session, **kwargs)
            session.execute(
                text(
                    "UPDATE trading_calendar_days SET is_open = 0 "
                    "WHERE exchange = 'SSE' AND calendar_date = '2026-01-02'"
                )
            )
            report = backfill_legacy_trading_calendar_days(session, **kwargs)
            facts = session.query(CalendarSessionFactRecord).order_by(CalendarSessionFactRecord.fact_version).all()
            self.assertEqual(report.changed, 1)
            self.assertEqual(len(facts), 2)
            self.assertEqual(facts[1].supersedes_fact_id, facts[0].fact_id)
            self.assertFalse(facts[1].is_open)

    def test_closed_valid_to_expands_to_half_open_daily_facts(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE trading_calendar_days ADD COLUMN valid_to DATE"))
            connection.execute(
                text(
                    "INSERT INTO trading_calendar_days(exchange, calendar_date, is_open, valid_to) "
                    "VALUES ('SSE', '2026-01-01', 1, '2026-01-02')"
                )
            )

        with Session(self.engine) as session:
            report = backfill_legacy_trading_calendar_days(
                session,
                source_revision="legacy-v1",
                data_cutoff=datetime(2026, 2, 1, tzinfo=timezone.utc),
                known_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                observed_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            )
            facts = session.query(CalendarSessionFactRecord).order_by(
                CalendarSessionFactRecord.session_date
            ).all()
            domain_facts = CalendarFactRepository(session).list_session_facts(
                ("SSE",), date(2026, 1, 1), date(2026, 1, 3)
            )

        self.assertEqual(report.status, "completed")
        self.assertEqual((report.fetched, report.changed), (1, 2))
        self.assertEqual(report.checkpoint.cursor["synced_through_date"], "2026-01-02")
        self.assertEqual(
            [(fact.session_date, fact.valid_from, fact.valid_to) for fact in facts],
            [
                (date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 2)),
                (date(2026, 1, 2), date(2026, 1, 2), date(2026, 1, 3)),
            ],
        )
        self.assertEqual(
            {fact.evidence["legacy_range_end"] for fact in facts},
            {"2026-01-03"},
        )
        self.assertEqual(len(domain_facts), 2)
        self.assertTrue(domain_facts[0].applies_to(date(2026, 1, 1)))
        self.assertTrue(domain_facts[1].applies_to(date(2026, 1, 2)))


if __name__ == "__main__":
    unittest.main()
