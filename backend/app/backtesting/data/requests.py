"""Request layering, shared value objects, and point-in-time query DTOs.

This module owns the boundary between an *unresolved run intent*
(:class:`DataPreflightRequest`) and a *frozen official run request*
(:class:`DataRequest`).  Callers never choose the final ``calendar_ids`` or
``timezone`` themselves: preflight resolves them and the frozen
:class:`DataRequest` carries only resolved values.

The first data-contract version pins ``data_contract_version = 1``,
``max_lookback_sessions = 512``, ``engine_price_basis = raw``, the
``strict_compatible@1`` calendar-axis policy, and the
``fixed_trading_sessions@1`` chunk policy with 20 sessions per chunk.

Nothing here imports ORM, database session, FastAPI, or Tushare types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Iterable, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.backtesting.data.errors import (
    DataCutoffExceededError,
    DataCutoffRequiredError,
    DataPreflightBlockedError,
    DataPreflightConfirmationMismatchError,
    InternalPreflightFixtureMissingError,
    InternalPreflightFixtureOutOfScopeError,
    InternalPreflightProfileMismatchError,
    InvalidDataRequestError,
    TradingStatusCapabilityRequirementMismatchError,
    LookbackSessionsLimitExceededError,
    UniversePreflightHashMismatchError,
)
from app.backtesting.domain import _aware_datetime
# Imported from the dependency-free references module, not
# app.instruments.domain: this edge is what breaks the instruments <->
# backtesting import cycle (see app/instruments/references.py).
from app.instruments.references import VersionedReference

__all__ = [
    "CALENDAR_AXIS_POLICY",
    "CHUNK_POLICY",
    "DATA_CONTRACT_VERSION",
    "MAX_LOOKBACK_SESSIONS",
    "AdjustedSeriesQuery",
    "BarQuery",
    "ConsistencyMode",
    "ContractRef",
    "CorporateActionQuery",
    "CoverageQuery",
    "DataCapability",
    "CapabilityAvailability",
    "InternalFixtureCapability",
    "DataChunkQuery",
    "DataPreflightRequest",
    "DataRequest",
    "DataValueQuery",
    "DateRange",
    "EffectiveDateRange",
    "CoverageQualificationRequest",
    "InstrumentCoverageQualification",
    "CoverageQualificationResult",
    "InternalFixture",
    "PreflightFixture",
    "InternalPreflightFixture",
    "PreflightProfile",
    "PreflightProfileRegistry",
    "DEFAULT_PREFLIGHT_PROFILE_REGISTRY",
    "get_preflight_profile",
    "registered_preflight_profiles",
    "CapabilitySource",
    "FORMAL_PROFILE",
    "FORMAL_PROFILE_REF",
    "INTERNAL_PROFILE",
    "FORMAL_PROFILE_KEY",
    "FORMAL_PROFILE_VERSION",
    "FORMAL_RUN_KIND",
    "INTERNAL_LINK_ACCEPTANCE_PROFILE",
    "INTERNAL_LINK_ACCEPTANCE_PROFILE_REF",
    "INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY",
    "INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION",
    "INTERNAL_LINK_ACCEPTANCE_RUN_KIND",
    "INTERNAL_FIXTURE_CAPABILITIES",
    "FIXTURE_SOURCE",
    "fixed_instrument_ids",
    "InstrumentMappingQuery",
    "InstrumentQuery",
    "InstrumentScopeMode",
    "IssueSeverity",
    "LookbackWindow",
    "MarketScope",
    "PitSupport",
    "PriceBasis",
    "QualityMode",
    "QualityStatus",
    "QueryBoundary",
    "derive_cutoff_local_date",
    "TickQuery",
    "TradingRuleQuery",
    "TradingStatusQuery",
    "UniverseQuery",
    "UniverseQueryPolicy",
    "QualificationPolicyRef",
]


# ---------------------------------------------------------------------------
# Version-pinned constants (single source of truth)
# ---------------------------------------------------------------------------

DATA_CONTRACT_VERSION = 1
"""Machine version of the generic data contract implemented by this package."""

MAX_LOOKBACK_SESSIONS = 512
"""First-version maximum for one lookback window, in trading sessions.

The limit is enforced before any data is read.  This constant is re-exported
by ``app.strategy_protocol.contract``; it must be defined exactly once.
"""

CALENDAR_AXIS_POLICY = VersionedReference(key="strict_compatible", version=1)
"""The only calendar-axis policy allowed in data-contract version 1."""

CHUNK_POLICY = VersionedReference(key="fixed_trading_sessions", version=1)
"""The only chunk policy allowed in data-contract version 1."""


# Versioned policy references are reused from the canonical instrument domain
# so that ``key``/``version`` has exactly one implementation everywhere.
ContractRef = VersionedReference
# The qualification policy is a versioned contract reference.  Keeping an
# alias makes the dynamic-universe boundary explicit without introducing a
# second reference implementation.
QualificationPolicyRef = ContractRef


# Profile/run-kind constants are deliberately defined at the generic request
# boundary.  A caller may select a profile, but it cannot invent a new
# profile/run-kind pair that the server has not registered.
FORMAL_PROFILE_KEY = "formal"
FORMAL_PROFILE_VERSION = 1
FORMAL_RUN_KIND = "backtest_run"
INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY = "internal_link_acceptance"
INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION = 1
INTERNAL_LINK_ACCEPTANCE_RUN_KIND = "internal_link_acceptance"
FIXTURE_SOURCE = "internal_fixture"

FORMAL_PROFILE = ContractRef(FORMAL_PROFILE_KEY, FORMAL_PROFILE_VERSION)
INTERNAL_LINK_ACCEPTANCE_PROFILE = ContractRef(
    INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY, INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION
)
FORMAL_PROFILE_REF = FORMAL_PROFILE
INTERNAL_PROFILE = INTERNAL_LINK_ACCEPTANCE_PROFILE
INTERNAL_LINK_ACCEPTANCE_PROFILE_REF = INTERNAL_LINK_ACCEPTANCE_PROFILE


def _normalize_profile_ref(value: object, field_name: str) -> ContractRef:
    """Normalize a profile reference without accepting the ``latest`` alias."""

    if isinstance(value, PreflightProfile):
        reference = value.reference
    elif isinstance(value, ContractRef):
        reference = value
    elif isinstance(value, str):
        text = value.strip()
        if "@" not in text:
            raise InvalidDataRequestError(
                f"{field_name} must use an exact key@version reference"
            )
        key, raw_version = text.rsplit("@", 1)
        try:
            version = int(raw_version)
        except ValueError as exc:
            raise InvalidDataRequestError(
                f"{field_name} must use an integer version, never latest"
            ) from exc
        reference = ContractRef(key=key, version=version)
    else:
        raise InvalidDataRequestError(
            f"{field_name} must be a ContractRef or exact key@version text"
        )
    return reference


def _normalize_fixture_capability(value: object, field_name: str = "capability") -> str:
    """Return one of the four named internal fixture capabilities."""

    if isinstance(value, InternalFixtureCapability):
        return value.value
    if not isinstance(value, str) or not value.strip():
        raise InvalidDataRequestError(f"{field_name} must be a named capability")
    aliases = {
        "quantity_action_integrity": InternalFixtureCapability.QUANTITY_ACTION_COVERAGE.value,
        "quantity_corporate_action_coverage": InternalFixtureCapability.QUANTITY_ACTION_COVERAGE.value,
        "trading_status_fact": InternalFixtureCapability.TRADING_STATUS.value,
        "source_revisions": InternalFixtureCapability.SOURCE_REVISION_AUDIT.value,
        "revision_audit": InternalFixtureCapability.SOURCE_REVISION_AUDIT.value,
        "transitional_repeatable_read_capability": InternalFixtureCapability.TRANSITIONAL_REPEATABLE_READ.value,
    }
    normalized = value.strip()
    normalized = aliases.get(normalized, normalized)
    if normalized not in INTERNAL_FIXTURE_CAPABILITIES:
        raise InvalidDataRequestError(
            f"{field_name} is not one of the four named internal fixture capabilities",
            details={"capability": normalized},
        )
    return normalized


def _sha256_digest(value: object, field_name: str) -> str:
    """Validate a lowercase SHA-256 digest used for fixture evidence."""

    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise InvalidDataRequestError(
            f"{field_name} must be a lowercase SHA-256 hex digest"
        )
    return value


def _sensitive_fixture_key(value: object) -> str | None:
    """Find credential-like keys before fixture evidence is frozen."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and any(
                word in key.lower()
                for word in ("token", "secret", "password", "credential", "api_key", "authorization")
            ):
                return key
            found = _sensitive_fixture_key(item)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _sensitive_fixture_key(item)
            if found is not None:
                return found
    return None


def _fixture_json_value(value: object) -> object:
    """Thaw fixture evidence into ordinary JSON-compatible containers."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _fixture_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fixture_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PreflightProfile:
    """Versioned rules for one preflight mode.

    ``allow_fixture_only`` controls only the source of explicitly supplied
    substitute facts.  It never weakens identity, calendar, Bar, rule,
    account, or settlement gates.  The profile object therefore remains a
    policy descriptor rather than a second preflight implementation.
    """

    key: str
    version: int
    run_kind: str
    allow_fixture_only: bool = False
    allow_degraded: bool = False
    allowed_fixture_capabilities: tuple[str, ...] = ()
    allowed_fixture_references: tuple[ContractRef, ...] = ()
    allowed_consistency_modes: tuple[ConsistencyMode, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _non_blank_text(self.key, "profile key"))
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise InvalidDataRequestError("profile version must be a positive integer")
        object.__setattr__(self, "run_kind", _non_blank_text(self.run_kind, "run_kind"))
        if not isinstance(self.allow_fixture_only, bool):
            raise InvalidDataRequestError("allow_fixture_only must be a boolean")
        if not isinstance(self.allow_degraded, bool):
            raise InvalidDataRequestError("allow_degraded must be a boolean")
        if self.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY:
            if self.version != INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION:
                raise InvalidDataRequestError(
                    "internal_link_acceptance profile is fixed at version 1"
                )
            if self.run_kind != INTERNAL_LINK_ACCEPTANCE_RUN_KIND:
                raise InvalidDataRequestError(
                    "internal_link_acceptance@1 has a fixed run kind"
                )
            if self.allow_degraded:
                raise InvalidDataRequestError(
                    "internal_link_acceptance@1 forbids degraded status"
                )
        if self.key == FORMAL_PROFILE_KEY:
            if self.version != FORMAL_PROFILE_VERSION or self.run_kind != FORMAL_RUN_KIND:
                raise InvalidDataRequestError(
                    "formal@1 has a fixed version and run kind"
                )
        capabilities = tuple(
            sorted(
                {
                    _normalize_fixture_capability(item, "allowed_fixture_capabilities")
                    for item in self.allowed_fixture_capabilities
                }
            )
        )
        if not self.allow_fixture_only and capabilities:
            raise InvalidDataRequestError(
                "a profile that rejects fixtures cannot allow fixture capabilities"
            )
        object.__setattr__(self, "allowed_fixture_capabilities", capabilities)
        references = tuple(self.allowed_fixture_references)
        if any(not isinstance(item, ContractRef) for item in references):
            raise InvalidDataRequestError(
                "allowed_fixture_references entries must be ContractRef"
            )
        object.__setattr__(
            self,
            "allowed_fixture_references",
            tuple(sorted(set(references), key=lambda item: (item.key, item.version))),
        )
        modes = tuple(self.allowed_consistency_modes)
        if any(not isinstance(item, ConsistencyMode) for item in modes):
            raise InvalidDataRequestError(
                "allowed_consistency_modes entries must be ConsistencyMode"
            )
        object.__setattr__(
            self, "allowed_consistency_modes", tuple(sorted(set(modes), key=lambda item: item.value))
        )

    @property
    def reference(self) -> ContractRef:
        """The stable key/version pointer used in request and hash payloads."""

        return ContractRef(self.key, self.version)

    @property
    def profile(self) -> str:
        """Human-readable key@version identifier (still machine stable)."""

        return f"{self.key}@{self.version}"

    def accepts_fixture(self, fixture: "InternalFixture") -> bool:
        """Whether this profile accepts one explicitly injected fixture."""

        if not self.allow_fixture_only or not isinstance(fixture, InternalFixture):
            return False
        if self.allowed_fixture_capabilities and fixture.capability not in self.allowed_fixture_capabilities:
            return False
        references = self.allowed_fixture_references
        if (
            not references
            and self.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY
            and self.version == INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION
        ):
            references = tuple(
                ContractRef(item, 1) for item in INTERNAL_FIXTURE_CAPABILITIES
            )
        if references:
            if fixture.version_number < 1:
                return False
            if ContractRef(fixture.fixture_key, fixture.version_number) not in references:
                return False
        # A profile with no reference allow-list deliberately delegates exact
        # fixture-key registration to its owning provider/fixture catalog; it
        # still requires a non-blank key, exact version, and one of the four
        # named capabilities above.
        return True

    def validate_fixture(
        self,
        fixture: "InternalFixture",
        request: "CoverageQualificationRequest | None" = None,
    ) -> None:
        """Raise a stable request error when a fixture is not admissible."""

        if not isinstance(fixture, InternalFixture) or not self.accepts_fixture(fixture):
            raise InternalPreflightFixtureMissingError(
                "fixture is not registered for the selected preflight profile",
                details={
                    "reason_code": "internal_preflight_fixture_missing",
                    "profile_key": self.key,
                    "profile_version": self.version,
                    "fixture_key": getattr(fixture, "fixture_key", None),
                    "fixture_version": getattr(fixture, "fixture_version", None),
                },
            )
        if request is not None and not fixture.covers(request):
            raise InternalPreflightFixtureOutOfScopeError(
                "fixture does not fully cover the qualification request",
                details={
                    "reason_code": "internal_preflight_fixture_out_of_scope",
                    "fixture_key": fixture.fixture_key,
                    "fixture_version": fixture.fixture_version,
                    "instrument_id": str(request.instrument_id),
                },
            )

    def validate_request(
        self,
        request: "CoverageQualificationRequest",
    ) -> None:
        """Validate profile ownership and every fixture attached to a request."""

        if not isinstance(request, CoverageQualificationRequest):
            raise InvalidDataRequestError(
                "request must be a CoverageQualificationRequest"
            )
        if request.preflight_profile != self.reference:
            raise InternalPreflightProfileMismatchError(
                "request profile does not match the selected profile",
                details={"reason_code": "internal_preflight_profile_mismatch"},
            )
        if request.run_kind != self.run_kind:
            raise InternalPreflightProfileMismatchError(
                "request run_kind is not fixed by the selected profile",
                details={
                    "reason_code": "internal_preflight_profile_mismatch",
                    "expected": self.run_kind,
                    "actual": request.run_kind,
                },
            )
        if self.key == FORMAL_PROFILE_KEY and request.fixtures:
            raise InternalPreflightProfileMismatchError(
                "formal@1 rejects fixture_only facts",
                details={"reason_code": "formal_fixture_not_allowed"},
            )
        for fixture in request.fixtures:
            self.validate_fixture(fixture, request)

    @property
    def is_internal(self) -> bool:
        """Whether this profile is the Phase-2a internal profile."""

        return self.reference == INTERNAL_LINK_ACCEPTANCE_PROFILE

    @property
    def allows_fixtures(self) -> bool:
        """Descriptive alias for the fixture-only policy switch."""

        return self.allow_fixture_only

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe, deterministic profile descriptor."""

        return {
            "key": self.key,
            "version": self.version,
            "run_kind": self.run_kind,
            "allow_fixture_only": self.allow_fixture_only,
            "allow_degraded": self.allow_degraded,
            "allowed_fixture_capabilities": list(self.allowed_fixture_capabilities),
            "allowed_fixture_references": [
                {"key": item.key, "version": item.version}
                for item in self.allowed_fixture_references
            ],
            "allowed_consistency_modes": [item.value for item in self.allowed_consistency_modes],
        }


@dataclass(frozen=True, slots=True)
class CoverageQualificationRequest:
    """Bounded single-instrument request consumed by a qualification port.

    The three envelopes are explicit even when a caller has no warmup or
    historical lookback.  This prevents an implementation from deriving a
    hidden range from a wall clock or from a provider's current catalogue.
    ``requested_window`` is the public/formal interval and
    ``effective_date`` is the point-in-time identity/rule date.
    """

    instrument_id: UUID
    effective_date: date
    requested_window: DateRange
    formal_envelope: DateRange
    warmup_envelope: DateRange | None
    history_envelope: DateRange | None
    required_capabilities: tuple[DataCapability, ...]
    query_boundary: QueryBoundary
    preflight_profile: ContractRef | str
    resolved_calendar_ids: tuple[str, ...]
    run_kind: str | None = None
    rule_package: ContractRef | None = None
    rule_exception_set: ContractRef | None = None
    market_scope: MarketScope | None = None
    universe_query_policy: UniverseQueryPolicy | None = None
    qualification_policy_version: ContractRef | None = None
    required_fixture_capabilities: tuple[str, ...] = ()
    fixtures: tuple["InternalFixture", ...] = ()
    frequency: str = "1d"

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise InvalidDataRequestError("instrument_id must be a UUID")
        object.__setattr__(self, "effective_date", _plain_date(self.effective_date, "effective_date"))
        for field_name in ("requested_window", "formal_envelope"):
            if not isinstance(getattr(self, field_name), DateRange):
                raise InvalidDataRequestError(f"{field_name} must be a DateRange")
        if self.formal_envelope.start_date > self.requested_window.start_date or self.formal_envelope.end_date < self.requested_window.end_date:
            raise InvalidDataRequestError(
                "formal_envelope must fully contain requested_window"
            )
        for field_name in ("warmup_envelope", "history_envelope"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, DateRange):
                raise InvalidDataRequestError(f"{field_name} must be a DateRange or None")
        if not isinstance(self.required_capabilities, Iterable) or isinstance(
            self.required_capabilities, (str, bytes)
        ):
            raise InvalidDataRequestError("required_capabilities must be an iterable")
        object.__setattr__(
            self,
            "required_capabilities",
            _sorted_unique_enum(self.required_capabilities, DataCapability, "required_capabilities"),
        )
        if not self.required_capabilities:
            raise InvalidDataRequestError("required_capabilities must not be empty")
        if not isinstance(self.query_boundary, QueryBoundary):
            raise InvalidDataRequestError("query_boundary must be a QueryBoundary")
        self.query_boundary.require_not_past_cutoff(self.effective_date, "effective_date")
        object.__setattr__(
            self,
            "preflight_profile",
            _normalize_profile_ref(self.preflight_profile, "preflight_profile"),
        )
        profile = get_preflight_profile(self.preflight_profile)
        if self.run_kind is None:
            object.__setattr__(self, "run_kind", profile.run_kind)
        elif self.run_kind != profile.run_kind:
            raise InvalidDataRequestError(
                "run_kind must be fixed by the selected preflight profile",
                details={
                    "reason_code": "internal_preflight_profile_mismatch",
                    "expected": profile.run_kind,
                    "actual": self.run_kind,
                },
            )
        if isinstance(self.resolved_calendar_ids, (str, bytes)) or not isinstance(
            self.resolved_calendar_ids, Iterable
        ):
            raise InvalidDataRequestError(
                "resolved_calendar_ids must be an iterable of canonical calendar ids"
            )
        normalized_calendar_ids: list[str] = []
        try:
            from app.backtesting.calendar_axis import normalize_calendar_id

            normalized_calendar_ids = sorted(
                {normalize_calendar_id(item) for item in self.resolved_calendar_ids}
            )
        except Exception as exc:
            raise InvalidDataRequestError(
                "resolved_calendar_ids must contain canonical calendar ids"
            ) from exc
        if not normalized_calendar_ids:
            raise InvalidDataRequestError("resolved_calendar_ids must not be empty")
        object.__setattr__(self, "resolved_calendar_ids", tuple(normalized_calendar_ids))
        for field_name in (
            "rule_package",
            "rule_exception_set",
            "qualification_policy_version",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, ContractRef):
                raise InvalidDataRequestError(f"{field_name} must be a ContractRef when provided")
        if self.market_scope is not None and not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope when provided")
        if self.universe_query_policy is not None and not isinstance(
            self.universe_query_policy, UniverseQueryPolicy
        ):
            raise InvalidDataRequestError(
                "universe_query_policy must be a UniverseQueryPolicy when provided"
            )
        fixture_caps = tuple(
            sorted(
                {
                    _normalize_fixture_capability(item, "required_fixture_capabilities")
                    for item in self.required_fixture_capabilities
                }
            )
        )
        object.__setattr__(self, "required_fixture_capabilities", fixture_caps)
        fixtures = tuple(self.fixtures)
        if any(not isinstance(item, InternalFixture) for item in fixtures):
            raise InvalidDataRequestError(
                "fixtures entries must be InternalFixture instances"
            )
        object.__setattr__(self, "fixtures", tuple(sorted(fixtures, key=lambda item: (item.capability, item.fixture_key, str(item.fixture_version)))))
        object.__setattr__(self, "frequency", _non_blank_text(self.frequency, "frequency"))

    @property
    def preflight_profile_ref(self) -> ContractRef:
        """Alias retained for code that uses an explicit ``*_ref`` suffix."""

        return self.preflight_profile

    @property
    def profile(self) -> PreflightProfile:
        """Resolve the exact registered profile used by this request."""

        return get_preflight_profile(self.preflight_profile)

    @property
    def preflight_profile_key(self) -> str:
        """Stable profile key projection for report adapters."""

        return self.preflight_profile.key

    @property
    def preflight_profile_version(self) -> int:
        """Stable profile version projection for report adapters."""

        return self.preflight_profile.version

    @property
    def formal_window(self) -> DateRange:
        """Compatibility alias for the formal envelope."""

        return self.formal_envelope

    @property
    def historical_envelope(self) -> DateRange | None:
        """Compatibility alias for the history envelope."""

        return self.history_envelope

    @property
    def lookback_envelope(self) -> DateRange | None:
        """Compatibility alias used by older qualification callers."""

        return self.history_envelope

    def machine_content(self) -> dict[str, object]:
        """Return business inputs used by qualification hashing."""

        def _range(value: DateRange | None) -> dict[str, str] | None:
            return (
                {"start_date": value.start_date.isoformat(), "end_date": value.end_date.isoformat()}
                if value is not None
                else None
            )

        return {
            "instrument_id": str(self.instrument_id),
            "effective_date": self.effective_date,
            "requested_window": _range(self.requested_window),
            "formal_envelope": _range(self.formal_envelope),
            "warmup_envelope": _range(self.warmup_envelope),
            "history_envelope": _range(self.history_envelope),
            "required_capabilities": [item.value for item in self.required_capabilities],
            "query_boundary": {
                "data_cutoff": self.query_boundary.data_cutoff,
                "knowledge_as_of": self.query_boundary.knowledge_as_of,
                "include_cutoff_day": self.query_boundary.include_cutoff_day,
            },
            "preflight_profile": {
                "key": self.preflight_profile.key,
                "version": self.preflight_profile.version,
            },
            "run_kind": self.run_kind,
            "resolved_calendar_ids": list(self.resolved_calendar_ids),
            "rule_package": (
                {"key": self.rule_package.key, "version": self.rule_package.version}
                if self.rule_package is not None
                else None
            ),
            "rule_exception_set": (
                {"key": self.rule_exception_set.key, "version": self.rule_exception_set.version}
                if self.rule_exception_set is not None
                else None
            ),
            "market_scope": (
                {
                    "markets": list(self.market_scope.markets),
                    "exchanges": list(self.market_scope.exchanges),
                    "asset_classes": list(self.market_scope.asset_classes),
                    "currencies": list(self.market_scope.currencies),
                }
                if self.market_scope is not None
                else None
            ),
            "universe_query_policy": (
                [
                    {"key": item.key, "version": item.version}
                    for item in self.universe_query_policy.candidate_set_rules
                ]
                if self.universe_query_policy is not None
                else None
            ),
            "qualification_policy_version": (
                {
                    "key": self.qualification_policy_version.key,
                    "version": self.qualification_policy_version.version,
                }
                if self.qualification_policy_version is not None
                else None
            ),
            "required_fixture_capabilities": list(self.required_fixture_capabilities),
            "fixtures": [item.machine_content() for item in self.fixtures],
            "frequency": self.frequency,
        }


@dataclass(frozen=True, slots=True)
class InternalFixture:
    """A bounded, named, versioned internal substitute fact.

    Fixtures are evidence for the internal-link acceptance path only.  They
    carry a stable identity/date scope and content digest; they are never
    inferred from empty tables or adapter defaults.  The explicit
    ``fixture_only=True`` marker is intentionally mandatory and formal
    profiles must reject the object regardless of its other fields.
    """

    fixture_key: str
    fixture_version: int | str
    capability: str | InternalFixtureCapability
    instrument_ids: tuple[UUID, ...] = ()
    start_date: date | None = None
    end_date: date | None = None
    proof_summary: object | None = None
    source: str = FIXTURE_SOURCE
    fixture_only: bool = True
    content_hash: str = ""
    scope: Mapping[str, object] | None = None
    proof: str | None = None
    # Derived integer version used only when a profile registry chooses to
    # constrain fixture references.  It is not accepted from callers.
    version_number: int = field(init=False, repr=False, default=-1)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_key", _non_blank_text(self.fixture_key, "fixture_key"))
        version = self.fixture_version
        if isinstance(version, bool) or not isinstance(version, (int, str)):
            raise InvalidDataRequestError("fixture_version must be an exact integer or text version")
        if isinstance(version, int):
            if version < 1:
                raise InvalidDataRequestError("fixture_version must be positive")
        else:
            version = _non_blank_text(version, "fixture_version")
            if version.lower() == "latest":
                raise InvalidDataRequestError("fixture_version must not be latest")
        object.__setattr__(self, "fixture_version", version)
        # Keep the integer form available for profile references while
        # retaining a textual version when an external fixture registry uses
        # one.  Text versions must still be exact and cannot be wildcarded.
        try:
            version_number = int(version)
        except (TypeError, ValueError):
            version_number = -1
        object.__setattr__(self, "version_number", version_number)
        capability = _normalize_fixture_capability(self.capability)
        object.__setattr__(self, "capability", capability)

        raw_ids = self.instrument_ids
        if self.scope is not None:
            if isinstance(self.scope, MarketScope):
                object.__setattr__(
                    self,
                    "scope",
                    {
                        "markets": self.scope.markets,
                        "exchanges": self.scope.exchanges,
                        "asset_classes": self.scope.asset_classes,
                        "currencies": self.scope.currencies,
                    },
                )
            elif not isinstance(self.scope, Mapping):
                raise InvalidDataRequestError("fixture scope must be a mapping")
            scope_ids = self.scope.get(
                "instrument_ids", self.scope.get("instruments", ())
            )
            if not raw_ids and scope_ids:
                raw_ids = scope_ids
        if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, Iterable):
            raise InvalidDataRequestError("fixture instrument_ids must be UUIDs")
        ids = tuple(raw_ids)
        if any(not isinstance(item, UUID) for item in ids):
            raise InvalidDataRequestError(
                "fixture instrument_ids entries must be UUIDs"
            )
        if not ids and self.scope is None:
            raise InvalidDataRequestError(
                "fixture must provide instrument_ids or a scope"
            )
        object.__setattr__(self, "instrument_ids", tuple(sorted(set(ids), key=str)))

        start = self.start_date
        end = self.end_date
        if self.scope is not None:
            date_range = self.scope.get("date_range", {})
            if not isinstance(date_range, Mapping):
                date_range = {}
            scope_start = self.scope.get("start_date", date_range.get("start_date"))
            scope_end = self.scope.get("end_date", date_range.get("end_date"))
            if start is None and isinstance(scope_start, str):
                try:
                    start = date.fromisoformat(scope_start)
                except ValueError as exc:
                    raise InvalidDataRequestError("fixture scope start_date is invalid") from exc
            if end is None and isinstance(scope_end, str):
                try:
                    end = date.fromisoformat(scope_end)
                except ValueError as exc:
                    raise InvalidDataRequestError("fixture scope end_date is invalid") from exc
        if start is None or end is None:
            raise InvalidDataRequestError("fixture start_date and end_date are required")
        start = _plain_date(start, "fixture start_date")
        end = _plain_date(end, "fixture end_date")
        if start > end:
            raise InvalidDataRequestError("fixture start_date must not be later than end_date")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)

        proof_summary = self.proof_summary if self.proof_summary is not None else self.proof
        if isinstance(proof_summary, str):
            proof_summary = _non_blank_text(proof_summary, "proof_summary")
        elif isinstance(proof_summary, Mapping):
            sensitive_key = _sensitive_fixture_key(proof_summary)
            if sensitive_key is not None:
                raise InvalidDataRequestError(
                    "proof_summary must not contain credentials or access tokens",
                    details={"field": "proof_summary", "key": sensitive_key},
                )
            from app.backtesting.data.errors import freeze_json

            try:
                proof_summary = freeze_json(dict(proof_summary), "proof_summary")
            except ValueError as exc:
                raise InvalidDataRequestError(
                    "proof_summary must contain JSON-safe values"
                ) from exc
        else:
            raise InvalidDataRequestError(
                "proof_summary must be non-blank text or a JSON mapping"
            )
        object.__setattr__(self, "proof_summary", proof_summary)
        if self.proof is not None:
            object.__setattr__(self, "proof", _non_blank_text(self.proof, "proof"))
        object.__setattr__(self, "source", _non_blank_text(self.source, "source"))
        if self.source != FIXTURE_SOURCE:
            raise InvalidDataRequestError(
                f"fixture source must be exactly {FIXTURE_SOURCE}",
                details={"source": self.source},
            )
        if self.fixture_only is not True:
            raise InvalidDataRequestError("internal fixtures must set fixture_only=True")
        object.__setattr__(self, "content_hash", _sha256_digest(self.content_hash, "content_hash"))

        if self.scope is None:
            scope = {
                "instrument_ids": [str(item) for item in self.instrument_ids],
                "start_date": self.start_date.isoformat(),
                "end_date": self.end_date.isoformat(),
            }
        else:
            # Dates and UUIDs are normalized before freeze_json so the scope
            # remains valid JSON and hashes the same way across constructors.
            scope = dict(self.scope)
            scope["instrument_ids"] = [str(item) for item in self.instrument_ids]
            scope["start_date"] = self.start_date.isoformat()
            scope["end_date"] = self.end_date.isoformat()
        sensitive_scope_key = _sensitive_fixture_key(scope)
        if sensitive_scope_key is not None:
            raise InvalidDataRequestError(
                "fixture scope must not contain credentials or access tokens",
                details={"field": "scope", "key": sensitive_scope_key},
            )
        from app.backtesting.data.errors import freeze_json

        frozen_scope = freeze_json(scope, "fixture scope")
        if not isinstance(frozen_scope, Mapping):  # pragma: no cover - defensive
            raise InvalidDataRequestError("fixture scope must be a JSON object")
        object.__setattr__(self, "scope", frozen_scope)

    def covers(self, request: object) -> bool:
        """Return whether this fixture fully contains a qualification request."""

        if not isinstance(request, CoverageQualificationRequest):
            return False
        if self.instrument_ids and request.instrument_id not in self.instrument_ids:
            return False
        if request.required_fixture_capabilities and self.capability not in request.required_fixture_capabilities:
            return False
        ranges = [request.requested_window]
        for envelope in (
            request.formal_envelope,
            request.warmup_envelope,
            request.history_envelope,
        ):
            if envelope is not None:
                ranges.append(envelope)
        return min(item.start_date for item in ranges) >= self.start_date and max(
            item.end_date for item in ranges
        ) <= self.end_date

    def machine_content(self) -> dict[str, object]:
        """Hash-relevant fixture content; no display-only text is included."""

        return {
            "fixture_key": self.fixture_key,
            "fixture_version": self.fixture_version,
            "capability": self.capability,
            "instrument_ids": [str(item) for item in self.instrument_ids],
            "start_date": self.start_date,
            "end_date": self.end_date,
            "proof_summary": self.proof_summary,
            "source": self.source,
            "fixture_only": self.fixture_only,
            "content_hash": self.content_hash,
            "scope": self.scope,
        }

    def as_dict(self) -> dict[str, object]:
        """Return the complete JSON-safe fixture audit projection."""

        payload = _fixture_json_value(self.machine_content())
        assert isinstance(payload, dict)
        return payload


# Descriptive aliases are kept as aliases so there is still one fixture
# contract and one profile registry in the data layer.
PreflightFixture = InternalFixture
InternalPreflightFixture = InternalFixture


class PreflightProfileRegistry:
    """Small immutable-by-entry registry for the two known profile modes."""

    def __init__(self, profiles: Iterable[PreflightProfile] | None = None) -> None:
        defaults = (
            PreflightProfile(
                key=FORMAL_PROFILE_KEY,
                version=FORMAL_PROFILE_VERSION,
                run_kind=FORMAL_RUN_KIND,
                allow_fixture_only=False,
                allow_degraded=True,
            ),
            PreflightProfile(
                key=INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY,
                version=INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION,
                run_kind=INTERNAL_LINK_ACCEPTANCE_RUN_KIND,
                allow_fixture_only=True,
                allow_degraded=False,
                allowed_fixture_capabilities=tuple(INTERNAL_FIXTURE_CAPABILITIES),
                allowed_fixture_references=tuple(
                    ContractRef(item, 1)
                    for item in sorted(INTERNAL_FIXTURE_CAPABILITIES)
                ),
                allowed_consistency_modes=(
                    ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
                    ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
                ),
            ),
        )
        selected = tuple(defaults if profiles is None else profiles)
        self._profiles: dict[ContractRef, PreflightProfile] = {}
        for profile in selected:
            self.register(profile)

    def register(self, profile: PreflightProfile) -> None:
        """Register one exact profile, rejecting conflicting duplicates."""

        if not isinstance(profile, PreflightProfile):
            raise InvalidDataRequestError("profile must be a PreflightProfile")
        if profile.key not in {FORMAL_PROFILE_KEY, INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY}:
            raise InvalidDataRequestError(
                "only formal@1 and internal_link_acceptance@1 are registered",
                details={"profile": profile.profile},
            )
        expected_run_kind = (
            FORMAL_RUN_KIND
            if profile.key == FORMAL_PROFILE_KEY
            else INTERNAL_LINK_ACCEPTANCE_RUN_KIND
        )
        expected_version = (
            FORMAL_PROFILE_VERSION
            if profile.key == FORMAL_PROFILE_KEY
            else INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION
        )
        if profile.version != expected_version or profile.run_kind != expected_run_kind:
            raise InvalidDataRequestError(
                "profile key/version/run_kind is not the fixed server contract",
                details={
                    "profile": profile.profile,
                    "expected_run_kind": expected_run_kind,
                    "expected_version": expected_version,
                },
            )
        if profile.key == FORMAL_PROFILE_KEY and profile.allow_fixture_only:
            raise InvalidDataRequestError(
                "formal@1 must reject fixture-only facts"
            )
        if profile.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY and (
            not profile.allow_fixture_only or profile.allow_degraded
        ):
            raise InvalidDataRequestError(
                "internal_link_acceptance@1 must allow named fixtures and reject degraded"
            )
        reference = profile.reference
        if reference in self._profiles and self._profiles[reference] != profile:
            raise InvalidDataRequestError(
                "profile key/version is already registered with another definition",
                details={"profile": profile.profile},
            )
        self._profiles[reference] = profile

    def resolve(self, value: object) -> PreflightProfile:
        """Resolve only an exact registered key/version pair."""

        reference = _normalize_profile_ref(value, "preflight_profile")
        profile = self._profiles.get(reference)
        if profile is None:
            raise InvalidDataRequestError(
                "preflight profile is not registered",
                details={"profile_key": reference.key, "profile_version": reference.version},
            )
        return profile

    def get(self, value: object) -> PreflightProfile:
        """Alias for ``resolve`` used by protocol adapters."""

        return self.resolve(value)

    def all(self) -> tuple[PreflightProfile, ...]:
        """Return registered profiles in stable key/version order."""

        return tuple(sorted(self._profiles.values(), key=lambda item: (item.key, item.version)))


# ---------------------------------------------------------------------------
# Stable enums (values are machine identifiers; never rename them)
# ---------------------------------------------------------------------------


class QualityStatus(StrEnum):
    """Fact-level quality outcome for one requested slice of data."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class PreflightStatus(StrEnum):
    """Overall admission-preflight outcome."""

    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class IssueSeverity(StrEnum):
    """Severity of one structured preflight issue."""

    WARNING = "warning"
    ERROR = "error"


class InstrumentScopeMode(StrEnum):
    """How the set of tradable instruments is determined for a run."""

    FIXED = "fixed"
    DYNAMIC = "dynamic"
    HYBRID = "hybrid"


class PriceBasis(StrEnum):
    """Which price series a query or fact refers to."""

    RAW = "raw"
    QFQ = "qfq"
    HFQ = "hfq"


class ConsistencyMode(StrEnum):
    """How a provider guarantees read consistency inside one session."""

    CHUNKED_LOGICAL_TOKEN = "chunked_logical_token"
    TRANSITIONAL_REPEATABLE_READ = "transitional_repeatable_read"


class ConsistencyValidation(StrEnum):
    """Lifecycle state of a chunk consistency validation."""

    NOT_VALIDATED = "not_validated"
    VALID = "valid"
    INVALID = "invalid"
    EXPIRED = "expired"
    COVERAGE_INCOMPLETE = "coverage_incomplete"


class PitSupport(StrEnum):
    """Point-in-time support level declared per fact capability."""

    STRICT = "strict"
    NON_STRICT = "non_strict"
    UNAVAILABLE = "unavailable"


class QualityMode(StrEnum):
    """Overall run-level data-quality policy."""

    STRICT = "strict"


class DataCapability(StrEnum):
    """Fact capabilities a provider may declare or a query may request."""

    BARS = "bars"
    RULES = "rules"
    STATUS = "status"
    ACTIONS = "actions"
    UNIVERSE = "universe"
    COVERAGE = "coverage"
    MAPPINGS = "mappings"
    CALENDARS = "calendars"
    ADJUSTED_SERIES = "adjusted_series"
    TICKS = "ticks"
    VALUES = "values"


class CapabilitySource(StrEnum):
    """Provenance class for one capability in a provider manifest.

    A capability's source is intentionally separate from whether the provider
    can serve it.  ``fixture`` and ``transitional`` are useful for the
    internal-link acceptance profile, but neither is production evidence.
    """

    PRODUCTION = "production"
    FIXTURE = "fixture"
    TRANSITIONAL = "transitional"
    UNAVAILABLE = "unavailable"


# A descriptive alias used by callers that refer to this value as an
# availability classification rather than a source classification.
CapabilityAvailability = CapabilitySource


class InternalFixtureCapability(StrEnum):
    """The only four named substitutions accepted by the internal profile."""

    QUANTITY_ACTION_COVERAGE = "quantity_action_coverage"
    TRADING_STATUS = "trading_status"
    SOURCE_REVISION_AUDIT = "source_revision_audit"
    TRANSITIONAL_REPEATABLE_READ = "transitional_repeatable_read"


INTERNAL_FIXTURE_CAPABILITIES = frozenset(
    capability.value for capability in InternalFixtureCapability
)


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def derive_cutoff_local_date(data_cutoff: datetime, timezone_name: str) -> date:
    """Derive a market-local cutoff date from a timezone-aware instant."""

    if not isinstance(data_cutoff, datetime) or data_cutoff.tzinfo is None or data_cutoff.utcoffset() is None:
        raise InvalidDataRequestError("data_cutoff must be timezone-aware")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidDataRequestError("timezone must be a resolvable IANA timezone") from exc
    return data_cutoff.astimezone(zone).date()


def __getattr__(name: str):
    """Lazily expose the calendar PIT context without an import cycle."""

    if name == "CalendarPITContext":
        from app.backtesting.calendar_axis import CalendarPITContext

        globals()[name] = CalendarPITContext
        return CalendarPITContext
    if name == "InstrumentCoverageQualification":
        # The result DTO lives beside the provider protocol.  A lazy alias is
        # kept here for callers that import request/response contracts from a
        # single module, without introducing a requests -> protocols cycle at
        # module import time.
        from app.backtesting.data.protocols import InstrumentCoverageQualification

        globals()[name] = InstrumentCoverageQualification
        return InstrumentCoverageQualification
    if name == "CoverageQualificationResult":
        from app.backtesting.data.protocols import InstrumentCoverageQualification

        globals()[name] = InstrumentCoverageQualification
        return InstrumentCoverageQualification
    raise AttributeError(name)


def _plain_date(value: object, field_name: str) -> date:
    """Require a calendar date and reject full datetimes."""

    if not isinstance(value, date) or isinstance(value, datetime):
        raise InvalidDataRequestError(f"{field_name} must be a datetime.date")
    return value


def _strict_int(value: object, field_name: str) -> int:
    """Require a plain integer; booleans are not integers in this contract."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataRequestError(f"{field_name} must be an integer")
    return value


def _non_blank_text(value: object, field_name: str) -> str:
    """Require non-blank plain text."""

    if not isinstance(value, str) or not value.strip():
        raise InvalidDataRequestError(f"{field_name} must be non-blank text")
    return value


def _sorted_unique_ids(
    value: UUID | Iterable[UUID], field_name: str
) -> tuple[UUID, ...]:
    """Accept one id or an iterable; deduplicate by stable string form."""

    if isinstance(value, UUID):
        return (value,)
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidDataRequestError(f"{field_name} must be a UUID or iterable")
    ids = tuple(value)
    if not ids:
        raise InvalidDataRequestError(f"{field_name} must not be empty")
    for instrument_id in ids:
        if not isinstance(instrument_id, UUID):
            raise InvalidDataRequestError(f"{field_name} entries must be UUIDs")
    return tuple(sorted(set(ids), key=str))


def fixed_instrument_ids(
    static_instrument_ids: Iterable[UUID] = (),
    mandatory_instrument_ids: Iterable[UUID] = (),
    non_zero_initial_position_instrument_ids: Iterable[UUID] = (),
) -> tuple[UUID, ...]:
    """Return the stable, order-independent fixed-instrument union.

    The non-zero initial-position IDs are supplied by the run-spec layer;
    keeping them as an explicit input prevents dynamic-universe selection
    from accidentally skipping opening holdings.  Empty inputs are valid for
    dynamic requests, but every supplied value must still be a UUID.
    """

    values: list[UUID] = []
    for field_name, source in (
        ("static_instrument_ids", static_instrument_ids),
        ("mandatory_instrument_ids", mandatory_instrument_ids),
        (
            "non_zero_initial_position_instrument_ids",
            non_zero_initial_position_instrument_ids,
        ),
    ):
        if isinstance(source, (str, bytes)) or not isinstance(source, Iterable):
            raise InvalidDataRequestError(
                f"{field_name} must be an iterable of UUIDs"
            )
        for instrument_id in source:
            if not isinstance(instrument_id, UUID):
                raise InvalidDataRequestError(
                    f"{field_name} entries must be UUIDs"
                )
            values.append(instrument_id)
    return tuple(sorted(set(values), key=str))


def _sorted_unique_refs(
    value: Iterable[ContractRef], field_name: str
) -> tuple[ContractRef, ...]:
    """Deduplicate versioned references and sort them by (key, version)."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidDataRequestError(
            f"{field_name} must be an iterable of ContractRef"
        )
    refs = tuple(value)
    for ref in refs:
        if not isinstance(ref, ContractRef):
            raise InvalidDataRequestError(f"{field_name} entries must be ContractRef")
    return tuple(sorted(set(refs), key=lambda ref: (ref.key, ref.version)))


def _sorted_unique_text(
    value: Iterable[str], field_name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    """Deduplicate non-blank machine labels and sort them stably."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidDataRequestError(f"{field_name} must be an iterable of strings")
    labels = tuple(_non_blank_text(item, f"{field_name} entry") for item in value)
    if not labels:
        if allow_empty:
            return ()
        raise InvalidDataRequestError(f"{field_name} must not be empty")
    return tuple(sorted(set(labels)))


def _sorted_unique_enum(
    value: Iterable, enum_type, field_name: str, *, allow_empty: bool = False
) -> tuple:
    """Deduplicate enum members and sort them by their stable value."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidDataRequestError(
            f"{field_name} must be an iterable of {enum_type.__name__}"
        )
    members = tuple(value)
    for member in members:
        if not isinstance(member, enum_type):
            raise InvalidDataRequestError(
                f"{field_name} entries must be {enum_type.__name__} members"
            )
    if not members:
        if allow_empty:
            return ()
        raise InvalidDataRequestError(f"{field_name} must not be empty")
    return tuple(sorted(set(members), key=lambda member: member.value))


# One process-wide read-only-by-convention registry is enough for this
# contract.  Providers resolve from it but never mutate it during a run.
DEFAULT_PREFLIGHT_PROFILE_REGISTRY = PreflightProfileRegistry()


def get_preflight_profile(value: object) -> PreflightProfile:
    """Resolve one of the exact server-registered profiles."""

    return DEFAULT_PREFLIGHT_PROFILE_REGISTRY.resolve(value)


def registered_preflight_profiles() -> tuple[PreflightProfile, ...]:
    """Return the stable profile registry snapshot."""

    return DEFAULT_PREFLIGHT_PROFILE_REGISTRY.all()


# ---------------------------------------------------------------------------
# Time windows and query boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DateRange:
    """An inclusive calendar-date window: both endpoints are contained."""

    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        start = _plain_date(self.start_date, "start_date")
        end = _plain_date(self.end_date, "end_date")
        if start > end:
            raise InvalidDataRequestError("start_date must not be later than end_date")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)


@dataclass(frozen=True, slots=True)
class EffectiveDateRange:
    """A half-open validity interval ``[valid_from, valid_to)``.

    ``valid_to=None`` means the interval is still open-ended.  Identity
    mappings, rules, and trading-status facts all use this semantics.
    """

    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        start = _plain_date(self.valid_from, "valid_from")
        if self.valid_to is not None:
            end = _plain_date(self.valid_to, "valid_to")
            if end <= start:
                raise InvalidDataRequestError(
                    "valid_to must be later than valid_from (half-open interval)"
                )
            object.__setattr__(self, "valid_to", end)
        object.__setattr__(self, "valid_from", start)

    def covers(self, day: date) -> bool:
        """Whether ``day`` falls inside the half-open interval."""

        day = _plain_date(day, "day")
        if day < self.valid_from:
            return False
        return self.valid_to is None or day < self.valid_to


@dataclass(frozen=True, slots=True)
class LookbackWindow:
    """A session-count window ending at one explicit cutoff instant.

    ``sessions`` must be between 1 and 512 inclusive; exceeding the limit
    fails before any data is read.  ``end_at`` must be timezone-aware.
    """

    sessions: int
    end_at: datetime

    def __post_init__(self) -> None:
        sessions = _strict_int(self.sessions, "sessions")
        if sessions <= 0:
            raise InvalidDataRequestError("sessions must be a positive integer")
        if sessions > MAX_LOOKBACK_SESSIONS:
            raise LookbackSessionsLimitExceededError(
                f"sessions {sessions} exceeds the maximum of "
                f"{MAX_LOOKBACK_SESSIONS}",
                details={"requested": sessions, "maximum": MAX_LOOKBACK_SESSIONS},
            )
        object.__setattr__(self, "sessions", sessions)
        object.__setattr__(self, "end_at", _aware_datetime(self.end_at, "end_at"))


@dataclass(frozen=True, slots=True)
class QueryBoundary:
    """The point-in-time visibility boundary carried by market-fact queries.

    ``data_cutoff`` is mandatory: no query may read facts recorded after it,
    and overflow fails instead of silently trimming.  Strict historical
    cognition additionally supplies ``knowledge_as_of``, which must not be
    later than ``data_cutoff``.  Both timestamps must be timezone-aware.

    Date-window queries need an explicit rule for the cutoff day itself,
    because a bare ``data_cutoff`` instant cannot tell whether the day's
    facts are already complete.  ``include_cutoff_day`` encodes that
    decision: the default ``False`` means the cutoff day counts as
    incomplete and any date window touching it fails; ``True`` means the
    caller has established that the whole cutoff day is readable.
    """

    data_cutoff: datetime
    knowledge_as_of: datetime | None = None
    include_cutoff_day: bool = False

    def __post_init__(self) -> None:
        cutoff = _aware_datetime(self.data_cutoff, "data_cutoff")
        object.__setattr__(self, "data_cutoff", cutoff)
        if not isinstance(self.include_cutoff_day, bool):
            raise InvalidDataRequestError(
                "include_cutoff_day must be a boolean"
            )
        if self.knowledge_as_of is not None:
            known = _aware_datetime(self.knowledge_as_of, "knowledge_as_of")
            if known > cutoff:
                raise InvalidDataRequestError(
                    "knowledge_as_of must not be later than data_cutoff"
                )
            object.__setattr__(self, "knowledge_as_of", known)

    def derive_cutoff_local_date(self, timezone_name: str) -> date:
        """Derive the frozen cutoff date in one confirmed IANA timezone."""

        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidDataRequestError(
                "timezone must be a resolvable IANA timezone"
            ) from exc
        return self.data_cutoff.astimezone(zone).date()

    @property
    def cutoff_date(self) -> date:
        """Legacy UTC-surface date retained only for diagnostics."""

        return self.data_cutoff.date()

    def require_not_past_cutoff(
        self,
        day: date,
        timezone_or_field: str,
        field_name: str | None = None,
    ) -> None:
        """Reject a date beyond the boundary instead of trimming it.

        A date strictly after ``data_cutoff``'s day always fails.  The
        cutoff day itself fails unless ``include_cutoff_day`` declares the
        whole day complete.
        """

        if field_name is None:
            timezone_name = None
            resolved_field_name = timezone_or_field
            cutoff_date = self.cutoff_date
        else:
            timezone_name = timezone_or_field
            resolved_field_name = field_name
            cutoff_date = self.derive_cutoff_local_date(timezone_name)
        day = _plain_date(day, resolved_field_name)
        details = {
            "requested": day.isoformat(),
            "data_cutoff": self.data_cutoff.isoformat(),
            "include_cutoff_day": self.include_cutoff_day,
            "timezone": timezone_name,
            "cutoff_local_date": cutoff_date.isoformat(),
        }
        if day > cutoff_date:
            raise DataCutoffExceededError(
                f"{resolved_field_name} {day.isoformat()} is later than data_cutoff local date {cutoff_date.isoformat()}",
                details=details,
            )
        if day == cutoff_date and not self.include_cutoff_day:
            raise DataCutoffExceededError(
                f"{resolved_field_name} {day.isoformat()} touches the incomplete cutoff day",
                details=details,
            )

    def require_instant_not_past_cutoff(
        self, instant: datetime, field_name: str
    ) -> None:
        """Reject an aware instant later than ``data_cutoff``."""

        instant = _aware_datetime(instant, field_name)
        if instant > self.data_cutoff:
            raise DataCutoffExceededError(
                f"{field_name} {instant.isoformat()} is later than data_cutoff "
                f"{self.data_cutoff.isoformat()}",
                details={
                    "requested": instant.isoformat(),
                    "data_cutoff": self.data_cutoff.isoformat(),
                },
            )


# ---------------------------------------------------------------------------
# Market scope and universe policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketScope:
    """Immutable allow-list of markets, exchanges, asset classes, currencies.

    An empty tuple means "no restriction along that axis"; a non-empty tuple
    is deduplicated and stably sorted.  Free-form SQL, callbacks, and
    unvalidated dictionaries are deliberately not expressible here.
    """

    markets: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()
    asset_classes: tuple[str, ...] = ()
    currencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("markets", "exchanges", "asset_classes", "currencies"):
            raw = getattr(self, field_name)
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
                raise InvalidDataRequestError(
                    f"{field_name} must be an iterable of strings"
                )
            labels = tuple(_non_blank_text(item, f"{field_name} entry") for item in raw)
            object.__setattr__(self, field_name, tuple(sorted(set(labels))))


@dataclass(frozen=True, slots=True)
class UniverseQueryPolicy:
    """Versioned candidate-set rules that bound dynamic universe queries.

    ``candidate_set_rules`` is empty exactly when the run has no dynamic
    candidate pool (fixed scope).  The rules are versioned references owned
    by the universe domain; this object never embeds rule bodies.
    """

    candidate_set_rules: tuple[ContractRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_set_rules",
            _sorted_unique_refs(self.candidate_set_rules, "candidate_set_rules"),
        )

    @property
    def has_candidate_rules(self) -> bool:
        """Whether any dynamic candidate-set rule is configured."""

        return bool(self.candidate_set_rules)


# ---------------------------------------------------------------------------
# DataPreflightRequest: the unresolved run intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataPreflightRequest:
    """A run's data intent before calendars and time zone are resolved.

    This object is what ``DataProvider.preflight()`` consumes.  It never
    contains resolved calendar ids, a resolved time zone, or session lists:
    those are preflight outputs frozen into :class:`DataRequest`.
    """

    provider_key: str
    requested_window: DateRange
    frequency: str
    rule_package: ContractRef
    market_scope: MarketScope
    universe_query_policy: UniverseQueryPolicy
    instrument_scope_mode: InstrumentScopeMode
    required_capabilities: tuple[DataCapability, ...]
    strategy_price_bases: tuple[PriceBasis, ...]
    consistency_mode: ConsistencyMode
    # The calendar PIT boundary is the sole request-level visibility input.
    # It deliberately appears before all optional fields so dataclass
    # construction requires callers to make the cutoff decision explicitly.
    query_boundary: QueryBoundary
    static_instrument_ids: tuple[UUID, ...] = ()
    mandatory_instrument_ids: tuple[UUID, ...] = ()
    # Non-zero opening holdings are a separate input because they are always
    # fixed-preflight subjects, even for dynamic-only runs.  The run-spec
    # layer may populate this field directly; admission also accepts the
    # legacy explicit parameter used by ``DataRequest.from_admission``.
    non_zero_initial_position_instrument_ids: tuple[UUID, ...] = ()
    warmup_sessions: int = 0
    rule_exception_set: ContractRef | None = None
    qualification_policy_version: QualificationPolicyRef | str | None = None
    qualification_policy: QualificationPolicyRef | str | None = None
    universe_scope_snapshot_hash: str | None = None
    allowed_settlement_rule_class: str | None = None
    adjustment_series_policy: ContractRef | None = None
    consistency_token_contract: ContractRef | None = None
    data_contract_version: int = DATA_CONTRACT_VERSION
    max_lookback_sessions: int = MAX_LOOKBACK_SESSIONS
    calendar_axis_policy: ContractRef = CALENDAR_AXIS_POLICY
    engine_price_basis: PriceBasis = PriceBasis.RAW
    quality_mode: QualityMode = QualityMode.STRICT
    data_chunk_policy: ContractRef = CHUNK_POLICY
    data_chunk_size_sessions: int = 20
    def require_query_boundary(self) -> QueryBoundary:
        """Return the sole calendar PIT boundary.

        ``query_boundary`` is required by the generated constructor and is
        validated in ``__post_init__``; keeping this helper makes call sites
        explicit and provides a stable guard for facade objects.
        """

        return self.query_boundary

    @property
    def dynamic_scope_enabled(self) -> bool:
        """Whether this request carries a dynamic candidate-set policy."""

        return self.instrument_scope_mode in (
            InstrumentScopeMode.DYNAMIC,
            InstrumentScopeMode.HYBRID,
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_key", _non_blank_text(self.provider_key, "provider_key")
        )
        if not isinstance(self.requested_window, DateRange):
            raise InvalidDataRequestError("requested_window must be a DateRange")
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        if not isinstance(self.rule_package, ContractRef):
            raise InvalidDataRequestError("rule_package must be a ContractRef")
        if not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope")
        if not isinstance(self.universe_query_policy, UniverseQueryPolicy):
            raise InvalidDataRequestError(
                "universe_query_policy must be a UniverseQueryPolicy"
            )
        if not isinstance(self.instrument_scope_mode, InstrumentScopeMode):
            raise InvalidDataRequestError(
                "instrument_scope_mode must be an InstrumentScopeMode"
            )
        object.__setattr__(
            self,
            "required_capabilities",
            _sorted_unique_enum(
                self.required_capabilities, DataCapability, "required_capabilities"
            ),
        )
        object.__setattr__(
            self,
            "strategy_price_bases",
            _sorted_unique_enum(
                self.strategy_price_bases, PriceBasis, "strategy_price_bases"
            ),
        )
        if not isinstance(self.consistency_mode, ConsistencyMode):
            raise InvalidDataRequestError("consistency_mode must be a ConsistencyMode")
        if self.static_instrument_ids:
            object.__setattr__(
                self,
                "static_instrument_ids",
                _sorted_unique_ids(self.static_instrument_ids, "static_instrument_ids"),
            )
        if self.mandatory_instrument_ids:
            object.__setattr__(
                self,
                "mandatory_instrument_ids",
                _sorted_unique_ids(
                    self.mandatory_instrument_ids, "mandatory_instrument_ids"
                ),
            )
        if self.non_zero_initial_position_instrument_ids:
            object.__setattr__(
                self,
                "non_zero_initial_position_instrument_ids",
                _sorted_unique_ids(
                    self.non_zero_initial_position_instrument_ids,
                    "non_zero_initial_position_instrument_ids",
                ),
            )
        else:
            object.__setattr__(self, "non_zero_initial_position_instrument_ids", ())
        warmup = _strict_int(self.warmup_sessions, "warmup_sessions")
        if warmup < 0:
            raise InvalidDataRequestError("warmup_sessions must not be negative")
        if warmup > MAX_LOOKBACK_SESSIONS:
            raise LookbackSessionsLimitExceededError(
                f"warmup_sessions {warmup} exceeds the maximum of "
                f"{MAX_LOOKBACK_SESSIONS}",
                details={
                    "requested": warmup,
                    "maximum": MAX_LOOKBACK_SESSIONS,
                    "cause_code": "calendar_warmup_limit_exceeded",
                },
            )
        object.__setattr__(self, "warmup_sessions", warmup)
        if self.rule_exception_set is not None and not isinstance(
            self.rule_exception_set, ContractRef
        ):
            raise InvalidDataRequestError(
                "rule_exception_set must be a ContractRef when provided"
            )
        policy = self.qualification_policy_version or self.qualification_policy
        if (
            self.qualification_policy_version is not None
            and self.qualification_policy is not None
            and self.qualification_policy_version != self.qualification_policy
        ):
            raise InvalidDataRequestError(
                "qualification_policy and qualification_policy_version disagree"
            )
        if policy is not None and not isinstance(policy, (ContractRef, str)):
            raise InvalidDataRequestError(
                "qualification_policy_version must be a ContractRef or text"
            )
        if isinstance(policy, str):
            policy = _non_blank_text(policy, "qualification_policy_version")
            if policy.lower() in {"latest", "current", "now"}:
                raise InvalidDataRequestError(
                    "qualification_policy_version must be an exact version, never latest"
                )
        object.__setattr__(self, "qualification_policy_version", policy)
        object.__setattr__(self, "qualification_policy", policy)
        if self.universe_scope_snapshot_hash is not None:
            snapshot_hash = _non_blank_text(
                self.universe_scope_snapshot_hash,
                "universe_scope_snapshot_hash",
            )
            if len(snapshot_hash) != 64 or any(
                char not in "0123456789abcdef" for char in snapshot_hash
            ):
                raise InvalidDataRequestError(
                    "universe_scope_snapshot_hash must be a lowercase SHA-256 digest"
                )
            object.__setattr__(self, "universe_scope_snapshot_hash", snapshot_hash)
        if self.allowed_settlement_rule_class is not None:
            object.__setattr__(
                self,
                "allowed_settlement_rule_class",
                _non_blank_text(
                    self.allowed_settlement_rule_class,
                    "allowed_settlement_rule_class",
                ),
            )
        if self.adjustment_series_policy is not None and not isinstance(
            self.adjustment_series_policy, ContractRef
        ):
            raise InvalidDataRequestError(
                "adjustment_series_policy must be a ContractRef when provided"
            )
        if self.consistency_token_contract is not None and not isinstance(
            self.consistency_token_contract, ContractRef
        ):
            raise InvalidDataRequestError(
                "consistency_token_contract must be a ContractRef when provided"
            )
        if self.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN and (
            self.consistency_token_contract is None
        ):
            raise InvalidDataRequestError(
                "chunked_logical_token consistency requires a "
                "consistency_token_contract"
            )
        if not isinstance(self.query_boundary, QueryBoundary):
            raise InvalidDataRequestError("query_boundary must be a QueryBoundary")
        self._validate_version_pinned_fields()
        self._validate_scope_consistency()

    @property
    def fixed_instrument_ids(self) -> tuple[UUID, ...]:
        """Return static and mandatory IDs in canonical fixed-scope order."""

        return fixed_instrument_ids(
            self.static_instrument_ids,
            self.mandatory_instrument_ids,
            self.non_zero_initial_position_instrument_ids,
        )

    def _validate_version_pinned_fields(self) -> None:
        """Enforce every value frozen by data-contract version 1."""

        version = _strict_int(self.data_contract_version, "data_contract_version")
        if version != DATA_CONTRACT_VERSION:
            raise InvalidDataRequestError(
                f"unsupported data_contract_version {version}; this package "
                f"implements version {DATA_CONTRACT_VERSION} only"
            )
        object.__setattr__(self, "data_contract_version", version)

        maximum = _strict_int(self.max_lookback_sessions, "max_lookback_sessions")
        if maximum > MAX_LOOKBACK_SESSIONS:
            raise LookbackSessionsLimitExceededError(
                f"max_lookback_sessions {maximum} exceeds the maximum of "
                f"{MAX_LOOKBACK_SESSIONS}",
                details={"requested": maximum, "maximum": MAX_LOOKBACK_SESSIONS},
            )
        # Version 1 freezes the run-level cap at exactly 512: callers can
        # neither raise nor lower it, mirroring strategy-side enforcement.
        if maximum != MAX_LOOKBACK_SESSIONS:
            raise InvalidDataRequestError(
                "data-contract version 1 fixes max_lookback_sessions to "
                f"{MAX_LOOKBACK_SESSIONS}"
            )
        object.__setattr__(self, "max_lookback_sessions", maximum)

        if self.calendar_axis_policy != CALENDAR_AXIS_POLICY:
            raise InvalidDataRequestError(
                "data-contract version 1 requires the strict_compatible@1 "
                "calendar axis policy"
            )
        if not isinstance(self.engine_price_basis, PriceBasis):
            raise InvalidDataRequestError("engine_price_basis must be a PriceBasis")
        if self.engine_price_basis is not PriceBasis.RAW:
            raise InvalidDataRequestError(
                "data-contract version 1 requires engine_price_basis=raw"
            )
        if not isinstance(self.quality_mode, QualityMode):
            raise InvalidDataRequestError("quality_mode must be a QualityMode")
        if self.quality_mode is not QualityMode.STRICT:
            raise InvalidDataRequestError(
                "data-contract version 1 requires quality_mode=strict"
            )
        if self.data_chunk_policy != CHUNK_POLICY:
            raise InvalidDataRequestError(
                "data-contract version 1 requires the fixed_trading_sessions@1 "
                "chunk policy"
            )
        chunk_size = _strict_int(
            self.data_chunk_size_sessions, "data_chunk_size_sessions"
        )
        if chunk_size != 20:
            raise InvalidDataRequestError(
                "data-contract version 1 fixes data_chunk_size_sessions to 20"
            )
        object.__setattr__(self, "data_chunk_size_sessions", chunk_size)

    def _validate_scope_consistency(self) -> None:
        """Enforce the fixed/dynamic/hybrid scope invariants."""

        has_fixed = bool(
            self.static_instrument_ids
            or self.mandatory_instrument_ids
            or self.non_zero_initial_position_instrument_ids
        )
        has_dynamic = self.universe_query_policy.has_candidate_rules
        mode = self.instrument_scope_mode
        if mode is InstrumentScopeMode.FIXED:
            if not has_fixed:
                raise InvalidDataRequestError(
                    "fixed scope requires static or mandatory instrument ids"
                )
            if has_dynamic:
                raise InvalidDataRequestError(
                    "fixed scope must not configure dynamic candidate rules"
                )
        elif mode is InstrumentScopeMode.DYNAMIC:
            if not has_dynamic:
                raise InvalidDataRequestError(
                    "dynamic scope requires at least one candidate-set rule"
                )
        else:  # hybrid
            if not has_fixed or not has_dynamic:
                raise InvalidDataRequestError(
                    "hybrid scope requires fixed instruments and "
                    "candidate-set rules together"
                )


# ---------------------------------------------------------------------------
# DataRequest: the frozen official run request
# ---------------------------------------------------------------------------

_HASH_ALPHABET = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class DataRequest(DataPreflightRequest):
    """A preflight-frozen official run request.

    Everything from :class:`DataPreflightRequest` is inherited unchanged;
    the resolved calendar axis, time zone, and admission evidence are added
    by preflight and can never be overridden by the caller afterwards.
    """

    resolved_calendar_ids: tuple[str, ...] = ()
    resolved_timezone: str = ""
    admission_calendar_session_signature: str = ""
    admission_preflight_status: PreflightStatus = PreflightStatus.READY
    admission_preflight_hash: str = ""
    accepted_degraded_preflight_hash: str | None = None
    resolved_rule_snapshot_hash: str = ""

    def __post_init__(self) -> None:
        # Explicit parent call: zero-arg super() breaks inside __post_init__
        # because dataclass(slots=True) recreates the class object.
        DataPreflightRequest.__post_init__(self)
        object.__setattr__(
            self,
            "resolved_calendar_ids",
            _sorted_unique_text(self.resolved_calendar_ids, "resolved_calendar_ids"),
        )
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        timezone = _non_blank_text(self.resolved_timezone, "resolved_timezone")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidDataRequestError(
                "resolved_timezone must be a resolvable IANA time-zone name"
            ) from exc
        object.__setattr__(self, "resolved_timezone", timezone)
        signature = _non_blank_text(
            self.admission_calendar_session_signature,
            "admission_calendar_session_signature",
        )
        object.__setattr__(self, "admission_calendar_session_signature", signature)
        if not isinstance(self.admission_preflight_status, PreflightStatus):
            raise InvalidDataRequestError(
                "admission_preflight_status must be a PreflightStatus"
            )
        admission_hash = _non_blank_text(
            self.admission_preflight_hash, "admission_preflight_hash"
        )
        if len(admission_hash) != 64 or any(
            character not in _HASH_ALPHABET for character in admission_hash
        ):
            raise InvalidDataRequestError(
                "admission_preflight_hash must be a lowercase SHA-256 hex digest"
            )
        object.__setattr__(self, "admission_preflight_hash", admission_hash)
        if self.admission_preflight_status is PreflightStatus.BLOCKED:
            raise DataPreflightBlockedError(
                "a blocked preflight report cannot be admitted into a DataRequest",
                details={"admission_preflight_hash": admission_hash},
            )
        if self.admission_preflight_status is PreflightStatus.DEGRADED:
            accepted = self.accepted_degraded_preflight_hash
            if not isinstance(accepted, str) or accepted != admission_hash:
                raise DataPreflightConfirmationMismatchError(
                    "a degraded admission requires the user-confirmed hash to "
                    "equal the preflight report hash",
                    details={
                        "admission_preflight_hash": admission_hash,
                        "accepted_degraded_preflight_hash": (
                            accepted if isinstance(accepted, str) else None
                        ),
                    },
                )
        elif self.accepted_degraded_preflight_hash is not None:
            raise InvalidDataRequestError(
                "accepted_degraded_preflight_hash must be None unless the "
                "admission preflight is degraded"
            )
        # A run with fixed subjects must carry the verified rule snapshot for
        # the complete fixed union.  A genuinely dynamic-only request has no
        # fixed rule snapshot to freeze; requiring a fabricated hash there
        # would turn a valid dynamic admission into a foreign fixed request.
        snapshot_hash = self.resolved_rule_snapshot_hash
        if snapshot_hash:
            snapshot_hash = _non_blank_text(
                snapshot_hash, "resolved_rule_snapshot_hash"
            )
            if len(snapshot_hash) != 64 or any(
                character not in _HASH_ALPHABET for character in snapshot_hash
            ):
                raise InvalidDataRequestError(
                    "resolved_rule_snapshot_hash must be a lowercase SHA-256 "
                    "hex digest"
                )
        elif self.fixed_instrument_ids:
            raise InvalidDataRequestError(
                "fixed subjects require a verified resolved_rule_snapshot_hash"
            )
        else:
            snapshot_hash = ""
        object.__setattr__(self, "resolved_rule_snapshot_hash", snapshot_hash)

    @classmethod
    def from_admission(
        cls,
        request: "DataPreflightRequest",
        report: "DataPreflightReport",
        *,
        accepted_degraded: bool = False,
        rule_preflight_report: "RulePreflightReport | None" = None,
        non_zero_initial_position_instrument_ids: Iterable[UUID] = (),
        session_report: "DataPreflightReport | None" = None,
        session_preflight_report: "DataPreflightReport | None" = None,
    ) -> "DataRequest":
        """Freeze an official run request from a request/report pair.

        This is the only sanctioned admission path: it verifies that the
        report actually describes *this* request (provider, window,
        frequency, market scope, universe rules, rule package, capabilities,
        consistency and chunking semantics), rejects blocked reports,
        enforces the degraded-confirmation rule, and copies the resolved
        calendar axis out of the report.  The report hash covers the full
        request semantics plus the report status, so the stored
        ``admission_preflight_hash`` binds everything a later audit needs.

        ``rule_preflight_report`` is a mandatory admission input: the READY
        fixed-instrument rule preflight report for this same intent.  The
        frozen snapshot hash is taken from the report's verified bundle —
        the report DTO itself guarantees that a READY report carries a
        consistent, non-forged bundle — and the report must match this
        request's rule package, exception set, window, knowledge cutoff,
        and full fixed-instrument scope (including non-zero initial
        positions).  A blocked or foreign rule
        preflight can never admit a formal run.
        """

        from dataclasses import fields

        from app.backtesting.data.errors import InvalidDataRequestError as _Invalid
        from app.backtesting.data.reports import DataPreflightReport  # noqa: F401
        from app.backtesting.data.universe import (
            UniverseScopeResolution as _UniverseScopeResolution,
            UniverseScopeStatus as _UniverseScopeStatus,
            compute_universe_scope_snapshot_hash,
        )
        from app.instruments.rule_preflight import (
            RulePreflightReport as _RulePreflightReport,
        )
        from app.instruments.rules.contracts import ResolutionStatus as _RuleStatus

        if not isinstance(request, DataPreflightRequest):
            raise _Invalid("request must be a DataPreflightRequest")
        if not isinstance(report, DataPreflightReport):
            raise _Invalid("report must be a DataPreflightReport")
        if (
            session_report is not None
            and session_preflight_report is not None
            and session_report is not session_preflight_report
        ):
            raise _Invalid(
                "session_report and session_preflight_report must agree"
            )
        bound_session_report = (
            session_report
            if session_report is not None
            else session_preflight_report
        )
        if bound_session_report is not None:
            if not isinstance(bound_session_report, DataPreflightReport):
                raise _Invalid("session_report must be a DataPreflightReport")
            if bound_session_report.status is PreflightStatus.BLOCKED:
                raise DataPreflightBlockedError(
                    "a blocked session preflight report cannot be bound",
                    details={"session_report_hash": bound_session_report.report_hash},
                )
            if bound_session_report.report_hash != report.report_hash:
                raise UniversePreflightHashMismatchError(
                    "admission and session preflight reports do not match",
                    details={
                        "admission_report_hash": report.report_hash,
                        "session_report_hash": bound_session_report.report_hash,
                    },
                )
            if (
                bound_session_report.universe_scope_snapshot_hash
                != report.universe_scope_snapshot_hash
                or bound_session_report.resolved_calendar_ids
                != report.resolved_calendar_ids
                or bound_session_report.calendar_session_signature
                != report.calendar_session_signature
            ):
                raise UniversePreflightHashMismatchError(
                    "admission and session universe scope bindings do not match",
                    details={
                        "admission_scope_hash": report.universe_scope_snapshot_hash,
                        "session_scope_hash": bound_session_report.universe_scope_snapshot_hash,
                    },
                )
        explicit_non_zero_initial_position_ids = tuple(
            non_zero_initial_position_instrument_ids
        )
        if any(
            not isinstance(item, UUID)
            for item in explicit_non_zero_initial_position_ids
        ):
            raise _Invalid(
                "non_zero_initial_position_instrument_ids must contain UUIDs"
            )
        required_fixed_ids = set(
            fixed_instrument_ids(
                request.static_instrument_ids,
                request.mandatory_instrument_ids,
                (
                    *request.non_zero_initial_position_instrument_ids,
                    *explicit_non_zero_initial_position_ids,
                ),
            )
        )
        if report.status is PreflightStatus.BLOCKED:
            raise DataPreflightBlockedError(
                "a blocked preflight report cannot be admitted",
                details={"admission_preflight_hash": report.report_hash},
            )
        dynamic_scope = request.instrument_scope_mode in (
            InstrumentScopeMode.DYNAMIC,
            InstrumentScopeMode.HYBRID,
        )
        frozen_scope_hash: str | None = None
        if dynamic_scope:
            # A dynamic report must carry the actual task-15 resolution, not
            # just a caller-provided digest.  ``UniverseScopeResolution``
            # recomputes its own hash, so the binding cannot be forged by
            # supplying an arbitrary signature or hash string.
            resolution = getattr(report, "universe_scope_resolution", None)
            if not isinstance(resolution, _UniverseScopeResolution):
                raise _Invalid(
                    "dynamic admission requires a verified universe scope resolution",
                    details={"field": "universe_scope_resolution"},
                )
            if resolution.status is not _UniverseScopeStatus.READY:
                raise DataPreflightBlockedError(
                    "a blocked dynamic universe scope cannot be admitted",
                    details={"scope_snapshot_hash": resolution.snapshot_hash},
                )
            if (
                resolution.scope_mode is not None
                and resolution.scope_mode is not request.instrument_scope_mode
            ):
                raise _Invalid(
                    "universe scope resolution does not belong to this request",
                    details={"mismatched_fields": ["instrument_scope_mode"]},
                )
            resolution_mismatches: list[str] = []
            if resolution.market_scope is not None and resolution.market_scope != request.market_scope:
                resolution_mismatches.append("market_scope")
            if resolution.universe_query_policy is not None and resolution.universe_query_policy != request.universe_query_policy:
                resolution_mismatches.append("universe_query_policy")
            if resolution.rule_package_reference is not None and resolution.rule_package_reference != request.rule_package:
                resolution_mismatches.append("rule_package")
            if resolution.rule_exception_set_reference is not None and resolution.rule_exception_set_reference != request.rule_exception_set:
                resolution_mismatches.append("rule_exception_set")
            if resolution.qualification_policy_version is not None and resolution.qualification_policy_version != request.qualification_policy_version:
                resolution_mismatches.append("qualification_policy_version")
            if tuple(resolution.resolved_calendar_ids) != tuple(report.resolved_calendar_ids):
                resolution_mismatches.append("resolved_calendar_ids")
            if resolution_mismatches:
                raise _Invalid(
                    "universe scope resolution does not belong to this request",
                    details={"mismatched_fields": sorted(set(resolution_mismatches))},
                )
            computed_scope_hash = compute_universe_scope_snapshot_hash(resolution)
            if computed_scope_hash != resolution.snapshot_hash:
                raise UniversePreflightHashMismatchError(
                    "universe scope resolution hash is not self-consistent",
                    details={
                        "expected": computed_scope_hash,
                        "actual": resolution.snapshot_hash,
                    },
                )
            if (
                resolution.current_snapshot_hash is not None
                and resolution.current_snapshot_hash != resolution.snapshot_hash
            ):
                raise UniversePreflightHashMismatchError(
                    "universe scope session observation does not match admission",
                    details={
                        "expected": resolution.snapshot_hash,
                        "actual": resolution.current_snapshot_hash,
                    },
                )
            profile_text = resolution.source_evidence.get(
                "preflight_profile", "formal@1"
            )
            if (
                str(profile_text) == "internal_link_acceptance@1"
                and report.status is PreflightStatus.DEGRADED
            ):
                raise DataPreflightBlockedError(
                    "internal_link_acceptance@1 does not admit degraded preflight",
                    details={"preflight_profile": profile_text},
                )
            report_scope_hash = getattr(
                report, "universe_scope_snapshot_hash", None
            )
            if report_scope_hash is None:
                report_scope_hash = resolution.snapshot_hash
            if report_scope_hash != resolution.snapshot_hash:
                raise UniversePreflightHashMismatchError(
                    "universe scope resolution hash does not match the report",
                    details={
                        "expected": resolution.snapshot_hash,
                        "actual": report_scope_hash,
                    },
                )
            if (
                request.universe_scope_snapshot_hash is not None
                and request.universe_scope_snapshot_hash != report_scope_hash
            ):
                raise UniversePreflightHashMismatchError(
                    "universe scope hash does not match the request",
                    details={
                        "expected": request.universe_scope_snapshot_hash,
                        "actual": report_scope_hash,
                    },
                )
            frozen_scope_hash = report_scope_hash
        # Bind every shared semantic.  A dynamic scope hash is generated by
        # the verified resolution and therefore is intentionally exempt from
        # the unresolved request/report equality loop below.
        mismatches = []
        for field in fields(DataPreflightRequest):
            name = field.name
            if (
                name == "non_zero_initial_position_instrument_ids"
                and explicit_non_zero_initial_position_ids
            ):
                continue
            if name == "universe_scope_snapshot_hash" and dynamic_scope:
                continue
            report_name = {
                "instrument_scope_mode": "scope_mode",
                "warmup_sessions": "warmup_sessions_count",
            }.get(name, name)
            if not hasattr(report, report_name) or getattr(report, report_name) != getattr(
                request, name
            ):
                mismatches.append(name)
        if mismatches:
            raise _Invalid(
                "preflight report does not belong to this request",
                details={"mismatched_fields": sorted(mismatches)},
            )
        if report.status is PreflightStatus.DEGRADED:
            if not accepted_degraded:
                raise DataPreflightConfirmationMismatchError(
                    "admitting a degraded preflight requires explicit "
                    "user confirmation of its hash",
                    details={"admission_preflight_hash": report.report_hash},
                )
            confirmed_hash: str | None = report.report_hash
        else:
            if accepted_degraded:
                raise _Invalid(
                    "accepted_degraded is only valid for degraded reports"
                )
            confirmed_hash = None

        # Only a request with fixed subjects needs a fixed rule snapshot.
        # Dynamic-only admission intentionally carries an empty snapshot.
        resolved_rule_snapshot_hash = ""
        if required_fixed_ids:
            if not isinstance(rule_preflight_report, _RulePreflightReport):
                raise _Invalid(
                    "fixed subjects require a ready fixed-instrument rule preflight report"
                )
            if rule_preflight_report.status is not _RuleStatus.READY:
                raise DataPreflightBlockedError(
                    "a blocked rule preflight report cannot be admitted",
                    details={"rule_report_hash": rule_preflight_report.report_hash},
                )
            rule_mismatches = []
            if rule_preflight_report.rule_package_reference != request.rule_package:
                rule_mismatches.append("rule_package")
            if rule_preflight_report.exception_set_reference != request.rule_exception_set:
                rule_mismatches.append("rule_exception_set")
            if (
                rule_preflight_report.start_date != request.requested_window.start_date
                or rule_preflight_report.end_date != request.requested_window.end_date
            ):
                rule_mismatches.append("requested_window")
            expected_rule_cutoff = (
                request.query_boundary.knowledge_as_of
                or request.query_boundary.data_cutoff
            )
            if rule_preflight_report.data_cutoff != expected_rule_cutoff:
                rule_mismatches.append("knowledge_as_of")
            checked_instrument_ids = {
                result.instrument_id
                for result in rule_preflight_report.checked_instruments
            }
            if checked_instrument_ids != required_fixed_ids:
                rule_mismatches.append("fixed_instrument_ids")
            if rule_mismatches:
                raise _Invalid(
                    "rule preflight report does not belong to this request",
                    details={"mismatched_fields": sorted(rule_mismatches)},
                )
            # STATUS is a request capability only when the frozen rule
            # segments explicitly require at least one trading-status
            # dimension.  Validate this after the report/snapshot identity
            # checks and before the frozen DataRequest is constructed; this
            # prevents a client or provider manifest from silently adding or
            # removing STATUS after rule facts have become authoritative.
            snapshot_bundle = rule_preflight_report.snapshot_bundle
            required_status_dimensions = tuple(
                sorted(
                    {
                        dimension
                        for segment in snapshot_bundle.instrument_segments
                        for dimension, requirement in segment.capability_declarations.items()
                        if requirement == "required"
                    }
                )
            )
            request_requires_status = DataCapability.STATUS in request.required_capabilities
            if bool(required_status_dimensions) != request_requires_status:
                raise TradingStatusCapabilityRequirementMismatchError(
                    "frozen rule applicability and request capabilities disagree",
                    details={
                        "reason_code": "trading_status_capability_requirement_mismatch",
                        "required_status_dimensions": required_status_dimensions,
                        "request_required_capabilities": tuple(
                            item.value for item in request.required_capabilities
                        ),
                        "expected_status": bool(required_status_dimensions),
                        "actual_status": request_requires_status,
                        "rule_package_reference": {
                            "key": request.rule_package.key,
                            "version": request.rule_package.version,
                        },
                        "rule_snapshot_hash": rule_preflight_report.snapshot_hash,
                    },
                )
            resolved_rule_snapshot_hash = rule_preflight_report.snapshot_hash
        elif rule_preflight_report is not None:
            checked_ids = {
                result.instrument_id
                for result in getattr(rule_preflight_report, "checked_instruments", ())
            }
            if checked_ids:
                raise _Invalid(
                    "dynamic-only admission must not carry a fixed rule snapshot",
                    details={"fixed_instrument_ids": sorted(map(str, checked_ids))},
                )

        request_values = {
            field.name: getattr(request, field.name)
            for field in fields(DataPreflightRequest)
        }
        request_values["non_zero_initial_position_instrument_ids"] = tuple(
            sorted(
                set(request.non_zero_initial_position_instrument_ids)
                | set(explicit_non_zero_initial_position_ids),
                key=str,
            )
        )
        if frozen_scope_hash is not None:
            request_values["universe_scope_snapshot_hash"] = frozen_scope_hash
        return cls(
            **request_values,
            resolved_calendar_ids=report.resolved_calendar_ids,
            resolved_timezone=report.resolved_timezone or "",
            admission_calendar_session_signature=report.calendar_session_signature,
            admission_preflight_status=report.status,
            admission_preflight_hash=report.report_hash,
            accepted_degraded_preflight_hash=confirmed_hash,
            resolved_rule_snapshot_hash=resolved_rule_snapshot_hash,
        )


# ---------------------------------------------------------------------------
# Point-in-time query DTOs (strongly typed; no dicts, no **kwargs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentQuery:
    """Resolve full instrument specs for one or more stable identities."""

    instrument_ids: UUID | tuple[UUID, ...]
    effective_at: datetime
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(
            self, "effective_at", _aware_datetime(self.effective_at, "effective_at")
        )
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_instant_not_past_cutoff(
            self.effective_at, "effective_at"
        )


@dataclass(frozen=True, slots=True)
class InstrumentMappingQuery:
    """Query evidenced PIT source-code mappings for a window."""

    instrument_ids: UUID | tuple[UUID, ...]
    source: str
    window: DateRange
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(self, "source", _non_blank_text(self.source, "source"))
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class TradingRuleQuery:
    """Query trading-rule facts valid inside a window."""

    instrument_ids: UUID | tuple[UUID, ...]
    window: DateRange
    boundary: QueryBoundary
    rule_class: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        if self.rule_class is not None:
            object.__setattr__(
                self, "rule_class", _non_blank_text(self.rule_class, "rule_class")
            )
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class TradingStatusQuery:
    """Query trading-status facts (suspensions and the like) for a window."""

    instrument_ids: UUID | tuple[UUID, ...]
    window: DateRange
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class UniverseQuery:
    """Resolve the candidate universe under frozen rules and market scope.

    The rule reference and market scope come from the frozen request, so
    callers cannot widen the candidate pool at query time.
    """

    rule: ContractRef
    market_scope: MarketScope
    effective_date: date
    boundary: QueryBoundary
    # The optional fields below make the PIT scope explicit for dynamic
    # queries while preserving the four-field constructor used by the
    # earlier data-contract version.  They are immutable copies of the run
    # admission values; callers cannot widen a query by replacing them.
    allowed_calendar_ids: tuple[str, ...] = ()
    rule_exception_set: ContractRef | None = None
    qualification_policy_version: ContractRef | str | None = None
    qualification_policy: ContractRef | str | None = None
    scope_mode: InstrumentScopeMode | None = None
    universe_scope_snapshot_hash: str | None = None
    rule_package_reference: ContractRef | None = None
    frozen_calendar_ids: tuple[str, ...] = ()
    # The candidate-set policy is copied from the admitted request.  Keeping
    # it on the query itself prevents a provider from filling policy values
    # from a mutable session/request side-channel after the query was built.
    # It is placed after the legacy positional fields for source compatibility.
    universe_query_policy: UniverseQueryPolicy | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.rule, ContractRef):
            raise InvalidDataRequestError("rule must be a ContractRef")
        if self.rule_package_reference is not None:
            if not isinstance(self.rule_package_reference, ContractRef):
                raise InvalidDataRequestError(
                    "rule_package_reference must be a ContractRef"
                )
            if self.rule_package_reference != self.rule:
                raise InvalidDataRequestError(
                    "rule and rule_package_reference must identify the same package"
                )
        object.__setattr__(self, "rule_package_reference", self.rule)
        if not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope")
        effective = _plain_date(self.effective_date, "effective_date")
        object.__setattr__(self, "effective_date", effective)
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_not_past_cutoff(effective, "effective_date")
        if self.universe_query_policy is not None and not isinstance(
            self.universe_query_policy, UniverseQueryPolicy
        ):
            raise InvalidDataRequestError(
                "universe_query_policy must be a UniverseQueryPolicy"
            )
        # Every query must carry the exact frozen policy.  A fixed query may
        # carry an explicitly empty policy; omission is never filled from a
        # mutable request/session side channel.  Dynamic/hybrid queries must
        # additionally carry at least one candidate-set rule.
        if self.universe_query_policy is None:
            raise InvalidDataRequestError(
                "UniverseQuery requires an explicit universe_query_policy"
            )
        if self.scope_mode in (
            InstrumentScopeMode.DYNAMIC,
            InstrumentScopeMode.HYBRID,
        ) and not self.universe_query_policy.has_candidate_rules:
            raise InvalidDataRequestError(
                "dynamic/hybrid UniverseQuery requires a non-empty "
                "universe_query_policy"
            )
        object.__setattr__(
            self,
            "universe_query_policy",
            self.universe_query_policy,
        )
        # Normalize the short policy/calendar spellings into the canonical
        # query fields before the provider sees this immutable request.
        if self.qualification_policy_version is not None and self.qualification_policy is not None and self.qualification_policy_version != self.qualification_policy:
            raise InvalidDataRequestError(
                "qualification_policy and qualification_policy_version disagree"
            )
        policy = self.qualification_policy_version or self.qualification_policy
        if policy is not None and not isinstance(policy, (ContractRef, str)):
            raise InvalidDataRequestError(
                "qualification_policy must be a ContractRef or text"
            )
        if isinstance(policy, str):
            policy = _non_blank_text(policy, "qualification_policy")
            if policy.lower() in {"latest", "current", "now"}:
                raise InvalidDataRequestError(
                    "qualification_policy must be an exact version, never latest"
                )
        object.__setattr__(self, "qualification_policy_version", policy)
        object.__setattr__(self, "qualification_policy", policy)
        raw_calendar_ids = self.allowed_calendar_ids or self.frozen_calendar_ids
        if self.allowed_calendar_ids and self.frozen_calendar_ids:
            try:
                if tuple(sorted(set(self.allowed_calendar_ids))) != tuple(
                    sorted(set(self.frozen_calendar_ids))
                ):
                    raise InvalidDataRequestError(
                        "allowed_calendar_ids and frozen_calendar_ids disagree"
                    )
            except TypeError as exc:
                raise InvalidDataRequestError(
                    "calendar id collections must be iterable"
                ) from exc
        try:
            from app.backtesting.calendar_axis import normalize_calendar_id

            normalized_calendar_ids = tuple(
                sorted(
                    {
                        normalize_calendar_id(item)
                        for item in raw_calendar_ids
                    }
                )
            )
        except Exception as exc:
            raise InvalidDataRequestError(
                "allowed_calendar_ids must contain canonical calendar ids"
            ) from exc
        object.__setattr__(self, "allowed_calendar_ids", normalized_calendar_ids)
        object.__setattr__(self, "frozen_calendar_ids", normalized_calendar_ids)
        if self.rule_exception_set is not None and not isinstance(
            self.rule_exception_set, ContractRef
        ):
            raise InvalidDataRequestError(
                "rule_exception_set must be a ContractRef when provided"
            )
        if self.scope_mode is not None and not isinstance(
            self.scope_mode, InstrumentScopeMode
        ):
            raise InvalidDataRequestError(
                "scope_mode must be an InstrumentScopeMode when provided"
            )
        if self.universe_scope_snapshot_hash is not None:
            snapshot_hash = _non_blank_text(
                self.universe_scope_snapshot_hash,
                "universe_scope_snapshot_hash",
            )
            if len(snapshot_hash) != 64 or any(
                character not in "0123456789abcdef" for character in snapshot_hash
            ):
                raise InvalidDataRequestError(
                    "universe_scope_snapshot_hash must be a lowercase SHA-256 digest"
                )
            object.__setattr__(
                self, "universe_scope_snapshot_hash", snapshot_hash
            )

    @property
    def data_cutoff(self) -> datetime:
        """The explicit PIT cutoff carried by ``boundary``."""

        return self.boundary.data_cutoff

    @property
    def resolved_calendar_ids(self) -> tuple[str, ...]:
        """Canonical alias for calendars frozen by run admission."""

        return self.allowed_calendar_ids

    @property
    def qualification_policy_reference(self) -> ContractRef | str | None:
        """Canonical alias for the candidate qualification policy."""

        return self.qualification_policy_version

    @property
    def candidate_set_policy(self) -> UniverseQueryPolicy:
        """Read-only alias for the frozen candidate-set policy."""

        return self.universe_query_policy


def _require_single_window(window: object, field_name: str) -> None:
    """Reject anything that is neither a DateRange nor a LookbackWindow."""

    if not isinstance(window, (DateRange, LookbackWindow)):
        raise InvalidDataRequestError(
            f"{field_name} must be a DateRange or a LookbackWindow, never both"
        )


@dataclass(frozen=True, slots=True)
class BarQuery:
    """Query bars for one window expressed either explicitly or as lookback.

    Exactly one of :class:`DateRange` or :class:`LookbackWindow` may be
    supplied; passing both shapes is a caller error.
    """

    instrument_ids: UUID | tuple[UUID, ...]
    frequency: str
    boundary: QueryBoundary
    window: DateRange | LookbackWindow
    # Price basis is explicit on every bar query.  Keeping the default at
    # ``raw`` preserves compatibility with existing callers while preventing
    # a provider from having to infer a basis from the selected series.
    price_basis: PriceBasis = PriceBasis.RAW

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        if not isinstance(self.price_basis, PriceBasis):
            raise InvalidDataRequestError("price_basis must be a PriceBasis")
        _require_single_window(self.window, "window")
        if isinstance(self.window, DateRange):
            self.boundary.require_not_past_cutoff(
                self.window.end_date, "window.end_date"
            )
        else:
            self.boundary.require_instant_not_past_cutoff(
                self.window.end_at, "window.end_at"
            )


@dataclass(frozen=True, slots=True)
class TickQuery:
    """Generic tick query boundary (first-version providers need not serve it).

    Only the common time bounds are defined here; the concrete minute/tick
    business capability is out of scope for this contract version.
    """

    instrument_ids: UUID | tuple[UUID, ...]
    start_at: datetime
    end_at: datetime
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        start = _aware_datetime(self.start_at, "start_at")
        end = _aware_datetime(self.end_at, "end_at")
        if start > end:
            raise InvalidDataRequestError("start_at must not be later than end_at")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_instant_not_past_cutoff(end, "end_at")


@dataclass(frozen=True, slots=True)
class DataValueQuery:
    """Generic named-series value query (first version defines bounds only).

    ``series`` is the machine name of the requested generic value series;
    ``frequency`` is optional because some series are not bar-shaped.
    """

    instrument_ids: UUID | tuple[UUID, ...]
    series: str
    boundary: QueryBoundary
    window: DateRange | LookbackWindow
    frequency: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(self, "series", _non_blank_text(self.series, "series"))
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        if self.frequency is not None:
            object.__setattr__(
                self, "frequency", _non_blank_text(self.frequency, "frequency")
            )
        _require_single_window(self.window, "window")
        if isinstance(self.window, DateRange):
            self.boundary.require_not_past_cutoff(
                self.window.end_date, "window.end_date"
            )
        else:
            self.boundary.require_instant_not_past_cutoff(
                self.window.end_at, "window.end_at"
            )


@dataclass(frozen=True, slots=True)
class AdjustedSeriesQuery:
    """Query an adjustment series with an explicitly chosen price basis."""

    instrument_ids: UUID | tuple[UUID, ...]
    frequency: str
    price_basis: PriceBasis
    boundary: QueryBoundary
    window: DateRange | LookbackWindow

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        if not isinstance(self.price_basis, PriceBasis):
            raise InvalidDataRequestError("price_basis must be a PriceBasis")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        _require_single_window(self.window, "window")
        if isinstance(self.window, DateRange):
            self.boundary.require_not_past_cutoff(
                self.window.end_date, "window.end_date"
            )
        else:
            self.boundary.require_instant_not_past_cutoff(
                self.window.end_at, "window.end_at"
            )


@dataclass(frozen=True, slots=True)
class CorporateActionQuery:
    """Query corporate actions for a window, optionally by action type."""

    instrument_ids: UUID | tuple[UUID, ...]
    window: DateRange
    boundary: QueryBoundary
    action_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        if self.action_types:
            object.__setattr__(
                self,
                "action_types",
                _sorted_unique_text(self.action_types, "action_types"),
            )
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class CoverageQuery:
    """Ask a provider for a coverage report over one capability and window."""

    capability: DataCapability
    instrument_ids: UUID | tuple[UUID, ...]
    window: DateRange
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        if not isinstance(self.capability, DataCapability):
            raise InvalidDataRequestError("capability must be a DataCapability")
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class DataChunkQuery:
    """Identify one fixed chunk of official sessions and its fact types.

    ``chunk_index`` starts at 0.  The first and last session identifiers
    refer to ``SessionPoint.session_id`` values of the resolved official
    sessions.  Computing chunk boundaries from the 20-session policy is
    deliberately not implemented in this contract version.
    """

    chunk_index: int
    first_session_id: str
    last_session_id: str
    fact_types: tuple[DataCapability, ...]

    def __post_init__(self) -> None:
        index = _strict_int(self.chunk_index, "chunk_index")
        if index < 0:
            raise InvalidDataRequestError("chunk_index must not be negative")
        object.__setattr__(self, "chunk_index", index)
        first = _non_blank_text(self.first_session_id, "first_session_id")
        last = _non_blank_text(self.last_session_id, "last_session_id")
        # Session ids are opaque stable labels; lexicographic order is not
        # chronological order, so the pair is only checked for well-formed
        # text.  Real boundary correctness is enforced where the query is
        # matched against the frozen official sessions.
        object.__setattr__(self, "first_session_id", first)
        object.__setattr__(self, "last_session_id", last)
        object.__setattr__(
            self,
            "fact_types",
            _sorted_unique_enum(self.fact_types, DataCapability, "fact_types"),
        )
