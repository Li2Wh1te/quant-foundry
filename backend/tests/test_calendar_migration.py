"""SQLite replay checks for the deterministic task-11 calendar bootstrap."""

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
import unittest
from unittest.mock import patch

from app.backtesting.data.errors import (
    CalendarSourcePriorityChainBrokenError,
    CalendarSourcePriorityInvalidError,
    CalendarSourcePriorityMissingError,
)


MIGRATION_MODULE = "app.db.migrations.versions.20260829_01_add_named_calendar_facts"
SEED_TABLES = (
    "calendar_source_priorities",
    "calendar_registry",
    "calendar_definitions",
    "calendar_exchange_bindings",
)


class CalendarMigrationTestCase(unittest.TestCase):
    @staticmethod
    def _upgrade(connection) -> None:
        migration = __import__(MIGRATION_MODULE, fromlist=["upgrade"])
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

    @staticmethod
    def _counts(connection) -> dict[str, int]:
        tables = (*SEED_TABLES, "calendar_session_facts", "calendar_capability_declarations")
        return {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in tables
        }

    def test_upgrade_replay_does_not_duplicate_seed_or_create_session_facts(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as connection:
            self._upgrade(connection)
            first_counts = self._counts(connection)
            first_hashes = {
                table: connection.execute(
                    text(f"SELECT fact_id, content_hash FROM {table} ORDER BY fact_id")
                ).all()
                for table in SEED_TABLES
            }

            self._upgrade(connection)

            self.assertEqual(
                first_counts,
                {
                    "calendar_source_priorities": 1,
                    "calendar_registry": 2,
                    "calendar_definitions": 2,
                    "calendar_exchange_bindings": 6,
                    "calendar_session_facts": 0,
                    "calendar_capability_declarations": 0,
                },
            )
            self.assertEqual(self._counts(connection), first_counts)
            self.assertEqual(
                {
                    table: connection.execute(
                        text(f"SELECT fact_id, content_hash FROM {table} ORDER BY fact_id")
                    ).all()
                    for table in SEED_TABLES
                },
                first_hashes,
            )

    def test_replay_repairs_seed_and_missing_append_only_guards(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as connection:
            self._upgrade(connection)
            trigger_names = connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).scalars()
            for trigger_name in trigger_names:
                connection.execute(text(f'DROP TRIGGER IF EXISTS "{trigger_name}"'))
            connection.execute(text("DELETE FROM calendar_exchange_bindings"))
            connection.execute(text("DELETE FROM calendar_definitions"))
            connection.execute(text("DELETE FROM calendar_registry"))
            connection.execute(text("DELETE FROM calendar_source_priorities"))

            self._upgrade(connection)

            counts = self._counts(connection)
            self.assertEqual(counts["calendar_source_priorities"], 1)
            self.assertEqual(counts["calendar_registry"], 2)
            self.assertEqual(counts["calendar_definitions"], 2)
            self.assertEqual(counts["calendar_exchange_bindings"], 6)
            self.assertEqual(counts["calendar_session_facts"], 0)
            trigger_count = connection.execute(
                text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'")
            ).scalar_one()
            self.assertEqual(trigger_count, 12)

    def test_replay_rejects_existing_seed_with_missing_metadata(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as connection:
            self._upgrade(connection)
            connection.execute(text("PRAGMA ignore_check_constraints=ON"))
            connection.execute(text("DROP TRIGGER trg_calendar_source_priority_append_only_update"))
            connection.execute(text("DROP TRIGGER trg_calendar_source_priority_append_only_delete"))
            connection.execute(text("UPDATE calendar_source_priorities SET bootstrap_seed_id = NULL"))

            with self.assertRaises(CalendarSourcePriorityMissingError) as raised:
                self._upgrade(connection)
            self.assertEqual(raised.exception.code, "calendar_source_priority_missing")

    def test_replay_rejects_existing_seed_with_wrong_hash_or_signature(self) -> None:
        for field in ("bootstrap_seed_hash", "content_hash"):
            with self.subTest(field=field):
                engine = create_engine("sqlite:///:memory:")
                with engine.connect() as connection:
                    self._upgrade(connection)
                    connection.execute(text("DROP TRIGGER trg_calendar_source_priority_append_only_update"))
                    connection.execute(text("DROP TRIGGER trg_calendar_source_priority_append_only_delete"))
                    connection.execute(text(f"UPDATE calendar_source_priorities SET {field} = :value"), {"value": "f" * 64})

                    with self.assertRaises(CalendarSourcePriorityInvalidError) as raised:
                        self._upgrade(connection)
                    self.assertEqual(raised.exception.code, "calendar_source_priority_invalid")

    def test_replay_rejects_existing_seed_with_broken_revision_chain(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as connection:
            self._upgrade(connection)
            connection.execute(text("DROP TRIGGER trg_calendar_source_priority_append_only_update"))
            connection.execute(text("DROP TRIGGER trg_calendar_source_priority_append_only_delete"))
            connection.execute(
                text("UPDATE calendar_source_priorities SET supersedes_fact_id = :value"),
                {"value": "deadbeefdeadbeefdeadbeefdeadbeef"},
            )

            with self.assertRaises(CalendarSourcePriorityChainBrokenError) as raised:
                self._upgrade(connection)
            self.assertEqual(raised.exception.code, "calendar_source_priority_chain_broken")

    def test_seed_manifest_contains_controlled_entries_and_bound_hash(self) -> None:
        migration = __import__(MIGRATION_MODULE, fromlist=["seed_manifest"])
        manifest = migration.seed_manifest()
        self.assertEqual(manifest["evidence_uri"], "release://calendar-source-priority-bootstrap/1")
        self.assertEqual(manifest["signature_status"], "verified")
        self.assertEqual(
            migration._seed_content_hash(manifest["entries"]),
            manifest["bootstrap_seed_hash"],
        )

    def test_upgrade_rejects_format_valid_hash_for_changed_seed_entries(self) -> None:
        migration = __import__(MIGRATION_MODULE, fromlist=["seed_manifest"])
        manifest = migration.seed_manifest()
        manifest["entries"] = [
            {
                **manifest["entries"][0],
                "source_priority": manifest["entries"][0]["source_priority"] + 1,
            }
        ]
        manifest["bootstrap_seed_hash"] = migration._seed_content_hash(manifest["entries"])
        with patch.object(migration, "seed_manifest", return_value=manifest):
            engine = create_engine("sqlite:///:memory:")
            with engine.connect() as connection:
                with self.assertRaises(CalendarSourcePriorityInvalidError) as raised:
                    self._upgrade(connection)
                self.assertEqual(raised.exception.code, "calendar_source_priority_invalid")

    def test_upgrade_rejects_missing_evidence_or_signature(self) -> None:
        migration = __import__(MIGRATION_MODULE, fromlist=["seed_manifest"])
        for field in ("evidence_uri", "signature_status"):
            with self.subTest(field=field):
                manifest = migration.seed_manifest()
                manifest[field] = None
                with patch.object(migration, "seed_manifest", return_value=manifest):
                    engine = create_engine("sqlite:///:memory:")
                    with engine.connect() as connection:
                        with self.assertRaises(CalendarSourcePriorityMissingError) as raised:
                            self._upgrade(connection)
                        self.assertEqual(raised.exception.code, "calendar_source_priority_missing")


if __name__ == "__main__":
    unittest.main()
