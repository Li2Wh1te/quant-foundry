"""Immutable account-profile versions and three-layer account resolution.

The first configuration slice kept one editable catalogue row per account;
this module adds the auditable version layer on top of those semantics:

* ``BacktestAccountProfileVersion`` is an immutable ``(profile_id,
  version)`` configuration object; edits create new versions and never
  overwrite historical ones.
* ``AccountProfileAvailability`` separates operational state from
  configuration: disabling a version affects only *new* run resolution,
  never historical reads.
* ``AccountResolver`` implements the fixed resolution order ``explicit
  selection > strategy default > user default``.  Empty layers keep
  falling back, but a *configured* layer whose pinned ``(profile_id,
  version)`` reference is missing, disabled, retired, or incompatible
  fails hard with a classified reason — it never silently falls through
  to a lower layer.
* Every resolution produces a complete :class:`AccountResolutionAudit`
  (all three candidates, their statuses, the hit layer, and the failure
  reason) plus a frozen selection binding the exact fee-schedule
  snapshot, so run creation can freeze what it hit.

Defaults are stored as pinned ``profile_id + version`` references; a
default never means "whatever is newest".  The reserved ``zero_cost@1``
schedule may be referenced by test runs only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import UUID

from app.backtesting.account_profiles import (
    AccountProfileError,
    AccountProfileStatus,
    _canonical,
    _metadata,
    _profile_id,
)
from app.backtesting.domain import DomainValidationError
from app.backtesting.fees import (
    FeeError,
    FeeScheduleSnapshot,
    FeeScheduleVersionRegistry,
)

__all__ = [
    "AccountDefaultReference",
    "AccountDefaultScope",
    "AccountNotApplicableError",
    "AccountNotSelectedError",
    "AccountProfileLifecycle",
    "AccountProfileUnavailableError",
    "AccountProfileVersionCatalog",
    "AccountProfileVersionNotFoundError",
    "AccountProfileVersionReferencedError",
    "AccountResolutionAudit",
    "AccountResolutionCandidate",
    "AccountResolutionError",
    "AccountResolutionLayer",
    "AccountResolver",
    "AccountRunMode",
    "BacktestAccountProfileVersion",
    "FeeScheduleVersionMissingError",
    "FeeScheduleVersionRegistry",
    "ResolvedAccountSelection",
    "ZERO_COST_SCHEDULE_KEY",
    "ZeroCostFormalForbiddenError",
]


#: Reserved test-only schedule key; formal runs can never bind to it.
ZERO_COST_SCHEDULE_KEY = "zero_cost"


class AccountResolutionError(AccountProfileError):
    """Base class of stable account-resolution failures.

    The exception carries the complete :class:`AccountResolutionAudit`
    so callers can persist the full resolution trail alongside the
    failure instead of reconstructing it from the message.
    """

    code = "account_resolution_failed"

    def __init__(
        self,
        message: str,
        *,
        audit: "AccountResolutionAudit",
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.audit = audit
        self.details = dict(details or {})


class AccountNotSelectedError(AccountResolutionError):
    """No explicit selection was supplied and no default resolved."""

    code = "account_not_selected"


class AccountProfileVersionNotFoundError(AccountResolutionError):
    """A configured layer pins a ``(profile_id, version)`` that does not exist."""

    code = "account_version_not_found"


class AccountProfileUnavailableError(AccountResolutionError):
    """The pinned version exists but is disabled or retired."""

    code = "account_version_unavailable"


class AccountNotApplicableError(AccountResolutionError):
    """The pinned version's applicability conflicts with the run context."""

    code = "account_not_applicable"


class AccountProfileVersionReferencedError(AccountResolutionError):
    """A version bound to a historical run cannot be physically deleted."""

    code = "account_version_referenced"


class FeeScheduleVersionMissingError(AccountResolutionError):
    """The pinned fee-schedule version of the profile is not registered."""

    code = "fee_schedule_version_missing"


class ZeroCostFormalForbiddenError(AccountResolutionError):
    """A formal run tried to bind the reserved test-only zero-cost schedule."""

    code = "zero_cost_formal_forbidden"


class AccountRunMode(StrEnum):
    """Whether a run is formal or an explicitly marked test run."""

    FORMAL = "formal"
    TEST = "test"


class AccountProfileLifecycle(StrEnum):
    """Operational availability states of one profile version.

    Unlike configuration content, availability changes never rewrite a
    version: they only decide whether *new* resolutions may hit it.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"

    @classmethod
    def from_legacy_status(
        cls, status: AccountProfileStatus | str
    ) -> "AccountProfileLifecycle":
        """Map the first-slice catalogue status onto the lifecycle states."""

        normalized = AccountProfileStatus(
            getattr(status, "value", status)
        )
        if normalized is AccountProfileStatus.ACTIVE:
            return cls.ACTIVE
        if normalized is AccountProfileStatus.INACTIVE:
            return cls.DISABLED
        return cls.RETIRED


def _lifecycle(value: object) -> AccountProfileLifecycle:
    try:
        return AccountProfileLifecycle(getattr(value, "value", value))
    except ValueError as exc:
        raise AccountProfileError("account lifecycle status is unsupported") from exc


def _frozen_json(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    """Deep-freeze a JSON-like mapping so snapshots cannot mutate later."""

    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise AccountProfileError(
                f"{field_name} keys must be non-blank text"
            )
        if isinstance(item, Mapping):
            frozen[key.strip()] = _frozen_json(item, field_name)
        elif isinstance(item, (list, tuple)):
            frozen[key.strip()] = tuple(
                _frozen_json({"v": entry})["v"] for entry in item
            )
        elif isinstance(item, (str, int, float, bool)) or item is None:
            frozen[key.strip()] = item
        else:
            raise AccountProfileError(
                f"{field_name} values must be JSON-compatible scalars, "
                f"mappings, or arrays"
            )
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class BacktestAccountProfileVersion:
    """One immutable account-profile configuration version.

    ``(profile_id, version)`` is the unique identity.  ``display_name``
    is presentation-only and never a resolution identifier.  The fee
    schedule is referenced by pinned ``key + version``, never by "the
    current schedule of this key".
    """

    profile_id: UUID
    version: int
    display_name: str
    fee_schedule_key: str
    fee_schedule_version: int
    applicability: Mapping[str, str] = MappingProxyType({})
    config_snapshot: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _profile_id(self.profile_id))
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version <= 0
        ):
            raise AccountProfileError("account profile version must be a positive integer")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise AccountProfileError(
                "account profile display_name must be non-blank text"
            )
        object.__setattr__(self, "display_name", self.display_name.strip())
        if not isinstance(self.fee_schedule_key, str) or not self.fee_schedule_key.strip():
            raise AccountProfileError(
                "account profile fee_schedule_key must be non-blank text"
            )
        object.__setattr__(self, "fee_schedule_key", self.fee_schedule_key.strip())
        if (
            isinstance(self.fee_schedule_version, bool)
            or not isinstance(self.fee_schedule_version, int)
            or self.fee_schedule_version <= 0
        ):
            raise AccountProfileError(
                "account profile fee_schedule_version must be a positive integer"
            )
        object.__setattr__(
            self, "applicability", _metadata(self.applicability, "applicability")
        )
        object.__setattr__(
            self, "config_snapshot", _frozen_json(self.config_snapshot, "config_snapshot")
        )

    @property
    def identity(self) -> tuple[UUID, int]:
        return (self.profile_id, self.version)


@dataclass(frozen=True, slots=True)
class AccountProfileAvailability:
    """Operational state bound to exactly one profile version."""

    profile_id: UUID
    version: int
    status: AccountProfileLifecycle

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _profile_id(self.profile_id))
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version <= 0
        ):
            raise AccountProfileError("version must be a positive integer")
        object.__setattr__(self, "status", _lifecycle(self.status))


class AccountResolutionLayer(StrEnum):
    """Resolution layers in fixed evaluation order."""

    EXPLICIT_SELECTION = "explicit_selection"
    STRATEGY_DEFAULT = "strategy_default"
    USER_DEFAULT = "user_default"


class AccountDefaultScope(StrEnum):
    """Scope of a stored default account reference."""

    STRATEGY = "strategy"
    USER = "user"


@dataclass(frozen=True, slots=True)
class AccountDefaultReference:
    """A stored default pinned to ``profile_id + version``.

    A default never means "latest version": the pinned pair is resolved
    literally, and a stale pin fails classification instead of drifting.
    """

    scope: AccountDefaultScope
    profile_id: UUID
    version: int

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "scope", AccountDefaultScope(self.scope))
        except ValueError as exc:
            raise AccountProfileError("default scope must be strategy or user") from exc
        object.__setattr__(self, "profile_id", _profile_id(self.profile_id))
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version <= 0
        ):
            raise AccountProfileError("default version must be a positive integer")


#: Resolution order of one run: explicit choice wins over both defaults.
_RESOLUTION_ORDER: tuple[AccountResolutionLayer, ...] = (
    AccountResolutionLayer.EXPLICIT_SELECTION,
    AccountResolutionLayer.STRATEGY_DEFAULT,
    AccountResolutionLayer.USER_DEFAULT,
)


@dataclass(frozen=True, slots=True)
class AccountResolutionCandidate:
    """Audit record of one evaluated resolution layer."""

    layer: AccountResolutionLayer
    configured: bool
    profile_id: UUID | None
    version: int | None
    status: AccountProfileLifecycle | None
    outcome: str
    failure_code: str | None = None
    failure_message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "configured": self.configured,
            "profile_id": str(self.profile_id) if self.profile_id else None,
            "version": self.version,
            "status": self.status.value if self.status else None,
            "outcome": self.outcome,
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
        }


@dataclass(frozen=True, slots=True)
class AccountResolutionAudit:
    """Complete, persistable trail of one resolution attempt."""

    run_mode: AccountRunMode
    candidates: tuple[AccountResolutionCandidate, ...]
    hit_layer: AccountResolutionLayer | None
    resolved_profile_id: UUID | None
    resolved_version: int | None
    failure_code: str | None = None
    failure_message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_mode": self.run_mode.value,
            "candidates": [candidate.to_payload() for candidate in self.candidates],
            "hit_layer": self.hit_layer.value if self.hit_layer else None,
            "resolved_profile_id": (
                str(self.resolved_profile_id) if self.resolved_profile_id else None
            ),
            "resolved_version": self.resolved_version,
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
        }


@dataclass(frozen=True, slots=True)
class ResolvedAccountSelection:
    """Frozen outcome of one successful resolution.

    The binding carries the exact profile version, its availability at
    resolution time, and the complete fee-schedule snapshot pulled from
    the registry.  Later configuration or availability changes cannot
    alter a historical binding; ``selection_hash`` proves it.
    """

    audit: AccountResolutionAudit
    profile_version: BacktestAccountProfileVersion
    availability: AccountProfileAvailability
    fee_schedule: FeeScheduleSnapshot
    selection_hash: str = field(init=False)

    def __post_init__(self) -> None:
        digest_input = {
            "audit": _canonical(self.audit.to_payload()),
            "profile_version": _canonical(
                {
                    "profile_id": self.profile_version.profile_id,
                    "version": self.profile_version.version,
                    "display_name": self.profile_version.display_name,
                    "fee_schedule_key": self.profile_version.fee_schedule_key,
                    "fee_schedule_version": (
                        self.profile_version.fee_schedule_version
                    ),
                    "applicability": self.profile_version.applicability,
                    "config_snapshot": self.profile_version.config_snapshot,
                }
            ),
            "availability_status": self.availability.status.value,
            "fee_schedule": _canonical(
                {
                    "key": self.fee_schedule.key,
                    "version": self.fee_schedule.version,
                    "metadata": self.fee_schedule.metadata,
                    "fee_rules": [
                        {
                            "key": rule.key,
                            "category": rule.category,
                            "side": rule.side,
                            "rate": format(rule.rate, "f"),
                            "minimum": format(rule.minimum, "f"),
                            "fixed_amount": format(rule.fixed_amount, "f"),
                            "base_measure": getattr(rule.base_measure, "value", rule.base_measure),
                            "rule_type": getattr(rule.rule_type, "value", rule.rule_type),
                            "currency": rule.currency,
                            "rounding_level": (
                                getattr(rule.rounding_level, "value", rule.rounding_level)
                                if rule.rounding_level is not None
                                else None
                            ),
                            "rounding_scope": rule.rounding_scope,
                            "rounding_mode": (
                                getattr(rule.rounding_mode, "value", rule.rounding_mode)
                                if rule.rounding_mode is not None
                                else None
                            ),
                            "rounding_precision": (
                                format(rule.rounding_precision, "f")
                                if rule.rounding_precision is not None
                                else None
                            ),
                            "applicability": dict(rule.applicability),
                        }
                        for rule in self.fee_schedule.fee_rules
                    ],
                }
            ),
        }
        encoded = json.dumps(
            digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        object.__setattr__(self, "selection_hash", hashlib.sha256(encoded).hexdigest())


class AccountProfileVersionCatalog:
    """In-memory store of immutable account-profile versions.

    Registration appends versions; availability flips independently.
    Versions recorded as referenced by a completed run resolution can
    never be physically deleted.
    """

    def __init__(
        self, versions: Iterable[BacktestAccountProfileVersion] = ()
    ) -> None:
        self._versions: dict[tuple[UUID, int], BacktestAccountProfileVersion] = {}
        self._availability: dict[tuple[UUID, int], AccountProfileLifecycle] = {}
        self._referenced: set[tuple[UUID, int]] = set()
        for version in versions:
            self.register(version)

    def register(
        self,
        version: BacktestAccountProfileVersion,
        *,
        status: AccountProfileLifecycle | str = AccountProfileLifecycle.ACTIVE,
    ) -> None:
        """Append one immutable version; duplicate identities are rejected."""

        if not isinstance(version, BacktestAccountProfileVersion):
            raise AccountProfileError(
                "version must be a BacktestAccountProfileVersion"
            )
        key = version.identity
        if key in self._versions:
            raise AccountProfileError(
                f"account profile {version.profile_id} version "
                f"{version.version} already exists"
            )
        self._versions[key] = version
        self._availability[key] = _lifecycle(status)

    def set_availability(
        self,
        profile_id: UUID | str,
        version: int,
        status: AccountProfileLifecycle | str,
    ) -> AccountProfileAvailability:
        """Flip operational state without touching configuration content."""

        key = (_profile_id(profile_id), version)
        if key not in self._versions:
            raise AccountProfileError(
                f"account profile {key[0]} version {version} does not exist"
            )
        lifecycle = _lifecycle(status)
        self._availability[key] = lifecycle
        return AccountProfileAvailability(
            profile_id=key[0], version=version, status=lifecycle
        )

    def mark_referenced(self, profile_id: UUID | str, version: int) -> None:
        """Record that a run binding froze this version permanently."""

        self._referenced.add((_profile_id(profile_id), version))

    def is_referenced(self, profile_id: UUID | str, version: int) -> bool:
        return (_profile_id(profile_id), version) in self._referenced

    def remove(self, profile_id: UUID | str, version: int) -> None:
        """Physically delete one version — forbidden once referenced."""

        key = (_profile_id(profile_id), version)
        if key not in self._versions:
            raise AccountProfileError(
                f"account profile {key[0]} version {version} does not exist"
            )
        if key in self._referenced:
            raise AccountProfileVersionReferencedError(
                f"account profile {key[0]} version {version} is referenced "
                "by historical runs and cannot be deleted",
                audit=_empty_audit(),
            )
        del self._versions[key]
        del self._availability[key]

    def get(
        self, profile_id: UUID | str, version: int
    ) -> BacktestAccountProfileVersion:
        key = (_profile_id(profile_id), version)
        try:
            return self._versions[key]
        except KeyError as exc:
            raise AccountProfileError(
                f"account profile {key[0]} version {version} does not exist"
            ) from exc

    def availability_of(
        self, profile_id: UUID | str, version: int
    ) -> AccountProfileAvailability:
        key = (_profile_id(profile_id), version)
        if key not in self._versions:
            raise AccountProfileError(
                f"account profile {key[0]} version {version} does not exist"
            )
        return AccountProfileAvailability(
            profile_id=key[0], version=version, status=self._availability[key]
        )

    def versions_of(self, profile_id: UUID | str) -> tuple[BacktestAccountProfileVersion, ...]:
        normalized = _profile_id(profile_id)
        found = [
            version
            for (pid, _), version in self._versions.items()
            if pid == normalized
        ]
        return tuple(sorted(found, key=lambda item: item.version))


def _pad_candidates(
    candidates: list[AccountResolutionCandidate],
    refs: Mapping[AccountResolutionLayer, "AccountDefaultReference | None"],
) -> tuple[AccountResolutionCandidate, ...]:
    """Complete the audit with every declared resolution layer.

    Layers after the evaluation stop (a hit or a failure) are appended
    as ``not_evaluated`` while keeping their pinned identity, so a
    persisted trail always shows all three candidates and their
    configured references.
    """

    padded = list(candidates)
    remaining = _RESOLUTION_ORDER[len(padded):]
    for unevaluated in remaining:
        reference_lower = refs.get(unevaluated)
        padded.append(
            AccountResolutionCandidate(
                layer=unevaluated,
                configured=reference_lower is not None,
                profile_id=(
                    reference_lower.profile_id
                    if reference_lower is not None
                    else None
                ),
                version=(
                    reference_lower.version
                    if reference_lower is not None
                    else None
                ),
                status=None,
                outcome="not_evaluated",
            )
        )
    return tuple(padded)


def _empty_audit() -> AccountResolutionAudit:
    """Placeholder audit for catalog-level errors outside a resolution."""

    return AccountResolutionAudit(
        run_mode=AccountRunMode.FORMAL,
        candidates=(),
        hit_layer=None,
        resolved_profile_id=None,
        resolved_version=None,
    )


class AccountResolver:
    """Fixed-order account resolver with classified failures and audit.

    Evaluation walks ``explicit selection > strategy default > user
    default``.  An unconfigured layer falls through to the next one; a
    configured layer that fails raises immediately with the classified
    reason and the full audit trail — silent fallback to a lower layer
    after a configured-but-broken reference is precisely the behaviour
    this resolver forbids.
    """

    def __init__(
        self,
        *,
        catalog: AccountProfileVersionCatalog,
        fee_registry: FeeScheduleVersionRegistry,
    ) -> None:
        self._catalog = catalog
        self._fee_registry = fee_registry

    def resolve(
        self,
        *,
        run_mode: AccountRunMode | str,
        explicit: AccountDefaultReference | None = None,
        strategy_default: AccountDefaultReference | None = None,
        user_default: AccountDefaultReference | None = None,
        applicability_context: Mapping[str, str] | None = None,
    ) -> ResolvedAccountSelection:
        """Resolve the account for one run, freezing everything it hits."""

        try:
            mode = AccountRunMode(getattr(run_mode, "value", run_mode))
        except ValueError as exc:
            raise AccountProfileError("run_mode must be formal or test") from exc
        refs: dict[AccountResolutionLayer, AccountDefaultReference | None] = {
            AccountResolutionLayer.EXPLICIT_SELECTION: explicit,
            AccountResolutionLayer.STRATEGY_DEFAULT: strategy_default,
            AccountResolutionLayer.USER_DEFAULT: user_default,
        }
        candidates: list[AccountResolutionCandidate] = []
        for layer in _RESOLUTION_ORDER:
            reference = refs[layer]
            if reference is None:
                candidates.append(
                    AccountResolutionCandidate(
                        layer=layer,
                        configured=False,
                        profile_id=None,
                        version=None,
                        status=None,
                        outcome="not_configured",
                    )
                )
                continue
            candidate = self._evaluate(
                layer=layer,
                reference=reference,
                run_mode=mode,
                applicability_context=applicability_context or {},
            )
            candidates.append(candidate)
            if candidate.outcome == "hit":
                audit = AccountResolutionAudit(
                    run_mode=mode,
                    candidates=_pad_candidates(candidates, refs),
                    hit_layer=layer,
                    resolved_profile_id=candidate.profile_id,
                    resolved_version=candidate.version,
                )
                return self._freeze(audit=audit, reference=reference)
            raise self._candidate_error(
                candidate,
                run_mode=mode,
                # A failed resolution carries every declared layer too:
                # lower configured layers keep their pinned identity and
                # only their evaluation is marked as skipped.
                candidates=_pad_candidates(candidates, refs),
            )
        audit = AccountResolutionAudit(
            run_mode=mode,
            candidates=tuple(candidates),
            hit_layer=None,
            resolved_profile_id=None,
            resolved_version=None,
            failure_code="account_not_selected",
            failure_message=(
                "no explicit selection and no usable default were configured"
            ),
        )
        raise AccountNotSelectedError(
            "an account must be explicitly selected or a configured "
            "default must resolve before run creation",
            audit=audit,
        )

    def _candidate_error(
        self,
        candidate: AccountResolutionCandidate,
        *,
        run_mode: AccountRunMode,
        candidates: tuple[AccountResolutionCandidate, ...],
    ) -> AccountResolutionError:
        audit = AccountResolutionAudit(
            run_mode=run_mode,
            candidates=candidates,
            hit_layer=None,
            resolved_profile_id=candidate.profile_id,
            resolved_version=candidate.version,
            failure_code=candidate.failure_code,
            failure_message=candidate.failure_message,
        )
        error_classes = {
            "account_version_not_found": AccountProfileVersionNotFoundError,
            "account_version_unavailable": AccountProfileUnavailableError,
            "account_not_applicable": AccountNotApplicableError,
            "zero_cost_formal_forbidden": ZeroCostFormalForbiddenError,
            "fee_schedule_version_missing": FeeScheduleVersionMissingError,
        }
        # A configured-but-invalid reference fails hard: falling through
        # would silently swap the operator's chosen account.
        error_class = error_classes.get(
            candidate.failure_code or "", AccountResolutionError
        )
        assert candidate.failure_message is not None
        return error_class(candidate.failure_message, audit=audit)

    def _evaluate(
        self,
        *,
        layer: AccountResolutionLayer,
        reference: AccountDefaultReference,
        run_mode: AccountRunMode,
        applicability_context: Mapping[str, str],
    ) -> AccountResolutionCandidate:
        """Classify one configured reference without raising."""

        def failed(
            code: str,
            message: str,
            *,
            status: AccountProfileLifecycle | None = None,
        ) -> AccountResolutionCandidate:
            return AccountResolutionCandidate(
                layer=layer,
                configured=True,
                profile_id=reference.profile_id,
                version=reference.version,
                status=status,
                outcome="failed",
                failure_code=code,
                failure_message=message,
            )

        try:
            version = self._catalog.get(reference.profile_id, reference.version)
        except AccountProfileError as exc:
            return failed("account_version_not_found", str(exc))
        availability = self._catalog.availability_of(
            reference.profile_id, reference.version
        )
        if availability.status is not AccountProfileLifecycle.ACTIVE:
            return failed(
                "account_version_unavailable",
                f"account profile {reference.profile_id} version "
                f"{reference.version} is {availability.status.value}; a "
                "configured reference must not silently fall back",
                status=availability.status,
            )
        incompatible = [
            (key, expected)
            for key, expected in version.applicability.items()
            if applicability_context.get(key) != expected
        ]
        if incompatible:
            return failed(
                "account_not_applicable",
                f"account profile {reference.profile_id} version "
                f"{reference.version} is incompatible with the run context "
                f"for {incompatible[0][0]!r}",
                status=availability.status,
            )
        if (
            version.fee_schedule_key == ZERO_COST_SCHEDULE_KEY
            and run_mode is not AccountRunMode.TEST
        ):
            return failed(
                "zero_cost_formal_forbidden",
                f"the {ZERO_COST_SCHEDULE_KEY}@{version.fee_schedule_version} "
                "schedule may be referenced by test runs only",
                status=availability.status,
            )
        try:
            fee_snapshot = self._fee_registry.get(
                version.fee_schedule_key, version.fee_schedule_version
            )
        except FeeError as exc:
            return failed(
                "fee_schedule_version_missing",
                str(exc),
                status=availability.status,
            )
        if run_mode is AccountRunMode.FORMAL:
            try:
                fee_snapshot.validate_for_run()
            except FeeError as exc:
                return failed(
                    "fee_schedule_version_missing",
                    str(exc),
                    status=availability.status,
                )
        return AccountResolutionCandidate(
            layer=layer,
            configured=True,
            profile_id=reference.profile_id,
            version=reference.version,
            status=availability.status,
            outcome="hit",
        )

    def _freeze(
        self,
        *,
        audit: AccountResolutionAudit,
        reference: AccountDefaultReference,
    ) -> ResolvedAccountSelection:
        version = self._catalog.get(reference.profile_id, reference.version)
        availability = self._catalog.availability_of(
            reference.profile_id, reference.version
        )
        fee_snapshot = self._fee_registry.get(
            version.fee_schedule_key, version.fee_schedule_version
        )
        # A successful resolution permanently protects the version from
        # physical deletion; the run boundary owns its own frozen copies.
        self._catalog.mark_referenced(version.profile_id, version.version)
        return ResolvedAccountSelection(
            audit=audit,
            profile_version=version,
            availability=availability,
            fee_schedule=fee_snapshot,
        )


def _audit_with_candidates(
    audit: AccountResolutionAudit,
    candidates: tuple[AccountResolutionCandidate, ...],
) -> AccountResolutionAudit:
    """Rebuild the audit with every evaluated candidate attached."""

    return AccountResolutionAudit(
        run_mode=audit.run_mode,
        candidates=candidates,
        hit_layer=audit.hit_layer,
        resolved_profile_id=audit.resolved_profile_id,
        resolved_version=audit.resolved_version,
        failure_code=audit.failure_code,
        failure_message=audit.failure_message,
    )


# Registry re-export: resolvers always need one, and importing it from the
# fees module directly is the same object contract.
__all__.append("FeeScheduleVersionRegistry")
