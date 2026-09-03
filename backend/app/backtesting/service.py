"""Application service for account-profile CRUD and explicit selection."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.backtesting.account_profiles import AccountProfileStatus
from app.backtesting.fees import FeeRule, FeeSchedule
from app.backtesting.models import BacktestAccountProfileRecord
from app.backtesting.repository import BacktestAccountProfileRepository


class AccountProfileStorageError(Exception):
    """Base class for expected account-profile persistence errors."""


class AccountProfileNotFoundError(AccountProfileStorageError):
    """Raised when a requested account profile does not exist."""


class AccountProfileNameConflictError(AccountProfileStorageError):
    """Raised when two profiles would share the same case-insensitive name."""


class AccountProfileValidationError(AccountProfileStorageError, ValueError):
    """Raised when profile or fee configuration is invalid for storage."""


class AccountProfileService:
    """Persist editable profiles; no run records or hidden defaults are touched."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = BacktestAccountProfileRepository(session)

    def create(
        self,
        *,
        name: str,
        status: AccountProfileStatus,
        fee_schedule: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> BacktestAccountProfileRecord:
        """Create one profile after validating its complete fee configuration."""

        normalized_name = _normalize_name(name)
        if self.repository.name_exists(normalized_name):
            raise AccountProfileNameConflictError(normalized_name)
        schedule = _build_schedule(fee_schedule)
        _validate_formal_schedule(schedule)
        record = BacktestAccountProfileRecord(
            id=uuid4(),
            name=normalized_name,
            status=_normalize_status(status),
            version=1,
            fee_schedule_key=schedule.key,
            fee_schedule_version=1,
            fee_rules=_json_value(
                [_fee_rule_payload(rule) for rule in schedule.fee_rules]
            ),
            fee_schedule_metadata=_json_value(dict(schedule.metadata)),
            profile_metadata=_json_value(dict(metadata)),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def update(
        self,
        profile_id: UUID,
        *,
        name: str | None = None,
        status: AccountProfileStatus | None = None,
        fee_schedule: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> BacktestAccountProfileRecord:
        """Replace supplied fields atomically; no revision is generated."""

        record = self.repository.get(profile_id, for_update=True)
        if record is None:
            raise AccountProfileNotFoundError(str(profile_id))
        changed = False
        if record.fee_schedule_version is None:
            record.fee_schedule_version = 1
        if name is not None:
            normalized_name = _normalize_name(name)
            if self.repository.name_exists(normalized_name, excluding_id=profile_id):
                raise AccountProfileNameConflictError(normalized_name)
            record.name = normalized_name
            changed = True
        if status is not None:
            record.status = _normalize_status(status)
            changed = True
        if fee_schedule is not None:
            schedule = _build_schedule(fee_schedule)
            _validate_formal_schedule(schedule)
            record.fee_schedule_key = schedule.key
            record.fee_schedule_version = int(record.fee_schedule_version or 1) + 1
            record.fee_rules = _json_value(
                [_fee_rule_payload(rule) for rule in schedule.fee_rules]
            )
            record.fee_schedule_metadata = _json_value(dict(schedule.metadata))
            changed = True
        if metadata is not None:
            record.profile_metadata = _json_value(dict(metadata))
            changed = True
        if changed:
            record.version = int(record.version or 1) + 1
        self.session.flush()
        return record

    def get(self, profile_id: UUID) -> BacktestAccountProfileRecord:
        """Load one profile or raise a stable not-found error."""

        record = self.repository.get(profile_id)
        if record is None:
            raise AccountProfileNotFoundError(str(profile_id))
        return record

    def delete(self, profile_id: UUID) -> None:
        """Delete one catalogue profile; future run snapshots are independent."""

        record = self.repository.get(profile_id, for_update=True)
        if record is None:
            raise AccountProfileNotFoundError(str(profile_id))
        self.session.delete(record)
        self.session.flush()

    def list(
        self,
        *,
        status: AccountProfileStatus | None,
        name_query: str | None,
        limit: int,
        offset: int,
    ) -> list[BacktestAccountProfileRecord]:
        """List profiles for the admin selector, including name filtering."""

        return self.repository.list(
            status=status.value if status is not None else None,
            name_query=name_query,
            limit=limit,
            offset=offset,
        )


def _normalize_name(value: str) -> str:
    """Normalize and bound the user-facing account name."""

    if not isinstance(value, str) or not value.strip():
        raise AccountProfileValidationError("账户名称不能为空")
    normalized = value.strip()
    if len(normalized) > 100:
        raise AccountProfileValidationError("账户名称不能超过 100 个字符")
    return normalized


def _normalize_status(value: AccountProfileStatus | str) -> str:
    """Normalize the lifecycle state accepted by the persistence model."""

    try:
        return AccountProfileStatus(value).value
    except ValueError as exc:
        raise AccountProfileValidationError("账户状态不受支持") from exc


def _build_schedule(payload: Mapping[str, Any]) -> FeeSchedule:
    """Build the domain fee schedule so all rule invariants are enforced."""

    try:
        rules = tuple(FeeRule(**deepcopy(dict(rule))) for rule in payload["fee_rules"])
        schedule = FeeSchedule(
            key=str(payload["key"]),
            fee_rules=rules,
            version=(
                int(payload["version"])
                if payload.get("version") is not None
                else None
            ),
            metadata=dict(payload.get("metadata", {})),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AccountProfileValidationError(f"费用方案无效：{exc}") from exc
    return schedule


def _validate_formal_schedule(schedule: FeeSchedule) -> None:
    """Reject test fixtures and incomplete fee rounding before persistence."""

    try:
        if not schedule.fee_rules:
            raise AccountProfileValidationError("正式费用方案至少需要一条费用规则")
        schedule.validate_for_run()
    except AccountProfileValidationError:
        raise
    except ValueError as exc:
        raise AccountProfileValidationError(str(exc)) from exc


def _fee_rule_payload(rule: FeeRule) -> dict[str, Any]:
    """Convert domain fee rules into JSONB-safe configuration data."""

    return {
        "key": rule.key,
        "category": rule.category,
        "side": rule.side,
        "rate": rule.rate,
        "minimum": rule.minimum,
        "fixed_amount": rule.fixed_amount,
        "rounding_level": rule.rounding_level.value if rule.rounding_level else None,
        "rounding_scope": rule.rounding_scope,
        "rounding_mode": rule.rounding_mode.value if rule.rounding_mode else None,
        "rounding_precision": rule.rounding_precision,
        "applicability": dict(rule.applicability),
    }


def _json_value(value: Any) -> Any:
    """Recursively convert Decimal values into exact decimal strings for JSONB."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def fee_schedule_from_record(record: BacktestAccountProfileRecord) -> FeeSchedule:
    """Rehydrate the complete fee schedule stored on one account row."""

    return _build_schedule(
        {
            "key": record.fee_schedule_key,
            "version": int(getattr(record, "fee_schedule_version", None) or 1),
            "fee_rules": deepcopy(record.fee_rules),
            "metadata": deepcopy(record.fee_schedule_metadata),
        }
    )


__all__ = [
    "AccountProfileNameConflictError",
    "AccountProfileNotFoundError",
    "AccountProfileService",
    "AccountProfileStorageError",
    "AccountProfileValidationError",
    "fee_schedule_from_record",
]
