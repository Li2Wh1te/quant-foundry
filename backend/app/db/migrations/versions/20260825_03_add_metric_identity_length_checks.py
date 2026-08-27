"""Add database length checks for the frozen analyzer identity contract.

Revision ID: 20260825_03
Revises: 20260825_02
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260825_03"
down_revision: str | None = "20260825_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Reject values that cannot be represented by the analyzer contract."""

    op.create_check_constraint(
        "metric_key_length",
        "backtest_metrics",
        "length(metric_key) BETWEEN 1 AND 100",
    )
    op.create_check_constraint(
        "formula_version_length",
        "backtest_metrics",
        "length(formula_version) BETWEEN 1 AND 64",
    )
    op.create_check_constraint(
        "metric_unit_length",
        "backtest_metrics",
        "unit IS NULL OR length(unit) BETWEEN 1 AND 32",
    )
    op.create_check_constraint(
        "analyzer_key_length",
        "backtest_metrics",
        "analyzer_key IS NULL OR length(analyzer_key) BETWEEN 1 AND 100",
    )


def downgrade() -> None:
    """Remove analyzer identity length checks."""

    op.drop_constraint("analyzer_key_length", "backtest_metrics", type_="check")
    op.drop_constraint("metric_unit_length", "backtest_metrics", type_="check")
    op.drop_constraint("formula_version_length", "backtest_metrics", type_="check")
    op.drop_constraint("metric_key_length", "backtest_metrics", type_="check")
