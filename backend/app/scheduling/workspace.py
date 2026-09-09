"""Filtered task pages with complete live counts, independent of history limits."""

from typing import Literal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.data_sources.models import DataSourceConfig
from app.data_sources.service import configured
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskRegistry
from app.scheduling.runtime import SchedulerRuntime
from app.scheduling.schemas import TaskResponse, TaskRunResponse, TaskWorkspaceItem, TaskWorkspaceResponse


WorkspaceStatus = Literal["all", "active", "paused", "completed", "running", "queued", "attention"]
ATTENTION_STATUSES = ("failed", "interrupted", "timed_out", "indeterminate")


def task_workspace(
    session: Session,
    runtime: SchedulerRuntime,
    registry: TaskRegistry,
    *,
    query: str = "",
    source_key: str | None = None,
    status: WorkspaceStatus = "all",
    limit: int = 100,
    offset: int = 0,
) -> TaskWorkspaceResponse:
    """Keep filtering, counting, and pagination in SQL, including older runs.

    A newer queued or skipped run must not conceal an older running execution.
    Correlated latest-run lookup and grouped live counts each produce at most
    one joined row per task, so pagination and totals cannot multiply tasks.
    """
    definitions = {definition.key: definition for definition in registry.list()}
    live = (
        select(
            TaskRun.task_id.label("task_id"),
            func.sum(case((TaskRun.status == "running", 1), else_=0)).label("running"),
            func.sum(case((TaskRun.status == "queued", 1), else_=0)).label("queued"),
        )
        .where(TaskRun.status.in_(("running", "queued")))
        .group_by(TaskRun.task_id)
        .subquery()
    )
    latest_id = (
        select(TaskRun.id)
        .where(TaskRun.task_id == ScheduledTask.id)
        .order_by(TaskRun.created_at.desc(), TaskRun.id.desc())
        .limit(1)
        .correlate(ScheduledTask)
        .scalar_subquery()
    )
    running = func.coalesce(live.c.running, 0)
    queued = func.coalesce(live.c.queued, 0)
    statement = (
        select(ScheduledTask, TaskRun, running, queued)
        .select_from(ScheduledTask)
        .outerjoin(TaskRun, TaskRun.id == latest_id)
        .outerjoin(live, live.c.task_id == ScheduledTask.id)
        .where(ScheduledTask.state != "archived")
    )
    if source_key is not None:
        statement = statement.where(ScheduledTask.task_type.in_([
            key for key, definition in definitions.items() if definition.source_key == source_key
        ]))
    needle = query.strip()
    if needle:
        matching_types = [key for key, definition in definitions.items()
                          if needle.casefold() in (definition.name + " " + definition.english_name).casefold()]
        # Escape SQL wildcard characters: operator searches are literal text.
        statement = statement.where(or_(
            ScheduledTask.name.icontains(needle, autoescape=True),
            ScheduledTask.description.icontains(needle, autoescape=True),
            ScheduledTask.task_type.in_(matching_types),
        ))
    if status in ("active", "paused", "completed"):
        statement = statement.where(ScheduledTask.state == status)
    elif status == "running":
        statement = statement.where(running > 0)
    elif status == "queued":
        statement = statement.where(queued > 0)
    elif status == "attention":
        # This view means the most recent run needs attention, not that every
        # historical failure remains actionable after later successful runs.
        statement = statement.where(TaskRun.status.in_(ATTENTION_STATUSES))
    total = session.scalar(statement.with_only_columns(func.count(), maintain_column_froms=True)) or 0
    rows = session.execute(statement.order_by(ScheduledTask.created_at.desc(), ScheduledTask.id.desc())
                           .limit(limit).offset(offset)).all()
    source_keys = {definitions[task.task_type].source_key for task, *_ in rows if task.task_type in definitions}
    sources = {row.key: row for row in session.scalars(
        select(DataSourceConfig).where(DataSourceConfig.key.in_([key for key in source_keys if key is not None]))
    )} if source_keys else {}
    items = []
    for task, latest_run, running_count, queued_count in rows:
        definition = definitions.get(task.task_type)
        key = definition.source_key if definition else None
        source = sources.get(key)
        source_enabled = bool(source and source.enabled) if key else None
        source_configured = configured(source) if source is not None else (False if key else None)
        # The scheduler's trigger may still exist while its source is disabled.
        # Expose no misleading next execution in that case; the plan is retained.
        next_run_at = (runtime.next_run_at(task.id) if task.state == "active"
                       and definition is not None and source_enabled is not False
                       and source_configured is not False else None)
        # Construct explicitly: never serialize source config values or secrets.
        item = TaskWorkspaceItem(**TaskResponse.model_validate(task).model_dump(),
            registered=definition is not None,
            task_type_name=definition.name if definition else None,
            task_type_english_name=definition.english_name if definition else None,
            source_key=key, source_enabled=source_enabled, source_configured=source_configured,
            running_count=running_count, queued_count=queued_count)
        items.append(item.model_copy(update={
            "next_run_at": next_run_at,
            "latest_run": TaskRunResponse.model_validate(latest_run) if latest_run else None,
        }))
    return TaskWorkspaceResponse(items=items, total=total, limit=limit, offset=offset)
