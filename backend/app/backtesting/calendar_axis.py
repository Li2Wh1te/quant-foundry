"""Named trading-calendar contracts and the strict compatible daily axis.

The module is intentionally a pure domain boundary.  It contains the
canonical calendar identifiers, append-only fact value objects, point-in-time
selection algorithm, the in-memory provider used by tests, and immutable
calendar snapshots.  SQL/HTTP/ingestion adapters may project into these
objects, but the domain never imports those frameworks and never infers a
missing day or a default trading time.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.backtesting.domain import DomainValidationError
from app.backtesting.data.errors import (
    CalendarBindingAmbiguousError,
    CalendarBindingUnknownError,
    CalendarCapabilityDeclarationAmbiguousError,
    CalendarContractError,
    CalendarCrossMidnightUnsupportedError,
    CalendarDateSpanLimitExceededError,
    CalendarDefinitionAmbiguousError,
    CalendarDefinitionInvalidError,
    CalendarDefinitionMissingError,
    CalendarFactAmbiguousError,
    CalendarFactInvalidError,
    CalendarFactMissingError,
    CalendarIdSetEmptyError,
    CalendarIdUnknownError,
    CalendarJsonInvalidError,
    CalendarPitMetadataMissingError,
    CalendarPreflightResourceLimitExceededError,
    LookbackSessionsLimitExceededError,
    CalendarRegistryAmbiguousError,
    CalendarRegistryFactMissingError,
    CalendarRegistryReferenceInvalidError,
    CalendarSessionIncompatibleError,
    CalendarSessionInvalidError,
    CalendarSessionUnresolvedError,
    CalendarSessionWindowLimitExceededError,
    CalendarSnapshotCoverageUnknownError,
    CalendarTimezoneInconsistentError,
    CalendarTimezoneMismatchError,
    CalendarTimezoneUnsupportedError,
    CalendarSourcePriorityAmbiguousError,
    CalendarSourcePriorityChainBrokenError,
    CalendarSourcePriorityInvalidError,
    CalendarSourceRevisionConflictError,
    CalendarSourcePriorityMissingError,
    DataCutoffExceededError,
    DataCutoffRequiredError,
    InvalidDataRequestError,
    ProviderContractViolationError,
    freeze_json,
)

POLICY_KEY_STRICT_COMPATIBLE = "strict_compatible"
POLICY_VERSION_STRICT_COMPATIBLE = "1"
CALENDAR_PIT_PROFILE_VERSION = "calendar_pit_profile@1:H"
PIT_PROFILE_STRICT_CALENDAR_CUTOFF = "strict_calendar_cutoff"
PIT_PROFILE_STRICT_HISTORICAL_COGNITION = "strict_historical_cognition"
CALENDAR_TIMEZONE_ASIA_SHANGHAI = "Asia/Shanghai"
MAX_CALENDAR_IDS = 32
MAX_FORMAL_DATE_SPAN = 10_000
MAX_WARMUP_SESSIONS = 512
MAX_WARMUP_SEARCH_SPAN = 10_000
MAX_PREFLIGHT_ISSUE_GROUPS = 4_096
# Backward-compatible spelling retained for callers that imported the early
# draft constant; all new code uses the correctly named limit above.
MAX_PRELIGHT_ISSUE_GROUPS = MAX_PREFLIGHT_ISSUE_GROUPS
MAX_PREFLIGHT_JSON_BYTES = 4 * 1024 * 1024

_CALENDAR_ID_RE = re.compile(r"^[A-Z][A-Z0-9._-]{0,31}$")
_WINDOW_KEYS = frozenset({"start", "end", "day_offset", "end_day_offset", "label"})
_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9](?:\.[0-9]{1,6})?)?$")


class CalendarDomainError(DomainValidationError):
    """A pure-domain failure carrying a stable machine code and JSON details."""

    code = "calendar_contract_error"

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = freeze_json(details or {}, "details")


class _CalendarError(CalendarDomainError):
    """Internal helper for specialised domain errors."""


# ---------------------------------------------------------------------------
# Small canonical helpers
# ---------------------------------------------------------------------------


def _raise(code: str, message: str, details: Mapping[str, object] | None = None) -> None:
    """Raise a domain error without importing a second parser or error map."""

    classes = {
        "calendar_json_invalid": CalendarJsonInvalidError,
        "calendar_session_invalid": CalendarSessionInvalidError,
        "calendar_session_window_limit_exceeded": CalendarContractError,
        "calendar_cross_midnight_unsupported": CalendarCrossMidnightUnsupportedError,
        "calendar_definition_invalid": CalendarDefinitionInvalidError,
        "calendar_fact_invalid": CalendarFactInvalidError,
        "calendar_session_unresolved": CalendarSessionUnresolvedError,
    }
    error_type = classes.get(code)
    if error_type is not None:
        raise error_type(message, details=details)
    raise CalendarDomainError(message, details=details)


def normalize_calendar_id(value: object, *, known_ids: Iterable[str] | None = None) -> str:
    """Normalize one canonical calendar id using ASCII-only upper-casing.

    Non-ASCII characters are intentionally rejected rather than folded.  A
    binding/registry lookup is optional at this boundary; when supplied it
    provides the separate ``calendar_id_unknown`` check required by strict
    callers.
    """

    if not isinstance(value, str) or not value.strip():
        raise CalendarIdUnknownError("calendar_id must be a non-empty string")
    text = value.strip()
    if any(ord(ch) > 0x7F for ch in text):
        raise CalendarIdUnknownError("calendar_id must contain ASCII characters only")
    canonical = "".join(ch.upper() if "a" <= ch <= "z" else ch for ch in text)
    if not _CALENDAR_ID_RE.fullmatch(canonical):
        raise CalendarIdUnknownError(f"invalid calendar_id format: {value!r}")
    if known_ids is not None and canonical not in {normalize_calendar_id(item) for item in known_ids}:
        raise CalendarIdUnknownError(f"unknown calendar_id: {canonical}")
    return canonical


def _non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalendarDomainError(f"{field_name} must be non-blank text")
    return value.strip()


def _plain_date(value: object, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise CalendarDomainError(f"{field_name} must be a calendar date")
    return value


def _aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CalendarDomainError(f"{field_name} must be timezone-aware")
    return value


def _uuid(value: object, field_name: str, *, optional: bool = False) -> UUID | None:
    if value is None and optional:
        return None
    if not isinstance(value, UUID):
        raise CalendarDomainError(f"{field_name} must be a UUID")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CalendarDomainError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CalendarDomainError(f"{field_name} must be a non-negative integer")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CalendarDomainError(f"{field_name} must be text when provided")
    return value.strip() or None


def _sha256_text(value: object, field_name: str, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise CalendarJsonInvalidError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _validate_content_hash(value: object, payload: object, field_name: str) -> str:
    """Validate an explicitly persisted semantic content hash.

    Constructors derive the hash when callers omit it, but persistence
    boundaries may provide a value loaded from storage.  Strict validation
    must reject both malformed digests and a digest that does not describe
    the object's canonical semantic payload; otherwise revision evidence can
    be forged while still passing the PIT admission gate.
    """

    digest = _sha256_text(value, field_name, optional=False)
    assert digest is not None
    expected = canonical_hash(payload)
    if digest != expected:
        raise CalendarJsonInvalidError(
            f"{field_name} does not match semantic payload",
            details={"expected": expected, "actual": digest},
        )
    return digest


def _content_hash_or_derive(value: object, payload: object, field_name: str) -> str:
    """Derive omitted hashes and reject forged persisted hashes at construction."""

    if value is None:
        return canonical_hash(payload)
    return _validate_content_hash(value, payload, field_name)


def _timezone_name(value: object, field_name: str) -> str:
    text = _non_blank_text(value, field_name)
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CalendarDomainError(f"{field_name} must be a resolvable IANA timezone") from exc
    return text


def _legacy_uuid(kind: str, logical_key: str, version: int, content: object = None) -> UUID:
    """Create repeatable IDs for old constructors that predate fact IDs."""

    payload = json.dumps(
        {"kind": kind, "logical_key": logical_key, "version": version, "content": content},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return uuid5(NAMESPACE_URL, f"quant-foundry:calendar:{payload}")


def _canonical_json(value: object) -> str:
    """Serialize pure JSON values without importing the report layer."""

    def convert(item: object) -> object:
        if isinstance(item, StrEnum):
            return item.value
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime):
            return item.astimezone(timezone.utc).isoformat()
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, time):
            return item.isoformat()
        if isinstance(item, Mapping):
            return {str(key): convert(val) for key, val in sorted(item.items(), key=lambda pair: str(pair[0]))}
        if isinstance(item, (list, tuple)):
            return [convert(val) for val in item]
        return item

    return json.dumps(convert(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: object) -> str:
    """SHA-256 for calendar payloads; reports re-export the same semantics."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _format_time(value: time) -> str:
    if value.second == 0 and value.microsecond == 0:
        return value.strftime("%H:%M")
    if value.microsecond == 0:
        return value.strftime("%H:%M:%S")
    return value.strftime("%H:%M:%S.%f")


def _parse_time_text(value: object, field_name: str) -> time:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        raise CalendarJsonInvalidError(f"{field_name} must be HH:MM[:SS[.ffffff]]")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise CalendarJsonInvalidError(f"{field_name} is not a valid local time") from exc
    return parsed


# ---------------------------------------------------------------------------
# Session windows and fact value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """One local half-open session window; v1 only permits same-day offsets."""

    start_time: time
    end_time: time
    label: str | None = None
    day_offset: int = 0
    end_day_offset: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.start_time, time) or (
            self.start_time.tzinfo is not None and self.start_time.utcoffset() is not None
        ):
            raise CalendarDomainError("start_time must be a naive local time")
        if not isinstance(self.end_time, time) or (
            self.end_time.tzinfo is not None and self.end_time.utcoffset() is not None
        ):
            raise CalendarDomainError("end_time must be a naive local time")
        if isinstance(self.day_offset, bool) or not isinstance(self.day_offset, int):
            raise CalendarJsonInvalidError("day_offset must be an integer")
        if isinstance(self.end_day_offset, bool) or not isinstance(self.end_day_offset, int):
            raise CalendarJsonInvalidError("end_day_offset must be an integer")
        if self.day_offset != 0 or self.end_day_offset != 0:
            raise CalendarCrossMidnightUnsupportedError(
                "v1 calendar session windows must use day_offset=end_day_offset=0",
                details={"day_offset": self.day_offset, "end_day_offset": self.end_day_offset},
            )
        if self.start_time >= self.end_time:
            raise CalendarCrossMidnightUnsupportedError(
                "v1 calendar session windows must end after they start",
                details={"start": _format_time(self.start_time), "end": _format_time(self.end_time)},
            )
        object.__setattr__(self, "label", _optional_text(self.label, "label"))

    @property
    def start(self) -> time:
        """Compatibility alias for callers that use the JSON field name."""

        return self.start_time

    @property
    def end(self) -> time:
        """Compatibility alias for callers that use the JSON field name."""

        return self.end_time

    def semantic_payload(self) -> dict[str, object]:
        return {
            "start": _format_time(self.start_time),
            "end": _format_time(self.end_time),
            "day_offset": self.day_offset,
            "end_day_offset": self.end_day_offset,
        }


def normalize_session_windows(
    value: Iterable[SessionWindow | tuple[time, time] | tuple[time, time, str | None]],
    field_name: str,
) -> tuple[SessionWindow, ...]:
    """Normalize typed windows, preserving same-day semantics and rejecting overlap."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise CalendarDomainError(f"{field_name} must be an iterable of sessions")
    try:
        items = list(value)
    except TypeError as exc:
        raise CalendarDomainError(f"{field_name} must be an iterable of sessions") from exc
    if len(items) > 16:
        raise CalendarSessionWindowLimitExceededError(
            f"{field_name} contains more than 16 windows",
            details={"count": len(items), "maximum": 16},
        )
    windows: list[SessionWindow] = []
    for item in items:
        if isinstance(item, SessionWindow):
            windows.append(item)
        elif isinstance(item, tuple):
            try:
                windows.append(SessionWindow(*item))
            except TypeError as exc:
                raise CalendarDomainError(f"{field_name} contains an invalid session entry") from exc
        else:
            raise CalendarDomainError(f"{field_name} entries must be SessionWindow or time tuples")
    windows.sort(key=lambda item: (item.day_offset, item.start_time, item.end_time))
    for earlier, later in zip(windows, windows[1:]):
        if later.start_time < earlier.end_time:
            raise CalendarSessionInvalidError(
                f"{field_name} must not contain overlapping sessions",
                details={
                    "previous": [
                        _format_time(earlier.start_time),
                        _format_time(earlier.end_time),
                    ],
                    "current": [_format_time(later.start_time), _format_time(later.end_time)],
                },
            )
    return tuple(windows)


def normalize_window_payloads(value: object, field_name: str = "sessions") -> tuple[SessionWindow, ...]:
    """Parse strict v1 JSON windows with the documented error precedence."""

    if not isinstance(value, list):
        raise CalendarJsonInvalidError(f"{field_name} must be an array")
    # Validate JSON shape before applying the cardinality limit so the
    # documented precedence is stable: malformed JSON first, then window
    # count, then cross-midnight/session semantics.
    required = {"start", "end", "day_offset", "end_day_offset"}
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or any(type(key) is not str for key in raw):
            raise CalendarJsonInvalidError(f"{field_name}[{index}] must be an object")
        unknown = set(raw) - _WINDOW_KEYS
        if unknown or not required <= set(raw):
            raise CalendarJsonInvalidError(
                f"{field_name}[{index}] has unknown or missing fields",
                details={"unknown": sorted(unknown), "required": sorted(required)},
            )
        if raw.get("label") is not None and not isinstance(raw.get("label"), str):
            raise CalendarJsonInvalidError(f"{field_name}[{index}].label must be text")
    if len(value) > 16:
        raise CalendarSessionWindowLimitExceededError(
            f"{field_name} contains more than 16 windows",
            details={"count": len(value), "maximum": 16},
        )
    windows: list[SessionWindow] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or any(type(key) is not str for key in raw):
            raise CalendarJsonInvalidError(f"{field_name}[{index}] must be an object")
        label = raw.get("label")
        if label is not None and not isinstance(label, str):
            raise CalendarJsonInvalidError(f"{field_name}[{index}].label must be text")
        start = _parse_time_text(raw["start"], f"{field_name}[{index}].start")
        end = _parse_time_text(raw["end"], f"{field_name}[{index}].end")
        day_offset = raw["day_offset"]
        end_day_offset = raw["end_day_offset"]
        if isinstance(day_offset, bool) or not isinstance(day_offset, int):
            raise CalendarJsonInvalidError(f"{field_name}[{index}].day_offset must be an integer")
        if isinstance(end_day_offset, bool) or not isinstance(end_day_offset, int):
            raise CalendarJsonInvalidError(f"{field_name}[{index}].end_day_offset must be an integer")
        if day_offset != 0 or end_day_offset != 0:
            raise CalendarCrossMidnightUnsupportedError(
                "v1 calendar session windows do not support cross-midnight offsets",
                details={"day_offset": day_offset, "end_day_offset": end_day_offset},
            )
        if start >= end:
            raise CalendarCrossMidnightUnsupportedError(
                "v1 calendar session windows must end after they start",
                details={"start": _format_time(start), "end": _format_time(end)},
            )
        try:
            windows.append(
                SessionWindow(
                    start_time=start,
                    end_time=end,
                    label=label,
                    day_offset=day_offset,
                    end_day_offset=end_day_offset,
                )
            )
        except CalendarContractError:
            raise
        except CalendarDomainError:
            raise
    # Strict JSON facts must already be in canonical order.  The typed
    # compatibility helper may sort legacy tuples, but persistence input may
    # not hide an ordering correction inside the semantic hash.
    source_order = [
        (item.day_offset, item.start_time, item.end_time) for item in windows
    ]
    if source_order != sorted(source_order):
        raise CalendarSessionInvalidError(f"{field_name} windows must be sorted by start boundary")
    for earlier, later in zip(windows, windows[1:]):
        if later.start_time < earlier.end_time:
            raise CalendarSessionInvalidError(f"{field_name} windows must not overlap")
    return tuple(windows)


class CalendarQualityStatus(StrEnum):
    ACCEPTED = "accepted"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CalendarDefinition:
    """Versioned default template for a named calendar."""

    calendar_id: str
    definition_version: str
    timezone: str
    default_sessions: tuple[SessionWindow, ...]
    valid_from: date | None = None
    valid_to: date | None = None
    source: str | None = None
    fact_id: UUID | None = None
    registry_fact_id: UUID | None = None
    registry_version: int | None = None
    fact_version: int = 1
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None
    source_revision: str | None = None
    evidence: object | None = None
    known_at: datetime | None = None
    knowledge_from: datetime | None = None
    knowledge_to: datetime | None = None
    knowledge_as_of: datetime | None = None
    observed_at: datetime | None = None
    quality_status: CalendarQualityStatus | str = CalendarQualityStatus.ACCEPTED
    source_priority_fact_id: UUID | None = None
    source_priority_version: str | None = None
    source_priority: int | None = None
    source_revision_order: int | None = None
    bootstrap_seed_id: str | None = None
    bootstrap_seed_version: int | None = None
    bootstrap_seed_hash: str | None = None
    content_hash: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        calendar_id = normalize_calendar_id(self.calendar_id)
        object.__setattr__(self, "calendar_id", calendar_id)
        object.__setattr__(self, "definition_version", _non_blank_text(self.definition_version, "definition_version"))
        object.__setattr__(self, "timezone", _timezone_name(self.timezone, "timezone"))
        object.__setattr__(self, "default_sessions", normalize_session_windows(self.default_sessions, "default_sessions"))
        valid_from = _plain_date(self.valid_from, "valid_from") if self.valid_from is not None else None
        valid_to = _plain_date(self.valid_to, "valid_to") if self.valid_to is not None else None
        if valid_from is not None and valid_to is not None and valid_to <= valid_from:
            raise CalendarDomainError("valid_to must be later than valid_from")
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)
        object.__setattr__(self, "source", _optional_text(self.source, "source"))
        object.__setattr__(self, "source_revision", _optional_text(self.source_revision, "source_revision"))
        logical = _optional_text(self.logical_fact_key, "logical_fact_key") or f"calendar_definition:{calendar_id}:template"
        object.__setattr__(self, "logical_fact_key", logical)
        version = _positive_int(self.fact_version, "fact_version")
        object.__setattr__(self, "fact_version", version)
        object.__setattr__(self, "fact_id", _uuid(self.fact_id, "fact_id", optional=True) or _legacy_uuid("definition", logical, version, self.semantic_payload()))
        object.__setattr__(self, "supersedes_fact_id", _uuid(self.supersedes_fact_id, "supersedes_fact_id", optional=True))
        if self.registry_fact_id is not None:
            object.__setattr__(self, "registry_fact_id", _uuid(self.registry_fact_id, "registry_fact_id"))
        if self.registry_version is not None:
            object.__setattr__(self, "registry_version", _positive_int(self.registry_version, "registry_version"))
        if self.known_at is not None:
            object.__setattr__(self, "known_at", _aware_datetime(self.known_at, "known_at"))
        if self.knowledge_from is not None:
            object.__setattr__(self, "knowledge_from", _aware_datetime(self.knowledge_from, "knowledge_from"))
        elif self.known_at is not None:
            object.__setattr__(self, "knowledge_from", self.known_at)
        if self.knowledge_to is not None:
            object.__setattr__(self, "knowledge_to", _aware_datetime(self.knowledge_to, "knowledge_to"))
            if self.knowledge_from is not None and self.knowledge_to <= self.knowledge_from:
                raise CalendarDomainError("knowledge_to must be later than knowledge_from")
        for field_name in ("knowledge_as_of", "observed_at", "created_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _aware_datetime(value, field_name))
        try:
            quality = CalendarQualityStatus(self.quality_status)
        except ValueError as exc:
            raise CalendarDefinitionInvalidError("quality_status is invalid") from exc
        object.__setattr__(self, "quality_status", quality)
        if self.source_priority is not None:
            _non_negative_int(self.source_priority, "source_priority")
        if self.source_revision_order is not None:
            _non_negative_int(self.source_revision_order, "source_revision_order")
        object.__setattr__(self, "content_hash", _content_hash_or_derive(self.content_hash, self.semantic_payload(), "definition.content_hash"))

    def applies_to(self, day: date) -> bool:
        day = _plain_date(day, "day")
        if self.valid_from is not None and day < self.valid_from:
            return False
        return self.valid_to is None or day < self.valid_to

    def strict_validate(self) -> None:
        """Validate fields mandatory for formal PIT facts."""

        try:
            freeze_json(self.evidence, "definition.evidence")
        except ValueError as exc:
            raise CalendarJsonInvalidError("definition evidence is not valid JSON") from exc
        if self.valid_from is None:
            raise CalendarDefinitionInvalidError("valid_from is required for a strict definition")
        if self.fact_id is None or self.known_at is None or self.observed_at is None:
            raise CalendarPitMetadataMissingError("strict definition PIT metadata is incomplete")
        if self.source is None or self.source_revision is None or self.evidence is None:
            raise CalendarDefinitionInvalidError("strict definition provenance is incomplete")
        if self.registry_fact_id is None or self.registry_version is None:
            raise CalendarRegistryReferenceInvalidError("strict definition registry reference is incomplete")
        if self.source_priority_fact_id is None or self.source_priority_version is None:
            raise CalendarSourcePriorityMissingError("strict definition source priority is missing")
        if self.source_priority is None or self.source_revision_order is None:
            raise CalendarSourcePriorityMissingError("strict definition source priority values are missing")
        if self.bootstrap_seed_id is None or self.bootstrap_seed_version is None:
            raise CalendarSourcePriorityMissingError("strict definition bootstrap seed is missing")
        _sha256_text(self.bootstrap_seed_hash, "bootstrap_seed_hash", optional=False)
        _validate_content_hash(self.content_hash, self.semantic_payload(), "definition.content_hash")

    def semantic_payload(self) -> dict[str, object]:
        return {
            "calendar_id": self.calendar_id,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "timezone": self.timezone,
            "default_sessions": [window.semantic_payload() for window in self.default_sessions],
        }

    def __hash__(self) -> int:
        return hash(canonical_hash(self.semantic_payload()))


@dataclass(frozen=True, slots=True)
class CalendarSessionFact:
    """Explicit open/closed fact for one calendar and natural day."""

    calendar_id: str
    session_date: date
    is_open: bool
    definition_version: str
    timezone_override: str | None = None
    sessions_override: tuple[SessionWindow, ...] | None = None
    source: str | None = None
    fact_id: UUID | None = None
    registry_fact_id: UUID | None = None
    registry_version: int | None = None
    definition_fact_id: UUID | None = None
    fact_version: int = 1
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None
    override_mode: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    source_revision: str | None = None
    evidence: object | None = None
    known_at: datetime | None = None
    knowledge_from: datetime | None = None
    knowledge_to: datetime | None = None
    knowledge_as_of: datetime | None = None
    observed_at: datetime | None = None
    quality_status: CalendarQualityStatus | str = CalendarQualityStatus.ACCEPTED
    source_priority_fact_id: UUID | None = None
    source_priority_version: str | None = None
    source_priority: int | None = None
    source_revision_order: int | None = None
    bootstrap_seed_id: str | None = None
    bootstrap_seed_version: int | None = None
    bootstrap_seed_hash: str | None = None
    content_hash: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        calendar_id = normalize_calendar_id(self.calendar_id)
        day = _plain_date(self.session_date, "session_date")
        object.__setattr__(self, "calendar_id", calendar_id)
        object.__setattr__(self, "session_date", day)
        if not isinstance(self.is_open, bool):
            raise CalendarJsonInvalidError("is_open must be a JSON boolean")
        object.__setattr__(self, "definition_version", _non_blank_text(self.definition_version, "definition_version"))
        if self.timezone_override is not None:
            object.__setattr__(self, "timezone_override", _timezone_name(self.timezone_override, "timezone_override"))
        if self.sessions_override is not None:
            object.__setattr__(self, "sessions_override", normalize_session_windows(self.sessions_override, "sessions_override"))
        mode = self.override_mode
        if mode is None:
            mode = "inherit" if self.sessions_override is None else "explicit"
        if mode not in {"inherit", "explicit"}:
            raise CalendarSessionInvalidError("override_mode must be inherit or explicit")
        if self.is_open and mode == "inherit" and self.sessions_override is not None:
            raise CalendarSessionInvalidError("inherit override_mode requires null sessions_override")
        if self.is_open and mode == "explicit" and self.sessions_override is None:
            raise CalendarSessionInvalidError("explicit override_mode requires sessions_override")
        # Legacy constructors may carry irrelevant closed-day overrides;
        # strict_validate() rejects the non-empty form before persistence,
        # while the legacy compatibility resolver intentionally ignores it.
        if not self.is_open and self.sessions_override is None:
            # Legacy closed input is normalized to an explicit empty array in
            # the domain object; the migration boundary records that this was
            # a compatibility conversion separately.
            object.__setattr__(self, "sessions_override", ())
            mode = "explicit"
        object.__setattr__(self, "override_mode", mode)
        valid_from = _plain_date(self.valid_from, "valid_from") if self.valid_from is not None else day
        valid_to = _plain_date(self.valid_to, "valid_to") if self.valid_to is not None else day + timedelta(days=1)
        if valid_from != day or valid_to != day + timedelta(days=1):
            raise CalendarFactInvalidError("session fact valid range must be [session_date, session_date+1 day)")
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)
        object.__setattr__(self, "source", _optional_text(self.source, "source"))
        object.__setattr__(self, "source_revision", _optional_text(self.source_revision, "source_revision"))
        logical = _optional_text(self.logical_fact_key, "logical_fact_key") or f"calendar_session:{calendar_id}:{day.isoformat()}"
        object.__setattr__(self, "logical_fact_key", logical)
        version = _positive_int(self.fact_version, "fact_version")
        object.__setattr__(self, "fact_version", version)
        object.__setattr__(self, "fact_id", _uuid(self.fact_id, "fact_id", optional=True) or _legacy_uuid("session", logical, version, self.semantic_payload()))
        object.__setattr__(self, "supersedes_fact_id", _uuid(self.supersedes_fact_id, "supersedes_fact_id", optional=True))
        if self.registry_fact_id is not None:
            object.__setattr__(self, "registry_fact_id", _uuid(self.registry_fact_id, "registry_fact_id"))
        if self.registry_version is not None:
            object.__setattr__(self, "registry_version", _positive_int(self.registry_version, "registry_version"))
        if self.definition_fact_id is not None:
            object.__setattr__(self, "definition_fact_id", _uuid(self.definition_fact_id, "definition_fact_id"))
        for name in ("known_at", "knowledge_from", "knowledge_to", "knowledge_as_of", "observed_at", "created_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware_datetime(value, name))
        if self.knowledge_from is None and self.known_at is not None:
            object.__setattr__(self, "knowledge_from", self.known_at)
        if self.knowledge_to is not None and self.knowledge_from is not None and self.knowledge_to <= self.knowledge_from:
            raise CalendarDomainError("knowledge_to must be later than knowledge_from")
        try:
            quality = CalendarQualityStatus(self.quality_status)
        except ValueError as exc:
            raise CalendarFactInvalidError("quality_status is invalid") from exc
        object.__setattr__(self, "quality_status", quality)
        if self.source_priority is not None:
            _non_negative_int(self.source_priority, "source_priority")
        if self.source_revision_order is not None:
            _non_negative_int(self.source_revision_order, "source_revision_order")
        object.__setattr__(self, "content_hash", _content_hash_or_derive(self.content_hash, self.semantic_payload(), "session_fact.content_hash"))

    def applies_to(self, day: date) -> bool:
        day = _plain_date(day, "day")
        return self.valid_from <= day < self.valid_to

    def effective_timezone_and_sessions(self, definition: CalendarDefinition) -> tuple[str, tuple[SessionWindow, ...]]:
        timezone_name = self.timezone_override or definition.timezone
        windows = self.sessions_override if self.override_mode == "explicit" else definition.default_sessions
        return timezone_name, tuple(windows)

    def strict_validate(self) -> None:
        try:
            freeze_json(self.evidence, "session_fact.evidence")
        except ValueError as exc:
            raise CalendarJsonInvalidError("session fact evidence is not valid JSON") from exc
        if self.known_at is None or self.observed_at is None:
            raise CalendarPitMetadataMissingError("strict session fact PIT metadata is incomplete")
        if self.source is None or self.source_revision is None or self.evidence is None:
            raise CalendarFactInvalidError("strict session fact provenance is incomplete")
        if self.registry_fact_id is None or self.registry_version is None:
            raise CalendarRegistryReferenceInvalidError("strict session fact registry reference is incomplete")
        if self.definition_fact_id is None:
            raise CalendarDefinitionMissingError("strict session fact definition_fact_id is missing")
        if self.source_priority_fact_id is None or self.source_priority_version is None:
            raise CalendarSourcePriorityMissingError("strict session fact source priority is missing")
        if self.source_priority is None or self.source_revision_order is None:
            raise CalendarSourcePriorityMissingError("strict session fact source priority values are missing")
        if self.bootstrap_seed_id is None or self.bootstrap_seed_version is None:
            raise CalendarSourcePriorityMissingError("strict session fact bootstrap seed is missing")
        _sha256_text(self.bootstrap_seed_hash, "bootstrap_seed_hash", optional=False)
        _validate_content_hash(self.content_hash, self.semantic_payload(), "session_fact.content_hash")
        if not self.is_open and self.sessions_override != ():
            raise CalendarFactInvalidError("closed session facts must use an explicit empty sessions_override")

    def semantic_payload(self) -> dict[str, object]:
        return {
            "calendar_id": self.calendar_id,
            "session_date": self.session_date,
            "is_open": self.is_open,
            "timezone_override": self.timezone_override,
            "sessions_override": [window.semantic_payload() for window in self.sessions_override or ()],
            "override_mode": self.override_mode,
        }

    def __hash__(self) -> int:
        return hash(canonical_hash(self.semantic_payload()))


@dataclass(frozen=True, slots=True)
class CalendarRegistry:
    """Append-only canonical calendar identity fact."""

    calendar_id: str
    display_name: str
    timezone_policy: str = "fixed_asia_shanghai"
    status: str = "active"
    registry_version: int = 1
    valid_from: date = date(1900, 1, 1)
    valid_to: date | None = None
    fact_id: UUID | None = None
    fact_version: int = 1
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None
    source: str = "operator-registry"
    source_revision: str = "seed-2026-01"
    evidence: object = "registry evidence"
    known_at: datetime | None = None
    knowledge_from: datetime | None = None
    knowledge_to: datetime | None = None
    knowledge_as_of: datetime | None = None
    observed_at: datetime | None = None
    quality_status: CalendarQualityStatus | str = CalendarQualityStatus.ACCEPTED
    source_priority_fact_id: UUID | None = None
    source_priority_version: str | None = None
    source_priority: int | None = 10
    source_revision_order: int | None = 1
    bootstrap_seed_id: str | None = "calendar-source-priority-bootstrap"
    bootstrap_seed_version: int | None = 1
    bootstrap_seed_hash: str | None = None
    content_hash: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        cid = normalize_calendar_id(self.calendar_id)
        object.__setattr__(self, "calendar_id", cid)
        object.__setattr__(self, "display_name", _non_blank_text(self.display_name, "display_name"))
        if self.timezone_policy != "fixed_asia_shanghai":
            raise CalendarTimezoneUnsupportedError("v1 registry only supports fixed_asia_shanghai")
        if self.status not in {"active", "deprecated"}:
            raise CalendarDomainError("registry status must be active or deprecated")
        object.__setattr__(self, "registry_version", _positive_int(self.registry_version, "registry_version"))
        object.__setattr__(self, "fact_version", _positive_int(self.fact_version, "fact_version"))
        object.__setattr__(self, "valid_from", _plain_date(self.valid_from, "valid_from"))
        if self.valid_to is not None:
            end = _plain_date(self.valid_to, "valid_to")
            if end <= self.valid_from:
                raise CalendarDomainError("valid_to must be later than valid_from")
            object.__setattr__(self, "valid_to", end)
        logical = _optional_text(self.logical_fact_key, "logical_fact_key") or f"calendar_registry:{cid}"
        object.__setattr__(self, "logical_fact_key", logical)
        object.__setattr__(self, "fact_id", _uuid(self.fact_id, "fact_id", optional=True) or _legacy_uuid("registry", logical, self.fact_version, {"registry_version": self.registry_version}))
        object.__setattr__(self, "supersedes_fact_id", _uuid(self.supersedes_fact_id, "supersedes_fact_id", optional=True))
        for name in ("known_at", "knowledge_from", "knowledge_to", "knowledge_as_of", "observed_at", "created_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware_datetime(value, name))
        if self.knowledge_from is None and self.known_at is not None:
            object.__setattr__(self, "knowledge_from", self.known_at)
        try:
            quality = CalendarQualityStatus(self.quality_status)
        except ValueError as exc:
            raise CalendarDomainError("quality_status is invalid") from exc
        object.__setattr__(self, "quality_status", quality)
        object.__setattr__(self, "content_hash", _content_hash_or_derive(self.content_hash, self.semantic_payload(), "registry.content_hash"))

    @property
    def timezone(self) -> str:
        return CALENDAR_TIMEZONE_ASIA_SHANGHAI

    def applies_to(self, day: date) -> bool:
        day = _plain_date(day, "day")
        return self.valid_from <= day and (self.valid_to is None or day < self.valid_to)

    def semantic_payload(self) -> dict[str, object]:
        return {
            "calendar_id": self.calendar_id,
            "registry_version": self.registry_version,
            "display_name": self.display_name,
            "timezone_policy": self.timezone_policy,
            "status": self.status,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }

    def __hash__(self) -> int:
        return hash(canonical_hash(self.semantic_payload()))

    def strict_validate(self) -> None:
        try:
            freeze_json(self.evidence, "registry.evidence")
        except ValueError as exc:
            raise CalendarJsonInvalidError("registry evidence is not valid JSON") from exc
        if self.known_at is None or self.observed_at is None:
            raise CalendarPitMetadataMissingError("registry fact known_at/observed_at is missing")
        if self.source is None or self.source_revision is None or self.evidence is None:
            raise CalendarDefinitionInvalidError("registry provenance is incomplete")
        # Registry facts are ordinary source facts.  Their source-priority
        # reference is mandatory; the bootstrap exception applies only to the
        # priority table itself and must never be used as a registry fallback.
        if self.source_priority_fact_id is None or self.source_priority_version is None:
            raise CalendarSourcePriorityMissingError("registry source priority evidence is missing")
        if self.source_priority is None or self.source_revision_order is None:
            raise CalendarSourcePriorityMissingError("registry source priority values are missing")
        if self.bootstrap_seed_id is None or self.bootstrap_seed_version is None:
            raise CalendarSourcePriorityMissingError("registry bootstrap seed is missing")
        _sha256_text(self.bootstrap_seed_hash, "bootstrap_seed_hash", optional=False)
        _validate_content_hash(self.content_hash, self.semantic_payload(), "registry.content_hash")


@dataclass(frozen=True, slots=True)
class CalendarExchangeBinding:
    """Versioned explicit alias -> canonical calendar binding."""

    alias: str
    canonical_calendar_id: str
    binding_version: str
    valid_from: date
    valid_to: date | None = None
    fact_id: UUID | None = None
    fact_version: int = 1
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None
    registry_fact_id: UUID | None = None
    registry_version: int | None = None
    source: str = "operator-registry"
    source_priority_fact_id: UUID | None = None
    source_priority_version: str | None = None
    source_priority: int | None = 10
    source_revision: str = "seed-2026-01"
    source_revision_order: int | None = 1
    evidence: object = "binding evidence"
    known_at: datetime | None = None
    knowledge_from: datetime | None = None
    knowledge_to: datetime | None = None
    knowledge_as_of: datetime | None = None
    observed_at: datetime | None = None
    quality_status: CalendarQualityStatus | str = CalendarQualityStatus.ACCEPTED
    bootstrap_seed_id: str | None = "calendar-source-priority-bootstrap"
    bootstrap_seed_version: int | None = 1
    bootstrap_seed_hash: str | None = None
    content_hash: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        alias = _non_blank_text(self.alias, "alias")
        if any(ord(ch) > 0x7F for ch in alias):
            raise CalendarBindingUnknownError("binding alias must be ASCII")
        object.__setattr__(self, "alias", "".join(ch.upper() if "a" <= ch <= "z" else ch for ch in alias))
        object.__setattr__(self, "canonical_calendar_id", normalize_calendar_id(self.canonical_calendar_id))
        object.__setattr__(self, "binding_version", _non_blank_text(self.binding_version, "binding_version"))
        object.__setattr__(self, "valid_from", _plain_date(self.valid_from, "valid_from"))
        if self.valid_to is not None:
            end = _plain_date(self.valid_to, "valid_to")
            if end <= self.valid_from:
                raise CalendarDomainError("valid_to must be later than valid_from")
            object.__setattr__(self, "valid_to", end)
        logical = _optional_text(self.logical_fact_key, "logical_fact_key") or f"calendar_binding:{self.alias}:{self.canonical_calendar_id}"
        object.__setattr__(self, "logical_fact_key", logical)
        object.__setattr__(self, "fact_version", _positive_int(self.fact_version, "fact_version"))
        object.__setattr__(self, "fact_id", _uuid(self.fact_id, "fact_id", optional=True) or _legacy_uuid("binding", logical, self.fact_version, {"alias": self.alias, "calendar_id": self.canonical_calendar_id}))
        object.__setattr__(self, "supersedes_fact_id", _uuid(self.supersedes_fact_id, "supersedes_fact_id", optional=True))
        if self.registry_fact_id is not None:
            object.__setattr__(self, "registry_fact_id", _uuid(self.registry_fact_id, "registry_fact_id"))
        if self.registry_version is not None:
            object.__setattr__(self, "registry_version", _positive_int(self.registry_version, "registry_version"))
        for name in ("known_at", "knowledge_from", "knowledge_to", "knowledge_as_of", "observed_at", "created_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware_datetime(value, name))
        if self.knowledge_from is None and self.known_at is not None:
            object.__setattr__(self, "knowledge_from", self.known_at)
        try:
            quality = CalendarQualityStatus(self.quality_status)
        except ValueError as exc:
            raise CalendarDomainError("quality_status is invalid") from exc
        object.__setattr__(self, "quality_status", quality)
        object.__setattr__(self, "content_hash", _content_hash_or_derive(self.content_hash, self.semantic_payload(), "binding.content_hash"))

    def applies_to(self, day: date) -> bool:
        day = _plain_date(day, "day")
        return self.valid_from <= day and (self.valid_to is None or day < self.valid_to)

    def strict_validate(self) -> None:
        try:
            freeze_json(self.evidence, "binding.evidence")
        except ValueError as exc:
            raise CalendarJsonInvalidError("binding evidence is not valid JSON") from exc
        if self.known_at is None or self.observed_at is None:
            raise CalendarPitMetadataMissingError("binding PIT metadata is incomplete")
        if self.source_priority_fact_id is None or self.source_priority_version is None:
            raise CalendarSourcePriorityMissingError("binding source priority is missing")
        if self.registry_fact_id is None or self.registry_version is None:
            raise CalendarRegistryReferenceInvalidError("binding registry reference is incomplete")
        if self.source is None or self.source_revision is None or self.evidence is None:
            raise CalendarDefinitionInvalidError("binding provenance is incomplete")
        if self.source_priority is None or self.source_revision_order is None:
            raise CalendarSourcePriorityMissingError("binding source priority values are missing")
        if self.bootstrap_seed_id is None or self.bootstrap_seed_version is None:
            raise CalendarSourcePriorityMissingError("binding bootstrap seed is missing")
        _sha256_text(self.bootstrap_seed_hash, "bootstrap_seed_hash", optional=False)
        _validate_content_hash(self.content_hash, self.semantic_payload(), "binding.content_hash")

    def semantic_payload(self) -> dict[str, object]:
        return {"alias": self.alias, "canonical_calendar_id": self.canonical_calendar_id, "valid_from": self.valid_from, "valid_to": self.valid_to}

    def __hash__(self) -> int:
        return hash(canonical_hash(self.semantic_payload()))


class CapabilityValue(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class CapabilityApplicability(StrEnum):
    REQUIRED = "required"
    NOT_APPLICABLE = "not_applicable"


CAPABILITY_SUSPENSION = "suspension"
CAPABILITY_OPENING_AVAILABILITY = "opening_availability"
CAPABILITY_PRICE_LIMIT_TRADABILITY = "price_limit_tradability"
CAPABILITY_STATUS_KEYS = frozenset({CAPABILITY_SUSPENSION, CAPABILITY_OPENING_AVAILABILITY, CAPABILITY_PRICE_LIMIT_TRADABILITY})


@dataclass(frozen=True, slots=True)
class CalendarCapabilityDeclaration:
    """One single-scope provider capability declaration."""

    scope_kind: str
    scope_key: str
    capability: str
    value: CapabilityValue | str = CapabilityValue.UNKNOWN
    applicability: CapabilityApplicability | str | None = None
    fact_id: UUID | None = None
    fact_version: int = 1
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None
    provider_key: str | None = None
    package_key: str | None = None
    package_version: int | str | None = None
    calendar_id: str | None = None
    registry_fact_id: UUID | None = None
    registry_version: int | None = None
    instrument_id: UUID | None = None
    valid_from: date = date(1900, 1, 1)
    valid_to: date | None = None
    source: str = "operator-registry"
    source_revision: str = "seed-2026-01"
    source_priority_fact_id: UUID | None = None
    source_priority_version: str | None = None
    source_priority: int | None = 10
    source_revision_order: int | None = 1
    known_at: datetime | None = None
    knowledge_from: datetime | None = None
    knowledge_to: datetime | None = None
    knowledge_as_of: datetime | None = None
    observed_at: datetime | None = None
    quality_status: CalendarQualityStatus | str = CalendarQualityStatus.ACCEPTED
    bootstrap_seed_id: str | None = "calendar-source-priority-bootstrap"
    bootstrap_seed_version: int | None = 1
    bootstrap_seed_hash: str | None = None
    evidence: object = "capability evidence"
    content_hash: str | None = None
    created_at: datetime | None = None

    SPECIFICITY = {"provider": 1, "rule_package": 2, "calendar": 3, "instrument": 4}

    def __post_init__(self) -> None:
        if self.scope_kind not in self.SPECIFICITY:
            raise CalendarJsonInvalidError("scope_kind must be provider, rule_package, calendar, or instrument")
        if self.capability not in CAPABILITY_STATUS_KEYS:
            raise CalendarJsonInvalidError("capability key is not a v1 canonical status capability")
        object.__setattr__(self, "scope_key", _non_blank_text(self.scope_key, "scope_key"))
        try:
            value = CapabilityValue(self.value)
        except ValueError as exc:
            raise CalendarJsonInvalidError("capability value must be supported, unsupported, or unknown") from exc
        object.__setattr__(self, "value", value)
        if self.applicability is not None:
            try:
                applicability = CapabilityApplicability(self.applicability)
            except ValueError as exc:
                raise CalendarJsonInvalidError("applicability must be required or not_applicable") from exc
            object.__setattr__(self, "applicability", applicability)
        self._validate_scope_columns()
        object.__setattr__(self, "valid_from", _plain_date(self.valid_from, "valid_from"))
        if self.valid_to is not None:
            end = _plain_date(self.valid_to, "valid_to")
            if end <= self.valid_from:
                raise CalendarDomainError("valid_to must be later than valid_from")
            object.__setattr__(self, "valid_to", end)
        object.__setattr__(self, "fact_version", _positive_int(self.fact_version, "fact_version"))
        logical = _optional_text(self.logical_fact_key, "logical_fact_key") or f"capability:{self.capability}:{self.scope_kind}:{self.scope_key}"
        object.__setattr__(self, "logical_fact_key", logical)
        object.__setattr__(self, "fact_id", _uuid(self.fact_id, "fact_id", optional=True) or _legacy_uuid("capability", logical, self.fact_version, self.semantic_payload()))
        object.__setattr__(self, "supersedes_fact_id", _uuid(self.supersedes_fact_id, "supersedes_fact_id", optional=True))
        for name in ("registry_fact_id", "instrument_id", "source_priority_fact_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _uuid(value, name))
        if self.registry_version is not None:
            object.__setattr__(self, "registry_version", _positive_int(self.registry_version, "registry_version"))
        for name in ("known_at", "knowledge_from", "knowledge_to", "knowledge_as_of", "observed_at", "created_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware_datetime(value, name))
        if self.knowledge_from is None and self.known_at is not None:
            object.__setattr__(self, "knowledge_from", self.known_at)
        try:
            object.__setattr__(self, "quality_status", CalendarQualityStatus(self.quality_status))
        except ValueError as exc:
            raise CalendarDomainError("quality_status is invalid") from exc
        object.__setattr__(self, "content_hash", _content_hash_or_derive(self.content_hash, self.semantic_payload(), "capability.content_hash"))

    def _validate_scope_columns(self) -> None:
        expected: str
        if self.scope_kind == "provider":
            provider_key = _non_blank_text(self.provider_key, "provider_key")
            object.__setattr__(self, "provider_key", provider_key)
            expected = f"provider:{provider_key}"
            if self.package_key is not None or self.package_version is not None or self.calendar_id is not None or self.instrument_id is not None or self.registry_fact_id is not None or self.registry_version is not None:
                raise CalendarJsonInvalidError("provider scope must not carry other scope columns")
        elif self.scope_kind == "rule_package":
            key = _non_blank_text(self.package_key, "package_key")
            object.__setattr__(self, "package_key", key)
            if self.package_version is None or isinstance(self.package_version, bool):
                raise CalendarJsonInvalidError("package_version is required for rule_package scope")
            version = _non_blank_text(str(self.package_version), "package_version")
            object.__setattr__(self, "package_version", version)
            expected = f"rule_package:{key}@{version}"
            if self.provider_key is not None or self.calendar_id is not None or self.instrument_id is not None or self.registry_fact_id is not None or self.registry_version is not None:
                raise CalendarJsonInvalidError("rule_package scope must not carry other scope columns")
        elif self.scope_kind == "calendar":
            cid = normalize_calendar_id(self.calendar_id)
            expected = f"calendar:{cid}"
            if self.registry_fact_id is None or self.registry_version is None:
                raise CalendarRegistryReferenceInvalidError("calendar capability scope requires registry reference")
            if self.provider_key is not None or self.package_key is not None or self.package_version is not None or self.instrument_id is not None:
                raise CalendarJsonInvalidError("calendar scope must not carry other scope columns")
            object.__setattr__(self, "calendar_id", cid)
        else:
            instrument_id = _uuid(self.instrument_id, "instrument_id")
            expected = f"instrument:{str(instrument_id)}"
            if self.provider_key is not None or self.package_key is not None or self.package_version is not None or self.calendar_id is not None or self.registry_fact_id is not None or self.registry_version is not None:
                raise CalendarJsonInvalidError("instrument scope must not carry calendar/provider columns")
        if self.scope_key != expected:
            raise CalendarJsonInvalidError("scope_key does not match its single scope columns", details={"expected": expected, "actual": self.scope_key})

    @property
    def specificity(self) -> int:
        return self.SPECIFICITY[self.scope_kind]

    def applies_to(self, day: date) -> bool:
        day = _plain_date(day, "day")
        return self.valid_from <= day and (self.valid_to is None or day < self.valid_to)

    def strict_validate(self) -> None:
        """Validate provenance required before a declaration is consumed."""

        try:
            freeze_json(self.evidence, "capability.evidence")
        except ValueError as exc:
            raise CalendarJsonInvalidError("capability evidence is not valid JSON") from exc
        if self.known_at is None or self.observed_at is None:
            raise CalendarPitMetadataMissingError("capability declaration PIT metadata is incomplete")
        if self.source_priority_fact_id is None or self.source_priority_version is None:
            raise CalendarSourcePriorityMissingError("capability declaration source priority is missing")
        if self.source is None or self.source_revision is None or self.evidence is None:
            raise CalendarDefinitionInvalidError("capability declaration provenance is incomplete")
        if self.source_priority is None or self.source_revision_order is None:
            raise CalendarSourcePriorityMissingError("capability declaration source priority values are missing")
        if self.bootstrap_seed_id is None or self.bootstrap_seed_version is None:
            raise CalendarSourcePriorityMissingError("capability declaration bootstrap seed is missing")
        _sha256_text(self.bootstrap_seed_hash, "bootstrap_seed_hash", optional=False)
        if self.applicability is None:
            raise CalendarJsonInvalidError("capability declaration applicability is required")
        _validate_content_hash(self.content_hash, self.semantic_payload(), "capability.content_hash")

    def semantic_payload(self) -> dict[str, object]:
        return {"scope_kind": self.scope_kind, "scope_key": self.scope_key, "capability": self.capability, "value": self.value, "applicability": self.applicability, "valid_from": self.valid_from, "valid_to": self.valid_to}

    def __hash__(self) -> int:
        return hash(canonical_hash(self.semantic_payload()))


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """Selected capability declaration plus explicit missing/unknown evidence."""

    capability: str
    value: CapabilityValue
    applicability: CapabilityApplicability | None
    declaration: CalendarCapabilityDeclaration | None
    specificity: int = 0
    missing: bool = False

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITY_STATUS_KEYS:
            raise CalendarJsonInvalidError("unknown capability key")
        object.__setattr__(self, "value", CapabilityValue(self.value))
        if self.applicability is not None:
            object.__setattr__(self, "applicability", CapabilityApplicability(self.applicability))
        if self.declaration is not None and self.declaration.capability != self.capability:
            raise ProviderContractViolationError("capability resolution declaration does not match capability")
        if self.specificity < 0:
            raise CalendarDomainError("capability specificity must be non-negative")


@dataclass(frozen=True, slots=True)
class CalendarSourcePriority:
    """Versioned source priority row rooted in an immutable bootstrap seed.

    ``source_revision`` is evidence supplied by the source-priority registry;
    it is deliberately not used as a lexical ordering key.  The integer
    ``source_revision_order`` is the only revision ordering authority.
    """

    source: str
    source_priority_version: str
    source_priority: int
    source_revision_order: int
    source_revision: str = "bootstrap"
    fact_id: UUID | None = None
    fact_version: int = 1
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None
    valid_from: date = date(1900, 1, 1)
    valid_to: date | None = None
    knowledge_from: datetime | None = None
    knowledge_to: datetime | None = None
    known_at: datetime | None = None
    knowledge_as_of: datetime | None = None
    observed_at: datetime | None = None
    evidence: object = "bootstrap evidence"
    quality_status: CalendarQualityStatus | str = CalendarQualityStatus.ACCEPTED
    bootstrap_seed_id: str = "calendar-source-priority-bootstrap"
    bootstrap_seed_version: int = 1
    bootstrap_seed_hash: str = ""
    content_hash: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _non_blank_text(self.source, "source"))
        object.__setattr__(self, "source_priority_version", _non_blank_text(self.source_priority_version, "source_priority_version"))
        object.__setattr__(self, "source_revision", _non_blank_text(self.source_revision, "source_revision"))
        _non_negative_int(self.source_priority, "source_priority")
        _non_negative_int(self.source_revision_order, "source_revision_order")
        valid_from = _plain_date(self.valid_from, "valid_from")
        object.__setattr__(self, "valid_from", valid_from)
        if self.valid_to is not None:
            end = _plain_date(self.valid_to, "valid_to")
            if end <= valid_from:
                raise CalendarSourcePriorityInvalidError("valid_to must be later than valid_from")
            object.__setattr__(self, "valid_to", end)
        object.__setattr__(self, "fact_version", _positive_int(self.fact_version, "fact_version"))
        logical = _optional_text(self.logical_fact_key, "logical_fact_key") or f"calendar_source_priority:{self.source}"
        object.__setattr__(self, "logical_fact_key", logical)
        object.__setattr__(self, "fact_id", _uuid(self.fact_id, "fact_id", optional=True) or _legacy_uuid("priority", logical, self.fact_version, self.source_priority_version))
        object.__setattr__(self, "supersedes_fact_id", _uuid(self.supersedes_fact_id, "supersedes_fact_id", optional=True))
        for name in ("known_at", "knowledge_from", "knowledge_to", "knowledge_as_of", "observed_at", "created_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware_datetime(value, name))
        if self.knowledge_from is None and self.known_at is not None:
            object.__setattr__(self, "knowledge_from", self.known_at)
        object.__setattr__(self, "bootstrap_seed_hash", _sha256_text(self.bootstrap_seed_hash, "bootstrap_seed_hash", optional=False))
        try:
            object.__setattr__(self, "quality_status", CalendarQualityStatus(self.quality_status))
        except ValueError as exc:
            raise CalendarSourcePriorityInvalidError("quality_status is invalid") from exc
        object.__setattr__(self, "content_hash", _content_hash_or_derive(self.content_hash, self.semantic_payload(), "source_priority.content_hash"))

    def applies_to(self, day: date) -> bool:
        day = _plain_date(day, "day")
        return self.valid_from <= day and (self.valid_to is None or day < self.valid_to)

    def covers_knowledge(self, instant: datetime) -> bool:
        instant = _aware_datetime(instant, "knowledge instant")
        return self.knowledge_from is not None and self.knowledge_from <= instant and (self.knowledge_to is None or instant < self.knowledge_to)

    def strict_validate(self) -> None:
        """Validate the non-self-referencing bootstrap priority root."""

        if self.known_at is None or self.observed_at is None or self.knowledge_from is None:
            raise CalendarPitMetadataMissingError("source priority PIT metadata is incomplete")
        if self.bootstrap_seed_id is None or self.bootstrap_seed_version is None:
            raise CalendarSourcePriorityInvalidError("source priority bootstrap seed is missing")
        _sha256_text(self.bootstrap_seed_hash, "bootstrap_seed_hash", optional=False)
        try:
            freeze_json(self.evidence, "source_priority.evidence")
        except ValueError as exc:
            raise CalendarJsonInvalidError("source priority evidence is not valid JSON") from exc
        _validate_content_hash(self.content_hash, self.semantic_payload(), "source_priority.content_hash")

    def semantic_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_priority_version": self.source_priority_version,
            "source_priority": self.source_priority,
            "source_revision_order": self.source_revision_order,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }

    def __hash__(self) -> int:
        return hash(canonical_hash(self.semantic_payload()))


# ---------------------------------------------------------------------------
# Capability selection and PIT context
# ---------------------------------------------------------------------------


def select_capability_declaration(
    declarations: Sequence[CalendarCapabilityDeclaration],
    *,
    capability: str,
    effective_day: date,
    pit_context: CalendarPITContext | None = None,
    provider_key: str | None = None,
    package_key: str | None = None,
    package_version: int | str | None = None,
    calendar_id: str | None = None,
    instrument_id: UUID | None = None,
    source_priorities: Sequence[CalendarSourcePriority] = (),
) -> CapabilityResolution:
    """Resolve one capability using specificity and the common PIT selector."""

    if capability not in CAPABILITY_STATUS_KEYS:
        raise CalendarJsonInvalidError("capability key is not a v1 canonical status capability")
    day = _plain_date(effective_day, "effective_day")
    canonical_calendar = normalize_calendar_id(calendar_id) if calendar_id is not None else None
    normalized_provider = _optional_text(provider_key, "provider_key")
    normalized_package = _optional_text(package_key, "package_key")
    normalized_instrument = _uuid(instrument_id, "instrument_id", optional=True)
    candidates: list[CalendarCapabilityDeclaration] = []
    for declaration in declarations:
        if declaration.capability != capability or not declaration.applies_to(day):
            continue
        if declaration.scope_kind == "provider" and normalized_provider is not None and declaration.provider_key == normalized_provider:
            candidates.append(declaration)
        elif declaration.scope_kind == "rule_package" and normalized_package is not None and declaration.package_key == normalized_package and str(declaration.package_version) == str(package_version):
            candidates.append(declaration)
        elif declaration.scope_kind == "calendar" and canonical_calendar is not None and declaration.calendar_id == canonical_calendar:
            candidates.append(declaration)
        elif declaration.scope_kind == "instrument" and normalized_instrument is not None and declaration.instrument_id == normalized_instrument:
            candidates.append(declaration)
    if not candidates:
        return CapabilityResolution(capability, CapabilityValue.UNKNOWN, None, None, missing=True)
    # PIT visibility is resolved before specificity: an invisible narrow-scope
    # declaration must not shadow a visible broader-scope fallback.
    specificities = sorted({item.specificity for item in candidates}, reverse=True)
    visibility_error: CalendarContractError | None = None
    for specificity in specificities:
        scoped_candidates = [item for item in candidates if item.specificity == specificity]
        try:
            selected = select_pit_candidate(
                scoped_candidates,
                effective_day=day,
                pit_context=pit_context,
                source_priorities=source_priorities,
                missing_code="calendar_fact_missing",
                ambiguous_code="calendar_fact_ambiguous",
            )
        except (CalendarFactMissingError, CalendarFactInvalidError) as exc:
            # This specificity has no PIT-visible accepted declaration; the
            # next broader scope remains eligible under the v1 contract.
            if visibility_error is None or isinstance(exc, CalendarFactInvalidError):
                visibility_error = exc
            continue
        except CalendarFactAmbiguousError as exc:
            raise CalendarCapabilityDeclarationAmbiguousError(str(exc), details=getattr(exc, "details", None)) from exc
        assert isinstance(selected, CalendarCapabilityDeclaration)
        selected.strict_validate() if pit_context is not None else None
        return CapabilityResolution(capability, selected.value, selected.applicability, selected, specificity)
    if visibility_error is not None:
        raise visibility_error
    raise _pit_error_class("calendar_fact_missing")(
        "no visible calendar capability declaration", details={"date": day.isoformat()}
    )


@dataclass(frozen=True, slots=True)
class CalendarPITContext:
    """Frozen calendar PIT context derived from one canonical QueryBoundary.

    Calendar v1 has one and only one local-date basis: ``Asia/Shanghai``.
    The public constructor remains a value-object constructor for backwards
    compatibility, but every production calendar path must use
    :meth:`from_query_boundary`; that factory is where the sole request
    boundary is validated and the local cutoff date is derived.
    """

    data_cutoff: datetime
    knowledge_as_of: datetime | None
    include_cutoff_day: bool
    cutoff_local_date: date
    pit_profile: str
    profile_version: str = CALENDAR_PIT_PROFILE_VERSION

    def __post_init__(self) -> None:
        # ``None`` is deliberately reported as the stable cutoff error rather
        # than allowing a lower-level datetime/attribute error to escape.
        if self.data_cutoff is None:
            raise DataCutoffRequiredError(
                "calendar strict PIT requires query_boundary.data_cutoff"
            )
        cutoff = _aware_datetime(self.data_cutoff, "data_cutoff")
        object.__setattr__(self, "data_cutoff", cutoff)
        if self.knowledge_as_of is not None:
            knowledge = _aware_datetime(self.knowledge_as_of, "knowledge_as_of")
            if knowledge.astimezone(timezone.utc) > cutoff.astimezone(timezone.utc):
                raise CalendarDomainError("knowledge_as_of must not be later than data_cutoff")
            object.__setattr__(self, "knowledge_as_of", knowledge)
        if not isinstance(self.include_cutoff_day, bool):
            raise CalendarDomainError("include_cutoff_day must be a boolean")
        local_date = _plain_date(self.cutoff_local_date, "cutoff_local_date")
        # v1 registry policy is fixed to Asia/Shanghai.  Validating the
        # derived value here prevents a caller from constructing a context
        # whose apparent cutoff date differs from its instant.
        expected_local_date = cutoff.astimezone(
            ZoneInfo(CALENDAR_TIMEZONE_ASIA_SHANGHAI)
        ).date()
        if local_date != expected_local_date:
            raise CalendarDomainError(
                "cutoff_local_date must be derived from data_cutoff"
            )
        object.__setattr__(self, "cutoff_local_date", local_date)
        expected = (
            PIT_PROFILE_STRICT_HISTORICAL_COGNITION
            if self.knowledge_as_of is not None
            else PIT_PROFILE_STRICT_CALENDAR_CUTOFF
        )
        if self.pit_profile != expected:
            raise CalendarDomainError("pit_profile must be derived from knowledge_as_of")
        if self.profile_version != CALENDAR_PIT_PROFILE_VERSION:
            raise CalendarDomainError("unsupported calendar PIT profile version")

    @classmethod
    def from_query_boundary(
        cls,
        boundary: object,
        timezone_name: str = CALENDAR_TIMEZONE_ASIA_SHANGHAI,
    ) -> "CalendarPITContext":
        # Only the canonical QueryBoundary is accepted.  Reading arbitrary
        # objects here would recreate the legacy dual-cutoff resolver path.
        from app.backtesting.data.requests import QueryBoundary

        if boundary is None or not isinstance(boundary, QueryBoundary):
            raise DataCutoffRequiredError(
                "calendar strict PIT requires query_boundary.data_cutoff"
            )
        # The registry's timezone policy is the only local-date basis in v1;
        # accepting an arbitrary IANA name here would make the same instant
        # resolve to different natural days in different callers.
        if timezone_name != CALENDAR_TIMEZONE_ASIA_SHANGHAI:
            raise CalendarTimezoneUnsupportedError(
                "calendar v1 only supports Asia/Shanghai cutoff derivation",
                details={"timezone": timezone_name},
            )
        cutoff = boundary.data_cutoff
        cutoff = _aware_datetime(cutoff, "data_cutoff")
        knowledge = boundary.knowledge_as_of
        include = boundary.include_cutoff_day
        local_day = derive_cutoff_local_date(cutoff, CALENDAR_TIMEZONE_ASIA_SHANGHAI)
        return cls(
            data_cutoff=cutoff,
            knowledge_as_of=knowledge,
            include_cutoff_day=include,
            cutoff_local_date=local_day,
            pit_profile=(
                PIT_PROFILE_STRICT_HISTORICAL_COGNITION
                if knowledge is not None
                else PIT_PROFILE_STRICT_CALENDAR_CUTOFF
            ),
        )

    @property
    def as_dict(self) -> Mapping[str, object]:
        return MappingProxyType({
            "data_cutoff": self.data_cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "knowledge_as_of": self.knowledge_as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if self.knowledge_as_of else None,
            "include_cutoff_day": self.include_cutoff_day,
            "cutoff_local_date": self.cutoff_local_date.isoformat(),
            "pit_profile": self.pit_profile,
            "profile_version": self.profile_version,
        })

    def require_date(self, day: date, field_name: str) -> None:
        day = _plain_date(day, field_name)
        if day > self.cutoff_local_date or (day == self.cutoff_local_date and not self.include_cutoff_day):
            raise DataCutoffExceededError(
                f"{field_name} touches the unavailable cutoff day",
                details={"date": day.isoformat(), "cutoff_local_date": self.cutoff_local_date.isoformat(), "include_cutoff_day": self.include_cutoff_day, "data_cutoff": self.data_cutoff.astimezone(timezone.utc).isoformat()},
            )


def derive_cutoff_local_date(data_cutoff: datetime, timezone_name: str) -> date:
    """Derive local date only after a validated IANA timezone is known."""

    cutoff = _aware_datetime(data_cutoff, "data_cutoff")
    name = _timezone_name(timezone_name, "timezone")
    return cutoff.astimezone(ZoneInfo(name)).date()


def _candidate_knowledge_visible(candidate: object, context: CalendarPITContext) -> bool:
    known_at = getattr(candidate, "known_at", None)
    if not isinstance(known_at, datetime) or known_at.tzinfo is None or known_at.utcoffset() is None:
        raise CalendarPitMetadataMissingError("calendar fact known_at is missing")
    if known_at.astimezone(timezone.utc) > context.data_cutoff.astimezone(timezone.utc):
        return False
    if context.knowledge_as_of is None:
        return True
    if known_at > context.knowledge_as_of:
        return False
    declared = getattr(candidate, "knowledge_as_of", None)
    if declared is None or declared > context.knowledge_as_of:
        raise CalendarPitMetadataMissingError("strict historical cognition evidence is incomplete")
    knowledge_from = getattr(candidate, "knowledge_from", None)
    knowledge_to = getattr(candidate, "knowledge_to", None)
    if (
        knowledge_from is None
        or knowledge_from > context.knowledge_as_of
        or (knowledge_to is not None and not knowledge_from <= context.knowledge_as_of < knowledge_to)
    ):
        raise CalendarPitMetadataMissingError("knowledge range does not cover historical cognition time")
    return True


def _candidate_content(candidate: object) -> str:
    value = getattr(candidate, "content_hash", None)
    if isinstance(value, str) and value:
        return value
    semantic = getattr(candidate, "semantic_payload", None)
    return canonical_hash(semantic() if callable(semantic) else repr(candidate))


def _validate_revision_chain(candidates: Sequence[object]) -> None:
    """Reject broken/cyclic chains before applying a deterministic choice."""

    by_id = {getattr(candidate, "fact_id", None): candidate for candidate in candidates}
    for candidate in candidates:
        predecessor = getattr(candidate, "supersedes_fact_id", None)
        if predecessor is None:
            continue
        visited: set[UUID] = set()
        current_id = getattr(candidate, "fact_id", None)
        current = candidate
        while predecessor is not None:
            if current_id in visited:
                raise CalendarSourcePriorityChainBrokenError("fact supersedes chain contains a cycle")
            if isinstance(current_id, UUID):
                visited.add(current_id)
            previous = by_id.get(predecessor)
            if previous is None:
                # The supplied candidate slice may not include old history;
                # strict providers must expose it.  Missing predecessor is a
                # chain break rather than a latest-row fallback.
                raise CalendarSourcePriorityChainBrokenError("fact supersedes chain is broken", details={"fact_id": str(current_id), "supersedes_fact_id": str(predecessor)})
            if getattr(previous, "logical_fact_key", None) != getattr(current, "logical_fact_key", None) or getattr(previous, "fact_version", 0) != getattr(current, "fact_version", 0) - 1:
                raise CalendarSourcePriorityChainBrokenError("fact supersedes chain is not contiguous")
            current = previous
            current_id = getattr(current, "fact_id", None)
            predecessor = getattr(current, "supersedes_fact_id", None)


def _select_source_priority(
    source: str | None,
    rows: Sequence[CalendarSourcePriority],
    *,
    day: date,
    context: CalendarPITContext,
) -> CalendarSourcePriority:
    """Select one source-priority fact without lexical fallbacks."""

    candidates = [
        row for row in rows
        if row.source == source and row.applies_to(day)
    ]
    if not candidates:
        raise CalendarSourcePriorityMissingError(
            "source priority fact is missing", details={"source": source}
        )
    visible: list[CalendarSourcePriority] = []
    for row in candidates:
        row.strict_validate()
        if row.quality_status is CalendarQualityStatus.ACCEPTED and _candidate_knowledge_visible(row, context):
            visible.append(row)
    if not visible:
        raise CalendarSourcePriorityMissingError(
            "no visible source priority fact", details={"source": source}
        )
    # The chain must be complete before any candidate is selected.  A newer
    # priority row may not hide a broken predecessor from a strict snapshot.
    _validate_revision_chain(visible)
    visible.sort(
        key=lambda row: (
            # A timedelta inversion preserves exact microsecond ordering;
            # float timestamps can collapse distinct instants and are not a
            # valid deterministic PIT tie-break at the edges of the range.
            datetime.max.replace(tzinfo=timezone.utc)
            - row.known_at.astimezone(timezone.utc),
            -row.source_revision_order,
            -row.fact_version,
            row.fact_id.bytes if isinstance(row.fact_id, UUID) else b"",
        )
    )
    first = visible[0]
    rank = (
        first.known_at.astimezone(timezone.utc),
        first.source_revision_order,
        first.fact_version,
    )
    leaders = [
        row for row in visible
        if (
            row.known_at.astimezone(timezone.utc),
            row.source_revision_order,
            row.fact_version,
        ) == rank
    ]
    # ``source_revision`` is evidence, not an ordering value.  Same-rank
    # priority rows must carry one revision and one complete priority tuple.
    if len({row.source_revision for row in leaders}) > 1:
        raise CalendarSourceRevisionConflictError(
            "source priority revisions are ambiguous", details={"source": source}
        )
    if len({(row.source_priority, row.source_priority_version, row.source_revision_order, row.content_hash) for row in leaders}) != 1:
        raise CalendarSourcePriorityAmbiguousError(
            "source priority candidates are ambiguous", details={"source": source}
        )
    return min(leaders, key=lambda row: row.fact_id.bytes if isinstance(row.fact_id, UUID) else b"")


def _select_pit_candidate_legacy(
    candidates: Sequence[object],
    *,
    effective_day: date,
    pit_context: CalendarPITContext | None = None,
    source_priorities: Sequence[CalendarSourcePriority] = (),
    missing_code: str = "calendar_fact_missing",
    ambiguous_code: str = "calendar_fact_ambiguous",
) -> object:
    """Select one candidate using the frozen ``select_pit_candidate@1`` order.

    The function is shared by memory and SQL projections.  It never sorts by
    source name or source revision text; priority and revision order must be
    supplied by a versioned source-priority fact.
    """

    day = _plain_date(effective_day, "effective_day")
    candidates = tuple(candidates)
    if pit_context is not None:
        visible: list[object] = []
        for candidate in candidates:
            valid_from = getattr(candidate, "valid_from", None)
            valid_to = getattr(candidate, "valid_to", None)
            if valid_from is None or not (valid_from <= day and (valid_to is None or day < valid_to)):
                continue
            if getattr(candidate, "quality_status", CalendarQualityStatus.ACCEPTED) != CalendarQualityStatus.ACCEPTED:
                continue
            if _candidate_knowledge_visible(candidate, pit_context):
                visible.append(candidate)
        candidates = tuple(visible)
    else:
        candidates = tuple(
            candidate for candidate in candidates
            if getattr(candidate, "valid_from", None) is None
            or (getattr(candidate, "valid_from") <= day and (getattr(candidate, "valid_to") is None or day < getattr(candidate, "valid_to")))
        )
    if not candidates:
        error_cls = {"calendar_fact_missing": CalendarFactMissingError, "calendar_definition_missing": CalendarDefinitionMissingError}.get(missing_code, CalendarFactMissingError)
        raise error_cls("no visible calendar fact candidate", details={"date": day.isoformat()})
    if pit_context is not None:
        # Resolve priority independently for every source.  A source with no
        # priority row is never ordered by lexical fallback.
        priority_by_source: dict[str | None, CalendarSourcePriority] = {
            source: _select_source_priority(source, source_priorities, day=day, context=pit_context)
            for source in {getattr(item, "source", None) for item in candidates}
        }
        def primary_rank(item: object) -> tuple[int, timedelta, int]:
            priority = priority_by_source.get(getattr(item, "source", None))
            if priority is None:
                raise CalendarSourcePriorityMissingError("source priority fact is missing")
            known = getattr(item, "known_at", None)
            if not isinstance(known, datetime):
                raise CalendarPitMetadataMissingError("calendar candidate known_at is missing")
            revision_order = getattr(item, "source_revision_order", None)
            if isinstance(revision_order, bool) or not isinstance(revision_order, int):
                raise CalendarSourceRevisionConflictError("calendar candidate source_revision_order is missing")
            expected_priority = getattr(item, "source_priority_fact_id", None)
            if expected_priority is None or expected_priority != priority.fact_id:
                raise CalendarSourceRevisionConflictError("fact source priority reference does not match selected priority fact")
            expected_priority_version = getattr(item, "source_priority_version", None)
            if expected_priority_version is None or expected_priority_version != priority.source_priority_version:
                raise CalendarSourceRevisionConflictError("fact source priority version does not match selected priority fact")
            return (
                priority.source_priority,
                datetime.max.replace(tzinfo=timezone.utc)
                - known.astimezone(timezone.utc),
                -revision_order,
            )
        # Validate the complete visible chain before narrowing candidates;
        # otherwise a newer row could hide a missing predecessor from the
        # strict append-only proof.
        _validate_revision_chain(candidates)
        # Fact version is deliberately not part of the first ranking pass.
        # All candidates in the best priority/knowledge/revision group must
        # prove equivalent content before fact_version/UUID can break a tie.
        best_primary = min(primary_rank(item) for item in candidates)
        candidates = tuple(item for item in candidates if primary_rank(item) == best_primary)
        if len({getattr(item, "source", None) for item in candidates}) > 1:
            if len({priority_by_source[getattr(item, "source", None)].source_priority_version for item in candidates}) > 1:
                raise CalendarSourceRevisionConflictError(
                    "same-rank candidates use different source-priority versions"
                )
            cross_source_contents = {
                (_candidate_content(item), _canonical_json(getattr(item, "semantic_payload")() if callable(getattr(item, "semantic_payload", None)) else repr(item)))
                for item in candidates
            }
            if len(cross_source_contents) > 1:
                error_cls = CalendarDefinitionAmbiguousError if ambiguous_code == "calendar_definition_ambiguous" else CalendarFactAmbiguousError
                raise error_cls("same-rank sources carry different content", details={"date": day.isoformat(), "fact_ids": [str(getattr(item, "fact_id", "")) for item in candidates]})
        best = min(candidates, key=lambda item: (-getattr(item, "fact_version", 1), getattr(item, "fact_id", UUID(int=0)).bytes if isinstance(getattr(item, "fact_id", None), UUID) else str(getattr(item, "fact_id", "")).encode("utf-8")))
        best_priority = priority_by_source[getattr(best, "source", None)]
        # A source revision is an evidence dimension, not a lexical tie-break.
        # Detect conflicting revisions before fact_version/fact_id narrowing.
        for source in {getattr(item, "source", None) for item in candidates}:
            source_items = [item for item in candidates if getattr(item, "source", None) == source]
            groups: dict[tuple[datetime, int], set[str | None]] = {}
            for item in source_items:
                priority = priority_by_source[source]
                known_at = getattr(item, "known_at", None)
                if not isinstance(known_at, datetime):
                    continue
                revision_order = getattr(item, "source_revision_order", None)
                key = (known_at.astimezone(timezone.utc), revision_order)
                groups.setdefault(key, set()).add(getattr(item, "source_revision", None))
            if any(len(revisions) > 1 for revisions in groups.values()):
                raise CalendarSourceRevisionConflictError("same source has multiple revisions at the selected PIT rank")
        expected_priority = getattr(best, "source_priority_fact_id", None)
        if expected_priority is not None and expected_priority != best_priority.fact_id:
            raise CalendarSourceRevisionConflictError("fact source priority reference does not match selected priority fact")
        expected_priority_version = getattr(best, "source_priority_version", None)
        if expected_priority_version is not None and expected_priority_version != best_priority.source_priority_version:
            raise CalendarSourceRevisionConflictError("fact source priority version does not match selected priority fact")
        best_known = getattr(best, "known_at").astimezone(timezone.utc)
        best_group = (
            best_priority.source_priority,
            best_known,
            getattr(best, "source_revision_order", None),
            getattr(best, "fact_version", 1),
        )
        leaders = [
            item for item in candidates
            if (
                priority_by_source[getattr(item, "source", None)].source_priority,
                getattr(item, "known_at").astimezone(timezone.utc),
                getattr(item, "source_revision_order", None),
                getattr(item, "fact_version", 1),
            ) == best_group
        ]
    else:
        candidates = tuple(sorted(candidates, key=lambda item: (-getattr(item, "fact_version", 1), str(getattr(item, "fact_id", "")).encode("utf-8"))))
        _validate_revision_chain(candidates)
        best = candidates[0]
        best_rank = (
            getattr(best, "source_priority", None),
            getattr(best, "known_at", None),
            getattr(best, "source_revision_order", None),
            getattr(best, "fact_version", None),
        )
        leaders = [item for item in candidates if (
            getattr(item, "source_priority", None),
            getattr(item, "known_at", None),
            getattr(item, "source_revision_order", None),
            getattr(item, "fact_version", None),
        ) == best_rank]
    source_revisions = {
        (getattr(item, "source", None), getattr(item, "source_revision", None))
        for item in leaders
    }
    if len({source for source, _ in source_revisions}) == 1 and len(source_revisions) > 1:
        raise CalendarSourceRevisionConflictError("same source has multiple revisions at the selected PIT rank")
    contents = {
        (_candidate_content(item), _canonical_json(getattr(item, "semantic_payload")() if callable(getattr(item, "semantic_payload", None)) else repr(item)))
        for item in leaders
    }
    if len(contents) > 1:
        error_cls = CalendarDefinitionAmbiguousError if ambiguous_code == "calendar_definition_ambiguous" else CalendarFactAmbiguousError
        raise error_cls("same-rank PIT candidates carry different content", details={"date": day.isoformat(), "fact_ids": [str(getattr(item, "fact_id", "")) for item in leaders]})
    return min(leaders, key=lambda item: getattr(item, "fact_id", UUID(int=0)).bytes if isinstance(getattr(item, "fact_id", None), UUID) else str(getattr(item, "fact_id", "")).encode("utf-8"))


# The legacy implementation above remains available only as a diagnostic
# helper for pre-task-11 callers.  All strict providers call the canonical
# implementation below, which keeps source-priority evidence and PIT
# filtering ahead of equivalence/fact-version tie-breaking.
def _pit_error_class(code: str):
    return {
        "calendar_definition_missing": CalendarDefinitionMissingError,
        "calendar_definition_ambiguous": CalendarDefinitionAmbiguousError,
        "calendar_binding_missing": CalendarBindingUnknownError,
        "calendar_binding_unknown": CalendarBindingUnknownError,
        "calendar_binding_ambiguous": CalendarBindingAmbiguousError,
        "calendar_registry_fact_missing": CalendarRegistryFactMissingError,
        "calendar_registry_ambiguous": CalendarRegistryAmbiguousError,
        "calendar_source_priority_missing": CalendarSourcePriorityMissingError,
    }.get(code, CalendarFactMissingError)


def _pit_ambiguous_class(code: str):
    return CalendarDefinitionAmbiguousError if code == "calendar_definition_ambiguous" else CalendarFactAmbiguousError


def _candidate_business_payload(candidate: object) -> str:
    semantic = getattr(candidate, "semantic_payload", None)
    return _canonical_json(semantic() if callable(semantic) else repr(candidate))


def _candidate_uuid_bytes(candidate: object) -> bytes:
    value = getattr(candidate, "fact_id", None)
    if isinstance(value, UUID):
        return value.bytes
    return str(value or "").encode("utf-8")


def select_pit_candidate(
    candidates: Sequence[object],
    *,
    effective_day: date,
    pit_context: CalendarPITContext | None = None,
    source_priorities: Sequence[CalendarSourcePriority] = (),
    missing_code: str = "calendar_fact_missing",
    ambiguous_code: str = "calendar_fact_ambiguous",
) -> object:
    """Select a candidate using the single strict PIT ordering contract.

    The ordering is intentionally staged: effective/quality/PIT evidence is
    filtered first, each source is resolved through the versioned priority
    registry, equivalent content is proven for the complete best rank group,
    and only then are ``fact_version`` and UUID bytes used for determinism.
    ``source_revision`` text is never an ordering key.
    """

    day = _plain_date(effective_day, "effective_day")
    all_candidates = tuple(candidates)
    if not all_candidates:
        raise _pit_error_class(missing_code)(
            "no calendar fact candidate", details={"date": day.isoformat()}
        )

    visible: list[object] = []
    invalid_quality = False
    if pit_context is None:
        # This branch is strictly a migration/diagnostic compatibility face.
        # It still uses half-open validity and deterministic identity ordering,
        # but strict callers must always supply a CalendarPITContext.
        for candidate in all_candidates:
            valid_from = getattr(candidate, "valid_from", None)
            valid_to = getattr(candidate, "valid_to", None)
            if valid_from is not None and not (
                valid_from <= day and (valid_to is None or day < valid_to)
            ):
                continue
            visible.append(candidate)
    else:
        for candidate in all_candidates:
            valid_from = getattr(candidate, "valid_from", None)
            valid_to = getattr(candidate, "valid_to", None)
            if valid_from is None or not (
                valid_from <= day and (valid_to is None or day < valid_to)
            ):
                continue
            quality = getattr(candidate, "quality_status", CalendarQualityStatus.ACCEPTED)
            if quality != CalendarQualityStatus.ACCEPTED and quality != CalendarQualityStatus.ACCEPTED.value:
                invalid_quality = True
                continue
            if _candidate_knowledge_visible(candidate, pit_context):
                visible.append(candidate)

    if not visible:
        if invalid_quality:
            raise CalendarFactInvalidError(
                "calendar candidates are not accepted quality", details={"date": day.isoformat()}
            )
        raise _pit_error_class(missing_code)(
            "no visible calendar fact candidate", details={"date": day.isoformat()}
        )

    if pit_context is None:
        _validate_revision_chain(visible)
        max_version = max(getattr(item, "fact_version", 1) for item in visible)
        leaders = [item for item in visible if getattr(item, "fact_version", 1) == max_version]
    else:
        # Resolve one source-priority fact for every source before ranking any
        # ordinary fact.  A missing/mismatched reference is a contract error,
        # never an invitation to sort by source name or revision text.
        priorities_by_source: dict[str | None, CalendarSourcePriority] = {}
        for source in {getattr(item, "source", None) for item in visible}:
            priorities_by_source[source] = _select_source_priority(
                source, source_priorities, day=day, context=pit_context
            )

        def primary_rank(item: object) -> tuple[int, timedelta, int]:
            source = getattr(item, "source", None)
            priority = priorities_by_source[source]
            if getattr(item, "source_priority_fact_id", None) != priority.fact_id:
                raise CalendarSourceRevisionConflictError(
                    "fact source priority reference does not match selected priority fact",
                    details={"source": source, "fact_id": str(getattr(item, "fact_id", ""))},
                )
            if getattr(item, "source_priority_version", None) != priority.source_priority_version:
                raise CalendarSourceRevisionConflictError(
                    "fact source priority version does not match selected priority fact",
                    details={"source": source, "fact_id": str(getattr(item, "fact_id", ""))},
                )
            if getattr(item, "source_priority", None) != priority.source_priority:
                raise CalendarSourceRevisionConflictError(
                    "fact source priority value is not registry-backed",
                    details={"source": source, "fact_id": str(getattr(item, "fact_id", ""))},
                )
            revision_order = getattr(item, "source_revision_order", None)
            if isinstance(revision_order, bool) or not isinstance(revision_order, int):
                raise CalendarSourceRevisionConflictError(
                    "calendar candidate source_revision_order is missing",
                    details={"fact_id": str(getattr(item, "fact_id", ""))},
                )
            if revision_order != priority.source_revision_order:
                raise CalendarSourceRevisionConflictError(
                    "fact source revision order is not registry-backed",
                    details={"source": source, "fact_id": str(getattr(item, "fact_id", ""))},
                )
            known_at = getattr(item, "known_at", None)
            if not isinstance(known_at, datetime) or known_at.tzinfo is None or known_at.utcoffset() is None:
                raise CalendarPitMetadataMissingError("calendar candidate known_at is missing")
            return (
                priority.source_priority,
                datetime.max.replace(tzinfo=timezone.utc)
                - known_at.astimezone(timezone.utc),
                -revision_order,
            )

        _validate_revision_chain(visible)
        best_rank = min(primary_rank(item) for item in visible)
        leaders = [item for item in visible if primary_rank(item) == best_rank]
        # A priority version is part of the ranking evidence.  It cannot be a
        # lexical tie-break when two sources occupy the same best rank.
        if len({priorities_by_source[getattr(item, "source", None)].source_priority_version for item in leaders}) > 1:
            raise CalendarSourceRevisionConflictError(
                "same-rank candidates use different source-priority versions",
                details={"date": day.isoformat()},
            )

    # Within one source and one best PIT rank, source_revision must be a
    # unique evidence value.  It is never sorted as text.
    for source in {getattr(item, "source", None) for item in leaders}:
        revisions = {
            getattr(item, "source_revision", None)
            for item in leaders
            if getattr(item, "source", None) == source
        }
        if len(revisions) > 1:
            raise CalendarSourceRevisionConflictError(
                "same source has multiple revisions at the selected PIT rank",
                details={"source": source, "date": day.isoformat()},
            )

    # Every candidate in the best rank group must prove both the same
    # advertised content hash and the same business payload before the
    # append-only fact version/UUID tie-break is allowed.
    content_keys = {
        (_candidate_content(item), _candidate_business_payload(item))
        for item in leaders
    }
    if len(content_keys) > 1:
        raise _pit_ambiguous_class(ambiguous_code)(
            "same-rank PIT candidates carry different content",
            details={
                "date": day.isoformat(),
                "fact_ids": [str(getattr(item, "fact_id", "")) for item in leaders],
            },
        )
    return min(
        leaders,
        key=lambda item: (
            -getattr(item, "fact_version", 1),
            _candidate_uuid_bytes(item),
        ),
    )


# ---------------------------------------------------------------------------
# Axis output, differences, snapshots and providers
# ---------------------------------------------------------------------------


class CalendarAxisStatus(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


class CalendarAxisDifferenceField(StrEnum):
    IS_OPEN = "is_open"
    TIMEZONE = "timezone"
    SESSIONS = "sessions"
    MISSING_FACT = "missing_fact"
    MISSING_DEFINITION = "missing_definition"
    UNRESOLVED_SESSION = "unresolved_session"
    REGISTRY = "registry"
    PIT_METADATA = "pit_metadata"


@dataclass(frozen=True, slots=True)
class CalendarAxisDifference:
    """Complete multi-calendar difference evidence."""

    date: date
    calendar_id: str
    field: CalendarAxisDifferenceField
    actual_value: str | None
    expected_value: str | None
    values_by_calendar: Mapping[str, object] | None = None
    definition_versions_by_calendar: Mapping[str, str | None] | None = None
    definition_fact_ids_by_calendar: Mapping[str, str | None] | None = None
    selected_fact_ids_by_calendar: Mapping[str, str | None] | None = None
    fact_versions_by_calendar: Mapping[str, int | None] | None = None
    source_revisions_by_calendar: Mapping[str, str | None] | None = None
    reference_calendar_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _plain_date(self.date, "date"))
        object.__setattr__(self, "calendar_id", normalize_calendar_id(self.calendar_id))
        if not isinstance(self.field, CalendarAxisDifferenceField):
            try:
                object.__setattr__(self, "field", CalendarAxisDifferenceField(self.field))
            except ValueError as exc:
                raise CalendarDomainError("field must be a CalendarAxisDifferenceField") from exc
        for name in ("values_by_calendar", "definition_versions_by_calendar", "definition_fact_ids_by_calendar", "selected_fact_ids_by_calendar", "fact_versions_by_calendar", "source_revisions_by_calendar"):
            value = getattr(self, name)
            if value is not None:
                frozen = freeze_json(dict(value), name)
                if not isinstance(frozen, MappingProxyType):
                    raise CalendarJsonInvalidError(f"{name} must be a JSON mapping")
                object.__setattr__(self, name, frozen)
        if self.error_code is not None:
            if not isinstance(self.error_code, str) or not self.error_code.strip():
                raise CalendarJsonInvalidError("difference error_code must be non-blank text")
            object.__setattr__(self, "error_code", self.error_code.strip())

    @property
    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (self.date.isoformat(), self.calendar_id, self.field.value, self.actual_value or "", self.expected_value or "")

    def evidence(self) -> Mapping[str, object]:
        return MappingProxyType({
            "date": self.date.isoformat(),
            "field": self.field.value,
            "calendar_ids": sorted((self.values_by_calendar or {}).keys()),
            "values_by_calendar": self.values_by_calendar or {},
            "definition_versions_by_calendar": self.definition_versions_by_calendar or {},
            "definition_fact_ids_by_calendar": self.definition_fact_ids_by_calendar or {},
            "selected_fact_ids_by_calendar": self.selected_fact_ids_by_calendar or {},
            "fact_versions_by_calendar": self.fact_versions_by_calendar or {},
            "source_revisions_by_calendar": self.source_revisions_by_calendar or {},
            "reference_calendar_id": self.reference_calendar_id,
            "error_code": self.error_code,
        })


@dataclass(frozen=True, slots=True)
class SessionPointContext:
    """Immutable instrument/calendar sidecar for one shared session point."""

    session_id: str
    session_date: date
    instrument_ids: tuple[UUID, ...] = ()
    calendar_ids: tuple[str, ...] = ()
    selected_fact_ids: Mapping[str, UUID | None] = field(default_factory=dict)
    definition_versions: Mapping[str, str | None] = field(default_factory=dict)
    snapshot_id: UUID | None = None
    snapshot_fingerprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _non_blank_text(self.session_id, "session_id"))
        object.__setattr__(self, "session_date", _plain_date(self.session_date, "session_date"))
        object.__setattr__(self, "instrument_ids", tuple(sorted({_uuid(item, "instrument_id") for item in self.instrument_ids}, key=str)))
        object.__setattr__(self, "calendar_ids", tuple(sorted({normalize_calendar_id(item) for item in self.calendar_ids})))
        object.__setattr__(self, "selected_fact_ids", MappingProxyType(dict(self.selected_fact_ids)))
        object.__setattr__(self, "definition_versions", MappingProxyType(dict(self.definition_versions)))
        if self.snapshot_id is not None:
            object.__setattr__(self, "snapshot_id", _uuid(self.snapshot_id, "snapshot_id"))


@dataclass(frozen=True, slots=True)
class SessionPoint:
    """One normalized common trading session on the daily axis."""

    session_date: date
    session_id: str
    timezone: str
    sessions: tuple[SessionWindow, ...]
    context: SessionPointContext | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_date", _plain_date(self.session_date, "session_date"))
        object.__setattr__(self, "session_id", _non_blank_text(self.session_id, "session_id"))
        object.__setattr__(self, "timezone", _timezone_name(self.timezone, "timezone"))
        object.__setattr__(self, "sessions", normalize_session_windows(self.sessions, "sessions"))
        if not self.sessions:
            raise CalendarSessionUnresolvedError("a common open session must carry at least one window")
        if self.context is not None and self.context.session_id != self.session_id:
            raise ProviderContractViolationError("SessionPointContext.session_id does not match SessionPoint")


@dataclass(frozen=True, slots=True)
class CalendarAxisResolution:
    """Immutable strict-axis resolution; blocked outcomes expose evidence only."""

    policy_key: str
    policy_version: str
    start_date: date
    end_date: date
    calendar_ids: tuple[str, ...]
    session_signature: str
    timezone: str | None
    resolved_sessions: tuple[SessionPoint, ...]
    status: CalendarAxisStatus
    differences: tuple[CalendarAxisDifference, ...]
    pit_context: CalendarPITContext | None = None
    selected_facts: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    resolved_calendar_definitions: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    calendar_semantic_signature: str = ""
    calendar_revision_digest: str = ""
    warmup_sessions: tuple[SessionPoint, ...] = ()
    warmup_session_signature: str = ""
    coverage_envelope: Mapping[str, object] | None = None
    non_strict_pit_capabilities: tuple[str, ...] = ()
    non_strict_pit: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_key", _non_blank_text(self.policy_key, "policy_key"))
        object.__setattr__(self, "policy_version", _non_blank_text(self.policy_version, "policy_version"))
        start, end = _plain_date(self.start_date, "start_date"), _plain_date(self.end_date, "end_date")
        if start > end:
            raise CalendarDomainError("start_date must not be later than end_date")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        ids = tuple(sorted({normalize_calendar_id(item) for item in self.calendar_ids}))
        if not ids:
            raise CalendarIdSetEmptyError("calendar_ids must not be empty")
        object.__setattr__(self, "calendar_ids", ids)
        try:
            status = CalendarAxisStatus(self.status)
        except ValueError as exc:
            raise CalendarDomainError("status must be compatible or incompatible") from exc
        object.__setattr__(self, "status", status)
        sessions = tuple(self.resolved_sessions)
        dates = [point.session_date for point in sessions]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ProviderContractViolationError("resolved_sessions must be ordered and unique")
        for point in sessions:
            if not start <= point.session_date <= end:
                raise ProviderContractViolationError("resolved_sessions must stay inside the formal range")
            if point.session_id not in {point.session_date.isoformat(), f"{point.session_date.isoformat()}"}:
                # Keep compatibility with legacy adapter ids such as
                # china_sse@YYYY-MM-DD while rejecting unrelated ids.
                if point.session_date.isoformat() not in point.session_id:
                    raise ProviderContractViolationError("session_id must identify its session_date")
        differences = tuple(sorted(tuple(self.differences), key=lambda item: item.sort_key))
        object.__setattr__(self, "differences", differences)
        if status is CalendarAxisStatus.INCOMPATIBLE:
            if not differences or sessions or self.timezone is not None or self.session_signature:
                raise CalendarDomainError("incompatible resolutions must expose evidence without consumable sessions")
        else:
            if differences or not self.session_signature or not sessions and self.status is CalendarAxisStatus.COMPATIBLE and self.session_signature == "":
                raise CalendarDomainError("compatible resolutions require a session signature and no differences")
            zones = {point.timezone for point in sessions}
            expected = next(iter(zones)) if len(zones) == 1 else None
            if self.timezone != expected:
                raise CalendarTimezoneMismatchError("compatible resolution must have one common timezone")
        object.__setattr__(self, "resolved_sessions", sessions)
        object.__setattr__(self, "warmup_sessions", tuple(self.warmup_sessions))
        object.__setattr__(self, "selected_facts", MappingProxyType({str(key): MappingProxyType(dict(value)) for key, value in self.selected_facts.items()}))
        object.__setattr__(self, "resolved_calendar_definitions", MappingProxyType({str(key): MappingProxyType(dict(value)) for key, value in self.resolved_calendar_definitions.items()}))
        if self.coverage_envelope is not None:
            object.__setattr__(self, "coverage_envelope", MappingProxyType(dict(self.coverage_envelope)))
        capabilities = tuple(sorted(set(str(value) for value in self.non_strict_pit_capabilities)))
        object.__setattr__(self, "non_strict_pit_capabilities", capabilities)
        if bool(capabilities) != bool(self.non_strict_pit):
            raise CalendarDomainError("non_strict_pit must equal whether the capability tuple is non-empty")


class CalendarAxisDataProvider(Protocol):
    def definitions(self, calendar_id: str) -> Sequence[CalendarDefinition]: ...
    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None: ...


@dataclass(frozen=True, slots=True)
class CalendarSnapshotRequest:
    """Canonical request consumed by atomic calendar snapshot providers."""

    calendar_ids: tuple[str, ...]
    formal_start: date
    formal_end: date
    warmup_sessions: int
    query_boundary: object
    instrument_ids: tuple[UUID, ...] = ()
    provider_key: str | None = None
    package_key: str | None = None
    package_version: int | str | None = None

    def __post_init__(self) -> None:
        ids = tuple(sorted({normalize_calendar_id(item) for item in self.calendar_ids}))
        if not ids:
            raise CalendarIdSetEmptyError("calendar_ids must not be empty")
        if len(ids) > MAX_CALENDAR_IDS:
            raise CalendarPreflightResourceLimitExceededError("calendar_id set exceeds the 32-calendar resource limit", details={"observed": len(ids), "limit": MAX_CALENDAR_IDS})
        start, end = _plain_date(self.formal_start, "formal_start"), _plain_date(self.formal_end, "formal_end")
        if start > end:
            raise CalendarDomainError("formal_start must not be later than formal_end")
        if (end - start).days + 1 > MAX_FORMAL_DATE_SPAN:
            raise CalendarDateSpanLimitExceededError("formal date span exceeds 10,000 natural days")
        if self.query_boundary is None:
            raise DataCutoffRequiredError("calendar snapshot requires query_boundary.data_cutoff")
        from app.backtesting.data.requests import QueryBoundary
        if not isinstance(self.query_boundary, QueryBoundary):
            raise CalendarJsonInvalidError("query_boundary must be a QueryBoundary")
        # Validate the formal upper bound while the request is still a pure
        # value object.  This is intentionally before a provider can resolve
        # registry/definition/fact rows, and uses the registry's one v1 local
        # timezone rather than the timestamp's surface UTC date.
        self.query_boundary.require_not_past_cutoff(
            end,
            CALENDAR_TIMEZONE_ASIA_SHANGHAI,
            "formal_end",
        )
        warmup = _non_negative_int(self.warmup_sessions, "warmup_sessions")
        if warmup > MAX_WARMUP_SESSIONS:
            raise LookbackSessionsLimitExceededError("warmup sessions exceed 512", details={"requested": warmup, "maximum": MAX_WARMUP_SESSIONS, "cause_code": "calendar_warmup_limit_exceeded"})
        object.__setattr__(self, "calendar_ids", ids)
        object.__setattr__(self, "formal_start", start)
        object.__setattr__(self, "formal_end", end)
        object.__setattr__(self, "warmup_sessions", warmup)
        object.__setattr__(self, "instrument_ids", tuple(sorted({_uuid(item, "instrument_id") for item in self.instrument_ids}, key=str)))
        object.__setattr__(self, "provider_key", _optional_text(self.provider_key, "provider_key"))
        package_key = _optional_text(self.package_key, "package_key")
        package_version = self.package_version
        if package_version is not None:
            if isinstance(package_version, bool) or not isinstance(package_version, (int, str)):
                raise CalendarDomainError("package_version must be text or an integer when provided")
            if isinstance(package_version, str):
                package_version = package_version.strip()
                if not package_version:
                    raise CalendarDomainError("package_version must be non-blank when provided")
            if package_key is None:
                raise CalendarDomainError("package_key is required when package_version is provided")
        elif package_key is not None:
            raise CalendarDomainError("package_version is required when package_key is provided")
        object.__setattr__(self, "package_key", package_key)
        object.__setattr__(self, "package_version", package_version)


class NeighborState(StrEnum):
    FOUND = "FOUND"
    NONE_WITHIN_COVERAGE = "NONE_WITHIN_COVERAGE"
    UNKNOWN_COVERAGE = "UNKNOWN_COVERAGE"


@dataclass(frozen=True, slots=True)
class NeighborResult:
    state: NeighborState
    target_date: date
    session: SessionPoint | None = None
    coverage_scope: str = "common"
    floor: date | None = None
    ceiling: date | None = None
    gaps: tuple[tuple[date, date], ...] = ()
    revision_summary: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_date", _plain_date(self.target_date, "target_date"))
        object.__setattr__(self, "state", NeighborState(self.state))
        if self.state is NeighborState.FOUND and self.session is None:
            raise CalendarDomainError("FOUND neighbor result requires a session")
        if self.state is not NeighborState.FOUND and self.session is not None:
            raise CalendarDomainError("non-FOUND neighbor result must not carry a session")
        if self.floor is not None:
            object.__setattr__(self, "floor", _plain_date(self.floor, "floor"))
        if self.ceiling is not None:
            object.__setattr__(self, "ceiling", _plain_date(self.ceiling, "ceiling"))
        object.__setattr__(self, "gaps", tuple((_plain_date(start, "gap start"), _plain_date(end, "gap end")) for start, end in self.gaps))
        if self.revision_summary is not None:
            object.__setattr__(self, "revision_summary", MappingProxyType(dict(self.revision_summary)))


@dataclass(frozen=True, slots=True)
class CalendarSnapshot:
    """Immutable formal+warmup envelope produced by one provider attempt."""

    snapshot_id: UUID
    request: CalendarSnapshotRequest
    pit_context: CalendarPITContext
    resolution: CalendarAxisResolution
    warmup_sessions: tuple[SessionPoint, ...]
    envelope_start: date
    envelope_end_exclusive: date
    coverage: Mapping[str, object]
    revision_watermark: str
    snapshot_fingerprint: str
    attempt_id: UUID | None = None
    prepare_calls: int = 1
    batch_read_calls: int = 1
    resolved_calendar_definitions: tuple[CalendarDefinition, ...] = ()
    resolved_calendar_bindings: Mapping[str, object] = field(default_factory=dict)
    open_session_index: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _uuid(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "envelope_start", _plain_date(self.envelope_start, "envelope_start"))
        object.__setattr__(self, "envelope_end_exclusive", _plain_date(self.envelope_end_exclusive, "envelope_end_exclusive"))
        if self.envelope_start >= self.envelope_end_exclusive:
            raise CalendarDomainError("snapshot envelope must be non-empty")
        try:
            frozen_coverage = freeze_json(_json_safe(dict(self.coverage)), "snapshot.coverage")
            frozen_bindings = freeze_json(_json_safe(dict(self.resolved_calendar_bindings)), "snapshot.resolved_calendar_bindings")
            frozen_index = freeze_json(_json_safe(dict(self.open_session_index)), "snapshot.open_session_index")
        except ValueError as exc:
            raise ProviderContractViolationError("snapshot evidence must be JSON serializable") from exc
        if not isinstance(frozen_coverage, MappingProxyType) or not isinstance(frozen_bindings, MappingProxyType) or not isinstance(frozen_index, MappingProxyType):
            raise ProviderContractViolationError("snapshot evidence must be JSON mappings")
        object.__setattr__(self, "coverage", frozen_coverage)
        warmup = tuple(self.warmup_sessions)
        object.__setattr__(self, "warmup_sessions", warmup)
        if self.resolution.calendar_ids != self.request.calendar_ids:
            raise ProviderContractViolationError("snapshot resolution calendar_ids do not match request")
        if self.resolution.pit_context != self.pit_context:
            raise ProviderContractViolationError("snapshot resolution PIT context does not match snapshot")
        if any(point.session_date in {item.session_date for item in self.resolution.resolved_sessions} for point in warmup):
            raise ProviderContractViolationError("formal and warmup sessions must be disjoint")
        if any(point.session_date < self.envelope_start or point.session_date >= self.envelope_end_exclusive for point in (*warmup, *self.resolution.resolved_sessions)):
            raise ProviderContractViolationError("snapshot sessions must lie inside its envelope")
        if self.prepare_calls != 1 or self.batch_read_calls != 1:
            raise ProviderContractViolationError("one snapshot attempt must perform exactly one prepare and one batch read")
        if len(self.snapshot_fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in self.snapshot_fingerprint):
            raise ProviderContractViolationError("snapshot_fingerprint must be a lowercase SHA-256 digest")
        definitions = tuple(self.resolved_calendar_definitions)
        if any(not isinstance(item, CalendarDefinition) for item in definitions):
            raise ProviderContractViolationError("snapshot definitions must be CalendarDefinition values")
        # Definitions are value objects, but their provenance evidence may have
        # originated as a mutable dict/list.  Copy each selected definition
        # with recursively frozen evidence so the snapshot cannot be mutated
        # through either the original source object or a nested value.
        try:
            frozen_definitions = tuple(
                replace(item, evidence=freeze_json(item.evidence, "snapshot.definition.evidence"))
                for item in definitions
            )
        except ValueError as exc:
            raise ProviderContractViolationError("snapshot definition evidence must be JSON serializable") from exc
        object.__setattr__(self, "resolved_calendar_definitions", tuple(sorted(frozen_definitions, key=lambda item: (item.calendar_id, item.definition_version, item.fact_version, str(item.fact_id)))))
        object.__setattr__(self, "resolved_calendar_bindings", frozen_bindings)
        object.__setattr__(self, "open_session_index", frozen_index)
        if self.attempt_id is not None:
            object.__setattr__(self, "attempt_id", _uuid(self.attempt_id, "attempt_id"))

    @property
    def warmup_start(self) -> date:
        """The exact envelope start proven by the prepare-stage index."""

        return self.envelope_start

    @property
    def calendar_ids(self) -> tuple[str, ...]:
        """Canonical calendar IDs frozen by this snapshot."""

        return self.request.calendar_ids

    @property
    def resolved_sessions(self) -> tuple[SessionPoint, ...]:
        """Formal sessions frozen by this snapshot."""

        return self.resolution.resolved_sessions

    @property
    def calendar_revision_digest(self) -> str:
        """Revision digest used as the immutable snapshot watermark."""

        return self.resolution.calendar_revision_digest or self.revision_watermark

    @property
    def calendar_semantic_signature(self) -> str:
        """Final daily calendar semantic signature."""

        return self.resolution.calendar_semantic_signature

    @property
    def calendar_session_signature(self) -> str:
        """Formal-session signature including selected revision evidence."""

        return self.resolution.session_signature

    @property
    def warmup_session_signature(self) -> str:
        """Warmup-only signature, excluded from the formal axis."""

        return self.resolution.warmup_session_signature

    def previous(self, target_date: date) -> NeighborResult:
        return self._neighbor(target_date, before=True)

    def next(self, target_date: date) -> NeighborResult:
        return self._neighbor(target_date, before=False)

    def _neighbor(self, target_date: date, *, before: bool) -> NeighborResult:
        target = _plain_date(target_date, "target_date")
        common = self.coverage.get("common", {}) if isinstance(self.coverage, Mapping) else {}
        floor = date.fromisoformat(common["floor"]) if isinstance(common, Mapping) and common.get("floor") else None
        ceiling = date.fromisoformat(common["ceiling"]) if isinstance(common, Mapping) and common.get("ceiling") else None
        raw_gaps = common.get("gaps", ()) if isinstance(common, Mapping) else ()
        gaps = tuple((date.fromisoformat(item[0]), date.fromisoformat(item[1])) for item in raw_gaps)
        raw_segments = common.get("segments", ()) if isinstance(common, Mapping) else ()
        segments = tuple(
            (date.fromisoformat(item[0]), date.fromisoformat(item[1]))
            for item in raw_segments
            if isinstance(item, (list, tuple)) and len(item) == 2
        )
        if floor is None or ceiling is None or target < floor or target >= ceiling:
            return NeighborResult(NeighborState.UNKNOWN_COVERAGE, target, floor=floor, ceiling=ceiling, gaps=gaps)
        segment = next(((start, end) for start, end in segments if start <= target < end), None)
        if segment is None or any(start < segment[1] and end > segment[0] for start, end in gaps):
            return NeighborResult(NeighborState.UNKNOWN_COVERAGE, target, floor=floor, ceiling=ceiling, gaps=gaps)
        points = tuple(
            point for point in (*self.warmup_sessions, *self.resolution.resolved_sessions)
            if segment[0] <= point.session_date < segment[1]
            and (point.session_date < target if before else point.session_date > target)
        )
        if points:
            return NeighborResult(
                NeighborState.FOUND,
                target,
                session=(points[-1] if before else points[0]),
                floor=floor,
                ceiling=ceiling,
                gaps=gaps,
            )
        return NeighborResult(NeighborState.NONE_WITHIN_COVERAGE, target, floor=floor, ceiling=ceiling, gaps=gaps)


@dataclass(frozen=True, slots=True)
class _ResolvedDay:
    is_open: bool
    timezone: str | None
    sessions: tuple[SessionWindow, ...]
    fact: CalendarSessionFact
    definition: CalendarDefinition


@dataclass(frozen=True, slots=True)
class _FailedDay:
    field: CalendarAxisDifferenceField
    actual_value: str | None
    expected_value: str | None
    fact: CalendarSessionFact | None = None
    definition: CalendarDefinition | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _CalendarIndexMetadata:
    """Prepare-stage metadata for one natural-day fact slot.

    The prepare phase is deliberately forbidden from materializing calendar
    definitions or session-window payloads.  This cell therefore carries
    only the selected fact identity/version, its open flag, and whether the
    resolution-head proof is complete.  Full facts and definitions are read
    exactly once by the subsequent batch phase and aligned against these
    fields before a snapshot can be published.
    """

    is_open: bool | None
    selected_fact_id: UUID | None
    fact_version: int | None
    complete: bool = True
    error_code: str | None = None


class InMemoryCalendarAxisDataProvider:
    """Immutable in-memory provider supporting legacy and strict batch paths."""

    def __init__(
        self,
        definitions: Iterable[CalendarDefinition] = (),
        facts: Iterable[CalendarSessionFact] = (),
        *,
        registries: Iterable[CalendarRegistry] = (),
        bindings: Iterable[CalendarExchangeBinding] = (),
        capabilities: Iterable[CalendarCapabilityDeclaration] = (),
        source_priorities: Iterable[CalendarSourcePriority] = (),
        fixture_revision: str = "memory-calendar@1",
    ) -> None:
        self._definitions = tuple(definitions)
        self._facts = tuple(facts)
        self._registries = tuple(registries)
        self._bindings = tuple(bindings)
        self._capabilities = tuple(capabilities)
        self._source_priorities = tuple(source_priorities)
        self._fixture_revision = _non_blank_text(fixture_revision, "fixture_revision")
        self._definition_index: dict[str, tuple[CalendarDefinition, ...]] = {}
        self._fact_index: dict[tuple[str, date], tuple[CalendarSessionFact, ...]] = {}
        self._registry_index: dict[str, tuple[CalendarRegistry, ...]] = {}
        for definition in self._definitions:
            self._definition_index.setdefault(definition.calendar_id, ())
            self._definition_index[definition.calendar_id] += (definition,)
        grouped: dict[tuple[str, date], list[CalendarSessionFact]] = {}
        for fact in self._facts:
            grouped.setdefault((fact.calendar_id, fact.session_date), []).append(fact)
        self._fact_index = {key: tuple(sorted(values, key=lambda item: (item.fact_version, str(item.fact_id)))) for key, values in grouped.items()}
        for registry in self._registries:
            self._registry_index.setdefault(registry.calendar_id, ())
            self._registry_index[registry.calendar_id] += (registry,)
        self.prepare_calls = 0
        self.batch_read_calls = 0
        self.fact_calls = 0
        self._snapshots: dict[UUID, CalendarSnapshot] = {}

    @property
    def fixture_revision(self) -> str:
        return self._fixture_revision

    def definitions(self, calendar_id: str) -> tuple[CalendarDefinition, ...]:
        try:
            canonical = normalize_calendar_id(calendar_id)
        except CalendarContractError:
            return ()
        return self._definition_index.get(canonical, ())

    def fact_candidates(self, calendar_id: str, day: date) -> tuple[CalendarSessionFact, ...]:
        self.fact_calls += 1
        try:
            canonical = normalize_calendar_id(calendar_id)
        except CalendarContractError:
            return ()
        return self._fact_index.get((canonical, _plain_date(day, "day")), ())

    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None:
        candidates = self.fact_candidates(calendar_id, day)
        return candidates[-1] if candidates else None

    def registries(self, calendar_id: str) -> tuple[CalendarRegistry, ...]:
        return self._registry_index.get(normalize_calendar_id(calendar_id), ())

    def bindings(self, alias: str) -> tuple[CalendarExchangeBinding, ...]:
        normalized = _non_blank_text(alias, "alias")
        normalized = "".join(ch.upper() if "a" <= ch <= "z" else ch for ch in normalized)
        return tuple(item for item in self._bindings if item.alias == normalized)

    def capabilities(self) -> tuple[CalendarCapabilityDeclaration, ...]:
        return self._capabilities

    def source_priorities(self) -> tuple[CalendarSourcePriority, ...]:
        return self._source_priorities

    def resolve_capability(
        self,
        capability: str,
        *,
        effective_day: date,
        pit_context: CalendarPITContext | None = None,
        provider_key: str | None = None,
        package_key: str | None = None,
        package_version: int | str | None = None,
        calendar_id: str | None = None,
        instrument_id: UUID | None = None,
    ) -> CapabilityResolution:
        """Resolve a single-scope declaration at the requested PIT point."""

        return select_capability_declaration(
            self._capabilities,
            capability=capability,
            effective_day=effective_day,
            pit_context=pit_context,
            provider_key=provider_key,
            package_key=package_key,
            package_version=package_version,
            calendar_id=calendar_id,
            instrument_id=instrument_id,
            source_priorities=self._source_priorities,
        )

    def resolve_binding(self, alias: str, *, effective_day: date, pit_context: CalendarPITContext | None = None) -> CalendarExchangeBinding:
        rows = [row for row in self.bindings(alias) if row.applies_to(effective_day)]
        if not rows:
            raise CalendarBindingUnknownError("no visible exchange binding", details={"alias": alias})
        selected = select_pit_candidate(
            rows,
            effective_day=effective_day,
            pit_context=pit_context,
            source_priorities=self._source_priorities,
            missing_code="calendar_binding_missing",
            ambiguous_code="calendar_binding_ambiguous",
        )
        assert isinstance(selected, CalendarExchangeBinding)
        if pit_context is not None:
            selected.strict_validate()
        return selected

    def resolve_registry(self, calendar_id: str, *, effective_day: date, pit_context: CalendarPITContext | None = None) -> CalendarRegistry:
        rows = [row for row in self.registries(calendar_id) if row.applies_to(effective_day)]
        if not rows:
            raise CalendarRegistryFactMissingError("no visible calendar registry fact", details={"calendar_id": calendar_id})
        selected = select_pit_candidate(
            rows,
            effective_day=effective_day,
            pit_context=pit_context,
            source_priorities=self._source_priorities,
            missing_code="calendar_registry_fact_missing",
            ambiguous_code="calendar_registry_ambiguous",
        )
        assert isinstance(selected, CalendarRegistry)
        if pit_context is not None:
            selected.strict_validate()
        return selected

    def open_calendar_snapshot(
        self,
        request: CalendarSnapshotRequest | object,
        *,
        query_boundary: object | None = None,
    ) -> CalendarSnapshot:
        """Open one immutable formal+warmup snapshot with one batch read."""

        if isinstance(request, CalendarSnapshotRequest):
            if (
                query_boundary is not None
                and query_boundary != request.query_boundary
            ):
                raise InvalidDataRequestError(
                    "snapshot query_boundary must match the request boundary"
                )
        else:
            request = _snapshot_request_from_object(
                request,
                query_boundary=query_boundary,
            )
        self.prepare_calls += 1
        # The v1 registry policy fixes the only supported calendar timezone;
        # derive the context from the request before selecting registry rows so
        # registry visibility itself is PIT-bound.
        if request.query_boundary is None:
            raise DataCutoffRequiredError("strict calendar snapshot requires query_boundary")
        provisional_context = CalendarPITContext.from_query_boundary(request.query_boundary, CALENDAR_TIMEZONE_ASIA_SHANGHAI)
        registries = {
            cid: self.resolve_registry(
                cid,
                effective_day=request.formal_start,
                pit_context=provisional_context,
            )
            for cid in request.calendar_ids
        }
        if {registry.timezone for registry in registries.values()} != {CALENDAR_TIMEZONE_ASIA_SHANGHAI}:
            raise CalendarTimezoneMismatchError("participating calendars do not share one IANA timezone")
        provisional_context.require_date(request.formal_end, "formal_end")
        # Canonical calendar IDs may arrive directly from InstrumentIdentityFact
        # and therefore need no exchange alias.  When the explicit canonical
        # alias exists, nevertheless select it at the same PIT and expose the
        # selected binding as audit evidence; absence is not converted into an
        # implicit SSE binding.
        binding_selections: dict[str, object] = {}
        for cid in request.calendar_ids:
            alias_rows = [row for row in self.bindings(cid) if row.canonical_calendar_id == cid and row.applies_to(request.formal_start)]
            if not alias_rows:
                binding_selections[cid] = {
                    "alias": None,
                    "selected_fact_id": None,
                    "fact_version": None,
                    "binding_version": None,
                    "registry_fact_id": registries[cid].fact_id,
                    "registry_version": registries[cid].registry_version,
                    "missing_reason": "canonical_binding_not_required",
                }
                continue
            binding = select_pit_candidate(
                alias_rows,
                effective_day=request.formal_start,
                pit_context=provisional_context,
                source_priorities=self._source_priorities,
                missing_code="calendar_binding_missing",
                ambiguous_code="calendar_binding_ambiguous",
            )
            assert isinstance(binding, CalendarExchangeBinding)
            if binding.registry_fact_id != registries[cid].fact_id or binding.registry_version != registries[cid].registry_version:
                raise CalendarRegistryReferenceInvalidError("binding registry reference does not match selected registry")
            binding_selections[cid] = {
                "alias": binding.alias,
                "selected_fact_id": binding.fact_id,
                "fact_version": binding.fact_version,
                "binding_version": binding.binding_version,
                "registry_fact_id": binding.registry_fact_id,
                "registry_version": binding.registry_version,
            }
        # Build one detached batch projection.  All subsequent anchor and
        # formal+warmup resolution calls operate on this projection, never on
        # the provider's per-date compatibility methods; this mirrors the SQL
        # provider's one set-based read and keeps ``fact_calls`` at zero for an
        # atomic snapshot attempt.
        batch_provider = InMemoryCalendarAxisDataProvider(
            self._definitions,
            self._facts,
            registries=self._registries,
            bindings=self._bindings,
            capabilities=self._capabilities,
            source_priorities=self._source_priorities,
            fixture_revision=self._fixture_revision,
        )
        formal_days = list(_iterate_days(request.formal_start, request.formal_end))
        formal_end_exclusive = request.formal_end + timedelta(days=1)
        # The prepare contract always searches one fixed, finite natural-day
        # envelope.  It must not use the first stored fact as an implicit
        # shortcut because doing so would make the SQL and memory index
        # watermarks differ and could hide an unproven gap.
        index_start = (
            request.formal_start
            if request.warmup_sessions == 0
            else request.formal_start - timedelta(days=MAX_WARMUP_SEARCH_SPAN)
        )
        # This detached index is the prepare-stage artifact.  It contains
        # selected natural-day outcomes and is reused by anchor, warmup and
        # coverage calculations; no later phase performs a date-by-date SQL
        # lookup or widens the envelope after the batch read.
        # Prepare reads only the in-memory resolution metadata index.  In
        # particular, do not call ``definitions``/``fact_candidates`` or the
        # full resolver before the batch-read boundary.
        prepare_index = _materialize_calendar_metadata_index(
            batch_provider,
            request.calendar_ids,
            index_start,
            formal_end_exclusive,
            provisional_context,
        )
        anchor_candidate = _find_common_anchor(prepare_index, request.calendar_ids, formal_days)
        envelope_start = request.formal_start
        if request.warmup_sessions and anchor_candidate is not None:
            open_history = _find_common_open_history(
                prepare_index,
                request.calendar_ids,
                anchor_candidate,
                request.warmup_sessions,
            )
            if len(open_history) < request.warmup_sessions:
                raise CalendarSnapshotCoverageUnknownError(
                    "warmup coverage is insufficient",
                    details={
                        "cause_code": "warmup_coverage_insufficient",
                        "requested_sessions": request.warmup_sessions,
                        "actual_sessions": len(open_history),
                    },
                )
            envelope_start = open_history[0]
            if (anchor_candidate - envelope_start).days > MAX_WARMUP_SEARCH_SPAN:
                raise CalendarDateSpanLimitExceededError("warmup search span exceeds 10,000 natural days")
        elif request.warmup_sessions and anchor_candidate is None:
            # Formal all-closed is read only for formal evidence; no warmup
            # search is allowed because there is no anchor.
            envelope_start = request.formal_start
        formal_end_exclusive = request.formal_end + timedelta(days=1)
        self.batch_read_calls += 1
        # The single batch read is the first point at which definitions and
        # session-window payloads may be materialized.  Its selected fact
        # identities must agree with the metadata index used for prepare.
        batch_index = _materialize_calendar_index(
            batch_provider,
            request.calendar_ids,
            envelope_start,
            formal_end_exclusive,
            provisional_context,
        )
        _assert_metadata_index_alignment(
            prepare_index,
            batch_index,
            request.calendar_ids,
            start=envelope_start,
            end_exclusive=formal_end_exclusive,
        )
        resolution, warmup = _resolve_snapshot_range(
            batch_provider,
            request.calendar_ids,
            request.formal_start,
            request.formal_end,
            envelope_start,
            formal_end_exclusive,
            provisional_context,
            request.instrument_ids,
            index=batch_index,
            warmup_count=request.warmup_sessions,
        )
        revision_payload = _revision_payload(batch_provider, request.calendar_ids, envelope_start, formal_end_exclusive, provisional_context)
        revision_digest = canonical_hash(revision_payload)
        # Rebuild resolution with provenance fields/fingerprint while keeping
        # the old constructor/API semantics intact.
        semantic_signature = _semantic_signature(resolution, request.formal_start, request.formal_end)
        session_signature = _session_signature(resolution, request.formal_start, request.formal_end, revision_digest, provisional_context)
        warmup_signature = _warmup_signature(warmup, revision_digest, anchor_candidate, request.warmup_sessions)
        coverage = _coverage_payload(
            batch_provider,
            request.calendar_ids,
            envelope_start,
            formal_end_exclusive,
            provisional_context,
            index=batch_index,
        )
        snapshot_payload = {
            "request": _snapshot_request_payload(request),
            "pit_context": dict(provisional_context.as_dict),
            "registry_selection": {
                cid: {
                    "fact_id": registries[cid].fact_id,
                    "registry_version": registries[cid].registry_version,
                }
                for cid in request.calendar_ids
            },
            "binding_selection": binding_selections,
            "coverage": coverage,
            "calendar_semantic_signature": semantic_signature,
            "calendar_session_signature": session_signature,
            "warmup_session_signature": warmup_signature,
            "calendar_revision_digest": revision_digest,
            "envelope": {
                "start_date": envelope_start,
                "end_date_exclusive": formal_end_exclusive,
            },
            # Persist only the envelope proven for this snapshot.  The
            # prepare-stage warmup search may inspect a wider bounded index,
            # but that extra history is not part of the immutable snapshot
            # evidence and would make memory and SQL fingerprints diverge.
            "open_session_index": _open_index_payload(
                batch_index,
                request.calendar_ids,
                start=envelope_start,
                end_exclusive=formal_end_exclusive,
            ),
            "protocol_version": "calendar_snapshot@1",
        }
        fingerprint = canonical_hash(snapshot_payload)
        snapshot_id = _legacy_uuid("snapshot", fingerprint, 1)
        context_ids = {}
        for point in (*warmup, *resolution.resolved_sessions):
            selected_fact_ids = {}
            definition_versions = {}
            for cid in request.calendar_ids:
                selected = resolution.selected_facts.get(f"{cid}:{point.session_date.isoformat()}", {})
                definition = resolution.resolved_calendar_definitions.get(f"{cid}:{point.session_date.isoformat()}", {})
                if isinstance(selected.get("selected_fact_id"), UUID):
                    selected_fact_ids[cid] = selected["selected_fact_id"]
                definition_versions[cid] = definition.get("definition_version")
            context_ids[point.session_id] = SessionPointContext(
                point.session_id,
                point.session_date,
                request.instrument_ids,
                request.calendar_ids,
                selected_fact_ids,
                definition_versions,
                snapshot_id,
                fingerprint,
            )
        # Sidecars are intentionally attached only when instruments were
        # explicitly frozen; absence is valid for a calendar-only diagnostic.
        formal = tuple(
            SessionPoint(point.session_date, point.session_id, point.timezone, point.sessions, context_ids.get(point.session_id))
            for point in resolution.resolved_sessions
        )
        warmup_with_context = tuple(
            SessionPoint(point.session_date, point.session_id, point.timezone, point.sessions, context_ids.get(point.session_id))
            for point in warmup
        )
        resolution = CalendarAxisResolution(
            policy_key=resolution.policy_key,
            policy_version=resolution.policy_version,
            start_date=resolution.start_date,
            end_date=resolution.end_date,
            calendar_ids=resolution.calendar_ids,
            session_signature=(session_signature if resolution.status is CalendarAxisStatus.COMPATIBLE else ""),
            timezone=resolution.timezone,
            resolved_sessions=formal,
            status=resolution.status,
            differences=resolution.differences,
            pit_context=provisional_context,
            selected_facts=resolution.selected_facts,
            resolved_calendar_definitions=resolution.resolved_calendar_definitions,
            calendar_semantic_signature=semantic_signature,
            calendar_revision_digest=revision_digest,
            warmup_sessions=warmup_with_context,
            warmup_session_signature=warmup_signature,
            coverage_envelope=coverage,
            non_strict_pit_capabilities=(),
            non_strict_pit=False,
        )
        snapshot = CalendarSnapshot(
            snapshot_id=snapshot_id,
            request=request,
            pit_context=provisional_context,
            resolution=resolution,
            warmup_sessions=warmup_with_context,
            envelope_start=envelope_start,
            envelope_end_exclusive=formal_end_exclusive,
            coverage=coverage,
            revision_watermark=revision_digest,
            snapshot_fingerprint=fingerprint,
            open_session_index=_open_index_payload(
                batch_index,
                request.calendar_ids,
                start=envelope_start,
                end_exclusive=formal_end_exclusive,
            ),
            resolved_calendar_definitions=_selected_snapshot_definitions(
                batch_provider,
                request.calendar_ids,
                resolution,
                envelope_start,
                formal_end_exclusive,
                provisional_context,
            ),
            resolved_calendar_bindings=binding_selections,
        )
        self._snapshots[snapshot_id] = snapshot
        return snapshot

    def snapshot(self, snapshot_id: UUID) -> CalendarSnapshot | None:
        return self._snapshots.get(snapshot_id)


# ---------------------------------------------------------------------------
# Pure strict resolver (legacy and modern paths)
# ---------------------------------------------------------------------------


def _resolve_calendar_day_legacy(provider: CalendarAxisDataProvider, calendar_id: str, day: date) -> _ResolvedDay | _FailedDay:
    candidates_method = getattr(provider, "fact_candidates", None)
    if callable(candidates_method):
        facts = tuple(candidates_method(calendar_id, day))
        fact = facts[-1] if facts else None
        if len(facts) > 1:
            try:
                fact = select_pit_candidate(facts, effective_day=day)
            except CalendarContractError:
                return _FailedDay(CalendarAxisDifferenceField.MISSING_FACT, "ambiguous", "one fact")
    else:
        fact = provider.fact(calendar_id, day)
    if fact is None or fact.session_date != day or fact.calendar_id != calendar_id:
        return _FailedDay(CalendarAxisDifferenceField.MISSING_FACT, "missing", "present")
    definitions = [definition for definition in provider.definitions(calendar_id) if definition.definition_version == fact.definition_version and definition.applies_to(day)]
    if not definitions:
        return _FailedDay(CalendarAxisDifferenceField.MISSING_DEFINITION, fact.definition_version, "exactly one applicable definition")
    if len(definitions) > 1:
        return _FailedDay(CalendarAxisDifferenceField.MISSING_DEFINITION, f"ambiguous:{len(definitions)}", "exactly one applicable definition")
    definition = definitions[0]
    timezone_name, windows = fact.effective_timezone_and_sessions(definition)
    if not fact.is_open:
        return _ResolvedDay(False, None, (), fact, definition)
    if not timezone_name or not windows:
        return _FailedDay(CalendarAxisDifferenceField.UNRESOLVED_SESSION, "unresolvable", "resolved timezone and sessions", fact, definition)
    return _ResolvedDay(True, timezone_name, windows, fact, definition)


def _resolve_calendar_day_modern(
    provider: object,
    calendar_id: str,
    day: date,
    context: CalendarPITContext,
) -> _ResolvedDay | _FailedDay:
    try:
        candidates_method = getattr(provider, "fact_candidates", None)
        candidates = (
            tuple(candidates_method(calendar_id, day))
            if callable(candidates_method)
            else tuple(
                fact
                for fact in (getattr(provider, "fact")(calendar_id, day),)
                if fact is not None
            )
        )
        priority_method = getattr(provider, "source_priorities", None)
        priorities = tuple(priority_method()) if callable(priority_method) else ()
        fact = select_pit_candidate(
            candidates,
            effective_day=day,
            pit_context=context,
            source_priorities=priorities,
            missing_code="calendar_fact_missing",
            ambiguous_code="calendar_fact_ambiguous",
        )
        fact.strict_validate()
    except CalendarFactMissingError as exc:
        return _FailedDay(
            CalendarAxisDifferenceField.MISSING_FACT,
            "missing",
            "present",
            error_code=exc.code,
        )
    except CalendarFactAmbiguousError as exc:
        return _FailedDay(
            CalendarAxisDifferenceField.MISSING_FACT,
            "ambiguous",
            "one fact",
            error_code=exc.code,
        )
    except CalendarFactInvalidError as exc:
        return _FailedDay(
            CalendarAxisDifferenceField.MISSING_FACT,
            "invalid",
            "accepted fact",
            error_code=exc.code,
        )
    except CalendarPitMetadataMissingError as exc:
        return _FailedDay(
            CalendarAxisDifferenceField.PIT_METADATA,
            "missing",
            "known_at/knowledge range",
            error_code=exc.code,
        )
    except CalendarContractError as exc:
        # A source-priority/chain failure is preserved as machine evidence in
        # the blocked axis instead of being reclassified as a missing fact.
        return _FailedDay(
            CalendarAxisDifferenceField.PIT_METADATA,
            getattr(exc, "code", "invalid"),
            "valid PIT metadata",
            error_code=getattr(exc, "code", "invalid"),
        )

    selected_registry = None
    try:
        registry_method = getattr(provider, "resolve_registry", None)
        if callable(registry_method):
            selected_registry = registry_method(
                calendar_id,
                effective_day=day,
                pit_context=context,
            )
            if fact.registry_fact_id is None or fact.registry_version is None:
                raise CalendarRegistryReferenceInvalidError(
                    "session fact registry reference is incomplete"
                )
            if (
                fact.registry_fact_id != selected_registry.fact_id
                or fact.registry_version != selected_registry.registry_version
            ):
                raise CalendarRegistryReferenceInvalidError(
                    "session fact registry reference does not match selected registry"
                )
    except CalendarContractError as exc:
        return _FailedDay(
            CalendarAxisDifferenceField.REGISTRY,
            getattr(exc, "code", "calendar_registry_reference_invalid"),
            "selected registry fact",
            fact=fact,
            error_code=getattr(exc, "code", "calendar_registry_reference_invalid"),
        )

    try:
        definition_candidates = [
            definition
            for definition in provider.definitions(calendar_id)
            if definition.definition_version == fact.definition_version
        ]
        priority_method = getattr(provider, "source_priorities", None)
        priorities = tuple(priority_method()) if callable(priority_method) else ()
        definition = select_pit_candidate(
            definition_candidates,
            effective_day=day,
            pit_context=context,
            source_priorities=priorities,
            missing_code="calendar_definition_missing",
            ambiguous_code="calendar_definition_ambiguous",
        )
        definition.strict_validate()
        if fact.definition_fact_id is not None and fact.definition_fact_id != definition.fact_id:
            raise CalendarSourceRevisionConflictError(
                "session fact definition reference does not match selected definition"
            )
        if selected_registry is not None and (
            definition.registry_fact_id != selected_registry.fact_id
            or definition.registry_version != selected_registry.registry_version
        ):
            raise CalendarRegistryReferenceInvalidError(
                "definition registry reference does not match selected registry"
            )
    except CalendarDefinitionMissingError as exc:
        return _FailedDay(
            CalendarAxisDifferenceField.MISSING_DEFINITION,
            fact.definition_version,
            "exactly one applicable definition",
            fact=fact,
            error_code=exc.code,
        )
    except CalendarDefinitionAmbiguousError as exc:
        return _FailedDay(
            CalendarAxisDifferenceField.MISSING_DEFINITION,
            "ambiguous",
            "exactly one applicable definition",
            fact=fact,
            error_code=exc.code,
        )
    except CalendarContractError as exc:
        return _FailedDay(
            CalendarAxisDifferenceField.MISSING_DEFINITION,
            getattr(exc, "code", "invalid"),
            "valid definition",
            fact=fact,
            error_code=getattr(exc, "code", "invalid"),
        )
    # Keep the effective IANA timezone in the resolved outcome and defer the
    # v1 Asia/Shanghai support check to the axis-level pass.  This lets the
    # strict policy distinguish a calendar that changes timezone across dates
    # (``calendar_timezone_inconsistent``) from one that uses one unsupported
    # timezone throughout (``calendar_timezone_unsupported``), while also
    # reporting cross-calendar differences as ``calendar_timezone_mismatch``.
    timezone_name, windows = fact.effective_timezone_and_sessions(definition)
    if not fact.is_open:
        return _ResolvedDay(False, timezone_name, (), fact, definition)
    if not windows:
        return _FailedDay(
            CalendarAxisDifferenceField.UNRESOLVED_SESSION,
            "unresolvable:sessions",
            "resolved timezone and sessions",
            fact,
            definition,
            "calendar_session_unresolved",
        )
    return _ResolvedDay(True, timezone_name, windows, fact, definition)


def _canonical_day_record(day: date, is_open: bool, timezone_name: str | None, sessions: Sequence[SessionWindow]) -> dict[str, object]:
    return {"date": day.isoformat(), "is_open": is_open, "timezone": timezone_name, "sessions": [window.semantic_payload() for window in sessions]}


def _difference_for_day(
    day: date,
    ids: tuple[str, ...],
    outcomes: Mapping[str, _ResolvedDay | _FailedDay],
    *,
    field: CalendarAxisDifferenceField,
    values: Mapping[str, object],
    error_code: str | None = None,
) -> CalendarAxisDifference:
    reference_id = ids[0]
    reference = values.get(reference_id)
    actual_id = next((cid for cid in ids if values.get(cid) != reference), reference_id)
    actual = values.get(actual_id)
    return CalendarAxisDifference(
        date=day,
        calendar_id=actual_id,
        field=field,
        actual_value=_canonical_json(actual) if actual is not None else "missing",
        expected_value=_canonical_json(reference) if reference is not None else "missing",
        values_by_calendar=values,
        definition_versions_by_calendar={cid: getattr(getattr(outcomes[cid], "definition", None), "definition_version", None) for cid in ids},
        definition_fact_ids_by_calendar={cid: str(getattr(getattr(outcomes[cid], "definition", None), "fact_id", "")) if getattr(getattr(outcomes[cid], "definition", None), "fact_id", None) else None for cid in ids},
        selected_fact_ids_by_calendar={cid: str(getattr(getattr(outcomes[cid], "fact", None), "fact_id", "")) if getattr(getattr(outcomes[cid], "fact", None), "fact_id", None) else None for cid in ids},
        fact_versions_by_calendar={cid: getattr(getattr(outcomes[cid], "fact", None), "fact_version", None) for cid in ids},
        source_revisions_by_calendar={cid: getattr(getattr(outcomes[cid], "fact", None), "source_revision", None) for cid in ids},
        reference_calendar_id=reference_id,
        error_code=(
            error_code
            or next(
                (
                    getattr(outcomes[cid], "error_code", None)
                    for cid in ids
                    if getattr(outcomes[cid], "error_code", None) is not None
                ),
                None,
            )
        ),
    )


def _resolve_axis_with_outcomes(
    provider: CalendarAxisDataProvider,
    *,
    start: date,
    end: date,
    ids: tuple[str, ...],
    context: CalendarPITContext | None = None,
    preloaded_outcomes: Mapping[tuple[str, date], _ResolvedDay | _FailedDay] | None = None,
) -> tuple[CalendarAxisResolution, dict[tuple[str, date], _ResolvedDay | _FailedDay]]:
    differences: list[CalendarAxisDifference] = []
    resolved_days: list[tuple[date, bool, str | None, tuple[SessionWindow, ...]]] = []
    outcomes_by_day: dict[tuple[str, date], _ResolvedDay | _FailedDay] = {}
    registry_failures: dict[str, _FailedDay] = {}
    if context is not None:
        registry_method = getattr(provider, "resolve_registry", None)
        registry_rows_method = getattr(provider, "registries", None)
        for cid in ids:
            try:
                if callable(registry_method):
                    registry = registry_method(cid, effective_day=start, pit_context=context)
                    if getattr(registry, "timezone", CALENDAR_TIMEZONE_ASIA_SHANGHAI) != CALENDAR_TIMEZONE_ASIA_SHANGHAI:
                        raise CalendarTimezoneUnsupportedError("calendar registry timezone is unsupported")
                elif callable(registry_rows_method):
                    rows = tuple(registry_rows_method(cid))
                    if not rows:
                        raise CalendarRegistryFactMissingError("calendar registry fact is missing")
                    selected = select_pit_candidate(rows, effective_day=start, pit_context=context, source_priorities=tuple(getattr(provider, "source_priorities", lambda: ())()), missing_code="calendar_registry_fact_missing", ambiguous_code="calendar_registry_ambiguous")
                    if getattr(selected, "timezone", CALENDAR_TIMEZONE_ASIA_SHANGHAI) != CALENDAR_TIMEZONE_ASIA_SHANGHAI:
                        raise CalendarTimezoneUnsupportedError("calendar registry timezone is unsupported")
                else:
                    raise CalendarRegistryFactMissingError("strict calendar provider exposes no registry facts")
            except CalendarContractError as exc:
                registry_failures[cid] = _FailedDay(CalendarAxisDifferenceField.REGISTRY, getattr(exc, "code", "calendar_registry_fact_missing"), "accepted registry fact")
    for day in _iterate_days(start, end):
        if context is not None:
            context.require_date(day, "calendar date")
            outcomes = {
                cid: registry_failures[cid]
                if cid in registry_failures
                else (
                    preloaded_outcomes[(cid, day)]
                    if preloaded_outcomes is not None and (cid, day) in preloaded_outcomes
                    else _resolve_calendar_day_modern(provider, cid, day, context)
                )
                for cid in ids
            }
        else:
            outcomes = {
                cid: (
                    preloaded_outcomes[(cid, day)]
                    if preloaded_outcomes is not None and (cid, day) in preloaded_outcomes
                    else _resolve_calendar_day_legacy(provider, cid, day)
                )
                for cid in ids
            }
        outcomes_by_day.update({(cid, day): outcome for cid, outcome in outcomes.items()})
        failed = {cid: outcome for cid, outcome in outcomes.items() if isinstance(outcome, _FailedDay)}
        if failed:
            for cid, outcome in failed.items():
                values = {item: ("missing" if item not in outcomes else _failed_or_resolved_value(outcomes[item])) for item in ids}
                differences.append(_difference_for_day(day, ids, outcomes, field=outcome.field, values=values))
            continue
        values = {cid: _resolved_value(outcomes[cid]) for cid in ids}
        if len({outcome.is_open for outcome in outcomes.values() if isinstance(outcome, _ResolvedDay)}) > 1:
            differences.append(_difference_for_day(day, ids, outcomes, field=CalendarAxisDifferenceField.IS_OPEN, values={cid: getattr(outcomes[cid], "is_open", None) for cid in ids}))
            continue
        # Legacy fixtures historically ignored closed-day overrides; retain
        # that compatibility only when no PIT context is supplied.  Strict
        # task-11 snapshots check the local timezone on every natural day.
        if context is None and all(isinstance(outcome, _ResolvedDay) and not outcome.is_open for outcome in outcomes.values()):
            resolved_days.append((day, False, None, ()))
            continue
        # Timezone is checked before the closed-day fast path: definitions
        # and overrides still establish the common local-date basis.
        zones = {outcome.timezone for outcome in outcomes.values() if isinstance(outcome, _ResolvedDay)}
        if len(zones) != 1:
            differences.append(
                _difference_for_day(
                    day,
                    ids,
                    outcomes,
                    field=CalendarAxisDifferenceField.TIMEZONE,
                    values={cid: getattr(outcomes[cid], "timezone", None) for cid in ids},
                    error_code="calendar_timezone_mismatch",
                )
            )
            continue
        if context is not None and next(iter(zones)) != CALENDAR_TIMEZONE_ASIA_SHANGHAI:
            # A single unsupported timezone is a policy failure.  If the
            # same calendar uses multiple timezones, the post-pass below
            # replaces these per-day failures with the more precise
            # ``calendar_timezone_inconsistent`` evidence.
            differences.append(
                _difference_for_day(
                    day,
                    ids,
                    outcomes,
                    field=CalendarAxisDifferenceField.TIMEZONE,
                    values={cid: getattr(outcomes[cid], "timezone", None) for cid in ids},
                    error_code="calendar_timezone_unsupported",
                )
            )
            continue
        if all(isinstance(outcome, _ResolvedDay) and not outcome.is_open for outcome in outcomes.values()):
            resolved_days.append((day, False, next(iter(zones)), ()))
            continue
        # All open: compare complete business windows and timezone.  Labels
        # are absent from _business_sessions and therefore never alter strict
        # compatibility.
        sessions_values = {cid: _business_sessions(outcomes[cid].sessions) for cid in ids}
        if len({_canonical_json(value) for value in sessions_values.values()}) != 1:
            differences.append(_difference_for_day(day, ids, outcomes, field=CalendarAxisDifferenceField.SESSIONS, values=sessions_values))
            continue
        first = outcomes[ids[0]]
        assert isinstance(first, _ResolvedDay)
        resolved_days.append((day, True, first.timezone, first.sessions))
    # Timezone consistency applies to every covered natural day, including
    # closed days.  A closed day does not generate a session point, but its
    # definition/override still participates in the calendar timezone proof.
    # The per-day loop above intentionally defers this cross-date check until
    # all outcomes are known, so a changing timezone is not misreported as a
    # generic unsupported-timezone error.
    inconsistent_calendars: set[str] = set()
    for cid in ids:
        timezone_by_day = {
            day: outcome.timezone
            for (calendar_id, day), outcome in outcomes_by_day.items()
            if calendar_id == cid and isinstance(outcome, _ResolvedDay) and outcome.timezone is not None
        }
        if len(set(timezone_by_day.values())) > 1:
            inconsistent_calendars.add(cid)
            first_timezone = next(iter(timezone_by_day.values()))
            first_day = next(
                day
                for day in sorted(timezone_by_day)
                if timezone_by_day[day] != first_timezone
            )
            values = {
                other_cid: getattr(
                    outcomes_by_day.get((other_cid, first_day)),
                    "timezone",
                    None,
                )
                for other_cid in ids
            }
            outcomes = {
                other_cid: outcomes_by_day.get(
                    (other_cid, first_day),
                    _FailedDay(
                        CalendarAxisDifferenceField.TIMEZONE,
                        "missing",
                        "timezone",
                    ),
                )
                for other_cid in ids
            }
            differences.append(
                CalendarAxisDifference(
                    date=first_day,
                    calendar_id=cid,
                    field=CalendarAxisDifferenceField.TIMEZONE,
                    actual_value=_canonical_json(timezone_by_day[first_day]),
                    expected_value=_canonical_json(first_timezone),
                    values_by_calendar=values,
                    definition_versions_by_calendar={
                        other_cid: getattr(
                            getattr(outcomes[other_cid], "definition", None),
                            "definition_version",
                            None,
                        )
                        for other_cid in ids
                    },
                    definition_fact_ids_by_calendar={
                        other_cid: str(
                            getattr(
                                getattr(outcomes[other_cid], "definition", None),
                                "fact_id",
                                "",
                            )
                        )
                        if getattr(
                            getattr(outcomes[other_cid], "definition", None),
                            "fact_id",
                            None,
                        )
                        else None
                        for other_cid in ids
                    },
                    selected_fact_ids_by_calendar={
                        other_cid: str(
                            getattr(
                                getattr(outcomes[other_cid], "fact", None),
                                "fact_id",
                                "",
                            )
                        )
                        if getattr(
                            getattr(outcomes[other_cid], "fact", None),
                            "fact_id",
                            None,
                        )
                        else None
                        for other_cid in ids
                    },
                    fact_versions_by_calendar={
                        other_cid: getattr(
                            getattr(outcomes[other_cid], "fact", None),
                            "fact_version",
                            None,
                        )
                        for other_cid in ids
                    },
                    source_revisions_by_calendar={
                        other_cid: getattr(
                            getattr(outcomes[other_cid], "fact", None),
                            "source_revision",
                            None,
                        )
                        for other_cid in ids
                    },
                    reference_calendar_id=cid,
                    error_code="calendar_timezone_inconsistent",
                )
            )
    if inconsistent_calendars:
        # Unsupported-timezone rows for a calendar that changed timezone are
        # secondary symptoms; keep only the precise cross-date evidence.  A
        # different calendar that is uniformly unsupported still retains its
        # own ``calendar_timezone_unsupported`` rows.
        differences = [
            difference
            for difference in differences
            if not (
                difference.error_code == "calendar_timezone_unsupported"
                and difference.calendar_id in inconsistent_calendars
            )
        ]
    unique_differences: dict[tuple[str, str, str, str, str], CalendarAxisDifference] = {
        difference.sort_key: difference for difference in differences
    }
    ordered_diffs = tuple(unique_differences[key] for key in sorted(unique_differences))
    selected_facts, resolved_definitions = _provenance_maps(ids, outcomes_by_day)
    if ordered_diffs:
        resolution = CalendarAxisResolution(
            POLICY_KEY_STRICT_COMPATIBLE,
            POLICY_VERSION_STRICT_COMPATIBLE,
            start,
            end,
            ids,
            "",
            None,
            (),
            CalendarAxisStatus.INCOMPATIBLE,
            ordered_diffs,
            pit_context=context,
            selected_facts=selected_facts,
            resolved_calendar_definitions=resolved_definitions,
        )
        return resolution, outcomes_by_day
    resolved_sessions = tuple(SessionPoint(day, day.isoformat(), tz, windows) for day, opened, tz, windows in resolved_days if opened)
    timezone_values = {point.timezone for point in resolved_sessions}
    records = [_canonical_day_record(day, opened, tz, windows) for day, opened, tz, windows in resolved_days]
    signature = canonical_hash({"policy": {"key": POLICY_KEY_STRICT_COMPATIBLE, "version": 1}, "calendar_ids": ids, "days": records})
    resolution = CalendarAxisResolution(
        POLICY_KEY_STRICT_COMPATIBLE,
        POLICY_VERSION_STRICT_COMPATIBLE,
        start,
        end,
        ids,
        signature,
        next(iter(timezone_values)) if len(timezone_values) == 1 else None,
        resolved_sessions,
        CalendarAxisStatus.COMPATIBLE,
        (),
        pit_context=context,
        selected_facts=selected_facts,
        resolved_calendar_definitions=resolved_definitions,
        calendar_semantic_signature=signature,
    )
    return resolution, outcomes_by_day


def _provenance_maps(
    ids: Sequence[str],
    outcomes: Mapping[tuple[str, date], _ResolvedDay | _FailedDay],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """Project selected fact/definition evidence without exposing mutable rows."""

    selected: dict[str, dict[str, object]] = {}
    definitions: dict[str, dict[str, object]] = {}
    for (calendar_id, day), outcome in sorted(outcomes.items(), key=lambda item: (item[0][0], item[0][1])):
        fact = getattr(outcome, "fact", None)
        definition = getattr(outcome, "definition", None)
        key = f"{calendar_id}:{day.isoformat()}"
        selected[key] = {
            "calendar_id": calendar_id,
            "date": day,
            "selected_fact_id": getattr(fact, "fact_id", None),
            "fact_version": getattr(fact, "fact_version", None),
            "logical_fact_key": getattr(fact, "logical_fact_key", None),
            "source": getattr(fact, "source", None),
            "source_priority_fact_id": getattr(fact, "source_priority_fact_id", None),
            "source_priority_version": getattr(fact, "source_priority_version", None),
            "source_priority": getattr(fact, "source_priority", None),
            "source_revision_order": getattr(fact, "source_revision_order", None),
            "source_revision": getattr(fact, "source_revision", None),
            "bootstrap_seed_id": getattr(fact, "bootstrap_seed_id", None),
            "bootstrap_seed_version": getattr(fact, "bootstrap_seed_version", None),
            "bootstrap_seed_hash": getattr(fact, "bootstrap_seed_hash", None),
            "quality_status": getattr(getattr(fact, "quality_status", None), "value", getattr(fact, "quality_status", None)),
            "content_hash": getattr(fact, "content_hash", None),
            "valid_from": getattr(fact, "valid_from", None),
            "valid_to": getattr(fact, "valid_to", None),
            "knowledge_from": getattr(fact, "knowledge_from", None),
            "knowledge_to": getattr(fact, "knowledge_to", None),
            "known_at": getattr(fact, "known_at", None),
            "knowledge_as_of": getattr(fact, "knowledge_as_of", None),
            "registry_fact_id": getattr(fact, "registry_fact_id", None),
            "registry_version": getattr(fact, "registry_version", None),
            "missing_reason": getattr(outcome, "actual_value", None) if fact is None else None,
        }
        definitions[key] = {
            "calendar_id": calendar_id,
            "date": day,
            "definition_fact_id": getattr(definition, "fact_id", None),
            "definition_version": getattr(definition, "definition_version", None),
            "fact_version": getattr(definition, "fact_version", None),
            "logical_fact_key": getattr(definition, "logical_fact_key", None),
            "source": getattr(definition, "source", None),
            "source_priority_fact_id": getattr(definition, "source_priority_fact_id", None),
            "source_priority_version": getattr(definition, "source_priority_version", None),
            "source_priority": getattr(definition, "source_priority", None),
            "source_revision_order": getattr(definition, "source_revision_order", None),
            "source_revision": getattr(definition, "source_revision", None),
            "bootstrap_seed_id": getattr(definition, "bootstrap_seed_id", None),
            "bootstrap_seed_version": getattr(definition, "bootstrap_seed_version", None),
            "bootstrap_seed_hash": getattr(definition, "bootstrap_seed_hash", None),
            "quality_status": getattr(getattr(definition, "quality_status", None), "value", getattr(definition, "quality_status", None)),
            "content_hash": getattr(definition, "content_hash", None),
            "valid_from": getattr(definition, "valid_from", None),
            "valid_to": getattr(definition, "valid_to", None),
            "knowledge_from": getattr(definition, "knowledge_from", None),
            "knowledge_to": getattr(definition, "knowledge_to", None),
            "known_at": getattr(definition, "known_at", None),
            "knowledge_as_of": getattr(definition, "knowledge_as_of", None),
            "registry_fact_id": getattr(definition, "registry_fact_id", None),
            "registry_version": getattr(definition, "registry_version", None),
        }
    return selected, definitions


def _business_sessions(sessions: Sequence[SessionWindow]) -> list[dict[str, object]]:
    return [window.semantic_payload() for window in sessions]


def _resolved_value(outcome: _ResolvedDay | _FailedDay) -> object:
    if isinstance(outcome, _FailedDay):
        return {"missing": outcome.actual_value or "invalid"}
    return {"is_open": outcome.is_open, "timezone": outcome.timezone, "sessions": _business_sessions(outcome.sessions)}


def _failed_or_resolved_value(outcome: _ResolvedDay | _FailedDay) -> object:
    return _resolved_value(outcome)


def resolve_strict_compatible_axis(
    provider: CalendarAxisDataProvider,
    *,
    start_date: date,
    end_date: date,
    calendar_ids: Sequence[str],
    query_boundary: object | None = None,
    pit_context: CalendarPITContext | None = None,
) -> CalendarAxisResolution:
    """Resolve ``strict_compatible@1`` with optional strict PIT context."""

    start, end = _plain_date(start_date, "start_date"), _plain_date(end_date, "end_date")
    if start > end:
        raise CalendarDomainError("start_date must not be later than end_date")
    if (end - start).days + 1 > MAX_FORMAL_DATE_SPAN:
        raise CalendarDateSpanLimitExceededError("formal date span exceeds 10,000 natural days")
    ids = tuple(sorted({normalize_calendar_id(item) for item in calendar_ids}))
    if not ids:
        raise CalendarIdSetEmptyError("calendar_ids must not be empty")
    if len(ids) > MAX_CALENDAR_IDS:
        raise CalendarPreflightResourceLimitExceededError("calendar_id set exceeds 32", details={"observed": len(ids), "limit": MAX_CALENDAR_IDS})
    context = pit_context
    if context is None and query_boundary is not None:
        context = CalendarPITContext.from_query_boundary(query_boundary, CALENDAR_TIMEZONE_ASIA_SHANGHAI)
    if context is None and query_boundary is None:
        # Legacy direct fixtures without canonical registry facts remain
        # usable for diagnostics.  Named-calendar providers carrying registry
        # facts, and the SQL provider, must never read through the UTC-date
        # resolver without the sole QueryBoundary authority.
        provider_name = provider.__class__.__name__
        has_canonical_rows = (
            isinstance(provider, InMemoryCalendarAxisDataProvider)
            and (
                bool(provider._registries)
                or any(
                    getattr(row, "registry_fact_id", None) is not None
                    or getattr(row, "known_at", None) is not None
                    for row in (*provider._definitions, *provider._facts)
                )
            )
        )
        if has_canonical_rows or provider_name == "SqlCalendarAxisDataProvider":
            raise DataCutoffRequiredError(
                "strict calendar axis requires query_boundary.data_cutoff"
            )
    # Block a request that reaches beyond the frozen local cutoff before
    # touching registry/definition/fact providers.  This keeps the strict
    # read-before-access contract even when a caller supplies a pre-derived
    # PIT context directly.
    if context is not None:
        context.require_date(end, "end_date")
    resolution, _ = _resolve_axis_with_outcomes(provider, start=start, end=end, ids=ids, context=context)
    if resolution.status is CalendarAxisStatus.COMPATIBLE:
        # A single calendar with no open days is structurally compatible, but
        # formal preflight maps this to NO_FORMAL_SESSIONS.  The axis object
        # remains a valid semantic result with an empty sequence.
        return resolution
    return resolution


def resolve_calendar_axis(provider: CalendarAxisDataProvider, *, policy_key: str, policy_version: str, start_date: date, end_date: date, calendar_ids: Sequence[str], query_boundary: object | None = None, pit_context: CalendarPITContext | None = None) -> CalendarAxisResolution:
    if (policy_key, str(policy_version)) != (POLICY_KEY_STRICT_COMPATIBLE, POLICY_VERSION_STRICT_COMPATIBLE):
        raise CalendarDomainError(f"unknown calendar axis policy: {policy_key}@{policy_version}")
    return resolve_strict_compatible_axis(provider, start_date=start_date, end_date=end_date, calendar_ids=calendar_ids, query_boundary=query_boundary, pit_context=pit_context)


def register_calendar_axis_policy(policy_key: str, policy_version: str, resolver: Callable[..., CalendarAxisResolution]) -> None:
    if (policy_key, str(policy_version)) in _POLICIES:
        raise CalendarDomainError(f"calendar axis policy already registered: {policy_key}@{policy_version}")
    _POLICIES[(policy_key, str(policy_version))] = resolver


_POLICIES: dict[tuple[str, str], Callable[..., CalendarAxisResolution]] = {
    (POLICY_KEY_STRICT_COMPATIBLE, POLICY_VERSION_STRICT_COMPATIBLE): resolve_strict_compatible_axis,
}


def _iterate_days(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=index) for index in range((end - start).days + 1))


def _snapshot_request_from_object(
    request: object,
    *,
    query_boundary: object | None,
) -> CalendarSnapshotRequest:
    """Convert one legacy facade object without creating a second PIT source."""

    ids = (
        getattr(request, "resolved_calendar_ids", None)
        or getattr(request, "calendar_ids", None)
        or ()
    )
    requested_window = getattr(request, "requested_window", None)
    start = getattr(requested_window, "start_date", None) or getattr(
        request, "formal_start", None
    )
    end = getattr(requested_window, "end_date", None) or getattr(
        request, "formal_end", None
    )
    object_boundary = getattr(request, "query_boundary", None)
    if (
        query_boundary is not None
        and object_boundary is not None
        and query_boundary != object_boundary
    ):
        raise InvalidDataRequestError(
            "legacy snapshot boundary and query_boundary must be equal"
        )
    boundary = query_boundary if query_boundary is not None else object_boundary
    package = getattr(request, "rule_package", None)
    return CalendarSnapshotRequest(
        tuple(ids),
        start,
        end,
        getattr(request, "warmup_sessions", 0),
        boundary,
        getattr(request, "instrument_ids", ()),
        provider_key=getattr(request, "provider_key", None),
        package_key=getattr(request, "package_key", None) or getattr(package, "key", None),
        package_version=getattr(request, "package_version", None)
        if getattr(request, "package_version", None) is not None
        else getattr(package, "version", None),
    )


def _materialize_calendar_index(
    provider: InMemoryCalendarAxisDataProvider,
    ids: tuple[str, ...],
    start: date,
    end_exclusive: date,
    context: CalendarPITContext,
) -> dict[date, dict[str, _ResolvedDay | _FailedDay]]:
    """Materialize one detached natural-day index for a snapshot attempt."""

    return {
        day: {
            cid: _resolve_calendar_day_modern(provider, cid, day, context)
            for cid in ids
        }
        for day in _iterate_days(start, end_exclusive - timedelta(days=1))
    }


def _selected_snapshot_definitions(
    provider: object,
    ids: Sequence[str],
    resolution: CalendarAxisResolution,
    start: date,
    end_exclusive: date,
    context: CalendarPITContext,
) -> tuple[CalendarDefinition, ...]:
    """Expose only definitions selected by the frozen envelope resolution."""

    selected_ids = {
        evidence.get("definition_fact_id")
        for evidence in resolution.resolved_calendar_definitions.values()
        if isinstance(evidence, Mapping) and evidence.get("definition_fact_id") is not None
    }
    if not selected_ids:
        return ()
    cutoff = context.data_cutoff.astimezone(timezone.utc)
    selected: list[CalendarDefinition] = []
    seen: set[UUID] = set()
    for calendar_id in ids:
        definitions = getattr(provider, "definitions")(calendar_id)
        for definition in definitions:
            if definition.fact_id not in selected_ids or definition.fact_id in seen:
                continue
            if (
                definition.valid_from is None
                or definition.valid_from >= end_exclusive
                or (definition.valid_to is not None and definition.valid_to <= start)
            ):
                continue
            if definition.quality_status not in {
                CalendarQualityStatus.ACCEPTED,
                CalendarQualityStatus.ACCEPTED.value,
            }:
                continue
            known_at = definition.known_at
            if (
                not isinstance(known_at, datetime)
                or known_at.tzinfo is None
                or known_at.utcoffset() is None
                or known_at.astimezone(timezone.utc) > cutoff
            ):
                continue
            if context.knowledge_as_of is not None:
                try:
                    if not _candidate_knowledge_visible(definition, context):
                        continue
                except CalendarContractError:
                    continue
            selected.append(definition)
            if isinstance(definition.fact_id, UUID):
                seen.add(definition.fact_id)
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.calendar_id,
                item.definition_version,
                item.fact_version,
                str(item.fact_id),
            ),
        )
    )


def _select_calendar_fact_metadata(
    provider: InMemoryCalendarAxisDataProvider,
    calendar_id: str,
    day: date,
    context: CalendarPITContext,
) -> _CalendarIndexMetadata:
    """Select a fact identity using metadata only.

    In-memory strict snapshots mirror SQL's resolution-head prepare query.  A
    candidate's business payload (definition, timezone override, or session
    windows) is intentionally never inspected here; those values are
    materialized by ``_materialize_calendar_index`` only after the one batch
    read boundary.  Ambiguous payloads are therefore left for that resolver
    to reject after alignment.
    """

    candidates = tuple(provider._fact_index.get((calendar_id, day), ()))
    if not candidates:
        return _CalendarIndexMetadata(None, None, None, complete=False, error_code="calendar_fact_missing")
    visible: list[CalendarSessionFact] = []
    for candidate in candidates:
        if not candidate.applies_to(day):
            continue
        if candidate.quality_status is not CalendarQualityStatus.ACCEPTED:
            continue
        if _candidate_knowledge_visible(candidate, context):
            visible.append(candidate)
    if not visible:
        return _CalendarIndexMetadata(None, None, None, complete=False, error_code="calendar_fact_missing")

    # Reproduce the strict PIT rank without looking at ``semantic_payload``.
    # Source-priority rows contain only provenance/ordering metadata and are
    # safe to consume in prepare.
    try:
        priorities = {
            getattr(item, "source", None): _select_source_priority(
                getattr(item, "source", None),
                provider._source_priorities,
                day=day,
                context=context,
            )
            for item in visible
        }
        _validate_revision_chain(visible)
        ranks: list[tuple[tuple[int, timedelta, int], CalendarSessionFact]] = []
        for item in visible:
            priority = priorities[item.source]
            if item.source_priority_fact_id != priority.fact_id:
                raise CalendarSourceRevisionConflictError(
                    "fact source priority reference does not match selected priority fact"
                )
            if item.source_priority_version != priority.source_priority_version:
                raise CalendarSourceRevisionConflictError(
                    "fact source priority version does not match selected priority fact"
                )
            if item.source_priority != priority.source_priority:
                raise CalendarSourceRevisionConflictError(
                    "fact source priority value is not registry-backed"
                )
            if item.source_revision_order != priority.source_revision_order:
                raise CalendarSourceRevisionConflictError(
                    "fact source revision order is not registry-backed"
                )
            if item.known_at is None:
                raise CalendarPitMetadataMissingError("calendar candidate known_at is missing")
            ranks.append(
                (
                    (
                        priority.source_priority,
                        datetime.max.replace(tzinfo=timezone.utc)
                        - item.known_at.astimezone(timezone.utc),
                        -item.source_revision_order,
                    ),
                    item,
                )
            )
        best_rank = min(rank for rank, _ in ranks)
        leaders = [item for rank, item in ranks if rank == best_rank]
        if len({priorities[item.source].source_priority_version for item in leaders}) > 1:
            raise CalendarSourceRevisionConflictError(
                "same-rank candidates use different source-priority versions"
            )
        for source in {item.source for item in leaders}:
            revisions = {item.source_revision for item in leaders if item.source == source}
            if len(revisions) > 1:
                raise CalendarSourceRevisionConflictError(
                    "same source has multiple revisions at the selected PIT rank"
                )
        selected = min(
            leaders,
            key=lambda item: (-item.fact_version, _candidate_uuid_bytes(item)),
        )
    except CalendarContractError as exc:
        return _CalendarIndexMetadata(
            None,
            None,
            None,
            complete=False,
            error_code=getattr(exc, "code", "calendar_fact_invalid"),
        )
    return _CalendarIndexMetadata(
        bool(selected.is_open),
        selected.fact_id,
        selected.fact_version,
        complete=True,
    )


def _materialize_calendar_metadata_index(
    provider: InMemoryCalendarAxisDataProvider,
    ids: tuple[str, ...],
    start: date,
    end_exclusive: date,
    context: CalendarPITContext,
) -> dict[date, dict[str, _CalendarIndexMetadata]]:
    """Build the prepare-only metadata index without parsing payloads."""

    return {
        day: {
            cid: _select_calendar_fact_metadata(provider, cid, day, context)
            for cid in ids
        }
        for day in _iterate_days(start, end_exclusive - timedelta(days=1))
    }


def _assert_metadata_index_alignment(
    metadata_index: Mapping[date, Mapping[str, _CalendarIndexMetadata]],
    loaded_index: Mapping[date, Mapping[str, _ResolvedDay | _FailedDay]],
    ids: Sequence[str],
    *,
    start: date,
    end_exclusive: date,
) -> None:
    """Ensure batch-selected fact identities match the prepare metadata.

    A complete metadata cell must still be present in the payload batch with
    the same fact UUID/version and open flag.  Incomplete cells remain
    evidence of an unresolved date; a later full resolver is responsible for
    reporting the precise missing-definition/fact issue rather than turning a
    stable absence into a false revision change.
    """

    for day, outcomes in metadata_index.items():
        if day < start or day >= end_exclusive:
            continue
        for cid in ids:
            expected = outcomes.get(cid)
            if not isinstance(expected, _CalendarIndexMetadata) or not expected.complete:
                continue
            actual = loaded_index.get(day, {}).get(cid)
            fact = getattr(actual, "fact", None)
            if fact is None:
                raise CalendarSnapshotRevisionChangedError(
                    "batch read no longer covers the prepare metadata cell"
                )
            if (
                getattr(fact, "fact_id", None) != expected.selected_fact_id
                or getattr(fact, "fact_version", None) != expected.fact_version
                or bool(getattr(fact, "is_open", False)) != bool(expected.is_open)
            ):
                raise CalendarSnapshotRevisionChangedError(
                    "batch read selected fact differs from prepare metadata"
                )


def _index_entry_complete(value: object) -> bool:
    """Return whether a metadata-only index cell proves one natural day."""

    return isinstance(value, _ResolvedDay) or bool(getattr(value, "complete", False))


def _index_entry_is_open(value: object) -> bool:
    """Read only the open flag exposed by a prepare-stage index."""

    if isinstance(value, _ResolvedDay):
        return value.is_open
    return getattr(value, "is_open", None) is True


def _find_common_anchor(
    index: Mapping[date, Mapping[str, object]],
    ids: tuple[str, ...],
    formal_days: Sequence[date],
) -> date | None:
    for day in formal_days:
        outcomes = index.get(day, {})
        if all(
            _index_entry_complete(outcomes.get(cid))
            and _index_entry_is_open(outcomes.get(cid))
            for cid in ids
        ):
            return day
    return None


def _find_common_open_history(
    index: Mapping[date, Mapping[str, object]],
    ids: tuple[str, ...],
    anchor: date,
    requested_sessions: int,
) -> list[date]:
    """Return open dates from the one gap-free common segment before anchor.

    Both full resolver outcomes and metadata-only SQL index cells are accepted;
    incomplete cells always break the segment and cannot be treated as closed.
    """

    days = sorted(day for day in index if day < anchor)
    if not days:
        return []
    segments: list[list[date]] = [[]]
    previous: date | None = None
    for day in days:
        outcomes = index[day]
        complete = all(_index_entry_complete(outcomes.get(cid)) for cid in ids)
        if not complete or (previous is not None and day != previous + timedelta(days=1)):
            if segments[-1]:
                segments.append([])
        else:
            segments[-1].append(day)
        previous = day
    segment = segments[-1] if segments and segments[-1] else []
    # A history segment is usable only when it reaches the natural day
    # immediately preceding the formal anchor.  Without this endpoint check,
    # a missing tail (for example facts ending on ``anchor - 3``) could be
    # mistaken for contiguous warmup history merely because the earlier rows
    # themselves had no internal gaps.
    if not segment or segment[-1] != anchor - timedelta(days=1):
        return []
    open_dates = [
        day
        for day in segment
        if all(_index_entry_is_open(index[day].get(cid)) for cid in ids)
    ]
    if requested_sessions <= 0:
        return []
    return open_dates[-requested_sessions:]


def _resolve_snapshot_range(
    provider: InMemoryCalendarAxisDataProvider,
    ids: tuple[str, ...],
    formal_start: date,
    formal_end: date,
    envelope_start: date,
    envelope_end: date,
    context: CalendarPITContext,
    instrument_ids: tuple[UUID, ...],
    index: Mapping[date, Mapping[str, _ResolvedDay | _FailedDay]] | None = None,
    warmup_count: int = 0,
) -> tuple[CalendarAxisResolution, tuple[SessionPoint, ...]]:
    # If formal days are compatible but empty, preserve an evidence-only
    # compatible resolution here; the session/preflight layer emits the
    # NO_FORMAL_SESSIONS issue and will not construct a runnable axis.
    index = index or _materialize_calendar_index(provider, ids, formal_start, envelope_end, context)
    formal_outcomes = {
        (cid, day): index[day][cid]
        for day in _iterate_days(formal_start, formal_end)
        for cid in ids
        if day in index and cid in index[day]
    }
    formal_resolved, _ = _resolve_axis_with_outcomes(
        provider,
        start=formal_start,
        end=formal_end,
        ids=ids,
        context=context,
        preloaded_outcomes=formal_outcomes,
    )
    resolution = formal_resolved
    outcomes = formal_outcomes
    all_outcomes: dict[tuple[str, date], _ResolvedDay | _FailedDay] = dict(outcomes)
    for day in _iterate_days(envelope_start, formal_start - timedelta(days=1)):
        for cid in ids:
            if day in index and cid in index[day]:
                all_outcomes[(cid, day)] = index[day][cid]
    warmup_points: list[SessionPoint] = []
    common_history = []
    for day in _iterate_days(envelope_start, formal_start - timedelta(days=1)):
        day_outcomes = [all_outcomes[(cid, day)] for cid in ids]
        if all(isinstance(item, _ResolvedDay) and item.is_open for item in day_outcomes):
            first = day_outcomes[0]
            assert isinstance(first, _ResolvedDay)
            common_history.append(SessionPoint(day, day.isoformat(), first.timezone, first.sessions))
    # Only the requested number of points immediately preceding the formal
    # anchor belongs to warmup.  The complete envelope remains available for
    # coverage/usage evidence, but it must never leak extra history sessions
    # into the formal session object.
    selected_history = common_history[-warmup_count:] if warmup_count > 0 else []
    # Add warmup-day provenance to the same resolution envelope.  This keeps
    # formal and warmup usage auditable from one immutable snapshot rather than
    # silently dropping the history rows from the report.
    selected_facts, resolved_definitions = _provenance_maps(ids, all_outcomes)
    resolution = CalendarAxisResolution(
        policy_key=resolution.policy_key,
        policy_version=resolution.policy_version,
        start_date=resolution.start_date,
        end_date=resolution.end_date,
        calendar_ids=resolution.calendar_ids,
        session_signature=resolution.session_signature,
        timezone=resolution.timezone,
        resolved_sessions=resolution.resolved_sessions,
        status=resolution.status,
        differences=resolution.differences,
        pit_context=resolution.pit_context,
        selected_facts=selected_facts,
        resolved_calendar_definitions=resolved_definitions,
        calendar_semantic_signature=resolution.calendar_semantic_signature,
        calendar_revision_digest=resolution.calendar_revision_digest,
        warmup_sessions=resolution.warmup_sessions,
        warmup_session_signature=resolution.warmup_session_signature,
        coverage_envelope=resolution.coverage_envelope,
        non_strict_pit_capabilities=resolution.non_strict_pit_capabilities,
        non_strict_pit=resolution.non_strict_pit,
    )
    warmup_points.extend(selected_history)
    return resolution, tuple(warmup_points)


def _open_index_payload(
    index: Mapping[date, Mapping[str, _ResolvedDay | _FailedDay]],
    ids: Sequence[str],
    *,
    start: date | None = None,
    end_exclusive: date | None = None,
) -> dict[str, object]:
    """Serialize the prepare-stage open-session index without fact objects.

    ``start``/``end_exclusive`` are optional for compatibility with callers
    that need the complete prepare search index.  Snapshot evidence must pass
    the selected envelope so its payload is bounded to the immutable range.
    """

    rows: list[dict[str, object]] = []
    for day in sorted(index):
        if start is not None and day < start:
            continue
        if end_exclusive is not None and day >= end_exclusive:
            continue
        outcomes = index[day]
        rows.append({
            "date": day.isoformat(),
            "is_open_by_calendar": {
                cid: (
                    outcome.is_open
                    if isinstance(outcome, _ResolvedDay)
                    else None
                )
                for cid in ids
                for outcome in (outcomes.get(cid),)
            },
            "selected_fact_ids": {
                cid: (
                    str(outcomes[cid].fact.fact_id)
                    if isinstance(outcomes.get(cid), _ResolvedDay)
                    else None
                )
                for cid in ids
            },
            "fact_versions": {
                cid: (
                    outcomes[cid].fact.fact_version
                    if isinstance(outcomes.get(cid), _ResolvedDay)
                    else None
                )
                for cid in ids
            },
        })
    return {"dates": rows}


def _coverage_payload(
    provider: InMemoryCalendarAxisDataProvider,
    ids: tuple[str, ...],
    start: date,
    end_exclusive: date,
    context: CalendarPITContext | None = None,
    *,
    index: Mapping[date, Mapping[str, _ResolvedDay | _FailedDay]] | None = None,
) -> dict[str, object]:
    """Build coverage from every natural day in the detached envelope.

    A range is continuous only when each natural day has one selectable,
    accepted fact and definition.  Restricting the computation to the actual
    envelope prevents a fact's first/last row from masquerading as a proof of
    coverage outside the batch and makes internal gaps explicit.
    """

    by_calendar: dict[str, object] = {}
    envelope_days = tuple(_iterate_days(start, end_exclusive - timedelta(days=1)))
    for cid in ids:
        valid_dates: list[date] = []
        invalid_dates: list[date] = []
        for day in envelope_days:
            if index is not None and day in index and cid in index[day]:
                outcome = index[day][cid]
            elif context is None:
                outcome = _resolve_calendar_day_legacy(provider, cid, day)
            else:
                outcome = _resolve_calendar_day_modern(provider, cid, day, context)
            if isinstance(outcome, _ResolvedDay):
                valid_dates.append(day)
            else:
                invalid_dates.append(day)
        floor = start if valid_dates and valid_dates[0] == start else None
        ceiling = end_exclusive if valid_dates and valid_dates[-1] == end_exclusive - timedelta(days=1) else None
        # The endpoint remains the full contiguous run only when every day in
        # the envelope is valid.  Otherwise gaps retain the exact invalid
        # ranges and both endpoints are reported as unknown where unproven.
        if valid_dates:
            first_valid, last_valid = valid_dates[0], valid_dates[-1]
            floor = first_valid
            ceiling = last_valid + timedelta(days=1)
        gaps: list[tuple[str, str]] = []
        if invalid_dates:
            gap_start = previous = invalid_dates[0]
            for day in invalid_dates[1:]:
                if day != previous + timedelta(days=1):
                    gaps.append((gap_start.isoformat(), (previous + timedelta(days=1)).isoformat()))
                    gap_start = day
                previous = day
            gaps.append((gap_start.isoformat(), (previous + timedelta(days=1)).isoformat()))
        by_calendar[cid] = {"range": [floor.isoformat() if floor else None, ceiling.isoformat() if ceiling else None], "gaps": gaps}
    floors = [date.fromisoformat(value["range"][0]) for value in by_calendar.values() if value["range"][0]]
    ceilings = [date.fromisoformat(value["range"][1]) for value in by_calendar.values() if value["range"][1]]
    common_floor = max(floors) if floors else None
    common_ceiling = min(ceilings) if ceilings else None
    common_gaps: list[tuple[str, str]] = []
    if common_floor is not None and common_ceiling is not None:
        gap_days = set()
        for value in by_calendar.values():
            for gap_start, gap_end in value["gaps"]:
                cursor = date.fromisoformat(gap_start)
                gap_end_date = date.fromisoformat(gap_end)
                while cursor < gap_end_date:
                    if common_floor <= cursor < common_ceiling:
                        gap_days.add(cursor)
                    cursor += timedelta(days=1)
        for day in sorted(gap_days):
            if not common_gaps or date.fromisoformat(common_gaps[-1][1]) != day:
                common_gaps.append((day.isoformat(), (day + timedelta(days=1)).isoformat()))
            else:
                common_gaps[-1] = (common_gaps[-1][0], (day + timedelta(days=1)).isoformat())
    common_segments: list[tuple[str, str]] = []
    if common_floor is not None and common_ceiling is not None:
        cursor = common_floor
        for gap_start_text, gap_end_text in common_gaps:
            gap_start = date.fromisoformat(gap_start_text)
            gap_end = date.fromisoformat(gap_end_text)
            if cursor < gap_start:
                common_segments.append((cursor.isoformat(), gap_start.isoformat()))
            cursor = max(cursor, gap_end)
        if cursor < common_ceiling:
            common_segments.append((cursor.isoformat(), common_ceiling.isoformat()))
    return {"by_calendar": by_calendar, "common": {"floor": common_floor.isoformat() if common_floor else None, "ceiling": common_ceiling.isoformat() if common_ceiling else None, "gaps": common_gaps, "segments": common_segments}}


def _revision_payload(
    provider: InMemoryCalendarAxisDataProvider,
    ids: tuple[str, ...],
    start: date,
    end: date,
    context: CalendarPITContext,
) -> dict[str, object]:
    """Build one bounded revision payload for memory and SQL projections.

    The SQL provider only materializes accepted, cutoff-visible rows whose
    effective range intersects the snapshot envelope.  Applying the same
    predicate here keeps the in-memory and SQL watermark/fingerprint equal
    and prevents an unrelated out-of-range revision from invalidating a
    snapshot.  Historical-cognition filtering remains the selector's job;
    the physical ``data_cutoff`` is the common read upper bound.
    """

    def visible(candidate: object, *, effective_day: date | None = None) -> bool:
        valid_from = getattr(candidate, "valid_from", None)
        valid_to = getattr(candidate, "valid_to", None)
        if effective_day is None:
            if valid_from is not None and valid_from >= end:
                return False
            if valid_to is not None and valid_to <= start:
                return False
        elif valid_from is not None and not (
            valid_from <= effective_day
            and (valid_to is None or effective_day < valid_to)
        ):
            return False
        quality = getattr(candidate, "quality_status", CalendarQualityStatus.ACCEPTED)
        if quality not in {
            CalendarQualityStatus.ACCEPTED,
            CalendarQualityStatus.ACCEPTED.value,
        }:
            return False
        # Reuse the same PIT predicate as the resolver.  Filtering only by
        # ``data_cutoff`` would let rows learned after an explicit
        # ``knowledge_as_of`` contribute to the revision digest even though
        # they are invisible to the historical snapshot.
        return _candidate_knowledge_visible(candidate, context)

    rows: list[dict[str, object]] = []
    registries = [
        registry
        for registry in provider._registries
        if registry.calendar_id in ids and visible(registry)
    ]
    for registry in registries:
        rows.append(
            {
                "scope_kind": "registry",
                "calendar_id": registry.calendar_id,
                "selected_fact_id": registry.fact_id,
                "fact_version": registry.fact_version,
                "logical_fact_key": registry.logical_fact_key,
                "registry_fact_id": registry.fact_id,
                "registry_version": registry.registry_version,
                "source": registry.source,
                "source_priority_fact_id": registry.source_priority_fact_id,
                "source_priority_version": registry.source_priority_version,
                "source_priority": registry.source_priority,
                "source_revision_order": registry.source_revision_order,
                "source_revision": registry.source_revision,
                "bootstrap_seed_id": registry.bootstrap_seed_id,
                "bootstrap_seed_version": registry.bootstrap_seed_version,
                "bootstrap_seed_hash": registry.bootstrap_seed_hash,
                "content_hash": registry.content_hash,
            }
        )

    bindings = [
        binding
        for binding in provider._bindings
        if binding.canonical_calendar_id in ids and visible(binding)
    ]
    for binding in bindings:
        rows.append(
            {
                "scope_kind": "binding",
                "calendar_id": binding.canonical_calendar_id,
                "scope_key": binding.alias,
                "selected_fact_id": binding.fact_id,
                "fact_version": binding.fact_version,
                "binding_version": binding.binding_version,
                "registry_fact_id": binding.registry_fact_id,
                "registry_version": binding.registry_version,
                "source": binding.source,
                "source_revision": binding.source_revision,
                "content_hash": binding.content_hash,
            }
        )

    capabilities = [
        capability
        for capability in provider.capabilities()
        if capability.scope_kind == "calendar"
        and capability.calendar_id in ids
        and visible(capability)
    ]
    for capability in capabilities:
        rows.append(
            {
                "scope_kind": "capability",
                "calendar_id": capability.calendar_id,
                "scope_key": capability.scope_key,
                "capability": capability.capability,
                "specificity": capability.specificity,
                "selected_fact_id": capability.fact_id,
                "fact_version": capability.fact_version,
                "value": capability.value,
                "applicability": capability.applicability,
                "source": capability.source,
                "source_revision": capability.source_revision,
                "content_hash": capability.content_hash,
            }
        )

    definitions = [
        definition
        for definition in provider._definitions
        if definition.calendar_id in ids and visible(definition)
    ]
    for definition in definitions:
        rows.append(
            {
                "scope_kind": "definition",
                "calendar_id": definition.calendar_id,
                "selected_fact_id": definition.fact_id,
                "fact_version": definition.fact_version,
                "logical_fact_key": definition.logical_fact_key,
                "definition_version": definition.definition_version,
                "registry_fact_id": definition.registry_fact_id,
                "registry_version": definition.registry_version,
                "source": definition.source,
                "source_priority_fact_id": definition.source_priority_fact_id,
                "source_priority_version": definition.source_priority_version,
                "source_priority": definition.source_priority,
                "source_revision_order": definition.source_revision_order,
                "source_revision": definition.source_revision,
                "bootstrap_seed_id": definition.bootstrap_seed_id,
                "bootstrap_seed_version": definition.bootstrap_seed_version,
                "bootstrap_seed_hash": definition.bootstrap_seed_hash,
                "quality_status": definition.quality_status,
                "content_hash": definition.content_hash,
                "valid_from": definition.valid_from,
                "valid_to": definition.valid_to,
                "knowledge_from": definition.knowledge_from,
                "knowledge_to": definition.knowledge_to,
            }
        )
    definitions_by_key = {
        (definition.calendar_id, definition.definition_version): definition
        for definition in definitions
    }
    for fact in provider._facts:
        if (
            fact.calendar_id not in ids
            or not start <= fact.session_date < end
            or not visible(fact, effective_day=fact.session_date)
        ):
            continue
        definition = definitions_by_key.get(
            (fact.calendar_id, fact.definition_version)
        )
        rows.append(
            {
                "calendar_id": fact.calendar_id,
                "date": fact.session_date,
                "fact_id": fact.fact_id,
                "fact_version": fact.fact_version,
                "logical_fact_key": fact.logical_fact_key,
                "definition_fact_id": getattr(definition, "fact_id", None),
                "definition_version": getattr(definition, "definition_version", None),
                "source": fact.source,
                "source_revision": fact.source_revision,
                "source_priority_fact_id": fact.source_priority_fact_id,
                "source_priority_version": fact.source_priority_version,
                "source_priority": fact.source_priority,
                "source_revision_order": fact.source_revision_order,
                "bootstrap_seed_id": fact.bootstrap_seed_id,
                "bootstrap_seed_version": fact.bootstrap_seed_version,
                "bootstrap_seed_hash": fact.bootstrap_seed_hash,
                "quality_status": fact.quality_status,
                "content_hash": fact.content_hash,
                "valid_from": fact.valid_from,
                "valid_to": fact.valid_to,
                "knowledge_from": fact.knowledge_from,
                "knowledge_to": fact.knowledge_to,
            }
        )

    # Priority rows are scoped to the sources represented by the bounded
    # ordinary facts/definitions/bindings/capabilities.  This is the same
    # source set used by the SQL batch query; priority rows remain separate
    # evidence and are not used as a lexical source fallback.
    sources = {
        getattr(item, "source", None)
        for item in (*registries, *definitions, *bindings, *capabilities)
    }
    sources.update(
        row.get("source")
        for row in rows
        if row.get("scope_kind") is None and row.get("source") is not None
    )
    for priority in provider.source_priorities():
        if priority.source not in sources or not visible(priority):
            continue
        rows.append(
            {
                "scope_kind": "source_priority",
                "source": priority.source,
                "selected_fact_id": priority.fact_id,
                "fact_version": priority.fact_version,
                "source_priority_version": priority.source_priority_version,
                "source_priority": priority.source_priority,
                "source_revision_order": priority.source_revision_order,
                "source_revision": priority.source_revision,
                "logical_fact_key": priority.logical_fact_key,
                "bootstrap_seed_id": priority.bootstrap_seed_id,
                "bootstrap_seed_version": priority.bootstrap_seed_version,
                "bootstrap_seed_hash": priority.bootstrap_seed_hash,
                "content_hash": priority.content_hash,
            }
        )
    return {
        "rows": sorted(
            rows,
            key=lambda row: (
                str(row.get("calendar_id", "")),
                row.get("date").isoformat()
                if isinstance(row.get("date"), date)
                else "",
                str(row.get("scope_kind", "")),
                str(row.get("fact_id", row.get("selected_fact_id", ""))),
            ),
        ),
        "pit_context": dict(context.as_dict),
    }


def _semantic_signature(resolution: CalendarAxisResolution, start: date, end: date) -> str:
    return canonical_hash({"policy": {"key": POLICY_KEY_STRICT_COMPATIBLE, "version": 1}, "calendar_ids": resolution.calendar_ids, "days": [{"date": point.session_date, "is_open": True, "timezone": point.timezone, "sessions": [window.semantic_payload() for window in point.sessions]} for point in resolution.resolved_sessions], "formal_range": {"start": start, "end": end}})


def _session_signature(resolution: CalendarAxisResolution, start: date, end: date, revision_digest: str, context: CalendarPITContext) -> str:
    return canonical_hash({"semantic_signature": _semantic_signature(resolution, start, end), "formal_sessions": [{"date": point.session_date, "timezone": point.timezone, "sessions": [window.semantic_payload() for window in point.sessions]} for point in resolution.resolved_sessions], "revision_digest": revision_digest, "pit_context": dict(context.as_dict)})


def _warmup_signature(points: Sequence[SessionPoint], revision_digest: str, anchor: date | None, requested: int) -> str:
    return canonical_hash({"requested_sessions": requested, "anchor": anchor, "sessions": [{"date": point.session_date, "timezone": point.timezone, "sessions": [window.semantic_payload() for window in point.sessions]} for point in points], "revision_digest": revision_digest})


def _json_safe(value: object) -> object:
    """Project domain provenance into the JSON scalar vocabulary.

    Usage rows are persisted in JSON result columns.  Keeping this conversion
    at the calendar boundary prevents UUID/date objects from leaking into the
    report freezer while preserving their canonical text representation.
    """

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def calendar_snapshot_usage(snapshot: CalendarSnapshot) -> tuple[Mapping[str, object], ...]:
    """Return one complete JSON usage row for every envelope day/calendar."""

    resolution = snapshot.resolution
    request = snapshot.request
    rows: list[Mapping[str, object]] = []
    formal_start = request.formal_start
    envelope_days = _iterate_days(
        snapshot.envelope_start,
        snapshot.envelope_end_exclusive - timedelta(days=1),
    )
    for day in envelope_days:
        scope = "formal" if formal_start <= day <= request.formal_end else "warmup"
        values: dict[str, object] = {}
        for calendar_id in request.calendar_ids:
            key = f"{calendar_id}:{day.isoformat()}"
            fact = dict(resolution.selected_facts.get(key, {}))
            definition = dict(resolution.resolved_calendar_definitions.get(key, {}))
            merged = {
                "calendar_id": calendar_id,
                "date": day.isoformat(),
                "definition_version": definition.get("definition_version"),
                "definition_fact_id": definition.get("definition_fact_id"),
                **fact,
            }
            if merged.get("selected_fact_id") is None and "missing_reason" not in merged:
                merged["missing_reason"] = "fact"
            elif merged.get("definition_fact_id") is None:
                merged["missing_reason"] = "definition"
            values[calendar_id] = _json_safe(merged)
        rows.append({"scope": scope, "date": day.isoformat(), "values_by_calendar": values})
    return tuple(rows)


def _snapshot_request_payload(request: CalendarSnapshotRequest) -> dict[str, object]:
    """Canonical request envelope, including the sole PIT boundary."""

    boundary = request.query_boundary
    return {
        "calendar_ids": request.calendar_ids,
        "formal_start": request.formal_start,
        "formal_end": request.formal_end,
        "warmup_sessions": request.warmup_sessions,
        "instrument_ids": [str(item) for item in request.instrument_ids],
        "provider_key": request.provider_key,
        "package_key": request.package_key,
        "package_version": request.package_version,
        "query_boundary": {
            "data_cutoff": getattr(boundary, "data_cutoff", None),
            "knowledge_as_of": getattr(boundary, "knowledge_as_of", None),
            "include_cutoff_day": getattr(boundary, "include_cutoff_day", False),
        },
    }


__all__ = [
    "POLICY_KEY_STRICT_COMPATIBLE",
    "POLICY_VERSION_STRICT_COMPATIBLE",
    "CALENDAR_PIT_PROFILE_VERSION",
    "PIT_PROFILE_STRICT_CALENDAR_CUTOFF",
    "PIT_PROFILE_STRICT_HISTORICAL_COGNITION",
    "CALENDAR_TIMEZONE_ASIA_SHANGHAI",
    "MAX_CALENDAR_IDS",
    "MAX_FORMAL_DATE_SPAN",
    "MAX_WARMUP_SESSIONS",
    "MAX_WARMUP_SEARCH_SPAN",
    "CalendarDomainError",
    "CalendarAxisDataProvider",
    "CalendarAxisDifference",
    "CalendarAxisDifferenceField",
    "CalendarAxisResolution",
    "CalendarAxisStatus",
    "CalendarDefinition",
    "CalendarSessionFact",
    "CalendarRegistry",
    "CalendarExchangeBinding",
    "CalendarCapabilityDeclaration",
    "CapabilityResolution",
    "CalendarSourcePriority",
    "CapabilityValue",
    "CapabilityApplicability",
    "select_capability_declaration",
    "CAPABILITY_SUSPENSION",
    "CAPABILITY_OPENING_AVAILABILITY",
    "CAPABILITY_PRICE_LIMIT_TRADABILITY",
    "CalendarPITContext",
    "CalendarQualityStatus",
    "CalendarSnapshotRequest",
    "CalendarSnapshot",
    "calendar_snapshot_usage",
    "NeighborState",
    "NeighborResult",
    "SessionPointContext",
    "InMemoryCalendarAxisDataProvider",
    "SessionPoint",
    "SessionWindow",
    "normalize_calendar_id",
    "normalize_session_windows",
    "normalize_window_payloads",
    "derive_cutoff_local_date",
    "select_pit_candidate",
    "register_calendar_axis_policy",
    "resolve_calendar_axis",
    "resolve_strict_compatible_axis",
    "canonical_hash",
]
