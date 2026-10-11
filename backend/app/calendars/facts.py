"""Shared named-calendar facts and ingestion selectors; no run axis or snapshots."""
from __future__ import annotations


import hashlib


import json


import re


from collections.abc import (Iterable, Mapping, Sequence)


from dataclasses import (dataclass)


from datetime import (date, datetime, time, timedelta, timezone)


from enum import (StrEnum)


from types import (MappingProxyType)


from typing import (Protocol)


from uuid import (NAMESPACE_URL, UUID, uuid5)


from zoneinfo import (ZoneInfo, ZoneInfoNotFoundError)


from app.backtest_service.config_values import (DomainValidationError)


from app.data_contracts.errors import (CalendarBindingAmbiguousError, CalendarBindingUnknownError, CalendarContractError, CalendarCrossMidnightUnsupportedError, CalendarDefinitionAmbiguousError, CalendarDefinitionInvalidError, CalendarDefinitionMissingError, CalendarFactAmbiguousError, CalendarFactInvalidError, CalendarFactMissingError, CalendarIdUnknownError, CalendarJsonInvalidError, CalendarPitMetadataMissingError, CalendarRegistryAmbiguousError, CalendarRegistryFactMissingError, CalendarRegistryReferenceInvalidError, CalendarSessionInvalidError, CalendarSessionWindowLimitExceededError, CalendarTimezoneUnsupportedError, CalendarSourcePriorityAmbiguousError, CalendarSourcePriorityChainBrokenError, CalendarSourcePriorityInvalidError, CalendarSourceRevisionConflictError, CalendarSourcePriorityMissingError, DataCutoffExceededError, DataCutoffRequiredError, freeze_json)


CALENDAR_PIT_PROFILE_VERSION = "calendar_pit_profile@1:H"


PIT_PROFILE_STRICT_CALENDAR_CUTOFF = "strict_calendar_cutoff"


PIT_PROFILE_STRICT_HISTORICAL_COGNITION = "strict_historical_cognition"


CALENDAR_TIMEZONE_ASIA_SHANGHAI = "Asia/Shanghai"


_CALENDAR_ID_RE = re.compile(r"^[A-Z][A-Z0-9._-]{0,31}$")


_WINDOW_KEYS = frozenset({"start", "end", "day_offset", "end_day_offset", "label"})


_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9](?:\.[0-9]{1,6})?)?$")


class CalendarDomainError(DomainValidationError):
    """A pure-domain failure carrying a stable machine code and JSON details."""

    code = "calendar_contract_error"

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = freeze_json(details or {}, "details")


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
        from app.calendars.contracts import QueryBoundary

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


class NamedCalendarFactProvider(Protocol):
    def definitions(self, calendar_id: str) -> Sequence[CalendarDefinition]: ...
    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None: ...


