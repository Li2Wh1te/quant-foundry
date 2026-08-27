"""Persist the formal session timeline used at analysis admission."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260825_04"
down_revision: str | None = "20260825_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
JSON_TYPE = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Add the explicit immutable timeline payload to run summaries."""

    op.add_column(
        "backtest_analysis_summaries",
        sa.Column("formal_timeline", JSON_TYPE, nullable=True),
    )
    op.add_column(
        "backtest_analysis_summaries",
        sa.Column("candidate_return_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Remove the timeline payload column."""

    op.drop_column("backtest_analysis_summaries", "candidate_return_count")
    op.drop_column("backtest_analysis_summaries", "formal_timeline")
