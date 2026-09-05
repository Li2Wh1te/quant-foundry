"""Close the PIT, revision, validity, and coverage evidence loop."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260906_01"
down_revision: str | None = "20260905_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    """Use JSONB in PostgreSQL and portable JSON in SQLite tests."""

    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _has_column(bind, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def _set_not_null(table: str, columns: tuple[str, ...]) -> None:
    """Align backfilled metadata nullability with the ORM contract."""

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table, recreate="always") as batch:
            for name in columns:
                batch.alter_column(name, existing_nullable=True, nullable=False)
    else:
        for name in columns:
            op.alter_column(table, name, existing_nullable=True, nullable=False)


def _add_columns(table: str, columns: tuple[sa.Column, ...]) -> None:
    """Add only missing columns so local upgrade retries stay idempotent."""

    bind = op.get_bind()
    missing = tuple(column for column in columns if not _has_column(bind, table, column.name))
    if not missing:
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table, recreate="always") as batch:
            for column in missing:
                batch.add_column(column)
    else:
        for column in missing:
            op.add_column(table, column)


def upgrade() -> None:
    """Add explicit source acceptance and effective-window metadata."""

    _add_columns(
        "trading_status_facts",
        (
            sa.Column("instrument_id", sa.Uuid(), nullable=True),
            sa.Column("dimension", sa.String(32), nullable=True),
            sa.Column("valid_from", sa.Date(), nullable=True),
            sa.Column("valid_to", sa.Date(), nullable=True),
            sa.Column("source_revision", sa.String(128), nullable=True),
            sa.Column("quality_status", sa.String(16), nullable=True),
            sa.Column("known_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                "UPDATE trading_status_facts "
                "SET dimension = COALESCE(dimension, 'suspension'), "
                "valid_from = COALESCE(valid_from, trade_date), "
                "valid_to = COALESCE(valid_to, date(trade_date, '+1 day')), "
                "quality_status = COALESCE(quality_status, 'complete')"
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE trading_status_facts "
                "SET dimension = COALESCE(dimension, 'suspension'), "
                "valid_from = COALESCE(valid_from, trade_date), "
                "valid_to = COALESCE(valid_to, trade_date + 1), "
                "quality_status = COALESCE(quality_status, 'complete')"
            )
        )
    _set_not_null("trading_status_facts", ("dimension", "quality_status"))
    op.create_index(
        "ix_trading_status_pit_lookup",
        "trading_status_facts",
        ["ts_code", "trade_date", "known_at", "observed_at"],
        if_not_exists=True,
    )

    _add_columns(
        "corporate_action_source_facts",
        (sa.Column("source_revision", sa.String(128), nullable=True),),
    )
    _add_columns(
        "corporate_action_facts",
        (
            sa.Column("source_revision", sa.String(128), nullable=True),
            sa.Column("valid_from", sa.Date(), nullable=True),
            sa.Column("valid_to", sa.Date(), nullable=True),
            sa.Column("effective_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("known_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )
    _add_columns(
        "corporate_action_coverage_facts",
        (
            sa.Column("source", sa.String(32), nullable=True),
            sa.Column("source_revision", sa.String(128), nullable=True),
            sa.Column("known_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )
    if bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                "UPDATE corporate_action_facts "
                "SET valid_from = COALESCE(valid_from, ex_date), "
                "valid_to = COALESCE(valid_to, date(ex_date, '+1 day'))"
            )
        )
        bind.execute(
            sa.text(
                "UPDATE corporate_action_coverage_facts "
                "SET source = COALESCE(source, 'tushare'), "
                "observed_at = COALESCE(observed_at, computed_at)"
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE corporate_action_facts "
                "SET valid_from = COALESCE(valid_from, ex_date), "
                "valid_to = COALESCE(valid_to, ex_date + 1)"
            )
        )
        op.execute(
            sa.text(
                "UPDATE corporate_action_coverage_facts "
                "SET source = COALESCE(source, 'tushare'), "
                "observed_at = COALESCE(observed_at, computed_at)"
            )
        )
    _set_not_null("corporate_action_coverage_facts", ("source", "observed_at"))

    op.create_table(
        "trading_status_source_facts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(64), nullable=False),
        sa.Column("query_kind", sa.String(32), nullable=True),
        sa.Column("query_value", sa.String(128), nullable=True),
        sa.Column("payload", _json_type(), nullable=False),
        sa.Column("source_hash", sa.String(128), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source", "endpoint", "query_kind", "query_value", "source_hash",
            name="uq_trading_status_source_snapshot",
        ),
    )
    op.create_index(
        "ix_trading_status_source_code_observed",
        "trading_status_source_facts",
        ["source", "observed_at"],
    )

    op.create_table(
        "trading_status_coverage_facts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=True),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", _json_type(), nullable=False),
        sa.Column("validation_rule", sa.String(64), nullable=True),
        sa.Column("summary", _json_type(), nullable=False),
        sa.CheckConstraint(
            "end_date >= start_date",
            name="trading_status_coverage_date_ordered",
        ),
        sa.CheckConstraint(
            "event_count >= 0",
            name="trading_status_coverage_event_count_non_negative",
        ),
    )
    op.create_index(
        "ix_trading_status_coverage_lookup",
        "trading_status_coverage_facts",
        ["instrument_id", "dimension", "start_date", "end_date", "known_at"],
    )

    op.create_table(
        "trading_status_fact_revision_audits",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("ts_code", sa.String(32), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("previous_instrument_id", sa.Uuid(), nullable=True),
        sa.Column("previous_dimension", sa.String(32), nullable=False),
        sa.Column("previous_status", sa.String(16), nullable=False),
        sa.Column("previous_valid_from", sa.Date(), nullable=True),
        sa.Column("previous_valid_to", sa.Date(), nullable=True),
        sa.Column("previous_source", sa.String(32), nullable=False),
        sa.Column("previous_quality_status", sa.String(16), nullable=False),
        sa.Column("previous_known_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_raw", _json_type(), nullable=False),
        sa.Column("previous_source_revision", sa.String(128), nullable=True),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("change_kind", sa.String(24), nullable=False),
        sa.Column("changed_fields", _json_type(), nullable=False),
        sa.CheckConstraint(
            "change_kind IN ('correction', 'metadata_backfill')",
            name="ck_trading_status_revision_audit_change_kind",
        ),
        sa.UniqueConstraint(
            "ts_code", "trade_date", "source_revision",
            name="uq_trading_status_revision_audit_identity",
        ),
    )
    op.create_index(
        "ix_trading_status_revision_audit_lookup",
        "trading_status_fact_revision_audits",
        ["ts_code", "trade_date", "accepted_at"],
    )


def downgrade() -> None:
    """Drop the new evidence tables and metadata columns."""

    op.drop_index(
        "ix_trading_status_revision_audit_lookup",
        table_name="trading_status_fact_revision_audits",
    )
    op.drop_table("trading_status_fact_revision_audits")
    op.drop_index(
        "ix_trading_status_coverage_lookup",
        table_name="trading_status_coverage_facts",
    )
    op.drop_table("trading_status_coverage_facts")
    op.drop_index(
        "ix_trading_status_source_code_observed",
        table_name="trading_status_source_facts",
    )
    op.drop_table("trading_status_source_facts")
    op.drop_index("ix_trading_status_pit_lookup", table_name="trading_status_facts")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("corporate_action_coverage_facts", recreate="always") as batch:
            for name in ("observed_at", "known_at", "source_revision", "source"):
                if _has_column(bind, "corporate_action_coverage_facts", name):
                    batch.drop_column(name)
        with op.batch_alter_table("corporate_action_facts", recreate="always") as batch:
            for name in ("observed_at", "known_at", "effective_time", "valid_to", "valid_from", "source_revision"):
                if _has_column(bind, "corporate_action_facts", name):
                    batch.drop_column(name)
        with op.batch_alter_table("corporate_action_source_facts", recreate="always") as batch:
            if _has_column(bind, "corporate_action_source_facts", "source_revision"):
                batch.drop_column("source_revision")
        with op.batch_alter_table("trading_status_facts", recreate="always") as batch:
            for name in ("known_at", "quality_status", "source_revision", "valid_to", "valid_from", "dimension", "instrument_id"):
                if _has_column(bind, "trading_status_facts", name):
                    batch.drop_column(name)
    else:
        for table, names in (
            ("corporate_action_coverage_facts", ("observed_at", "known_at", "source_revision", "source")),
            ("corporate_action_facts", ("observed_at", "known_at", "effective_time", "valid_to", "valid_from", "source_revision")),
            ("corporate_action_source_facts", ("source_revision",)),
            ("trading_status_facts", ("known_at", "quality_status", "source_revision", "valid_to", "valid_from", "dimension", "instrument_id")),
        ):
            for name in names:
                if _has_column(bind, table, name):
                    op.drop_column(table, name)
