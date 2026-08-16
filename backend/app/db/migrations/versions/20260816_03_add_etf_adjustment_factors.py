"""Add current ETF adjustment factors.

Revision ID: 20260816_03
Revises: 20260816_02
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260816_03"
down_revision: str | None = "20260816_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "etf_adjustment_factors",
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            comment="Source system that produced the factor.",
        ),
        sa.Column(
            "ts_code",
            sa.String(length=16),
            nullable=False,
            comment="Source-specific ETF trading code.",
        ),
        sa.Column(
            "trade_date",
            sa.Date(),
            nullable=False,
            comment="Trading date represented by the factor.",
        ),
        sa.Column(
            "adj_factor",
            sa.Numeric(precision=24, scale=12),
            nullable=False,
            comment="Latest authoritative cumulative adjustment factor.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Time at which the factor was first stored.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Time at which a source correction last changed the factor.",
        ),
        sa.CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(btrim(ts_code)) > 0", name="ts_code_not_blank"),
        sa.CheckConstraint("adj_factor > 0", name="factor_positive"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.PrimaryKeyConstraint(
            "source", "ts_code", "trade_date", name="pk_etf_adjustment_factors"
        ),
        comment="Stores the latest authoritative source value for each ETF adjustment-factor date.",
    )
    op.create_index(
        "ix_etf_adjustment_factors_trade_date_code",
        "etf_adjustment_factors",
        ["trade_date", "source", "ts_code"],
    )


def downgrade() -> None:
    op.drop_table("etf_adjustment_factors")
