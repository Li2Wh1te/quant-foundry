from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint(
            "length(btrim(task_type)) > 0", name="task_type_not_blank"
        ),
        CheckConstraint(
            "jsonb_typeof(parameters) = 'object'", name="parameters_object"
        ),
        CheckConstraint("parameter_version > 0", name="parameter_version_positive"),
        CheckConstraint("jsonb_typeof(schedule) = 'object'", name="schedule_object"),
        CheckConstraint(
            "schedule ? 'type' AND schedule ->> 'type' IN "
            "('cron', 'interval', 'once')",
            name="schedule_type",
        ),
        CheckConstraint(
            "state IN ('active', 'paused', 'completed', 'archived')",
            name="state",
        ),
        CheckConstraint(
            "state <> 'completed' OR schedule ->> 'type' = 'once'",
            name="completed_only_once",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "concurrency_limit BETWEEN 1 AND 32", name="concurrency_limit"
        ),
        CheckConstraint(
            "overlap_policy IN ('skip', 'queue')", name="overlap_policy"
        ),
        CheckConstraint("queue_limit BETWEEN 1 AND 10000", name="queue_limit"),
        CheckConstraint("priority BETWEEN -100 AND 100", name="priority"),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        Index("ix_scheduled_tasks_state", "state"),
        Index("ix_scheduled_tasks_task_type", "task_type"),
        Index("ix_scheduled_tasks_updated_at", text("updated_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    parameter_version: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default="1"
    )
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    concurrency_limit: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default="1"
    )
    overlap_policy: Mapped[str] = mapped_column(
        String(16), default="skip", server_default="skip"
    )
    queue_limit: Mapped[int] = mapped_column(default=1, server_default="1")
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        CheckConstraint("task_version > 0", name="task_version_positive"),
        CheckConstraint(
            "length(btrim(task_type)) > 0", name="task_type_not_blank"
        ),
        CheckConstraint(
            "trigger_type IN ('scheduled', 'manual')", name="trigger_type"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'skipped', 'interrupted')",
            name="status",
        ),
        CheckConstraint(
            "jsonb_typeof(parameters) = 'object'", name="parameters_object"
        ),
        CheckConstraint("parameter_version > 0", name="parameter_version_positive"),
        CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name="result_object",
        ),
        CheckConstraint("priority BETWEEN -100 AND 100", name="priority"),
        CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="started_after_created",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at",
            name="finished_after_created",
        ),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
        CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status IN ('skipped', 'interrupted') AND finished_at IS NOT NULL)",
            name="status_timestamps",
        ),
        Index("ix_task_runs_task_id_created_at", "task_id", text("created_at DESC")),
        Index("ix_task_runs_status_created_at", "status", text("created_at DESC")),
        Index("ix_task_runs_created_at", text("created_at DESC")),
        Index(
            "ix_task_runs_active",
            "task_id",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_task_runs_dispatch",
            text("priority DESC"),
            "available_at",
            "created_at",
            postgresql_where=text("status = 'queued'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("scheduled_tasks.id", ondelete="RESTRICT")
    )
    task_version: Mapped[int]
    task_type: Mapped[str] = mapped_column(String(64))
    trigger_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued"
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parameter_version: Mapped[int] = mapped_column(SmallInteger)
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_type: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
