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
    DataCutoffExceededError,
    HistoryBarsDuplicateError,
    HistoryBarInstrumentMismatchError,
    HistoryBarsIncompleteError,
    IdentityMappingConflictError,
    IdentityMappingEvidenceMissingError,
    IdentityMappingIncompleteError,
    freeze_json,
)
from app.backtesting.data.facts import AdjustedSeriesPoint, Bar
from app.backtesting.data.requests import QualityStatus
from app.backtesting.domain import _aware_datetime
from app.instruments.domain import InstrumentCodeMapping

__all__ = [
    "HistoryCompletenessValidator",
    "PITMappingCoverage",
    "PITMappingResolution",
    "PITMappingSegment",
    "SegmentBarReader",
    "SegmentFactorReader",
    "SegmentedAdjustedSeries",
    "SegmentedBarHistory",
    "read_segmented_adjusted_series",
    "read_segmented_history",
    "resolve_pit_mappings",
]


class PITMappingCoverage(StrEnum):
    """Coverage status of a finished mapping resolution."""

    COMPLETE = "complete"


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

    def validate(self, returned_dates: Sequence[date]) -> None:
        """Block unless ``returned_dates`` matches the expected sessions."""

        expected_set = set(self.expected_sessions)
        seen: set[date] = set()
        for day in returned_dates:
            if day not in expected_set:
                raise HistoryBarsIncompleteError(
                    "segment reader returned a fact outside the requested "
                    "sessions",
                    details={
                        "source_code": self.source_code,
                        "trade_date": day.isoformat(),
                    },
                )
            if day in seen:
                raise HistoryBarsDuplicateError(
                    "segment reader returned a duplicate fact for one session",
                    details={
                        "source_code": self.source_code,
                        "trade_date": day.isoformat(),
                    },
                )
            seen.add(day)
        missing = [day for day in self.expected_sessions if day not in seen]
        if missing:
            raise HistoryBarsIncompleteError(
                "segment reader returned no fact for every requested session",
                details={
                    "source_code": self.source_code,
                    "missing_session_count": len(missing),
                    "first_missing_session": missing[0].isoformat(),
                },
            )


@dataclass(frozen=True, slots=True)
class PITMappingSegment:
    """One half-open identity segment clamped to the requested sessions."""

    source_code: str
    trading_code: str
    valid_from: date
    valid_to: date | None
    requested_sessions: tuple[date, ...]
    mapping: InstrumentCodeMapping

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


def _validate_sessions(sessions: Sequence[date]) -> tuple[date, ...]:
    """Require distinct ascending calendar dates (calendar output order)."""

    ordered = tuple(sessions)
    for day in ordered:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise IdentityMappingIncompleteError(
                "requested sessions must be calendar dates",
                details={"offending_value": repr(day)},
            )
    if any(later <= earlier for earlier, later in zip(ordered, ordered[1:])):
        raise IdentityMappingIncompleteError(
            "requested sessions must be distinct dates in ascending order"
        )
    return ordered


def resolve_pit_mappings(
    instrument_id: UUID,
    *,
    source: str,
    sessions: Sequence[date],
    mappings: Sequence[InstrumentCodeMapping],
    data_cutoff: datetime,
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

    _aware_datetime(data_cutoff, "data_cutoff")
    if not isinstance(instrument_id, UUID):
        raise IdentityMappingIncompleteError("instrument_id must be a UUID")
    ordered_sessions = _validate_sessions(sessions)
    # A session after the cutoff cannot have visible history: asking for
    # one is a caller contract breach, blocked before any resolution.
    cutoff_date = data_cutoff.date()
    future = [day for day in ordered_sessions if day > cutoff_date]
    if future:
        raise DataCutoffExceededError(
            "requested sessions extend past the data cutoff",
            details={
                "data_cutoff": data_cutoff.isoformat(),
                "first_session_past_cutoff": future[0].isoformat(),
            },
        )
    if not isinstance(source, str) or not source.strip():
        raise IdentityMappingIncompleteError("source must be non-blank text")

    # Knowledge-time visibility is enforced here as the last line of
    # defense: rows learned after the cutoff do not exist for this query.
    visible: list[InstrumentCodeMapping] = []
    for mapping in mappings:
        if not isinstance(mapping, InstrumentCodeMapping):
            raise IdentityMappingIncompleteError(
                "mappings entries must be InstrumentCodeMapping instances"
            )
        if mapping.instrument_id != instrument_id or mapping.source != source:
            raise IdentityMappingIncompleteError(
                "mappings entries must belong to the queried "
                "instrument_id/source pair",
                details={
                    "mapping_instrument_id": str(mapping.instrument_id),
                    "mapping_source": mapping.source,
                },
            )
        if mapping.known_at > data_cutoff:
            # Knowledge-time filtering: rows learned after the cutoff do
            # not exist for this query, wherever they sit.  Whether the
            # remaining visible rows cover every requested session is the
            # only completeness question; a hidden mapping never blocks
            # by its mere presence.
            continue
        visible.append(mapping)

    bindings: dict[date, InstrumentCodeMapping] = {}
    for day in ordered_sessions:
        covering = [mapping for mapping in visible if mapping.covers(day)]
        if not covering:
            raise IdentityMappingIncompleteError(
                f"no visible instrument code mapping covers session {day}",
                details={
                    "instrument_id": str(instrument_id),
                    "session": day.isoformat(),
                },
            )
        if len(covering) > 1:
            raise IdentityMappingConflictError(
                f"{len(covering)} instrument code mappings cover session {day}",
                details={
                    "instrument_id": str(instrument_id),
                    "session": day.isoformat(),
                    "source_codes": sorted(m.source_code for m in covering),
                },
            )
        chosen = covering[0]
        if not chosen.evidence.strip():
            # InstrumentCodeMapping already rejects blank evidence at
            # construction; the check keeps corrupted providers from
            # bypassing that contract through exotic construction paths.
            raise IdentityMappingEvidenceMissingError(
                f"the mapping covering session {day} carries no evidence",
                details={
                    "instrument_id": str(instrument_id),
                    "source_code": chosen.source_code,
                    "session": day.isoformat(),
                },
            )
        bindings[day] = chosen

    # Group consecutive bound sessions into contiguous segments so each
    # source code is read once over its full requested range.
    segments: list[PITMappingSegment] = []
    current_mapping: InstrumentCodeMapping | None = None
    current_days: list[date] = []
    for day in ordered_sessions:
        mapping = bindings[day]
        if current_mapping is not None and mapping is not current_mapping:
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
            "session_bindings": {
                day.isoformat(): binding.source_code
                for day, binding in bindings.items()
            },
            "segment_count": len(segments),
            "segments": [
                {
                    "source_code": segment.source_code,
                    "trading_code": segment.trading_code,
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
    )


def _build_segment(
    mapping: InstrumentCodeMapping, days: list[date]
) -> PITMappingSegment:
    """Freeze one contiguous per-mapping group of requested sessions."""

    return PITMappingSegment(
        source_code=mapping.source_code,
        trading_code=mapping.trading_code,
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


def read_segmented_history(
    resolution: PITMappingResolution,
    reader: SegmentBarReader,
    *,
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

    collected: dict[date, Bar] = {}
    cutoff_date = resolution.data_cutoff.date()
    for segment in resolution.segments:
        expected = segment.requested_sessions
        expected_set = set(expected)
        rows = reader.read_bars(
            segment.source_code,
            segment.first_requested_session,
            segment.last_requested_session,
        )
        seen: set[date] = set()
        for row in rows:
            if not isinstance(row, Bar):
                raise HistoryBarsIncompleteError(
                    "segment reader returned a non-Bar row",
                    details={
                        "source_code": segment.source_code,
                        "row_type": type(row).__name__,
                    },
                )
            if row.instrument_id != resolution.instrument_id:
                raise HistoryBarInstrumentMismatchError(
                    "segment reader returned a bar keyed by another "
                    "instrument",
                    details={
                        "source_code": segment.source_code,
                        "expected_instrument_id": str(resolution.instrument_id),
                        "returned_instrument_id": str(row.instrument_id),
                        "trade_date": row.trade_date.isoformat(),
                    },
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
                            "source_code": segment.source_code,
                            "trade_date": row.trade_date.isoformat(),
                            "known_at": None,
                            "data_cutoff": resolution.data_cutoff.isoformat(),
                        },
                    )
            elif row.evidence.known_at > resolution.data_cutoff:
                raise HistoryBarsIncompleteError(
                    "segment reader returned a bar whose knowledge time "
                    "is after the data cutoff",
                    details={
                        "source_code": segment.source_code,
                        "trade_date": row.trade_date.isoformat(),
                        "known_at": row.evidence.known_at.isoformat(),
                        "data_cutoff": resolution.data_cutoff.isoformat(),
                    },
                )
            if row.evidence.quality_status is not QualityStatus.COMPLETE:
                raise HistoryBarsIncompleteError(
                    "segment reader returned a bar that is not complete "
                    "quality",
                    details={
                        "source_code": segment.source_code,
                        "trade_date": row.trade_date.isoformat(),
                        "quality_status": row.evidence.quality_status.value,
                    },
                )
            if row.trade_date not in expected_set:
                raise HistoryBarsIncompleteError(
                    "segment reader returned a bar outside the requested "
                    "sessions",
                    details={
                        "source_code": segment.source_code,
                        "trade_date": row.trade_date.isoformat(),
                    },
                )
            if row.trade_date in seen:
                raise HistoryBarsDuplicateError(
                    "segment reader returned a duplicate bar for one session",
                    details={
                        "source_code": segment.source_code,
                        "trade_date": row.trade_date.isoformat(),
                    },
                )
            seen.add(row.trade_date)
            collected[row.trade_date] = row
        missing = [day for day in expected if day not in seen]
        if missing:
            raise HistoryBarsIncompleteError(
                "segment reader returned no bar for every requested session",
                details={
                    "source_code": segment.source_code,
                    "missing_session_count": len(missing),
                    "first_missing_session": missing[0].isoformat(),
                },
            )
    return SegmentedBarHistory(
        bars=tuple(collected[day] for day in resolution.requested_sessions),
        resolution=resolution,
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
    cutoff_date = resolution.data_cutoff.date()
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
