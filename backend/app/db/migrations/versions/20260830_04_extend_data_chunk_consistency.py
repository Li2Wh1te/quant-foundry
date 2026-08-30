"""Extend data chunk rows with consistency evidence."""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260830_04"
down_revision = "20260830_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _json_type():
    return postgresql.JSONB(astext_type=sa.Text()) if op.get_bind().dialect.name == "postgresql" else sa.JSON()

def upgrade() -> None:
    op.alter_column("backtest_data_chunks", "token_digest", existing_type=sa.String(length=128), nullable=True)
    op.add_column("backtest_data_chunks", sa.Column("consistency_mode", sa.String(length=40), nullable=False, server_default="chunked_logical_token"))
    op.add_column("backtest_data_chunks", sa.Column("coverage_summary", _json_type(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("backtest_data_chunks", sa.Column("failure_phase", sa.String(length=64), nullable=True))
    op.alter_column("backtest_data_chunks", "consistency_mode", server_default=None)
    op.alter_column("backtest_data_chunks", "coverage_summary", server_default=None)

def downgrade() -> None:
    op.drop_column("backtest_data_chunks", "failure_phase")
    op.drop_column("backtest_data_chunks", "coverage_summary")
    op.drop_column("backtest_data_chunks", "consistency_mode")
    op.alter_column("backtest_data_chunks", "token_digest", existing_type=sa.String(length=128), nullable=False)
