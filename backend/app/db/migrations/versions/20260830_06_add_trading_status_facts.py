"""Persist normalized suspend_d trading status facts."""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "20260830_06"
down_revision: str | None = "20260830_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

def upgrade() -> None:
    op.create_table(
        "trading_status_facts",
        sa.Column("ts_code", sa.String(32), primary_key=True),
        sa.Column("trade_date", sa.Date, primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("raw", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_trading_status_date", "trading_status_facts", ["trade_date", "status"])

def downgrade() -> None:
    op.drop_index("ix_trading_status_date", table_name="trading_status_facts")
    op.drop_table("trading_status_facts")
