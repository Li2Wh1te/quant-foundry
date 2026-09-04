"""SQLite smoke test for the data-fact provenance migration."""

import importlib
from datetime import date

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_provenance_migration_backfills_validity_and_creates_evidence_tables():
    engine = sa.create_engine("sqlite://")
    sa.event.listen(
        engine,
        "connect",
        lambda connection, _: connection.create_function(
            "btrim", 1, lambda value: value.strip() if value is not None else None
        ),
    )
    connection = engine.connect()
    context = MigrationContext.configure(connection)
    migration = importlib.import_module(
        "app.db.migrations.versions.20260906_01_close_data_fact_provenance"
    )
    try:
        metadata = sa.MetaData()
        sa.Table(
            "trading_status_facts",
            metadata,
            sa.Column("ts_code", sa.String(32), primary_key=True),
            sa.Column("trade_date", sa.Date, primary_key=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("raw", sa.JSON, nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True)),
        )
        sa.Table(
            "corporate_action_source_facts",
            metadata,
            sa.Column("id", sa.Uuid, primary_key=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("endpoint", sa.String(64), nullable=False),
            sa.Column("query_kind", sa.String(32)),
            sa.Column("query_value", sa.String(32)),
            sa.Column("ts_code", sa.String(32), nullable=False),
            sa.Column("payload", sa.JSON, nullable=False),
            sa.Column("source_hash", sa.String(128), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True)),
        )
        sa.Table(
            "corporate_action_facts",
            metadata,
            sa.Column("event_id", sa.Uuid, primary_key=True),
            sa.Column("logical_fact_key", sa.String(256), nullable=False),
            sa.Column("fact_version", sa.Integer, nullable=False),
            sa.Column("instrument_id", sa.Uuid, nullable=False),
            sa.Column("action_type", sa.String(32), nullable=False),
            sa.Column("record_date", sa.Date),
            sa.Column("ex_date", sa.Date),
            sa.Column("cash_effective_date", sa.Date),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        sa.Table(
            "corporate_action_coverage_facts",
            metadata,
            sa.Column("id", sa.Uuid, primary_key=True),
            sa.Column("instrument_id", sa.Uuid, nullable=False),
            sa.Column("start_date", sa.Date, nullable=False),
            sa.Column("end_date", sa.Date, nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("event_count", sa.Integer),
            sa.Column("evidence", sa.JSON),
            sa.Column("validation_rule", sa.String(64)),
            sa.Column("summary", sa.JSON),
            sa.Column("computed_at", sa.DateTime(timezone=True)),
        )
        metadata.create_all(connection)
        connection.execute(
            sa.text(
                "INSERT INTO trading_status_facts "
                "(ts_code, trade_date, status, source, raw) "
                "VALUES ('510300.SH', :day, 'suspended', 'tushare', '{}')"
            ),
            {"day": date(2026, 8, 31)},
        )
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert {
            "trading_status_source_facts",
            "trading_status_coverage_facts",
            "trading_status_fact_revision_audits",
        }.issubset(inspector.get_table_names())
        status_columns = {
            item["name"] for item in inspector.get_columns("trading_status_facts")
        }
        status_metadata = {
            item["name"]: item for item in inspector.get_columns("trading_status_facts")
        }
        assert {
            "instrument_id",
            "dimension",
            "valid_from",
            "valid_to",
            "source_revision",
            "quality_status",
            "known_at",
        }.issubset(status_columns)
        assert status_metadata["dimension"]["nullable"] is False
        assert status_metadata["quality_status"]["nullable"] is False
        row = connection.execute(
            sa.text(
                "SELECT dimension, valid_from, valid_to, quality_status "
                "FROM trading_status_facts"
            )
        ).one()
        assert row == ("suspension", "2026-08-31", "2026-09-01", "complete")

        action_columns = {
            item["name"] for item in inspector.get_columns("corporate_action_facts")
        }
        assert {
            "source_revision",
            "valid_from",
            "valid_to",
            "effective_time",
            "known_at",
            "observed_at",
        }.issubset(action_columns)
        coverage_columns = {
            item["name"]
            for item in inspector.get_columns("corporate_action_coverage_facts")
        }
        assert {"source", "source_revision", "known_at", "observed_at"}.issubset(
            coverage_columns
        )
        audit_columns = {
            item["name"]
            for item in inspector.get_columns("trading_status_fact_revision_audits")
        }
        assert {"previous_status", "previous_raw", "previous_source_revision"}.issubset(
            audit_columns
        )
    finally:
        connection.close()
        engine.dispose()
