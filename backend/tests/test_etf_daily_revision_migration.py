"""Acceptance tests for the ETF source-revision migration (MIG-01..MIG-06)."""

import importlib
import unittest
from datetime import date, datetime, timezone

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class EtfRevisionMigrationTest(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        # PostgreSQL exposes ``btrim`` used by the production constraints;
        # register an equivalent SQLite scalar function for migration tests.
        sa.event.listen(
            self.engine,
            "connect",
            lambda connection, _: connection.create_function(
                "btrim", 1, lambda value: value.strip() if value is not None else None
            ),
        )
        self.conn = self.engine.connect()
        self.base = importlib.import_module("app.db.migrations.versions.20260816_02_add_etf_daily_bars")
        self.migration = importlib.import_module("app.db.migrations.versions.20260830_03_add_etf_daily_revision_audit")
        self.ctx = MigrationContext.configure(self.conn)
        self.op = Operations(self.ctx)
        # Migration modules use Alembic's module-level ``op`` proxy.  Bind
        # that proxy to the connection context while invoking each revision,
        # matching the runtime migration environment used in production.
        with Operations.context(self.ctx):
            self.base.upgrade()
            self.migration.upgrade()

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def test_columns_constraints_indexes_and_nullable_legacy_row(self):
        insp = sa.inspect(self.conn)
        bars = {c["name"]: c for c in insp.get_columns("etf_daily_bars")}
        self.assertTrue(bars["source_revision"]["nullable"])
        audits = {c["name"]: c for c in insp.get_columns("etf_daily_bar_revision_audits")}
        for name in ("source", "ts_code", "trade_date", "source_revision", "batch_revision", "accepted_at", "change_kind", "changed_fields"):
            self.assertFalse(audits[name]["nullable"])
        uniques = [u["column_names"] for u in insp.get_unique_constraints("etf_daily_bar_revision_audits")]
        self.assertIn(["source", "ts_code", "trade_date", "source_revision"], uniques)
        indexes = {tuple(i["column_names"]) for i in insp.get_indexes("etf_daily_bar_revision_audits")}
        self.assertTrue({("source", "trade_date"), ("source", "ts_code", "trade_date", "accepted_at"), ("batch_revision",)} <= indexes)

    def test_existing_rows_keep_null_revision_and_round_trip(self):
        self.conn.execute(sa.text("INSERT INTO etf_daily_bars (source,ts_code,trade_date,open,high,low,close,vol,amount) VALUES ('x','a',:d,1,1,1,1,1,1)"), {"d": date(2024, 1, 1)})
        self.assertIsNone(self.conn.execute(sa.text("SELECT source_revision FROM etf_daily_bars")).scalar())
        with Operations.context(self.ctx):
            self.migration.downgrade()
        self.assertNotIn("etf_daily_bar_revision_audits", sa.inspect(self.conn).get_table_names())
        self.assertNotIn("source_revision", {c["name"] for c in sa.inspect(self.conn).get_columns("etf_daily_bars")})


if __name__ == "__main__":
    unittest.main()
