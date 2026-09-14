"""Persist owned backtest comparison definitions and ordered run membership."""
from alembic import op
import sqlalchemy as sa
revision = "20260917_01"
down_revision = "20260916_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("backtest_saved_comparisons",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_scope", sa.String(128), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("baseline_run_id", sa.Uuid(), sa.ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint("version > 0", name="version_positive"),
    )
    op.create_index("ix_backtest_saved_comparisons_owner_updated", "backtest_saved_comparisons", ["owner_scope", "updated_at"])
    op.create_table("backtest_saved_comparison_members",
        sa.Column("comparison_id", sa.Uuid(), sa.ForeignKey("backtest_saved_comparisons.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("backtest_runs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("comparison_id", "position"),
        sa.CheckConstraint("position >= 0 AND position < 10", name="position_range"),
    )


def downgrade():
    op.drop_table("backtest_saved_comparison_members")
    op.drop_table("backtest_saved_comparisons")
