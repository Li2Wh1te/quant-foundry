"""HTTP schemas for editable backtest account profiles."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.backtesting.account_profiles import AccountProfileStatus
from app.backtesting.fees import FeeRoundingLevel, FeeRoundingMode


class FeeRuleRequest(BaseModel):
    """One fee item; rounding fields stay optional for editable drafts."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    side: str | None = Field(default=None, min_length=1, max_length=16)
    rate: Decimal = Field(default=Decimal("0"), ge=0)
    minimum: Decimal = Field(default=Decimal("0"), ge=0)
    fixed_amount: Decimal = Field(default=Decimal("0"), ge=0)
    rounding_level: FeeRoundingLevel | None = None
    rounding_scope: str | None = Field(default=None, min_length=1, max_length=100)
    rounding_mode: FeeRoundingMode | None = None
    rounding_precision: Decimal | None = Field(default=None, gt=0)
    applicability: dict[str, str] = Field(default_factory=dict)


class FeeScheduleRequest(BaseModel):
    """Fee schedule bound to one account profile."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100)
    fee_rules: list[FeeRuleRequest] = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_test_fixture_key(self) -> "FeeScheduleRequest":
        """Keep the kernel-only zero-cost fixture out of the formal catalogue."""

        if self.key.strip() == "zero_cost":
            raise ValueError("zero_cost 仅用于内核测试，不能保存到账户档案")
        return self


class AccountProfileCreateRequest(BaseModel):
    """Create one editable account profile with an explicit fee schedule."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    status: AccountProfileStatus = AccountProfileStatus.ACTIVE
    fee_schedule: FeeScheduleRequest
    metadata: dict[str, str] = Field(default_factory=dict)


class AccountProfileUpdateRequest(BaseModel):
    """Replace selected account configuration fields without a version number."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    status: AccountProfileStatus | None = None
    fee_schedule: FeeScheduleRequest | None = None
    metadata: dict[str, str] | None = None

    @model_validator(mode="after")
    def require_a_change(self) -> "AccountProfileUpdateRequest":
        """Reject empty patches and explicit nulls for selected fields."""

        if not self.model_fields_set:
            raise ValueError("至少提供一个账户档案字段")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 不能为 null")
        return self


class FeeRuleResponse(FeeRuleRequest):
    """Serialized fee-rule configuration returned to the selector/admin UI."""


class FeeScheduleResponse(BaseModel):
    """Serialized fee schedule bound to an account profile."""

    key: str
    version: int = Field(ge=1)
    fee_rules: list[FeeRuleResponse]
    metadata: dict[str, str]


class AccountProfileResponse(BaseModel):
    """Account profile response; ``name`` is the selector's display field."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: AccountProfileStatus
    version: int
    fee_schedule_version: int
    fee_schedule: FeeScheduleResponse
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
