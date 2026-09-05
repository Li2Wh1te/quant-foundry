"""Add the explicit exchange attribute to PIT identity facts.

The field is intentionally nullable for existing rows.  Historical exchange
facts cannot be inferred safely from a current code catalogue; formal ETF
resolution blocks rows that remain incomplete until an evidenced revision is
appended.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_01"
down_revision: str | None = "20260829_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable exchange without rewriting existing identity facts."""

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("instrument_identity_facts", recreate="always") as batch:
            batch.add_column(sa.Column("exchange", sa.String(length=32), nullable=True))
        return
    op.add_column(
        "instrument_identity_facts",
        sa.Column("exchange", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    """Remove only the exchange column introduced by this revision."""

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("instrument_identity_facts", recreate="always") as batch:
            batch.drop_column("exchange")
        return
    op.drop_column("instrument_identity_facts", "exchange")
