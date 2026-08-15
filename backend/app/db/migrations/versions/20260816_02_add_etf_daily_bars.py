"""Add current authoritative ETF daily bars.

Revision ID: 20260816_02
Revises: 20260815_03
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260816_02"
down_revision: str | None = "20260815_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "etf_daily_bars",
        sa.Column("source", sa.String(length=32), nullable=False, comment="Source system that produced the daily bar."),
        sa.Column("ts_code", sa.String(length=16), nullable=False, comment="Source-specific ETF trading code."),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="Trading date represented by the bar."),
        sa.Column("open", sa.Numeric(precision=20, scale=6), nullable=False, comment="Opening price in yuan."),
        sa.Column("high", sa.Numeric(precision=20, scale=6), nullable=False, comment="Highest price in yuan."),
        sa.Column("low", sa.Numeric(precision=20, scale=6), nullable=False, comment="Lowest price in yuan."),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False, comment="Closing price in yuan."),
        sa.Column("vol", sa.Numeric(precision=24, scale=4), nullable=False, comment="Trading volume in lots."),
        sa.Column("amount", sa.Numeric(precision=24, scale=4), nullable=False, comment="Trading amount in thousand yuan."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the bar was first stored."),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which a source correction last changed the bar."),
        sa.CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(btrim(ts_code)) > 0", name="ts_code_not_blank"),
        sa.CheckConstraint("open >= 0", name="open_not_negative"),
        sa.CheckConstraint("high >= 0", name="high_not_negative"),
        sa.CheckConstraint("low >= 0", name="low_not_negative"),
        sa.CheckConstraint("close >= 0", name="close_not_negative"),
        sa.CheckConstraint("vol >= 0", name="volume_not_negative"),
        sa.CheckConstraint("amount >= 0", name="amount_not_negative"),
        sa.CheckConstraint("high >= low", name="high_not_below_low"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.PrimaryKeyConstraint("source", "ts_code", "trade_date", name="pk_etf_daily_bars"),
        comment="Stores the latest authoritative source value for each ETF trading day.",
    )
    op.create_index(
        "ix_etf_daily_bars_trade_date_code",
        "etf_daily_bars",
        ["trade_date", "source", "ts_code"],
    )


def downgrade() -> None:
    op.drop_table("etf_daily_bars")
