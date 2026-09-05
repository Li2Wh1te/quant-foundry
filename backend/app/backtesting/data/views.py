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

from app.backtesting.calendar_axis import normalize_calendar_id
from app.backtesting.data.consistency import BoundedChunkCache
from app.backtesting.data.errors import (
    InvalidDataRequestError,
    ProviderContractViolationError,
    UniverseProviderContractViolationError,
    UniverseCalendarNotPreflightedError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.facts import (
    AdjustedSeriesPoint,
    Bar,
    CorporateAction,
    TradingRule,
    TradingStatus,
)
from app.backtesting.data.requests import (
    BarQuery,
    AdjustedSeriesQuery,
    CorporateActionQuery,
    DateRange,
    LookbackWindow,
    MAX_LOOKBACK_SESSIONS,
    PriceBasis,
    QualityStatus,
    QueryBoundary,
    TradingRuleQuery,
    TradingStatusQuery,
    UniverseQuery as DataUniverseQuery,
)

__all__ = [
    "ChunkEngineDataView",
    "ChunkStrategyDataView",
    "EngineDataView",
    "require_preflighted_calendar_ids",
]


def _candidate_dto_from_spec(spec):
    """Project one complete PIT instrument spec onto the strategy DTO.

    ``InstrumentSpec`` is the engine-side source of truth.  This helper keeps
    the projection deliberately narrow: source codes, rule internals and raw
    corporate-action payloads never cross into strategy code.  All three
    display labels must be present in the already-resolved PIT fact; missing
    identity labels remain a candidate-level qualification failure and are not
    fabricated from a current catalogue snapshot.
    """

    from app.instruments.domain import InstrumentSpec
    from app.strategy_protocol.data_view import InstrumentCandidateDTO

    if isinstance(spec, InstrumentCandidateDTO):
        # Re-project even an already-shaped DTO so provider metadata cannot
        # smuggle source codes, internal fact ids, or raw rule payloads across
        # the strategy boundary.  The public candidate contract is exactly
        # the six display/identity fields below.
        instrument_id = spec.instrument_id
        trading_code = spec.trading_code
        name = spec.name
        display_name = spec.display_name
        asset_class = spec.asset_class
        exchange = spec.exchange
        return InstrumentCandidateDTO(
            instrument_id=instrument_id,
            trading_code=trading_code,
            name=name,
            display_name=display_name,
            asset_class=asset_class,
            exchange=exchange,
        )

    if isinstance(spec, InstrumentSpec):
        display = spec.display
        trading_code = display.trading_code
        name = display.name
        display_name = display.display_name
        instrument_id = spec.instrument_id
        asset_class = spec.asset_class
        exchange = spec.exchange
    else:
        # A task-15 provider may expose an already-resolved candidate input
        # rather than the full spec.  Accept that narrow, immutable shape only
        # when all fields needed by the DTO are explicit; no source-code or
        # current-catalogue lookup is performed here.
        instrument_id = getattr(spec, "instrument_id", None)
        trading_code = getattr(spec, "trading_code", None)
        name = getattr(spec, "name", None)
        display_name = getattr(spec, "display_name", None)
        asset_class = getattr(spec, "asset_class", None)
        exchange = getattr(spec, "exchange", None)
        if not isinstance(instrument_id, UUID):
            raise ProviderContractViolationError(
                "universe provider returned a non-InstrumentSpec row"
            )
    # The DTO is a projection of a complete PIT display fact.  Filling a
    # missing label from another label would silently turn an incomplete
    # historical mapping into a valid-looking candidate, which is expressly
    # forbidden by the universe contract.
    if not all(
        type(value) is str and value.strip()
        for value in (
            trading_code,
            name,
            display_name,
            asset_class,
            exchange,
        )
    ):
        raise UniverseProviderContractViolationError(
            "PIT candidate display identity is incomplete",
            details={"instrument_id": str(instrument_id), "reason_code": "identity_mapping_incomplete"},
        )
    return InstrumentCandidateDTO(
        instrument_id=instrument_id,
        trading_code=trading_code,
        name=name,
        display_name=display_name,
        asset_class=asset_class,
        exchange=exchange,
    )


def _candidate_projection_key(candidate) -> tuple[str, ...]:
    """Stable six-field tie-break for duplicate strategy projections."""

    return (
        candidate.trading_code,
        candidate.name,
        candidate.display_name,
        candidate.asset_class,
        candidate.exchange,
    )


class _ChunkUniverseQuery:
    """Strategy-facing, read-only wrapper over one bound PIT universe query."""

    __slots__ = (
        "__view",
        "__query",
        "__cache",
        "__cache_scope",
        "__queried",
    )

    def __init__(self, view: "ChunkStrategyDataView", query: DataUniverseQuery):
        object.__setattr__(self, "_ChunkUniverseQuery__view", view)
        object.__setattr__(self, "_ChunkUniverseQuery__query", query)
        # One facade belongs to exactly one chunk.  Object identity is a safe
        # private scope marker because cached rows are never persisted or
        # shared, while BoundedChunkCache supplies the common 128-entry LRU
        # ceiling used by chunk-local query caches.
        chunk = getattr(view, "_ChunkStrategyDataView__chunk", None)
        object.__setattr__(
            self,
            "_ChunkUniverseQuery__cache_scope",
            ("chunk", id(chunk)),
        )
        object.__setattr__(
            self,
            "_ChunkUniverseQuery__cache",
            BoundedChunkCache(),
        )
        object.__setattr__(self, "_ChunkUniverseQuery__queried", False)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("universe query is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("universe query is read-only")

    @property
    def effective_date(self):
        """The bound PIT identity date (read-only diagnostic metadata)."""

        return self.__query.effective_date

    @property
    def boundary(self):
        """The bound QueryBoundary (read-only diagnostic metadata)."""

        return self.__query.boundary

    @property
    def market_scope(self):
        """The frozen market scope; strategies cannot replace it."""

        return self.__query.market_scope

    @property
    def universe_query_policy(self):
        """The versioned candidate-set policy frozen into this query."""

        return self.__query.universe_query_policy

    @property
    def scope_mode(self):
        """The immutable fixed/dynamic/hybrid mode of this query."""

        return self.__query.scope_mode

    @property
    def scope_snapshot_hash(self):
        """Optional immutable admission hash carried by the bound query."""

        return getattr(self.__query, "universe_scope_snapshot_hash", None)

    @property
    def filter_reason_counts(self):
        """Expose immutable candidate-filter counts for audit consumers."""

        chunk = getattr(self.__view, "_ChunkStrategyDataView__chunk", None)
        value = getattr(chunk, "universe_filter_reason_counts", {})
        return MappingProxyType(dict(value))

    @property
    def filter_summary(self):
        """Expose provider filter evidence without exposing source objects."""

        chunk = getattr(self.__view, "_ChunkStrategyDataView__chunk", None)
        value = getattr(chunk, "candidate_filter_summary", None)
        if value is None:
            return MappingProxyType(
                {
                    "reason_counts": self.filter_reason_counts,
                    "records": (),
                    "query_hash": None,
                }
            )
        return MappingProxyType(dict(value))

    def for_step(
        self,
        *,
        effective_date: date | None = None,
        data_cutoff: datetime | None = None,
        session_date: date | None = None,
        decision_time: datetime | None = None,
    ) -> "_ChunkUniverseQuery":
        """Derive a fresh PIT query for exactly one decision step.

        The frozen policy, market scope, exception set, calendar ids, and
        optional scope hash are copied verbatim.  Only the two step
        coordinates are replaced, and the data contract validates that the
        resulting cutoff cannot move later than the run's bound.
        """

        del session_date
        query = self.__query
        step_date = effective_date if effective_date is not None else query.effective_date
        step_cutoff = data_cutoff if data_cutoff is not None else (
            decision_time if decision_time is not None else query.boundary.data_cutoff
        )
        # A strategy may narrow visibility for an individual decision, but it
        # must never move the bound later than the cutoff fixed when this view
        # was admitted.  Enforce this before constructing a new query so a
        # provider cannot observe an expanded PIT window even transiently.
        if not isinstance(step_cutoff, datetime) or step_cutoff.tzinfo is None:
            raise InvalidDataRequestError("data_cutoff must be timezone-aware")
        view_cutoff = self._ChunkUniverseQuery__view.data_cutoff
        # Advancing to a later decision date is the normal per-step binding
        # operation; only an expansion within the same PIT date is forbidden.
        # Runtime creates a fresh view for each chunk, while this distinction
        # keeps legacy step resolvers able to move from one session to the
        # next without permitting an in-session cutoff widening attack.
        if step_cutoff > view_cutoff and step_date <= query.effective_date:
            raise InvalidDataRequestError(
                "strategy universe cutoff cannot expand the bound data_cutoff",
                details={
                    "bound_data_cutoff": view_cutoff.isoformat(),
                    "requested_data_cutoff": step_cutoff.isoformat(),
                },
            )
        knowledge_as_of = query.boundary.knowledge_as_of
        if knowledge_as_of is not None and knowledge_as_of > step_cutoff:
            # A step may narrow the cognition cutoff together with the
            # physical cutoff; it may never carry a knowledge time later than
            # the instant being queried.
            knowledge_as_of = step_cutoff
        begin_step = getattr(
            self._ChunkUniverseQuery__view._ChunkStrategyDataView__chunk,
            "begin_decision_step",
            None,
        )
        if callable(begin_step):
            begin_step(step_date)
        boundary = QueryBoundary(
            data_cutoff=step_cutoff,
            knowledge_as_of=knowledge_as_of,
            include_cutoff_day=query.boundary.include_cutoff_day,
        )
        return _ChunkUniverseQuery(
            self.__view,
            DataUniverseQuery(
                rule=query.rule,
                market_scope=query.market_scope,
                effective_date=step_date,
                boundary=boundary,
                allowed_calendar_ids=query.allowed_calendar_ids,
                universe_query_policy=query.universe_query_policy,
                rule_exception_set=query.rule_exception_set,
                qualification_policy_version=query.qualification_policy_version,
                qualification_policy=getattr(query, "qualification_policy", None),
                scope_mode=query.scope_mode,
                universe_scope_snapshot_hash=getattr(
                    query, "universe_scope_snapshot_hash", None
                ),
                rule_package_reference=getattr(query, "rule_package_reference", None),
                frozen_calendar_ids=getattr(query, "frozen_calendar_ids", ()),
            ),
        )

    def query(self, *, exchanges=None, asset_classes=None):
        """Return immutable PIT candidate DTOs after strategy filters."""

        query = self._ChunkUniverseQuery__query
        # A failed narrowing request must not leave the previous query's
        # dynamic IDs usable if a strategy catches the exception and tries a
        # different data entry point.
        self._clear_authorized_candidates()
        # Strategy filters are a narrowing operation only.  Reject values
        # outside the frozen market scope instead of silently broadening it.
        scope = query.market_scope
        def _labels(value, name):
            if value is None:
                return None
            if isinstance(value, (str, bytes)):
                raise InvalidDataRequestError(f"{name} must be an iterable of strings")
            try:
                labels = tuple(sorted({item for item in value}))
            except (TypeError, AttributeError) as exc:
                raise InvalidDataRequestError(f"{name} must be an iterable of strings") from exc
            if any(type(item) is not str or not item.strip() for item in labels):
                raise InvalidDataRequestError(f"{name} entries must be non-blank strings")
            return tuple(item.strip() for item in labels)

        requested_exchanges = _labels(exchanges, "exchanges")
        requested_assets = _labels(asset_classes, "asset_classes")
        if requested_exchanges is not None and scope.exchanges:
            outside = sorted(set(requested_exchanges) - set(scope.exchanges))
            if outside:
                raise InvalidDataRequestError(
                    "strategy exchange filter widens the frozen market scope",
                    details={"outside_scope_exchanges": outside},
                )
        if requested_assets is not None and scope.asset_classes:
            outside = sorted(set(requested_assets) - set(scope.asset_classes))
            if outside:
                raise InvalidDataRequestError(
                    "strategy asset-class filter widens the frozen market scope",
                    details={"outside_scope_asset_classes": outside},
                )
        cache_key = (requested_exchanges, requested_assets)
        cache = self.__cache
        cached = cache.get(self.__cache_scope, cache_key)
        if cached is not None:
            self._bind_authorized_candidates(query, cached)
            object.__setattr__(self, "_ChunkUniverseQuery__queried", True)
            return cached
        specs = self._ChunkUniverseQuery__view._query_universe_specs(query)
        rows = []
        for spec in specs:
            if requested_exchanges is not None and spec.exchange not in requested_exchanges:
                continue
            if requested_assets is not None and spec.asset_class not in requested_assets:
                continue
            try:
                rows.append(_candidate_dto_from_spec(spec))
            except UniverseProviderContractViolationError:
                # Incomplete display facts are candidate-level failures.  The
                # underlying query already recorded the reason summary; no
                # malformed DTO may reach strategy code.
                continue
        # De-duplicate by stable identity with a value-based tie-break.  The
        # provider may expose more than one source/display version for one
        # identity; selecting by input order would make strategy results
        # depend on database iteration order.
        by_id = {}
        for row in rows:
            current = by_id.get(row.instrument_id)
            key = _candidate_projection_key(row)
            if current is None or key < _candidate_projection_key(current):
                by_id[row.instrument_id] = row
        result = tuple(
            by_id[instrument_id]
            for instrument_id in sorted(by_id, key=str)
        )
        # The chunk is the authorization owner.  Bind exactly the narrowed
        # result, not the unfiltered provider result, so a strategy cannot
        # query one exchange and then read another exchange through bars().
        self._bind_authorized_candidates(query, result)
        cache.put(self.__cache_scope, cache_key, result)
        object.__setattr__(self, "_ChunkUniverseQuery__queried", True)
        return result

    def _clear_cache(self) -> None:
        """Release candidate results when the owning runtime chunk ends."""

        self.__cache.clear()

    def _bind_authorized_candidates(self, query, candidates) -> None:
        """Pass the narrowed candidate ids to the owning chunk session."""

        binder = getattr(
            self._ChunkUniverseQuery__view,
            "_authorize_step_candidates",
            None,
        )
        if callable(binder):
            binder(
                tuple(candidate.instrument_id for candidate in candidates),
                query=query,
            )

    def _clear_authorized_candidates(self) -> None:
        """Clear current-step dynamic authorization before validation."""

        clearer = getattr(
            self._ChunkUniverseQuery__view._ChunkStrategyDataView__chunk,
            "clear_step_candidate_authorization",
            None,
        )
        if callable(clearer):
            clearer()

    @property
    def has_queried(self) -> bool:
        """Whether this bound query has served at least one result."""

        return self.__queried

    @property
    def candidate_ids(self) -> frozenset[UUID]:
        """Stable ids returned by this query's narrowed result cache."""

        return frozenset(
            candidate.instrument_id
            for rows in self.__cache.values()
            for candidate in rows
        )


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
        """Engine bar read; only raw prices may cross this boundary.

        The engine has no adjustment semantics.  Rejecting a non-raw query
        before delegating also protects providers whose chunk implementation
        does not repeat the same check; returned rows are re-checked for the
        same reason.
        """

        if not isinstance(query, BarQuery):
            raise InvalidDataRequestError("engine bars query must be a BarQuery")
        if query.price_basis is not PriceBasis.RAW:
            raise ProviderContractViolationError(
                "engine bars require the raw price basis; adjusted prices are strategy-only",
                details={"price_basis": query.price_basis.value},
            )
        rows = tuple(self._ChunkEngineDataView__chunk.bars(query))
        for row in rows:
            if not isinstance(row, Bar):
                raise ProviderContractViolationError(
                    "engine provider returned a non-Bar row"
                )
            if row.price_basis is not PriceBasis.RAW:
                raise ProviderContractViolationError(
                    "engine provider returned an adjusted bar",
                    details={"price_basis": row.price_basis.value},
                )
        return rows

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
        "_ChunkStrategyDataView__effective_date",
        "_ChunkStrategyDataView__bound_universe_query",
    )

    def __init__(
        self,
        *,
        chunk,
        frequency: str,
        data_cutoff: datetime,
        adjustment_gate=None,
        include_cutoff_day: bool = False,
        effective_date: date | None = None,
        universe_query: DataUniverseQuery | None = None,
        step_key: object | None = None,
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
        if effective_date is None:
            effective_date = self._ChunkStrategyDataView__data_cutoff.date()
        if not isinstance(effective_date, date) or isinstance(effective_date, datetime):
            raise InvalidDataRequestError("effective_date must be a calendar date")
        if universe_query is not None and not isinstance(
            universe_query, DataUniverseQuery
        ):
            raise InvalidDataRequestError("universe_query must be a UniverseQuery")
        object.__setattr__(
            self,
            "_ChunkStrategyDataView__effective_date",
            effective_date,
        )
        object.__setattr__(
            self,
            "_ChunkStrategyDataView__bound_universe_query",
            universe_query,
        )
        # Opening a new strategy view is the explicit step boundary for the
        # chunk.  Real chunks clear dynamic ids here; old synthetic chunks
        # simply lack this optional hook and keep their existing behaviour.
        begin_step = getattr(chunk, "begin_decision_step", None)
        if callable(begin_step):
            begin_step(step_key)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(
            "strategy query facades are read-only; query conditions cannot be modified"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError(
            "strategy query facades are read-only; query conditions cannot be deleted"
        )

    @property
    def data_cutoff(self) -> datetime:
        """The immutable strategy visibility cutoff for this step."""

        return self.__data_cutoff

    @property
    def effective_date(self) -> date:
        """The immutable PIT identity date for this step."""

        return self.__effective_date

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
                    lookback_sessions,
                    MAX_LOOKBACK_SESSIONS,
                    instrument_id=resolved_id,
                    expected=f"<= {MAX_LOOKBACK_SESSIONS} sessions",
                    actual=lookback_sessions,
                    data_cutoff=self._ChunkStrategyDataView__data_cutoff,
                )
        for field_name, value in (
            ("start_date", start_date),
            ("end_date", end_date),
        ):
            if value is not None:
                if not isinstance(value, date) or isinstance(value, datetime):
                    raise ValueError(f"{field_name} must be a calendar date")
                if value > cutoff_date:
                    raise DataCutoffViolationError(
                        value,
                        cutoff_date,
                        instrument_id=resolved_id,
                        session_date=value,
                        expected=f"<= {cutoff_date.isoformat()}",
                        actual=value,
                        data_cutoff=self._ChunkStrategyDataView__data_cutoff,
                    )
                # The cutoff day itself stays invisible unless the caller
                # proved whole-day completeness at construction; fail here
                # with the stable strategy error instead of letting the
                # generic query DTO reject the read later.
                if (
                    value == cutoff_date
                    and not self._ChunkStrategyDataView__include_cutoff_day
                ):
                    raise DataCutoffViolationError(
                        value,
                        cutoff_date,
                        instrument_id=resolved_id,
                        session_date=value,
                        expected=f"< {cutoff_date.isoformat()} (cutoff day incomplete)",
                        actual=value,
                        data_cutoff=self._ChunkStrategyDataView__data_cutoff,
                    )
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
            # Invalid/partial facts are useful to preflight, but they are
            # never allowed across the strategy-consumption boundary.
            if row.evidence.quality_status is not QualityStatus.COMPLETE:
                raise InvalidProviderResultError(
                    "provider returned an incomplete or invalid bar"
                )
            # Enforce the generic OHLC safety invariant at the last boundary;
            # asset adapters may add stricter rules, but no strategy should
            # ever receive an inverted range or an open/close outside it.
            if row.high < row.low or not (row.low <= row.open <= row.high) or not (
                row.low <= row.close <= row.high
            ):
                raise InvalidProviderResultError(
                    "provider returned a bar with invalid OHLC relationships"
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
                        if getattr(row, field_name) is not None
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
        """Read verified adjustment factors for a strategy window.

        The generic chunk session owns factor reads; this view only applies
        the strategy boundary and converts the immutable provider facts into
        strategy DTOs.  Providers that do not declare the capability keep the
        existing ``UnsupportedCapabilityError`` behavior.  Price generation
        remains an ETF-adapter concern and is never reimplemented here.
        """

        from app.strategy_protocol.contract import AdjustmentNotActiveError
        from app.strategy_protocol.data_view import AdjustmentBasis, AdjustedSeriesPointDTO

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
        if resolved_basis is AdjustmentBasis.RAW:
            raise UnsupportedCapabilityError(
                "raw prices are served through bars(), not adjusted_series()",
                details={"basis": resolved_basis.value},
            )

        # Apply the same bounded-window rules as ``bars`` before touching the
        # chunk.  Keeping this validation local prevents an adjusted read
        # from widening the fixed run window merely because a provider has a
        # permissive query implementation.
        cutoff_date = self._ChunkStrategyDataView__data_cutoff.date()
        if lookback_sessions is not None:
            if (
                isinstance(lookback_sessions, bool)
                or not isinstance(lookback_sessions, int)
                or lookback_sessions <= 0
            ):
                raise ValueError("lookback_sessions must be a positive integer")
            if lookback_sessions > MAX_LOOKBACK_SESSIONS:
                from app.strategy_protocol.contract import LookbackLimitExceededError

                raise LookbackLimitExceededError(
                    lookback_sessions,
                    MAX_LOOKBACK_SESSIONS,
                    instrument_id=instrument_id,
                    data_cutoff=self._ChunkStrategyDataView__data_cutoff,
                )
            if start_date is not None or end_date is not None:
                raise ValueError(
                    "pass either lookback_sessions or an explicit date range, "
                    "never both"
                )
        for name, value in (("start_date", start_date), ("end_date", end_date)):
            if value is None:
                continue
            if not isinstance(value, date) or isinstance(value, datetime):
                raise ValueError(f"{name} must be a calendar date")
            if value > cutoff_date or (
                value == cutoff_date
                and not self._ChunkStrategyDataView__include_cutoff_day
            ):
                from app.strategy_protocol.contract import DataCutoffViolationError

                raise DataCutoffViolationError(
                    value,
                    cutoff_date,
                    instrument_id=instrument_id,
                    session_date=value,
                    data_cutoff=self._ChunkStrategyDataView__data_cutoff,
                )
        if lookback_sessions is None:
            if start_date is None or end_date is None:
                raise InvalidDataRequestError(
                    "an adjusted-series range needs both start_date and end_date; "
                    "use lookback_sessions for an open-ended history window"
                )
            if start_date > end_date:
                raise ValueError("start_date cannot be after end_date")

        boundary = QueryBoundary(
            data_cutoff=self._ChunkStrategyDataView__data_cutoff,
            include_cutoff_day=self._ChunkStrategyDataView__include_cutoff_day,
        )
        if lookback_sessions is not None:
            window = LookbackWindow(
                sessions=lookback_sessions,
                end_at=self._ChunkStrategyDataView__data_cutoff,
            )
        else:
            window = DateRange(start_date=start_date, end_date=end_date)
        reader = getattr(self._ChunkStrategyDataView__chunk, "adjusted_series", None)
        if not callable(reader):
            raise UnsupportedCapabilityError(
                "this run's provider does not serve verified adjusted series",
                details={"basis": resolved_basis.value},
            )
        rows = reader(
            AdjustedSeriesQuery(
                instrument_ids=instrument_id,
                frequency=self._ChunkStrategyDataView__frequency,
                price_basis=PriceBasis(resolved_basis.value),
                boundary=boundary,
                window=window,
            )
        )
        result: list[AdjustedSeriesPointDTO] = []
        previous: date | None = None
        for row in rows:
            if isinstance(row, AdjustedSeriesPointDTO):
                point = row
            elif isinstance(row, AdjustedSeriesPoint):
                if row.instrument_id != instrument_id:
                    raise ProviderContractViolationError(
                        "provider returned an adjustment point for another instrument"
                    )
                if row.price_basis is not PriceBasis(resolved_basis.value):
                    raise ProviderContractViolationError(
                        "provider returned an adjustment point with another price basis"
                    )
                if row.evidence.quality_status is not QualityStatus.COMPLETE:
                    raise ProviderContractViolationError(
                        "provider returned an incomplete adjustment point"
                    )
                point = AdjustedSeriesPointDTO(
                    instrument_id=row.instrument_id,
                    trade_date=row.point_date,
                    adj_factor=row.adj_factor,
                )
            else:
                raise ProviderContractViolationError(
                    "provider returned a non-adjustment-series row"
                )
            if point.instrument_id != instrument_id:
                raise ProviderContractViolationError(
                    "provider returned an adjustment point for another instrument"
                )
            if point.trade_date > cutoff_date or (
                point.trade_date == cutoff_date
                and not self._ChunkStrategyDataView__include_cutoff_day
            ):
                raise ProviderContractViolationError(
                    "provider returned an adjustment point beyond the data cutoff"
                )
            if previous is not None and point.trade_date <= previous:
                raise ProviderContractViolationError(
                    "provider returned adjustment points out of ascending date order"
                )
            previous = point.trade_date
            result.append(point)
        return tuple(result)

    def universe(self, query=None):
        """Return a strategy-facing PIT universe query facade.

        The lower-level chunk owns the immutable request boundary and the
        candidate qualification/filtering.  This view only projects complete
        ``InstrumentSpec`` rows to the narrow strategy DTO.  Supplying a
        pre-built :class:`~app.backtesting.data.requests.UniverseQuery` is the
        preferred path; when omitted, a bound query is derived from the
        chunk's frozen request and this view's cutoff date for compatibility
        with strategy protocol callers.
        """

        if query is None:
            query = self._ChunkStrategyDataView__bound_universe_query
        if query is None:
            request = getattr(self._ChunkStrategyDataView__chunk, "_session", None)
            request = getattr(request, "_request", None)
            if request is None:
                raise InvalidDataRequestError(
                    "a bound UniverseQuery is required for this strategy view"
                )
            query = DataUniverseQuery(
                rule=request.rule_package,
                market_scope=request.market_scope,
                effective_date=self._ChunkStrategyDataView__effective_date,
                boundary=QueryBoundary(
                    data_cutoff=self._ChunkStrategyDataView__data_cutoff,
                    knowledge_as_of=(
                        min(
                            request.query_boundary.knowledge_as_of,
                            self._ChunkStrategyDataView__data_cutoff,
                        )
                        if request.query_boundary.knowledge_as_of is not None
                        else None
                    ),
                    include_cutoff_day=self._ChunkStrategyDataView__include_cutoff_day,
                ),
                allowed_calendar_ids=tuple(
                    getattr(request, "resolved_calendar_ids", ())
                ),
                universe_query_policy=getattr(
                    request, "universe_query_policy", None
                ),
                rule_exception_set=request.rule_exception_set,
                qualification_policy_version=getattr(
                    request, "qualification_policy_version", None
                ),
                scope_mode=getattr(request, "instrument_scope_mode", None),
                universe_scope_snapshot_hash=getattr(
                    request, "universe_scope_snapshot_hash", None
                ),
            )
        if not isinstance(query, DataUniverseQuery):
            raise InvalidDataRequestError("query must be a UniverseQuery")
        # Defensive boundary at the strategy facade: custom/synthetic chunks
        # may not implement the memory provider's validation, so reject any
        # attempt to move the cutoff or add calendars before delegation.
        if query.boundary.data_cutoff > self.__data_cutoff:
            raise InvalidDataRequestError(
                "strategy universe cutoff cannot expand the bound data_cutoff",
                details={
                    "bound_data_cutoff": self.__data_cutoff.isoformat(),
                    "requested_data_cutoff": query.boundary.data_cutoff.isoformat(),
                },
            )
        bound_query = self.__bound_universe_query
        allowed_source = (
            bound_query.allowed_calendar_ids if bound_query is not None else None
        )
        if allowed_source is None:
            request = getattr(self.__chunk, "_session", None)
            request = getattr(request, "_request", None)
            allowed_source = getattr(request, "resolved_calendar_ids", ())
        if allowed_source:
            allowed = {
                normalize_calendar_id(value)
                for value in allowed_source
            }
            requested = {
                normalize_calendar_id(value)
                for value in query.allowed_calendar_ids
            }
            if requested - allowed:
                raise InvalidDataRequestError(
                    "strategy universe query cannot expand preflighted calendars",
                    details={
                        "unpreflighted_calendar_ids": sorted(requested - allowed),
                    },
                )
        return _ChunkUniverseQuery(self, query)

    def _query_universe_specs(self, query: DataUniverseQuery):
        """Delegate one immutable universe query to the wrapped chunk."""

        reader = getattr(self._ChunkStrategyDataView__chunk, "universe", None)
        if not callable(reader):
            raise UnsupportedCapabilityError(
                "this run's provider does not serve dynamic universe queries"
            )
        return tuple(reader(query))

    def _authorize_step_candidates(self, instrument_ids, *, query):
        """Bind a strategy-filtered candidate set to this chunk, if supported.

        The generic strategy facade can still be used with older synthetic
        chunks that have no authorization hook.  Real data chunks implement
        the hook and validate that every id belongs to the exact provider
        result for this bound PIT query before making it readable through any
        other data method.
        """

        authorizer = getattr(
            self._ChunkStrategyDataView__chunk,
            "authorize_step_candidates",
            None,
        )
        if callable(authorizer):
            authorizer(instrument_ids, query=query)

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

    def _normalize(value: Iterable[str], field_name: str) -> dict[str, str]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            raise InvalidDataRequestError(
                f"{field_name} must be an iterable of strings"
            )
        # Compare canonical ids while retaining the caller's spelling for
        # stable, backwards-compatible diagnostics in the blocking error.
        labels: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise InvalidDataRequestError(
                    f"{field_name} entries must be non-blank strings"
                )
            labels.setdefault(normalize_calendar_id(item), item)
        return labels

    requested = _normalize(candidate_calendar_ids, "candidate_calendar_ids")
    allowed = _normalize(allowed_calendar_ids, "allowed_calendar_ids")
    offender_keys = sorted(requested.keys() - allowed.keys())
    offenders = [requested[key] for key in offender_keys]
    if offenders:
        raise UniverseCalendarNotPreflightedError(
            "the dynamic candidate set introduces calendars that did not "
            "pass this run's preflight",
            details={
                "unpreflighted_calendar_ids": offenders,
                "allowed_calendar_ids": sorted(allowed),
            },
        )
