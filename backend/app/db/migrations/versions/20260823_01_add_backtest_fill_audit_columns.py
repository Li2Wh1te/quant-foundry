"""Add execution audit columns to backtest_fills.

Adds the fill-level audit contract required by task package 04-04:
settlement currency, contract multiplier, gross notional, frozen fee
breakdown, and deferred-settlement due-session/boundary identifiers.
All new columns are nullable (or server-defaulted) so existing rows
remain valid without backfill.

Revision ID: 20260823_01
Revises: 20260822_04
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260823_01"
down_revision: str | None = "20260822_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(38, 18)


def upgrade() -> None:
    op.add_column(
        "backtest_fills",
        sa.Column(
            "currency",
            sa.String(length=8),
            nullable=False,
            server_default="CNY",
            comment="Settlement currency of the fill.",
        ),
    )
    op.add_column(
        "backtest_fills",
        sa.Column(
            "contract_multiplier",
            AMOUNT,
            nullable=False,
            server_default="1",
            comment=(
                "Contract multiplier resolved from the frozen rule snapshot."
            ),
        ),
    )
    op.add_column(
        "backtest_fills",
        sa.Column(
            "gross_notional",
            AMOUNT,
            nullable=True,
            comment=(
                "execution_price x quantity x contract_multiplier before fees."
            ),
        ),
    )
    op.add_column(
        "backtest_fills",
        sa.Column(
            "fee_breakdown",
            sa.JSON().with_variant(
                sa.dialects.postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
            comment=(
                "Frozen fee components with schedule key/version and "
                "rounding contract."
            ),
        ),
    )
    op.add_column(
        "backtest_fills",
        sa.Column(
            "settlement_calendar_id",
            sa.String(length=100),
            nullable=True,
            comment="Calendar that owns the deferred settlement of a buy fill.",
        ),
    )
    op.add_column(
        "backtest_fills",
        sa.Column(
            "settlement_due_session",
            sa.Date(),
            nullable=True,
            comment="Session whose pre-match boundary releases this buy fill.",
        ),
    )
    op.add_column(
        "backtest_fills",
        sa.Column(
            "settlement_boundary_id",
            sa.String(length=100),
            nullable=True,
            comment="Boundary identifier that released the due quantity.",
        ),
    )


def downgrade() -> None:
    op.drop_column("backtest_fills", "settlement_boundary_id")
    op.drop_column("backtest_fills", "settlement_due_session")
    op.drop_column("backtest_fills", "settlement_calendar_id")
    op.drop_column("backtest_fills", "fee_breakdown")
    op.drop_column("backtest_fills", "gross_notional")
    op.drop_column("backtest_fills", "contract_multiplier")
    op.drop_column("backtest_fills", "currency")
