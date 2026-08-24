"""Explicit account selection and immutable run configuration snapshots.

Account profiles and fee schedules are mutable operator configuration.
A run captures a complete snapshot at admission time, which is the
historical boundary that matters to execution.  Immutable profile
*versions*, operational availability, and the fixed three-layer
resolution order (explicit > strategy default > user default) live in
:mod:`app.backtesting.account_resolution`; this module stays the
per-run snapshot boundary over whichever configuration a resolution
selected.  No platform fallback exists here; callers must provide the
resolved profile explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from threading import RLock
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import UUID

from app.backtesting.domain import DomainValidationError
from app.backtesting.fees import FeeRule, FeeSchedule, FeeScheduleSnapshot


class AccountProfileError(DomainValidationError):
    """Raised when an account profile cannot be selected for a run."""


class AccountSelectionRequiredError(AccountProfileError):
    """Raised when run creation omits the required explicit account choice."""


class AccountProfileNotFoundError(AccountProfileError):
    """Raised when the selected account profile does not exist."""


class AccountProfileAlreadyExistsError(AccountProfileError):
    """Raised when a profile id is registered more than once."""


class AccountProfileUnavailableError(AccountProfileError):
    """Raised when a profile is not currently eligible for a formal run."""


class AccountProfileStatus(StrEnum):
    """Lifecycle states visible to account selection."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    RETIRED = "retired"


def _profile_id(value: UUID | str, field_name: str = "profile_id") -> UUID:
    """Normalize a stable profile identity without accepting empty values."""

    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        raise AccountProfileError(f"{field_name} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise AccountProfileError(f"{field_name} must be a UUID") from exc


def _metadata(value: Mapping[str, str], field_name: str) -> Mapping[str, str]:
    """Copy metadata so later caller mutations cannot alter a profile/snapshot."""

    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise AccountProfileError(f"{field_name} keys must be non-blank text")
        if not isinstance(item, str):
            raise AccountProfileError(f"{field_name} values must be text")
        normalized[key.strip()] = item
    return MappingProxyType(normalized)


def _canonical(value: Any) -> Any:
    """Convert domain values to deterministic JSON-compatible structures."""

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: Any) -> str:
    """Hash a canonical snapshot payload for audit and change detection."""

    encoded = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fee_rule_payload(rule: FeeRule) -> dict[str, Any]:
    """Build the complete serializable representation of one fee rule."""

    return {
        "key": rule.key,
        "category": rule.category,
        "side": rule.side,
        "rate": rule.rate,
        "minimum": rule.minimum,
        "fixed_amount": rule.fixed_amount,
        "rounding_level": rule.rounding_level,
        "rounding_scope": rule.rounding_scope,
        "rounding_mode": rule.rounding_mode,
        "rounding_precision": rule.rounding_precision,
        "applicability": rule.applicability,
    }


def _fee_schedule_payload(schedule: FeeScheduleSnapshot) -> dict[str, Any]:
    """Build the complete serializable representation of a fee snapshot."""

    return {
        "key": schedule.key,
        "fee_rules": [_fee_rule_payload(rule) for rule in schedule.fee_rules],
        "metadata": schedule.metadata,
    }


@dataclass(frozen=True, slots=True, init=False)
class BacktestAccountProfile:
    """Mutable-configuration representation of one selectable account.

    The dataclass is immutable at runtime so a registry update replaces the
    whole object atomically.  This avoids partially updated profiles while
    still keeping the product model free of user-managed version numbers.
    """

    profile_id: UUID
    name: str
    fee_schedule: FeeSchedule
    status: AccountProfileStatus | str = AccountProfileStatus.ACTIVE
    metadata: Mapping[str, str] = MappingProxyType({})

    def __init__(
        self,
        profile_id: UUID,
        name: str | None = None,
        fee_schedule: FeeSchedule | None = None,
        status: AccountProfileStatus | str = AccountProfileStatus.ACTIVE,
        metadata: Mapping[str, str] = MappingProxyType({}),
        *,
        display_name: str | None = None,
    ) -> None:
        """Construct a profile while accepting the previous display-name alias."""

        if name is None:
            name = display_name
        elif display_name is not None and name.strip() != display_name.strip():
            raise AccountProfileError("name and display_name must match")
        if name is None:
            raise AccountProfileError("account profile name must be provided")
        if fee_schedule is None:
            raise AccountProfileError("account profile fee_schedule must be provided")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "fee_schedule", fee_schedule)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metadata", metadata)
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _profile_id(self.profile_id))
        if not isinstance(self.name, str) or not self.name.strip():
            raise AccountProfileError("account profile name must be non-blank text")
        object.__setattr__(self, "name", self.name.strip())
        try:
            normalized_status = AccountProfileStatus(self.status)
        except ValueError as exc:
            raise AccountProfileError("account profile status is unsupported") from exc
        object.__setattr__(self, "status", normalized_status)
        if self.fee_schedule.test_only:
            raise AccountProfileError(
                "test-only fee schedules cannot be attached to formal account profiles"
            )
        object.__setattr__(self, "metadata", _metadata(self.metadata, "account metadata"))

    def snapshot(self) -> "BacktestAccountProfileSnapshot":
        """Freeze the complete account and fee configuration for one run."""

        self.fee_schedule.validate_for_run()
        fee_snapshot = self.fee_schedule.snapshot()
        return BacktestAccountProfileSnapshot(
            profile_id=self.profile_id,
            name=self.name,
            status=self.status,
            metadata=self.metadata,
            fee_schedule=fee_snapshot,
        )

    @property
    def display_name(self) -> str:
        """Compatibility alias for callers using the domain-document name."""

        return self.name


@dataclass(frozen=True, slots=True, init=False)
class BacktestAccountProfileSnapshot:
    """Immutable account/fee snapshot persisted with a newly created run."""

    profile_id: UUID
    name: str
    status: AccountProfileStatus
    metadata: Mapping[str, str]
    fee_schedule: FeeScheduleSnapshot
    fee_schedule_hash: str = field(init=False)
    snapshot_hash: str = field(init=False)

    def __init__(
        self,
        profile_id: UUID,
        name: str | None = None,
        status: AccountProfileStatus = AccountProfileStatus.ACTIVE,
        metadata: Mapping[str, str] = MappingProxyType({}),
        fee_schedule: FeeScheduleSnapshot | None = None,
        *,
        display_name: str | None = None,
    ) -> None:
        """Construct a snapshot while accepting the previous display-name alias."""

        if name is None:
            name = display_name
        elif display_name is not None and name.strip() != display_name.strip():
            raise AccountProfileError("name and display_name must match")
        if name is None:
            raise AccountProfileError("account snapshot name must be provided")
        if fee_schedule is None:
            raise AccountProfileError("account snapshot fee_schedule must be provided")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "fee_schedule", fee_schedule)
        self.__post_init__()

    def __post_init__(self) -> None:
        normalized_id = _profile_id(self.profile_id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise AccountProfileError("account snapshot name must be non-blank text")
        object.__setattr__(self, "profile_id", normalized_id)
        object.__setattr__(self, "name", self.name.strip())
        try:
            object.__setattr__(self, "status", AccountProfileStatus(self.status))
        except ValueError as exc:
            raise AccountProfileError("account snapshot status is unsupported") from exc
        object.__setattr__(self, "metadata", _metadata(self.metadata, "snapshot metadata"))
        self.fee_schedule.validate_for_run()
        fee_hash = _digest(_fee_schedule_payload(self.fee_schedule))
        object.__setattr__(self, "fee_schedule_hash", fee_hash)
        object.__setattr__(
            self,
            "snapshot_hash",
            _digest(
                {
                    "profile_id": self.profile_id,
                    "name": self.name,
                    "status": self.status,
                    "metadata": self.metadata,
                    "fee_schedule": _fee_schedule_payload(self.fee_schedule),
                }
            ),
        )

    @property
    def display_name(self) -> str:
        """Compatibility alias for callers using the domain-document name."""

        return self.name


class AccountProfileCatalog:
    """In-memory account profile catalog with explicit run selection only.

    Persistence adapters can implement the same operations later.  The
    catalog intentionally has no default profile state and no fallback path.
    A lock makes replacement and snapshot creation atomic for callers that
    create runs concurrently with configuration edits.
    """

    def __init__(self, profiles: Iterable[BacktestAccountProfile] = ()) -> None:
        self._lock = RLock()
        self._profiles: dict[UUID, BacktestAccountProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: BacktestAccountProfile) -> None:
        """Register a new profile without silently replacing an existing one."""

        if not isinstance(profile, BacktestAccountProfile):
            raise AccountProfileError("profile must be a BacktestAccountProfile")
        with self._lock:
            if profile.profile_id in self._profiles:
                raise AccountProfileAlreadyExistsError(
                    f"account profile {profile.profile_id} already exists"
                )
            self._profiles[profile.profile_id] = profile

    def replace(self, profile: BacktestAccountProfile) -> None:
        """Replace one mutable configuration atomically using the same id."""

        if not isinstance(profile, BacktestAccountProfile):
            raise AccountProfileError("profile must be a BacktestAccountProfile")
        with self._lock:
            if profile.profile_id not in self._profiles:
                raise AccountProfileNotFoundError(
                    f"account profile {profile.profile_id} does not exist"
                )
            self._profiles[profile.profile_id] = profile

    def get(self, profile_id: UUID | str) -> BacktestAccountProfile:
        """Return the current configuration or fail without a fallback."""

        normalized_id = _profile_id(profile_id)
        with self._lock:
            try:
                return self._profiles[normalized_id]
            except KeyError as exc:
                raise AccountProfileNotFoundError(
                    f"account profile {normalized_id} does not exist"
                ) from exc

    def selectable(self) -> tuple[BacktestAccountProfile, ...]:
        """Return active profiles in a deterministic selector order."""

        with self._lock:
            profiles = [
                profile
                for profile in self._profiles.values()
                if profile.status is AccountProfileStatus.ACTIVE
            ]
        return tuple(sorted(profiles, key=lambda item: (item.name.casefold(), str(item.profile_id))))

    def create_run_snapshot(
        self,
        account_profile_id: UUID | str | None,
    ) -> BacktestAccountProfileSnapshot:
        """Create a run snapshot from the explicitly selected profile id.

        ``None`` is intentionally an error.  This is the enforcement boundary
        for the product rule that every run must visibly choose an account.
        """

        if account_profile_id is None:
            raise AccountSelectionRequiredError(
                "an account profile must be explicitly selected before run creation"
            )
        normalized_id = _profile_id(account_profile_id, "account_profile_id")
        with self._lock:
            try:
                profile = self._profiles[normalized_id]
            except KeyError as exc:
                raise AccountProfileNotFoundError(
                    f"account profile {normalized_id} does not exist"
                ) from exc
            if profile.status is not AccountProfileStatus.ACTIVE:
                raise AccountProfileUnavailableError(
                    f"account profile {normalized_id} is not active"
                )
            return profile.snapshot()


# The registry name is useful to adapters that prefer repository terminology;
# both names refer to the same explicit-selection contract.
AccountProfileRegistry = AccountProfileCatalog


__all__ = [
    "AccountProfileAlreadyExistsError",
    "AccountProfileCatalog",
    "AccountProfileError",
    "AccountProfileNotFoundError",
    "AccountProfileRegistry",
    "AccountProfileStatus",
    "AccountProfileUnavailableError",
    "AccountSelectionRequiredError",
    "BacktestAccountProfile",
    "BacktestAccountProfileSnapshot",
]
