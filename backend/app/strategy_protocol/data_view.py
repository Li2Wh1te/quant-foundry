"""Data and candidate-set query contract for page strategies.

The protocols are deliberately generic: they key every query by the stable
``instrument_id`` and never expose ETF trading codes, ORM sessions, FastAPI
types, or Tushare clients to strategy code.  Concrete adapters (including the
synthetic fixture) implement the read side; this module owns the boundary
rules that every implementation shares:

* queries strictly respect ``data_cutoff`` and fail on overflow instead of
  silently trimming;
* ``lookback_sessions`` is capped before any data is read;
* cross-code history is stitched from PIT identity segments in stable date
  order, and incomplete mapping evidence or missing bars block the query;
* ``raw`` never applies adjustment factors while ``qfq`` / ``hfq`` require an
  active verified adjustment source.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import UUID

from .contract import (
    MAX_LOOKBACK_SESSIONS,
    AdjustmentNotActiveError,
    DataCutoffViolationError,
    IdentityMappingMissingError,
    IncompleteHistoryError,
    InvalidProviderResultError,
    LookbackLimitExceededError,
)


class AdjustmentBasis(StrEnum):
    """Adjustment bases accepted by ``adjusted_series``."""

    RAW = "raw"
    QFQ = "qfq"
    HFQ = "hfq"


@dataclass(frozen=True, slots=True)
class BarDTO:
    """One immutable daily bar identified by the stable instrument id.

    ``values`` holds only the requested fields as finite ``Decimal`` values;
    binary floats, booleans, and non-finite numbers are rejected.  A missing
    source bar stays absent instead of being forward-filled.
    """

    instrument_id: UUID
    trade_date: date
    values: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise ValueError("instrument_id must be a UUID")
        if isinstance(self.trade_date, datetime) or not isinstance(self.trade_date, date):
            raise ValueError("trade_date must be a calendar date")
        if not isinstance(self.values, Mapping):
            raise ValueError("values must be a mapping")
        normalized: dict[str, Decimal] = {}
        for key, value in self.values.items():
            if not isinstance(key, str):
                raise ValueError("bar field names must be strings")
            normalized[key] = _finite_decimal(value, f"bar values[{key!r}]")
        object.__setattr__(self, "values", MappingProxyType(normalized))


def _finite_decimal(value: object, field_name: str) -> Decimal:
    """Normalize one numeric DTO value, rejecting float/bool/non-finite input."""

    from decimal import Decimal as _Decimal, InvalidOperation

    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(
            f"{field_name} must be a decimal string or Decimal; "
            "binary floats are unsupported"
        )
    if isinstance(value, _Decimal):
        if not value.is_finite():
            raise ValueError(f"{field_name} must be finite")
        return value
    if not isinstance(value, (str, int)):
        raise ValueError(f"{field_name} must be a decimal string or Decimal")
    try:
        normalized = _Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


@dataclass(frozen=True, slots=True)
class AdjustedSeriesPointDTO:
    """One point of an adjustment series keyed by the stable instrument id."""

    instrument_id: UUID
    trade_date: date
    adj_factor: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise ValueError("instrument_id must be a UUID")
        if isinstance(self.trade_date, datetime) or not isinstance(self.trade_date, date):
            raise ValueError("trade_date must be a calendar date")
        factor = _finite_decimal(self.adj_factor, "adj_factor")
        if factor <= 0:
            raise ValueError("adj_factor must be positive")
        object.__setattr__(self, "adj_factor", factor)


@dataclass(frozen=True, slots=True)
class InstrumentCandidateDTO:
    """Read-only candidate with its point-in-time display identity.

    ``instrument_id`` must be a real UUID (the stable identity strategies
    submit targets with); trading code, name, and display name are non-blank
    display-only strings.  ``metadata`` holds deep-frozen JSON values.
    """

    instrument_id: UUID
    trading_code: str
    name: str
    display_name: str
    asset_class: str
    exchange: str
    metadata: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise ValueError("instrument_id must be a UUID")
        for field_name in (
            "trading_code",
            "name",
            "display_name",
            "asset_class",
            "exchange",
        ):
            value = getattr(self, field_name)
            # Exact str only: a str subclass could smuggle mutable attributes
            # from the provider into strategy-visible objects.
            if type(value) is not str or not value.strip():
                raise ValueError(f"{field_name} must be a non-blank string")
        # Metadata accepts JSON values only: mappings and sequences are
        # deep-frozen, scalars are kept, and non-JSON objects (sets, custom
        # classes) are rejected so nothing mutable can reach strategy code.
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        frozen: dict[str, object] = {}
        for key, value in self.metadata.items():
            frozen[_freeze_key(key)] = _freeze_meta(value)
        object.__setattr__(self, "metadata", MappingProxyType(frozen))


def _freeze_meta(value: object) -> object:
    """Recursively freeze one metadata value, rejecting non-JSON objects.

    Only exact JSON scalar types are accepted: subclasses of ``int``/``str``
    etc. can carry mutable attributes and must not pass through as scalars.
    Floats must be finite — NaN and infinities are not standard JSON numbers
    and would break deterministic comparison and strict serialization.
    """

    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("metadata float values must be finite")
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {_freeze_key(key): _freeze_meta(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_meta(item) for item in value)
    raise ValueError(
        "metadata values must be JSON scalars, mappings, or sequences"
    )


def _freeze_key(key: object) -> str:
    """Validate one metadata key, rejecting str subclasses."""

    if type(key) is not str:
        # A str subclass could smuggle mutable attributes into the frozen
        # mapping, so only exact strings are accepted.
        raise ValueError("metadata keys must be plain strings")
    return key


@runtime_checkable
class StrategyDataView(Protocol):
    """Raw read side implemented by real adapters and synthetic fixtures.

    Implementations receive already-validated windows (never past
    ``data_cutoff``, never over the lookback cap) and must return immutable
    DTOs keyed by the queried ``instrument_id``.
    """

    def bars(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
    ) -> Sequence[BarDTO]: ...

    def adjusted_series(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
        basis: AdjustmentBasis,
    ) -> Sequence[AdjustedSeriesPointDTO]: ...


@runtime_checkable
class UniverseQuery(Protocol):
    """Raw candidate-set read side bounded by permission and PIT eligibility."""

    def query(
        self,
        *,
        exchanges: Iterable[str] | None = None,
        asset_classes: Iterable[str] | None = None,
    ) -> Sequence[InstrumentCandidateDTO]: ...


class AdjustmentPolicyGate:
    """Immutable boolean gate backed by a named, versioned policy fact.

    Adjusted series may only be served after the native adjustment-factor
    source has passed real-source verification and been marked active.  The
    gate rejects all attribute writes so strategy code cannot flip it open.
    """

    POLICY_KEY = "tushare_adj_factor_native@1"

    __slots__ = ("_AdjustmentPolicyGate__active",)

    def __init__(self, active: bool) -> None:
        object.__setattr__(self, "_AdjustmentPolicyGate__active", bool(active))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("the adjustment policy gate is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the adjustment policy gate is read-only")

    @classmethod
    def from_policy_key(cls, policy_key: str | None) -> "AdjustmentPolicyGate":
        """Only the verified native adjustment source activates the gate."""

        return cls(active=policy_key == cls.POLICY_KEY)

    @classmethod
    def active_gate(cls) -> "AdjustmentPolicyGate":
        """Gate for a verified and active native adjustment policy."""

        return cls(active=True)

    @classmethod
    def inactive_gate(cls) -> "AdjustmentPolicyGate":
        """Gate used while the policy is unverified or inactive."""

        return cls(active=False)

    def is_active(self) -> bool:
        return self._AdjustmentPolicyGate__active


@dataclass(frozen=True, slots=True)
class _ResolvedWindow:
    start_date: date | None
    end_date: date | None
    lookback_sessions: int | None


class _ReadOnlyFacade:
    """Base that makes the strategy-facing facades fully read-only.

    Strategies must not be able to widen ``data_cutoff``, lift the lookback
    cap, or swap the underlying data provider, so every attribute write is
    rejected regardless of name mangling or slot availability.
    """

    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(
            "strategy query facades are read-only; query conditions cannot be modified"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError(
            "strategy query facades are read-only; query conditions cannot be deleted"
        )


class StrategyDataDTO(_ReadOnlyFacade):
    """Strategy-facing facade that enforces the shared query boundaries.

    The facade validates the window against ``data_cutoff`` and the lookback
    cap *before* delegating to the injected view, so oversized or future-facing
    requests can never reach a partial read.  Provider results are validated
    again on the way out (identity, cutoff, ordering, decimal types), so a
    broken provider cannot leak future rows or mutable floats to strategies.
    The injected view and cutoff are fixed at construction; the underlying
    provider objects are stored under name-mangled attributes so they are not
    part of the strategy-facing surface.  qfq/hfq requests are additionally
    gated on the adjustment policy being active.

    Note: this is the non-adversarial read-only boundary approved for this
    phase.  CPython cannot hide objects from deliberate in-process
    introspection; hostile-code isolation remains the subprocess boundary.
    """

    __slots__ = (
        "__view",
        "__data_cutoff",
        "__max_lookback_sessions",
        "__adjustment_gate",
    )

    def __init__(
        self,
        view: StrategyDataView,
        *,
        data_cutoff: datetime,
        max_lookback_sessions: int = MAX_LOOKBACK_SESSIONS,
        adjustment_gate: AdjustmentPolicyGate | None = None,
    ) -> None:
        if (
            isinstance(max_lookback_sessions, bool)
            or not isinstance(max_lookback_sessions, int)
            or not 1 <= max_lookback_sessions <= MAX_LOOKBACK_SESSIONS
        ):
            # The protocol limit itself can never be raised by configuration;
            # callers may only lower it.
            raise ValueError(
                "max_lookback_sessions must be between 1 and "
                f"{MAX_LOOKBACK_SESSIONS}"
            )
        object.__setattr__(self, "_StrategyDataDTO__view", view)
        object.__setattr__(self, "_StrategyDataDTO__data_cutoff", data_cutoff)
        object.__setattr__(
            self, "_StrategyDataDTO__max_lookback_sessions", max_lookback_sessions
        )
        # Conservative default: without an explicit active gate, adjusted
        # series stay blocked so unverified factors can never be served.
        object.__setattr__(
            self,
            "_StrategyDataDTO__adjustment_gate",
            adjustment_gate or AdjustmentPolicyGate.inactive_gate(),
        )

    def bars(
        self,
        instrument_id: UUID | str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
    ) -> tuple[BarDTO, ...]:
        """Read raw bars ending no later than ``data_cutoff``."""

        resolved_id = self._require_uuid(instrument_id)
        window = self._resolve_window(
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )
        result = tuple(
            self.__view.bars(
                resolved_id,
                start_date=window.start_date,
                end_date=window.end_date,
                lookback_sessions=window.lookback_sessions,
            )
        )
        return self._validate_bars_result(resolved_id, result)

    def adjusted_series(
        self,
        instrument_id: UUID | str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
        basis: str | AdjustmentBasis = AdjustmentBasis.RAW,
    ) -> tuple[AdjustedSeriesPointDTO, ...]:
        """Read an adjustment-bounded series for one stable instrument id."""

        try:
            resolved_basis = AdjustmentBasis(basis)
        except ValueError as exc:
            raise ValueError(f"unknown adjustment basis {basis!r}") from exc
        if resolved_basis is not AdjustmentBasis.RAW and not self.__adjustment_gate.is_active():
            raise AdjustmentNotActiveError(
                "qfq/hfq series require tushare_adj_factor_native@1 to be "
                "verified and active"
            )
        resolved_id = self._require_uuid(instrument_id)
        window = self._resolve_window(
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )
        result = tuple(
            self.__view.adjusted_series(
                resolved_id,
                start_date=window.start_date,
                end_date=window.end_date,
                lookback_sessions=window.lookback_sessions,
                basis=resolved_basis,
            )
        )
        return self._validate_series_result(resolved_id, result)

    def _validate_bars_result(
        self, requested_id: UUID, result: tuple[BarDTO, ...]
    ) -> tuple[BarDTO, ...]:
        """Reject provider rows that violate the identity or cutoff contract."""

        cutoff_date = self.__data_cutoff.date()
        previous: date | None = None
        for bar in result:
            if not isinstance(bar, BarDTO):
                raise InvalidProviderResultError(
                    "provider returned a non-BarDTO row; only immutable bar "
                    "DTOs may reach strategy code"
                )
            if bar.instrument_id != requested_id:
                raise InvalidProviderResultError(
                    f"provider returned instrument_id {bar.instrument_id} "
                    f"for a query on {requested_id}"
                )
            if bar.trade_date > cutoff_date:
                raise InvalidProviderResultError(
                    f"provider returned bar {bar.trade_date} later than "
                    f"data_cutoff {cutoff_date}"
                )
            if previous is not None and bar.trade_date <= previous:
                raise InvalidProviderResultError(
                    "provider returned bars out of ascending date order"
                )
            previous = bar.trade_date
        return result

    def _validate_series_result(
        self, requested_id: UUID, result: tuple[AdjustedSeriesPointDTO, ...]
    ) -> tuple[AdjustedSeriesPointDTO, ...]:
        """Apply the same outbound checks to adjustment series."""

        cutoff_date = self.__data_cutoff.date()
        previous: date | None = None
        for point in result:
            if not isinstance(point, AdjustedSeriesPointDTO):
                raise InvalidProviderResultError(
                    "provider returned a non-AdjustedSeriesPointDTO row; only "
                    "immutable series DTOs may reach strategy code"
                )
            if point.instrument_id != requested_id:
                raise InvalidProviderResultError(
                    f"provider returned instrument_id {point.instrument_id} "
                    f"for a query on {requested_id}"
                )
            if point.trade_date > cutoff_date:
                raise InvalidProviderResultError(
                    f"provider returned series point {point.trade_date} later "
                    f"than data_cutoff {cutoff_date}"
                )
            if previous is not None and point.trade_date <= previous:
                raise InvalidProviderResultError(
                    "provider returned series points out of ascending date order"
                )
            previous = point.trade_date
        return result

    def _require_uuid(self, instrument_id: UUID | str) -> UUID:
        """Accept UUID or canonical string form, nothing else."""

        if isinstance(instrument_id, UUID):
            return instrument_id
        if isinstance(instrument_id, str):
            try:
                return UUID(instrument_id)
            except ValueError as exc:
                raise ValueError(
                    f"instrument_id {instrument_id!r} is not a valid UUID"
                ) from exc
        raise ValueError("instrument_id must be a UUID or UUID string")

    def _resolve_window(
        self,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
    ) -> _ResolvedWindow:
        """Validate the requested window before any data access happens."""

        if lookback_sessions is not None:
            if (
                isinstance(lookback_sessions, bool)
                or not isinstance(lookback_sessions, int)
                or lookback_sessions <= 0
            ):
                raise ValueError("lookback_sessions must be a positive integer")
            if lookback_sessions > self.__max_lookback_sessions:
                # Fail before touching any data source.
                raise LookbackLimitExceededError(
                    lookback_sessions, self.__max_lookback_sessions
                )
        cutoff_date = self.__data_cutoff.date()
        if end_date is not None and end_date > cutoff_date:
            raise DataCutoffViolationError(end_date, cutoff_date)
        if start_date is not None and start_date > cutoff_date:
            raise DataCutoffViolationError(start_date, cutoff_date)
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        if lookback_sessions is not None and (start_date is not None or end_date is not None):
            # Lookback windows are resolved by the trading calendar against
            # the cutoff alone.  Mixing them with an explicit date range
            # would let different providers interpret the combination
            # differently, so the combination is a caller error, always.
            raise ValueError(
                "pass either lookback_sessions or an explicit date range, "
                "never both"
            )
        return _ResolvedWindow(
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )


class UniverseQueryDTO(_ReadOnlyFacade):
    """Strategy-facing candidate-set facade returning validated results.

    Provider output is re-checked on the way out (type, duplicate identity)
    and returned in stable ``instrument_id`` order as an immutable tuple, so
    strategies can never receive mutable or duplicated candidates.
    """

    __slots__ = ("__query",)

    def __init__(self, query: UniverseQuery) -> None:
        object.__setattr__(self, "_UniverseQueryDTO__query", query)

    def query(
        self,
        *,
        exchanges: Iterable[str] | None = None,
        asset_classes: Iterable[str] | None = None,
    ) -> tuple[InstrumentCandidateDTO, ...]:
        """Return PIT-eligible candidates as an immutable sorted tuple."""

        result = self._UniverseQueryDTO__query.query(
            exchanges=exchanges, asset_classes=asset_classes
        )
        candidates = tuple(result)
        seen: set[UUID] = set()
        for candidate in candidates:
            if not isinstance(candidate, InstrumentCandidateDTO):
                raise InvalidProviderResultError(
                    "universe provider returned a non-candidate row"
                )
            if candidate.instrument_id in seen:
                raise InvalidProviderResultError(
                    f"universe provider returned duplicate instrument_id "
                    f"{candidate.instrument_id}"
                )
            seen.add(candidate.instrument_id)
        return tuple(sorted(candidates, key=lambda c: str(c.instrument_id)))


@dataclass(frozen=True, slots=True)
class PitSegment:
    """One point-in-time identity segment of a stable instrument.

    A stable ``instrument_id`` may have traded under several historical codes.
    Each segment records which trading code was valid for which inclusive date
    range, providing the mapping evidence required for cross-code reads.
    """

    trading_code: str
    effective_from: date
    effective_to: date

    def __post_init__(self) -> None:
        if self.effective_from > self.effective_to:
            raise ValueError("segment effective_from cannot be after effective_to")

    def covers(self, day: date) -> bool:
        return self.effective_from <= day <= self.effective_to

    def clamp_window(
        self, start_date: date | None, end_date: date | None
    ) -> tuple[date, date]:
        """Intersect the segment validity with the requested window."""

        lower = self.effective_from
        upper = self.effective_to
        if start_date is not None and start_date > lower:
            lower = start_date
        if end_date is not None and end_date < upper:
            upper = end_date
        return lower, upper


SegmentReader = Callable[[str, date, date], Sequence[BarDTO]]


def stitch_segmented_history(
    segments: Sequence[PitSegment],
    *,
    sessions: Sequence[date],
    read_segment: SegmentReader,
) -> tuple[BarDTO, ...]:
    """Read per-segment windows and stitch them back into one stable series.

    ``sessions`` lists the tradable sessions of the requested window in
    ascending order without duplicates (resolved by the trading calendar);
    unsorted or duplicated input is rejected instead of silently normalized.
    Every requested session must be covered by exactly one identity segment:
    coverage gaps and overlapping segments both raise
    :class:`IdentityMappingMissingError` instead of silently returning a
    shorter window or duplicated bars.  Each segment reader must return a bar
    for every session inside its clamped range; missing bars raise
    :class:`IncompleteHistoryError` and are never forward-filled.  The result
    is sorted by trade date regardless of segment order.
    """

    ordered_sessions = list(sessions)
    if any(
        later <= earlier
        for earlier, later in zip(ordered_sessions, ordered_sessions[1:])
    ):
        raise ValueError("sessions must contain distinct ascending dates")
    if not ordered_sessions:
        return ()

    # Interval semantics are unified to half-open [from, to) everywhere:
    # the public closed PitSegment bounds are converted explicitly here so
    # exactly one interval convention enters the query path.  Sessions are
    # discrete dates, so the conversion is lossless.
    def _covers(segment: PitSegment, day: date) -> bool:
        return segment.effective_from <= day < segment.effective_to + timedelta(
            days=1
        )

    # Coverage is verified before any provider read so overlapping segments
    # can never be queried twice, and gaps never trigger partial reads.
    coverage: dict[date, list[PitSegment]] = {}
    for day in ordered_sessions:
        covering = [segment for segment in segments if _covers(segment, day)]
        if not covering:
            raise IdentityMappingMissingError(
                f"no PIT identity mapping covers session {day}"
            )
        if len(covering) > 1:
            raise IdentityMappingMissingError(
                f"PIT identity mappings overlap on session {day}: "
                f"{sorted(segment.trading_code for segment in covering)}"
            )
        coverage[day] = covering

    collected: list[BarDTO] = []
    for segment in segments:
        lower, upper = segment.clamp_window(ordered_sessions[0], ordered_sessions[-1])
        if lower > upper:
            continue
        expected = [day for day in ordered_sessions if _covers(segment, day)]
        if not expected:
            continue
        rows = tuple(read_segment(segment.trading_code, expected[0], expected[-1]))
        # Every segment row must be a real immutable BarDTO, mirroring the
        # provider validation on the direct facade path.
        for row in rows:
            if not isinstance(row, BarDTO):
                raise InvalidProviderResultError(
                    f"segment reader for {segment.trading_code} returned a "
                    "non-BarDTO row; only immutable bar DTOs are accepted"
                )
        # Each segment must return exactly the requested sessions: no
        # duplicates, no out-of-range or non-session dates, and no short
        # windows.
        expected_set = set(expected)
        seen_dates: set[date] = set()
        for row in rows:
            if row.trade_date not in expected_set:
                raise IncompleteHistoryError(
                    f"segment {segment.trading_code} returned a bar outside "
                    f"the requested sessions on {row.trade_date}"
                )
            if row.trade_date in seen_dates:
                raise IncompleteHistoryError(
                    f"segment {segment.trading_code} returned duplicate bars "
                    f"for session {row.trade_date}"
                )
            seen_dates.add(row.trade_date)
        if len(rows) != len(expected):
            missing = [day for day in expected if day not in seen_dates]
            raise IncompleteHistoryError(
                f"history bars are missing for {len(missing)} sessions, "
                f"first missing {missing[0]}"
            )
        collected.extend(rows)
    return tuple(sorted(collected, key=lambda bar: bar.trade_date))


__all__ = [
    "AdjustmentBasis",
    "AdjustmentPolicyGate",
    "AdjustedSeriesPointDTO",
    "BarDTO",
    "InstrumentCandidateDTO",
    "PitSegment",
    "StrategyDataDTO",
    "StrategyDataView",
    "UniverseQuery",
    "UniverseQueryDTO",
    "stitch_segmented_history",
]
