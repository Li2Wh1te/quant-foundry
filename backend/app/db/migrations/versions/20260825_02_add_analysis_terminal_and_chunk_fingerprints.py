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
    """Remove retry columns only when doing so cannot discard evidence."""

    bind = op.get_bind()
    evidence_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM backtest_analysis_summaries "
            "WHERE terminal_fingerprint IS NOT NULL "
            "OR last_chunk_token IS NOT NULL"
        )
    ).scalar_one()
    if evidence_count:
        raise RuntimeError(
            "refusing downgrade 20260825_02: analysis retry fingerprint "
            "evidence exists"
        )

    op.drop_column("backtest_analysis_summaries", "terminal_fingerprint")
    op.drop_column("backtest_analysis_summaries", "last_chunk_token")
