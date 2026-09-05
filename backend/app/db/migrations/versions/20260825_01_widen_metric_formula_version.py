"""Widen metric formula versions for analyzer contracts.

The turnover v1 formula identity is longer than the original 32-character
column contract.  Existing PostgreSQL deployments need an additive type
change; fresh installations also receive the widened type from the base
results migration.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260825_01"
down_revision: str | None = "20260824_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Increase the formula-version column to hold registered identities."""

    op.alter_column(
        "backtest_metrics",
        "formula_version",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Refuse narrowing while persisted formula identities exceed 32 chars."""

    connection = op.get_bind()
    count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM backtest_metrics "
            "WHERE LENGTH(formula_version) > 32"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"cannot narrow formula_version while {count} persisted row(s) "
            "exceed 32 characters"
        )
    op.alter_column(
        "backtest_metrics",
        "formula_version",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
