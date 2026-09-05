"""Add persisted scheduler run progress and supervision evidence."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_05"
down_revision: str | None = "20260830_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # progress belongs to an execution, never to the reusable task definition.
    op.execute("ALTER TABLE scheduled_tasks DROP CONSTRAINT IF EXISTS progress_range")
    op.drop_constraint("status", "task_runs", type_="check")
    op.drop_constraint("status_timestamps", "task_runs", type_="check")
    op.create_check_constraint(
        "status",
        "task_runs",
        "status IN ('queued', 'running', 'succeeded', 'failed', 'skipped', "
        "'interrupted', 'cancelled', 'timed_out', 'indeterminate')",
    )
    op.create_check_constraint(
        "status_timestamps",
        "task_runs",
        "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR "
        "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
        "(status IN ('succeeded', 'failed', 'cancelled', 'timed_out', 'indeterminate') "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR "
        "(status IN ('skipped', 'interrupted') AND finished_at IS NOT NULL)",
    )
    op.add_column("task_runs", sa.Column("current_trading_date", sa.String(10)))
    op.add_column("task_runs", sa.Column("current_step", sa.String(128)))
    op.add_column(
        "task_runs",
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_check_constraint("progress_range", "task_runs", "progress BETWEEN 0 AND 1")
    op.add_column("task_runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("task_runs", sa.Column("worker_id", sa.String(128)))
    op.add_column("task_runs", sa.Column("exit_code", sa.Integer()))
    op.add_column("task_runs", sa.Column("completion_marker", sa.String(128)))
    op.add_column("task_runs", sa.Column("failure_phase", sa.String(128)))
    op.add_column(
        "task_runs", sa.Column("cancellation_requested_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    for column in (
        "cancellation_requested_at",
        "failure_phase",
        "completion_marker",
        "exit_code",
        "worker_id",
        "last_heartbeat_at",
    ):
        op.drop_column("task_runs", column)
    op.drop_constraint("progress_range", "task_runs", type_="check")
    op.drop_column("task_runs", "progress")
    op.drop_column("task_runs", "current_step")
    op.drop_column("task_runs", "current_trading_date")
    op.drop_constraint("status_timestamps", "task_runs", type_="check")
    op.drop_constraint("status", "task_runs", type_="check")
    op.create_check_constraint(
        "status",
        "task_runs",
        "status IN ('queued', 'running', 'succeeded', 'failed', 'skipped', 'interrupted')",
    )
    op.create_check_constraint(
        "status_timestamps",
        "task_runs",
        "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR "
        "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
        "(status IN ('succeeded', 'failed') AND started_at IS NOT NULL AND "
        "finished_at IS NOT NULL) OR "
        "(status IN ('skipped', 'interrupted') AND finished_at IS NOT NULL)",
    )
