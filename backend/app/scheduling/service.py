from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskRegistry
from app.scheduling.repository import SchedulerRepository
from app.scheduling.schemas import (
    OverlapPolicy,
    OnceSchedule,
    RunStatus,
    TaskCreate,
    TaskState,
    TaskUpdate,
    TriggerType,
    schedule_adapter,
)
from app.scheduling.triggers import build_trigger


class TaskNotFoundError(Exception):
    pass


class TaskConflictError(Exception):
    pass


class UnknownTaskTypeError(Exception):
    pass


class InvalidTaskParametersError(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("invalid task parameters")
        self.errors = errors


class SchedulerService:
    def __init__(self, session: Session, registry: TaskRegistry) -> None:
        self.session = session
        self.registry = registry
        self.repository = SchedulerRepository(session)

    def create_task(self, payload: TaskCreate) -> ScheduledTask:
        parameters, parameter_version = self._validate_definition(
            payload.task_type,
            payload.parameters,
            payload.schedule,
        )
        task = ScheduledTask(
            name=payload.name,
            description=payload.description,
            task_type=payload.task_type,
            parameters=parameters,
            parameter_version=parameter_version,
            schedule=payload.schedule.model_dump(mode="json"),
            state=TaskState.ACTIVE.value,
            concurrency_limit=payload.concurrency_limit,
            overlap_policy=payload.overlap_policy.value,
            queue_limit=payload.queue_limit,
            priority=payload.priority,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def update_task(self, task_id: UUID, payload: TaskUpdate) -> ScheduledTask:
        task = self._require_task(task_id, for_update=True)
        if task.state == TaskState.ARCHIVED.value:
            raise TaskConflictError("archived tasks cannot be modified")
        if task.version != payload.version:
            raise TaskConflictError("task version does not match")

        changes = payload.model_dump(exclude_unset=True, exclude={"version"})
        task_type = changes.get("task_type", task.task_type)
        parameters = changes.get("parameters", task.parameters)
        schedule_data = changes.get("schedule", task.schedule)
        schedule = schedule_adapter.validate_python(schedule_data)
        validated_parameters, parameter_version = self._validate_definition(
            task_type,
            parameters,
            schedule,
            require_future_once="schedule" in changes,
        )

        for field in (
            "name",
            "description",
            "task_type",
            "concurrency_limit",
            "queue_limit",
            "priority",
        ):
            if field in changes:
                setattr(task, field, changes[field])
        if "overlap_policy" in changes:
            task.overlap_policy = changes["overlap_policy"].value
        task.parameters = validated_parameters
        task.parameter_version = parameter_version
        task.schedule = schedule.model_dump(mode="json")
        if task.state == TaskState.COMPLETED.value and "schedule" in changes:
            task.state = TaskState.PAUSED.value
        task.version += 1
        task.updated_at = datetime.now(UTC)
        self.session.flush()
        return task

    def change_state(
        self, task_id: UUID, *, expected_version: int, target: TaskState
    ) -> ScheduledTask:
        task = self._require_task(task_id, for_update=True)
        if task.version != expected_version:
            raise TaskConflictError("task version does not match")
        if task.state == TaskState.ARCHIVED.value:
            raise TaskConflictError("archived tasks cannot change state")
        if target == TaskState.ACTIVE and task.state != TaskState.PAUSED.value:
            raise TaskConflictError("only paused tasks can be resumed")
        if target == TaskState.PAUSED and task.state != TaskState.ACTIVE.value:
            raise TaskConflictError("only active tasks can be paused")
        task.state = target.value
        task.version += 1
        task.updated_at = datetime.now(UTC)
        if target == TaskState.ARCHIVED:
            self.repository.skip_queued_runs_for_archived_task(task.id)
        self.session.flush()
        return task

    def archive_task(self, task_id: UUID, *, expected_version: int) -> ScheduledTask:
        return self.change_state(
            task_id,
            expected_version=expected_version,
            target=TaskState.ARCHIVED,
        )

    def enqueue_run(
        self,
        task_id: UUID,
        *,
        trigger_type: TriggerType,
        max_queued_runs: int,
        scheduled_at: datetime | None = None,
    ) -> TaskRun:
        task = self._require_task(task_id, for_update=True)
        allowed_states = (
            (TaskState.ACTIVE.value,)
            if trigger_type == TriggerType.SCHEDULED
            else (TaskState.ACTIVE.value, TaskState.PAUSED.value)
        )
        if task.state not in allowed_states:
            raise TaskConflictError(
                f"task state {task.state!r} does not allow {trigger_type.value} runs"
            )

        running = self.repository.count_runs(
            task_id=task.id, statuses=(RunStatus.RUNNING.value,)
        )
        queued = self.repository.count_runs(
            task_id=task.id, statuses=(RunStatus.QUEUED.value,)
        )
        total_queued = self.repository.count_runs(
            task_id=None, statuses=(RunStatus.QUEUED.value,)
        )

        skip_reason: str | None = None
        if total_queued >= max_queued_runs:
            skip_reason = "Global queue limit reached."
        elif task.overlap_policy == OverlapPolicy.SKIP.value:
            if running + queued >= task.concurrency_limit:
                skip_reason = "Task concurrency limit reached."
        elif queued >= task.queue_limit:
            skip_reason = "Task queue limit reached."

        run = self.repository.add_run(
            task,
            trigger_type=trigger_type,
            status=RunStatus.SKIPPED if skip_reason else RunStatus.QUEUED,
            error_message=skip_reason,
            scheduled_at=scheduled_at,
        )

        schedule = schedule_adapter.validate_python(task.schedule)
        if trigger_type == TriggerType.SCHEDULED and schedule.type == "once":
            task.state = TaskState.COMPLETED.value
            task.version += 1
            task.updated_at = datetime.now(UTC)
        self.session.flush()
        return run

    def _require_task(self, task_id: UUID, *, for_update: bool) -> ScheduledTask:
        task = self.repository.get_task(task_id, for_update=for_update)
        if task is None:
            raise TaskNotFoundError(str(task_id))
        return task

    def _validate_definition(
        self,
        task_type: str,
        parameters: dict,
        schedule,
        *,
        require_future_once: bool = True,
    ) -> tuple[dict, int]:
        definition = self.registry.get(task_type)
        if definition is None:
            raise UnknownTaskTypeError(task_type)
        try:
            validated = definition.parameters_model.model_validate(parameters)
        except ValidationError as exc:
            raise InvalidTaskParametersError(exc.errors()) from exc
        if (
            require_future_once
            and isinstance(schedule, OnceSchedule)
            and schedule.run_at <= datetime.now(UTC)
        ):
            raise ValueError("once run_at must be in the future")
        # Building the trigger performs APScheduler's semantic validation,
        # including cron field ranges that cannot be expressed by JSON shape.
        build_trigger(schedule)
        return validated.model_dump(mode="json"), definition.parameter_version
