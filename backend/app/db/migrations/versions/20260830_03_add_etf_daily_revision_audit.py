"""Add nullable current-state revisions and append-only audit evidence."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260830_03"
down_revision: str | None = "20260830_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    """Use JSONB on PostgreSQL while remaining portable to SQLite tests."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # Existing rows intentionally retain NULL: no provider revision can be
    # reconstructed safely from current values or timestamps.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("etf_daily_bars", recreate="always") as batch:
            batch.add_column(sa.Column("source_revision", sa.String(length=128), nullable=True))
    else:
        op.add_column("etf_daily_bars", sa.Column("source_revision", sa.String(length=128), nullable=True))

    op.create_table(
        "etf_daily_bar_revision_audits",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("ts_code", sa.String(length=16), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("previous_source_revision", sa.String(length=128), nullable=True),
        sa.Column("source_revision", sa.String(length=128), nullable=False),
        sa.Column("batch_revision", sa.String(length=128), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("change_kind", sa.String(length=24), nullable=False),
        sa.Column("changed_fields", _json_type(), nullable=False),
        sa.CheckConstraint(
            "change_kind IN ('correction', 'metadata_backfill')",
            name="ck_etf_revision_audit_change_kind",
        ),
        sa.UniqueConstraint(
            "source", "ts_code", "trade_date", "source_revision",
            name="uq_etf_revision_audit_identity",
        ),
    )
    op.create_index(
        "ix_etf_revision_audit_source_date",
        "etf_daily_bar_revision_audits", ["source", "trade_date"],
    )
    op.create_index(
        "ix_etf_revision_audit_source_code_date_accepted",
        "etf_daily_bar_revision_audits",
        ["source", "ts_code", "trade_date", "accepted_at"],
    )
    op.create_index(
        "ix_etf_revision_audit_batch_revision",
        "etf_daily_bar_revision_audits", ["batch_revision"],
    )


def downgrade() -> None:
    op.drop_index("ix_etf_revision_audit_batch_revision", table_name="etf_daily_bar_revision_audits")
    op.drop_index("ix_etf_revision_audit_source_code_date_accepted", table_name="etf_daily_bar_revision_audits")
    op.drop_index("ix_etf_revision_audit_source_date", table_name="etf_daily_bar_revision_audits")
    op.drop_table("etf_daily_bar_revision_audits")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("etf_daily_bars", recreate="always") as batch:
            batch.drop_column("source_revision")
    else:
        op.drop_column("etf_daily_bars", "source_revision")
