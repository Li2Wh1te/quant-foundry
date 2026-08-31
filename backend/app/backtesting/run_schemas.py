"""Strict HTTP contracts for formal and internal run endpoints."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Any
from uuid import UUID

class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict[str, Any]
    strategy_revision_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    degraded: bool = False
    confirmed_admission_report_hash: str | None = None

class InternalRunCreateRequest(RunCreateRequest):
    pass

class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    run_kind: str
    profile: str
    status: str
    config_hash: str

class RunError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
