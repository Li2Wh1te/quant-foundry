"""Small, credential-free response models for the operations overview."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.scheduling.schemas import RunStatus


class OverviewRun(BaseModel):
    id: UUID
    task_id: UUID
    task_name: str
    task_type: str
    task_type_name: str
    task_type_english_name: str
    source_key: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None


class AttentionTask(BaseModel):
    run: OverviewRun
    retrying: bool


class SourceOverview(BaseModel):
    key: str
    name: str
    configured: bool
    connection_status: Literal["not_checked"] = "not_checked"
    active_tasks: int
    last_success_at: datetime | None


class OverviewMetrics(BaseModel):
    configured_sources: int
    total_sources: int
    active_tasks: int
    queued_runs: int
    running_runs: int
    today_runs: int
    today_succeeded: int
    attention_tasks: int


class OperationsOverview(BaseModel):
    generated_at: datetime
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    day_start: datetime
    day_end: datetime
    metrics: OverviewMetrics
    sources: list[SourceOverview]
    recent_runs: list[OverviewRun]
    attention: list[AttentionTask]
