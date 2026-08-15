"""Add trading calendar days.

Revision ID: 20260815_02
Revises: 20260815_01
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260815_02"
down_revision: str | None = "20260815_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trading_calendar_days",
        sa.Column("exchange", sa.String(length=16), nullable=False, comment="Tushare exchange code."),
        sa.Column("calendar_date", sa.Date(), nullable=False, comment="Calendar date for the exchange."),
        sa.Column("is_open", sa.Boolean(), nullable=False, comment="Whether the exchange is open for trading."),
        sa.Column("previous_trading_date", sa.Date(), nullable=True, comment="Most recent prior trading date supplied by Tushare."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the calendar day was first stored."),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the calendar day was last synchronized."),
        sa.CheckConstraint("length(btrim(exchange)) > 0", name="exchange_not_blank"),
        sa.CheckConstraint("previous_trading_date IS NULL OR previous_trading_date < calendar_date", name="previous_date_before_calendar_date"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.PrimaryKeyConstraint("exchange", "calendar_date", name="pk_trading_calendar_days"),
        comment="Stores daily trading status independently for each exchange.",
    )
    op.create_index(
        "ix_trading_calendar_days_open_date",
        "trading_calendar_days",
        ["exchange", sa.text("calendar_date DESC")],
        postgresql_where=sa.text("is_open"),
    )


def downgrade() -> None:
    op.drop_table("trading_calendar_days")
