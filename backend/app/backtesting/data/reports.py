"""Coverage reports, admission preflight reports, and the canonical hash.

The hash is produced by exactly one normalization in this module
(:func:`canonical_json` plus SHA-256); providers never define their own
hash semantics.  The canonical form sorts mapping keys, renders decimals
as fixed-point strings, and orders collections stably, so equivalent
reports hash identically even when constructed in different input order,
with different Chinese display messages, or generated at different times.

Excluded from every hash: human-readable ``message`` text, generation
timestamps, elapsed timings, database primary keys, raw tokens, and any
credential material.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import (
    CalendarAxisDifference,
    CalendarAxisStatus,
    CalendarDefinition,
    SessionPoint,
)
from app.backtesting.data.errors import InvalidDataRequestError, freeze_json
from app.backtesting.data.requests import (
    CALENDAR_AXIS_POLICY,
    CHUNK_POLICY,
    DATA_CONTRACT_VERSION,
    MAX_LOOKBACK_SESSIONS,
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DateRange,
    InstrumentScopeMode,
    IssueSeverity,
    MarketScope,
    PreflightStatus,
    PriceBasis,
    QualityMode,
    QualityStatus,
    UniverseQueryPolicy,
    _aware_datetime,
    _non_blank_text,
    _sorted_unique_enum,
    _sorted_unique_text,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "DataCoverageReport",
    "DataPreflightReport",
    "PreflightIssue",
    "canonical_hash",
    "canonical_json",
]


# ---------------------------------------------------------------------------
# Canonical serialization and hashing
# ---------------------------------------------------------------------------

_HEX_ALPHABET = frozenset("0123456789abcdef")


def _canonical_value(value: object, where: str) -> object:
    """Convert one value into its canonical JSON-serializable form."""

    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        # Finite floats serialize directly; NaN/inf are rejected because
        # they have no deterministic JSON representation.
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{where} float values must be finite")
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        # Also covers datetime.time via its own branch below.
        return value.isoformat()
    try:
        from datetime import time as _time

        if isinstance(value, _time):
            return value.isoformat()
    except ImportError:  # pragma: no cover - stdlib import cannot fail
        pass
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        canonical: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{where} mapping keys must be plain strings")
            canonical[key] = _canonical_value(item, f"{where}[{key!r}]")
        return canonical
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, where) for item in value]
    raise ValueError(
        f"{where} contains a value that has no canonical JSON form: "
        f"{type(value).__name__}"
    )


def canonical_json(value: object) -> str:
    """Serialize ``value`` into deterministic canonical JSON text.

    Mapping keys are sorted, separators are compact, and non-ASCII text is
    preserved so identical logical content always yields identical bytes.
    """

    canonical = _canonical_value(value, "value")
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: object) -> str:
    """SHA-256 hex digest of :func:`canonical_json` output."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_hash_digest(value: str, field_name: str) -> str:
    """Require a lowercase SHA-256 hex digest."""

    text = _non_blank_text(value, field_name)
    if len(text) != 64 or any(character not in _HEX_ALPHABET for character in text):
        raise InvalidDataRequestError(
            f"{field_name} must be a lowercase SHA-256 hex digest"
        )
    return text


def _strict_version(value: object) -> int:
    """Require exactly the data-contract version this package implements."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataRequestError("data_contract_version must be an integer")
    if value != DATA_CONTRACT_VERSION:
        raise InvalidDataRequestError(
            f"unsupported data_contract_version {value}; this package "
            f"implements version {DATA_CONTRACT_VERSION} only"
        )
    return value


# ---------------------------------------------------------------------------
# Structured preflight issues
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    """One structured preflight finding.

    ``code`` is a stable machine identifier; ``message`` is concise Chinese
    display copy.  Only the machine fields participate in sorting and in
    the report hash, so wording changes never change hashes.
    """

    code: str
    severity: IssueSeverity
    scope: str
    message: str
    instrument_id: UUID | None = None
    field: str | None = None
    details: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code.strip():
            raise InvalidDataRequestError("issue code must be non-blank text")
        if not isinstance(self.severity, IssueSeverity):
            raise InvalidDataRequestError("issue severity must be an IssueSeverity")
        if type(self.scope) is not str or not self.scope.strip():
            raise InvalidDataRequestError("issue scope must be non-blank text")
        if type(self.message) is not str or not self.message.strip():
            raise InvalidDataRequestError("issue message must be non-blank text")
        if self.instrument_id is not None and not isinstance(
            self.instrument_id, UUID
        ):
            raise InvalidDataRequestError("issue instrument_id must be a UUID")
        if self.field is not None and (
            type(self.field) is not str or not self.field.strip()
        ):
            raise InvalidDataRequestError("issue field must be non-blank text")
        if self.details is not None:
            if not isinstance(self.details, Mapping):
                raise InvalidDataRequestError(
                    "issue details must be a mapping of JSON values"
                )
            frozen = freeze_json(dict(self.details), "issue details")
            assert isinstance(frozen, MappingProxyType)
            object.__setattr__(self, "details", frozen)

    @property
    def sort_key(self) -> tuple[str, str, str, str, str, str]:
        """Stable ordering key built from all machine fields.

        ``details`` participates through its canonical JSON form so that
        two issues differing only in details never tie: ties would keep
        input order under stable sorting and make hashes depend on it.
        """

        return (
            self.code,
            self.severity.value,
            self.scope,
            str(self.instrument_id) if self.instrument_id else "",
            self.field or "",
            (
                canonical_json(dict(self.details))
                if self.details is not None
                else ""
            ),
        )

    def machine_fields(self) -> dict[str, object]:
        """Hash-relevant content of this issue (message excluded)."""

        return {
            "code": self.code,
            "severity": self.severity.value,
            "scope": self.scope,
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "field": self.field,
            "details": self.details,
        }


def _sorted_issues(issues: Sequence[PreflightIssue]) -> tuple[PreflightIssue, ...]:
    """Validate element types and sort issues by machine fields."""

    validated = tuple(issues)
    for issue in validated:
        if not isinstance(issue, PreflightIssue):
            raise InvalidDataRequestError(
                "issues entries must be PreflightIssue instances"
            )
    return tuple(sorted(validated, key=lambda issue: issue.sort_key))


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataCoverageReport:
    """Per-capability coverage accounting for one requested slice.

    Counts are non-negative and must sum up to ``expected_count``.  An
    empty result is reported as ``unavailable`` evidence, never silently
    interpreted as "no events existed in the period".
    """

    requested_window: DateRange
    capability: DataCapability
    instrument_ids: tuple[UUID, ...]
    expected_count: int
    complete_count: int
    partial_count: int
    invalid_count: int
    unavailable_count: int
    quality_status: QualityStatus
    missing_ranges: tuple[DateRange, ...] = ()
    source_revisions: Mapping[str, str] | None = None
    issues: tuple[PreflightIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.requested_window, DateRange):
            raise InvalidDataRequestError(
                "requested_window must be a DateRange"
            )
        if not isinstance(self.capability, DataCapability):
            raise InvalidDataRequestError("capability must be a DataCapability")
        if not self.instrument_ids:
            raise InvalidDataRequestError("instrument_ids must not be empty")
        for instrument_id in self.instrument_ids:
            if not isinstance(instrument_id, UUID):
                raise InvalidDataRequestError(
                    "instrument_ids entries must be UUIDs"
                )
        object.__setattr__(
            self,
            "instrument_ids",
            tuple(sorted(set(self.instrument_ids), key=str)),
        )
        counts = {}
        total = 0
        for name in (
            "expected_count",
            "complete_count",
            "partial_count",
            "invalid_count",
            "unavailable_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidDataRequestError(f"{name} must be a non-negative int")
            counts[name] = value
            if name != "expected_count":
                total += value
        if total != counts["expected_count"]:
            raise InvalidDataRequestError(
                "coverage counts must sum up to expected_count"
            )
        if not isinstance(self.quality_status, QualityStatus):
            raise InvalidDataRequestError(
                "quality_status must be a QualityStatus"
            )
        ranges = tuple(self.missing_ranges)
        for missing_range in ranges:
            if not isinstance(missing_range, DateRange):
                raise InvalidDataRequestError(
                    "missing_ranges entries must be DateRange values"
                )
        ranges = tuple(sorted(set(ranges), key=lambda item: item.start_date))
        for earlier, later in zip(ranges, ranges[1:]):
            if later.start_date <= earlier.end_date:
                raise InvalidDataRequestError(
                    "missing_ranges must not contain overlapping ranges"
                )
        object.__setattr__(self, "missing_ranges", ranges)
        revisions = self.source_revisions or {}
        if not isinstance(revisions, Mapping):
            raise InvalidDataRequestError(
                "source_revisions must be a mapping"
            )
        normalized_revisions: dict[str, str] = {}
        for source, revision in revisions.items():
            if type(source) is not str or not source.strip():
                raise InvalidDataRequestError(
                    "source_revisions keys must be non-blank strings"
                )
            if type(revision) is not str or not revision.strip():
                raise InvalidDataRequestError(
                    "source_revisions values must be non-blank strings"
                )
            normalized_revisions[source] = revision
        object.__setattr__(
            self, "source_revisions", MappingProxyType(normalized_revisions)
        )
        object.__setattr__(self, "issues", _sorted_issues(self.issues))

    def machine_content(self) -> dict[str, object]:
        """Hash-relevant canonical content of this coverage report."""

        return {
            "requested_window": {
                "start_date": self.requested_window.start_date,
                "end_date": self.requested_window.end_date,
            },
            "capability": self.capability,
            "instrument_ids": [str(item) for item in self.instrument_ids],
            "counts": {
                "expected": self.expected_count,
                "complete": self.complete_count,
                "partial": self.partial_count,
                "invalid": self.invalid_count,
                "unavailable": self.unavailable_count,
            },
            "quality_status": self.quality_status,
            "missing_ranges": [
                {"start": item.start_date, "end": item.end_date}
                for item in self.missing_ranges
            ],
            "source_revisions": self.source_revisions,
            "issues": [issue.machine_fields() for issue in self.issues],
        }

    def __lt__(self, other: "DataCoverageReport") -> bool:
        return self.sort_key < other.sort_key

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """Stable ordering key independent of construction order."""

        return (
            self.capability.value,
            self.requested_window.start_date.isoformat(),
            self.requested_window.end_date.isoformat(),
        )


# ---------------------------------------------------------------------------
# Admission preflight report
# ---------------------------------------------------------------------------


def _validated_session_tuple(
    value: Sequence[SessionPoint], field_name: str
) -> tuple[SessionPoint, ...]:
    """Require SessionPoint tuples in ascending unique date order."""

    points = tuple(value)
    for point in points:
        if not isinstance(point, SessionPoint):
            raise InvalidDataRequestError(
                f"{field_name} entries must be SessionPoint instances"
            )
    dates = [point.session_date for point in points]
    if dates != sorted(dates):
        raise InvalidDataRequestError(
            f"{field_name} must be ordered by session_date"
        )
    if len(dates) != len(set(dates)):
        raise InvalidDataRequestError(
            f"{field_name} must not repeat session dates"
        )
    return points


@dataclass(frozen=True, slots=True)
class DataPreflightReport:
    """Authoritative admission-preflight outcome for one run intent.

    ``report_hash`` is recomputed defensively on construction from the
    canonical machine content, so callers cannot forge a mismatched hash.
    Display-only content (Chinese issue messages, generation time) and
    audit-only content (calendar definition sources) never affect it.
    """

    status: PreflightStatus
    generated_at: datetime
    provider_key: str
    capability_manifest_version: int
    requested_window: DateRange
    scope_mode: InstrumentScopeMode
    resolved_calendar_ids: tuple[str, ...]
    resolved_calendar_definitions: tuple[CalendarDefinition, ...]
    resolved_timezone: str | None
    calendar_axis_policy: ContractRef
    calendar_compatibility_status: CalendarAxisStatus
    calendar_session_signature: str
    resolved_sessions: tuple[SessionPoint, ...]
    warmup_sessions: tuple[SessionPoint, ...]
    max_lookback_sessions: int
    knowledge_as_of: datetime | None
    non_strict_pit_capabilities: tuple[DataCapability, ...]
    consistency_mode: ConsistencyMode
    consistency_token_capability: bool
    consistency_token_contract: ContractRef | None
    data_chunk_policy: ContractRef
    data_chunk_size_sessions: int
    required_capabilities: tuple[DataCapability, ...]
    rule_package: ContractRef
    rule_exception_set: ContractRef | None
    static_instrument_ids: tuple[UUID, ...]
    mandatory_instrument_ids: tuple[UUID, ...]
    strategy_price_bases: tuple[PriceBasis, ...]
    engine_price_basis: PriceBasis
    # Full request semantics bound into the report (and therefore the hash).
    # Defaults exist purely for dataclass field ordering; they are validated.
    data_contract_version: int = DATA_CONTRACT_VERSION
    frequency: str = ""
    warmup_sessions_count: int = 0
    market_scope: MarketScope | None = None
    universe_query_policy: UniverseQueryPolicy | None = None
    allowed_settlement_rule_class: str | None = None
    adjustment_series_policy: ContractRef | None = None
    quality_mode: QualityMode = QualityMode.STRICT
    coverage_reports: tuple[DataCoverageReport, ...] = ()
    source_revisions: Mapping[str, str] | None = None
    issues: tuple[PreflightIssue, ...] = ()
    # Warmup-resolution audit fields (task 02-02); ``warmup_sessions_count``
    # above is the requested warmup count.  Defaults are deterministic so
    # reports without a warmup attempt keep one canonical form.
    warmup_resolution: "WarmupResolution | None" = None
    warmup_resolution_signature: str | None = None
    calendar_axis_differences: tuple[CalendarAxisDifference, ...] = ()
    warmup_axis_differences: tuple[CalendarAxisDifference, ...] = ()
    # Recomputed in __post_init__; the placeholder keeps the field defaulted.
    report_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, PreflightStatus):
            raise InvalidDataRequestError("status must be a PreflightStatus")
        object.__setattr__(
            self, "generated_at", _aware_datetime(self.generated_at, "generated_at")
        )
        object.__setattr__(
            self, "provider_key", _non_blank_text(self.provider_key, "provider_key")
        )
        version = self.capability_manifest_version
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise InvalidDataRequestError(
                "capability_manifest_version must be a positive integer"
            )
        if not isinstance(self.requested_window, DateRange):
            raise InvalidDataRequestError("requested_window must be a DateRange")
        if not isinstance(self.scope_mode, InstrumentScopeMode):
            raise InvalidDataRequestError("scope_mode must be an InstrumentScopeMode")
        object.__setattr__(
            self,
            "resolved_calendar_ids",
            # An empty tuple is legitimate for reports that never resolved a
            # calendar axis (blocked runs); DataRequest still requires a
            # non-empty resolution at admission time.
            _sorted_unique_text(
                self.resolved_calendar_ids, "resolved_calendar_ids", allow_empty=True
            ),
        )
        if not self.resolved_calendar_ids:
            # An unresolved calendar axis is only expressible in a fully
            # blocked, incompatible report: no timezone, no signature, and
            # (via the incompatible branch below) no formal sessions.
            if self.status is not PreflightStatus.BLOCKED:
                raise InvalidDataRequestError(
                    "only blocked reports may leave the calendar axis "
                    "unresolved"
                )
            if (
                self.calendar_compatibility_status
                is not CalendarAxisStatus.INCOMPATIBLE
            ):
                raise InvalidDataRequestError(
                    "an unresolved calendar axis must be reported as "
                    "incompatible"
                )
            if self.resolved_timezone is not None:
                raise InvalidDataRequestError(
                    "a report with an unresolved calendar axis must not "
                    "declare a resolved timezone"
                )
        definitions = tuple(self.resolved_calendar_definitions)
        for definition in definitions:
            if not isinstance(definition, CalendarDefinition):
                raise InvalidDataRequestError(
                    "resolved_calendar_definitions entries must be "
                    "CalendarDefinition instances"
                )
        object.__setattr__(
            self,
            "resolved_calendar_definitions",
            tuple(
                sorted(
                    set(definitions),
                    key=lambda d: (d.calendar_id, d.definition_version),
                )
            ),
        )
        if self.resolved_timezone is not None:
            timezone = _non_blank_text(self.resolved_timezone, "resolved_timezone")
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise InvalidDataRequestError(
                    "resolved_timezone must be a resolvable IANA time-zone name"
                ) from exc
            object.__setattr__(self, "resolved_timezone", timezone)
        if self.calendar_axis_policy != CALENDAR_AXIS_POLICY:
            raise InvalidDataRequestError(
                "data-contract version 1 requires the strict_compatible@1 "
                "calendar axis policy"
            )
        if not isinstance(
            self.calendar_compatibility_status, CalendarAxisStatus
        ):
            raise InvalidDataRequestError(
                "calendar_compatibility_status must be a CalendarAxisStatus"
            )
        compatible = (
            self.calendar_compatibility_status is CalendarAxisStatus.COMPATIBLE
        )
        if compatible:
            # A fully compatible calendar is the precondition for publishing
            # a session signature and formal sessions at all.
            object.__setattr__(
                self,
                "calendar_session_signature",
                _non_blank_text(
                    self.calendar_session_signature,
                    "calendar_session_signature",
                ),
            )
        else:
            # Calendar incompatibility blocks the run outright.
            if self.status is not PreflightStatus.BLOCKED:
                raise InvalidDataRequestError(
                    "an incompatible calendar axis forces status=blocked"
                )
            if self.calendar_session_signature:
                raise InvalidDataRequestError(
                    "an incompatible calendar axis must not carry a "
                    "session signature"
                )
        sessions = _validated_session_tuple(self.resolved_sessions, "resolved_sessions")
        warmup = _validated_session_tuple(self.warmup_sessions, "warmup_sessions")
        if self.status is PreflightStatus.BLOCKED and sessions:
            raise InvalidDataRequestError(
                "a blocked report must not carry resolved_sessions"
            )
        if not compatible and warmup:
            raise InvalidDataRequestError(
                "warmup sessions require a compatible calendar axis"
            )
        official_dates = {point.session_date for point in sessions}
        for point in warmup:
            if point.session_date in official_dates:
                raise InvalidDataRequestError(
                    "warmup sessions must be kept separate from official "
                    "sessions"
                )
        object.__setattr__(self, "resolved_sessions", sessions)
        object.__setattr__(self, "warmup_sessions", warmup)
        # Warmup audit fields: the mounted resolution object, its mirrored
        # signature, the difference evidence, and the session tuples must
        # stay mutually consistent with the overall status.
        if self.status is PreflightStatus.BLOCKED and warmup:
            raise InvalidDataRequestError(
                "a blocked report must not carry warmup_sessions"
            )
        if self.warmup_sessions_count == 0 and warmup:
            raise InvalidDataRequestError(
                "warmup_sessions must be empty when no warmup was requested"
            )
        # Copy, type-check, sort, and freeze the warmup difference evidence
        # before anything compares it against the mounted resolution.
        warmup_differences = tuple(self.warmup_axis_differences)
        for difference in warmup_differences:
            if not isinstance(difference, CalendarAxisDifference):
                raise InvalidDataRequestError(
                    "warmup_axis_differences entries must be "
                    "CalendarAxisDifference instances"
                )
        warmup_differences = tuple(
            sorted(warmup_differences, key=lambda item: item.sort_key)
        )
        object.__setattr__(
            self, "warmup_axis_differences", warmup_differences
        )
        if self.warmup_resolution is not None:
            from app.backtesting.data.warmup import WarmupResolution, WarmupStatus

            if not isinstance(self.warmup_resolution, WarmupResolution):
                raise InvalidDataRequestError(
                    "warmup_resolution must be a WarmupResolution instance"
                )
            resolution = self.warmup_resolution
            if self.warmup_resolution_signature != resolution.resolution_signature:
                raise InvalidDataRequestError(
                    "warmup_resolution_signature must equal the mounted "
                    "warmup resolution signature"
                )
            if resolution.requested_sessions != self.warmup_sessions_count:
                raise InvalidDataRequestError(
                    "warmup_resolution requested count must equal "
                    "warmup_sessions_count"
                )
            if resolution.status is WarmupStatus.READY and (
                len(warmup) != len(resolution.resolved_sessions)
                or warmup != resolution.resolved_sessions
            ):
                raise InvalidDataRequestError(
                    "a ready warmup resolution requires the report to carry "
                    "exactly the resolved warmup sessions"
                )
            if (
                resolution.status is WarmupStatus.READY
                and sessions
                and resolution.first_formal_session != sessions[0].session_date
            ):
                raise InvalidDataRequestError(
                    "the mounted warmup anchor must equal the first formal "
                    "session of this report"
                )
            if warmup_differences != tuple(resolution.axis_differences):
                raise InvalidDataRequestError(
                    "warmup_axis_differences must equal the axis "
                    "differences of the mounted warmup resolution"
                )
            if (
                self.status is PreflightStatus.BLOCKED
                and resolution.status is WarmupStatus.READY
            ):
                raise InvalidDataRequestError(
                    "a blocked report cannot mount a ready warmup resolution"
                )
            if (
                self.status is PreflightStatus.READY
                and resolution.status is not WarmupStatus.READY
            ):
                raise InvalidDataRequestError(
                    "a ready report cannot mount a blocked warmup resolution"
                )
        elif self.warmup_resolution_signature is not None:
            raise InvalidDataRequestError(
                "warmup_resolution_signature requires a mounted "
                "warmup_resolution"
            )
        elif warmup_differences:
            # Difference evidence only exists as part of a mounted warmup
            # resolution; it can never be fabricated standalone.
            raise InvalidDataRequestError(
                "warmup_axis_differences require a mounted warmup_resolution"
            )
        if (
            self.status is PreflightStatus.READY
            and self.warmup_sessions_count > 0
            and (self.warmup_resolution is None or len(warmup) == 0)
        ):
            raise InvalidDataRequestError(
                "a ready report with requested warmup sessions requires a "
                "ready warmup resolution and the resolved warmup sessions"
            )
        differences = tuple(self.calendar_axis_differences)
        for difference in differences:
            if not isinstance(difference, CalendarAxisDifference):
                raise InvalidDataRequestError(
                    "calendar_axis_differences entries must be "
                    "CalendarAxisDifference instances"
                )
        if differences and compatible:
            raise InvalidDataRequestError(
                "a compatible calendar axis cannot carry differences"
            )
        object.__setattr__(
            self,
            "calendar_axis_differences",
            tuple(sorted(differences, key=lambda item: item.sort_key)),
        )
        maximum = self.max_lookback_sessions
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise InvalidDataRequestError(
                "max_lookback_sessions must be an integer"
            )
        if maximum != MAX_LOOKBACK_SESSIONS:
            raise InvalidDataRequestError(
                f"data-contract version 1 fixes max_lookback_sessions to "
                f"{MAX_LOOKBACK_SESSIONS}"
            )
        if self.knowledge_as_of is not None:
            object.__setattr__(
                self,
                "knowledge_as_of",
                _aware_datetime(self.knowledge_as_of, "knowledge_as_of"),
            )
        object.__setattr__(
            self,
            "non_strict_pit_capabilities",
            _sorted_unique_enum(
                self.non_strict_pit_capabilities,
                DataCapability,
                "non_strict_pit_capabilities",
                allow_empty=True,
            ),
        )
        if not isinstance(self.consistency_mode, ConsistencyMode):
            raise InvalidDataRequestError(
                "consistency_mode must be a ConsistencyMode"
            )
        if not isinstance(self.consistency_token_capability, bool):
            raise InvalidDataRequestError(
                "consistency_token_capability must be a boolean"
            )
        if self.consistency_token_contract is not None and not isinstance(
            self.consistency_token_contract, ContractRef
        ):
            raise InvalidDataRequestError(
                "consistency_token_contract must be a ContractRef when provided"
            )
        if (
            self.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
            and self.consistency_token_contract is None
        ):
            raise InvalidDataRequestError(
                "chunked_logical_token consistency requires a "
                "consistency_token_contract"
            )
        # The declared token capability must not contradict the mode: the
        # logical-token mode always advertises token support, while the
        # transitional repeatable-read mode has no logical token at all.
        if self.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN:
            if not self.consistency_token_capability:
                raise InvalidDataRequestError(
                    "chunked_logical_token consistency requires "
                    "consistency_token_capability=True"
                )
        elif self.consistency_token_capability:
            raise InvalidDataRequestError(
                "transitional_repeatable_read has no logical token and must "
                "not advertise consistency_token_capability"
            )
        # Validate the request-semantics fields bound into this report.
        contract_version = _strict_version(self.data_contract_version)
        object.__setattr__(self, "data_contract_version", contract_version)
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        warmup_count = self.warmup_sessions_count
        if isinstance(warmup_count, bool) or not isinstance(warmup_count, int) or (
            warmup_count < 0
        ):
            raise InvalidDataRequestError(
                "warmup_sessions_count must be a non-negative integer"
            )
        if not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope")
        if not isinstance(self.universe_query_policy, UniverseQueryPolicy):
            raise InvalidDataRequestError(
                "universe_query_policy must be a UniverseQueryPolicy"
            )
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
        if not isinstance(self.quality_mode, QualityMode):
            raise InvalidDataRequestError("quality_mode must be a QualityMode")
        if self.data_chunk_policy != CHUNK_POLICY:
            raise InvalidDataRequestError(
                "data-contract version 1 requires the fixed_trading_sessions@1 "
                "chunk policy"
            )
        chunk_size = self.data_chunk_size_sessions
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise InvalidDataRequestError(
                "data_chunk_size_sessions must be an integer"
            )
        if chunk_size != 20:
            raise InvalidDataRequestError(
                "data-contract version 1 fixes data_chunk_size_sessions to 20"
            )
        object.__setattr__(
            self,
            "required_capabilities",
            _sorted_unique_enum(
                self.required_capabilities, DataCapability, "required_capabilities"
            ),
        )
        if not isinstance(self.rule_package, ContractRef):
            raise InvalidDataRequestError("rule_package must be a ContractRef")
        if self.rule_exception_set is not None and not isinstance(
            self.rule_exception_set, ContractRef
        ):
            raise InvalidDataRequestError(
                "rule_exception_set must be a ContractRef when provided"
            )
        for field_name in ("static_instrument_ids", "mandatory_instrument_ids"):
            ids = getattr(self, field_name)
            for instrument_id in ids:
                if not isinstance(instrument_id, UUID):
                    raise InvalidDataRequestError(
                        f"{field_name} entries must be UUIDs"
                    )
            object.__setattr__(
                self, field_name, tuple(sorted(set(ids), key=str))
            )
        object.__setattr__(
            self,
            "strategy_price_bases",
            _sorted_unique_enum(
                self.strategy_price_bases, PriceBasis, "strategy_price_bases"
            ),
        )
        if not isinstance(self.engine_price_basis, PriceBasis):
            raise InvalidDataRequestError(
                "engine_price_basis must be a PriceBasis"
            )
        if self.engine_price_basis is not PriceBasis.RAW:
            raise InvalidDataRequestError(
                "data-contract version 1 requires engine_price_basis=raw"
            )
        coverages = tuple(self.coverage_reports)
        for report in coverages:
            if not isinstance(report, DataCoverageReport):
                raise InvalidDataRequestError(
                    "coverage_reports entries must be DataCoverageReport"
                )
        object.__setattr__(
            self, "coverage_reports", tuple(sorted(coverages))
        )
        revisions = self.source_revisions or {}
        if not isinstance(revisions, Mapping):
            raise InvalidDataRequestError("source_revisions must be a mapping")
        normalized_revisions: dict[str, str] = {}
        for source, revision in revisions.items():
            if type(source) is not str or not source.strip():
                raise InvalidDataRequestError(
                    "source_revisions keys must be non-blank strings"
                )
            if type(revision) is not str or not revision.strip():
                raise InvalidDataRequestError(
                    "source_revisions values must be non-blank strings"
                )
            normalized_revisions[source] = revision
        object.__setattr__(
            self, "source_revisions", MappingProxyType(normalized_revisions)
        )
        issues = _sorted_issues(self.issues)
        errors = [
            issue for issue in issues
            if issue.severity is IssueSeverity.ERROR
        ]
        warnings = [
            issue for issue in issues
            if issue.severity is IssueSeverity.WARNING
        ]
        if self.status is PreflightStatus.READY and errors:
            raise InvalidDataRequestError("ready reports must not carry error issues")
        if self.status is PreflightStatus.DEGRADED:
            if errors:
                raise InvalidDataRequestError(
                    "degraded reports must not carry error issues"
                )
            if not warnings:
                raise InvalidDataRequestError(
                    "degraded reports must carry at least one warning"
                )
        if self.status is PreflightStatus.BLOCKED and not errors:
            raise InvalidDataRequestError(
                "blocked reports must carry at least one error issue"
            )
        object.__setattr__(self, "issues", issues)
        # Recompute defensively so a caller cannot forge a mismatched hash.
        object.__setattr__(self, "report_hash", self._compute_hash())

    def _hash_content(self) -> dict[str, object]:
        """Build the hash-relevant machine content of this report.

        Excluded deliberately: ``generated_at``, issue ``message`` texts,
        and ``resolved_calendar_definitions`` (their semantics are already
        covered by the calendar session signature).
        """

        def sessions_payload(points: Sequence[SessionPoint]) -> list[dict[str, object]]:
            return [
                {
                    "session_date": point.session_date,
                    "session_id": point.session_id,
                    "timezone": point.timezone,
                    "sessions": [
                        {"start": w.start_time, "end": w.end_time}
                        for w in point.sessions
                    ],
                }
                for point in points
            ]

        return {
            "status": self.status,
            "provider_key": self.provider_key,
            "capability_manifest_version": self.capability_manifest_version,
            "data_contract_version": self.data_contract_version,
            "requested_window": {
                "start_date": self.requested_window.start_date,
                "end_date": self.requested_window.end_date,
            },
            "frequency": self.frequency,
            "warmup_sessions_count": self.warmup_sessions_count,
            "market_scope": {
                "markets": self.market_scope.markets if self.market_scope else (),
                "exchanges": self.market_scope.exchanges if self.market_scope else (),
                "asset_classes": (
                    self.market_scope.asset_classes if self.market_scope else ()
                ),
                "currencies": self.market_scope.currencies if self.market_scope else (),
            },
            "universe_query_policy": {
                "candidate_set_rules": [
                    {"key": rule.key, "version": rule.version}
                    for rule in (
                        self.universe_query_policy.candidate_set_rules
                        if self.universe_query_policy
                        else ()
                    )
                ]
            },
            "scope_mode": self.scope_mode,
            "static_instrument_ids": [
                str(item) for item in self.static_instrument_ids
            ],
            "mandatory_instrument_ids": [
                str(item) for item in self.mandatory_instrument_ids
            ],
            "rule_package": {"key": self.rule_package.key, "version": self.rule_package.version},
            "rule_exception_set": (
                {"key": self.rule_exception_set.key, "version": self.rule_exception_set.version}
                if self.rule_exception_set
                else None
            ),
            "allowed_settlement_rule_class": self.allowed_settlement_rule_class,
            "adjustment_series_policy": (
                {
                    "key": self.adjustment_series_policy.key,
                    "version": self.adjustment_series_policy.version,
                }
                if self.adjustment_series_policy
                else None
            ),
            "quality_mode": self.quality_mode,
            "required_capabilities": self.required_capabilities,
            "strategy_price_bases": self.strategy_price_bases,
            "engine_price_basis": self.engine_price_basis,
            "resolved_calendar_ids": self.resolved_calendar_ids,
            "resolved_timezone": self.resolved_timezone,
            "calendar_axis_policy": {
                "key": self.calendar_axis_policy.key,
                "version": self.calendar_axis_policy.version,
            },
            "calendar_compatibility_status": self.calendar_compatibility_status,
            "calendar_session_signature": self.calendar_session_signature,
            "resolved_sessions": sessions_payload(self.resolved_sessions),
            "warmup_sessions": sessions_payload(self.warmup_sessions),
            "warmup_resolution_signature": self.warmup_resolution_signature,
            "calendar_axis_differences": [
                {
                    "date": difference.date,
                    "calendar_id": difference.calendar_id,
                    "field": difference.field,
                    "actual_value": difference.actual_value,
                    "expected_value": difference.expected_value,
                }
                for difference in self.calendar_axis_differences
            ],
            "warmup_axis_differences": [
                {
                    "date": difference.date,
                    "calendar_id": difference.calendar_id,
                    "field": difference.field,
                    "actual_value": difference.actual_value,
                    "expected_value": difference.expected_value,
                }
                for difference in self.warmup_axis_differences
            ],
            "max_lookback_sessions": self.max_lookback_sessions,
            "knowledge_as_of": self.knowledge_as_of,
            "non_strict_pit_capabilities": self.non_strict_pit_capabilities,
            "consistency_mode": self.consistency_mode,
            "consistency_token_capability": self.consistency_token_capability,
            "consistency_token_contract": (
                {
                    "key": self.consistency_token_contract.key,
                    "version": self.consistency_token_contract.version,
                }
                if self.consistency_token_contract
                else None
            ),
            "data_chunk_policy": {
                "key": self.data_chunk_policy.key,
                "version": self.data_chunk_policy.version,
            },
            "data_chunk_size_sessions": self.data_chunk_size_sessions,
            "coverage_reports": [
                report.machine_content() for report in self.coverage_reports
            ],
            "source_revisions": self.source_revisions,
            "issues": [issue.machine_fields() for issue in self.issues],
        }

    def _compute_hash(self) -> str:
        return canonical_hash(self._hash_content())
