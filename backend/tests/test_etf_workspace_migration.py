"""Upgrade/downgrade preserves original daily evidence and unknown facts."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_legacy_prices_survive_upgrade_and_downgrade():
    engine = create_engine("sqlite://")
    migration = importlib.import_module("app.db.migrations.versions.20260914_01_etf_workspace")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE etf_daily_bars (ts_code TEXT PRIMARY KEY, close NUMERIC, source_revision TEXT)"))
        connection.execute(text("INSERT INTO etf_daily_bars VALUES ('510300.SH', 1.049, 'original')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            row = connection.execute(text("SELECT close, source_revision, pre_close, change, pct_chg FROM etf_daily_bars")).one()
            assert tuple(row) == (1.049, "original", None, None, None)
            assert "etf_watchlist_entries" in inspect(connection).get_table_names()
            migration.downgrade()
            assert "etf_watchlist_entries" not in inspect(connection).get_table_names()
            assert tuple(connection.execute(text("SELECT close, source_revision FROM etf_daily_bars")).one()) == (1.049, "original")
    engine.dispose()
