"""Engine and strategy data views over one bounded chunk session.

One logical :class:`~app.backtesting.data.protocols.DataChunkSession` can
legitimately back two different read surfaces inside the provider boundary:

* :class:`EngineDataView` -- the engine-only surface (timeline matching,
  valuation, trading rules, trading status, corporate actions).  It may
  read the *current* official session because matching needs it.
* the strategy-facing view (:class:`ChunkStrategyDataView`, which
  satisfies the existing ``app.strategy_protocol.data_view.StrategyDataView``
  protocol) -- bars, adjustment series, and candidate sets visible no
  later than ``data_cutoff``, returned as immutable DTOs keyed by the
  stable ``instrument_id``.

Strategies receive only the strategy view.  Neither view hands out the
underlying chunk session, provider, database session, ORM objects, or any
access credential, and both facades reject attribute writes so query
conditions cannot be mutated at runtime.  Dynamic candidate sets are
bounded by :func:`require_preflighted_calendar_ids`: a candidate whose
calendar was never preflighted for this run blocks instead of widening
the frozen time axis.

This module deliberately contains no ORM, database session, FastAPI, or
Tushare imports.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Iterable, Protocol, runtime_checkable
from uuid import UUID

from app.backtesting.data.errors import (
    InvalidDataRequestError,
    UniverseCalendarNotPreflightedError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.facts import (
    Bar,
    CorporateAction,
    TradingRule,
    TradingStatus,
)
from app.backtesting.data.requests import (
    BarQuery,
    CorporateActionQuery,
    DateRange,
    LookbackWindow,
    MAX_LOOKBACK_SESSIONS,
    PriceBasis,
    QueryBoundary,
    TradingRuleQuery,
    TradingStatusQuery,
)

__all__ = [
    "ChunkEngineDataView",
    "ChunkStrategyDataView",
    "EngineDataView",
    "require_preflighted_calendar_ids",
]


# ---------------------------------------------------------------------------
# Engine view
# ---------------------------------------------------------------------------


@runtime_checkable
class EngineDataView(Protocol):
    """Engine-only read surface over one bounded chunk.

    Exposed to the timeline, matching, valuation, and rule engines; never
    handed to strategy code.  Engine reads may cover the current official
    session (for example the open price of the day being matched), which
    is precisely what the strategy-visible view must refuse.
    """

    def bars(self, query: BarQuery) -> tuple[Bar, ...]: ...
    def trading_rules(self, query: TradingRuleQuery) -> tuple[TradingRule, ...]: ...
    def trading_status(
        self, query: TradingStatusQuery
    ) -> tuple[TradingStatus, ...]: ...
    def corporate_actions(
        self, query: CorporateActionQuery
    ) -> tuple[CorporateAction, ...]: ...


class ChunkEngineDataView:
    """Concrete engine view delegating engine reads to one chunk session.

    The wrapped chunk stays private: callers of this facade can only run
    the four engine reads, never reach the chunk session object itself,
    its provider, or anything beyond the chunk's bounds.
    """

    __slots__ = ("_ChunkEngineDataView__chunk",)

    def __init__(self, chunk) -> None:
        if not callable(getattr(chunk, "bars", None)):
            raise InvalidDataRequestError(
                "chunk must expose a bars() business query"
            )
        object.__setattr__(self, "_ChunkEngineDataView__chunk", chunk)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("the engine data view is read-only once constructed")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the engine data view is read-only once constructed")

    def bars(self, query: BarQuery) -> tuple[Bar, ...]:
        """Engine bar read; the chunk enforces its own bounded window."""

        return self._ChunkEngineDataView__chunk.bars(query)

    def trading_rules(self, query: TradingRuleQuery) -> tuple[object, ...]:
        return self._ChunkEngineDataView__chunk.trading_rules(query)

    def trading_status(self, query: TradingStatusQuery) -> tuple[object, ...]:
        return self._ChunkEngineDataView__chunk.trading_status(query)

    def corporate_actions(self, query: CorporateActionQuery) -> tuple[object, ...]:
        return self._ChunkEngineDataView__chunk.corporate_actions(query)


# ---------------------------------------------------------------------------
# Strategy view
# ---------------------------------------------------------------------------

_BAR_VALUE_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _aware_cutoff(value: datetime, field_name: str) -> datetime:
    """Require a timezone-aware cutoff instant."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidDataRequestError(f"{field_name} must be timezone-aware")
    return value


class ChunkStrategyDataView:
    """Strategy-facing read view over one bounded chunk session.

    Satisfies the existing
    ``app.strategy_protocol.data_view.StrategyDataView`` protocol so no
    second strategy contract exists.  Every request is validated against
    ``data_cutoff`` and the 512-session lookback cap *before* the chunk
    is touched; oversized or future-facing queries therefore fail without
    any read.  Results are immutable :class:`BarDTO` tuples keyed by the
    stable instrument id, ascending by trade date, with gaps preserved.
    """

    __slots__ = (
        "_ChunkStrategyDataView__chunk",
        "_ChunkStrategyDataView__frequency",
        "_ChunkStrategyDataView__data_cutoff",
        "_ChunkStrategyDataView__adjustment_gate",
        "_ChunkStrategyDataView__include_cutoff_day",
    )

    def __init__(
        self,
        *,
        chunk,
        frequency: str,
        data_cutoff: datetime,
        adjustment_gate=None,
        include_cutoff_day: bool = False,
    ) -> None:
        if not callable(getattr(chunk, "bars", None)):
            raise InvalidDataRequestError(
                "chunk must expose a bars() business query"
            )
        if not isinstance(frequency, str) or not frequency.strip():
            raise InvalidDataRequestError("frequency must be non-blank text")
        object.__setattr__(
            self,
            "_ChunkStrategyDataView__chunk",
            chunk,
        )
        object.__setattr__(self, "_ChunkStrategyDataView__frequency", frequency)
        object.__setattr__(
            self,
            "_ChunkStrategyDataView__data_cutoff",
            _aware_cutoff(data_cutoff, "data_cutoff"),
        )
        # Conservative default: without an explicitly active gate, adjusted
        # series stay blocked so unverified factors can never be served.
        if adjustment_gate is None:
            from app.strategy_protocol.data_view import AdjustmentPolicyGate

            adjustment_gate = AdjustmentPolicyGate.inactive_gate()
        object.__setattr__(
            self,
            "_ChunkStrategyDataView__adjustment_gate",
            adjustment_gate,
        )
        # The cutoff day stays invisible unless the caller has PROVEN that
        # the whole day's facts are already complete at ``data_cutoff``
        # (for example an after-close cutoff over a completed daily
        # series).  An intraday or pre-open cutoff must keep the default:
        # serving a full daily bar for an unfinished day would leak data
        # the cutoff has not produced yet.
        if not isinstance(include_cutoff_day, bool):
            raise InvalidDataRequestError("include_cutoff_day must be a boolean")
        object.__setattr__(
            self,
            "_ChunkStrategyDataView__include_cutoff_day",
            include_cutoff_day,
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(
            "strategy query facades are read-only; query conditions cannot be modified"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError(
            "strategy query facades are read-only; query conditions cannot be deleted"
        )

    # ------------------------------------------------------------------
    # StrategyDataView protocol surface
    # ------------------------------------------------------------------

    def bars(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
    ):
        """Read raw bars ending no later than ``data_cutoff``."""

        from app.strategy_protocol.contract import (
            DataCutoffViolationError,
            LookbackLimitExceededError,
        )
        from app.strategy_protocol.data_view import BarDTO

        resolved_id = self._require_uuid(instrument_id)
        cutoff_date = self._ChunkStrategyDataView__data_cutoff.date()
        if lookback_sessions is not None:
            if (
                isinstance(lookback_sessions, bool)
                or not isinstance(lookback_sessions, int)
                or lookback_sessions <= 0
            ):
                raise ValueError("lookback_sessions must be a positive integer")
            if lookback_sessions > MAX_LOOKBACK_SESSIONS:
                # Fail before touching any data source.
                raise LookbackLimitExceededError(
                    lookback_sessions, MAX_LOOKBACK_SESSIONS
                )
        for field_name, value in (
            ("start_date", start_date),
            ("end_date", end_date),
        ):
            if value is not None:
                if not isinstance(value, date) or isinstance(value, datetime):
                    raise ValueError(f"{field_name} must be a calendar date")
                if value > cutoff_date:
                    raise DataCutoffViolationError(value, cutoff_date)
                # The cutoff day itself stays invisible unless the caller
                # proved whole-day completeness at construction; fail here
                # with the stable strategy error instead of letting the
                # generic query DTO reject the read later.
                if (
                    value == cutoff_date
                    and not self._ChunkStrategyDataView__include_cutoff_day
                ):
                    raise DataCutoffViolationError(value, cutoff_date)
        if (
            lookback_sessions is not None
            and (start_date is not None or end_date is not None)
        ):
            raise ValueError(
                "pass either lookback_sessions or an explicit date range, "
                "never both"
            )
        if (
            start_date is None
            and end_date is None
            and lookback_sessions is None
        ):
            raise InvalidDataRequestError(
                "a strategy bar read needs an explicit range or a "
                "lookback window; unbounded reads do not exist"
            )
        if lookback_sessions is None and (start_date is None or end_date is None):
            raise InvalidDataRequestError(
                "an explicit range needs both start_date and end_date; "
                "use lookback_sessions for an open-ended history window"
            )
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")

        # The boundary includes the cutoff day only when the caller proved
        # whole-day completeness at construction; otherwise the cutoff day
        # itself fails closed instead of leaking an unfinished daily bar.
        boundary = QueryBoundary(
            data_cutoff=self._ChunkStrategyDataView__data_cutoff,
            include_cutoff_day=(
                self._ChunkStrategyDataView__include_cutoff_day
            ),
        )
        if lookback_sessions is not None:
            window = LookbackWindow(
                sessions=lookback_sessions,
                end_at=self._ChunkStrategyDataView__data_cutoff,
            )
        else:
            window = DateRange(start_date=start_date, end_date=end_date)
        rows = self._ChunkStrategyDataView__chunk.bars(
            BarQuery(
                instrument_ids=resolved_id,
                frequency=self._ChunkStrategyDataView__frequency,
                boundary=boundary,
                window=window,
            )
        )
        from app.strategy_protocol.contract import InvalidProviderResultError

        cutoff_date = self._ChunkStrategyDataView__data_cutoff.date()
        previous: date | None = None
        result: list[BarDTO] = []
        for row in rows:
            # Second line of defence at the strategy boundary: the view
            # never trusts the underlying provider to have policed identity,
            # frequency, basis, or visibility.
            if not isinstance(row, Bar):
                raise InvalidProviderResultError(
                    "provider returned a non-Bar row; only immutable bar "
                    "facts may reach strategy code"
                )
            if row.instrument_id != resolved_id:
                raise InvalidProviderResultError(
                    f"provider returned instrument_id {row.instrument_id} "
                    f"for a query on {resolved_id}"
                )
            if row.frequency != self._ChunkStrategyDataView__frequency:
                raise InvalidProviderResultError(
                    "provider returned a bar with an unexpected frequency",
                )
            if row.price_basis is not PriceBasis.RAW:
                raise InvalidProviderResultError(
                    "provider returned a bar with a non-raw price basis"
                )
            if row.trade_date > cutoff_date:
                raise InvalidProviderResultError(
                    f"provider returned bar {row.trade_date.isoformat()} "
                    f"later than data_cutoff {cutoff_date.isoformat()}"
                )
            if (
                row.trade_date == cutoff_date
                and not self._ChunkStrategyDataView__include_cutoff_day
            ):
                raise InvalidProviderResultError(
                    f"provider returned bar {row.trade_date.isoformat()} on "
                    "the cutoff day, which this view has not proven complete"
                )
            if start_date is not None and row.trade_date < start_date:
                raise InvalidProviderResultError(
                    f"provider returned bar {row.trade_date.isoformat()} "
                    "before the requested start date"
                )
            if end_date is not None and row.trade_date > end_date:
                raise InvalidProviderResultError(
                    f"provider returned bar {row.trade_date.isoformat()} "
                    "beyond the requested end date"
                )
            dto = BarDTO(
                instrument_id=row.instrument_id,
                trade_date=row.trade_date,
                values=MappingProxyType(
                    {
                        field_name: Decimal(str(getattr(row, field_name)))
                        for field_name in _BAR_VALUE_FIELDS
                    }
                ),
            )
            if previous is not None and dto.trade_date <= previous:
                raise InvalidProviderResultError(
                    "provider returned bars out of ascending date order"
                )
            previous = dto.trade_date
            result.append(dto)
        return tuple(result)

    def adjusted_series(
        self,
        instrument_id: UUID,
        *,
        basis,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
    ):
        """Adjusted series stay blocked until their source is verified."""

        from app.strategy_protocol.contract import AdjustmentNotActiveError
        from app.strategy_protocol.data_view import AdjustmentBasis

        try:
            resolved_basis = AdjustmentBasis(basis)
        except ValueError as exc:
            raise ValueError(f"unknown adjustment basis {basis!r}") from exc
        if resolved_basis is not AdjustmentBasis.RAW and (
            not self._ChunkStrategyDataView__adjustment_gate.is_active()
        ):
            raise AdjustmentNotActiveError(
                "qfq/hfq series require tushare_adj_factor_native@1 to be "
                "verified and active"
            )
        raise UnsupportedCapabilityError(
            "this run's provider does not serve verified adjusted series",
            details={"basis": resolved_basis.value},
        )

    def universe(self, query=None):
        """Dynamic candidate sets are not served by this task package."""

        raise UnsupportedCapabilityError(
            "this run's provider does not serve dynamic universe queries"
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _require_uuid(instrument_id: object) -> UUID:
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


# ---------------------------------------------------------------------------
# Dynamic candidate-set calendar gate (run-preflight bounded)
# ---------------------------------------------------------------------------


def require_preflighted_calendar_ids(
    candidate_calendar_ids: Iterable[str],
    *,
    allowed_calendar_ids: Iterable[str],
) -> None:
    """Block candidates whose calendar was never preflighted for this run.

    A dynamic candidate set may only introduce calendars that passed the
    run's compatibility preflight: anything else would force a time-axis
    rebuild or silently widen the run's scope, so the offending calendar
    ids block the candidate set with the stable
    ``universe_calendar_not_preflighted`` error instead.
    """

    def _normalize(value: Iterable[str], field_name: str) -> frozenset[str]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            raise InvalidDataRequestError(
                f"{field_name} must be an iterable of strings"
            )
        labels: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise InvalidDataRequestError(
                    f"{field_name} entries must be non-blank strings"
                )
            labels.add(item)
        return frozenset(labels)

    requested = _normalize(candidate_calendar_ids, "candidate_calendar_ids")
    allowed = _normalize(allowed_calendar_ids, "allowed_calendar_ids")
    offenders = sorted(requested - allowed)
    if offenders:
        raise UniverseCalendarNotPreflightedError(
            "the dynamic candidate set introduces calendars that did not "
            "pass this run's preflight",
            details={
                "unpreflighted_calendar_ids": offenders,
                "allowed_calendar_ids": sorted(allowed),
            },
        )
