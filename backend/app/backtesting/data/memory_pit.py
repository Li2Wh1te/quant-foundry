"""In-memory PIT-mapping test fixture for cross-code history queries.

This module is a *test fixture*, not a production data source: it stores
facts keyed by **source code** (the way real ingestion tables are keyed)
plus evidenced ``InstrumentCodeMapping`` rows, and serves them through
exactly the production PIT read path of :mod:`app.backtesting.data.pit_history`.
Queries are made only by stable ``instrument_id``; there is deliberately no
reverse lookup from a source code, so a test proves that history cannot be
stitched from today's code.

Fault injection is structural: omit a mapping row, a bar row, or a factor
row and the corresponding completeness check must block the query instead
of shortening the window.  Duplicate rows can be injected through
``duplicate_bar_dates`` / ``duplicate_factor_dates`` to exercise duplicate
detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID

from app.backtesting.data.errors import (
    HistoryIncompleteError,
    IdentityMappingIncompleteError,
    InvalidDataRequestError,
)
from app.backtesting.data.facts import AdjustedSeriesPoint, Bar, FactEvidence
from app.backtesting.data.pit_history import (
    PITMappingResolution,
    SegmentFactorReader,
    SegmentedAdjustedSeries,
    SegmentedBarHistory,
    read_segmented_adjusted_series,
    read_segmented_history,
    resolve_pit_mappings,
)
from app.backtesting.data.requests import (
    DateRange,
    LookbackWindow,
    PriceBasis,
    QualityStatus,
    QueryBoundary,
)
from app.backtesting.domain import _aware_datetime
from app.instruments.domain import InstrumentCodeMapping

__all__ = [
    "PITFixtureBarRow",
    "PITFixtureFactorRow",
    "PITMappingFixture",
]


def _finite_decimal(value: object, field_name: str) -> Decimal:
    """Accept only exact finite decimals in fixture rows."""

    if isinstance(value, bool) or isinstance(value, float) or not isinstance(
        value, (Decimal, int, str)
    ):
        raise InvalidDataRequestError(
            f"{field_name} must be Decimal, int, or str; float is unsupported"
        )
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidDataRequestError(f"{field_name} is not a valid decimal") from exc
    if not normalized.is_finite():
        raise InvalidDataRequestError(f"{field_name} must be finite")
    return normalized


@dataclass(frozen=True, slots=True)
class PITFixtureBarRow:
    """One raw bar fact keyed by the *source code* of its validity window.

    The row mirrors how an ingestion table stores facts (source-code key,
    no stable identity): the stable ``instrument_id`` is attached only at
    projection time from the resolved PIT mapping segment.  Values are kept
    exactly as given; nothing is repaired here or at projection.
    """

    source_code: str
    trade_date: date
    open: Decimal | int | str
    high: Decimal | int | str
    low: Decimal | int | str
    close: Decimal | int | str
    volume: Decimal | int | str
    amount: Decimal | int | str
    observed_at: datetime
    quality_status: QualityStatus = QualityStatus.COMPLETE
    known_at: datetime | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_code, str) or not self.source_code.strip():
            raise InvalidDataRequestError("source_code must be non-blank text")
        if not isinstance(self.trade_date, date) or isinstance(self.trade_date, datetime):
            raise InvalidDataRequestError("trade_date must be a calendar date")
        for name in ("open", "high", "low", "close", "volume", "amount"):
            object.__setattr__(
                self, name, _finite_decimal(getattr(self, name), name)
            )
        object.__setattr__(
            self, "observed_at", _aware_datetime(self.observed_at, "observed_at")
        )
        if not isinstance(self.quality_status, QualityStatus):
            raise InvalidDataRequestError("quality_status must be a QualityStatus")
        if self.known_at is not None:
            object.__setattr__(
                self, "known_at", _aware_datetime(self.known_at, "known_at")
            )

    def project(
        self,
        instrument_id: UUID,
        *,
        source: str,
        frequency: str,
        price_basis: PriceBasis,
    ) -> Bar:
        """Project one raw row onto the generic ``Bar`` envelope.

        ``source`` must come from the owning fixture so the evidence
        provenance always matches the mapping source the query resolved.
        """

        return Bar(
            instrument_id=instrument_id,
            trade_date=self.trade_date,
            frequency=frequency,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            amount=self.amount,
            price_basis=price_basis,
            evidence=FactEvidence(
                source=source,
                observed_at=self.observed_at,
                quality_status=self.quality_status,
                known_at=self.known_at,
                source_revision=self.source_revision,
            ),
        )


@dataclass(frozen=True, slots=True)
class PITFixtureFactorRow:
    """One adjustment-factor fact keyed by the source code of its window."""

    source_code: str
    point_date: date
    adj_factor: Decimal | int | str
    observed_at: datetime
    price_basis: PriceBasis = PriceBasis.QFQ
    source_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_code, str) or not self.source_code.strip():
            raise InvalidDataRequestError("source_code must be non-blank text")
        if not isinstance(self.point_date, date) or isinstance(self.point_date, datetime):
            raise InvalidDataRequestError("point_date must be a calendar date")
        normalized = _finite_decimal(self.adj_factor, "adj_factor")
        if normalized <= 0:
            # Factors must be positive even before projection; a non-positive
            # row can never become a consumable AdjustedSeriesPoint.
            raise InvalidDataRequestError("adj_factor must be positive")
        object.__setattr__(self, "adj_factor", normalized)
        object.__setattr__(
            self, "observed_at", _aware_datetime(self.observed_at, "observed_at")
        )
        if not isinstance(self.price_basis, PriceBasis):
            raise InvalidDataRequestError("price_basis must be a PriceBasis")

    def project(self, instrument_id: UUID, *, source: str) -> AdjustedSeriesPoint:
        """Project one factor row onto the generic series-point envelope.

        ``source`` must come from the owning fixture so the evidence
        provenance always matches the mapping source the query resolved.
        """

        return AdjustedSeriesPoint(
            instrument_id=instrument_id,
            point_date=self.point_date,
            price_basis=self.price_basis,
            adj_factor=self.adj_factor,
            evidence=FactEvidence(
                source=source,
                observed_at=self.observed_at,
                quality_status=QualityStatus.COMPLETE,
                source_revision=self.source_revision,
            ),
        )


@dataclass(frozen=True)
class PITMappingFixture:
    """Immutable dataset backing cross-code PIT history tests.

    Construction validates and indexes everything once; later mutation of
    the input collections never changes query results.  ``clock`` is the
    deterministic observation time stamped into projections so tests stay
    reproducible.
    """

    instrument_id: UUID
    mappings: tuple[InstrumentCodeMapping, ...] = ()
    bar_rows: tuple[PITFixtureBarRow, ...] = ()
    factor_rows: tuple[PITFixtureFactorRow, ...] = ()
    source: str = "tushare"
    clock: datetime = field(default_factory=lambda: datetime.now().astimezone())

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise IdentityMappingIncompleteError("instrument_id must be a UUID")
        object.__setattr__(self, "clock", _aware_datetime(self.clock, "clock"))
        # Read-call audit trail: lets tests prove that validation failures
        # happen before any segment data is touched.
        object.__setattr__(self, "read_calls", [])
        mappings = tuple(self.mappings)
        for mapping in mappings:
            if not isinstance(mapping, InstrumentCodeMapping):
                raise IdentityMappingIncompleteError(
                    "mappings entries must be InstrumentCodeMapping instances"
                )
            if (
                mapping.instrument_id != self.instrument_id
                or mapping.source != self.source
            ):
                raise IdentityMappingIncompleteError(
                    "mappings entries must belong to this fixture's "
                    "instrument_id/source pair"
                )
        object.__setattr__(self, "mappings", mappings)

        bar_index: dict[tuple[str, date], list[PITFixtureBarRow]] = {}
        for row in self.bar_rows:
            if not isinstance(row, PITFixtureBarRow):
                raise InvalidDataRequestError(
                    "bar_rows entries must be PITFixtureBarRow instances"
                )
            bar_index.setdefault((row.source_code, row.trade_date), []).append(row)
        object.__setattr__(
            self,
            "_bar_index",
            MappingProxyType({key: tuple(rows) for key, rows in bar_index.items()}),
        )
        factor_index: dict[tuple[str, date], list[PITFixtureFactorRow]] = {}
        for row in self.factor_rows:
            if not isinstance(row, PITFixtureFactorRow):
                raise InvalidDataRequestError(
                    "factor_rows entries must be PITFixtureFactorRow instances"
                )
            factor_index.setdefault((row.source_code, row.point_date), []).append(row)
        object.__setattr__(
            self,
            "_factor_index",
            MappingProxyType(
                {key: tuple(rows) for key, rows in factor_index.items()}
            ),
        )

    # ------------------------------------------------------------------
    # Session-window resolution (flow step 1-2 of the data contract)
    # ------------------------------------------------------------------

    @staticmethod
    def select_window_sessions(
        sessions: Sequence[date],
        *,
        window: DateRange | LookbackWindow,
        boundary: QueryBoundary,
    ) -> tuple[date, ...]:
        """Resolve an explicit-date or lookback window to session dates.

        The ``QueryBoundary`` cutoff rules are enforced here before any
        session selection: an end date touching the incomplete cutoff day
        fails instead of being silently trimmed, and a lookback whose
        ``end_at`` lies past the boundary fails outright.  Lookback
        windows take the last ``sessions`` trading sessions strictly
        before their own ``end_at``; the cutoff day itself counts only
        when the caller proved whole-day completion via
        ``include_cutoff_day``.  An explicit ``DateRange`` selects every
        supplied session inside the inclusive bounds.  A lookback that
        reaches past the provided sessions fails before any data is read.
        """

        ordered = tuple(sessions)
        for day in ordered:
            if not isinstance(day, date) or isinstance(day, datetime):
                raise IdentityMappingIncompleteError(
                    "sessions entries must be calendar dates"
                )
        if any(later <= earlier for earlier, later in zip(ordered, ordered[1:])):
            raise IdentityMappingIncompleteError(
                "sessions must be distinct dates in ascending order"
            )
        # Boundary enforcement: overflow fails, the cutoff day never slips
        # through without the whole-day-completion proof.
        if isinstance(window, LookbackWindow):
            boundary.require_instant_not_past_cutoff(window.end_at, "window.end_at")
        else:
            boundary.require_not_past_cutoff(window.end_date, "window.end_date")
        if isinstance(window, LookbackWindow):
            last_eligible_day = window.end_at.date()
            end_day_admitted = (
                boundary.include_cutoff_day
                and last_eligible_day == boundary.cutoff_date
            )
            eligible = [
                day
                for day in ordered
                if day < last_eligible_day
                or (end_day_admitted and day == last_eligible_day)
            ]
            if len(eligible) < window.sessions:
                raise HistoryIncompleteError(
                    "the lookback window requests more sessions than the "
                    "provided trading sessions provide",
                    details={
                        "requested": window.sessions,
                        "available": len(eligible),
                    },
                )
            return tuple(eligible[-window.sessions :])
        wanted = tuple(
            day
            for day in ordered
            if window.start_date <= day <= window.end_date
        )
        return wanted

    # ------------------------------------------------------------------
    # Production read paths
    # ------------------------------------------------------------------

    def resolve(
        self,
        *,
        sessions: Sequence[date],
        data_cutoff: datetime,
    ) -> PITMappingResolution:
        """Bind every session to exactly one evidenced source code."""

        return resolve_pit_mappings(
            self.instrument_id,
            source=self.source,
            sessions=sessions,
            mappings=self.mappings,
            data_cutoff=data_cutoff,
        )

    def read_history(
        self, resolution: PITMappingResolution, *, frequency: str = "1d"
    ) -> SegmentedBarHistory:
        """Read every segment by source code and stitch one bar series."""

        self._require_supported_frequency(frequency)
        return read_segmented_history(
            resolution, self._SegmentBarReader(self, frequency)
        )

    def read_adjusted_series(
        self, resolution: PITMappingResolution, *, price_basis: PriceBasis
    ) -> SegmentedAdjustedSeries:
        """Read every segment's factors by source code and stitch one series."""

        return read_segmented_adjusted_series(
            resolution, self._SegmentFactorReader(self, price_basis)
        )

    @staticmethod
    def _require_supported_frequency(frequency: str) -> None:
        """Reject any frequency the fixture cannot serve truthfully.

        The fixture only stores daily bars, so answering ``5m`` requests
        with projected ``1d`` facts would silently fabricate a series.
        Every public bar entry point shares this check.
        """

        if frequency != "1d":
            raise InvalidDataRequestError(
                "this fixture only serves 1d bars",
                details={"requested_frequency": frequency},
            )

    @staticmethod
    def _require_matching_cutoff(
        boundary: QueryBoundary, data_cutoff: datetime
    ) -> None:
        """Reject a boundary whose cutoff disagrees with the query's.

        A mismatch would let the window rules and the mapping visibility
        filter be evaluated against different knowledge horizons, so the
        query fails before any selection or read.
        """

        if boundary.data_cutoff != _aware_datetime(data_cutoff, "data_cutoff"):
            raise InvalidDataRequestError(
                "boundary.data_cutoff must match the query's data_cutoff",
                details={
                    "boundary_data_cutoff": boundary.data_cutoff.isoformat(),
                    "query_data_cutoff": _aware_datetime(
                        data_cutoff, "data_cutoff"
                    ).isoformat(),
                },
            )

    def bars(
        self,
        *,
        sessions: Sequence[date],
        window: DateRange | LookbackWindow,
        boundary: QueryBoundary,
        data_cutoff: datetime,
        frequency: str = "1d",
    ) -> SegmentedBarHistory:
        """Full PIT flow: validate, resolve, plan segments, read, validate."""

        self._require_supported_frequency(frequency)
        self._require_matching_cutoff(boundary, data_cutoff)
        wanted_days = self.select_window_sessions(
            sessions, window=window, boundary=boundary
        )
        if not wanted_days:
            raise HistoryIncompleteError(
                "the requested window contains no trading sessions",
                details={"requested_window": repr(window)},
            )
        resolution = self.resolve(sessions=wanted_days, data_cutoff=data_cutoff)
        return self.read_history(resolution, frequency=frequency)

    def adjusted_series(
        self,
        *,
        sessions: Sequence[date],
        window: DateRange | LookbackWindow,
        boundary: QueryBoundary,
        data_cutoff: datetime,
        price_basis: PriceBasis,
    ) -> SegmentedAdjustedSeries:
        """Full PIT flow for adjustment factors over mapped segments."""

        self._require_matching_cutoff(boundary, data_cutoff)
        wanted_days = self.select_window_sessions(
            sessions, window=window, boundary=boundary
        )
        if not wanted_days:
            raise HistoryIncompleteError(
                "the requested window contains no trading sessions",
                details={"requested_window": repr(window)},
            )
        resolution = self.resolve(sessions=wanted_days, data_cutoff=data_cutoff)
        return self.read_adjusted_series(
            resolution, price_basis=price_basis
        )

    # ------------------------------------------------------------------
    # Segment readers (projection happens here, keyed by PIT segment)
    # ------------------------------------------------------------------

    class _SegmentBarReader:
        """Serves projected bars for exactly the requested segment window."""

        def __init__(self, fixture: "PITMappingFixture", frequency: str) -> None:
            self._fixture = fixture
            self._frequency = frequency

        def read_bars(
            self, source_code: str, start_date: date, end_date: date
        ) -> list[Bar]:
            self._fixture.read_calls.append(("bars", source_code))
            rows: list[Bar] = []
            for (code, day), candidates in sorted(
                self._fixture._bar_index.items(),
                key=lambda item: (item[0][0], item[0][1]),
            ):
                if code != source_code or not (start_date <= day <= end_date):
                    continue
                for candidate in candidates:
                    rows.append(
                        candidate.project(
                            self._fixture.instrument_id,
                            source=self._fixture.source,
                            frequency=self._frequency,
                            price_basis=PriceBasis.RAW,
                        )
                    )
            return rows

    class _SegmentFactorReader:
        """Serves projected factors for one basis over a segment window."""

        def __init__(
            self, fixture: "PITMappingFixture", price_basis: PriceBasis
        ) -> None:
            self._fixture = fixture
            self._price_basis = price_basis

        def read_factors(
            self, source_code: str, start_date: date, end_date: date
        ) -> list[AdjustedSeriesPoint]:
            self._fixture.read_calls.append(("factors", source_code))
            rows: list[AdjustedSeriesPoint] = []
            for (code, day), candidates in sorted(
                self._fixture._factor_index.items(),
                key=lambda item: (item[0][0], item[0][1]),
            ):
                if code != source_code or not (start_date <= day <= end_date):
                    continue
                for candidate in candidates:
                    if candidate.price_basis is not self._price_basis:
                        continue
                    rows.append(
                        candidate.project(
                            self._fixture.instrument_id,
                            source=self._fixture.source,
                        )
                    )
            return rows
