"""Persist bounded failure, stdout, and force-termination evidence."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260904_01"
down_revision: str | None = "20260903_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = postgresql.JSONB(astext_type=sa.Text())


def _has_column(bind, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Add bounded diagnostic payloads without changing run ownership."""

    bind = op.get_bind()
    if not _has_column(bind, "backtest_runs", "forced_termination"):
        op.add_column(
            "backtest_runs",
            sa.Column(
                "forced_termination",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if not _has_column(bind, "backtest_runs", "failure_evidence"):
        op.add_column(
            "backtest_runs",
            sa.Column("failure_evidence", JSON_TYPE, nullable=True),
        )
    if not _has_column(bind, "backtest_runs", "stdout_evidence"):
        op.add_column(
            "backtest_runs",
            sa.Column("stdout_evidence", JSON_TYPE, nullable=True),
        )


def downgrade() -> None:
    """Remove only the three additive diagnostic columns/payloads."""

    bind = op.get_bind()
    if _has_column(bind, "backtest_runs", "stdout_evidence"):
        op.drop_column("backtest_runs", "stdout_evidence")
    if _has_column(bind, "backtest_runs", "failure_evidence"):
        op.drop_column("backtest_runs", "failure_evidence")
    if _has_column(bind, "backtest_runs", "forced_termination"):
        op.drop_column("backtest_runs", "forced_termination")
