"""Preserve the worker's 28-digit Decimal progress in persisted runs."""

from alembic import op
import sqlalchemy as sa

revision = "20260909_01"
down_revision = "20260908_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rounded observations remain valid historical evidence. Do not
    # invent missing digits; subsequent worker writes retain full precision.
    with op.batch_alter_table("backtest_runs") as batch:
        batch.alter_column("progress", existing_type=sa.Numeric(6, 5),
                           type_=sa.Numeric(), existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("backtest_runs") as batch:
        batch.alter_column("progress", existing_type=sa.Numeric(),
                           type_=sa.Numeric(6, 5), existing_nullable=False)
