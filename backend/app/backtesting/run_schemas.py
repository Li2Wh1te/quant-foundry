"""Strict HTTP contracts for formal and internal run endpoints."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator


DecimalInput = Decimal | int | str


class InitialPositionRequest(BaseModel):
    """HTTP shape for one opening position in the immutable run config."""

    model_config = ConfigDict(extra="forbid")
    instrument_id: UUID
    side: Literal["long", "short", "net"] = "long"
    quantity: DecimalInput
    available_quantity: DecimalInput | None = None
    average_price: DecimalInput | None = None

    @field_validator("quantity", "available_quantity", "average_price", mode="before")
    @classmethod
    def reject_binary_floats(cls, value: Any) -> Any:
        if isinstance(value, (bool, float)):
            raise ValueError("decimal inputs must be integers or decimal strings")
        return value


class BacktestConfigRequest(BaseModel):
    """Typed single-run configuration accepted by both preflight and create."""

    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date
    initial_cash: DecimalInput = "0"
    initial_positions: list[InitialPositionRequest] = Field(default_factory=list)
    dynamic_universe: bool = False
    instrument_ids: list[UUID] = Field(default_factory=list)
    exchanges: list[str] = Field(default_factory=lambda: ["SSE", "SZSE"])
    strategy_price_bases: list[Literal["raw", "qfq", "hfq"]] = Field(
        default_factory=lambda: ["raw"]
    )
    currency: str = "CNY"
    timezone: str = "Asia/Shanghai"
    frequency: Literal["1d"] = "1d"
    warmup_sessions: int = Field(default=0, ge=0, le=512)

    @field_validator("initial_cash", mode="before")
    @classmethod
    def reject_binary_float_cash(cls, value: Any) -> Any:
        if isinstance(value, (bool, float)):
            raise ValueError("initial_cash must be an integer or decimal string")
        return value


class ComponentSelectionRequest(BaseModel):
    """Exact platform component selected by stable key and version."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_cutoff: datetime | None = None
    strategy_revision_id: UUID
    parameters: dict[str, Any] | None = None
    backtest_config: BacktestConfigRequest
    # Account selection belongs to a run, not to the strategy. The server
    # resolves this profile once and freezes its complete configuration.
    account_profile_id: UUID | None = None
    account_profile_version: int | None = Field(default=None, ge=1)
    fee_schedule_selection: ComponentSelectionRequest | None = None
    component_selections: dict[str, ComponentSelectionRequest] = Field(default_factory=dict)
    analyzer_selections: list[ComponentSelectionRequest] = Field(default_factory=list)
    slippage_model: ComponentSelectionRequest = Field(
        default_factory=lambda: ComponentSelectionRequest(
            key="none", version=1, parameters={"price_tick": "0.01"}
        )
    )
    random_seed: int | None = None
    # Body form remains supported for existing clients; the canonical API
    # also accepts the standard Idempotency-Key header.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=200)
    degraded: bool = False
    confirmed_admission_report_hash: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_spec(cls, value: Any) -> Any:
        """Accept the old ``spec`` envelope while emitting one canonical model."""

        if not isinstance(value, dict) or "spec" not in value:
            return value
        normalized = dict(value)
        if "backtest_config" in normalized:
            raise ValueError("use backtest_config, not both backtest_config and spec")
        raw_spec = normalized.pop("spec")
        if not isinstance(raw_spec, dict):
            raise ValueError("spec must be an object")
        config = dict(raw_spec)
        if "parameters" in config:
            if "parameters" in normalized:
                raise ValueError("parameters must be provided only once")
            normalized["parameters"] = config.pop("parameters")
        key = config.pop("slippage_model_key", None)
        version = config.pop("slippage_model_version", None)
        if key is not None or version is not None:
            if "slippage_model" in normalized:
                raise ValueError("slippage_model must be provided only once")
            normalized["slippage_model"] = {
                "key": key,
                "version": version,
                "parameters": config.pop("slippage_model_parameters", {}),
            }
        normalized["backtest_config"] = config
        return normalized


class InternalFixtureRequest(BaseModel):
    """Reuse the data contract before accepting internal evidence over HTTP."""

    model_config = ConfigDict(extra="forbid")
    fixture_key: str
    fixture_version: StrictInt | StrictStr
    capability: str
    scope: dict[str, Any]
    proof_summary: str | dict[str, Any]
    content_hash: str

    @model_validator(mode="after")
    def validate_fixture(self):
        self.to_domain()
        return self

    def to_domain(self):
        from app.backtesting.data.requests import InternalFixture

        values = self.model_dump()
        scope = dict(values["scope"])
        ids = scope.get("instrument_ids", ())
        if not isinstance(ids, (list, tuple)):
            raise ValueError("fixture instrument_ids must be a list")
        if not self.proof_summary:
            raise ValueError("fixture proof_summary must not be empty")
        scope["instrument_ids"] = [UUID(str(value)) for value in ids]
        values["scope"] = scope
        return InternalFixture(**values)


class InternalRunCreateRequest(RunCreateRequest):
    internal_fixtures: list[InternalFixtureRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_fixtures(self):
        identities = [(item.fixture_key, str(item.fixture_version)) for item in self.internal_fixtures]
        if len(identities) != len(set(identities)):
            raise ValueError("internal fixture identities must be unique")
        for item in self.internal_fixtures:
            fixture = item.to_domain()
            if fixture.start_date > self.backtest_config.start_date or fixture.end_date < self.backtest_config.end_date:
                raise ValueError("internal fixture scope must cover the run dates")
        return self

class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    run_kind: str
    profile: str
    visibility: Literal["formal", "internal"] = "formal"
    label: str = "正式回测"
    status: str
    terminal_status: str | None = None
    config_hash: str
    rerun_of_run_id: UUID | None = None
    strategy_revision_id: UUID | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    backtest_config: dict[str, Any] = Field(default_factory=dict)
    data_request: dict[str, Any] = Field(default_factory=dict)
    behavior_versions: dict[str, Any] = Field(default_factory=dict)
    # Runtime-observed component identities captured by the result writer;
    # before execution this falls back to the frozen binding selection.
    component_snapshot: dict[str, Any] = Field(default_factory=dict)
    # Immutable four-level admission evidence captured with the run binding.
    # It is exposed separately so callers do not need to inspect the full
    # configuration snapshot to explain why a formal run was admitted.
    formal_gates: dict[str, Any] = Field(default_factory=dict)
    account_profile_id: UUID | None = None
    account_profile_version: str | None = None
    fee_schedule_key: str | None = None
    fee_schedule_version: str | None = None
    random_seed: int | None = None
    # ``progress_ratio`` is the public protocol field.  The persistence layer
    # still stores the task-08/22 ``progress`` column, so the router performs
    # the one-way projection at this boundary instead of exposing both names.
    progress_ratio: Decimal = Decimal("0")
    completed_steps: int | None = None
    total_steps: int | None = None
    current_trading_date: date | None = None
    current_step: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    claimed_at: datetime | None = None
    child_pid: int | None = None
    # Start tokens are process-control evidence and must never be returned by
    # the operator API.  The Supervisor keeps them in the run root internally.
    child_process_group_id: int | None = None
    worker_id: str | None = None
    worker_handshake_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_progress_persisted_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancel_requested: bool = False
    termination_requested_at: datetime | None = None
    termination_reason: str | None = None
    forced_termination: bool = False
    recovery_observed_at: datetime | None = None
    recovery_action: str | None = None
    recovery_process_state: dict[str, Any] | None = None
    child_exit_code: int | None = None
    child_exit_code_protocol: str | None = None
    runner_exit_category: str | None = None
    completion_marker_protocol: str | None = None
    completion_marker_validation: dict[str, Any] | None = None
    result_integrity_status: str | None = None
    terminal_decision_reason: str | None = None
    failure_phase: str | None = None
    failure_step: int | None = None
    failure_type: str | None = None
    source_line: int | None = None
    technical_detail: str | None = None
    error_message: str | None = None
    failure_evidence: dict[str, Any] | None = None
    stdout_bytes: int | None = None
    stdout_digest: str | None = None
    stdout_truncated: bool | None = None
    stdout_evidence: dict[str, Any] | None = None
    resource_limit_evidence: dict[str, Any] | None = None
    runner_config_evidence: dict[str, Any] | None = None
    completion_marker: dict[str, Any] | None = None
    runner_exit_report: dict[str, Any] | None = None
    result_integrity_evidence: dict[str, Any] | None = None
    result_counts: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def classify_run(self):
        # All creation, list and detail projections share the same label.
        internal = self.run_kind == "internal_link_acceptance"
        self.visibility = "internal" if internal else "formal"
        self.label = "内部链路验收" if internal else "正式回测"
        return self

class RunError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
