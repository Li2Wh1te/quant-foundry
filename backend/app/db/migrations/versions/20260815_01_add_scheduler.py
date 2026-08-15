"""Add persistent scheduler tables.

Revision ID: 20260815_01
Revises:
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260815_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Application-generated task UUID."),
        sa.Column("name", sa.String(length=100), nullable=False, comment="Human-readable task name."),
        sa.Column("description", sa.Text(), nullable=True, comment="Optional task purpose and behavior."),
        sa.Column("task_type", sa.String(length=64), nullable=False, comment="Stable key from the backend task registry."),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="Current validated custom task parameters.",
        ),
        sa.Column("parameter_version", sa.SmallInteger(), server_default="1", nullable=False, comment="Version of the task parameter schema."),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="Validated cron, interval, or once schedule."),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False, comment="Task lifecycle state."),
        sa.Column("concurrency_limit", sa.SmallInteger(), server_default="1", nullable=False, comment="Maximum concurrent runs for this task."),
        sa.Column("overlap_policy", sa.String(length=16), server_default="skip", nullable=False, comment="Whether overlapping triggers are skipped or queued."),
        sa.Column("queue_limit", sa.Integer(), server_default="1", nullable=False, comment="Maximum queued runs for this task."),
        sa.Column("priority", sa.SmallInteger(), server_default="0", nullable=False, comment="Dispatch priority; higher values run first."),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False, comment="Optimistic locking version."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Task creation timestamp."),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Latest task update timestamp."),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint("length(btrim(task_type)) > 0", name="task_type_not_blank"),
        sa.CheckConstraint("jsonb_typeof(parameters) = 'object'", name="parameters_object"),
        sa.CheckConstraint("parameter_version > 0", name="parameter_version_positive"),
        sa.CheckConstraint("jsonb_typeof(schedule) = 'object'", name="schedule_object"),
        sa.CheckConstraint("schedule ? 'type' AND schedule ->> 'type' IN ('cron', 'interval', 'once')", name="schedule_type"),
        sa.CheckConstraint("state IN ('active', 'paused', 'completed', 'archived')", name="state"),
        sa.CheckConstraint("state <> 'completed' OR schedule ->> 'type' = 'once'", name="completed_only_once"),
        sa.CheckConstraint("concurrency_limit BETWEEN 1 AND 32", name="concurrency_limit"),
        sa.CheckConstraint("overlap_policy IN ('skip', 'queue')", name="overlap_policy"),
        sa.CheckConstraint("queue_limit BETWEEN 1 AND 10000", name="queue_limit"),
        sa.CheckConstraint("priority BETWEEN -100 AND 100", name="priority"),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.PrimaryKeyConstraint("id", name="pk_scheduled_tasks"),
        comment="Stores persistent task definitions managed by the scheduler API.",
    )
    op.create_index("ix_scheduled_tasks_state", "scheduled_tasks", ["state"])
    op.create_index("ix_scheduled_tasks_task_type", "scheduled_tasks", ["task_type"])
    op.create_index("ix_scheduled_tasks_updated_at", "scheduled_tasks", [sa.text("updated_at DESC")])

    op.create_table(
        "task_runs",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Application-generated run UUID."),
        sa.Column("task_id", sa.Uuid(), nullable=False, comment="Task definition that produced this run."),
        sa.Column("task_version", sa.Integer(), nullable=False, comment="Task definition version used by this run."),
        sa.Column("task_type", sa.String(length=64), nullable=False, comment="Task handler key used by this run."),
        sa.Column("trigger_type", sa.String(length=16), nullable=False, comment="Scheduled or manual trigger origin."),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False, comment="Current execution lifecycle state."),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="Validated parameter snapshot for this run."),
        sa.Column("parameter_version", sa.SmallInteger(), nullable=False, comment="Parameter schema version used by this run."),
        sa.Column("priority", sa.SmallInteger(), server_default="0", nullable=False, comment="Priority snapshot used by the dispatcher."),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="Optional structured result summary."),
        sa.Column("error_type", sa.String(length=255), nullable=True, comment="Stable exception or error category."),
        sa.Column("error_message", sa.Text(), nullable=True, comment="Sanitized failure or skip description."),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True, comment="Time at which the scheduler handed off this run."),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Earliest time at which this run may be dispatched."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which this run entered the queue."),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True, comment="Time at which handler execution started."),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="Time at which execution reached a terminal state."),
        sa.CheckConstraint("task_version > 0", name="task_version_positive"),
        sa.CheckConstraint("length(btrim(task_type)) > 0", name="task_type_not_blank"),
        sa.CheckConstraint("trigger_type IN ('scheduled', 'manual')", name="trigger_type"),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'skipped', 'interrupted')", name="status"),
        sa.CheckConstraint("jsonb_typeof(parameters) = 'object'", name="parameters_object"),
        sa.CheckConstraint("parameter_version > 0", name="parameter_version_positive"),
        sa.CheckConstraint("result IS NULL OR jsonb_typeof(result) = 'object'", name="result_object"),
        sa.CheckConstraint("priority BETWEEN -100 AND 100", name="priority"),
        sa.CheckConstraint("started_at IS NULL OR started_at >= created_at", name="started_after_created"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= created_at", name="finished_after_created"),
        sa.CheckConstraint("finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at", name="finished_after_started"),
        sa.CheckConstraint("(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR (status IN ('succeeded', 'failed') AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR (status IN ('skipped', 'interrupted') AND finished_at IS NOT NULL)", name="status_timestamps"),
        sa.ForeignKeyConstraint(["task_id"], ["scheduled_tasks.id"], name="fk_task_runs_task_id_scheduled_tasks", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_task_runs"),
        comment="Stores persistent queue state and task execution history.",
    )
    op.create_index("ix_task_runs_task_id_created_at", "task_runs", ["task_id", sa.text("created_at DESC")])
    op.create_index("ix_task_runs_status_created_at", "task_runs", ["status", sa.text("created_at DESC")])
    op.create_index("ix_task_runs_created_at", "task_runs", [sa.text("created_at DESC")])
    op.create_index("ix_task_runs_active", "task_runs", ["task_id"], postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_index("ix_task_runs_dispatch", "task_runs", [sa.text("priority DESC"), "available_at", "created_at"], postgresql_where=sa.text("status = 'queued'"))


def downgrade() -> None:
    op.drop_table("task_runs")
    op.drop_table("scheduled_tasks")
