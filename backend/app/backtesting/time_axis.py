"""Generic time axis and fixed trading-session time chunks.

This module owns three version-one domain objects of the backtesting
engine timeline:

* :class:`TimeStep` -- one immutable, ordered step on a run timeline;
* :class:`TimeAxis` -- a read-only iterable container of validated steps;
* :class:`TradingDayAxis` -- the daily-frequency axis converted from
  already-resolved calendar sessions;
* :class:`TimeChunk` and :class:`FixedTradingSessionsV1` -- the
  ``fixed_trading_sessions@1`` chunk policy that slices the official
  axis into fixed-size groups of consecutive trading sessions.

The axis never queries, infers, fills, sorts, deduplicates, or rebuilds
a calendar.  ``TradingDayAxis`` consumes only the immutable
``resolved_sessions`` produced upstream by ``strict_compatible@1`` and
holds no data provider and no mutable runtime state.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.backtesting.calendar_axis import SessionPoint, SessionWindow
from app.backtesting.domain import DomainValidationError, _aware_datetime

__all__ = [
    "CHUNK_POLICY_KEY_FIXED_TRADING_SESSIONS",
    "CHUNK_POLICY_VERSION_V1",
    "SESSIONS_PER_CHUNK_V1",
    "FixedTradingSessionsV1",
    "TimeAxis",
    "TimeChunk",
    "TimeStep",
    "TradingDayAxis",
]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _non_negative_int(value: object, field_name: str) -> int:
    """Require a plain non-negative integer (booleans are not integers)."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an integer")
    if value < 0:
        raise DomainValidationError(f"{field_name} must not be negative")
    return value


def _non_blank_text(value: object, field_name: str) -> str:
    """Require non-blank text."""

    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be non-blank text")
    return value


def _iana_timezone(value: object, field_name: str) -> ZoneInfo:
    """Require a resolvable IANA time-zone name and return its ZoneInfo."""

    name = _non_blank_text(value, field_name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainValidationError(
            f"{field_name} must be a resolvable IANA time-zone name"
        ) from exc


def _frozen_value(value: object) -> object:
    """Deep-freeze one metadata value.

    Mappings become frozen mappings, lists and tuples become tuples of
    deep-frozen items, and everything else is assumed immutable.  A
    shallow copy alone would still let callers reach nested dicts and
    lists inside the step.
    """

    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _frozen_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_frozen_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_frozen_value(item) for item in value)
    return value


def _frozen_metadata(value: Mapping[str, object]) -> Mapping[str, object]:
    """Copy a mapping and deep-freeze it against later caller mutation.

    The copy is important twice over: callers may mutate their original
    dictionary afterwards without affecting the step, and downstream code
    must not be able to mutate the step's view either -- including
    nested containers held inside the metadata values.
    """

    if not isinstance(value, Mapping):
        raise DomainValidationError("metadata must be a string-keyed mapping")
    for key in value:
        if not isinstance(key, str):
            raise DomainValidationError("metadata keys must be strings")
    frozen = {key: _frozen_value(item) for key, item in value.items()}
    return MappingProxyType(frozen)


def _same_iana_zone(instant: datetime, zone: ZoneInfo, field_name: str) -> None:
    """Require the datetime's zone to be exactly the declared IANA zone.

    Only a real :class:`ZoneInfo` instance proves it expresses the named
    IANA rule across DST transitions: a custom fixed-offset tzinfo can
    forge a ``key`` attribute, so key comparison alone is not enough.
    """

    _aware_datetime(instant, field_name)
    if not isinstance(instant.tzinfo, ZoneInfo) or instant.tzinfo.key != zone.key:
        raise DomainValidationError(
            f"{field_name} must use the declared IANA timezone {zone.key!r}"
        )


# ---------------------------------------------------------------------------
# TimeStep
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeStep:
    """One immutable step on a run timeline.

    A step spans one official trading session: ``start_time`` opens its
    first trading window and ``end_time`` closes its last one.  All
    frequency-specific detail lives in the frozen ``metadata`` mapping,
    so the generic step never assumes daily bars, minutes, or ticks.
    """

    sequence: int
    start_time: datetime
    end_time: datetime
    session_id: str
    timezone: str
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        _non_negative_int(self.sequence, "sequence")
        zone = _iana_timezone(self.timezone, "timezone")
        _same_iana_zone(self.start_time, zone, "start_time")
        _same_iana_zone(self.end_time, zone, "end_time")
        if self.start_time > self.end_time:
            raise DomainValidationError(
                "start_time must not be later than end_time"
            )
        object.__setattr__(
            self, "session_id", _non_blank_text(self.session_id, "session_id")
        )
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


# ---------------------------------------------------------------------------
# TimeAxis
# ---------------------------------------------------------------------------


class TimeAxis:
    """Read-only, iterable container of ordered :class:`TimeStep` values.

    The axis trusts the caller's order for already-valid input and
    otherwise only validates: out-of-order sequences, duplicate session
    ids, and non-step entries are rejected instead of being repaired.  It
    never holds a data provider, never pads weekends or holidays, and an
    empty session list simply yields an empty axis (whether an empty run
    is admissible is an upstream preflight decision).
    """

    def __init__(self, steps: Sequence[TimeStep]) -> None:
        if isinstance(steps, (str, bytes)):
            raise DomainValidationError("steps must be a sequence of TimeStep")
        normalized: list[TimeStep] = []
        seen_session_ids: set[str] = set()
        for index, step in enumerate(steps):
            if not isinstance(step, TimeStep):
                raise DomainValidationError("steps entries must be TimeStep")
            if step.sequence != index:
                raise DomainValidationError(
                    "steps must carry strictly increasing sequence numbers "
                    f"starting at 0; entry {index} has sequence {step.sequence}"
                )
            if step.session_id in seen_session_ids:
                raise DomainValidationError(
                    f"duplicate session_id {step.session_id!r} in steps"
                )
            seen_session_ids.add(step.session_id)
            normalized.append(step)
        self._steps: tuple[TimeStep, ...] = tuple(normalized)

    def __iter__(self) -> Iterator[TimeStep]:
        return iter(self._steps)

    def __len__(self) -> int:
        return len(self._steps)

    def at(self, index: int) -> TimeStep:
        """Return the step at ``index``; out-of-range raises IndexError."""

        return self._steps[index]


# ---------------------------------------------------------------------------
# TradingDayAxis
# ---------------------------------------------------------------------------


def _format_window_time(value: time) -> str:
    """Canonical ``HH:MM`` text for minute-precision window boundaries.

    Higher declared precision is preserved so distinct boundaries can
    never collide into the same metadata text.
    """

    if value.second == 0 and value.microsecond == 0:
        return value.strftime("%H:%M")
    if value.microsecond == 0:
        return value.strftime("%H:%M:%S")
    return value.strftime("%H:%M:%S.%f")


class TradingDayAxis(TimeAxis):
    """Daily-frequency axis converted from resolved calendar sessions.

    The constructor consumes only :class:`~app.backtesting.calendar_axis.SessionPoint`
    values that the ``strict_compatible@1`` resolver already normalized:
    it never queries calendars, adds dates, sorts, deduplicates, or
    rebuilds anything.  One session becomes exactly one step even when
    the day has multiple trading windows; the midday break survives only
    as window detail inside frozen metadata.
    """

    def __init__(self, resolved_sessions: Sequence[SessionPoint]) -> None:
        if isinstance(resolved_sessions, (str, bytes)):
            raise DomainValidationError(
                "resolved_sessions must be a sequence of SessionPoint"
            )
        steps: list[TimeStep] = []
        previous_date: date | None = None
        for index, point in enumerate(resolved_sessions):
            if not isinstance(point, SessionPoint):
                raise DomainValidationError(
                    "resolved_sessions entries must be SessionPoint"
                )
            if previous_date is not None and point.session_date <= previous_date:
                raise DomainValidationError(
                    "resolved_sessions must be ordered by session_date "
                    "without duplicates"
                )
            previous_date = point.session_date
            steps.append(self._convert(point, index))
        super().__init__(steps)

    @staticmethod
    def _convert(point: SessionPoint, index: int) -> TimeStep:
        """Convert one resolved session into one generic time step.

        ``start_time`` opens the first window, ``end_time`` closes the
        last one, and every raw window is preserved in normalized order.
        """

        zone = ZoneInfo(point.timezone)
        windows: tuple[SessionWindow, ...] = point.sessions
        first_window = windows[0]
        last_window = windows[-1]
        start_time = datetime.combine(
            point.session_date, first_window.start_time, tzinfo=zone
        )
        end_time = datetime.combine(
            point.session_date, last_window.end_time, tzinfo=zone
        )
        metadata: dict[str, object] = {
            "session_date": point.session_date.isoformat(),
            "session_windows": tuple(
                (
                    _format_window_time(window.start_time),
                    _format_window_time(window.end_time),
                )
                for window in windows
            ),
        }
        return TimeStep(
            sequence=index,
            start_time=start_time,
            end_time=end_time,
            session_id=point.session_id,
            timezone=point.timezone,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# fixed_trading_sessions@1 time chunks
# ---------------------------------------------------------------------------

CHUNK_POLICY_KEY_FIXED_TRADING_SESSIONS = "fixed_trading_sessions"
CHUNK_POLICY_VERSION_V1 = 1
SESSIONS_PER_CHUNK_V1 = 20


@dataclass(frozen=True, slots=True)
class TimeChunk:
    """One immutable slice of the official timeline.

    Chunks manage only the lifecycle of data resources; they never reset
    strategies, accounts, positions, active orders, analyzers, or the
    global event sequence, all of which live outside the chunk loop.
    """

    chunk_sequence: int
    steps: tuple[TimeStep, ...]

    def __post_init__(self) -> None:
        _non_negative_int(self.chunk_sequence, "chunk_sequence")
        if isinstance(self.steps, (str, bytes)) or not isinstance(
            self.steps, Sequence
        ):
            raise DomainValidationError("steps must be a sequence of TimeStep")
        if not self.steps:
            raise DomainValidationError("steps must contain at least one TimeStep")
        # Copy to tuple so a caller-supplied mutable list can never be
        # changed after construction, and verify the slice is a
        # contiguous run of the official timeline: consecutive sequence
        # numbers with no gaps and no reordering.
        normalized = tuple(self.steps)
        first_sequence = normalized[0].sequence
        for index, step in enumerate(normalized):
            if not isinstance(step, TimeStep):
                raise DomainValidationError("steps entries must be TimeStep")
            if step.sequence != first_sequence + index:
                raise DomainValidationError(
                    "steps must be a contiguous slice of the official "
                    "timeline: expected sequence "
                    f"{first_sequence + index} at position {index}, got "
                    f"{step.sequence}"
                )
        object.__setattr__(self, "steps", normalized)

    @property
    def first_session_id(self) -> str:
        """Stable machine id of the chunk's earliest session."""

        return self.steps[0].session_id

    @property
    def last_session_id(self) -> str:
        """Stable machine id of the chunk's latest session."""

        return self.steps[-1].session_id


class FixedTradingSessionsV1:
    """The ``fixed_trading_sessions@1`` chunk partition policy.

    Every chunk holds at most :attr:`sessions_per_chunk` consecutive
    official steps, sliced purely by input order -- never by natural
    month, query load, or data size.  The tail chunk may hold fewer
    steps.  Warmup steps never enter this input: the policy receives the
    official axis only.
    """

    policy_key: str = CHUNK_POLICY_KEY_FIXED_TRADING_SESSIONS
    policy_version: int = CHUNK_POLICY_VERSION_V1

    @property
    def sessions_per_chunk(self) -> int:
        """Fixed chunk size frozen by ``fixed_trading_sessions@1``.

        Exposed as a read-only property bound to the version constant so
        runtime code cannot shrink or grow the chunk size.
        """

        return SESSIONS_PER_CHUNK_V1

    def partition(
        self,
        steps: Sequence[TimeStep],
    ) -> tuple[TimeChunk, ...]:
        """Slice the official steps into fixed-size consecutive chunks."""

        if isinstance(steps, (str, bytes)):
            raise DomainValidationError("steps must be a sequence of TimeStep")
        normalized = tuple(steps)
        # Contiguity between sequence numbers and tuple indexes is a
        # precondition: a gap would silently split one run into two.
        for index, step in enumerate(normalized):
            if not isinstance(step, TimeStep):
                raise DomainValidationError("steps entries must be TimeStep")
            if step.sequence != index:
                raise DomainValidationError(
                    "steps must be the complete official timeline with "
                    f"sequence equal to index; entry {index} has sequence "
                    f"{step.sequence}"
                )
        size = self.sessions_per_chunk
        return tuple(
            TimeChunk(
                chunk_sequence=chunk_sequence,
                steps=normalized[offset : offset + size],
            )
            for chunk_sequence, offset in enumerate(range(0, len(normalized), size))
        )
