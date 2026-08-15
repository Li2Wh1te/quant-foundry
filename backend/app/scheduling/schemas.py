from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class TaskState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class OverlapPolicy(StrEnum):
    SKIP = "skip"
    QUEUE = "queue"


class TriggerType(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


class CronSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["cron"]
    expression: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown IANA timezone") from exc
        return value


class IntervalSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["interval"]
    seconds: int = Field(ge=1, le=31_536_000)
    start_at: datetime

    @field_validator("start_at")
    @classmethod
    def validate_start_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)


class OnceSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["once"]
    run_at: datetime

    @field_validator("run_at")
    @classmethod
    def validate_run_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)


ScheduleConfig = Annotated[
    CronSchedule | IntervalSchedule | OnceSchedule,
    Field(discriminator="type"),
]
schedule_adapter = TypeAdapter(ScheduleConfig)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=10_000)
    task_type: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)
    schedule: ScheduleConfig
    concurrency_limit: int = Field(default=1, ge=1, le=32)
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    queue_limit: int = Field(default=1, ge=1, le=10_000)
    priority: int = Field(default=0, ge=-100, le=100)

    @field_validator("name", "task_type")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=10_000)
    task_type: str | None = Field(default=None, min_length=1, max_length=64)
    parameters: dict[str, Any] | None = None
    schedule: ScheduleConfig | None = None
    concurrency_limit: int | None = Field(default=None, ge=1, le=32)
    overlap_policy: OverlapPolicy | None = None
    queue_limit: int | None = Field(default=None, ge=1, le=10_000)
    priority: int | None = Field(default=None, ge=-100, le=100)

    @field_validator("name", "task_type")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def reject_null_required_updates(self) -> "TaskUpdate":
        for field in ("name", "task_type", "parameters", "schedule"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must not be null")
        return self


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    task_type: str
    parameters: dict[str, Any]
    parameter_version: int
    schedule: ScheduleConfig
    state: TaskState
    concurrency_limit: int
    overlap_policy: OverlapPolicy
    queue_limit: int
    priority: int
    version: int
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    task_version: int
    task_type: str
    trigger_type: TriggerType
    status: RunStatus
    parameters: dict[str, Any]
    parameter_version: int
    priority: int
    result: dict[str, Any] | None
    error_type: str | None
    error_message: str | None
    scheduled_at: datetime | None
    available_at: datetime
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskTypeResponse(BaseModel):
    key: str
    name: str
    english_name: str | None
    parameter_version: int
    parameter_schema: dict[str, Any]


def require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value
