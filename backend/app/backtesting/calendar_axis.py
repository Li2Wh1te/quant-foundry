"""Named trading-calendar domain model and the ``strict_compatible@1`` policy.

This module is deliberately free of SQLAlchemy, FastAPI, Tushare, and any
concrete market-data table.  Callers supply versioned calendar definitions
and per-day session facts through the :class:`CalendarAxisDataProvider`
protocol (or the in-memory adapter) and receive an immutable resolution
describing whether the named calendars can share one trading timeline.

The ``strict_compatible@1`` policy never selects a master calendar, never
unions or intersects calendars, never splits a backtest, and never infers
missing facts.  Any difference, gap, or unresolvable session inside the
requested inclusive date range makes the whole result ``incompatible``;
upstream run gates are expected to map that status to ``blocked``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.backtesting.domain import DomainValidationError

POLICY_KEY_STRICT_COMPATIBLE = "strict_compatible"
POLICY_VERSION_STRICT_COMPATIBLE = "1"


class CalendarAxisStatus(StrEnum):
    """Outcome of a calendar-axis compatibility check."""

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


class CalendarAxisDifferenceField(StrEnum):
    """Locatable field that caused an incompatibility."""

    IS_OPEN = "is_open"
    TIMEZONE = "timezone"
    SESSIONS = "sessions"
    MISSING_FACT = "missing_fact"
    MISSING_DEFINITION = "missing_definition"
    UNRESOLVED_SESSION = "unresolved_session"


def _non_blank_text(value: object, field_name: str) -> str:
    """Require non-blank text and return it unchanged (no silent rewriting)."""

    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be non-blank text")
    return value


def _optional_label(value: str | None, field_name: str) -> str | None:
    """Normalize an optional human-readable label; blank text means missing."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be text when provided")
    return value.strip() or None


def _plain_date(value: object, field_name: str) -> date:
    """Require a calendar date and reject full datetimes."""

    if not isinstance(value, date) or isinstance(value, datetime):
        raise DomainValidationError(f"{field_name} must be a datetime.date")
    return value


def _local_time(value: object, field_name: str) -> time:
    """Require a naive wall-clock local time for session boundaries.

    Session boundaries are declared in the calendar's local time; an
    embedded UTC offset would duplicate the calendar time zone, leak into
    signatures, and make mixed naive/aware lists unsortable.
    """

    if not isinstance(value, time):
        raise DomainValidationError(f"{field_name} must be a datetime.time")
    if value.tzinfo is not None and value.utcoffset() is not None:
        raise DomainValidationError(
            f"{field_name} must be a naive local time without tzinfo"
        )
    return value


def _timezone_name(value: object, field_name: str) -> str:
    """Require a resolvable IANA time-zone name."""

    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be non-blank text")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainValidationError(
            f"{field_name} must be a resolvable IANA time-zone name"
        ) from exc
    return value


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """One intraday trading session bounded by local wall-clock times.

    The first version only expresses sessions inside one calendar day;
    overnight sessions that cross midnight are out of scope.
    """

    start_time: time
    end_time: time
    label: str | None = None

    def __post_init__(self) -> None:
        start = _local_time(self.start_time, "start_time")
        end = _local_time(self.end_time, "end_time")
        if start >= end:
            raise DomainValidationError(
                "start_time must be strictly earlier than end_time"
            )
        object.__setattr__(self, "start_time", start)
        object.__setattr__(self, "end_time", end)
        object.__setattr__(self, "label", _optional_label(self.label, "label"))


def normalize_session_windows(
    value: Iterable[SessionWindow | tuple[time, time] | tuple[time, time, str | None]],
    field_name: str,
) -> tuple[SessionWindow, ...]:
    """Normalize an ordered session list into validated, sorted windows.

    Equivalent representations (for example windows supplied out of order)
    normalize to the same tuple, which keeps signatures and comparisons
    stable.  Overlapping windows are rejected instead of merged.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise DomainValidationError(f"{field_name} must be an iterable of sessions")
    try:
        items = list(value)
    except TypeError as exc:
        raise DomainValidationError(
            f"{field_name} must be an iterable of sessions"
        ) from exc
    windows: list[SessionWindow] = []
    for item in items:
        if isinstance(item, SessionWindow):
            windows.append(item)
            continue
        if isinstance(item, tuple):
            # A malformed tuple would otherwise surface as a bare TypeError;
            # every domain validation failure must be a DomainValidationError.
            try:
                windows.append(SessionWindow(*item))
            except TypeError as exc:
                raise DomainValidationError(
                    f"{field_name} contains an invalid session entry"
                ) from exc
            continue
        raise DomainValidationError(
            f"{field_name} entries must be SessionWindow or time tuples"
        )
    windows.sort(key=lambda window: window.start_time)
    for earlier, later in zip(windows, windows[1:]):
        if later.start_time < earlier.end_time:
            raise DomainValidationError(f"{field_name} must not contain overlapping sessions")
    return tuple(windows)


def _format_time(value: time) -> str:
    """Canonical text used in reports and signatures.

    Minute-precision boundaries normalize to ``HH:MM`` per the task
    contract; larger declared precision is preserved so distinct session
    boundaries can never collide into the same signature text.
    """

    if value.second == 0 and value.microsecond == 0:
        return value.strftime("%H:%M")
    if value.microsecond == 0:
        return value.strftime("%H:%M:%S")
    return value.strftime("%H:%M:%S.%f")


def _format_sessions(sessions: Sequence[SessionWindow]) -> str:
    """Canonical, deterministic text rendering of a session list."""

    return json.dumps(
        [[_format_time(w.start_time), _format_time(w.end_time)] for w in sessions],
        separators=(",", ":"),
    )


def _format_flag(value: bool) -> str:
    """Canonical text rendering of a boolean fact."""

    return "true" if value else "false"


def _session_business_key(
    sessions: Sequence[SessionWindow],
) -> tuple[tuple[time, time], ...]:
    """Business identity of a session list: boundaries only.

    ``label`` is human-readable audit text, not a business field, so two
    session lists that differ only by label are compatible.
    """

    return tuple((window.start_time, window.end_time) for window in sessions)


@dataclass(frozen=True, slots=True)
class CalendarDefinition:
    """Versioned default definition of one named trading calendar.

    A definition only declares the time zone and the default session
    template; it can never decide by itself whether a specific date is
    open.  ``source`` is audit metadata and never participates in the
    compatibility decision.
    """

    calendar_id: str
    definition_version: str
    timezone: str
    default_sessions: tuple[SessionWindow, ...]
    valid_from: date | None = None
    valid_to: date | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "calendar_id", _non_blank_text(self.calendar_id, "calendar_id")
        )
        object.__setattr__(
            self,
            "definition_version",
            _non_blank_text(self.definition_version, "definition_version"),
        )
        object.__setattr__(
            self, "timezone", _timezone_name(self.timezone, "timezone")
        )
        object.__setattr__(
            self,
            "default_sessions",
            normalize_session_windows(self.default_sessions, "default_sessions"),
        )
        valid_from = (
            _plain_date(self.valid_from, "valid_from") if self.valid_from else None
        )
        valid_to = _plain_date(self.valid_to, "valid_to") if self.valid_to else None
        if valid_from and valid_to and valid_from > valid_to:
            raise DomainValidationError("valid_from must not be later than valid_to")
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)
        object.__setattr__(self, "source", _optional_label(self.source, "source"))

    def applies_to(self, day: date) -> bool:
        """Whether this definition version is valid on ``day``."""

        if self.valid_from and day < self.valid_from:
            return False
        if self.valid_to and day > self.valid_to:
            return False
        return True


@dataclass(frozen=True, slots=True)
class CalendarSessionFact:
    """Explicit open/closed fact for one named calendar on one date.

    ``is_open`` must always be supplied explicitly; a missing fact can
    never be inferred as closed.  The optional overrides replace the
    definition time zone or default session template for this date only.
    Whether an override is active is derived from the field being present;
    no extra boolean flag is stored alongside it.
    """

    calendar_id: str
    session_date: date
    is_open: bool
    definition_version: str
    timezone_override: str | None = None
    sessions_override: tuple[SessionWindow, ...] | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "calendar_id", _non_blank_text(self.calendar_id, "calendar_id")
        )
        object.__setattr__(
            self, "session_date", _plain_date(self.session_date, "session_date")
        )
        if not isinstance(self.is_open, bool):
            raise DomainValidationError("is_open must be an explicit boolean")
        object.__setattr__(
            self,
            "definition_version",
            _non_blank_text(self.definition_version, "definition_version"),
        )
        if self.timezone_override is not None:
            object.__setattr__(
                self,
                "timezone_override",
                _timezone_name(self.timezone_override, "timezone_override"),
            )
        if self.sessions_override is not None:
            object.__setattr__(
                self,
                "sessions_override",
                normalize_session_windows(
                    self.sessions_override, "sessions_override"
                ),
            )
        object.__setattr__(self, "source", _optional_label(self.source, "source"))


@dataclass(frozen=True, slots=True)
class SessionPoint:
    """One normalized common trading session on the shared timeline.

    The point carries no ``calendar_id`` because it belongs to the deduped
    common axis of all compatible calendars; every instrument keeps its own
    ``calendar_id`` in the instrument or data-eligibility objects.  Closed
    days never produce a point.
    """

    session_date: date
    session_id: str
    timezone: str
    sessions: tuple[SessionWindow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_date", _plain_date(self.session_date, "session_date")
        )
        object.__setattr__(
            self, "session_id", _non_blank_text(self.session_id, "session_id")
        )
        object.__setattr__(
            self, "timezone", _timezone_name(self.timezone, "timezone")
        )
        if not self.sessions:
            raise DomainValidationError("sessions must contain at least one window")
        object.__setattr__(
            self,
            "sessions",
            normalize_session_windows(self.sessions, "sessions"),
        )


@dataclass(frozen=True, slots=True)
class CalendarAxisDifference:
    """One locatable incompatibility between the requested calendars.

    ``expected_value`` is a deterministic reporting reference only.  The
    reference calendar is the lexically first calendar of the request; it
    is NOT a semantically selected master calendar and no policy decision
    is based on it.
    """

    date: date
    calendar_id: str
    field: CalendarAxisDifferenceField
    actual_value: str | None
    expected_value: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _plain_date(self.date, "date"))
        object.__setattr__(
            self, "calendar_id", _non_blank_text(self.calendar_id, "calendar_id")
        )
        if not isinstance(self.field, CalendarAxisDifferenceField):
            raise DomainValidationError("field must be a CalendarAxisDifferenceField")

    @property
    def sort_key(self) -> tuple[str, str, str, str, str]:
        """Stable ordering key: date, calendar, field, actual, expected."""

        return (
            self.date.isoformat(),
            self.calendar_id,
            self.field.value,
            self.actual_value or "",
            self.expected_value or "",
        )


@dataclass(frozen=True, slots=True)
class CalendarAxisResolution:
    """Immutable result of one calendar-axis compatibility check.

    ``calendar_ids`` is deduplicated and sorted so the result does not
    depend on input order.  An incompatible result never carries
    ``resolved_sessions``: the empty tuple prevents downstream code from
    consuming a partially valid axis.
    """

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

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "policy_key", _non_blank_text(self.policy_key, "policy_key")
        )
        object.__setattr__(
            self,
            "policy_version",
            _non_blank_text(self.policy_version, "policy_version"),
        )
        object.__setattr__(self, "start_date", _plain_date(self.start_date, "start_date"))
        object.__setattr__(self, "end_date", _plain_date(self.end_date, "end_date"))
        if self.start_date > self.end_date:
            raise DomainValidationError("start_date must not be later than end_date")
        if not self.calendar_ids or any(
            not isinstance(calendar_id, str) or not calendar_id.strip()
            for calendar_id in self.calendar_ids
        ):
            raise DomainValidationError(
                "calendar_ids must be a non-empty sequence of non-blank ids"
            )
        object.__setattr__(self, "calendar_ids", tuple(sorted(set(self.calendar_ids))))
        if not isinstance(self.status, CalendarAxisStatus):
            raise DomainValidationError("status must be a CalendarAxisStatus")
        sessions = tuple(self.resolved_sessions)
        dates = [point.session_date for point in sessions]
        if dates != sorted(dates):
            raise DomainValidationError(
                "resolved_sessions must be ordered by session_date"
            )
        if len(dates) != len(set(dates)):
            raise DomainValidationError(
                "resolved_sessions must not contain duplicate session dates"
            )
        for point in sessions:
            if not (
                self.start_date <= point.session_date <= self.end_date
            ):
                raise DomainValidationError(
                    "resolved_sessions must stay inside the requested date range"
                )
            # First version is a daily axis: the session id must be the
            # normalized date string of its own session_date.
            if point.session_id != point.session_date.isoformat():
                raise DomainValidationError(
                    "session_id must equal the ISO date of session_date"
                )
        if self.status is CalendarAxisStatus.INCOMPATIBLE:
            if not self.differences:
                raise DomainValidationError(
                    "incompatible results must carry at least one difference"
                )
            if sessions:
                raise DomainValidationError(
                    "incompatible results must not carry resolved_sessions"
                )
            if self.timezone is not None:
                raise DomainValidationError(
                    "incompatible results must not declare a common timezone"
                )
            if self.session_signature:
                raise DomainValidationError(
                    "incompatible results must not carry a session signature"
                )
        else:
            if self.differences:
                raise DomainValidationError(
                    "compatible results must not carry differences"
                )
            if not self.session_signature:
                raise DomainValidationError(
                    "compatible results must carry a session signature"
                )
            open_timezones = {point.timezone for point in sessions}
            expected_timezone = (
                next(iter(open_timezones)) if len(open_timezones) == 1 else None
            )
            if self.timezone != expected_timezone:
                raise DomainValidationError(
                    "timezone must be the unique open-session timezone or None"
                )
        object.__setattr__(self, "resolved_sessions", sessions)
        object.__setattr__(
            self,
            "differences",
            tuple(sorted(tuple(self.differences), key=lambda diff: diff.sort_key)),
        )


class CalendarAxisDataProvider(Protocol):
    """Structural source of calendar definitions and per-day facts.

    Implementations may be backed by the ORM trading-calendar tables, a
    market-data client, or an in-memory fake in tests; the policy depends
    only on this protocol and never queries a database itself.
    """

    def definitions(self, calendar_id: str) -> Sequence[CalendarDefinition]:
        """Return every known definition version for one calendar."""
        ...

    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None:
        """Return the explicit session fact for one calendar and date."""
        ...


class InMemoryCalendarAxisDataProvider:
    """Immutable in-memory adapter used by tests and future tooling."""

    def __init__(
        self,
        definitions: Iterable[CalendarDefinition],
        facts: Iterable[CalendarSessionFact],
    ) -> None:
        self._definitions: dict[str, tuple[CalendarDefinition, ...]] = {}
        for definition in definitions:
            self._definitions.setdefault(definition.calendar_id, ())
            self._definitions[definition.calendar_id] += (definition,)
        self._facts: dict[tuple[str, date], CalendarSessionFact] = {}
        for fact in facts:
            key = (fact.calendar_id, fact.session_date)
            if key in self._facts:
                raise DomainValidationError(
                    "duplicate session fact for one calendar and date"
                )
            self._facts[key] = fact

    def definitions(self, calendar_id: str) -> tuple[CalendarDefinition, ...]:
        return self._definitions.get(calendar_id, ())

    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None:
        return self._facts.get((calendar_id, day))


@dataclass(frozen=True, slots=True)
class _ResolvedDay:
    """Internal per-calendar resolution outcome for one date."""

    is_open: bool
    timezone: str | None
    sessions: tuple[SessionWindow, ...]


@dataclass(frozen=True, slots=True)
class _FailedDay:
    """Internal per-calendar failure outcome for one date."""

    field: CalendarAxisDifferenceField
    actual_value: str | None
    expected_value: str | None


def _resolve_calendar_day(
    provider: CalendarAxisDataProvider, calendar_id: str, day: date
) -> _ResolvedDay | _FailedDay:
    """Resolve the effective session for one calendar on one date.

    The fact must exist and reference exactly one applicable definition
    version.  Open days additionally require a resolvable time zone and a
    non-empty session list; closed days only need the explicit fact.
    """

    fact = provider.fact(calendar_id, day)
    if fact is None or fact.session_date != day or fact.calendar_id != calendar_id:
        return _FailedDay(
            field=CalendarAxisDifferenceField.MISSING_FACT,
            actual_value="missing",
            expected_value="present",
        )
    applicable = [
        definition
        for definition in provider.definitions(calendar_id)
        if definition.definition_version == fact.definition_version
        and definition.applies_to(day)
    ]
    if not applicable:
        return _FailedDay(
            field=CalendarAxisDifferenceField.MISSING_DEFINITION,
            actual_value=fact.definition_version,
            expected_value="exactly one applicable definition",
        )
    if len(applicable) > 1:
        return _FailedDay(
            field=CalendarAxisDifferenceField.MISSING_DEFINITION,
            actual_value=f"ambiguous:{len(applicable)}",
            expected_value="exactly one applicable definition",
        )
    definition = applicable[0]
    timezone = fact.timezone_override or definition.timezone
    sessions = (
        fact.sessions_override
        if fact.sessions_override is not None
        else definition.default_sessions
    )
    if not fact.is_open:
        return _ResolvedDay(is_open=False, timezone=None, sessions=())
    if not timezone or not sessions:
        missing = "timezone" if not timezone else "sessions"
        return _FailedDay(
            field=CalendarAxisDifferenceField.UNRESOLVED_SESSION,
            actual_value=f"unresolvable:{missing}",
            expected_value="resolved timezone and sessions",
        )
    return _ResolvedDay(is_open=True, timezone=timezone, sessions=sessions)


def _canonical_day_record(
    day: date, is_open: bool, timezone: str | None, sessions: Sequence[SessionWindow]
) -> dict[str, Any]:
    """Build the stable signature input record for one date.

    Closed days contribute ``is_open = false`` with no time zone and no
    sessions, regardless of any overrides stored on their facts.
    """

    return {
        "date": day.isoformat(),
        "is_open": is_open,
        "timezone": timezone,
        "sessions": [
            [_format_time(w.start_time), _format_time(w.end_time)] for w in sessions
        ],
    }


def _session_signature(records: Sequence[dict[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of the per-day records."""

    payload = json.dumps(list(records), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_strict_compatible_axis(
    provider: CalendarAxisDataProvider,
    *,
    start_date: date,
    end_date: date,
    calendar_ids: Sequence[str],
) -> CalendarAxisResolution:
    """Check whether the named calendars share one axis on ``strict_compatible@1``."""

    start = _plain_date(start_date, "start_date")
    end = _plain_date(end_date, "end_date")
    if start > end:
        raise DomainValidationError("start_date must not be later than end_date")
    normalized_ids: list[str] = []
    for calendar_id in calendar_ids:
        normalized_ids.append(_non_blank_text(calendar_id, "calendar_ids entry"))
    if not normalized_ids:
        raise DomainValidationError("at least one calendar_id is required")
    # Deduplicated lexical order makes every downstream comparison and the
    # signature independent of the caller's input order.
    ordered_ids = tuple(sorted(set(normalized_ids)))

    differences: list[CalendarAxisDifference] = []
    # Structured per-day outcome of the shared axis: (date, is_open,
    # timezone, sessions).  Closed days carry no timezone and no sessions.
    resolved_days: list[tuple[date, bool, str | None, tuple[SessionWindow, ...]]] = []
    for day in _iterate_days(start, end):
        outcomes = {
            calendar_id: _resolve_calendar_day(provider, calendar_id, day)
            for calendar_id in ordered_ids
        }
        failed = False
        for calendar_id, outcome in outcomes.items():
            if isinstance(outcome, _FailedDay):
                differences.append(
                    CalendarAxisDifference(
                        date=day,
                        calendar_id=calendar_id,
                        field=outcome.field,
                        actual_value=outcome.actual_value,
                        expected_value=outcome.expected_value,
                    )
                )
                failed = True
        if failed:
            continue

        # The lexically first calendar is only the deterministic reporting
        # reference for difference evidence; it is NOT a master calendar
        # and the policy never adopts its values over the others'.
        reference = outcomes[ordered_ids[0]]
        assert isinstance(reference, _ResolvedDay)
        if not all(outcome.is_open for outcome in outcomes.values()):
            if any(outcome.is_open for outcome in outcomes.values()):
                for calendar_id, outcome in outcomes.items():
                    assert isinstance(outcome, _ResolvedDay)
                    if outcome.is_open != reference.is_open:
                        differences.append(
                            CalendarAxisDifference(
                                date=day,
                                calendar_id=calendar_id,
                                field=CalendarAxisDifferenceField.IS_OPEN,
                                actual_value=_format_flag(outcome.is_open),
                                expected_value=_format_flag(reference.is_open),
                            )
                        )
            resolved_days.append((day, False, None, ()))
            continue

        for calendar_id in ordered_ids[1:]:
            outcome = outcomes[calendar_id]
            assert isinstance(outcome, _ResolvedDay)
            if outcome.timezone != reference.timezone:
                differences.append(
                    CalendarAxisDifference(
                        date=day,
                        calendar_id=calendar_id,
                        field=CalendarAxisDifferenceField.TIMEZONE,
                        actual_value=outcome.timezone,
                        expected_value=reference.timezone,
                    )
                )
            if _session_business_key(outcome.sessions) != _session_business_key(
                reference.sessions
            ):
                differences.append(
                    CalendarAxisDifference(
                        date=day,
                        calendar_id=calendar_id,
                        field=CalendarAxisDifferenceField.SESSIONS,
                        actual_value=_format_sessions(outcome.sessions),
                        expected_value=_format_sessions(reference.sessions),
                    )
                )
        resolved_days.append((day, True, reference.timezone, reference.sessions))

    if differences:
        return CalendarAxisResolution(
            policy_key=POLICY_KEY_STRICT_COMPATIBLE,
            policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
            start_date=start,
            end_date=end,
            calendar_ids=ordered_ids,
            session_signature="",
            timezone=None,
            resolved_sessions=(),
            status=CalendarAxisStatus.INCOMPATIBLE,
            differences=tuple(differences),
        )

    resolved_sessions = tuple(
        SessionPoint(
            session_date=day,
            session_id=day.isoformat(),
            timezone=timezone,
            sessions=sessions,
        )
        for day, is_open, timezone, sessions in resolved_days
        if is_open
    )
    open_timezones = {timezone for _, is_open, timezone, _ in resolved_days if is_open}
    return CalendarAxisResolution(
        policy_key=POLICY_KEY_STRICT_COMPATIBLE,
        policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
        start_date=start,
        end_date=end,
        calendar_ids=ordered_ids,
        session_signature=_session_signature(
            [
                _canonical_day_record(day, is_open, timezone, sessions)
                for day, is_open, timezone, sessions in resolved_days
            ]
        ),
        timezone=next(iter(open_timezones)) if len(open_timezones) == 1 else None,
        resolved_sessions=resolved_sessions,
        status=CalendarAxisStatus.COMPATIBLE,
        differences=(),
    )


def _iterate_days(start: date, end: date) -> list[date]:
    """Every inclusive calendar date between ``start`` and ``end``."""

    span = (end - start).days
    return [date.fromordinal(start.toordinal() + offset) for offset in range(span + 1)]


CalendarAxisResolver = Callable[..., CalendarAxisResolution]

_POLICIES: dict[tuple[str, str], CalendarAxisResolver] = {}


def register_calendar_axis_policy(
    policy_key: str, policy_version: str, resolver: CalendarAxisResolver
) -> None:
    """Register a versioned calendar-axis policy resolver."""

    key = (_non_blank_text(policy_key, "policy_key"), _non_blank_text(policy_version, "policy_version"))
    if key in _POLICIES:
        raise DomainValidationError(
            f"calendar axis policy already registered: {key[0]}@{key[1]}"
        )
    _POLICIES[key] = resolver


def resolve_calendar_axis(
    provider: CalendarAxisDataProvider,
    *,
    policy_key: str,
    policy_version: str,
    start_date: date,
    end_date: date,
    calendar_ids: Sequence[str],
) -> CalendarAxisResolution:
    """Resolve the axis through a registered versioned policy."""

    try:
        resolver = _POLICIES[(policy_key, policy_version)]
    except KeyError:
        raise DomainValidationError(
            f"unknown calendar axis policy: {policy_key}@{policy_version}"
        ) from None
    return resolver(
        provider,
        start_date=start_date,
        end_date=end_date,
        calendar_ids=calendar_ids,
    )


register_calendar_axis_policy(
    POLICY_KEY_STRICT_COMPATIBLE,
    POLICY_VERSION_STRICT_COMPATIBLE,
    resolve_strict_compatible_axis,
)


__all__ = [
    "POLICY_KEY_STRICT_COMPATIBLE",
    "POLICY_VERSION_STRICT_COMPATIBLE",
    "CalendarAxisDataProvider",
    "CalendarAxisDifference",
    "CalendarAxisDifferenceField",
    "CalendarAxisResolution",
    "CalendarAxisStatus",
    "CalendarDefinition",
    "CalendarSessionFact",
    "InMemoryCalendarAxisDataProvider",
    "SessionPoint",
    "SessionWindow",
    "normalize_session_windows",
    "register_calendar_axis_policy",
    "resolve_calendar_axis",
    "resolve_strict_compatible_axis",
]
