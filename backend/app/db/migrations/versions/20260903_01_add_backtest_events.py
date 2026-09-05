"""Persist the ordered domain-event audit stream for backtest runs."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260903_01"
down_revision: str | None = "20260902_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Create the append-only event table used by result auditing."""

    op.create_table(
        "backtest_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("step_sequence", sa.Integer(), nullable=False),
        sa.Column("phase_sequence", sa.Integer(), nullable=False),
        sa.Column("phase_key", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_backtest_events_run_sequence",
        "backtest_events",
        ["run_id", "event_sequence"],
        unique=True,
    )
    op.create_index(
        "ix_backtest_events_run_sort_key",
        "backtest_events",
        ["run_id", "event_sequence"],
    )


def downgrade() -> None:
    """Remove the event audit table."""

    op.drop_index("ix_backtest_events_run_sort_key", table_name="backtest_events")
    op.drop_index("uq_backtest_events_run_sequence", table_name="backtest_events")
    op.drop_table("backtest_events")
