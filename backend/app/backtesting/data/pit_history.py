"""Formal point-in-time identity resolution and segmented bar history.

This module is the production read path for historical bars: every query
starts from the stable ``instrument_id``, resolves the evidenced
``InstrumentCodeMapping`` segments that were visible at ``data_cutoff``,
binds each requested trading session to exactly one source code, reads
bars per segment, and validates strict session coverage before the
segments are stitched into one stable-identity series.

Hard rules implemented here:

* effective time and knowledge time are separated; only mappings with
  ``known_at <= data_cutoff`` participate, regardless of their validity
  dates;
* validity intervals are half-open ``[valid_from, valid_to)`` everywhere;
* a mapping gap, an overlap, or missing evidence blocks the whole query —
  nothing is ever repaired with today's code and no window is silently
  shortened;
* per-segment reads must return exactly one valid bar per requested
  session keyed by the stable instrument id: missing, duplicate,
  out-of-range, or wrong-identity bars block the query.

The resolution result carries auditable evidence (segments, source codes,
requested ranges, coverage status) but never a fallback code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence
from uuid import UUID

from app.backtesting.data.errors import (
    DataContractError,
    DataCutoffExceededError,
    HistoryBarsDuplicateError,
    HistoryBarInstrumentMismatchError,
    HistoryBarsIncompleteError,
    IdentityMappingConflictError,
    IdentityMappingEvidenceMissingError,
    IdentityMappingIncompleteError,
    InvalidDataRequestError,
    ProviderContractViolationError,
    freeze_json,
)
from app.backtesting.data.facts import AdjustedSeriesPoint, Bar
from app.backtesting.data.requests import QualityStatus
from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.instruments.domain import (
    InstrumentCodeMapping,
    MappingConflictError,
    MappingCoverageGapError,
)


def _source_key(value: str) -> str:
    """Normalize source labels for case/whitespace-insensitive matching."""

    return value.strip().casefold()


def _mapping_fact_key(mapping: InstrumentCodeMapping) -> tuple[object, ...]:
    """Return an immutable identity key for one mapping fact.

    Repository adapters may materialize the same persisted fact as distinct
    Python objects.  Segment grouping must therefore use persisted identity
    (fact id/version or logical key), never object identity.  The structural
    fallback is reserved for deliberately corrupted legacy rows that lack
    both immutable identifiers; it remains deterministic for equal rows.
    """

    fact_id = getattr(mapping, "fact_id", None)
    fact_version = getattr(mapping, "fact_version", 1)
    logical_key = getattr(mapping, "logical_fact_key", None)
    if fact_id is not None:
        identity: object = fact_id
    elif logical_key is not None:
        identity = logical_key
    else:
        identity = (
            getattr(mapping, "source", None),
            getattr(mapping, "source_code", None),
            getattr(mapping, "valid_from", None),
            getattr(mapping, "valid_to", None),
            getattr(mapping, "known_at", None),
            getattr(mapping, "observed_at", None),
            getattr(mapping, "evidence", None),
        )
    return (identity, fact_version, logical_key)


_DETAIL_MISSING = object()


def _detail_value(value: object) -> object:
    """Convert diagnostic values into the JSON-safe representation.

    Contract errors are persisted and returned to callers, so details must
    not retain UUID/date/Decimal objects or provider-owned mutable values.
    This conversion deliberately happens before ``freeze_json`` so every
    error branch can use the same diagnostic shape.
    """

    if value is _DETAIL_MISSING:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _detail_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_detail_value(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    enum_value = getattr(value, "value", _DETAIL_MISSING)
    if enum_value is not _DETAIL_MISSING:
        return _detail_value(enum_value)
    return repr(value)


def _error_details(
    *,
    instrument_id: object = None,
    source: object = None,
    source_code: object = None,
    session_date: object = None,
    expected: object = None,
    actual: object = None,
    data_cutoff: object = None,
    fact_version: object = None,
    **extra: object,
) -> dict[str, object]:
    """Build the common stable diagnostic fields for PIT contract errors.

    The fixed keys make blocked requests machine-locatable even when no
    mapping or Bar exists (their values are then ``None``).  Branch-specific
    evidence is added through ``extra`` without replacing the common
    instrument/source/session/expectation context.
    """

    details: dict[str, object] = {
        "instrument_id": _detail_value(instrument_id),
        "source": _detail_value(source),
        "source_code": _detail_value(source_code),
        "session_date": _detail_value(session_date),
        "expected": _detail_value(expected),
        "actual": _detail_value(actual),
        "data_cutoff": _detail_value(data_cutoff),
        "fact_version": _detail_value(fact_version),
    }
    if session_date is not None:
        # ``session`` is retained as a compact compatibility alias used by
        # existing consumers while ``session_date`` is the canonical field.
        details["session"] = _detail_value(session_date)
    if expected is not None:
        details["expected_value"] = _detail_value(expected)
    if actual is not None:
        details["actual_value"] = _detail_value(actual)
    details.update({key: _detail_value(value) for key, value in extra.items()})
    return details


def _validated_data_cutoff(
    value: object, *, instrument_id: object = None, source: object = None
) -> datetime:
    """Validate a PIT cutoff at the generic data-contract boundary.

    ``_aware_datetime`` is shared with older domain objects and raises the
    legacy ``DomainValidationError``.  The PIT entry points expose the
    stable data-contract hierarchy instead, so malformed values (including
    ``None`` and naive datetimes) are translated here while retaining the
    query coordinates in ``details``.
    """

    if not isinstance(value, datetime):
        raise InvalidDataRequestError(
            "data_cutoff must be a timezone-aware datetime",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                expected="timezone-aware datetime",
                actual=value,
                data_cutoff=value,
            ),
        )
    try:
        return _aware_datetime(value, "data_cutoff")
    except (DomainValidationError, TypeError, ValueError, AttributeError) as exc:
        raise InvalidDataRequestError(
            "data_cutoff must be a timezone-aware datetime",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                expected="timezone-aware datetime",
                actual=value,
                data_cutoff=value,
            ),
        ) from exc

__all__ = [
    "HistoryCompletenessValidator",
    "PITMappingCoverage",
    "PITMappingResolution",
    "PITMappingSegment",
    "ResolvedSessions",
    "SegmentBarEnvelope",
    "SegmentBarEvidence",
    "SegmentBarSummary",
    "SegmentBarReader",
    "SegmentFactorReader",
    "SegmentedAdjustedSeries",
    "SegmentedBarHistory",
    "read_segmented_adjusted_series",
    "read_segmented_history",
    "resolve_resolved_sessions",
    "resolve_pit_mappings",
]


class PITMappingCoverage(StrEnum):
    """Coverage status of a finished mapping resolution."""

    COMPLETE = "complete"


@dataclass(frozen=True, slots=True, init=False)
class ResolvedSessions:
    """Immutable, already-calendar-resolved trading sessions.

    This type deliberately has no ``lookback_sessions`` field or resolver.
    The public compatibility layer resolves a lookback first and passes only
    this value into the PIT history path.
    """

    sessions: tuple[date, ...]

    def __init__(self, sessions: Sequence[date] = (), *, dates: Sequence[date] | None = None) -> None:
        selected = sessions if dates is None else dates
        object.__setattr__(self, "sessions", _validate_sessions(selected, allow_empty=False))

    @property
    def dates(self) -> tuple[date, ...]:
        """Alias used by calendar adapters that call the field ``dates``."""

        return self.sessions

    def __iter__(self):
        return iter(self.sessions)

    def __len__(self) -> int:
        return len(self.sessions)

    def __getitem__(self, item):
        return self.sessions[item]


def resolve_resolved_sessions(
    sessions: Sequence[date], *, data_cutoff: datetime
) -> ResolvedSessions:
    """Validate a calendar-provided session sequence before PIT reads."""

    cutoff = _validated_data_cutoff(data_cutoff)
    ordered = _validate_sessions(sessions, data_cutoff=cutoff)
    future = [day for day in ordered if day > cutoff.date()]
    if future:
        raise DataCutoffExceededError(
            "resolved sessions extend past the data cutoff",
            details=_error_details(
                session_date=future[0],
                expected=f"<= {cutoff.date().isoformat()}",
                actual=future[0],
                data_cutoff=cutoff,
                first_session_past_cutoff=future[0],
            ),
        )
    return ResolvedSessions(ordered)


@dataclass(frozen=True, slots=True, init=False)
class SegmentBarEnvelope:
    """Internal provider envelope retaining source and segment identity.

    ``Bar`` intentionally stays generic and does not expose a source-code
    field.  Segment readers may return this envelope to prove their exact
    request.  Only the original ``(PITMappingResolution, reader)`` adapter
    shape may return bare ``Bar`` facts; the sessions-only shape must carry
    the source-code evidence in this envelope.
    """

    instrument_id: UUID
    source: str
    source_code: str
    bars: tuple[Bar, ...]
    requested_sessions: tuple[date, ...]

    def __init__(
        self,
        instrument_id: UUID,
        source: str,
        source_code: str,
        trade_date: date | None = None,
        bar: Bar | None = None,
        *,
        bars: Sequence[Bar] = (),
        requested_sessions: Sequence[date] = (),
    ) -> None:
        try:
            rows = tuple(bars)
        except DataContractError:
            raise
        except Exception as exc:
            raise ProviderContractViolationError(
                "envelope bars must be a sequence of Bar rows",
                details=_error_details(
                    instrument_id=instrument_id,
                    source=source,
                    source_code=source_code,
                    expected="sequence[Bar]",
                    actual=type(bars).__name__,
                ),
            ) from exc
        if bar is not None:
            rows = rows + (bar,)
        if trade_date is not None:
            if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
                raise ProviderContractViolationError(
                    "envelope trade_date must be a calendar date",
                    details=_error_details(
                        instrument_id=instrument_id,
                        source=source,
                        source_code=source_code,
                        expected="date",
                        actual=trade_date,
                    ),
                )
            if len(rows) > 1:
                raise ProviderContractViolationError(
                    "envelope trade_date is only valid for one-bar envelopes",
                    details=_error_details(
                        instrument_id=instrument_id,
                        source=source,
                        source_code=source_code,
                        session_date=trade_date,
                        expected="one bar",
                        actual=len(rows),
                    ),
                )
            if rows and isinstance(rows[0], Bar) and rows[0].trade_date != trade_date:
                raise ProviderContractViolationError(
                    "envelope trade_date does not match its bar trade_date",
                    details=_error_details(
                        instrument_id=instrument_id,
                        source=source,
                        source_code=source_code,
                        session_date=trade_date,
                        expected=trade_date,
                        actual=rows[0].trade_date,
                    ),
                )
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "source", source.strip() if isinstance(source, str) else source)
        object.__setattr__(
            self,
            "source_code",
            source_code.strip() if isinstance(source_code, str) else source_code,
        )
        object.__setattr__(self, "bars", rows)
        if trade_date is not None:
            try:
                declared_sessions = tuple(requested_sessions)
            except DataContractError:
                raise
            except Exception as exc:
                raise ProviderContractViolationError(
                    "envelope requested_sessions must be a sequence of dates",
                    details=_error_details(
                        instrument_id=instrument_id,
                        source=source,
                        source_code=source_code,
                        expected="sequence[date]",
                        actual=type(requested_sessions).__name__,
                    ),
                ) from exc
            if declared_sessions and declared_sessions != (trade_date,):
                raise ProviderContractViolationError(
                    "envelope trade_date and requested_sessions disagree",
                    details=_error_details(
                        instrument_id=instrument_id,
                        source=source,
                        source_code=source_code,
                        session_date=trade_date,
                        expected=(trade_date,),
                        actual=declared_sessions,
                    ),
                )
            selected_sessions = (trade_date,)
        else:
            try:
                selected_sessions = tuple(requested_sessions)
            except DataContractError:
                raise
            except Exception as exc:
                raise ProviderContractViolationError(
                    "envelope requested_sessions must be a sequence of dates",
                    details=_error_details(
                        instrument_id=instrument_id,
                        source=source,
                        source_code=source_code,
                        expected="sequence[date]",
                        actual=type(requested_sessions).__name__,
                    ),
                ) from exc
        object.__setattr__(self, "requested_sessions", selected_sessions)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise ProviderContractViolationError(
                "envelope instrument_id must be a UUID",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="UUID",
                    actual=type(self.instrument_id).__name__,
                ),
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise ProviderContractViolationError(
                "envelope source must be non-blank",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="non-blank source",
                    actual=self.source,
                ),
            )
        if not isinstance(self.source_code, str) or not self.source_code.strip():
            raise ProviderContractViolationError(
                "envelope source_code must be non-blank",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="non-blank source_code",
                    actual=self.source_code,
                ),
            )
        if not isinstance(self.bars, tuple) or any(
            not isinstance(row, Bar) for row in self.bars
        ):
            raise ProviderContractViolationError(
                "envelope bars must contain only Bar rows",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="tuple[Bar]",
                    actual=[type(row).__name__ for row in self.bars]
                    if isinstance(self.bars, tuple)
                    else type(self.bars).__name__,
                ),
            )
        if not self.requested_sessions:
            raise ProviderContractViolationError(
                "envelope requested_sessions must be explicitly declared",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="at least one requested session",
                    actual=self.requested_sessions,
                ),
            )
        try:
            validated_sessions = _validate_sessions(self.requested_sessions)
        except InvalidDataRequestError as exc:
            raise ProviderContractViolationError(
                "envelope requested_sessions must be distinct ascending dates",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="distinct ascending dates",
                    actual=self.requested_sessions,
                ),
            ) from exc
        object.__setattr__(self, "requested_sessions", validated_sessions)
        for row in self.bars:
            if row.instrument_id != self.instrument_id:
                raise HistoryBarInstrumentMismatchError(
                    "envelope bar instrument_id does not match the requested identity",
                    details={
                        "instrument_id": str(self.instrument_id),
                        "source": self.source,
                        "expected_instrument_id": str(self.instrument_id),
                        "returned_instrument_id": str(row.instrument_id),
                        "source_code": self.source_code,
                        "session_date": row.trade_date.isoformat(),
                        "session": row.trade_date.isoformat(),
                        "expected": str(self.instrument_id),
                        "actual": str(row.instrument_id),
                        "data_cutoff": None,
                        "fact_version": None,
                    },
                )
        for day in self.requested_sessions:
            if not isinstance(day, date) or isinstance(day, datetime):
                raise ProviderContractViolationError(
                    "envelope requested_sessions must contain calendar dates",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=self.source,
                        source_code=self.source_code,
                        expected="calendar date",
                        actual=day,
                    ),
                )

    @property
    def trade_date(self) -> date | None:
        """Compatibility accessor for a one-bar envelope."""

        if len(self.bars) == 1:
            return self.bars[0].trade_date
        return None

    @property
    def bar(self) -> Bar | None:
        """Compatibility accessor for a one-bar envelope."""

        return self.bars[0] if len(self.bars) == 1 else None


@dataclass(frozen=True, slots=True)
class SegmentBarEvidence:
    """Immutable evidence summary for one source-code read segment."""

    source: str
    source_code: str
    first_session: date
    last_session: date
    requested_count: int
    returned_dates: tuple[date, ...]
    coverage_status: str = PITMappingCoverage.COMPLETE.value
    fact_id: UUID | None = None
    fact_version: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    mapping_evidence: str | None = None

    def __post_init__(self) -> None:
        """Validate and recursively freeze one public segment evidence row.

        Result objects cross the provider boundary and are frequently kept in
        run snapshots.  ``frozen=True`` only protects the top-level dataclass;
        callers can still mutate a list passed as ``returned_dates`` unless it
        is copied here.  The same validation is intentionally performed for
        the public alias below so both construction paths have one contract.
        """

        normalized = _normalize_segment_evidence(
            source=self.source,
            source_code=self.source_code,
            first_session=self.first_session,
            last_session=self.last_session,
            requested_count=self.requested_count,
            returned_dates=self.returned_dates,
            coverage_status=self.coverage_status,
            fact_id=self.fact_id,
            fact_version=self.fact_version,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            mapping_evidence=self.mapping_evidence,
        )
        for name, value in normalized.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class SegmentBarSummary:
    """Public alias retained for callers naming segment evidence summaries."""

    source: str
    source_code: str
    first_session: date
    last_session: date
    requested_count: int
    returned_dates: tuple[date, ...]
    coverage_status: str = PITMappingCoverage.COMPLETE.value
    fact_id: UUID | None = None
    fact_version: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    mapping_evidence: str | None = None

    def __post_init__(self) -> None:
        """Apply the same immutable evidence contract as ``SegmentBarEvidence``."""

        normalized = _normalize_segment_evidence(
            source=self.source,
            source_code=self.source_code,
            first_session=self.first_session,
            last_session=self.last_session,
            requested_count=self.requested_count,
            returned_dates=self.returned_dates,
            coverage_status=self.coverage_status,
            fact_id=self.fact_id,
            fact_version=self.fact_version,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            mapping_evidence=self.mapping_evidence,
        )
        for name, value in normalized.items():
            object.__setattr__(self, name, value)


def _normalize_segment_evidence(
    *,
    source: object,
    source_code: object,
    first_session: object,
    last_session: object,
    requested_count: object,
    returned_dates: object,
    coverage_status: object,
    fact_id: object,
    fact_version: object,
    valid_from: object,
    valid_to: object,
    mapping_evidence: object,
) -> dict[str, object]:
    """Normalize the fields shared by the two segment-evidence DTOs."""

    if not isinstance(source, str) or not source.strip():
        raise ProviderContractViolationError(
            "segment evidence source must be non-blank",
            details=_error_details(
                source=source, expected="non-blank source", actual=source
            ),
        )
    if not isinstance(source_code, str) or not source_code.strip():
        raise ProviderContractViolationError(
            "segment evidence source_code must be non-blank",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="non-blank source_code",
                actual=source_code,
            ),
        )
    for field_name, value in (
        ("first_session", first_session),
        ("last_session", last_session),
    ):
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ProviderContractViolationError(
                f"segment evidence {field_name} must be a calendar date",
                details=_error_details(
                    source=source,
                    source_code=source_code,
                    session_date=value,
                    expected="date",
                    actual=value,
                ),
            )
    if last_session < first_session:
        raise ProviderContractViolationError(
            "segment evidence last_session cannot precede first_session",
            details=_error_details(
                source=source,
                source_code=source_code,
                session_date=first_session,
                expected=f">= {first_session.isoformat()}",
                actual=last_session,
            ),
        )
    if isinstance(requested_count, bool) or not isinstance(requested_count, int):
        raise ProviderContractViolationError(
            "segment evidence requested_count must be an integer",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="positive integer",
                actual=requested_count,
            ),
        )
    if requested_count < 1:
        raise ProviderContractViolationError(
            "segment evidence requested_count must be positive",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="> 0",
                actual=requested_count,
            ),
        )
    if isinstance(returned_dates, (str, bytes)):
        raise ProviderContractViolationError(
            "segment evidence returned_dates must be a sequence of dates",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="sequence[date]",
                actual=type(returned_dates).__name__,
            ),
        )
    try:
        returned = tuple(returned_dates)  # type: ignore[arg-type]
    except Exception as exc:
        raise ProviderContractViolationError(
            "segment evidence returned_dates must be a sequence of dates",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="sequence[date]",
                actual=type(returned_dates).__name__,
            ),
        ) from exc
    for day in returned:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ProviderContractViolationError(
                "segment evidence returned_dates must contain dates",
                details=_error_details(
                    source=source,
                    source_code=source_code,
                    session_date=day,
                    expected="date",
                    actual=day,
                ),
            )
    if len(returned) != requested_count:
        raise ProviderContractViolationError(
            "segment evidence returned_dates count must match requested_count",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected=requested_count,
                actual=len(returned),
            ),
        )
    if len(set(returned)) != len(returned):
        raise HistoryBarsDuplicateError(
            "segment evidence returned_dates contains duplicates",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="one date per returned bar",
                actual=returned,
            ),
        )
    if tuple(sorted(returned)) != returned:
        raise ProviderContractViolationError(
            "segment evidence returned_dates must be ascending",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="ascending dates",
                actual=returned,
            ),
        )
    if returned and (returned[0] != first_session or returned[-1] != last_session):
        raise ProviderContractViolationError(
            "segment evidence first/last session does not match returned dates",
            details=_error_details(
                source=source,
                source_code=source_code,
                session_date=first_session,
                expected={"first_session": returned[0], "last_session": returned[-1]},
                actual={"first_session": first_session, "last_session": last_session},
            ),
        )
    try:
        status = PITMappingCoverage(getattr(coverage_status, "value", coverage_status))
    except (TypeError, ValueError) as exc:
        raise ProviderContractViolationError(
            "segment evidence coverage_status must be complete",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected=PITMappingCoverage.COMPLETE.value,
                actual=coverage_status,
            ),
        ) from exc
    if fact_id is not None and not isinstance(fact_id, UUID):
        raise ProviderContractViolationError(
            "segment evidence fact_id must be a UUID",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="UUID or None",
                actual=fact_id,
            ),
        )
    if fact_version is not None and (
        isinstance(fact_version, bool)
        or not isinstance(fact_version, int)
        or fact_version < 1
    ):
        raise ProviderContractViolationError(
            "segment evidence fact_version must be positive",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="positive integer or None",
                actual=fact_version,
            ),
        )
    if valid_from is not None and (
        not isinstance(valid_from, date) or isinstance(valid_from, datetime)
    ):
        raise ProviderContractViolationError(
            "segment evidence valid_from must be a calendar date",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="date",
                actual=valid_from,
            ),
        )
    if valid_to is not None:
        if not isinstance(valid_to, date) or isinstance(valid_to, datetime):
            raise ProviderContractViolationError(
                "segment evidence valid_to must be a calendar date",
                details=_error_details(
                    source=source,
                    source_code=source_code,
                    expected="date or None",
                    actual=valid_to,
                ),
            )
        if valid_from is None:
            raise ProviderContractViolationError(
                "segment evidence valid_to requires valid_from",
                details=_error_details(
                    source=source,
                    source_code=source_code,
                    expected="valid_from when valid_to is provided",
                    actual=valid_from,
                ),
            )
        if valid_to <= valid_from:
            raise ProviderContractViolationError(
                "segment evidence valid_to must be later than valid_from",
                details=_error_details(
                    source=source,
                    source_code=source_code,
                    expected=f"> {valid_from.isoformat()}",
                    actual=valid_to,
                ),
            )
    if not isinstance(mapping_evidence, str) or not mapping_evidence.strip():
        raise IdentityMappingEvidenceMissingError(
            "segment evidence must include the selected mapping evidence",
            details=_error_details(
                source=source,
                source_code=source_code,
                expected="non-blank mapping evidence",
                actual=mapping_evidence,
            ),
        )
    return {
        "source": source.strip(),
        "source_code": source_code.strip(),
        "first_session": first_session,
        "last_session": last_session,
        "requested_count": requested_count,
        "returned_dates": returned,
        "coverage_status": status.value,
        "fact_id": fact_id,
        "fact_version": fact_version,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "mapping_evidence": mapping_evidence.strip()
        if isinstance(mapping_evidence, str)
        else mapping_evidence,
    }


@dataclass(frozen=True, slots=True)
class HistoryCompletenessValidator:
    """Checks that one segment returned exactly one fact per session.

    Shared by the bar and adjustment-factor read paths so both enforce the
    same completeness rule: a missing session blocks instead of shrinking
    the window, and a repeated session blocks instead of being deduplicated
    silently.
    """

    source_code: str
    expected_sessions: tuple[date, ...]
    instrument_id: UUID | None = None
    source: str | None = None
    data_cutoff: datetime | None = None
    fact_version: int | None = None

    def __post_init__(self) -> None:
        """Freeze validator input so validation cannot observe later mutations."""

        if not isinstance(self.source_code, str) or not self.source_code.strip():
            raise InvalidDataRequestError(
                "source_code must be non-blank text",
                details=_error_details(
                    source=self.source,
                    source_code=self.source_code,
                    expected="non-blank source_code",
                    actual=self.source_code,
                    data_cutoff=self.data_cutoff,
                    fact_version=self.fact_version,
                ),
            )
        object.__setattr__(self, "source_code", self.source_code.strip())
        if self.instrument_id is not None and not isinstance(self.instrument_id, UUID):
            raise InvalidDataRequestError(
                "instrument_id must be a UUID when provided",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="UUID or None",
                    actual=self.instrument_id,
                    data_cutoff=self.data_cutoff,
                    fact_version=self.fact_version,
                ),
            )
        if self.source is not None:
            if not isinstance(self.source, str) or not self.source.strip():
                raise InvalidDataRequestError(
                    "source must be non-blank text when provided",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=self.source,
                        source_code=self.source_code,
                        expected="non-blank source or None",
                        actual=self.source,
                        data_cutoff=self.data_cutoff,
                        fact_version=self.fact_version,
                    ),
                )
            object.__setattr__(self, "source", self.source.strip())
        cutoff = self.data_cutoff
        if self.data_cutoff is not None:
            cutoff = _validated_data_cutoff(
                self.data_cutoff,
                instrument_id=self.instrument_id,
                source=self.source,
            )
            object.__setattr__(self, "data_cutoff", cutoff)
        expected = _validate_sessions(
            self.expected_sessions,
            instrument_id=self.instrument_id,
            source=self.source,
            data_cutoff=cutoff,
        )
        object.__setattr__(self, "expected_sessions", expected)
        if self.fact_version is not None and (
            isinstance(self.fact_version, bool)
            or not isinstance(self.fact_version, int)
            or self.fact_version < 1
        ):
            raise InvalidDataRequestError(
                "fact_version must be positive when provided",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="positive integer or None",
                    actual=self.fact_version,
                    data_cutoff=self.data_cutoff,
                    fact_version=self.fact_version,
                ),
            )

    def validate(self, returned_dates: Sequence[date]) -> None:
        """Block unless ``returned_dates`` matches the expected sessions."""

        if isinstance(returned_dates, (str, bytes)):
            raise ProviderContractViolationError(
                "segment reader returned_dates must be a sequence of dates",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="sequence[date]",
                    actual=type(returned_dates).__name__,
                    data_cutoff=self.data_cutoff,
                    fact_version=self.fact_version,
                ),
            )
        try:
            returned = tuple(returned_dates)
        except DataContractError:
            raise
        except Exception as exc:
            raise ProviderContractViolationError(
                "segment reader returned_dates must be a sequence of dates",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    expected="sequence[date]",
                    actual=type(returned_dates).__name__,
                    data_cutoff=self.data_cutoff,
                    fact_version=self.fact_version,
                    reason=str(exc),
                ),
            ) from exc
        expected_set = set(self.expected_sessions)
        seen: set[date] = set()
        for day in returned:
            if day not in expected_set:
                raise HistoryBarsIncompleteError(
                    "segment reader returned a fact outside the requested "
                    "sessions",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=self.source,
                        source_code=self.source_code,
                        session_date=day,
                        expected=self.expected_sessions,
                        actual=day,
                        data_cutoff=self.data_cutoff,
                        fact_version=self.fact_version,
                    ),
                )
            if day in seen:
                raise HistoryBarsDuplicateError(
                    "segment reader returned a duplicate fact for one session",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=self.source,
                        source_code=self.source_code,
                        session_date=day,
                        expected="one fact per session",
                        actual="duplicate",
                        data_cutoff=self.data_cutoff,
                        fact_version=self.fact_version,
                    ),
                )
            seen.add(day)
        missing = [day for day in self.expected_sessions if day not in seen]
        if missing:
            raise HistoryBarsIncompleteError(
                "segment reader returned no fact for every requested session",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    source_code=self.source_code,
                    session_date=missing[0],
                    expected=self.expected_sessions,
                    actual=tuple(seen),
                    data_cutoff=self.data_cutoff,
                    fact_version=self.fact_version,
                    missing_session_count=len(missing),
                    first_missing_session=missing[0],
                ),
            )


@dataclass(frozen=True, slots=True)
class PITMappingSegment:
    """One half-open identity segment clamped to the requested sessions.

    ``source_code`` is the only code used for a historical read.  The
    optional ``trading_code`` field is retained solely as a migration-era
    compatibility mirror for audit consumers; it is never required for
    validation, grouping, or provider calls.
    """

    source_code: str
    valid_from: date
    valid_to: date | None
    requested_sessions: tuple[date, ...]
    mapping: InstrumentCodeMapping
    trading_code: str | None = None

    def __post_init__(self) -> None:
        """Validate and freeze one segment before it enters a resolution."""

        if not isinstance(self.source_code, str) or not self.source_code.strip():
            raise InvalidDataRequestError(
                "segment source_code must be non-blank text",
                details=_error_details(
                    instrument_id=getattr(self.mapping, "instrument_id", None),
                    source=getattr(self.mapping, "source", None),
                    source_code=self.source_code,
                    expected="non-blank source_code",
                    actual=self.source_code,
                    fact_version=getattr(self.mapping, "fact_version", None),
                ),
            )
        if not isinstance(self.valid_from, date) or isinstance(self.valid_from, datetime):
            raise InvalidDataRequestError(
                "segment valid_from must be a calendar date",
                details=_error_details(
                    instrument_id=getattr(self.mapping, "instrument_id", None),
                    source=getattr(self.mapping, "source", None),
                    source_code=self.source_code,
                    expected="date",
                    actual=self.valid_from,
                    fact_version=getattr(self.mapping, "fact_version", None),
                ),
            )
        if self.valid_to is not None:
            if not isinstance(self.valid_to, date) or isinstance(self.valid_to, datetime):
                raise InvalidDataRequestError(
                    "segment valid_to must be a calendar date",
                    details=_error_details(
                        instrument_id=getattr(self.mapping, "instrument_id", None),
                        source=getattr(self.mapping, "source", None),
                        source_code=self.source_code,
                        expected="date or None",
                        actual=self.valid_to,
                        fact_version=getattr(self.mapping, "fact_version", None),
                    ),
                )
            if self.valid_to <= self.valid_from:
                raise InvalidDataRequestError(
                    "segment valid_to must be later than valid_from",
                    details=_error_details(
                        instrument_id=getattr(self.mapping, "instrument_id", None),
                        source=getattr(self.mapping, "source", None),
                        source_code=self.source_code,
                        expected=f"> {self.valid_from.isoformat()}",
                        actual=self.valid_to,
                        fact_version=getattr(self.mapping, "fact_version", None),
                    ),
                )
        if not isinstance(self.mapping, InstrumentCodeMapping):
            raise InvalidDataRequestError(
                "segment mapping must be an InstrumentCodeMapping",
                details=_error_details(
                    source_code=self.source_code,
                    expected="InstrumentCodeMapping",
                    actual=type(self.mapping).__name__,
                ),
            )
        if (
            self.source_code.strip() != self.mapping.source_code
            or self.valid_from != self.mapping.valid_from
            or self.valid_to != self.mapping.valid_to
        ):
            raise InvalidDataRequestError(
                "segment fields must match the selected mapping fact",
                details=_error_details(
                    instrument_id=self.mapping.instrument_id,
                    source=self.mapping.source,
                    source_code=self.source_code,
                    expected={
                        "source_code": self.mapping.source_code,
                        "valid_from": self.mapping.valid_from,
                        "valid_to": self.mapping.valid_to,
                    },
                    actual={
                        "source_code": self.source_code,
                        "valid_from": self.valid_from,
                        "valid_to": self.valid_to,
                    },
                    fact_version=getattr(self.mapping, "fact_version", None),
                ),
            )
        object.__setattr__(self, "source_code", self.source_code.strip())
        if self.trading_code is not None:
            if not isinstance(self.trading_code, str):
                raise InvalidDataRequestError(
                    "segment trading_code compatibility mirror must be text or None",
                    details=_error_details(
                        instrument_id=self.mapping.instrument_id,
                        source=self.mapping.source,
                        source_code=self.source_code,
                        expected="text or None",
                        actual=self.trading_code,
                        fact_version=getattr(self.mapping, "fact_version", None),
                    ),
                )
            mirror = self.trading_code.strip()
            object.__setattr__(self, "trading_code", mirror or None)
        object.__setattr__(
            self, "requested_sessions", _validate_sessions(self.requested_sessions)
        )
        for day in self.requested_sessions:
            if not self.mapping.covers(day):
                raise IdentityMappingIncompleteError(
                    "segment requested session is outside its mapping validity",
                    details=_error_details(
                        instrument_id=self.mapping.instrument_id,
                        source=self.mapping.source,
                        source_code=self.source_code,
                        session_date=day,
                        expected="mapping validity interval",
                        actual=day,
                        data_cutoff=self.mapping.known_at,
                        fact_version=getattr(self.mapping, "fact_version", None),
                    ),
                )

    @property
    def fact_id(self) -> UUID | None:
        """Stable mapping-fact identifier used for audit and grouping."""

        return getattr(self.mapping, "fact_id", None)

    @property
    def fact_version(self) -> int:
        """Mapping revision selected for this segment."""

        return getattr(self.mapping, "fact_version", 1)

    @property
    def first_requested_session(self) -> date:
        return self.requested_sessions[0]

    @property
    def last_requested_session(self) -> date:
        return self.requested_sessions[-1]


@dataclass(frozen=True, slots=True)
class PITMappingResolution:
    """Auditable result of resolving one instrument over fixed sessions.

    ``session_bindings`` maps every requested session to exactly one
    source code.  The object expresses the resolution outcome only; it
    never carries a default or fallback code for uncovered sessions —
    uncovered requests fail with a stable error instead.
    """

    instrument_id: UUID
    source: str
    requested_sessions: tuple[date, ...]
    segments: tuple[PITMappingSegment, ...]
    session_bindings: Mapping[date, str]
    data_cutoff: datetime
    coverage_status: PITMappingCoverage
    evidence_summary: Mapping[str, object]
    # Optional market-local calendar date corresponding to ``data_cutoff``.
    # Generic callers retain the legacy UTC-surface date; asset adapters can
    # bind their own confirmed market timezone without changing the instant
    # used for PIT mapping visibility.
    session_cutoff_date: date | None = None

    def __post_init__(self) -> None:
        """Validate and deep-freeze externally supplied resolutions.

        Resolutions are often passed across provider boundaries.  A frozen
        dataclass alone does not protect nested lists or dictionaries, so the
        constructor verifies every binding and replaces mutable containers
        with immutable equivalents before a reader can use them.
        """

        if not isinstance(self.instrument_id, UUID):
            raise InvalidDataRequestError(
                "resolution instrument_id must be a UUID",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    expected="UUID",
                    actual=type(self.instrument_id).__name__,
                ),
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidDataRequestError(
                "resolution source must be non-blank text",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=self.source,
                    expected="non-blank source",
                    actual=self.source,
                    data_cutoff=self.data_cutoff,
                ),
            )
        source = self.source.strip()
        object.__setattr__(self, "source", source)
        # Resolve the cutoff before validating sessions or mapping evidence so
        # malformed/naive values at this public boundary consistently use the
        # stable data-contract error hierarchy instead of leaking the legacy
        # ``DomainValidationError`` from the shared datetime helper.
        cutoff = _validated_data_cutoff(
            self.data_cutoff,
            instrument_id=self.instrument_id,
            source=source,
        )
        ordered = _validate_sessions(
            self.requested_sessions,
            instrument_id=self.instrument_id,
            source=source,
            data_cutoff=cutoff,
        )
        object.__setattr__(self, "requested_sessions", ordered)
        object.__setattr__(self, "data_cutoff", cutoff)
        session_cutoff = self.session_cutoff_date
        if session_cutoff is None:
            session_cutoff = cutoff.date()
        elif not isinstance(session_cutoff, date) or isinstance(session_cutoff, datetime):
            raise InvalidDataRequestError(
                "resolution session_cutoff_date must be a calendar date"
            )
        object.__setattr__(self, "session_cutoff_date", session_cutoff)
        future = [day for day in ordered if day > session_cutoff]
        if future:
            raise DataCutoffExceededError(
                "resolved sessions extend past the data cutoff",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    session_date=future[0],
                    expected=f"<= {session_cutoff.isoformat()}",
                    actual=future[0],
                    data_cutoff=cutoff,
                    first_session_past_cutoff=future[0],
                ),
            )
        try:
            coverage = PITMappingCoverage(self.coverage_status)
        except (TypeError, ValueError) as exc:
            raise InvalidDataRequestError(
                "resolution coverage_status must be complete",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    expected=PITMappingCoverage.COMPLETE.value,
                    actual=self.coverage_status,
                    data_cutoff=cutoff,
                ),
            ) from exc
        if coverage is not PITMappingCoverage.COMPLETE:
            raise InvalidDataRequestError(
                "an incomplete PIT mapping resolution cannot be read",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    expected=PITMappingCoverage.COMPLETE.value,
                    actual=coverage,
                    data_cutoff=cutoff,
                ),
            )
        object.__setattr__(self, "coverage_status", coverage)
        try:
            segments = tuple(self.segments)
        except DataContractError:
            raise
        except Exception as exc:
            raise InvalidDataRequestError("resolution segments must be a sequence") from exc
        if not segments:
            raise IdentityMappingIncompleteError(
                "a complete PIT mapping resolution must contain segments",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    expected="at least one mapping segment",
                    actual=0,
                    data_cutoff=cutoff,
                ),
            )
        object.__setattr__(self, "segments", segments)
        expected_set = set(ordered)
        derived_bindings: dict[date, str] = {}
        for segment in segments:
            if not isinstance(segment, PITMappingSegment):
                raise InvalidDataRequestError(
                    "resolution segments must contain PITMappingSegment instances",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=source,
                        expected="PITMappingSegment",
                        actual=type(segment).__name__,
                        data_cutoff=cutoff,
                    ),
                )
            mapping = segment.mapping
            if mapping.instrument_id != self.instrument_id:
                raise IdentityMappingIncompleteError(
                    "resolution segment mapping has another instrument_id",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=source,
                        source_code=segment.source_code,
                        session_date=segment.requested_sessions[0]
                        if segment.requested_sessions
                        else None,
                        expected=self.instrument_id,
                        actual=mapping.instrument_id,
                        data_cutoff=cutoff,
                        fact_version=getattr(mapping, "fact_version", None),
                        mapping_instrument_id=mapping.instrument_id,
                    ),
                )
            if _source_key(mapping.source) != _source_key(source):
                raise IdentityMappingIncompleteError(
                    "resolution segment mapping has another source",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=source,
                        source_code=segment.source_code,
                        session_date=segment.requested_sessions[0]
                        if segment.requested_sessions
                        else None,
                        expected=source,
                        actual=mapping.source,
                        data_cutoff=cutoff,
                        fact_version=getattr(mapping, "fact_version", None),
                        mapping_source=mapping.source,
                    ),
                )
            if _aware_datetime(mapping.known_at, "mapping.known_at") > cutoff:
                raise DataCutoffExceededError(
                    "resolution contains a mapping learned after the data cutoff",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=source,
                        source_code=segment.source_code,
                        session_date=segment.requested_sessions[0]
                        if segment.requested_sessions
                        else None,
                        expected=f"known_at <= {cutoff.isoformat()}",
                        actual=mapping.known_at,
                        data_cutoff=cutoff,
                        fact_version=getattr(mapping, "fact_version", None),
                        known_at=mapping.known_at,
                    ),
                )
            if not isinstance(mapping.evidence, str) or not mapping.evidence.strip():
                raise IdentityMappingEvidenceMissingError(
                    "resolution contains a mapping without evidence",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=source,
                        source_code=segment.source_code,
                        session_date=segment.requested_sessions[0]
                        if segment.requested_sessions
                        else None,
                        expected="non-blank evidence",
                        actual=mapping.evidence,
                        data_cutoff=cutoff,
                        fact_version=getattr(mapping, "fact_version", None),
                    ),
                )
            for day in segment.requested_sessions:
                if day not in expected_set:
                    raise IdentityMappingIncompleteError(
                        "resolution segment contains an unrequested session",
                        details=_error_details(
                            instrument_id=self.instrument_id,
                            source=source,
                            source_code=segment.source_code,
                            session_date=day,
                            expected="requested session",
                            actual=day,
                            data_cutoff=cutoff,
                            fact_version=getattr(mapping, "fact_version", None),
                        ),
                    )
                if day in derived_bindings:
                    raise IdentityMappingConflictError(
                        "resolution segments overlap on one requested session",
                        details=_error_details(
                            instrument_id=self.instrument_id,
                            source=source,
                            source_code=segment.source_code,
                            session_date=day,
                            expected="one segment binding",
                            actual="multiple segment bindings",
                            data_cutoff=cutoff,
                            fact_version=getattr(mapping, "fact_version", None),
                            existing_source_code=derived_bindings[day],
                        ),
                    )
                derived_bindings[day] = segment.source_code
        if set(derived_bindings) != expected_set:
            missing = [day for day in ordered if day not in derived_bindings]
            raise IdentityMappingIncompleteError(
                "resolution segments do not cover every requested session",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    session_date=missing[0] if missing else None,
                    expected=ordered,
                    actual=tuple(derived_bindings),
                    data_cutoff=cutoff,
                    fact_version=None,
                    missing_sessions=missing,
                ),
            )
        if not isinstance(self.session_bindings, Mapping):
            raise InvalidDataRequestError(
                "resolution session_bindings must be a mapping",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    expected="mapping[date, source_code]",
                    actual=type(self.session_bindings).__name__,
                    data_cutoff=cutoff,
                ),
            )
        supplied_bindings: dict[date, str] = {}
        for day, source_code in self.session_bindings.items():
            if not isinstance(day, date) or isinstance(day, datetime):
                raise InvalidDataRequestError(
                    "resolution session_bindings keys must be calendar dates",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=source,
                        source_code=source_code,
                        session_date=day,
                        expected="date",
                        actual=type(day).__name__,
                        data_cutoff=cutoff,
                    ),
                )
            if not isinstance(source_code, str) or not source_code.strip():
                raise InvalidDataRequestError(
                    "resolution session_bindings values must be non-blank text",
                    details=_error_details(
                        instrument_id=self.instrument_id,
                        source=source,
                        source_code=source_code,
                        session_date=day,
                        expected="non-blank source_code",
                        actual=source_code,
                        data_cutoff=cutoff,
                    ),
                )
            supplied_bindings[day] = source_code.strip()
        if supplied_bindings != derived_bindings:
            raise IdentityMappingConflictError(
                "resolution session_bindings do not match its segments",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    source_code=None,
                    session_date=None,
                    expected=derived_bindings,
                    actual=supplied_bindings,
                    data_cutoff=cutoff,
                    fact_version=None,
                ),
            )
        object.__setattr__(self, "session_bindings", MappingProxyType(supplied_bindings))
        if not isinstance(self.evidence_summary, Mapping):
            raise InvalidDataRequestError(
                "resolution evidence_summary must be a mapping",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    expected="mapping",
                    actual=type(self.evidence_summary).__name__,
                    data_cutoff=cutoff,
                ),
            )
        try:
            frozen_summary = freeze_json(self.evidence_summary, "evidence_summary")
        except ValueError as exc:
            raise InvalidDataRequestError(
                f"resolution evidence_summary must contain only JSON values: {exc}",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    expected="JSON mapping",
                    actual=self.evidence_summary,
                    data_cutoff=cutoff,
                ),
            ) from exc
        if not isinstance(frozen_summary, Mapping):
            raise InvalidDataRequestError(
                "resolution evidence_summary must be a mapping",
                details=_error_details(
                    instrument_id=self.instrument_id,
                    source=source,
                    expected="mapping",
                    actual=type(frozen_summary).__name__,
                    data_cutoff=cutoff,
                ),
            )
        object.__setattr__(self, "evidence_summary", frozen_summary)

    @property
    def resolved_sessions(self) -> tuple[date, ...]:
        """Alias exposing the immutable session input under its new name."""

        return self.requested_sessions


class SegmentBarReader(Protocol):
    """Structural source of bars for one source-code segment window."""

    def read_bars(
        self,
        source_code: str,
        start_date: date,
        end_date: date,
    ) -> Sequence[object]:
        """Return bars for exactly the sessions inside ``[start, end]``."""
        ...


def _mapping_provider_error_details(
    error: DataContractError,
    *,
    instrument_id: UUID,
    source: str,
    data_cutoff: datetime,
    session_date: object = None,
) -> dict[str, object]:
    """Preserve provider diagnostics while completing the PIT error shape.

    Mapping repositories may identify the exact conflicting or missing
    source code, session, expected/actual interval, and fact version.  The
    adapter must not discard that evidence when translating repository
    exceptions into the generic PIT error hierarchy.  Older providers may
    not expose ``details`` at all, so the query coordinates are supplied as
    defaults for every stable public field.
    """

    source_details = dict(getattr(error, "details", {}) or {})
    # Preserve aliases emitted by older mapping providers while exposing the
    # canonical field names required by the current PIT contract.
    for canonical, aliases in (
        ("session_date", ("session",)),
        ("expected", ("expected_value",)),
        ("actual", ("actual_value",)),
    ):
        if canonical not in source_details:
            for alias in aliases:
                if alias in source_details:
                    source_details[canonical] = source_details[alias]
                    break
    common = _error_details(
        instrument_id=instrument_id,
        source=source,
        session_date=session_date,
        data_cutoff=data_cutoff,
    )
    for key, value in common.items():
        if key not in source_details or source_details[key] is None:
            source_details[key] = value
    if source_details.get("session_date") is not None:
        source_details.setdefault("session", source_details["session_date"])
    source_details.setdefault("reason", str(error))
    # Provider exceptions may carry typed diagnostic values (dates, UUIDs,
    # enums, or nested containers).  Normalize them before the details are
    # passed to another stable DataContractError constructor.
    return {
        str(key): _detail_value(value)
        for key, value in source_details.items()
    }


def _enrich_provider_contract_error(
    error: DataContractError,
    *,
    defaults: Mapping[str, object],
) -> None:
    """Complete provider-raised details without changing its stable code."""

    details = {
        str(key): _detail_value(value)
        for key, value in dict(getattr(error, "details", {}) or {}).items()
    }
    for key, value in defaults.items():
        if key not in details or details[key] is None:
            details[key] = _detail_value(value)
    frozen = freeze_json(details, "details")
    if isinstance(frozen, Mapping):
        error.details = frozen


def _validate_sessions(
    sessions: Sequence[date],
    *,
    allow_empty: bool = False,
    instrument_id: object = None,
    source: object = None,
    data_cutoff: object = None,
) -> tuple[date, ...]:
    """Require a distinct ascending immutable calendar-session sequence.

    The optional query coordinates are used by public PIT entry points to
    make invalid-request diagnostics actionable.  Constructors and generic
    calendar helpers may omit them when no identity/cutoff is available.
    """

    def details(**extra: object) -> dict[str, object]:
        """Attach the caller's query context to every session error."""

        return _error_details(
            instrument_id=instrument_id,
            source=source,
            data_cutoff=data_cutoff,
            **extra,
        )

    if isinstance(sessions, (str, bytes)) or not isinstance(sessions, Sequence):
        raise InvalidDataRequestError(
            "resolved_sessions must be an immutable sequence of calendar dates",
            details=details(
                expected="sequence[date]",
                actual=type(sessions).__name__,
            ),
        )

    try:
        ordered = tuple(sessions)
    except DataContractError:
        raise
    except Exception as exc:
        raise InvalidDataRequestError(
            "resolved_sessions could not be iterated",
            details=details(
                expected="sequence[date]",
                actual=type(sessions).__name__,
                reason=str(exc),
            ),
        ) from exc
    if not ordered and not allow_empty:
        raise InvalidDataRequestError(
            "resolved_sessions must contain at least one trading session",
            details=details(
                expected="at least one session",
                actual=ordered,
            ),
        )
    for day in ordered:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise InvalidDataRequestError(
                "requested sessions must be calendar dates",
                details=details(
                    session_date=day,
                    expected="date",
                    actual=repr(day),
                ),
            )
    for earlier, later in zip(ordered, ordered[1:]):
        if later <= earlier:
            raise InvalidDataRequestError(
                "resolved_sessions must be distinct dates in ascending order",
                details=details(
                    session_date=later,
                    expected=f"> {earlier.isoformat()}",
                    actual=later,
                    previous_session=earlier,
                    sessions=ordered,
                ),
            )
    return ordered


def resolve_pit_mappings(
    instrument_id: UUID,
    *,
    source: str,
    sessions: Sequence[date],
    mappings: Sequence[InstrumentCodeMapping],
    data_cutoff: datetime,
    session_cutoff_date: date | None = None,
) -> PITMappingResolution:
    """Bind every requested session to exactly one evidenced source code.

    ``mappings`` are the candidate rows returned by a repository or
    provider for this ``instrument_id + source`` pair; visibility by
    ``known_at <= data_cutoff`` is re-checked here so a caller bug can
    never leak post-cutoff knowledge into a formal run.  Every requested
    session must hit exactly one surviving half-open segment; gaps,
    overlaps, and empty evidence among the *visible* rows are stable
    blocking errors raised before any market data is read.  Mappings
    learned after the cutoff are excluded silently: they do not exist
    for this query and never block by their mere presence.
    """

    data_cutoff = _validated_data_cutoff(
        data_cutoff,
        instrument_id=instrument_id,
        source=source,
    )
    if not isinstance(instrument_id, UUID):
        raise InvalidDataRequestError(
            "instrument_id must be a UUID",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                expected="UUID",
                actual=type(instrument_id).__name__,
                data_cutoff=data_cutoff,
            ),
        )
    ordered_sessions = _validate_sessions(
        sessions,
        instrument_id=instrument_id,
        source=source,
        data_cutoff=data_cutoff,
    )
    # A session after the cutoff cannot have visible history: asking for
    # one is a caller contract breach, blocked before any resolution.
    cutoff_date = data_cutoff.date() if session_cutoff_date is None else session_cutoff_date
    if not isinstance(cutoff_date, date) or isinstance(cutoff_date, datetime):
        raise InvalidDataRequestError(
            "session_cutoff_date must be a calendar date",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                data_cutoff=data_cutoff,
                actual=cutoff_date,
            ),
        )
    future = [day for day in ordered_sessions if day > cutoff_date]
    if future:
        raise DataCutoffExceededError(
            "requested sessions extend past the data cutoff",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                session_date=future[0],
                expected=f"<= {cutoff_date.isoformat()}",
                actual=future[0],
                data_cutoff=data_cutoff,
                first_session_past_cutoff=future[0],
            ),
        )
    if not isinstance(source, str) or not source.strip():
        raise InvalidDataRequestError(
            "source must be non-blank text",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                expected="non-blank source",
                actual=source,
                data_cutoff=data_cutoff,
            ),
        )
    source = source.strip()
    source_lookup = source.casefold()

    # Knowledge-time visibility is enforced here as the last line of
    # defense: rows learned after the cutoff do not exist for this query.
    try:
        mapping_items = tuple(mappings)
    except DataContractError:
        raise
    except Exception as exc:
        raise ProviderContractViolationError(
            "mappings iterable could not be read",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                expected="iterable[InstrumentCodeMapping]",
                actual=type(mappings).__name__,
                data_cutoff=data_cutoff,
                provider_error_type=type(exc).__name__,
                reason=str(exc),
            ),
        ) from exc
    if isinstance(mappings, (str, bytes)):
        raise InvalidDataRequestError(
            "mappings must be an iterable of InstrumentCodeMapping instances",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                expected="iterable[InstrumentCodeMapping]",
                actual=type(mappings).__name__,
                data_cutoff=data_cutoff,
            ),
        )
    visible: list[InstrumentCodeMapping] = []
    for mapping in mapping_items:
        if not isinstance(mapping, InstrumentCodeMapping):
            raise InvalidDataRequestError(
                "mappings entries must be InstrumentCodeMapping instances",
                details=_error_details(
                    instrument_id=instrument_id,
                    source=source,
                    expected="InstrumentCodeMapping",
                    actual=type(mapping).__name__,
                    data_cutoff=data_cutoff,
                ),
            )
        if (
            mapping.instrument_id != instrument_id
            or mapping.source.strip().casefold() != source_lookup
        ):
            raise IdentityMappingIncompleteError(
                "mappings entries must belong to the queried "
                "instrument_id/source pair",
                details=_error_details(
                    instrument_id=instrument_id,
                    source=source,
                    source_code=getattr(mapping, "source_code", None),
                    expected={
                        "instrument_id": instrument_id,
                        "source": source,
                    },
                    actual={
                        "instrument_id": mapping.instrument_id,
                        "source": mapping.source,
                    },
                    data_cutoff=data_cutoff,
                    fact_version=getattr(mapping, "fact_version", None),
                    mapping_instrument_id=mapping.instrument_id,
                    mapping_source=mapping.source,
                ),
            )
        if _aware_datetime(mapping.known_at, "mapping.known_at") > data_cutoff:
            # Knowledge-time filtering: rows learned after the cutoff do
            # not exist for this query, wherever they sit.  Whether the
            # remaining visible rows cover every requested session is the
            # only completeness question; a hidden mapping never blocks
            # by its mere presence.
            continue
        visible.append(mapping)

    # The append-only mapping store has one row per
    # ``(logical_fact_key, fact_version)``.  Check that invariant before the
    # PIT revision fold; otherwise two malformed rows with the same version
    # could be silently resolved by whichever one has the newer ``known_at``.
    duplicate_versions: dict[tuple[str, object], list[InstrumentCodeMapping]] = {}
    for mapping in visible:
        logical_key = getattr(mapping, "logical_fact_key", None)
        if not isinstance(logical_key, str) or not logical_key.strip():
            logical_key = "|".join(str(part) for part in _mapping_fact_key(mapping))
        duplicate_versions.setdefault(
            (logical_key.strip(), getattr(mapping, "fact_version", 1)), []
        ).append(mapping)
    for (logical_key, fact_version), duplicates in duplicate_versions.items():
        if len(duplicates) < 2:
            continue
        # Repeated materialization of one persisted row is common when a
        # provider joins fact data.  It is safe to collapse only when both
        # immutable identity and every fact field agree; independent rows
        # claiming one revision remain a hard conflict.
        first = duplicates[0]
        first_id = getattr(first, "fact_id", None)
        same_materialization = all(
            first_id is not None
            and getattr(item, "fact_id", None) == first_id
            and item == first
            for item in duplicates[1:]
        )
        if same_materialization:
            continue
        raise IdentityMappingConflictError(
            "duplicate immutable mapping fact version in one logical fact chain",
            details=_error_details(
                instrument_id=instrument_id,
                source=source,
                expected="one immutable fact per logical_fact_key/fact_version",
                actual=len(duplicates),
                data_cutoff=data_cutoff,
                fact_version=fact_version,
                logical_fact_key=logical_key,
                fact_ids=[getattr(item, "fact_id", None) for item in duplicates],
                known_ats=[getattr(item, "known_at", None) for item in duplicates],
                fact_versions=[
                    getattr(item, "fact_version", None) for item in duplicates
                ],
            ),
        )

    # Corrections are append-only revisions.  For a given logical fact key,
    # only the newest revision known at this cutoff participates; retaining
    # both the old and corrected row would incorrectly report a conflict.
    latest_by_logical: dict[str, InstrumentCodeMapping] = {}
    for mapping in visible:
        key = getattr(mapping, "logical_fact_key", None)
        if not isinstance(key, str) or not key.strip():
            # Every constructed InstrumentCodeMapping receives a logical key;
            # use a deterministic fact identity for legacy/corrupted rows
            # rather than the process-local object id.
            key = "|".join(
                str(part)
                for part in _mapping_fact_key(mapping)
            )
        current = latest_by_logical.get(key)
        mapping_version = getattr(mapping, "fact_version", 1)
        current_version = getattr(current, "fact_version", 1) if current is not None else 0
        # ``known_at`` is the PIT ordering key.  Fact versions are a
        # deterministic tie-breaker for two revisions published at exactly
        # the same knowledge instant; they must never make a later-version
        # historical backfill visible before its own knowledge time.
        mapping_known_at = _aware_datetime(mapping.known_at, "mapping.known_at")
        current_known_at = (
            _aware_datetime(current.known_at, "mapping.known_at")
            if current is not None
            else None
        )
        if current is None or (mapping_known_at, mapping_version) > (
            current_known_at,
            current_version,
        ):
            latest_by_logical[key] = mapping
    visible = list(latest_by_logical.values())

    bindings: dict[date, InstrumentCodeMapping] = {}
    for day in ordered_sessions:
        covering = [mapping for mapping in visible if mapping.covers(day)]
        if not covering:
            raise IdentityMappingIncompleteError(
                f"no visible instrument code mapping covers session {day}",
                details=_error_details(
                    instrument_id=instrument_id,
                    source=source,
                    session_date=day,
                    expected="one visible mapping covering session",
                    actual="no covering mapping",
                    data_cutoff=data_cutoff,
                    fact_version=None,
                ),
            )
        if len(covering) > 1:
            raise IdentityMappingConflictError(
                f"{len(covering)} instrument code mappings cover session {day}",
                details=_error_details(
                    instrument_id=instrument_id,
                    source=source,
                    session_date=day,
                    expected="one covering mapping",
                    actual=len(covering),
                    data_cutoff=data_cutoff,
                    fact_version=None,
                    fact_versions=[getattr(mapping, "fact_version", None) for mapping in covering],
                    source_codes=sorted(m.source_code for m in covering),
                ),
            )
        chosen = covering[0]
        # Guard corrupted provider materializations as well as the normal
        # constructor invariant: a missing/non-text evidence value is a
        # named mapping block, never an incidental AttributeError.
        if not isinstance(chosen.evidence, str) or not chosen.evidence.strip():
            # InstrumentCodeMapping already rejects blank evidence at
            # construction; the check keeps corrupted providers from
            # bypassing that contract through exotic construction paths.
            raise IdentityMappingEvidenceMissingError(
                f"the mapping covering session {day} carries no evidence",
                details=_error_details(
                    instrument_id=instrument_id,
                    source=source,
                    source_code=chosen.source_code,
                    session_date=day,
                    expected="non-blank evidence",
                    actual=chosen.evidence,
                    data_cutoff=data_cutoff,
                    fact_version=getattr(chosen, "fact_version", None),
                ),
            )
        bindings[day] = chosen

    # Group consecutive bound sessions into contiguous segments so each
    # source code is read once over its full requested range.
    segments: list[PITMappingSegment] = []
    current_mapping: InstrumentCodeMapping | None = None
    current_days: list[date] = []
    for day in ordered_sessions:
        mapping = bindings[day]
        # Group by immutable fact identity, not Python object identity.  A
        # repository may materialize equal rows as separate objects, and
        # doing so must not produce duplicate source reads.
        current_key = (
            _mapping_fact_key(current_mapping)
            if current_mapping is not None
            else None
        )
        mapping_key = _mapping_fact_key(mapping)
        if current_mapping is not None and mapping_key != current_key:
            segments.append(_build_segment(current_mapping, current_days))
            current_days = []
        current_mapping = mapping
        current_days.append(day)
    if current_mapping is not None and current_days:
        segments.append(_build_segment(current_mapping, current_days))

    # ``session_bindings`` keeps typed date keys for programmatic use; the
    # JSON-safe rendering of the same bindings goes into the frozen
    # evidence summary for reports and run snapshots.
    typed_bindings = MappingProxyType(
        {day: binding.source_code for day, binding in bindings.items()}
    )
    evidence_summary = freeze_json(
        {
            "coverage_status": PITMappingCoverage.COMPLETE.value,
            "data_cutoff": data_cutoff.isoformat(),
            "session_cutoff_date": cutoff_date.isoformat(),
            "session_bindings": {
                day.isoformat(): binding.source_code
                for day, binding in bindings.items()
            },
            "segment_count": len(segments),
            "segments": [
                {
                    "source_code": segment.source_code,
                    # ``trading_code`` is a migration-era mirror and is not
                    # read from the mapping or used by the history path.
                    "trading_code": segment.trading_code,
                    "fact_id": str(segment.mapping.fact_id)
                    if segment.mapping.fact_id is not None
                    else None,
                    "fact_version": getattr(segment.mapping, "fact_version", 1),
                    "logical_fact_key": segment.mapping.logical_fact_key,
                    "valid_from": segment.valid_from.isoformat(),
                    "valid_to": (
                        segment.valid_to.isoformat()
                        if segment.valid_to is not None
                        else None
                    ),
                    "first_session": segment.first_requested_session.isoformat(),
                    "last_session": segment.last_requested_session.isoformat(),
                    "session_count": len(segment.requested_sessions),
                    "evidence": segment.mapping.evidence,
                    "mapping_source": segment.mapping.mapping_source,
                    "source_revision": segment.mapping.source_revision,
                }
                for segment in segments
            ],
        },
        "evidence_summary",
    )
    assert isinstance(evidence_summary, Mapping)

    return PITMappingResolution(
        instrument_id=instrument_id,
        source=source,
        requested_sessions=ordered_sessions,
        segments=tuple(segments),
        session_bindings=typed_bindings,
        data_cutoff=data_cutoff,
        coverage_status=PITMappingCoverage.COMPLETE,
        evidence_summary=evidence_summary,
        session_cutoff_date=cutoff_date,
    )


def _build_segment(
    mapping: InstrumentCodeMapping, days: list[date]
) -> PITMappingSegment:
    """Freeze one contiguous per-mapping group of requested sessions."""

    return PITMappingSegment(
        source_code=mapping.source_code,
        valid_from=mapping.valid_from,
        valid_to=mapping.valid_to,
        requested_sessions=tuple(days),
        mapping=mapping,
    )


@dataclass(frozen=True, slots=True)
class SegmentedBarHistory:
    """Stitched bar history plus its auditable mapping provenance."""

    bars: tuple[Bar, ...]
    resolution: PITMappingResolution
    segment_evidence: tuple[SegmentBarEvidence, ...] = ()

    def __post_init__(self) -> None:
        """Deep-freeze the stitched result and validate its public rows."""

        if not isinstance(self.resolution, PITMappingResolution):
            raise InvalidDataRequestError(
                "history resolution must be a PITMappingResolution",
                details=_error_details(
                    expected="PITMappingResolution",
                    actual=type(self.resolution).__name__,
                ),
            )
        try:
            bars = tuple(self.bars)
        except DataContractError:
            raise
        except Exception as exc:
            raise ProviderContractViolationError(
                "history bars must be a sequence of Bar rows",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    data_cutoff=self.resolution.data_cutoff,
                    expected="sequence[Bar]",
                    actual=type(self.bars).__name__,
                ),
            ) from exc
        if any(not isinstance(row, Bar) for row in bars):
            raise ProviderContractViolationError(
                "history bars must contain only Bar rows",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    data_cutoff=self.resolution.data_cutoff,
                    expected="tuple[Bar]",
                    actual=[type(row).__name__ for row in bars],
                ),
            )
        if tuple(row.instrument_id for row in bars) and any(
            row.instrument_id != self.resolution.instrument_id for row in bars
        ):
            wrong = next(
                row for row in bars if row.instrument_id != self.resolution.instrument_id
            )
            raise HistoryBarInstrumentMismatchError(
                "history contains a bar keyed by another instrument",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    session_date=wrong.trade_date,
                    expected=self.resolution.instrument_id,
                    actual=wrong.instrument_id,
                    data_cutoff=self.resolution.data_cutoff,
                ),
            )
        expected_dates = self.resolution.requested_sessions
        actual_dates = tuple(getattr(row, "trade_date", None) for row in bars)
        if any(
            not isinstance(day, date) or isinstance(day, datetime)
            for day in actual_dates
        ):
            raise ProviderContractViolationError(
                "history bars must contain calendar trade dates",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    expected=expected_dates,
                    actual=actual_dates,
                    data_cutoff=self.resolution.data_cutoff,
                ),
            )
        if len(set(actual_dates)) != len(actual_dates):
            raise HistoryBarsDuplicateError(
                "history contains duplicate bars for one session",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    session_date=next(
                        day
                        for day in actual_dates
                        if actual_dates.count(day) > 1
                    ),
                    expected="one bar per requested session",
                    actual=actual_dates,
                    data_cutoff=self.resolution.data_cutoff,
                ),
            )
        out_of_range = [day for day in actual_dates if day not in expected_dates]
        if out_of_range:
            raise HistoryBarsIncompleteError(
                "history contains a bar outside the requested sessions",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    session_date=out_of_range[0],
                    expected=expected_dates,
                    actual=actual_dates,
                    data_cutoff=self.resolution.data_cutoff,
                ),
            )
        missing = [day for day in expected_dates if day not in actual_dates]
        if missing:
            raise HistoryBarsIncompleteError(
                "history does not contain one bar for every requested session",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    session_date=missing[0],
                    expected=expected_dates,
                    actual=actual_dates,
                    missing_sessions=missing,
                    data_cutoff=self.resolution.data_cutoff,
                ),
            )
        if actual_dates != expected_dates:
            raise ProviderContractViolationError(
                "history bars must be in requested ascending session order",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    session_date=actual_dates[0] if actual_dates else None,
                    expected=expected_dates,
                    actual=actual_dates,
                    data_cutoff=self.resolution.data_cutoff,
                ),
            )
        try:
            evidence = tuple(self.segment_evidence)
        except DataContractError:
            raise
        except Exception as exc:
            raise ProviderContractViolationError(
                "history segment_evidence must be a sequence",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    data_cutoff=self.resolution.data_cutoff,
                    expected="sequence[SegmentBarEvidence]",
                    actual=type(self.segment_evidence).__name__,
                ),
            ) from exc
        if len(evidence) != len(self.resolution.segments):
            raise ProviderContractViolationError(
                "history segment_evidence must match resolution segments",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    expected=len(self.resolution.segments),
                    actual=len(evidence),
                    data_cutoff=self.resolution.data_cutoff,
                ),
            )
        if any(not isinstance(item, SegmentBarEvidence) for item in evidence):
            raise ProviderContractViolationError(
                "history segment_evidence must contain SegmentBarEvidence rows",
                details=_error_details(
                    instrument_id=self.resolution.instrument_id,
                    source=self.resolution.source,
                    data_cutoff=self.resolution.data_cutoff,
                    expected="tuple[SegmentBarEvidence]",
                    actual=[type(item).__name__ for item in evidence],
                ),
            )
        for segment, item in zip(self.resolution.segments, evidence):
            expected_evidence = {
                "source": self.resolution.source,
                "source_code": segment.source_code,
                "first_session": segment.first_requested_session,
                "last_session": segment.last_requested_session,
                "requested_count": len(segment.requested_sessions),
                "returned_dates": segment.requested_sessions,
                "coverage_status": PITMappingCoverage.COMPLETE.value,
                "fact_id": segment.fact_id,
                "fact_version": segment.fact_version,
                "valid_from": segment.valid_from,
                "valid_to": segment.valid_to,
                "mapping_evidence": segment.mapping.evidence,
            }
            actual_evidence = {
                "source": item.source,
                "source_code": item.source_code,
                "first_session": item.first_session,
                "last_session": item.last_session,
                "requested_count": item.requested_count,
                "returned_dates": item.returned_dates,
                "coverage_status": item.coverage_status,
                "fact_id": item.fact_id,
                "fact_version": item.fact_version,
                "valid_from": item.valid_from,
                "valid_to": item.valid_to,
                "mapping_evidence": item.mapping_evidence,
            }
            if actual_evidence != expected_evidence:
                raise ProviderContractViolationError(
                    "history segment_evidence does not match its mapping segment",
                    details=_error_details(
                        instrument_id=self.resolution.instrument_id,
                        source=self.resolution.source,
                        source_code=segment.source_code,
                        session_date=segment.first_requested_session,
                        expected=expected_evidence,
                        actual=actual_evidence,
                        data_cutoff=self.resolution.data_cutoff,
                        fact_version=segment.fact_version,
                    ),
                )
        object.__setattr__(self, "bars", bars)
        object.__setattr__(self, "segment_evidence", evidence)

    @property
    def coverage_summary(self) -> tuple[SegmentBarEvidence, ...]:
        """Alias used by result/preflight consumers for segment coverage."""

        return self.segment_evidence


def read_segmented_history(
    resolution: PITMappingResolution | UUID | None = None,
    reader: SegmentBarReader | str | None = None,
    *args,
    instrument_id: UUID | None = None,
    source: str | None = None,
    resolved_sessions: ResolvedSessions | Sequence[date] | None = None,
    data_cutoff: datetime | None = None,
    mappings: Sequence[InstrumentCodeMapping] | None = None,
    mapping_provider=None,
    bar_reader: SegmentBarReader | None = None,
    allow_non_strict_facts: bool = False,
) -> SegmentedBarHistory:
    """Read every segment by source code and stitch one stable history.

    Each segment must return exactly one bar per requested session: any
    missing, duplicated, out-of-range, or non-session date blocks the
    query instead of shrinking the window.  Every bar must carry the
    queried stable ``instrument_id``; results are stitched in ascending
    ``trade_date`` order regardless of segment order.

    By default every bar must also carry strict knowledge-time evidence
    (``known_at <= data_cutoff``).  Sources whose fact tables keep no
    reliable ``known_at`` may opt into ``allow_non_strict_facts``: bars
    without any knowledge-time evidence are then accepted as latest
    authoritative revisions (declared ``non_strict_pit`` in run metadata),
    while a bar that *does* carry a ``known_at`` after the cutoff is still
    blocked — future knowledge never leaks either way.
    """

    # The original API accepted ``(PITMappingResolution, reader)``.  The
    # task-10 API additionally accepts already-resolved sessions and obtains
    # mappings before invoking the bar reader.  Normalize both shapes here
    # so there remains exactly one strict read implementation.
    legacy_resolution = isinstance(resolution, PITMappingResolution)
    if legacy_resolution:
        actual_reader = bar_reader if bar_reader is not None else reader
        if actual_reader is None or isinstance(actual_reader, str):
            raise InvalidDataRequestError(
                "a segment bar reader is required",
                details=_error_details(
                    instrument_id=resolution.instrument_id,
                    source=resolution.source,
                    session_date=resolution.requested_sessions[0]
                    if resolution.requested_sessions
                    else None,
                    expected="segment bar reader",
                    actual=type(actual_reader).__name__,
                    data_cutoff=resolution.data_cutoff,
                ),
            )
        if args:
            raise InvalidDataRequestError(
                "unexpected positional arguments for resolved history",
                details=_error_details(
                    instrument_id=resolution.instrument_id,
                    source=resolution.source,
                    session_date=resolution.requested_sessions[0]
                    if resolution.requested_sessions
                    else None,
                    expected="no extra positional arguments",
                    actual=len(args),
                    data_cutoff=resolution.data_cutoff,
                ),
            )
        resolved = resolution
    else:
        # Positional compatibility shape:
        # (instrument_id, source, resolved_sessions, data_cutoff,
        #  mapping_provider, bar_reader)
        actual_instrument = resolution if resolution is not None else instrument_id
        actual_source = source if source is not None else reader
        if bar_reader is None and reader is not None and not isinstance(reader, str):
            # Keyword callers historically used ``reader=`` for the bar
            # adapter while the new contract names it ``bar_reader``.
            bar_reader = reader  # type: ignore[assignment]
        positional = list(args)
        if resolved_sessions is None and positional:
            resolved_sessions = positional.pop(0)
        if data_cutoff is None and positional:
            data_cutoff = positional.pop(0)
        if mapping_provider is None and positional:
            mapping_provider = positional.pop(0)
        if bar_reader is None and positional:
            bar_reader = positional.pop(0)
        if positional:
            raise InvalidDataRequestError(
                "too many positional arguments for resolved history",
                details=_error_details(
                    instrument_id=actual_instrument,
                    source=actual_source,
                    expected="at most six positional arguments",
                    actual=len(positional),
                    data_cutoff=data_cutoff,
                ),
            )
        if not isinstance(actual_instrument, UUID):
            raise InvalidDataRequestError(
                "instrument_id must be a UUID",
                details=_error_details(
                    instrument_id=actual_instrument,
                    source=actual_source,
                    expected="UUID",
                    actual=type(actual_instrument).__name__,
                    data_cutoff=data_cutoff,
                ),
            )
        if not isinstance(actual_source, str) or not actual_source.strip():
            raise InvalidDataRequestError(
                "source must be non-blank text",
                details=_error_details(
                    instrument_id=actual_instrument,
                    source=actual_source,
                    expected="non-blank source",
                    actual=actual_source,
                    data_cutoff=data_cutoff,
                ),
            )
        if data_cutoff is None:
            raise InvalidDataRequestError(
                "data_cutoff is required",
                details=_error_details(
                    instrument_id=actual_instrument,
                    source=actual_source,
                    expected="timezone-aware datetime",
                    actual=None,
                    data_cutoff=None,
                ),
            )
        data_cutoff = _validated_data_cutoff(
            data_cutoff,
            instrument_id=actual_instrument,
            source=actual_source,
        )
        if resolved_sessions is None:
            raise InvalidDataRequestError(
                "resolved_sessions is required",
                details=_error_details(
                    instrument_id=actual_instrument,
                    source=actual_source,
                    expected="non-empty sequence[date]",
                    actual=None,
                    data_cutoff=data_cutoff,
                ),
            )
        ordered = (
            resolved_sessions.sessions
            if isinstance(resolved_sessions, ResolvedSessions)
            else _validate_sessions(
                resolved_sessions,
                instrument_id=actual_instrument,
                source=actual_source,
                data_cutoff=data_cutoff,
            )
        )
        if mappings is None:
            provider = mapping_provider
            if provider is None:
                raise InvalidDataRequestError(
                    "mapping_provider or mappings is required",
                    details=_error_details(
                        instrument_id=actual_instrument,
                        source=actual_source,
                        expected="mapping_provider or mappings",
                        actual=None,
                        data_cutoff=data_cutoff,
                    ),
                )
            resolver = getattr(provider, "resolve_code_mappings", provider)
            try:
                provider_result = resolver(
                    actual_instrument,
                    source=actual_source,
                    start_date=ordered[0],
                    end_date=ordered[-1],
                    data_cutoff=data_cutoff,
                )
                if provider_result is None or isinstance(provider_result, (str, bytes)):
                    raise TypeError(
                        "mapping provider must return an iterable of mappings"
                    )
                mappings = tuple(provider_result)
            except MappingConflictError as exc:
                details = _mapping_provider_error_details(
                    exc,
                    instrument_id=actual_instrument,
                    source=actual_source,
                    data_cutoff=data_cutoff,
                    session_date=ordered[0],
                )
                raise IdentityMappingConflictError(
                    "PIT mapping provider returned conflicting facts",
                    details=details,
                ) from exc
            except MappingCoverageGapError as exc:
                details = _mapping_provider_error_details(
                    exc,
                    instrument_id=actual_instrument,
                    source=actual_source,
                    data_cutoff=data_cutoff,
                    session_date=ordered[0],
                )
                raise IdentityMappingIncompleteError(
                    "PIT mapping provider could not cover the resolved sessions",
                    details=details,
                ) from exc
            except DataContractError as exc:
                _enrich_provider_contract_error(
                    exc,
                    defaults=_mapping_provider_error_details(
                        exc,
                        instrument_id=actual_instrument,
                        source=actual_source,
                        data_cutoff=data_cutoff,
                        session_date=ordered[0],
                    ),
                )
                raise
            except Exception as exc:
                raise ProviderContractViolationError(
                    "mapping provider rejected the sessions-only resolution request",
                    details=_error_details(
                        instrument_id=actual_instrument,
                        source=actual_source,
                        session_date=ordered[0],
                        expected=(
                            "resolve_code_mappings(instrument_id, source, "
                            "start_date, end_date, data_cutoff) returning "
                            "iterable[InstrumentCodeMapping]"
                        ),
                        actual=(
                            type(provider_result).__name__
                        if "provider_result" in locals()
                            else type(provider).__name__
                        ),
                        data_cutoff=data_cutoff,
                        provider_error_type=type(exc).__name__,
                        reason=str(exc),
                    ),
                ) from exc
        else:
            try:
                if isinstance(mappings, (str, bytes)):
                    raise TypeError("mappings must not be text")
                mappings = tuple(mappings)
            except DataContractError:
                raise
            except Exception as exc:
                raise InvalidDataRequestError(
                    "mappings must be an iterable of InstrumentCodeMapping instances",
                    details=_error_details(
                        instrument_id=actual_instrument,
                        source=actual_source,
                        session_date=ordered[0],
                        expected="iterable[InstrumentCodeMapping]",
                        actual=type(mappings).__name__,
                        data_cutoff=data_cutoff,
                    ),
                ) from exc
        resolved = resolve_pit_mappings(
            actual_instrument,
            source=actual_source,
            sessions=ordered,
            mappings=tuple(mappings),
            data_cutoff=data_cutoff,
        )
        actual_reader = bar_reader
        if actual_reader is None or isinstance(actual_reader, str):
            raise InvalidDataRequestError(
                "bar_reader is required",
                details=_error_details(
                    instrument_id=actual_instrument,
                    source=actual_source,
                    session_date=ordered[0],
                    expected="segment bar reader",
                    actual=type(actual_reader).__name__,
                    data_cutoff=data_cutoff,
                ),
            )

    # A ``ResolvedSessions`` instance validates ordering but does not bind
    # itself to a cutoff.  Re-check the boundary here so callers cannot bypass
    # the public calendar resolver by constructing the value directly.
    future_sessions = [
        day for day in resolved.requested_sessions if day > resolved.data_cutoff.date()
    ]
    if future_sessions:
        raise DataCutoffExceededError(
            "resolved sessions extend past the data cutoff",
            details=_error_details(
                instrument_id=resolved.instrument_id,
                source=resolved.source,
                session_date=future_sessions[0],
                expected=f"<= {resolved.data_cutoff.date().isoformat()}",
                actual=future_sessions[0],
                data_cutoff=resolved.data_cutoff,
                first_session_past_cutoff=future_sessions[0],
            ),
        )

    collected: dict[date, Bar] = {}
    segment_evidence: list[SegmentBarEvidence] = []

    def segment_details(
        segment: PITMappingSegment,
        *,
        session_date: object = None,
        expected: object = None,
        actual: object = None,
        **extra: object,
    ) -> dict[str, object]:
        """Attach the selected mapping and request coordinates to an error."""

        return _error_details(
            instrument_id=resolved.instrument_id,
            source=resolved.source,
            source_code=segment.source_code,
            session_date=session_date,
            expected=expected,
            actual=actual,
            data_cutoff=resolved.data_cutoff,
            fact_version=segment.fact_version,
            mapping_fact_id=segment.fact_id,
            mapping_valid_from=segment.valid_from,
            mapping_valid_to=segment.valid_to,
            mapping_evidence=segment.mapping.evidence,
            **extra,
        )

    # Both the compatibility and direct APIs converge on this validated
    # immutable resolution before any provider row is read.
    for segment in resolved.segments:
        expected = segment.requested_sessions
        expected_set = set(expected)
        try:
            returned = actual_reader.read_bars(
                segment.source_code,
                segment.first_requested_session,
                segment.last_requested_session,
            )
        except DataContractError as exc:
            _enrich_provider_contract_error(
                exc,
                defaults=segment_details(
                    segment,
                    session_date=segment.first_requested_session,
                    expected="sequence[Bar] or SegmentBarEnvelope",
                    actual=type(exc).__name__,
                    provider_error_type=type(exc).__name__,
                    reason=str(exc),
                ),
            )
            raise
        except AttributeError as exc:
            raise ProviderContractViolationError(
                "bar_reader must provide read_bars(source_code, start_date, end_date)",
                details=segment_details(
                    segment,
                    expected="read_bars(source_code, start_date, end_date)",
                    actual="missing read_bars method",
                    provider_error_type=type(exc).__name__,
                    reason=str(exc),
                ),
            ) from exc
        except Exception as exc:
            raise ProviderContractViolationError(
                "bar_reader rejected the sessions-only read request",
                details=segment_details(
                    segment,
                    session_date=segment.first_requested_session,
                    expected="read_bars(source_code, start_date, end_date)",
                    actual=str(exc),
                    provider_error_type=type(exc).__name__,
                    reason=str(exc),
                ),
            ) from exc
        if returned is None or isinstance(returned, (str, bytes)):
            raise ProviderContractViolationError(
                "segment reader must return a sequence of bars or a segment envelope",
                details=segment_details(
                    segment,
                    expected="sequence[Bar] or SegmentBarEnvelope",
                    actual=type(returned).__name__,
                ),
            )
        # New providers may return a single envelope carrying the source
        # metadata and all rows.  Legacy providers return bare Bars; keep
        # accepting that shape while binding source metadata from the call.
        if isinstance(returned, SegmentBarEnvelope):
            if returned.instrument_id != resolved.instrument_id:
                raise HistoryBarInstrumentMismatchError(
                    "segment envelope returned another instrument_id",
                    details=segment_details(
                        segment,
                        expected=resolved.instrument_id,
                        actual=returned.instrument_id,
                        returned_instrument_id=returned.instrument_id,
                    ),
                )
            if (
                _source_key(returned.source) != _source_key(resolved.source)
                or returned.source_code != segment.source_code
            ):
                raise ProviderContractViolationError(
                    "segment envelope source identity does not match the request",
                    details=segment_details(
                        segment,
                        expected={
                            "source": resolved.source,
                            "source_code": segment.source_code,
                        },
                        actual={
                            "source": returned.source,
                            "source_code": returned.source_code,
                        },
                        expected_source=resolved.source,
                        actual_source=returned.source,
                        expected_source_code=segment.source_code,
                        actual_source_code=returned.source_code,
                    ),
                )
            if tuple(returned.requested_sessions) != expected:
                raise ProviderContractViolationError(
                    "segment envelope requested_sessions do not match the request",
                    details=segment_details(
                        segment,
                        session_date=expected[0] if expected else None,
                        expected=expected,
                        actual=returned.requested_sessions,
                        expected_sessions=expected,
                        actual_sessions=returned.requested_sessions,
                    ),
                )
            rows = returned.bars
        else:
            try:
                returned_items = tuple(returned)
            except DataContractError:
                raise
            except Exception as exc:
                raise ProviderContractViolationError(
                    "segment reader must return a sequence of bars or a segment envelope",
                    details=segment_details(
                        segment,
                        expected="sequence[Bar] or SegmentBarEnvelope",
                        actual=type(returned).__name__,
                    ),
                ) from exc
            embedded = tuple(item for item in returned_items if isinstance(item, SegmentBarEnvelope))
            if embedded:
                if len(embedded) != len(returned_items):
                    raise ProviderContractViolationError(
                        "segment reader mixed envelopes and bare bars",
                        details=segment_details(
                            segment,
                            expected="all envelopes or all Bar rows",
                            actual="mixed envelopes and Bar rows",
                        ),
                    )
                for envelope in embedded:
                    if envelope.instrument_id != resolved.instrument_id:
                        raise HistoryBarInstrumentMismatchError(
                            "segment envelope returned another instrument_id",
                            details=segment_details(
                                segment,
                                expected=resolved.instrument_id,
                                actual=envelope.instrument_id,
                                returned_instrument_id=envelope.instrument_id,
                            ),
                        )
                    if (
                        _source_key(envelope.source) != _source_key(resolved.source)
                        or envelope.source_code != segment.source_code
                    ):
                        raise ProviderContractViolationError(
                            "segment envelope source identity does not match the request",
                            details=segment_details(
                                segment,
                                expected={
                                    "source": resolved.source,
                                    "source_code": segment.source_code,
                                },
                                actual={
                                    "source": envelope.source,
                                    "source_code": envelope.source_code,
                                },
                                expected_source=resolved.source,
                                actual_source=envelope.source,
                                expected_source_code=segment.source_code,
                                actual_source_code=envelope.source_code,
                            ),
                        )
                declared_sessions = tuple(
                    day for envelope in embedded for day in envelope.requested_sessions
                )
                if declared_sessions != expected:
                    raise ProviderContractViolationError(
                        "segment envelopes requested_sessions do not match the request",
                        details=segment_details(
                            segment,
                            session_date=expected[0] if expected else None,
                            expected=expected,
                            actual=declared_sessions,
                            expected_sessions=expected,
                            actual_sessions=declared_sessions,
                        ),
                    )
                rows = tuple(row for envelope in embedded for row in envelope.bars)
            else:
                rows = returned_items
            if not legacy_resolution and not embedded:
                # The sessions-only API has no positional source-code
                # context beyond the selected mapping segment.  Bare Bars
                # therefore cannot prove which source-code fact produced
                # them and must not be accepted at this boundary.  The
                # legacy (PITMappingResolution, reader) shape retains its
                # historical bare-Bar compatibility.
                raise ProviderContractViolationError(
                    "sessions-only segment readers must return "
                    "SegmentBarEnvelope values",
                    details=segment_details(
                        segment,
                        session_date=segment.first_requested_session,
                        expected="SegmentBarEnvelope",
                        actual="sequence[Bar]",
                        source_code_evidence_missing=True,
                    ),
                )
        seen: set[date] = set()
        previous_date: date | None = None
        for row in rows:
            if not isinstance(row, Bar):
                raise HistoryBarsIncompleteError(
                    "segment reader returned a non-Bar row",
                    details=segment_details(
                        segment,
                        session_date=expected[len(seen)] if len(seen) < len(expected) else None,
                        expected="Bar",
                        actual=type(row).__name__,
                        row_type=type(row).__name__,
                    ),
                )
            if row.instrument_id != resolved.instrument_id:
                raise HistoryBarInstrumentMismatchError(
                    "segment reader returned a bar keyed by another "
                    "instrument",
                    details=segment_details(
                        segment,
                        session_date=row.trade_date,
                        expected=resolved.instrument_id,
                        actual=row.instrument_id,
                        expected_instrument_id=resolved.instrument_id,
                        returned_instrument_id=row.instrument_id,
                        trade_date=row.trade_date,
                    ),
                )
            if _source_key(row.evidence.source) != _source_key(resolved.source):
                raise ProviderContractViolationError(
                    "segment reader returned a bar from another source",
                    details=segment_details(
                        segment,
                        session_date=row.trade_date,
                        expected=resolved.source,
                        actual=row.evidence.source,
                        expected_source=resolved.source,
                        actual_source=row.evidence.source,
                        trade_date=row.trade_date,
                    ),
                )
            attributes = getattr(row, "attributes", None)
            declared_code = attributes.get("source_code") if attributes else None
            if declared_code is not None and declared_code != segment.source_code:
                raise ProviderContractViolationError(
                    "segment reader returned a bar for another source code",
                    details=segment_details(
                        segment,
                        session_date=row.trade_date,
                        expected=segment.source_code,
                        actual=declared_code,
                        expected_source_code=segment.source_code,
                        actual_source_code=declared_code,
                        trade_date=row.trade_date,
                    ),
                )
            # Formal reads re-check fact quality and knowledge time even
            # though upstream boundaries should already enforce them: a
            # leaky or partial row must block instead of stitching in.
            if row.evidence.known_at is None:
                if not allow_non_strict_facts:
                    raise HistoryBarsIncompleteError(
                        "segment reader returned a bar without strict "
                        "knowledge-time evidence inside the data cutoff",
                        details={
                            **segment_details(
                                segment,
                                session_date=row.trade_date,
                                expected="known_at <= data_cutoff",
                                actual=None,
                            ),
                            "known_at": None,
                        },
                    )
            elif row.evidence.known_at > resolved.data_cutoff:
                raise HistoryBarsIncompleteError(
                    "segment reader returned a bar whose knowledge time "
                    "is after the data cutoff",
                    details={
                        **segment_details(
                            segment,
                            session_date=row.trade_date,
                            expected=resolved.data_cutoff,
                            actual=row.evidence.known_at,
                        ),
                        "known_at": row.evidence.known_at.isoformat(),
                    },
                )
            if row.evidence.quality_status is not QualityStatus.COMPLETE:
                raise HistoryBarsIncompleteError(
                    "segment reader returned a bar that is not complete "
                    "quality",
                    details={
                        **segment_details(
                            segment,
                            session_date=row.trade_date,
                            expected=QualityStatus.COMPLETE,
                            actual=row.evidence.quality_status,
                        ),
                        "quality_status": row.evidence.quality_status.value,
                    },
                )
            # Check the global result before this segment's coverage check.
            # A malformed reader can return a date belonging to an earlier
            # segment; that is a duplicate fact across segments, not merely
            # an out-of-range row, and must retain the duplicate error code.
            if row.trade_date in collected:
                raise HistoryBarsDuplicateError(
                    "two segments returned a bar for the same session",
                    details={
                        **segment_details(
                            segment,
                            session_date=row.trade_date,
                            expected="one segment bar",
                            actual="multiple segment bars",
                            trade_date=row.trade_date,
                        ),
                        "duplicate_source_code": segment.source_code,
                    },
                )
            if row.trade_date not in expected_set:
                raise HistoryBarsIncompleteError(
                    "segment reader returned a bar outside the requested "
                    "sessions",
                    details={
                        **segment_details(
                            segment,
                            session_date=row.trade_date,
                            expected=expected,
                            actual=row.trade_date,
                        ),
                    },
                )
            if row.trade_date in seen:
                raise HistoryBarsDuplicateError(
                    "segment reader returned a duplicate bar for one session",
                    details={
                        **segment_details(
                            segment,
                            session_date=row.trade_date,
                            expected="one bar per session",
                            actual="duplicate",
                        ),
                    },
                )
            if previous_date is not None and row.trade_date <= previous_date:
                raise ProviderContractViolationError(
                    "segment reader returned bars out of ascending date order",
                    details={
                        **segment_details(
                            segment,
                            session_date=row.trade_date,
                            expected=f"> {previous_date.isoformat()}",
                            actual=row.trade_date,
                            previous_trade_date=previous_date,
                            trade_date=row.trade_date,
                        ),
                    },
                )
            seen.add(row.trade_date)
            collected[row.trade_date] = row
            previous_date = row.trade_date
        missing = [day for day in expected if day not in seen]
        if missing:
            raise HistoryBarsIncompleteError(
                "segment reader returned no bar for every requested session",
                details={
                    **segment_details(
                        segment,
                        session_date=missing[0],
                        expected=expected,
                        actual=tuple(seen),
                        missing_session_count=len(missing),
                        first_missing_session=missing[0],
                    ),
                },
            )
        segment_evidence.append(
            SegmentBarEvidence(
                source=resolved.source,
                source_code=segment.source_code,
                first_session=segment.first_requested_session,
                last_session=segment.last_requested_session,
                requested_count=len(expected),
                returned_dates=tuple(row.trade_date for row in rows),
                fact_id=segment.fact_id,
                fact_version=segment.fact_version,
                valid_from=segment.valid_from,
                valid_to=segment.valid_to,
                mapping_evidence=segment.mapping.evidence,
            )
        )
    return SegmentedBarHistory(
        bars=tuple(collected[day] for day in resolved.requested_sessions),
        resolution=resolved,
        segment_evidence=tuple(segment_evidence),
    )


class SegmentFactorReader(Protocol):
    """Structural source of adjustment factors for one source-code segment."""

    def read_factors(
        self,
        source_code: str,
        start_date: date,
        end_date: date,
    ) -> Sequence[object]:
        """Return factors for exactly the sessions inside ``[start, end]``."""
        ...


@dataclass(frozen=True, slots=True)
class SegmentedAdjustedSeries:
    """Stitched adjustment series plus its auditable mapping provenance."""

    points: tuple[AdjustedSeriesPoint, ...]
    resolution: PITMappingResolution


def read_segmented_adjusted_series(
    resolution: PITMappingResolution,
    reader: SegmentFactorReader,
    *,
    cutoff_date: date | None = None,
) -> SegmentedAdjustedSeries:
    """Read every segment's factors by source code and stitch one series.

    The adjusted series uses the same mapping split as the bar path: each
    source-code interval is read separately and the results are keyed back
    to the stable ``instrument_id``.  First-version factor selection
    follows the approved ``effective_date <= data_cutoff`` contract: a
    factor point must sit exactly on each requested session, which is
    never after the cutoff; no knowledge-time evidence is required and no
    historical factor revisions are fabricated.

    Any missing, duplicated, out-of-range, or wrong-identity factor blocks
    the query instead of returning a partial series.
    """

    collected: dict[date, AdjustedSeriesPoint] = {}
    # Providers with a market-local calendar may pass the already resolved
    # cutoff date.  The legacy default remains the UTC-surface date for
    # generic callers, while ETF adapters bind this to Asia/Shanghai.
    if cutoff_date is None:
        cutoff_date = resolution.data_cutoff.date()
    elif not isinstance(cutoff_date, date) or isinstance(cutoff_date, datetime):
        raise InvalidDataRequestError("cutoff_date must be a calendar date")
    for segment in resolution.segments:
        expected = segment.requested_sessions
        rows = reader.read_factors(
            segment.source_code,
            segment.first_requested_session,
            segment.last_requested_session,
        )
        returned_dates: list[date] = []
        for row in rows:
            if not isinstance(row, AdjustedSeriesPoint):
                raise HistoryBarsIncompleteError(
                    "segment reader returned a non-AdjustedSeriesPoint row",
                    details={
                        "source_code": segment.source_code,
                        "row_type": type(row).__name__,
                    },
                )
            if row.instrument_id != resolution.instrument_id:
                raise HistoryBarInstrumentMismatchError(
                    "segment reader returned an adjustment factor keyed by "
                    "another instrument",
                    details={
                        "source_code": segment.source_code,
                        "expected_instrument_id": str(resolution.instrument_id),
                        "returned_instrument_id": str(row.instrument_id),
                        "point_date": row.point_date.isoformat(),
                    },
                )
            # ``AdjustedSeriesPoint`` keeps source coordinates optional for
            # compatibility with generic providers.  When a reader supplies
            # them, however, they are authoritative provenance and must
            # agree with the PIT segment that was queried; otherwise a
            # cross-code factor could be silently stitched into this history.
            if row.source_code is not None and row.source_code != segment.source_code:
                raise ProviderContractViolationError(
                    "segment reader returned an adjustment factor for another source code",
                    details={
                        "source_code": segment.source_code,
                        "returned_source_code": row.source_code,
                        "point_date": row.point_date.isoformat(),
                    },
                )
            if _source_key(row.evidence.source) != _source_key(resolution.source):
                raise ProviderContractViolationError(
                    "segment reader returned an adjustment factor from another source",
                    details={
                        "source_code": segment.source_code,
                        "expected_source": resolution.source,
                        "actual_source": row.evidence.source,
                        "point_date": row.point_date.isoformat(),
                    },
                )
            if row.point_date in collected:
                raise HistoryBarsDuplicateError(
                    "two segments returned an adjustment factor for the same session",
                    details={
                        "instrument_id": str(resolution.instrument_id),
                        "source": resolution.source,
                        "source_code": segment.source_code,
                        "session_date": row.point_date.isoformat(),
                        "expected": "one factor per session",
                        "actual": "duplicate",
                        "data_cutoff": resolution.data_cutoff.isoformat(),
                        "fact_version": segment.fact_version,
                        "trade_date": row.point_date.isoformat(),
                    },
                )
            # Strict quality gate: a partial or invalid factor must block
            # the series instead of being stitched into it.
            if row.evidence.quality_status is not QualityStatus.COMPLETE:
                raise HistoryBarsIncompleteError(
                    "segment reader returned an adjustment factor that is "
                    "not complete quality",
                    details={
                        "source_code": segment.source_code,
                        "point_date": row.point_date.isoformat(),
                        "quality_status": row.evidence.quality_status.value,
                    },
                )
            if row.point_date > cutoff_date:
                raise HistoryBarsIncompleteError(
                    "segment reader returned an adjustment factor with an "
                    "effective date after the data cutoff",
                    details={
                        "source_code": segment.source_code,
                        "point_date": row.point_date.isoformat(),
                        "data_cutoff": resolution.data_cutoff.isoformat(),
                    },
                )
            returned_dates.append(row.point_date)
            collected[row.point_date] = row
        HistoryCompletenessValidator(
            source_code=segment.source_code,
            expected_sessions=expected,
        ).validate(returned_dates)
    return SegmentedAdjustedSeries(
        points=tuple(collected[day] for day in resolution.requested_sessions),
        resolution=resolution,
    )
