from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.schemas import RunStatus, TaskState, TriggerType


TERMINAL_RUN_STATUSES = (
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.SKIPPED.value,
    RunStatus.INTERRUPTED.value,
)


class SchedulerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_task(self, task_id: UUID, *, for_update: bool = False) -> ScheduledTask | None:
        statement = select(ScheduledTask).where(ScheduledTask.id == task_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_tasks(
        self, *, include_archived: bool, limit: int, offset: int
    ) -> list[ScheduledTask]:
        statement = select(ScheduledTask)
        if not include_archived:
            statement = statement.where(
                ScheduledTask.state != TaskState.ARCHIVED.value
            )
        statement = statement.order_by(ScheduledTask.created_at.desc()).limit(limit).offset(offset)
        return list(self.session.scalars(statement))

    def list_active_task_ids(self) -> list[UUID]:
        statement = select(ScheduledTask.id).where(
            ScheduledTask.state == TaskState.ACTIVE.value
        )
        return list(self.session.scalars(statement))

    def list_runs(
        self, *, task_id: UUID | None, limit: int, offset: int
    ) -> list[TaskRun]:
        statement = select(TaskRun)
        if task_id is not None:
            statement = statement.where(TaskRun.task_id == task_id)
        statement = statement.order_by(TaskRun.created_at.desc()).limit(limit).offset(offset)
        return list(self.session.scalars(statement))

    def list_latest_runs_for_tasks(
        self, task_ids: list[UUID]
    ) -> dict[UUID, TaskRun]:
        """Return the newest execution for each requested task in one query."""
        if not task_ids:
            return {}

        ranked_runs = (
            select(
                TaskRun.id.label("run_id"),
                func.row_number()
                .over(
                    partition_by=TaskRun.task_id,
                    order_by=(TaskRun.created_at.desc(), TaskRun.id.desc()),
                )
                .label("run_rank"),
            )
            .where(TaskRun.task_id.in_(task_ids))
            .subquery()
        )
        statement = (
            select(TaskRun)
            .join(ranked_runs, ranked_runs.c.run_id == TaskRun.id)
            .where(ranked_runs.c.run_rank == 1)
        )
        return {run.task_id: run for run in self.session.scalars(statement)}

    def get_run(self, run_id: UUID) -> TaskRun | None:
        return self.session.get(TaskRun, run_id)

    def count_runs(self, *, task_id: UUID | None, statuses: tuple[str, ...]) -> int:
        statement = select(func.count()).select_from(TaskRun).where(
            TaskRun.status.in_(statuses)
        )
        if task_id is not None:
            statement = statement.where(TaskRun.task_id == task_id)
        return self.session.scalar(statement) or 0

    def add_run(
        self,
        task: ScheduledTask,
        *,
        trigger_type: TriggerType,
        status: RunStatus,
        error_message: str | None = None,
        scheduled_at: datetime | None = None,
    ) -> TaskRun:
        now = datetime.now(UTC)
        run = TaskRun(
            task_id=task.id,
            task_version=task.version,
            task_type=task.task_type,
            trigger_type=trigger_type.value,
            status=status.value,
            # Keep historical executions independent from later edits to nested JSON.
            parameters=deepcopy(task.parameters),
            parameter_version=task.parameter_version,
            priority=task.priority,
            error_message=error_message,
            scheduled_at=scheduled_at,
            available_at=now,
            finished_at=now if status == RunStatus.SKIPPED else None,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def claim_queued_runs(self, limit: int) -> list[UUID]:
        if limit <= 0:
            return []

        now = datetime.now(UTC)
        running_counts = (
            select(
                TaskRun.task_id.label("running_task_id"),
                func.count().label("running_count"),
            )
            .where(TaskRun.status == RunStatus.RUNNING.value)
            .group_by(TaskRun.task_id)
            .subquery()
        )
        running_count = func.coalesce(running_counts.c.running_count, 0)
        task_order = (
            TaskRun.priority.desc(),
            TaskRun.available_at,
            TaskRun.created_at,
            TaskRun.id,
        )
        eligible_runs = (
            select(
                TaskRun.id.label("run_id"),
                ScheduledTask.concurrency_limit.label("concurrency_limit"),
                running_count.label("running_count"),
                func.row_number()
                .over(partition_by=TaskRun.task_id, order_by=task_order)
                .label("task_rank"),
            )
            .join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
            .outerjoin(
                running_counts,
                running_counts.c.running_task_id == TaskRun.task_id,
            )
            .where(
                TaskRun.status == RunStatus.QUEUED.value,
                TaskRun.available_at <= now,
                ScheduledTask.state != TaskState.ARCHIVED.value,
                running_count < ScheduledTask.concurrency_limit,
            )
            .cte("eligible_task_runs")
        )
        statement = (
            select(TaskRun, ScheduledTask)
            .join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
            .where(
                TaskRun.id.in_(
                    select(eligible_runs.c.run_id).where(
                        eligible_runs.c.task_rank
                        <= eligible_runs.c.concurrency_limit
                        - eligible_runs.c.running_count
                    )
                )
            )
            .order_by(
                TaskRun.priority.desc(),
                TaskRun.available_at,
                TaskRun.created_at,
                TaskRun.id,
            )
            .limit(limit)
            .with_for_update(of=TaskRun, skip_locked=True)
        )
        candidates = list(self.session.execute(statement).all())

        claimed: list[UUID] = []
        for run, task in candidates:
            run.status = RunStatus.RUNNING.value
            run.started_at = now
            claimed.append(run.id)
        self.session.flush()
        return claimed

    def finish_run(
        self,
        run_id: UUID,
        *,
        status: RunStatus,
        result: dict | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        if status.value not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"non-terminal run status: {status}")
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == run_id,
                TaskRun.status == RunStatus.RUNNING.value,
            )
            .values(
                status=status.value,
                result=result,
                error_type=error_type,
                error_message=error_message,
                finished_at=datetime.now(UTC),
            )
        )
        return (self.session.execute(statement).rowcount or 0) == 1

    def interrupt_running_runs(self) -> int:
        now = datetime.now(UTC)
        result = self.session.execute(
            update(TaskRun)
            .where(TaskRun.status == RunStatus.RUNNING.value)
            .values(
                status=RunStatus.INTERRUPTED.value,
                error_type="ProcessRestarted",
                error_message="The application stopped before the run completed.",
                finished_at=now,
            )
        )
        return result.rowcount or 0

    def skip_queued_runs_for_archived_task(self, task_id: UUID) -> int:
        now = datetime.now(UTC)
        result = self.session.execute(
            update(TaskRun)
            .where(
                TaskRun.task_id == task_id,
                TaskRun.status == RunStatus.QUEUED.value,
            )
            .values(
                status=RunStatus.SKIPPED.value,
                error_message="Task was archived before execution.",
                finished_at=now,
            )
        )
        return result.rowcount or 0
