"""HTTP request and response schemas for private strategy administration."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrategyState(StrEnum):
    """The small lifecycle surface exposed by the first strategy API."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class StrategyCreateRequest(BaseModel):
    """Create one private strategy and its first mutable database draft."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=10_000)
    source_code: str = Field(min_length=1, max_length=1_048_576)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)
    default_parameters: dict[str, Any] = Field(default_factory=dict)


class StrategyMetadataUpdateRequest(BaseModel):
    """Optimistically update only user-facing strategy metadata."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def require_a_metadata_change(self) -> "StrategyMetadataUpdateRequest":
        """Reject no-op requests and explicit null for the required name field."""
        editable_fields = self.model_fields_set & {"name", "description"}
        if not editable_fields:
            raise ValueError("at least one strategy metadata field is required")
        if "name" in editable_fields and self.name is None:
            raise ValueError("name must not be null")
        return self


class StrategyDraftSaveRequest(BaseModel):
    """Patch editor fields when the draft's prior version still matches."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    source_code: str | None = Field(default=None, min_length=1, max_length=1_048_576)
    parameter_schema: dict[str, Any] | None = None
    default_parameters: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_a_draft_field(self) -> "StrategyDraftSaveRequest":
        """Reject a no-op patch and explicit null values for editable fields."""
        editable_fields = self.model_fields_set - {"version"}
        if not editable_fields:
            raise ValueError("at least one draft field is required")
        for field_name in editable_fields:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must not be null")
        return self


class StrategyPublishRequest(BaseModel):
    """Publish exactly the draft revision the editor previously inspected."""

    model_config = ConfigDict(extra="forbid")

    draft_version: int = Field(ge=1)


class StrategyRevisionSummaryResponse(BaseModel):
    """Audit metadata for a revision without returning its private source code."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    revision_number: int
    source_hash: str
    runtime_manifest: dict[str, Any]
    published_at: datetime


class StrategyRevisionResponse(StrategyRevisionSummaryResponse):
    """One private immutable revision, including source for an authorized editor."""

    source_code: str
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]


class StrategyDraftResponse(BaseModel):
    """The only mutable source representation for an authorized strategy editor."""

    model_config = ConfigDict(from_attributes=True)

    source_code: str
    source_hash: str
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class StrategySummaryResponse(BaseModel):
    """List-safe strategy metadata that deliberately excludes private source."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    state: StrategyState
    current_revision_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime


class StrategyDetailResponse(StrategySummaryResponse):
    """One editor-oriented strategy response with its draft and current revision."""

    draft: StrategyDraftResponse
    current_revision: StrategyRevisionSummaryResponse | None


class StrategyBacktestWorkspaceResponse(BaseModel):
    """Strategy-scoped projection consumed by the backtest workbench."""

    strategy: dict[str, Any]
    published_revisions: list[dict[str, Any]]
    slippage_models: list[dict[str, Any]]
    component_options: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    formal_gate: dict[str, Any]
    runs: dict[str, Any]


class StrategyValidationIssueResponse(BaseModel):
    """A display-safe static validation issue that does not echo private source."""

    code: str
    message: str
    line: int | None
    column: int | None


class StrategyDraftValidationResponse(BaseModel):
    """Result of static validation for the precise saved draft version."""

    valid: bool
    draft_version: int
    source_hash: str
    issues: list[StrategyValidationIssueResponse]
