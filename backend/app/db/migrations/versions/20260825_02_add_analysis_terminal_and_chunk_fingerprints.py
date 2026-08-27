"""Add analysis chunk and terminal retry fingerprints.

Revision ID: 20260825_02
Revises: 20260825_01
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260825_02"
down_revision: str | None = "20260825_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist the latest accepted chunk token and terminal fingerprint."""

    op.add_column(
        "backtest_analysis_summaries",
        sa.Column("last_chunk_token", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "backtest_analysis_summaries",
        sa.Column("terminal_fingerprint", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    """Remove analysis retry fingerprints."""

    op.drop_column("backtest_analysis_summaries", "terminal_fingerprint")
    op.drop_column("backtest_analysis_summaries", "last_chunk_token")
