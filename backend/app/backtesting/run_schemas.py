"""Strict HTTP contracts for formal and internal run endpoints."""

from datetime import date, datetime
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
    terminal_status: str | None = None
    config_hash: str
    rerun_of_run_id: UUID | None = None
    strategy_revision_id: UUID | None = None
    parameters: dict[str, Any] = {}
    backtest_config: dict[str, Any] = {}
    data_request: dict[str, Any] = {}
    behavior_versions: dict[str, Any] = {}
    progress: float = 0
    current_trading_date: date | None = None
    current_step: str | None = None
    # ``current_date`` is retained as a wire-compatible alias for clients
    # released with task-08.  New responses populate both fields from the
    # canonical ``current_trading_date`` column.
    current_date: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    claimed_at: datetime | None = None
    child_pid: int | None = None
    child_start_identity: str | None = None
    child_process_group_id: int | None = None
    worker_id: str | None = None
    worker_handshake_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_progress_persisted_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancel_requested: bool = False
    termination_requested_at: datetime | None = None
    termination_reason: str | None = None
    recovery_observed_at: datetime | None = None
    recovery_action: str | None = None
    recovery_process_state: dict[str, Any] | None = None
    runner_exit_code: int | None = None
    runner_exit_code_protocol: str | None = None
    runner_exit_category: str | None = None
    completion_marker_protocol: str | None = None
    completion_marker_validation: dict[str, Any] | None = None
    result_integrity_status: str | None = None
    terminal_decision_reason: str | None = None
    failure_phase: str | None = None
    failure_type: str | None = None
    error_message: str | None = None
    stdout_bytes: int | None = None
    stdout_digest: str | None = None
    stdout_truncated: bool | None = None
    resource_limit_evidence: dict[str, Any] | None = None
    runner_config_evidence: dict[str, Any] | None = None
    completion_marker: dict[str, Any] | None = None
    runner_exit_report: dict[str, Any] | None = None
    result_integrity_evidence: dict[str, Any] | None = None
    result_counts: dict[str, Any] = {}

class RunError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
