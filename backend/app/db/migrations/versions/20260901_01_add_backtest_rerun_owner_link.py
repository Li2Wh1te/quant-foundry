"""Persist the owner-scoped parent link for manual backtest reruns."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_01"
down_revision: str | None = "20260831_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def _has_index(bind, table: str, name: str) -> bool:
    return any(item["name"] == name for item in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "backtest_runs", "idempotency_request_hash"):
        op.add_column(
            "backtest_runs",
            sa.Column("idempotency_request_hash", sa.String(64), nullable=True),
        )
    if not _has_column(bind, "backtest_runs", "rerun_of_run_id"):
        op.add_column(
            "backtest_runs",
            sa.Column("rerun_of_run_id", sa.Uuid(), nullable=True),
        )
    if not _has_index(bind, "backtest_runs", "ix_backtest_runs_rerun_parent"):
        op.create_index(
            "ix_backtest_runs_rerun_parent",
            "backtest_runs",
            ["rerun_of_run_id"],
        )
    inspector = sa.inspect(bind)
    foreign_keys = inspector.get_foreign_keys("backtest_runs")
    if not any(
        fk.get("name") == "fk_backtest_runs_rerun_parent"
        for fk in foreign_keys
    ) and bind.dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_backtest_runs_rerun_parent",
            "backtest_runs",
            "backtest_runs",
            ["rerun_of_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        foreign_keys = sa.inspect(bind).get_foreign_keys("backtest_runs")
        if any(
            fk.get("name") == "fk_backtest_runs_rerun_parent"
            for fk in foreign_keys
        ):
            op.drop_constraint(
                "fk_backtest_runs_rerun_parent",
                "backtest_runs",
                type_="foreignkey",
            )
    if _has_index(bind, "backtest_runs", "ix_backtest_runs_rerun_parent"):
        op.drop_index("ix_backtest_runs_rerun_parent", table_name="backtest_runs")
    if _has_column(bind, "backtest_runs", "rerun_of_run_id"):
        op.drop_column("backtest_runs", "rerun_of_run_id")
    if _has_column(bind, "backtest_runs", "idempotency_request_hash"):
        op.drop_column("backtest_runs", "idempotency_request_hash")
