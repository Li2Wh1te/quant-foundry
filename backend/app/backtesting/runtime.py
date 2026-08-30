"""Deterministic phase-loop runtime for the backtesting engine.

This module owns the main loop that walks the official :class:`TimeAxis`,
asks the registered :class:`TimingPolicy` for the ordered phase
instructions of every step, and advances exactly one phase at a time:

* the ``decide`` phase sees only a :class:`StrategyDataDTO` strategy view
  strictly bounded by ``data_cutoff`` -- engine market data, providers, and
  future bars are unreachable through it;
* ``match``/``value`` phases see an engine-internal
  :class:`EngineDataView` that is never handed to strategy code;
* every observable outcome becomes an immutable :class:`EventEnvelope`
  with a run-global monotonic ``event_sequence``;
* all run-scoped identifiers (decision, intent, order) are derived from
  ``run_id + step_sequence + local index`` via deterministic ``uuid5``
  namespaces, so identical inputs reproduce identical business results.

Phase keys follow the committed ``after_close_to_next_open@1`` timing
policy: ``pre_open_settle -> observe -> match -> account -> cash_actions ->
value -> analyze -> decide -> submit``, where ``pre_open_settle`` restores
T+1 sale availability before the open match (the task package's
``settle_restore``), ``observe`` observes the session open, and
``cash_actions`` freezes record-date dividend entitlements and credits
cash dividends in their derived cash-effective session, strictly after
that session's opening match (the task package's ``cash_effective``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from enum import StrEnum
from inspect import Parameter, signature
from types import MappingProxyType
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.backtesting.accounting import (
    AccountingPolicy,
    DeferredSettlementPlan,
    Fill,
    OrderSide,
)
from app.backtesting.analysis_inputs import (
    AppliedFillFact,
    EquityObservation,
    FillObservation,
)
from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    PortfolioSnapshot,
    PortfolioState,
    ValuationStatus,
    _aware_datetime,
    _positive,
)
from app.backtesting.dividends import (
    CashDividendEvent,
    DividendDerivationError,
    DividendError,
    derive_cash_effective_session,
    entitlement_from_portfolio,
)
from app.backtesting.session_matching import (
    _clone_portfolio,
    _copy_shadow_into,
)
from app.backtesting.execution import (
    ExecutionModel,
    MarketState,
    MatchContext,
    Order,
    OrderIntent,
    OrderStatus,
    PriceLimitStatus,
)
from app.backtesting.time_axis import TimeAxis, TimeStep
from app.backtesting.timing import DataViewKind, TimingInstruction, TimingPhase, TimingPolicy
from app.strategy_protocol.context import (
    DecisionContext,
    DeterministicClockDTO,
    FillSummaryDTO,
    OrderSummaryDTO,
    PortfolioDTO,
    PositionDTO,
    PreviousStepDTO,
)
from app.strategy_protocol.data_view import (
    AdjustmentPolicyGate,
    InstrumentCandidateDTO,
    StrategyDataDTO,
    StrategyDataView,
    UniverseQuery,
    UniverseQueryDTO,
)
from app.backtesting.result_models import (
    InstrumentDisplaySnapshot,
)

__all__ = [
    "BacktestEventType",
    "BacktestRunResult",
    "BacktestViewFactory",
    "CorporateActionSource",
    "DecisionInterpreter",
    "DeterministicBacktestRunner",
    "EngineDataView",
    "EngineMarketData",
    "EquitySample",
    "EventEnvelope",
    "InstrumentFacts",
    "OrderOutcomeRecord",
    "PhaseContext",
    "PhaseExecutionError",
    "SessionQuote",
    "SettlementCalendarGateway",
    "SettlementScheduleError",
    "StrategyProgram",
    "TargetWeightsInterpreter",
    "ValuationBlockedError",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _json_safe_runtime_value(value: Any) -> Any:
    """Convert runtime evidence to the JSON-safe scalar vocabulary.

    Final candidate qualification errors cross the runtime boundary and are
    persisted through the existing decision ``validation_issues`` JSON field.
    Keeping this conversion local prevents UUID/date/Decimal objects (and,
    more importantly, provider or ORM objects) from leaking into that field.
    """

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        return _json_safe_runtime_value(enum_value)
    if isinstance(value, Mapping):
        forbidden_keys = {
            "token",
            "raw_token",
            "access_token",
            "credential",
            "credentials",
            "secret",
            "password",
            "api_key",
            "access_key",
        }
        return {
            str(key): _json_safe_runtime_value(item)
            for key, item in value.items()
            if str(key).strip().lower() not in forbidden_keys
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe_runtime_value(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    # ``repr`` is deliberately the last resort.  It keeps an error useful
    # without serialising a provider client, connection, or credential.
    return repr(value)


def _freeze_runtime_evidence(value: Any) -> Any:
    """Deep-freeze already JSON-safe runtime evidence."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_runtime_evidence(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_runtime_evidence(item) for item in value)
    return value


class PhaseExecutionError(DomainValidationError):
    """Raised when one phase fails; always carries its exact location.

    The original error type name and message are preserved as structured
    fields so callers can surface failures without losing the phase
    coordinates inside the run.
    """

    def __init__(
        self,
        *,
        run_id: str,
        step_sequence: int,
        phase_sequence: int,
        phase_key: str,
        error_type: str,
        message: str,
        error_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"phase {phase_key} failed at step {step_sequence}: "
            f"{error_type}: {message}"
        )
        self.run_id = run_id
        self.step_sequence = step_sequence
        self.phase_sequence = phase_sequence
        self.phase_key = phase_key
        self.error_type = error_type
        # Preserve the stable machine code and structured evidence of a data
        # contract error while retaining the established phase wrapper.  The
        # result layer can therefore persist the exact failed qualification
        # without parsing the human-readable exception text.
        self.error_code = error_code
        self.details = _freeze_runtime_evidence(
            _json_safe_runtime_value(dict(details or {}))
        )

    @property
    def code(self) -> str | None:
        """Expose the wrapped stable code using the data-error convention."""

        return self.error_code


class SettlementScheduleError(DomainValidationError):
    """Raised when a T+1 settlement session cannot be resolved from the
    named trading calendar.  Natural-calendar-day guesses are never used."""


class ValuationBlockedError(DomainValidationError):
    """Raised when a close valuation is blocked by missing marks.

    Continuing to decide and submit on a stale equity would let the
    strategy trade on outdated facts, so the run terminates at the value
    phase instead.
    """


_VERSION_ATTRS = ("policy_version", "model_version", "interpreter_version")


def _component_version_of(obj: Any) -> int | None:
    """Return the component's registered version, whichever attr it uses."""

    for attr in _VERSION_ATTRS:
        version = getattr(obj, attr, None)
        if isinstance(version, int) and not isinstance(version, bool):
            return version
    return None


def _enum_value_or_self(value: Any) -> Any:
    """Render enum fields as their stable string values inside snapshots."""

    return getattr(value, "value", value)


def _is_step_sequence(steps: Any) -> bool:
    """Reject non-iterable or string inputs before any tuple() conversion.

    ``tuple(None)`` and ``tuple(object())`` raise bare TypeErrors, so the
    runner validates the container shape first and fails with a stable
    domain error instead.  Strings are sequences of the wrong element type.
    """

    if isinstance(steps, (str, bytes)):
        return False
    return hasattr(steps, "__iter__")


def _require_registered_identity(
    kind: str, obj: Any, key_attr: str
) -> None:
    """Admission check: replaceable components must be registry-built.

    A component without a stable key/version cannot be audited or
    reproduced, so the runner refuses it instead of accepting an anonymous
    implementation.
    """

    key = getattr(obj, key_attr, None)
    version = _component_version_of(obj)
    if not isinstance(key, str) or not key.strip():
        raise DomainValidationError(
            f"{kind} must carry a stable {key_attr}; construct it through "
            "the versioned ComponentRegistry"
        )
    if version is None:
        raise DomainValidationError(
            f"{kind} must carry a stable integer version; construct it "
            "through the versioned ComponentRegistry"
        )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class BacktestEventType(StrEnum):
    """Business event types emitted by the phase loop."""

    SETTLEMENT_RESTORED = "settlement_restored"
    FILL_CREATED = "fill_created"
    FILL_APPLIED = "fill_applied"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_EXPIRED = "order_expired"
    CASH_DIVIDEND_APPLIED = "cash_dividend_applied"
    PORTFOLIO_VALUED = "portfolio_valued"
    STRATEGY_DECISION_CREATED = "strategy_decision_created"


def _freeze_payload(value: Any) -> Any:
    """Deep-freeze one event payload value against later mutation.

    Tuples are recursed into like lists: a tuple remains a valid nesting
    container, but mutable mappings inside it must still be frozen.  Sets
    are rejected outright because their iteration order is not
    deterministic and they cannot be frozen safely.
    """

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_payload(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)
    if isinstance(value, (set, frozenset)):
        raise DomainValidationError(
            "event payloads must not contain set containers"
        )
    return value


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """One immutable business fact on the run-global event stream."""

    run_id: str
    event_sequence: int
    step_sequence: int
    phase_sequence: int
    phase_key: str
    event_type: str
    event_time: datetime
    payload: Mapping[str, Any]
    # A single-instrument event carries its immutable point-in-time display
    # identity here in addition to the JSON-safe payload projection.  Events
    # that mention several instruments use ``display_snapshots`` below.
    display_snapshot: InstrumentDisplaySnapshot | None = None
    display_snapshots: Mapping[UUID, InstrumentDisplaySnapshot] = MappingProxyType({})

    def __post_init__(self) -> None:
        _aware_datetime(self.event_time, "event_time")
        if isinstance(self.event_sequence, bool) or not isinstance(
            self.event_sequence, int
        ):
            raise DomainValidationError("event_sequence must be an integer")
        object.__setattr__(self, "payload", _freeze_payload(self.payload))
        if self.display_snapshot is not None and not isinstance(
            self.display_snapshot, InstrumentDisplaySnapshot
        ):
            raise DomainValidationError(
                "display_snapshot must be an InstrumentDisplaySnapshot"
            )
        if not isinstance(self.display_snapshots, Mapping):
            raise DomainValidationError("display_snapshots must be a mapping")
        frozen_snapshots: dict[UUID, InstrumentDisplaySnapshot] = {}
        for instrument_id, snapshot in self.display_snapshots.items():
            if not isinstance(instrument_id, UUID):
                raise DomainValidationError(
                    "display_snapshots keys must be UUID instrument identities"
                )
            if not isinstance(snapshot, InstrumentDisplaySnapshot):
                raise DomainValidationError(
                    "display_snapshots values must be InstrumentDisplaySnapshot"
                )
            snapshot.require_matching_instrument(instrument_id, "display_snapshot")
            frozen_snapshots[instrument_id] = snapshot
        object.__setattr__(
            self, "display_snapshots", MappingProxyType(frozen_snapshots)
        )


@dataclass(frozen=True, slots=True)
class PhaseContext:
    """Immutable description of one phase execution inside one step."""

    run_id: str
    step_sequence: int
    phase_sequence: int
    phase_key: str
    session_date: date
    timezone: str
    decision_time: datetime
    data_cutoff: datetime | None
    effective_from: datetime | None
    phase_view: Any | None


# ---------------------------------------------------------------------------
# Engine-side market facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionQuote:
    """Raw (unadjusted) open/close quotes of one instrument-session."""

    instrument_id: UUID
    session_date: date
    open_price: Decimal | None
    close_price: Decimal | None
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, Mapping) or not self.evidence:
            raise DomainValidationError(
                "session quote evidence must be a non-empty source mapping"
            )
        from app.backtesting.analysis_inputs import freeze_canonical_evidence

        object.__setattr__(
            self,
            "evidence",
            freeze_canonical_evidence(self.evidence, "session quote evidence"),
        )


@dataclass(frozen=True, slots=True)
class InstrumentFacts:
    """Static trading facts required before a match or sizing may proceed.

    Trading-status fields carry no defaults on purpose: a missing
    suspension, price-limit, or calendar attribution must be stated
    explicitly by the data source instead of silently meaning "tradable".
    """

    instrument_id: UUID
    price_tick: Decimal
    calendar_id: str
    suspended: bool
    buy_allowed: bool
    sell_allowed: bool
    board_lot: Decimal = Decimal("100")

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        object.__setattr__(
            self, "price_tick", _positive(self.price_tick, "price_tick")
        )
        if not isinstance(self.calendar_id, str) or not self.calendar_id.strip():
            raise DomainValidationError(
                "calendar_id must be non-blank text; every instrument is "
                "settled against its own named trading calendar"
            )
        for field_name in ("suspended", "buy_allowed", "sell_allowed"):
            if not isinstance(getattr(self, field_name), bool):
                raise DomainValidationError(
                    f"{field_name} must be an explicit boolean trading fact"
                )
        lot = _positive(self.board_lot, "board_lot")
        object.__setattr__(self, "board_lot", lot)


class EngineMarketData(Protocol):
    """Read side the engine phases use for raw same-session market facts."""

    def session_quotes(
        self,
        instrument_ids: Sequence[UUID],
        session_date: date,
    ) -> Mapping[UUID, SessionQuote]: ...

    def instrument_facts(
        self, instrument_ids: Sequence[UUID]
    ) -> Mapping[UUID, InstrumentFacts]: ...


class SettlementCalendarGateway(Protocol):
    """Resolve the next official open session of one named calendar.

    The gateway is the single authority for T+1 settlement dates: it never
    guesses with natural-calendar days, and an unresolvable next session
    (for example after the final session) returns ``None`` so the caller
    fails explicitly instead of inventing a date.
    """

    def next_open_session(
        self, calendar_id: str, *, after_session: date
    ) -> date | None: ...


class EngineDataView:
    """Engine-internal, read-only view for match/value-style phases.

    Instances are created fresh per phase by the view factory and are never
    exposed through a :class:`DecisionContext`; strategies cannot reach the
    underlying market-data source from here.
    """

    __slots__ = ("_quotes", "_facts", "_session_date")

    def __init__(
        self,
        *,
        quotes: Mapping[UUID, SessionQuote],
        facts: Mapping[UUID, InstrumentFacts],
        session_date: date,
    ) -> None:
        object.__setattr__(self, "_quotes", MappingProxyType(dict(quotes)))
        object.__setattr__(self, "_facts", MappingProxyType(dict(facts)))
        object.__setattr__(self, "_session_date", session_date)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("the engine data view is read-only")

    @property
    def session_date(self) -> date:
        return self._session_date

    def quote(self, instrument_id: UUID) -> SessionQuote | None:
        return self._quotes.get(instrument_id)

    def close_marks(self) -> dict[UUID, Decimal]:
        """Every available raw close mark keyed by stable instrument id."""

        return {
            instrument_id: quote.close_price
            for instrument_id, quote in self._quotes.items()
            if quote.close_price is not None
        }

    def close_mark(self, instrument_id: UUID) -> Decimal | None:
        quote = self._quotes.get(instrument_id)
        return quote.close_price if quote is not None else None

    def close_mark_evidence(
        self, instrument_ids: Sequence[UUID]
    ) -> Mapping[str, Any]:
        """Return immutable provenance for every requested valuation mark.

        Missing marks remain explicit in the payload. Consequently a blocked
        valuation and a later repaired data revision cannot share evidence.
        """

        return MappingProxyType(
            {
                str(instrument_id): (
                    {
                        "close_price": quote.close_price,
                        "evidence": quote.evidence,
                    }
                    if (quote := self._quotes.get(instrument_id)) is not None
                    else None
                )
                for instrument_id in sorted(instrument_ids, key=str)
            }
        )

    def facts(self, instrument_id: UUID) -> InstrumentFacts:
        facts = self._facts.get(instrument_id)
        if facts is None:
            raise DomainValidationError(
                f"instrument facts are missing for {instrument_id}"
            )
        return facts

    def facts_snapshot(self) -> dict[UUID, InstrumentFacts]:
        """Detached copy of the view's instrument facts.

        The runner keeps this snapshot so later phases without an engine
        view (notably ``submit``) still size orders against the same
        explicit trading facts observed earlier in the step.
        """

        return dict(self._facts)

    def market_state(
        self, instrument_id: UUID, *, timestamp: datetime
    ) -> MarketState:
        """Build the PIT market state one opening match needs.

        A missing quote degrades to an explicitly unavailable open instead
        of a fabricated price, so the order receives a stable no-fill
        reason rather than a silent zero-price execution.
        """

        quote = self._quotes.get(instrument_id)
        facts = self.facts(instrument_id)
        return MarketState(
            instrument_id=instrument_id,
            timestamp=timestamp,
            open_price=quote.open_price if quote is not None else None,
            price_tick=facts.price_tick,
            is_suspended=facts.suspended,
            open_available=quote is not None and quote.open_price is not None,
            buy_allowed=facts.buy_allowed,
            sell_allowed=facts.sell_allowed,
            price_limit_status=PriceLimitStatus.NONE,
            status_reason=None if quote is not None else "quote_missing",
        )


# ---------------------------------------------------------------------------
# View factory
# ---------------------------------------------------------------------------


class PhaseViewFactory(Protocol):
    """Creates the data view each phase may read."""

    def for_phase(
        self,
        instruction: TimingInstruction,
        step: TimeStep,
        *,
        next_step: TimeStep | None,
    ) -> Any | None: ...

    def universe(self) -> UniverseQueryDTO:
        """The candidate-set facade used to build decision contexts."""
        ...

    def display_snapshot(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> Any:
        """Resolve one immutable point-in-time display snapshot."""
        ...


class BacktestViewFactory:
    """First-version factory implementing the documented view rules.

    ``decide`` receives a :class:`StrategyDataDTO` whose cutoff equals the
    decision time (the session close); ``observe``/``match``/``cash_actions``/
    ``value`` receive an :class:`EngineDataView`; phases without a declared
    data view receive ``None``.
    """

    def __init__(
        self,
        *,
        strategy_view: StrategyDataView,
        universe_query: UniverseQuery,
        engine_market_data: EngineMarketData,
        scope_instrument_ids: Sequence[UUID],
        adjustment_gate: AdjustmentPolicyGate | None = None,
        display_provider: Any | None = None,
        instrument_display_provider: Any | None = None,
        candidate_eligibility_evaluator: Any | None = None,
        universe_scope_resolution: Any | None = None,
        candidate_evaluator: Any | None = None,
    ) -> None:
        if display_provider is not None and instrument_display_provider is not None:
            raise DomainValidationError(
                "pass only one of display_provider and "
                "instrument_display_provider"
            )
        self._strategy_view = strategy_view
        self._engine_market_data = engine_market_data
        self._adjustment_gate = adjustment_gate
        self._display_provider = (
            display_provider
            if display_provider is not None
            else instrument_display_provider
        )
        self._scope_instrument_ids = tuple(dict.fromkeys(scope_instrument_ids))
        self._runtime_instrument_ids: set[UUID] = set()
        self._universe_dto = UniverseQueryDTO(universe_query)
        self._candidate_eligibility_evaluator = candidate_eligibility_evaluator
        if (
            self._candidate_eligibility_evaluator is not None
            and candidate_evaluator is not None
        ):
            raise DomainValidationError(
                "provide only one of candidate_eligibility_evaluator and "
                "candidate_evaluator"
            )
        if self._candidate_eligibility_evaluator is None:
            self._candidate_eligibility_evaluator = candidate_evaluator
        self._universe_scope_resolution = universe_scope_resolution

    def universe(self) -> UniverseQueryDTO:
        return self._universe_dto

    def universe_for_step(
        self,
        *,
        effective_date: date | None = None,
        data_cutoff: datetime | None = None,
        session_date: date | None = None,
        decision_time: datetime | None = None,
    ) -> Any:
        """Return the query bound to one decision's PIT coordinates.

        A provider may implement ``for_step``/``bind`` on its raw query.  The
        default fixed fixture has no such method and simply returns its
        immutable query facade.  Runtime still executes that facade once per
        step, preserving the old fixed-scope behavior without inventing a
        dynamic catalogue.
        """

        raw = getattr(self._universe_dto, "_UniverseQueryDTO__query", None)
        resolver = None
        for name in ("for_step", "bind_for_step", "bind"):
            candidate = getattr(raw, name, None)
            if callable(candidate):
                resolver = candidate
                break
        if resolver is None:
            # A plain frozen data-layer ``UniverseQuery`` still needs a new
            # PIT coordinate at each decision.  Clone its value object with
            # only effective_date/data_cutoff changed; all scope, policy,
            # exception, and calendar fields remain byte-for-byte frozen.
            try:
                from app.backtesting.data.requests import (
                    QueryBoundary,
                    UniverseQuery as DataUniverseQuery,
                )

                if isinstance(raw, DataUniverseQuery):
                    cutoff = data_cutoff or raw.boundary.data_cutoff
                    knowledge_as_of = raw.boundary.knowledge_as_of
                    if knowledge_as_of is not None and knowledge_as_of > cutoff:
                        knowledge_as_of = cutoff
                    boundary = QueryBoundary(
                        data_cutoff=cutoff,
                        knowledge_as_of=knowledge_as_of,
                        include_cutoff_day=raw.boundary.include_cutoff_day,
                    )
                    return DataUniverseQuery(
                        rule=raw.rule,
                        market_scope=raw.market_scope,
                        effective_date=effective_date or raw.effective_date,
                        boundary=boundary,
                        allowed_calendar_ids=raw.allowed_calendar_ids,
                        rule_exception_set=raw.rule_exception_set,
                        qualification_policy_version=(
                            raw.qualification_policy_version
                        ),
                        scope_mode=raw.scope_mode,
                        universe_scope_snapshot_hash=getattr(
                            raw, "universe_scope_snapshot_hash", None
                        ),
                        universe_query_policy=getattr(
                            raw, "universe_query_policy", None
                        ),
                    )
            except (ImportError, TypeError, ValueError):
                # Legacy strategy-only universe implementations have no
                # data-layer query object and continue through the existing
                # immutable facade.
                pass
            return self._universe_dto
        try:
            parameters = signature(resolver).parameters
        except (TypeError, ValueError):
            return resolver()
        available = {
            "effective_date": effective_date,
            "session_date": session_date or effective_date,
            "data_cutoff": data_cutoff,
            "decision_time": decision_time or data_cutoff,
        }
        kwargs = {
            name: value
            for name, value in available.items()
            if value is not None
            and name in parameters
            and parameters[name].kind
            in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)
        }
        bound = resolver(**kwargs) if kwargs else resolver()
        return bound

    @property
    def candidate_eligibility_evaluator(self) -> Any | None:
        """Optional final candidate qualification port owned by the provider."""

        return self._candidate_eligibility_evaluator

    @property
    def universe_scope_resolution(self) -> Any | None:
        """Optional immutable dynamic-scope preflight result."""

        return self._universe_scope_resolution

    @property
    def fixed_authorized_instrument_ids(self) -> tuple[UUID, ...]:
        """Expose the admission fixed set as a read-only audit projection."""

        return tuple(sorted(self._scope_instrument_ids, key=str))

    def register_runtime_instrument_ids(
        self, instrument_ids: Sequence[UUID]
    ) -> None:
        """Make final-qualified dynamic identities visible to engine phases.

        The set is an engine read scope only; it never changes the frozen
        strategy market scope or calendar set.  Runtime calls this after the
        target check and before the next step's matching/value views are
        constructed.
        """

        values = tuple(instrument_ids)
        if any(not isinstance(value, UUID) for value in values):
            raise DomainValidationError(
                "runtime instrument ids must be UUID values"
            )
        self._runtime_instrument_ids.update(values)

    def refresh_market_data(
        self, instrument_ids: Sequence[UUID], session_date: date
    ) -> tuple[Mapping[UUID, SessionQuote], Mapping[UUID, InstrumentFacts]]:
        """Read explicit same-session quotes/facts for final order sizing."""

        ids = tuple(sorted(set(instrument_ids), key=str))
        quotes = self._engine_market_data.session_quotes(ids, session_date)
        facts = self._engine_market_data.instrument_facts(ids)
        return quotes, facts

    def strategy_data_view_for_step(
        self,
        *,
        effective_date: date,
        data_cutoff: datetime,
        session_date: date | None = None,
        decision_time: datetime | None = None,
    ) -> Any:
        """Bind the strategy data view to one step when the provider supports it."""

        resolver = None
        for name in ("for_step", "bind_for_step", "bind"):
            candidate = getattr(self._strategy_view, name, None)
            if callable(candidate):
                resolver = candidate
                break
        if resolver is None:
            return self._strategy_view
        try:
            parameters = signature(resolver).parameters
        except (TypeError, ValueError):
            return resolver()
        available = {
            "effective_date": effective_date,
            "session_date": session_date or effective_date,
            "data_cutoff": data_cutoff,
            "decision_time": decision_time or data_cutoff,
        }
        kwargs = {
            name: value
            for name, value in available.items()
            if name in parameters
            and parameters[name].kind
            in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)
        }
        return resolver(**kwargs) if kwargs else resolver()

    def candidate_identities(self) -> dict[UUID, InstrumentCandidateDTO]:
        """Return the already-provided candidate DTOs by stable identity.

        This method deliberately does not enrich candidates from the display
        provider or from ``etf_codes``.  Candidate DTOs are a separate
        strategy-facing contract; missing historical display facts must stay
        missing rather than being replaced with today's catalogue snapshot.
        """

        return {
            candidate.instrument_id: candidate
            for candidate in self._universe_dto.query()
        }

    def display_snapshot(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> Any:
        """Freeze display identity at the event's effective instant.

        The optional provider is the only source for event display fields.
        When it is absent, the result still records the stable identity with
        all display fields explicitly missing; no candidate/current snapshot
        is substituted.
        """

        from app.backtesting.result_models import (
            InstrumentDisplaySnapshot,
            resolve_display_snapshot,
        )

        provider = self._display_provider
        if provider is None:
            return InstrumentDisplaySnapshot(instrument_id=instrument_id)
        if not callable(getattr(provider, "resolve_display", None)):
            raise DomainValidationError(
                "display_provider must expose resolve_display()"
            )
        return resolve_display_snapshot(
            provider,
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )

    def for_phase(
        self,
        instruction: TimingInstruction,
        step: TimeStep,
        *,
        next_step: TimeStep | None,
    ) -> Any | None:
        if instruction.data_view is None:
            return None
        if instruction.data_view is DataViewKind.STRATEGY:
            step_date = date.fromisoformat(step.metadata["session_date"])
            strategy_view = self.strategy_data_view_for_step(
                effective_date=step_date,
                data_cutoff=instruction.timestamp,
                session_date=step_date,
                decision_time=instruction.timestamp,
            )
            universe = self.universe_for_step(
                effective_date=step_date,
                data_cutoff=instruction.timestamp,
                session_date=step_date,
                decision_time=instruction.timestamp,
            )
            if not callable(getattr(universe, "query", None)):
                raise _universe_provider_error(
                    "step universe resolver must return a query facade"
                )
            return StrategyDataDTO(
                strategy_view,
                data_cutoff=instruction.timestamp,
                adjustment_gate=self._adjustment_gate,
                universe=universe,
            )
        session_date = date.fromisoformat(step.metadata["session_date"])
        engine_ids = tuple(
            sorted(
                set(self._scope_instrument_ids) | self._runtime_instrument_ids,
                key=str,
            )
        )
        quotes = self._engine_market_data.session_quotes(engine_ids, session_date)
        facts = self._engine_market_data.instrument_facts(engine_ids)
        return EngineDataView(
            quotes=quotes, facts=facts, session_date=session_date
        )


def _universe_provider_error(
    message: str, *, details: Mapping[str, Any] | None = None
) -> Exception:
    """Build a JSON-safe provider contract error at the runtime boundary."""

    from app.backtesting.data.errors import UniverseProviderContractViolationError

    return UniverseProviderContractViolationError(
        message,
        details=_json_safe_runtime_value(dict(details or {})),
    )


def _universe_capability_error(
    message: str, *, details: Mapping[str, Any] | None = None
) -> Exception:
    """Build a request-level missing-universe-capability error."""

    from app.backtesting.data.errors import UniverseCapabilityMissingError

    return UniverseCapabilityMissingError(
        message,
        details=_json_safe_runtime_value(dict(details or {})),
    )


def _raw_universe_source(source: Any) -> Any:
    """Unwrap the known read-only query facades for audit-only inspection."""

    source = getattr(source, "_UniverseQueryDTO__query", source)
    source = getattr(source, "_ChunkUniverseQuery__query", source)
    return source


@dataclass(frozen=True, slots=True)
class _RuntimeCandidateEligibilityContext:
    """Dependency-free fallback context for provider qualification ports."""

    instrument_id: UUID
    effective_date: date
    data_cutoff: datetime
    market_scope: Any | None = None
    universe_query_policy: Any | None = None
    rule_package: Any | None = None
    rule_exception_set: Any | None = None
    qualification_policy_version: Any | None = None
    frozen_calendar_ids: tuple[str, ...] = ()
    scope_mode: Any | None = None
    required_capabilities: tuple[Any, ...] = ()
    requested_window: Any | None = None
    query_boundary: Any | None = None
    universe_scope_snapshot_hash: str | None = None
    provider_capability_summary: Mapping[str, Any] = MappingProxyType({})
    candidate: Any | None = None


def _project_strategy_candidate(candidate: Any) -> InstrumentCandidateDTO:
    """Project one complete provider candidate onto the narrow strategy DTO."""

    if isinstance(candidate, InstrumentCandidateDTO):
        return candidate
    spec = getattr(candidate, "spec", None)
    source = spec if spec is not None else candidate
    instrument_id = getattr(candidate, "instrument_id", None) or getattr(
        source, "instrument_id", None
    )
    display = getattr(source, "display", source)
    trading_code = getattr(candidate, "trading_code", None) or getattr(
        display, "trading_code", None
    )
    name = getattr(candidate, "name", None) or getattr(display, "name", None)
    display_name = getattr(candidate, "display_name", None) or getattr(
        display, "display_name", None
    )
    asset_class = getattr(candidate, "asset_class", None) or getattr(
        source, "asset_class", None
    )
    exchange = getattr(candidate, "exchange", None) or getattr(
        source, "exchange", None
    )
    if not isinstance(instrument_id, UUID) or not all(
        isinstance(value, str) and value.strip()
        for value in (trading_code, name, display_name, asset_class, exchange)
    ):
        raise _universe_provider_error(
            "universe provider candidate cannot be projected to InstrumentCandidateDTO",
            details={
                "candidate_type": type(candidate).__name__,
                "instrument_id": instrument_id,
            },
        )
    return InstrumentCandidateDTO(
        instrument_id=instrument_id,
        trading_code=trading_code,
        name=name,
        display_name=display_name,
        asset_class=asset_class,
        exchange=exchange,
    )


class _StepBoundUniverse:
    """Immutable full candidate snapshot plus strategy DTO projection."""

    __slots__ = (
        "_candidates",
        "_strategy_candidates",
        "_source",
        "_market_scope",
        "_query_observer",
    )

    def __init__(
        self,
        candidates: Sequence[Any],
        source: Any | None = None,
        query_observer: Callable[[tuple[UUID, ...]], None] | None = None,
    ) -> None:
        by_id: dict[UUID, Any] = {}
        for candidate in tuple(candidates):
            instrument_id = getattr(candidate, "instrument_id", None)
            if not isinstance(instrument_id, UUID):
                raise _universe_provider_error(
                    "universe provider returned a candidate without a stable instrument_id",
                    details={"candidate_type": type(candidate).__name__},
                )
            if instrument_id in by_id:
                raise _universe_provider_error(
                    "universe provider returned duplicate candidate identities",
                    details={"instrument_id": str(instrument_id)},
                )
            by_id[instrument_id] = candidate
        ordered_ids = tuple(sorted(by_id, key=str))
        object.__setattr__(self, "_candidates", tuple(by_id[item] for item in ordered_ids))
        object.__setattr__(
            self,
            "_strategy_candidates",
            tuple(_project_strategy_candidate(by_id[item]) for item in ordered_ids),
        )
        object.__setattr__(self, "_source", source)
        raw_source = _raw_universe_source(source)
        object.__setattr__(self, "_market_scope", getattr(raw_source, "market_scope", None))
        # The snapshot itself remains immutable.  This callback only records
        # which rows the strategy-facing ``universe.query()`` actually
        # returned; it is deliberately separate from the provider's full
        # candidate snapshot so a strategy cannot authorize an unseen ID by
        # hard-coding it in a decision.
        object.__setattr__(self, "_query_observer", query_observer)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("the step-bound universe is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the step-bound universe is read-only")

    @property
    def candidates(self) -> tuple[Any, ...]:
        return self._candidates

    @property
    def strategy_candidates(self) -> tuple[InstrumentCandidateDTO, ...]:
        return self._strategy_candidates

    @property
    def source(self) -> Any | None:
        return self._source

    def query(
        self,
        *,
        exchanges: Sequence[str] | None = None,
        asset_classes: Sequence[str] | None = None,
    ) -> tuple[InstrumentCandidateDTO, ...]:
        """Apply only narrowing filters and update optional chunk permission."""

        def normalize(value: Sequence[str] | None, field_name: str) -> set[str] | None:
            if value is None:
                return None
            if isinstance(value, (str, bytes)):
                raise DomainValidationError(f"{field_name} must be an iterable of strings")
            try:
                result = {item.strip() for item in value}
            except (AttributeError, TypeError) as exc:
                raise DomainValidationError(
                    f"{field_name} must be an iterable of strings"
                ) from exc
            if any(not item for item in result):
                raise DomainValidationError(f"{field_name} entries must be non-blank strings")
            return result

        exchange_filter = normalize(exchanges, "exchanges")
        asset_filter = normalize(asset_classes, "asset_classes")
        scope = self._market_scope
        if exchange_filter is not None and scope is not None:
            allowed = set(getattr(scope, "exchanges", ()) or ())
            if allowed and not exchange_filter.issubset(allowed):
                raise DomainValidationError("strategy exchange filter widens the frozen market scope")
        if asset_filter is not None and scope is not None:
            allowed = set(getattr(scope, "asset_classes", ()) or ())
            if allowed and not asset_filter.issubset(allowed):
                raise DomainValidationError("strategy asset-class filter widens the frozen market scope")
        rows: list[InstrumentCandidateDTO] = []
        for candidate, projection in zip(self._candidates, self._strategy_candidates):
            if exchange_filter is not None and getattr(candidate, "exchange", None) not in exchange_filter:
                continue
            if asset_filter is not None and getattr(candidate, "asset_class", None) not in asset_filter:
                continue
            rows.append(projection)
        source = self._source
        raw_source = getattr(source, "_UniverseQueryDTO__query", source)
        chunk_view = getattr(raw_source, "_ChunkUniverseQuery__view", None)
        authorizer = getattr(chunk_view, "_authorize_step_candidates", None)
        if callable(authorizer):
            authorizer(
                tuple(item.instrument_id for item in rows),
                query=getattr(raw_source, "_ChunkUniverseQuery__query", raw_source),
            )
        observer = self._query_observer
        if callable(observer):
            observer(tuple(item.instrument_id for item in rows))
        return tuple(rows)


# ---------------------------------------------------------------------------
# Collaborator protocols
# ---------------------------------------------------------------------------


class StrategyProgram(Protocol):
    """Internal lifecycle protocol the runner drives (see the adapter)."""

    def on_step(self, context: DecisionContext) -> Any:
        """Return the validated :class:`StrategyDecision` for this step."""
        ...


class CorporateActionSource(Protocol):
    def cash_dividend_events(self) -> tuple[CashDividendEvent, ...]:
        """Return every cash-dividend event of this run's scope."""
        ...


#: Supported entitlement derivation rules are resolved by
#: :func:`_dividend_includes_pending_settlement`; the pending-lot choice
#: is part of the frozen rule identity, never inferred.
def _dividend_includes_pending_settlement(
    event: CashDividendEvent,
) -> bool:
    """Resolve the declared entitlement rule's pending-lot handling."""

    from app.backtesting.dividends import DividendEntitlementRuleError

    if (
        event.derivation_rule_key == "record_date_entitlement"
        and event.derivation_rule_version == 1
    ):
        return True
    if (
        event.derivation_rule_key == "record_date_entitlement_settled_only"
        and event.derivation_rule_version == 1
    ):
        return False
    raise DividendEntitlementRuleError(
        f"dividend event {event.event_id} declares unsupported "
        f"entitlement rule {event.derivation_rule_key}@"
        f"{event.derivation_rule_version}",
    )


def _load_dividend_declarations(
    corporate_actions: CorporateActionSource | None,
) -> tuple[CashDividendEvent, ...]:
    """Load and deterministically order this run's dividend declarations."""

    if corporate_actions is None:
        return ()
    if not hasattr(corporate_actions, "cash_dividend_events"):
        raise DomainValidationError(
            "corporate_actions must satisfy CorporateActionSource with "
            "cash_dividend_events(); arrival-day dividend sources are no "
            "longer accepted"
        )
    declarations = tuple(corporate_actions.cash_dividend_events())
    seen: set[UUID] = set()
    for event in declarations:
        if event.event_id in seen:
            raise DividendError(
                f"dividend event id {event.event_id} was declared twice; "
                "ids are unique across the whole run"
            )
        seen.add(event.event_id)
    return tuple(
        sorted(
            declarations,
            key=lambda event: (str(event.instrument_id), str(event.event_id)),
        )
    )


class DecisionInterpreter(Protocol):
    """Turns a validated strategy decision into executable order intents."""

    def interpret(
        self,
        decision: Any,
        *,
        portfolio: PortfolioState,
        equity: Decimal,
        reference_prices: Mapping[UUID, Decimal],
        facts: Mapping[UUID, InstrumentFacts],
    ) -> tuple[OrderIntent, ...]: ...


# ---------------------------------------------------------------------------
# target_weights@1 interpreter
# ---------------------------------------------------------------------------

def _floor_to_lot(quantity: Decimal, board_lot: Decimal) -> Decimal:
    """Floor one share quantity onto the board-lot grid."""

    return (quantity / board_lot).to_integral_value(rounding=ROUND_FLOOR) * board_lot


class TargetWeightsInterpreter:
    """The ``target_weights@1`` decision interpreter.

    Desired quantities are sized from the latest close marks against the
    valued equity, floored onto the board-lot grid.  Positions absent from
    the target mapping are interpreted as a zero target.  A rebalance below
    one board lot produces no order.  Sells are submitted for the full
    target delta: capping by settlement-limited availability belongs to
    the matching stage, never to interpretation.
    """

    interpreter_key = "target_weights"
    interpreter_version = 1

    def __init__(self, board_lot: Decimal | int | str = 100) -> None:
        lot = Decimal(str(board_lot))
        if lot <= 0:
            raise DomainValidationError("board_lot must be positive")
        self._board_lot = lot

    @property
    def board_lot(self) -> Decimal:
        return self._board_lot

    @property
    def parameters(self) -> Mapping[str, Decimal]:
        """Live parameter snapshot captured verbatim into run audits."""

        return MappingProxyType({"board_lot": self._board_lot})

    def interpret(
        self,
        decision: Any,
        *,
        portfolio: PortfolioState,
        equity: Decimal,
        reference_prices: Mapping[UUID, Decimal],
        facts: Mapping[UUID, InstrumentFacts],
    ) -> tuple[OrderIntent, ...]:
        raw_targets = getattr(decision, "targets", {}) or {}
        # Decision payload keys arrive as instrument-id strings; normalize
        # them once so lookups line up with the UUID-keyed portfolio.
        try:
            targets = {
                key if isinstance(key, UUID) else UUID(str(key)): Decimal(str(value))
                for key, value in dict(raw_targets).items()
            }
        except ValueError as exc:
            raise DomainValidationError(
                f"decision targets contain an invalid instrument id: {exc}"
            ) from exc
        intents: list[OrderIntent] = []
        instrument_ids = sorted(
            set(targets) | set(portfolio.positions),
            key=str,
        )
        for instrument_id in instrument_ids:
            weight = Decimal(str(targets.get(instrument_id, 0)))
            reference = reference_prices.get(instrument_id)
            if reference is None or reference <= 0:
                # Without a reference price the target cannot be sized; the
                # position is intentionally left untouched this step.
                continue
            lot = facts.get(instrument_id)
            board_lot = lot.board_lot if lot is not None else self._board_lot
            desired_quantity = _floor_to_lot(
                weight * equity / reference, board_lot
            )
            position = portfolio.positions.get(instrument_id)
            current_quantity = (
                position.quantity if position is not None else ZERO
            )
            delta = desired_quantity - current_quantity
            if delta >= board_lot:
                intents.append(
                    OrderIntent(
                        intent_id=uuid5(
                            _INTERPRETER_NAMESPACE,
                            f"{self.interpreter_key}@{self.interpreter_version}:"
                            f"{decision.decision_time.isoformat()}:"
                            f"{instrument_id}",
                        ),
                        instrument_id=instrument_id,
                        side=OrderSide.BUY,
                        quantity=delta,
                        valid_from=decision.decision_time,
                    )
                )
            elif delta <= -board_lot:
                # The interpreter submits the full target delta; capping a
                # sell by settlement-limited availability is the matching
                # stage's job, not an interpretation decision.
                sell_quantity = -delta
                intents.append(
                    OrderIntent(
                        intent_id=uuid5(
                            _INTERPRETER_NAMESPACE,
                            f"{self.interpreter_key}@{self.interpreter_version}:"
                            f"{decision.decision_time.isoformat()}:"
                            f"{instrument_id}",
                        ),
                        instrument_id=instrument_id,
                        side=OrderSide.SELL,
                        quantity=sell_quantity,
                        valid_from=decision.decision_time,
                    )
                )
        return tuple(intents)


_INTERPRETER_NAMESPACE = uuid5(NAMESPACE_URL, "quant-foundry:decision-interpreter")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EquitySample:
    """One close-time valuation sample of the run."""

    step_sequence: int
    session_date: date
    as_of: datetime
    equity: Decimal | None
    valuation_status: str


@dataclass(frozen=True, slots=True)
class OrderOutcomeRecord:
    """Final outcome of one runtime order, safe to keep on the result."""

    order_id: UUID
    intent_id: UUID
    instrument_id: UUID
    side: str
    quantity: Decimal
    status: str
    status_reason: str | None
    submitted_at: datetime
    valid_from: datetime | None
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class BacktestRunResult:
    """Immutable aggregate of one completed run.

    ``analysis_status`` is explicit so callers never infer the analysis
    lifecycle from whether metrics are present: a successful slice that
    still has official steps ahead reports ``partial``, and only the run
    that consumed the final official step reports ``final`` with the
    frozen metric results.
    """

    run_id: str
    events: tuple[EventEnvelope, ...]
    equity_curve: tuple[EquitySample, ...]
    decisions: tuple[Any, ...]
    order_outcomes: tuple[OrderOutcomeRecord, ...]
    final_snapshot: PortfolioSnapshot
    components: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
    analysis_status: str | None = None
    chunk_sequence: int | None = None
    analysis_chunk_token: str | None = None
    completed_through_step_sequence: int | None = None
    analysis_metrics: tuple[Any, ...] = ()
    # The verified run-level rule snapshot identity, when this runtime was
    # admitted with a formal snapshot bundle.
    rule_snapshot_hash: str | None = None
    # Candidate-universe audit data is carried through existing run/result
    # projections rather than a new candidate table.  Mappings are frozen at
    # construction so a caller cannot rewrite qualification evidence after
    # the run has completed.
    universe_scope_snapshot_hash: str | None = None
    universe_eligibility_summary: Mapping[str, Any] = MappingProxyType({})
    final_qualification_results: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.universe_scope_snapshot_hash is not None:
            if not isinstance(self.universe_scope_snapshot_hash, str) or not self.universe_scope_snapshot_hash.strip():
                raise DomainValidationError(
                    "universe_scope_snapshot_hash must be non-blank text when provided"
                )
            if len(self.universe_scope_snapshot_hash.strip()) != 64 or any(
                character not in "0123456789abcdef"
                for character in self.universe_scope_snapshot_hash.strip()
            ):
                raise DomainValidationError(
                    "universe_scope_snapshot_hash must be a lowercase SHA-256 digest"
                )
            object.__setattr__(
                self,
                "universe_scope_snapshot_hash",
                self.universe_scope_snapshot_hash.strip(),
            )
        summary = _freeze_payload(
            _json_safe_runtime_value(dict(self.universe_eligibility_summary or {}))
        )
        if not isinstance(summary, Mapping):
            raise DomainValidationError(
                "universe_eligibility_summary must be a mapping"
            )
        object.__setattr__(self, "universe_eligibility_summary", summary)
        normalized_results: list[Mapping[str, Any]] = []
        for index, result in enumerate(self.final_qualification_results):
            if not isinstance(result, Mapping):
                raise DomainValidationError(
                    f"final_qualification_results[{index}] must be a mapping"
                )
            frozen_result = _freeze_payload(
                _json_safe_runtime_value(dict(result))
            )
            if not isinstance(frozen_result, Mapping):
                raise DomainValidationError(
                    f"final_qualification_results[{index}] must be a JSON mapping"
                )
            normalized_results.append(frozen_result)
        object.__setattr__(
            self,
            "final_qualification_results",
            tuple(normalized_results),
        )

    @property
    def universe_final_rechecks(self) -> tuple[Mapping[str, Any], ...]:
        """Architecture-name alias for final target qualification evidence."""

        return self.final_qualification_results

    @property
    def universe_target_ids(self) -> tuple[str, ...]:
        """Stable selected target identities represented by this result."""

        return tuple(
            str(item.get("instrument_id"))
            for item in self.final_qualification_results
            if isinstance(item, Mapping) and item.get("instrument_id") is not None
        )

    @property
    def universe_filtered_reason_counts(self) -> Mapping[str, int]:
        """Return candidate-level filter counts from the audit summary."""

        value = self.universe_eligibility_summary.get(
            "filtered_reason_counts", {}
        )
        return value if isinstance(value, Mapping) else MappingProxyType({})

    @property
    def qualification_policy_version(self) -> str | None:
        """Return the frozen candidate qualification-policy identity."""

        value = self.universe_eligibility_summary.get(
            "qualification_policy_version"
        )
        return value if isinstance(value, str) else None

    @property
    def resolved_calendar_ids(self) -> tuple[str, ...]:
        """Return the immutable named calendars used by candidate checks."""

        value = self.universe_eligibility_summary.get("resolved_calendar_ids", ())
        return tuple(value) if isinstance(value, (tuple, list)) else ()


class DeterministicBacktestRunner:
    """Drive the registered timing policy over the axis, phase by phase.

    The runner owns only mutable runtime state (portfolio, orders, event
    counter).  Every business mutation flows through injected components:
    orders become intents only in ``submit``, fills are produced solely by
    the :class:`ExecutionModel`, and the account changes exclusively through
    :meth:`AccountingPolicy.apply_fill`, explicit settlement restoration,
    and cash-dividend accounting events.
    """

    @classmethod
    def create_admitted(
        cls,
        *,
        admission_kwargs: Mapping[str, Any],
        runner_kwargs: Mapping[str, Any],
    ) -> "DeterministicBacktestRunner":
        """Construct an analyzer-enabled runner through one admission path."""

        from app.backtesting.analysis_admission import build_admitted_runner

        return build_admitted_runner(
            admission_kwargs=admission_kwargs,
            runner_kwargs=runner_kwargs,
        )

    def __init__(
        self,
        *,
        run_id: str,
        axis: TimeAxis,
        timing_policy: TimingPolicy,
        view_factory: PhaseViewFactory,
        strategy: StrategyProgram,
        interpreter: DecisionInterpreter,
        execution_model: ExecutionModel,
        accounting: AccountingPolicy,
        initial_portfolio: PortfolioState,
        settlement_calendar: SettlementCalendarGateway,
        corporate_actions: CorporateActionSource | None = None,
        analyzers: Sequence[Callable[[PortfolioSnapshot], None]] = (),
        currency: str = "CNY",
        component_parameters: Mapping[str, Any] | None = None,
        analysis_engine: Any | None = None,
        analysis_admission: Any | None = None,
        pit_data_gateway: Any | None = None,
        display_provider: Any | None = None,
        instrument_display_provider: Any | None = None,
        rule_snapshot_bundle: Any | None = None,
        snapshot_bundle: Any | None = None,
        # Candidate qualification is optional for legacy fixed-scope
        # fixtures.  Formal dynamic/hybrid callers may provide the canonical
        # evaluator and frozen scope resolution through either explicit
        # arguments or the view factory's equivalent read-only ports.
        candidate_eligibility_evaluator: Any | None = None,
        candidate_eligibility: Any | None = None,
        candidate_evaluator: Any | None = None,
        universe_scope_resolution: Any | None = None,
        universe_scope: Any | None = None,
        fixed_authorized_instrument_ids: Sequence[UUID] | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise DomainValidationError("run_id must be non-blank text")
        if not isinstance(axis, TimeAxis):
            raise DomainValidationError("axis must be a TimeAxis")
        if not hasattr(settlement_calendar, "next_open_session"):
            raise DomainValidationError(
                "settlement_calendar must satisfy the SettlementCalendarGateway "
                "protocol; T+1 dates are resolved only from named calendars"
            )
        if not hasattr(timing_policy, "phases") or not hasattr(
            timing_policy, "policy_key"
        ):
            raise DomainValidationError(
                "timing_policy must satisfy the TimingPolicy protocol"
            )
        if display_provider is not None and instrument_display_provider is not None:
            raise DomainValidationError(
                "pass only one of display_provider and "
                "instrument_display_provider"
            )
        if rule_snapshot_bundle is not None and snapshot_bundle is not None:
            raise DomainValidationError(
                "pass only one of rule_snapshot_bundle and snapshot_bundle"
            )
        selected_rule_snapshot = (
            rule_snapshot_bundle
            if rule_snapshot_bundle is not None
            else snapshot_bundle
        )
        if selected_rule_snapshot is not None:
            from app.instruments.rule_snapshots import RunRuleSnapshotBundle

            if not isinstance(selected_rule_snapshot, RunRuleSnapshotBundle):
                raise DomainValidationError(
                    "rule_snapshot_bundle must be a RunRuleSnapshotBundle"
                )
            if selected_rule_snapshot.run_id is not None and str(
                selected_rule_snapshot.run_id
            ) != run_id:
                raise DomainValidationError(
                    "rule_snapshot_bundle is bound to a different run"
                )
            # Verify once at admission and again whenever a segment is read;
            # this keeps a tampered in-memory or persisted bundle from being
            # consumed by the execution loop.
            selected_rule_snapshot.verify_hash()
        self._run_id = run_id
        self._axis = axis
        self._timing_policy = timing_policy
        self._view_factory = view_factory
        self._display_provider = (
            display_provider
            if display_provider is not None
            else instrument_display_provider
        )
        self._strategy = strategy
        self._interpreter = interpreter
        self._execution_model = execution_model
        self._accounting = accounting
        self._corporate_actions = corporate_actions
        self._analyzers = tuple(analyzers)
        self._currency = currency.upper()
        self._settlement_calendar = settlement_calendar
        self._rule_snapshot_bundle = selected_rule_snapshot
        if (
            candidate_eligibility_evaluator is not None
            and candidate_eligibility is not None
        ):
            raise DomainValidationError(
                "provide only one of candidate_eligibility_evaluator and "
                "candidate_eligibility"
            )
        if (
            candidate_evaluator is not None
            and (
                candidate_eligibility_evaluator is not None
                or candidate_eligibility is not None
            )
        ):
            raise DomainValidationError(
                "provide only one candidate eligibility evaluator"
            )
        if universe_scope_resolution is not None and universe_scope is not None:
            raise DomainValidationError(
                "provide only one of universe_scope_resolution and universe_scope"
            )
        self._candidate_eligibility_evaluator = (
            candidate_eligibility_evaluator
            if candidate_eligibility_evaluator is not None
            else candidate_eligibility
            if candidate_eligibility is not None
            else candidate_evaluator
        )
        self._universe_scope_resolution = (
            universe_scope_resolution
            if universe_scope_resolution is not None
            else universe_scope
        )
        # Keep the view factory as the dependency boundary: callers that
        # construct the provider-backed factory can attach the same frozen
        # scope/evaluator there without duplicating it in runner kwargs.
        if self._candidate_eligibility_evaluator is None:
            self._candidate_eligibility_evaluator = getattr(
                view_factory, "candidate_eligibility_evaluator", None
            )
        if self._universe_scope_resolution is None:
            self._universe_scope_resolution = getattr(
                view_factory, "universe_scope_resolution", None
            )
        if self._universe_scope_resolution is not None:
            scope_status = self._resolve_attr(
                self._universe_scope_resolution, ("status",)
            )
            scope_status = getattr(scope_status, "value", scope_status)
            if scope_status not in (None, "ready"):
                issue_code = self._resolve_attr(
                    self._universe_scope_resolution,
                    ("primary_issue_code", "code"),
                )
                if issue_code == "universe_capability_missing":
                    raise _universe_capability_error(
                        "the dynamic universe scope is missing a required provider capability",
                        details={"reason_code": issue_code},
                    )
                from app.backtesting.data.errors import UniverseScopeUnresolvedError

                raise UniverseScopeUnresolvedError(
                    "the dynamic universe scope is unresolved",
                    details={"reason_code": issue_code or "universe_scope_unresolved"},
                )
        self._explicit_fixed_authorized_ids = self._normalize_uuid_ids(
            fixed_authorized_instrument_ids,
            "fixed_authorized_instrument_ids",
            allow_none=True,
        )
        self._frozen_universe_calendar_ids = self._resolve_frozen_calendar_ids(
            self._universe_scope_resolution
        )
        scope_mode = self._resolve_attr(
            self._universe_scope_resolution, ("scope_mode",)
        )
        scope_mode = getattr(scope_mode, "value", scope_mode)
        if scope_mode in ("dynamic", "hybrid") and not self._frozen_universe_calendar_ids:
            from app.backtesting.data.errors import UniverseScopeUnresolvedError

            raise UniverseScopeUnresolvedError(
                "a dynamic/hybrid runtime requires a finite preflighted calendar set",
                details={"reason_code": "universe_scope_unresolved"},
            )
        self._universe_scope_snapshot_hash = self._resolve_scope_hash(
            self._universe_scope_resolution
        )
        if self._universe_scope_snapshot_hash is None:
            factory_universe = getattr(view_factory, "_universe_dto", None)
            raw_universe = getattr(
                factory_universe, "_UniverseQueryDTO__query", factory_universe
            )
            self._universe_scope_snapshot_hash = self._resolve_scope_hash(
                raw_universe
            )
        self._universe_eligibility_policy_version = (
            self._resolve_policy_version(self._universe_scope_resolution)
        )
        self._rule_policy_cache: dict[tuple[UUID, date], Any] = {}
        self._used_rule_segments: dict[tuple[UUID, date], str] = {}
        if analysis_admission is not None:
            if analysis_engine is not None:
                raise DomainValidationError(
                    "provide either analysis_admission or analysis_engine, "
                    "never both"
                )
            analysis_engine = getattr(analysis_admission, "engine", None)
            if analysis_engine is None:
                raise DomainValidationError(
                    "analysis_admission must expose its admitted engine"
                )
        elif analysis_engine is not None:
            # A bare AnalyzerEngine has no proof that E0, cash-flow, PIT, and
            # portfolio gates were executed.  Requiring the coordinator
            # object (below) closes the old mutable-attribute bypass.
            raise DomainValidationError(
                "analyzer-enabled runners must be constructed through the "
                "run-admission coordinator; pass analysis_admission"
            )
        # Analyzer subsystem wiring: the engine accumulates accounting and
        # valuation facts; the PIT gateway is the only accepted source of
        # valuation data_cutoff values.  Both are optional so legacy runs
        # without metric analysis keep working unchanged.
        if analysis_engine is not None:
            from app.backtesting.analyzers import _is_coordinator_admitted

            capability_token = getattr(analysis_admission, "_capability_token", None)
            if not _is_coordinator_admitted(analysis_engine, capability_token):
                raise DomainValidationError(
                    "analysis_admission is not a coordinator-issued admission "
                    "for this engine"
                )
            engine_run_id = getattr(analysis_engine, "run_id", None)
            if engine_run_id != run_id:
                raise DomainValidationError(
                    f"analysis_engine belongs to run {engine_run_id!r} but "
                    f"the runner executes {run_id!r}"
                )
            engine_currency = getattr(
                analysis_engine, "reporting_currency", None
            )
            if (
                isinstance(engine_currency, str)
                and engine_currency.strip().upper() != self._currency
            ):
                raise DomainValidationError(
                    f"analysis_engine reporting currency {engine_currency!r} "
                    f"does not match the runner currency {self._currency!r}"
                )
            # Admission evidence is read from the immutable coordinator
            # result, never from mutable engine attributes.
            admission_evidence = getattr(analysis_admission, "admission_evidence", None)
            if not admission_evidence:
                raise DomainValidationError(
                    "analysis_admission has no frozen admission evidence"
                )
            if admission_evidence.get("run_id") != run_id:
                raise DomainValidationError(
                    "analysis admission evidence is bound to a different run"
                )
            axis_sessions = tuple(
                step.metadata.get("session_date") for step in tuple(axis)
            )
            admitted_sessions = tuple(
                admission_evidence.get("formal_sessions", ())
            )
            if admitted_sessions != axis_sessions:
                raise DomainValidationError(
                    "analysis admission evidence does not match the official "
                    "runner timeline"
                )
            if admission_evidence.get("formal_session_count") != len(axis_sessions):
                raise DomainValidationError(
                    "analysis admission evidence has an invalid session count"
                )
            from app.backtesting.analysis_inputs import (
                FormalSessionTimeline,
                compute_formal_timeline_hash,
            )

            admitted_timeline_hash = admission_evidence.get("timeline_hash")
            parsed_admitted_sessions = tuple(
                date.fromisoformat(value) for value in admitted_sessions
            )
            timeline_payload = admission_evidence.get("formal_timeline")
            if not isinstance(timeline_payload, Mapping):
                raise DomainValidationError(
                    "analysis admission evidence has no FormalSessionTimeline"
                )
            admitted_timeline = FormalSessionTimeline(
                tuple(date.fromisoformat(value) for value in timeline_payload.get("sessions", ())),
                timeline_hash=timeline_payload.get("timeline_hash"),
            )
            if admitted_timeline.sessions != parsed_admitted_sessions:
                raise DomainValidationError(
                    "analysis admission evidence carries conflicting formal timelines"
                )
            if admitted_timeline_hash != compute_formal_timeline_hash(
                parsed_admitted_sessions
            ):
                raise DomainValidationError(
                    "analysis admission evidence has an invalid formal timeline hash"
                )
            if admitted_timeline_hash != compute_formal_timeline_hash(
                tuple(date.fromisoformat(value) for value in axis_sessions)
            ):
                raise DomainValidationError(
                    "analysis admission evidence does not match the runner timeline hash"
                )
            if admission_evidence.get("initial_equity_hash") != getattr(
                getattr(analysis_engine, "_initial_equity_snapshot", None),
                "evidence_hash",
                None,
            ):
                raise DomainValidationError(
                    "analysis admission evidence is not bound to the engine E0"
                )
            if admitted_timeline_hash != getattr(
                getattr(analysis_engine, "_initial_equity_snapshot", None),
                "timeline_hash",
                None,
            ):
                raise DomainValidationError(
                    "analysis admission evidence is not bound to the engine timeline"
                )
            if admitted_timeline != getattr(
                getattr(analysis_engine, "_initial_equity_snapshot", None),
                "formal_timeline",
                None,
            ):
                raise DomainValidationError(
                    "analysis admission evidence is not bound to the engine FormalSessionTimeline"
                )
            from app.backtesting.analysis_admission import (
                compute_portfolio_snapshot_binding,
            )

            portfolio_snapshot_id, portfolio_snapshot_hash = (
                compute_portfolio_snapshot_binding(
                    initial_portfolio,
                    reporting_currency=self._currency,
                )
            )
            if (
                admission_evidence.get("portfolio_snapshot_id")
                != portfolio_snapshot_id
                or admission_evidence.get("portfolio_snapshot_hash")
                != portfolio_snapshot_hash
            ):
                raise DomainValidationError(
                    "the runner initial portfolio changed after analysis "
                    "admission or belongs to different E0 evidence"
                )
            expected_first_step = self._axis.at(0).sequence
            if admission_evidence.get("first_step_sequence") != expected_first_step:
                raise DomainValidationError(
                    "analysis admission evidence is not bound to the runner "
                    "step sequence"
                )
            rate_snapshot = getattr(analysis_engine, "_rate_snapshot", None)
            if any(
                getattr(spec, "analyzer_key", None) == "sharpe_pit_rf"
                for spec in getattr(analysis_engine, "specs", ())
            ) and rate_snapshot is None:
                raise DomainValidationError(
                    "analyzer-enabled runner requires a frozen PIT rate snapshot"
                )
        if pit_data_gateway is not None and not hasattr(pit_data_gateway, "data_cutoff_at"):
            raise DomainValidationError(
                "pit_data_gateway must satisfy the PitAnalysisGateway "
                "protocol with an explicit data_cutoff_at()"
            )
        if analysis_engine is not None and pit_data_gateway is None:
            raise DomainValidationError(
                "runs with an analyzer engine require a pit_data_gateway; "
                "the VALUE phase must never guess its PIT data cutoff"
            )
        self._analysis_engine = analysis_engine
        self._analysis_admission = analysis_admission
        self._pit_data_gateway = pit_data_gateway
        # Pin the official timeline into the analyzer so equity facts can
        # never skip a formal session (e.g. a zero-return day) or arrive
        # with an inverted/duplicated step sequence.
        if analysis_engine is not None and hasattr(
            analysis_engine, "attach_formal_timeline"
        ):
            sessions = [
                date.fromisoformat(step.metadata["session_date"])
                for step in tuple(self._axis)
            ]
            analysis_engine.attach_formal_timeline(
                sessions, first_step_sequence=self._axis.at(0).sequence
            )
        self._latest_analysis_snapshot: Any | None = None
        self._last_equity_observation: EquityObservation | None = None
        # Admission check: the accounting policy owns the real settlement
        # currency.  A mismatch would record a wrong currency in the audit
        # snapshot and fail later at fill application, so it is rejected
        # before the run starts instead.
        accounting_currency = getattr(self._accounting, "currency", None)
        if isinstance(accounting_currency, str):
            if accounting_currency.strip().upper() != self._currency:
                raise DomainValidationError(
                    f"runner currency {self._currency!r} does not match the "
                    f"accounting policy currency "
                    f"{accounting_currency.strip().upper()!r}"
                )
        # The timing policy's semantics assume T+1-before-open-match
        # settlement: a same-day or legacy policy would let sells fill one
        # session early and silently rewrite the documented walk-through.
        from app.backtesting.accounting import SettlementPolicy
        from app.backtesting.settlement import require_formal_settlement_policy

        require_formal_settlement_policy(
            getattr(self._accounting, "settlement_policy", None)
        )
        # Replaceable components must carry the stable identity a registry
        # entry guarantees; ad-hoc instances without key/version cannot be
        # audited and are rejected at admission.
        _require_registered_identity(
            "timing_policy", timing_policy, "policy_key"
        )
        _require_registered_identity(
            "execution_model", execution_model, "model_key"
        )
        _require_registered_identity(
            "decision_interpreter", interpreter, "interpreter_key"
        )
        self._component_parameters = MappingProxyType(
            {
                str(key): _freeze_payload(value)
                for key, value in dict(component_parameters or {}).items()
            }
        )
        # Lifecycle: the runner accepts only the contiguous next slice of
        # the official timeline and can never run twice.
        self._next_expected_step = 0
        self._analysis_chunk_sequence = 0
        self._last_analysis_chunk_sequence: int | None = None
        self._last_analysis_chunk_token: str | None = None
        self._last_analysis_completed_session: date | None = None
        self._finished = False
        self._failed = False

        self._portfolio = initial_portfolio
        register_initial = getattr(
            self._view_factory, "register_runtime_instrument_ids", None
        )
        if callable(register_initial):
            # Opening holdings are fixed preflight subjects even in a
            # dynamic-only run.  Registering them only broadens the engine's
            # read set for valuation/matching; it never widens strategy
            # candidate permissions or the frozen calendar set.
            register_initial(tuple(self._portfolio.positions))
        self._orders: list[Order] = []
        self._pending_fills: tuple[Fill, ...] = ()
        # Accounting-applied fee accumulator: the analyzer's equity
        # observations and cumulative-fee metrics read this total instead
        # of recomputing fees from fills.
        self._applied_fees_total = Decimal("0")
        # Cash-dividend state: declarations come from the source with the
        # entitlement still open; the runner freezes each entitlement in
        # its record-date session and credits it in the derived
        # cash-effective session, strictly after that session's match.
        self._dividend_declarations: tuple[CashDividendEvent, ...] = (
            _load_dividend_declarations(corporate_actions)
        )
        self._completed_dividend_events: dict[UUID, CashDividendEvent] = {
            # Declarations that already carry the source-supplied
            # entitlement are complete at admission, even when their
            # record date lies before the run window: without this
            # registration they would be silently skipped and never
            # credited in their cash-effective session.
            declaration.event_id: declaration
            for declaration in self._dividend_declarations
            if declaration.is_entitlement_frozen
        }
        # Frozen trading facts observed from engine views; later phases
        # without a view (submit, account) reuse exactly these values.
        self._instrument_facts: dict[UUID, InstrumentFacts] = {}
        self._events: list[EventEnvelope] = []
        self._equity_curve: list[EquitySample] = []
        self._decisions: list[Any] = []
        self._pending_decision: Any = None
        self._last_marks: dict[UUID, Decimal] = {}
        # Evidence captured by the engine-side refresh immediately before
        # final qualification.  This is intentionally keyed by stable ID and
        # never exposed to strategy code; formal dynamic runs use it to prove
        # that a target still has current-session market facts.
        self._final_market_data_evidence: dict[UUID, Mapping[str, Any]] = {}
        # Digest pieces of the most recently completed step, consumed by the
        # next decide phase's PreviousStepDTO.
        self._previous_step: PreviousStepDTO | None = None
        self._step_order_records: list[OrderSummaryDTO] = []
        self._step_fill_summaries: list[FillSummaryDTO] = []
        # Candidate identities are intentionally populated per decide step.
        # Calling ``candidate_identities()`` here would snapshot a dynamic
        # universe at runner construction and make later PIT dates observe a
        # stale/current catalogue.  Existing fixed fixtures are still loaded
        # on the first step by ``_bind_step_universe``.
        self._identities: dict[UUID, InstrumentCandidateDTO] = {}
        self._step_universes: dict[int, _StepBoundUniverse] = {}
        self._step_candidates: dict[int, tuple[Any, ...]] = {}
        self._final_qualification_results: list[Mapping[str, Any]] = []
        self._last_final_qualification_failure: Mapping[str, Any] | None = None
        self._filtered_reason_counts: dict[str, int] = {}
        self._filter_evidence_records: list[Mapping[str, Any]] = []
        # Dynamic authorization is earned only by a successful strategy-side
        # query in the current decision step.  The provider snapshot used to
        # build the context is intentionally not sufficient to authorize a
        # hard-coded target.
        self._step_queried_candidate_ids: dict[int, frozenset[UUID]] = {}
        self._step_decision_data_cutoffs: dict[int, datetime] = {}
        self._step_qualification_ports: dict[int, Any] = {}
        self._last_decision_data_cutoff: datetime | None = None
        if self._display_provider is None:
            # The factory is an explicit dependency boundary.  Only a
            # factory-provided resolver is considered; candidate identities
            # and current catalogue snapshots are never used as historical
            # display fallback data.
            factory_display = getattr(view_factory, "display_snapshot", None)
            if callable(factory_display):
                self._display_provider = factory_display
        self._namespace = uuid5(
            NAMESPACE_URL, f"quant-foundry:backtest-run:{self._run_id}"
        )

    @property
    def rule_snapshot_hash(self) -> str | None:
        """Return the immutable rule snapshot hash bound at admission."""

        bundle = self._rule_snapshot_bundle
        return bundle.snapshot_hash if bundle is not None else None

    @property
    def universe_scope_snapshot_hash(self) -> str | None:
        """Return the frozen dynamic-scope hash used by this run."""

        return self._universe_scope_snapshot_hash

    @property
    def final_qualification_results(self) -> tuple[Mapping[str, Any], ...]:
        """Return immutable target qualification evidence collected so far."""

        return tuple(self._final_qualification_results)

    @property
    def final_qualification_failure(self) -> Mapping[str, Any] | None:
        """Return the latest failed-target evidence, if the run aborted there."""

        return self._last_final_qualification_failure

    @staticmethod
    def _normalize_uuid_ids(
        values: Sequence[UUID] | None,
        field_name: str,
        *,
        allow_none: bool = False,
    ) -> frozenset[UUID]:
        """Normalize an optional fixed-authority identity collection."""

        if values is None and allow_none:
            return frozenset()
        if values is None:
            raise DomainValidationError(f"{field_name} must be an iterable of UUIDs")
        if isinstance(values, (str, bytes)):
            raise DomainValidationError(f"{field_name} must be an iterable of UUIDs")
        try:
            normalized = tuple(values)
        except TypeError as exc:
            raise DomainValidationError(
                f"{field_name} must be an iterable of UUIDs"
            ) from exc
        if any(not isinstance(value, UUID) for value in normalized):
            raise DomainValidationError(f"{field_name} entries must be UUIDs")
        return frozenset(normalized)

    @staticmethod
    def _resolve_attr(source: Any, names: Sequence[str]) -> Any:
        """Return the first non-None attribute among a compatibility alias set."""

        if source is None:
            return None
        for name in names:
            value = getattr(source, name, None)
            if value is not None:
                return value
        return None

    @classmethod
    def _resolve_frozen_calendar_ids(cls, resolution: Any) -> tuple[str, ...]:
        """Read canonical calendar IDs from an immutable scope resolution."""

        value = cls._resolve_attr(
            resolution,
            (
                "resolved_calendar_ids",
                "frozen_calendar_ids",
                "allowed_calendar_ids",
            ),
        )
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            value = (value,)
        try:
            values = tuple(value)
        except TypeError as exc:
            raise DomainValidationError(
                "universe scope calendar IDs must be an iterable"
            ) from exc
        normalized: set[str] = set()
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise DomainValidationError(
                    "universe scope calendar IDs must be non-blank strings"
                )
            normalized.add(cls._canonical_calendar_id(item))
        return tuple(sorted(normalized))

    @staticmethod
    def _canonical_calendar_id(value: str) -> str:
        """Normalize a named calendar without deriving it from market labels."""

        try:
            from app.backtesting.calendar_axis import normalize_calendar_id

            return normalize_calendar_id(value)
        except Exception:
            # Legacy fixtures use simple labels that predate task-11's
            # canonicalizer.  Trimming/casing is the only compatibility
            # normalization; exchange, code prefix, and asset class are never
            # used to infer a calendar.
            return value.strip().upper()

    @classmethod
    def _resolve_scope_hash(cls, resolution: Any) -> str | None:
        """Extract a frozen scope hash while ignoring display metadata."""

        value = cls._resolve_attr(
            resolution,
            (
                "snapshot_hash",
                "scope_snapshot_hash",
                "universe_scope_snapshot_hash",
            ),
        )
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise DomainValidationError(
                "universe scope snapshot hash must be non-blank text"
            )
        return value.strip()

    @classmethod
    def _resolve_policy_version(cls, resolution: Any) -> str | None:
        """Extract the qualification-policy identity for runtime audit output."""

        value = cls._resolve_attr(
            resolution,
            (
                "qualification_policy_version",
                "qualification_policy",
                "policy_version",
            ),
        )
        if value is None:
            return None
        key = getattr(value, "key", None)
        version = getattr(value, "version", None)
        if isinstance(key, str) and version is not None:
            return f"{key}@{version}"
        return str(value)

    @classmethod
    def _scope_mode_value(cls, value: Any) -> str | None:
        """Normalize a scope-mode enum or string for runtime gates."""

        value = getattr(value, "value", value)
        return value if isinstance(value, str) else None

    def _is_formal_dynamic_scope(
        self,
        *,
        bound: _StepBoundUniverse | None = None,
        source: Any | None = None,
    ) -> bool:
        """Return whether the current candidate path is formal dynamic/hybrid.

        Legacy strategy-only fixtures do not carry a scope mode and retain
        their historical fixed behavior.  Once admission or a bound data
        query explicitly says ``dynamic``/``hybrid``, however, the runtime
        must require a real qualification port and step-earned permissions.
        """

        resolution = self._universe_scope_resolution
        mode = self._scope_mode_value(
            self._resolve_attr(resolution, ("scope_mode",))
        )
        if mode in {"dynamic", "hybrid"}:
            return True
        if source is None and bound is not None:
            source = self._source_universe_query(bound)
        raw_source = _raw_universe_source(source)
        mode = self._scope_mode_value(
            self._resolve_attr(raw_source, ("scope_mode",))
        )
        if mode in {"dynamic", "hybrid"}:
            return True
        policy = self._resolve_attr(
            raw_source, ("universe_query_policy", "universe_policy")
        )
        return bool(getattr(policy, "candidate_set_rules", ()))

    def _record_step_queried_candidates(
        self, step_sequence: int, instrument_ids: Sequence[UUID]
    ) -> None:
        """Record only IDs returned through this step's strategy query."""

        values = tuple(instrument_ids)
        if any(not isinstance(value, UUID) for value in values):
            raise DomainValidationError(
                "strategy universe query returned non-UUID instrument ids"
            )
        previous = self._step_queried_candidate_ids.get(step_sequence, frozenset())
        self._step_queried_candidate_ids[step_sequence] = frozenset(
            previous | set(values)
        )

    def _fixed_authorized_ids(self) -> frozenset[UUID]:
        """Return fixed permissions, including every opening holding."""

        values = set(self._explicit_fixed_authorized_ids)
        # BacktestViewFactory keeps this tuple private by design; reading it
        # here only captures the already-admitted fixed scope and does not
        # expose the underlying provider to strategy code.
        values.update(
            value
            for value in getattr(self._view_factory, "_scope_instrument_ids", ())
            if isinstance(value, UUID)
        )
        values.update(
            value
            for value in getattr(self._view_factory, "scope_instrument_ids", ())
            if isinstance(value, UUID)
        )
        values.update(
            value
            for value in getattr(
                self._view_factory, "fixed_authorized_instrument_ids", ()
            )
            if isinstance(value, UUID)
        )
        values.update(self._portfolio.positions)
        resolution = self._universe_scope_resolution
        for name in (
            "fixed_instrument_ids",
            "fixed_authorized_instrument_ids",
            "mandatory_instrument_ids",
        ):
            candidate_ids = getattr(resolution, name, ()) if resolution is not None else ()
            if candidate_ids:
                values.update(
                    value for value in candidate_ids if isinstance(value, UUID)
                )
        return frozenset(values)

    def _refresh_target_market_data(
        self, instrument_ids: Sequence[UUID], context: PhaseContext
    ) -> None:
        """Load explicit current-close facts after final qualification.

        Dynamic targets are selected after the close valuation view has been
        constructed.  A provider-backed factory may therefore need one
        explicit same-session read for sizing; this read happens only after
        all target qualification checks pass and before any order is built.
        """

        if not instrument_ids:
            return
        requested_ids = tuple(sorted(set(instrument_ids), key=str))
        # Start with an explicit unavailable record for every requested
        # identity.  Provider responses replace these entries only when the
        # returned object has the exact stable identity and expected type.
        self._final_market_data_evidence = {
            instrument_id: {
                "session_date": context.session_date,
                "data_cutoff": self._step_decision_data_cutoffs.get(
                    context.step_sequence,
                    self._last_decision_data_cutoff or context.decision_time,
                ),
                "quote_present": False,
                "close_available": False,
                "facts_present": False,
                "complete": False,
            }
            for instrument_id in requested_ids
        }
        resolver = getattr(self._view_factory, "refresh_market_data", None)
        if callable(resolver):
            try:
                result = resolver(requested_ids, context.session_date)
            except TypeError:
                result = resolver(
                    instrument_ids=requested_ids,
                    session_date=context.session_date,
                )
        else:
            market = getattr(self._view_factory, "_engine_market_data", None)
            if market is None:
                return
            try:
                result = (
                    market.session_quotes(requested_ids, context.session_date),
                    market.instrument_facts(requested_ids),
                )
            except Exception as exc:
                raise _universe_provider_error(
                    "engine market data cannot refresh a final-qualified target",
                    details={"error_type": type(exc).__name__},
                ) from exc
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            raise _universe_provider_error(
                "refresh_market_data must return (quotes, facts)"
            )
        quotes, facts = result
        if isinstance(quotes, Mapping):
            for instrument_id, quote in quotes.items():
                if isinstance(instrument_id, UUID) and isinstance(quote, SessionQuote):
                    if instrument_id not in self._final_market_data_evidence:
                        continue
                    if quote.instrument_id != instrument_id:
                        raise _universe_provider_error(
                            "market data quote identity does not match its key",
                            details={"instrument_id": instrument_id},
                        )
                    if quote.close_price is not None:
                        self._last_marks[instrument_id] = quote.close_price
                    evidence = dict(self._final_market_data_evidence[instrument_id])
                    evidence.update(
                        {
                            "quote_present": True,
                            "close_available": quote.close_price is not None,
                            "quote_evidence": quote.evidence,
                        }
                    )
                    self._final_market_data_evidence[instrument_id] = evidence
        if isinstance(facts, Mapping):
            for instrument_id, fact in facts.items():
                if isinstance(instrument_id, UUID) and isinstance(fact, InstrumentFacts):
                    if instrument_id not in self._final_market_data_evidence:
                        continue
                    if fact.instrument_id != instrument_id:
                        raise _universe_provider_error(
                            "instrument facts identity does not match its key",
                            details={"instrument_id": instrument_id},
                        )
                    self._instrument_facts[instrument_id] = fact
                    evidence = dict(self._final_market_data_evidence[instrument_id])
                    evidence.update(
                        {
                            "facts_present": True,
                            "calendar_id": fact.calendar_id,
                            "suspended": fact.suspended,
                            "buy_allowed": fact.buy_allowed,
                            "sell_allowed": fact.sell_allowed,
                        }
                    )
                    self._final_market_data_evidence[instrument_id] = evidence
        for instrument_id, evidence in tuple(
            self._final_market_data_evidence.items()
        ):
            updated = dict(evidence)
            updated["complete"] = bool(
                updated.get("quote_present")
                and updated.get("close_available")
                and updated.get("facts_present")
            )
            self._final_market_data_evidence[instrument_id] = updated

    def _candidate_evaluator(
        self, *, allow_pure_fallback: bool = True
    ) -> Any | None:
        """Resolve the one canonical candidate qualification port.

        ``evaluate_candidate`` is a useful compatibility fallback for legacy
        in-memory checks, but it is not a provider-owned requalification port:
        it cannot re-fetch current identity, coverage, action, or status facts.
        Formal dynamic/hybrid execution therefore asks this method with the
        fallback disabled and requires an explicitly wired provider port.
        """

        evaluator = self._candidate_eligibility_evaluator
        if evaluator is not None:
            if callable(evaluator):
                return evaluator
            if isinstance(evaluator, Mapping):
                # Small in-memory acceptance fixtures may provide one
                # already-evaluated result per stable identity.  Treat that
                # map as a read-only qualification port, not as a second
                # filtering implementation.
                def _lookup(candidate=None, instrument_id=None, **_kwargs):
                    resolved_id = instrument_id or getattr(
                        candidate, "instrument_id", None
                    )
                    return evaluator.get(
                        resolved_id,
                        evaluator.get(str(resolved_id)) if resolved_id is not None else None,
                    )

                return _lookup
            method = getattr(evaluator, "evaluate_candidate", None)
            if callable(method):
                return method
            method = getattr(evaluator, "evaluate", None)
            if callable(method):
                return method
            for name in (
                "qualify_candidate",
                "qualify",
                "final_qualify",
                "recheck_candidate",
                "recheck",
            ):
                method = getattr(evaluator, name, None)
                if callable(method):
                    return method
            raise DomainValidationError(
                "candidate_eligibility_evaluator must be callable or expose "
                "evaluate_candidate()"
            )
        for source in (
            self._view_factory,
            self._universe_scope_resolution,
        ):
            for name in (
                "evaluate_candidate",
                "qualify_candidate",
                "qualify",
                "evaluate",
                "candidate_eligibility",
                "candidate_eligibility_evaluator",
                "final_qualify",
                "recheck_candidate",
                "recheck",
            ):
                candidate = getattr(source, name, None)
                if callable(candidate):
                    return candidate
        for bound in self._step_universes.values():
            source = self._source_universe_query(bound)
            for name in (
                "evaluate_candidate",
                "qualify_candidate",
                "qualify",
                "evaluate",
                "candidate_eligibility",
                "candidate_eligibility_evaluator",
                "final_qualify",
                "recheck_candidate",
                "recheck",
            ):
                candidate = getattr(source, name, None)
                if callable(candidate):
                    return candidate
                if isinstance(candidate, Mapping):
                    mapping = candidate

                    def _lookup_from_source(
                        candidate=None, instrument_id=None, **_kwargs
                    ):
                        resolved_id = instrument_id or getattr(
                            candidate, "instrument_id", None
                        )
                        return mapping.get(
                            resolved_id,
                            mapping.get(str(resolved_id))
                            if resolved_id is not None
                            else None,
                        )

                    return _lookup_from_source
        # The pure task-15 evaluator is auto-selected only when the run has
        # an explicit formal scope resolution or provider-side evidence.  A
        # legacy strategy-only DTO has no identity/rule/coverage evidence and
        # must not be misclassified as ineligible merely because those fields
        # are intentionally hidden from strategy code.
        has_formal_evidence = self._universe_scope_resolution is not None or any(
            getattr(self._source_universe_query(bound), name, None) is not None
            for bound in self._step_universes.values()
            for name in ("universe_scope_snapshot_hash", "allowed_calendar_ids")
        ) or any(
            any(
                hasattr(candidate, name)
                for name in (
                    "spec",
                    "identity_evidence",
                    "mapping_evidence",
                    "rule_evidence",
                    "market_data_evidence",
                )
            )
            for bound in self._step_universes.values()
            for candidate in bound.candidates
        )
        if allow_pure_fallback and has_formal_evidence:
            try:
                from app.backtesting.data.universe import evaluate_candidate
            except (ImportError, AttributeError):
                evaluate_candidate = None
            if callable(evaluate_candidate):
                return evaluate_candidate
        return None

    def _verify_scope_snapshot(self, context: PhaseContext) -> None:
        """Verify the frozen dynamic scope before strategy code can run.

        Scope providers may expose a verifier, a current hash, or both.  The
        runtime accepts all three forms so the data-layer implementation can
        remain independent of the phase loop, but every mismatch is surfaced
        as the same stable machine code.  No changed scope is installed and
        no time-axis operation is attempted here.
        """

        resolution = self._universe_scope_resolution
        if resolution is None:
            return
        verifier = self._resolve_attr(
            resolution,
            ("verify_hash", "verify_snapshot", "assert_unchanged"),
        )
        if callable(verifier):
            try:
                verifier()
            except Exception as exc:
                if getattr(exc, "code", None) == "universe_preflight_hash_mismatch":
                    raise
                self._raise_final_qualification_error(
                    "universe_preflight_hash_mismatch",
                    instrument_id=UUID(int=0),
                    context=context,
                    decision=self._pending_decision,
                    candidate=None,
                    failed_check="scope_snapshot",
                    reason_codes=("universe_preflight_hash_mismatch",),
                    expected=self._universe_scope_snapshot_hash,
                    actual={"error_type": type(exc).__name__, "error": str(exc)},
                )
        expected = self._universe_scope_snapshot_hash
        if expected is None:
            return
        current = self._resolve_attr(
            resolution,
            (
                "current_snapshot_hash",
                "current_scope_snapshot_hash",
                "current_hash",
                "observed_snapshot_hash",
                "snapshot_hash",
                "scope_snapshot_hash",
            ),
        )
        if current is not None and str(current) != expected:
            self._raise_final_qualification_error(
                "universe_preflight_hash_mismatch",
                instrument_id=UUID(int=0),
                context=context,
                decision=self._pending_decision,
                candidate=None,
                failed_check="scope_snapshot",
                reason_codes=("universe_preflight_hash_mismatch",),
                expected=expected,
                actual=current,
            )

    @staticmethod
    def _source_universe_query(bound: _StepBoundUniverse) -> Any | None:
        """Return the original query object for immutable boundary evidence."""

        source = bound.source
        # A ``UniverseQueryDTO`` wraps the data query in a private attribute;
        # the fallback is intentionally best-effort and never mutates it.
        source = getattr(source, "_UniverseQueryDTO__query", source)
        return getattr(source, "_ChunkUniverseQuery__query", source)

    @staticmethod
    def _clear_prestrategy_universe_authorization(source: Any) -> None:
        """Undo provider-side eager authorization before strategy execution.

        Some chunk-backed query facades authorize their result as part of
        ``query()``.  Runtime performs that read to freeze a complete engine
        snapshot, but the permission must not become effective until the
        strategy invokes its own bound ``universe.query()``.  The compatibility
        hooks are intentionally private/read-only and are ignored for simple
        strategy-only fixtures that do not expose them.
        """

        pending: list[Any] = [source]
        seen: set[int] = set()
        while pending and len(seen) < 8:
            current = pending.pop(0)
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            clearer = getattr(current, "_clear_authorized_candidates", None)
            if callable(clearer):
                clearer()
            clearer = getattr(current, "clear_step_candidate_authorization", None)
            if callable(clearer):
                clearer()
            for attribute in (
                "_UniverseQueryDTO__query",
                "_ChunkUniverseQuery__view",
                "_ChunkStrategyDataView__chunk",
            ):
                nested = getattr(current, attribute, None)
                if nested is not None:
                    pending.append(nested)

    def _step_universe_source(self, context: PhaseContext) -> Any:
        """Resolve a fresh bound universe source for one decide step."""

        for name in ("universe_for_step", "bound_universe", "universe"):
            resolver = getattr(self._view_factory, name, None)
            if not callable(resolver):
                continue
            if name == "universe":
                try:
                    return resolver(
                        effective_date=context.session_date,
                        data_cutoff=context.decision_time,
                    )
                except TypeError:
                    return resolver()
            # Step-aware providers can use either date names or the complete
            # phase context.  Signature inspection avoids catching a provider
            # body TypeError and accidentally invoking it twice.
            try:
                parameters = signature(resolver).parameters
            except (TypeError, ValueError):
                return resolver()
            kwargs: dict[str, Any] = {}
            aliases = {
                "effective_date": context.session_date,
                "session_date": context.session_date,
                "decision_date": context.session_date,
                "data_cutoff": context.decision_time,
                "decision_time": context.decision_time,
                "context": context,
            }
            for parameter_name, value in aliases.items():
                parameter = parameters.get(parameter_name)
                if parameter is not None and parameter.kind in (
                    Parameter.POSITIONAL_OR_KEYWORD,
                    Parameter.KEYWORD_ONLY,
                ):
                    kwargs[parameter_name] = value
            if kwargs:
                return resolver(**kwargs)
            return resolver()
        scope_mode = self._resolve_attr(
            self._universe_scope_resolution, ("scope_mode",)
        )
        scope_mode = getattr(scope_mode, "value", scope_mode)
        if scope_mode in ("dynamic", "hybrid"):
            raise _universe_capability_error(
                "view factory does not expose the required PIT UniverseQuery",
                details={"scope_mode": scope_mode},
            )
        raise _universe_provider_error(
            "view factory does not expose a bound UniverseQuery"
        )

    def _bind_step_universe(
        self,
        context: PhaseContext,
        *,
        source_override: Any | None = None,
    ) -> _StepBoundUniverse:
        """Query and freeze candidates immediately before strategy execution."""

        existing = self._step_universes.get(context.step_sequence)
        if existing is not None:
            return existing
        self._verify_scope_snapshot(context)
        source = (
            source_override
            if source_override is not None
            else self._step_universe_source(context)
        )
        expected_hash = self._universe_scope_snapshot_hash
        raw_source = _raw_universe_source(source)
        source_hash = self._resolve_attr(
            raw_source,
            ("snapshot_hash", "scope_snapshot_hash", "universe_scope_snapshot_hash"),
        )
        if expected_hash is not None and source_hash is not None and str(source_hash) != expected_hash:
            self._raise_final_qualification_error(
                "universe_preflight_hash_mismatch",
                instrument_id=UUID(int=0),
                context=context,
                decision=self._pending_decision,
                candidate=None,
                failed_check="scope_snapshot",
                reason_codes=("universe_preflight_hash_mismatch",),
                expected=expected_hash,
                actual=source_hash,
            )
        source_effective = getattr(raw_source, "effective_date", None)
        if source_effective is not None and source_effective != context.session_date:
            self._raise_final_qualification_error(
                "universe_pit_boundary_violation",
                instrument_id=UUID(int=0),
                context=context,
                decision=self._pending_decision,
                candidate=None,
                failed_check="effective_date",
                reason_codes=("universe_pit_boundary_violation",),
                expected=context.session_date,
                actual=source_effective,
            )
        source_boundary = getattr(raw_source, "boundary", None)
        source_cutoff = getattr(source_boundary, "data_cutoff", None)
        if source_cutoff is not None and source_cutoff != context.decision_time:
            self._raise_final_qualification_error(
                "universe_pit_boundary_violation",
                instrument_id=UUID(int=0),
                context=context,
                decision=self._pending_decision,
                candidate=None,
                failed_check="data_cutoff",
                reason_codes=("universe_pit_boundary_violation",),
                expected=context.decision_time,
                actual=source_cutoff,
            )
        query_observer = (
            lambda instrument_ids, step_sequence=context.step_sequence: self._record_step_queried_candidates(
                step_sequence, instrument_ids
            )
        )
        if isinstance(source, _StepBoundUniverse):
            # A factory may already return a bound immutable snapshot.  It is
            # still safe to use it as the provider snapshot, but only a
            # runtime-created wrapper can observe strategy query results.
            bound = _StepBoundUniverse(
                source.candidates,
                source=source.source,
                query_observer=query_observer,
            )
        elif isinstance(source, (list, tuple)):
            bound = _StepBoundUniverse(source, query_observer=query_observer)
        else:
            # ``UniverseQueryDTO`` is a strategy boundary and validates that
            # its output is already projected DTO data.  Runtime needs the
            # provider's complete engine-side rows first, so unwrap only the
            # known DTO facade for this internal snapshot read; the bound
            # ``_StepBoundUniverse`` performs the projection afterwards.
            query_source = getattr(source, "_UniverseQueryDTO__query", source)
            query_method = getattr(query_source, "query", None)
            if not callable(query_method):
                # A chunk facade may itself wrap a typed ``DataUniverseQuery``
                # (which is a request value, not a strategy query).  Keep the
                # facade in that case so it can delegate to its owning chunk.
                query_source = source
                query_method = getattr(query_source, "query", None)
            if not callable(query_method):
                raise _universe_provider_error(
                    "bound UniverseQuery must expose query()"
                )
            try:
                candidates = query_method()
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code == "unsupported_capability":
                    raise _universe_capability_error(
                        "the provider cannot serve the frozen universe query",
                        details={"cause_code": code},
                    ) from exc
                raise
            try:
                bound = _StepBoundUniverse(
                    candidates,
                    source=query_source,
                    query_observer=query_observer,
                )
            except TypeError as exc:
                raise _universe_provider_error(
                    "universe provider returned a non-iterable result"
                ) from exc
        self._step_universes[context.step_sequence] = bound
        self._step_candidates[context.step_sequence] = bound.candidates
        self._capture_universe_filter_evidence(source)
        for candidate in bound.strategy_candidates:
            self._identities[candidate.instrument_id] = candidate
        return bound

    def _capture_universe_filter_evidence(self, source: Any) -> None:
        """Copy provider-owned filter counts into the run audit summary."""

        sources: list[Any] = [
            source,
            getattr(source, "_UniverseQueryDTO__query", source),
        ]
        # A chunk-backed strategy facade keeps its chunk private.  The
        # optional public provider summary is preferred; these narrowly named
        # compatibility attributes are only read for audit projection and
        # never used to authorize a target.
        raw_source = getattr(source, "_UniverseQueryDTO__query", source)
        view = getattr(raw_source, "_ChunkUniverseQuery__view", None)
        chunk = getattr(view, "_ChunkStrategyDataView__chunk", None)
        sources.extend((view, chunk))

        def collect_counts(value: Any) -> Mapping[Any, Any] | None:
            """Accept both direct count maps and named filter summaries."""

            if not isinstance(value, Mapping):
                return None
            for nested_name in (
                "reason_counts",
                "filtered_reason_counts",
                "universe_filtered_reason_counts",
            ):
                nested = value.get(nested_name)
                if isinstance(nested, Mapping):
                    return nested
            return value

        def collect_records(value: Any) -> Sequence[Any] | None:
            if not isinstance(value, Mapping):
                return None
            records = value.get("records") or value.get("filters")
            if isinstance(records, Sequence) and not isinstance(
                records, (str, bytes)
            ):
                return records
            return None

        for candidate_source in sources:
            if candidate_source is None:
                continue
            counts = self._resolve_attr(
                candidate_source,
                (
                    "universe_filter_reason_counts",
                    "filtered_reason_counts",
                    "filter_reason_counts",
                ),
            )
            counts = collect_counts(counts)
            if counts is None:
                summary = self._resolve_attr(
                    candidate_source,
                    (
                        "filter_summary",
                        "candidate_filter_summary",
                        "universe_filter_summary",
                    ),
                )
                counts = collect_counts(summary)
            if counts is None:
                continue
            summary = self._resolve_attr(
                candidate_source,
                (
                    "filter_summary",
                    "candidate_filter_summary",
                    "universe_filter_summary",
                ),
            )
            records = collect_records(summary)
            if records:
                self._filter_evidence_records.extend(
                    item for item in records if isinstance(item, Mapping)
                )
            for reason, count in counts.items():
                if isinstance(count, bool):
                    continue
                try:
                    numeric_count = int(count)
                except (TypeError, ValueError):
                    continue
                if numeric_count < 0:
                    continue
                key = str(reason)
                self._filtered_reason_counts[key] = max(
                    self._filtered_reason_counts.get(key, 0), numeric_count
                )

    def _candidate_context(
        self,
        *,
        candidate: Any | None,
        instrument_id: UUID,
        context: PhaseContext,
        bound: _StepBoundUniverse,
    ) -> Any:
        """Build the canonical context when available, otherwise a safe fallback."""

        source = self._source_universe_query(bound)
        scope = self._universe_scope_resolution
        effective_date = context.session_date
        data_cutoff = self._step_decision_data_cutoffs.get(
            context.step_sequence,
            self._last_decision_data_cutoff or context.decision_time,
        )
        source_calendar_ids = tuple(
            getattr(source, "allowed_calendar_ids", ()) or ()
        )
        frozen_calendar_ids = (
            self._frozen_universe_calendar_ids or source_calendar_ids
        )
        values = {
            "candidate": candidate,
            "instrument": candidate,
            "instrument_id": instrument_id,
            "effective_date": effective_date,
            "effective_at": context.decision_time,
            "session_date": effective_date,
            "data_cutoff": data_cutoff,
            "data_cutoff_at": data_cutoff,
            "query_boundary": getattr(source, "boundary", None),
            "market_scope": getattr(source, "market_scope", None)
            or getattr(scope, "market_scope", None),
            "universe_query_policy": getattr(source, "universe_query_policy", None)
            or getattr(scope, "universe_query_policy", None),
            "universe_policy": getattr(source, "universe_query_policy", None)
            or getattr(scope, "universe_query_policy", None),
            "rule_package": getattr(source, "rule", None)
            or getattr(scope, "rule_package_reference", None),
            "rule_package_reference": getattr(source, "rule", None)
            or getattr(scope, "rule_package_reference", None),
            "rule_exception_set": getattr(source, "rule_exception_set", None)
            or getattr(scope, "rule_exception_set_reference", None),
            "rule_exception_set_reference": getattr(
                source, "rule_exception_set", None
            )
            or getattr(scope, "rule_exception_set_reference", None),
            "exception_set_reference": getattr(source, "rule_exception_set", None)
            or getattr(scope, "rule_exception_set_reference", None),
            "qualification_policy_version": getattr(
                source, "qualification_policy_version", None
            )
            or getattr(scope, "qualification_policy_version", None),
            "qualification_policy": getattr(
                source, "qualification_policy_version", None
            )
            or getattr(scope, "qualification_policy_version", None),
            "frozen_calendar_ids": frozen_calendar_ids,
            "resolved_calendar_ids": frozen_calendar_ids,
            "frozen_resolved_calendar_ids": frozen_calendar_ids,
            "scope_mode": getattr(source, "scope_mode", None)
            or getattr(scope, "scope_mode", None),
            "decision_time": context.decision_time,
            "required_capabilities": tuple(
                getattr(source, "required_capabilities", None)
                or getattr(scope, "required_capabilities", ())
                or ()
            ),
            "requested_window": getattr(scope, "requested_window", None),
            "universe_scope_snapshot_hash": getattr(
                source, "universe_scope_snapshot_hash", None
            )
            or self._universe_scope_snapshot_hash,
            "fixed_authorized_instrument_ids": tuple(
                sorted(self._fixed_authorized_ids(), key=str)
            ),
            "requested_window": getattr(scope, "requested_window", None),
            "required_capabilities": tuple(
                getattr(scope, "required_capabilities", ()) or ()
            ),
            "provider_capability_summary": getattr(
                scope, "capability_summary", {}
            )
            or {},
        }
        try:
            from app.backtesting.data.universe import CandidateEligibilityContext
        except (ImportError, AttributeError):
            CandidateEligibilityContext = None
        if CandidateEligibilityContext is not None:
            try:
                parameters = signature(CandidateEligibilityContext).parameters
                kwargs: dict[str, Any] = {}
                for name, parameter in parameters.items():
                    if name == "self" or name not in values:
                        continue
                    value = values[name]
                    if value is not None or parameter.default is Parameter.empty:
                        kwargs[name] = value
                return CandidateEligibilityContext(**kwargs)
            except (TypeError, ValueError):
                # The fallback still carries the same two PIT boundaries and
                # frozen scope; no data source is consulted here.
                pass
        return _RuntimeCandidateEligibilityContext(
            instrument_id=instrument_id,
            effective_date=effective_date,
            data_cutoff=data_cutoff,
            market_scope=values["market_scope"],
            universe_query_policy=values["universe_query_policy"],
            rule_package=values["rule_package"],
            rule_exception_set=values["rule_exception_set"],
            qualification_policy_version=values["qualification_policy_version"],
            frozen_calendar_ids=tuple(values["frozen_calendar_ids"] or ()),
            scope_mode=values["scope_mode"],
            required_capabilities=tuple(values["required_capabilities"] or ()),
            requested_window=values["requested_window"],
            query_boundary=values["query_boundary"],
            universe_scope_snapshot_hash=values["universe_scope_snapshot_hash"],
            provider_capability_summary=values["provider_capability_summary"] or {},
            candidate=candidate,
        )

    @staticmethod
    def _invoke_candidate_evaluator(
        evaluator: Any,
        *,
        candidate: Any | None,
        instrument_id: UUID,
        eligibility_context: Any,
    ) -> Any:
        """Call one evaluator using its declared signature only once."""

        try:
            parameters = list(signature(evaluator).parameters.values())
        except (TypeError, ValueError):
            return evaluator(candidate, eligibility_context)
        positional: list[Any] = []
        kwargs: dict[str, Any] = {}
        context_values = {
            name: getattr(eligibility_context, name, None)
            for name in (
                "effective_date",
                "effective_at",
                "session_date",
                "data_cutoff",
                "data_cutoff_at",
                "query_boundary",
                "market_scope",
                "universe_query_policy",
                "rule_package",
                "rule_package_reference",
                "rule_exception_set",
                "exception_set_reference",
                "qualification_policy_version",
                "qualification_policy",
                "frozen_calendar_ids",
                "resolved_calendar_ids",
                "scope_mode",
                "required_capabilities",
                "requested_window",
                "universe_scope_snapshot_hash",
                "fixed_authorized_instrument_ids",
                "provider_capability_summary",
            )
        }
        value_by_name = {
            "candidate": candidate,
            "instrument": candidate,
            "instrument_id": instrument_id,
            "context": eligibility_context,
            "eligibility_context": eligibility_context,
            **context_values,
        }
        for parameter in parameters:
            if parameter.kind is Parameter.VAR_POSITIONAL:
                continue
            if parameter.kind is Parameter.VAR_KEYWORD:
                continue
            if parameter.name in value_by_name:
                value = value_by_name[parameter.name]
            elif parameter.default is not Parameter.empty:
                continue
            else:
                # The documented positional form is ``(candidate, context)``.
                value = candidate if len(positional) == 0 else eligibility_context
            if parameter.kind is Parameter.POSITIONAL_ONLY:
                positional.append(value)
            elif parameter.kind is Parameter.POSITIONAL_OR_KEYWORD:
                # Positional invocation keeps compatibility with simple
                # fake ports while named invocation preserves unusual names.
                positional.append(value)
            elif parameter.kind is Parameter.KEYWORD_ONLY:
                kwargs[parameter.name] = value
        return evaluator(*positional, **kwargs)

    @staticmethod
    def _prepare_evaluator_candidate(
        candidate: Any | None,
        evaluator: Any,
        *,
        calendar_id: str | None = None,
    ) -> Any | None:
        """Adapt a strategy DTO without importing hidden provider evidence.

        ``InstrumentCandidateDTO`` deliberately hides the provider's full
        ``InstrumentSpec`` from strategy code.  When the canonical pure
        evaluator receives a DTO-only fallback, preserve only its six public
        display fields and the explicitly supplied calendar.  The resulting
        ``CandidateInput`` has empty qualification evidence, so required
        checks fail closed instead of treating strategy-visible identity as
        proof of provider facts.
        """

        if candidate is None:
            return None
        if getattr(evaluator, "__module__", "") != "app.backtesting.data.universe":
            return candidate
        if not isinstance(candidate, InstrumentCandidateDTO):
            return candidate
        try:
            from app.backtesting.data.universe import CandidateInput
        except (ImportError, AttributeError):
            return candidate
        kwargs: dict[str, Any] = {
            "instrument_id": candidate.instrument_id,
            "trading_code": candidate.trading_code,
            "name": candidate.name,
            "display_name": candidate.display_name,
            "asset_class": candidate.asset_class,
            "exchange": candidate.exchange,
        }
        if calendar_id is not None:
            kwargs["calendar_id"] = calendar_id
        try:
            return CandidateInput(**kwargs)
        except Exception:
            # A malformed adapter value is reported by the evaluator/provider
            # contract, never repaired with a guessed default.
            return candidate

    @staticmethod
    def _eligibility_payload(result: Any) -> tuple[bool | None, dict[str, Any]]:
        """Normalize a CandidateEligibility-like result for audit and branching."""

        if isinstance(result, bool):
            return result, {"eligible": result}
        if isinstance(result, Mapping):
            payload = dict(result)
            eligible = payload.get("eligible")
            return (
                eligible if isinstance(eligible, bool) else None,
                _json_safe_runtime_value(payload),
            )
        if result is None:
            return None, {}
        payload: dict[str, Any] = {}
        for name in (
            "instrument_id",
            "eligible",
            "reason_codes",
            "calendar_id",
            "identity_evidence",
            "mapping_evidence",
            "rule_evidence",
            "capability_evidence",
            "provenance",
            "settlement_evidence",
            "exception_evidence",
            "scope_evidence",
            "market_scope_evidence",
            "market_data_evidence",
            "corporate_action_evidence",
            "quantity_action_coverage_evidence",
            "status_evidence",
            "evidence_summary",
            "coverage_reports",
            "coverage_qualification",
            "coverage_result",
            "qualification_hash",
            "resolution_hash",
            "request",
        ):
            value = getattr(result, name, None)
            if value is not None:
                payload[name] = value
        to_payload = getattr(result, "as_dict", None)
        if callable(to_payload):
            try:
                rendered = to_payload()
            except Exception:
                rendered = None
            if isinstance(rendered, Mapping):
                payload.update(rendered)
        to_payload = getattr(result, "to_payload", None)
        if callable(to_payload):
            try:
                rendered = to_payload()
            except Exception:
                rendered = None
            if isinstance(rendered, Mapping):
                payload.update(rendered)
        eligible = payload.get("eligible")
        return (
            eligible if isinstance(eligible, bool) else None,
            _json_safe_runtime_value(payload),
        )

    @classmethod
    def _candidate_calendar_id(
        cls,
        candidate: Any | None,
        eligibility_payload: Mapping[str, Any],
        facts: Mapping[UUID, InstrumentFacts],
        instrument_id: UUID,
    ) -> str | None:
        """Read an explicit candidate calendar, never derive one implicitly."""

        value = getattr(candidate, "calendar_id", None) if candidate is not None else None
        if value is None and candidate is not None:
            metadata = getattr(candidate, "metadata", None)
            if isinstance(metadata, Mapping):
                value = metadata.get("calendar_id")
        if value is None:
            value = eligibility_payload.get("calendar_id")
        if value is None:
            fact = facts.get(instrument_id)
            value = fact.calendar_id if fact is not None else None
        if not isinstance(value, str) or not value.strip():
            return None
        return cls._canonical_calendar_id(value)

    @staticmethod
    def _invoke_candidate_resolver(
        resolver: Any,
        *,
        instrument_id: UUID,
        context: PhaseContext,
        eligibility_context: Any,
    ) -> Any:
        """Invoke an engine-side candidate resolver using its declared port."""

        try:
            parameters = list(signature(resolver).parameters.values())
        except (TypeError, ValueError):
            return resolver(instrument_id, eligibility_context)
        values = {
            "instrument_id": instrument_id,
            "candidate_id": instrument_id,
            "id": instrument_id,
            "effective_date": context.session_date,
            "session_date": context.session_date,
            "decision_date": context.session_date,
            "effective_at": context.decision_time,
            "decision_time": context.decision_time,
            "data_cutoff": getattr(eligibility_context, "data_cutoff", None)
            or context.decision_time,
            "data_cutoff_at": getattr(eligibility_context, "data_cutoff", None)
            or context.decision_time,
            "context": eligibility_context,
            "eligibility_context": eligibility_context,
        }
        positional: list[Any] = []
        kwargs: dict[str, Any] = {}
        for parameter in parameters:
            if parameter.kind is Parameter.VAR_POSITIONAL:
                continue
            if parameter.kind is Parameter.VAR_KEYWORD:
                continue
            if parameter.name in values:
                value = values[parameter.name]
            elif parameter.default is not Parameter.empty:
                continue
            else:
                # The documented resolver form is ``(instrument_id,
                # context)``.  Keep this compatibility fallback explicit so
                # a provider's own body TypeError is never hidden by retries.
                value = instrument_id if not positional else eligibility_context
            if parameter.kind is Parameter.POSITIONAL_ONLY:
                positional.append(value)
            elif parameter.kind is Parameter.POSITIONAL_OR_KEYWORD:
                positional.append(value)
            elif parameter.kind is Parameter.KEYWORD_ONLY:
                kwargs[parameter.name] = value
        return resolver(*positional, **kwargs)

    def _resolve_final_candidate(
        self,
        *,
        candidate: Any | None,
        instrument_id: UUID,
        context: PhaseContext,
        bound: _StepBoundUniverse,
        evaluator: Any,
        eligibility_context: Any,
    ) -> Any | None:
        """Resolve the complete engine-side candidate for final checking.

        The strategy DTO is a projection and must never become the final
        evaluator's source of truth.  Provider ports may resolve a fresh spec
        at the current PIT coordinates; otherwise an already complete row
        from the engine snapshot is acceptable.  A DTO-only path returns
        ``None`` and is handled as a fail-closed contract violation by the
        caller.
        """

        sources: list[Any] = [
            evaluator,
            self._view_factory,
            bound.source,
            self._source_universe_query(bound),
            getattr(self._view_factory, "_engine_market_data", None),
            self._universe_scope_resolution,
        ]
        resolver_names = (
            "resolve_full_candidate",
            "resolve_candidate",
            "candidate_for",
            "get_candidate",
            "resolve_spec",
            "spec_for",
            "get_spec",
            "resolve_instrument_spec",
            "instrument_spec_for",
        )
        seen: set[int] = set()
        for source in sources:
            if source is None or id(source) in seen:
                continue
            seen.add(id(source))
            for name in resolver_names:
                resolver = getattr(source, name, None)
                if not callable(resolver):
                    continue
                try:
                    resolved = self._invoke_candidate_resolver(
                        resolver,
                        instrument_id=instrument_id,
                        context=context,
                        eligibility_context=eligibility_context,
                    )
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    if code in {
                        "universe_calendar_not_preflighted",
                        "universe_pit_boundary_violation",
                        "universe_preflight_hash_mismatch",
                        "universe_capability_missing",
                        "universe_provider_contract_violation",
                    }:
                        raise
                    raise _universe_provider_error(
                        "engine candidate resolver failed during final qualification",
                        details={
                            "resolver": name,
                            "error_type": type(exc).__name__,
                        },
                    ) from exc
                if resolved is not None:
                    return resolved
            # ChunkStrategyDataView keeps the complete provider rows behind
            # its strategy projection.  Resolve one requested spec through
            # the chunk's instrument port, which is bounded by the current
            # step authorization and never exposes the chunk to the strategy.
            view = getattr(source, "_ChunkUniverseQuery__view", None)
            chunk = getattr(view, "_ChunkStrategyDataView__chunk", None)
            if chunk is not None and isinstance(candidate, InstrumentCandidateDTO):
                try:
                    from app.backtesting.data.requests import (
                        InstrumentQuery,
                        QueryBoundary,
                    )

                    cutoff = getattr(
                        eligibility_context,
                        "data_cutoff",
                        None,
                    ) or context.decision_time
                    resolved_rows = chunk.instruments(
                        InstrumentQuery(
                            instrument_ids=(instrument_id,),
                            effective_at=context.decision_time,
                            boundary=QueryBoundary(
                                data_cutoff=cutoff,
                                include_cutoff_day=True,
                            ),
                        )
                    )
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    if code in {
                        "universe_target_outside_scope",
                        "universe_pit_boundary_violation",
                        "universe_provider_contract_violation",
                    }:
                        raise
                    raise _universe_provider_error(
                        "engine candidate lookup failed during final qualification",
                        details={"error_type": type(exc).__name__},
                    ) from exc
                for resolved in tuple(resolved_rows or ()):
                    resolved_id = getattr(resolved, "instrument_id", None)
                    if resolved_id == instrument_id:
                        return resolved
        # A provider may already have supplied a complete candidate row in the
        # engine snapshot.  A strategy-facing DTO is explicitly excluded.
        if candidate is None or isinstance(candidate, InstrumentCandidateDTO):
            return None
        return candidate

    @staticmethod
    def _candidate_evidence_value(
        candidate: Any | None,
        payload: Mapping[str, Any],
        name: str,
    ) -> Any:
        """Read one evidence section from result, nested summary, or spec."""

        value = payload.get(name)
        if value is not None:
            return value
        summary = payload.get("evidence_summary")
        if isinstance(summary, Mapping):
            value = summary.get(name)
            if value is not None:
                return value
        for source in (
            candidate,
            getattr(candidate, "spec", None) if candidate is not None else None,
        ):
            if source is None:
                continue
            value = getattr(source, name, None)
            if value is not None:
                return value
            metadata = getattr(source, "metadata", None)
            if isinstance(metadata, Mapping) and name in metadata:
                return metadata[name]
        return None

    @staticmethod
    def _evidence_present(value: Any) -> bool:
        """Return whether a qualification evidence section is non-empty."""

        if value is None:
            return False
        if isinstance(value, Mapping):
            return bool(value)
        if isinstance(value, (str, bytes, list, tuple, set, frozenset)):
            return bool(value)
        return True

    def _missing_final_evidence(
        self,
        *,
        candidate: Any | None,
        payload: Mapping[str, Any],
        instrument_id: UUID,
        context: PhaseContext,
    ) -> tuple[str, ...]:
        """List mandatory evidence sections absent from a formal result."""

        required = [
            "identity_evidence",
            "mapping_evidence",
            "rule_evidence",
            "market_data_evidence",
            "corporate_action_evidence",
            "quantity_action_coverage_evidence",
        ]
        required_status = False
        # Status is required when the run explicitly declares that dimension;
        # full rule policies may also carry a required declaration.
        mode_source = self._universe_scope_resolution
        bound = self._step_universes.get(context.step_sequence)
        source = self._source_universe_query(bound) if bound is not None else None
        required_caps = self._resolve_attr(
            source, ("required_capabilities",)
        ) or self._resolve_attr(
            mode_source, ("required_capabilities",)
        ) or ()
        required_status = any(
            getattr(item, "value", item) == "status" for item in required_caps
        )
        if not required_status:
            spec = getattr(candidate, "spec", None) or candidate
            policy = getattr(spec, "trading_status_policy", None)
            if isinstance(policy, Mapping):
                required_status = any(
                    str(getattr(value, "value", value)).lower() == "required"
                    for value in policy.values()
                )
        if required_status:
            required.append("status_evidence")
        return tuple(
            name
            for name in required
            if not self._evidence_present(
                # Formal final qualification must be proven by the current
                # port result.  Candidate source rows are inputs to that port,
                # not a substitute when the result omits an evidence section.
                self._candidate_evidence_value(None, payload, name)
            )
        )

    def _final_candidate_pit_issues(
        self,
        candidate: Any | None,
        payload: Mapping[str, Any],
        *,
        context: PhaseContext,
    ) -> tuple[str, str, Any, Any] | None:
        """Validate explicit candidate PIT coordinates at the final gate."""

        if candidate is None:
            return None
        spec = getattr(candidate, "spec", None)
        sources = (candidate, spec)

        def read(name: str) -> Any:
            value = payload.get(name)
            if value is not None:
                return value
            summary = payload.get("evidence_summary")
            if isinstance(summary, Mapping) and summary.get(name) is not None:
                return summary.get(name)
            for source in sources:
                if source is None:
                    continue
                value = getattr(source, name, None)
                if value is not None:
                    return value
                metadata = getattr(source, "metadata", None)
                if isinstance(metadata, Mapping) and metadata.get(name) is not None:
                    return metadata.get(name)
            return None

        effective = read("effective_date")
        if effective is not None:
            if isinstance(effective, datetime):
                effective = effective.date()
            elif isinstance(effective, str):
                try:
                    effective = date.fromisoformat(effective[:10])
                except ValueError:
                    return (
                        "universe_pit_boundary_violation",
                        "effective_date",
                        context.session_date,
                        effective,
                    )
            if effective != context.session_date:
                return (
                    "universe_pit_boundary_violation",
                    "effective_date",
                    context.session_date,
                    effective,
                )
        known_at = read("known_at")
        if known_at is not None:
            if isinstance(known_at, str):
                try:
                    known_at = datetime.fromisoformat(known_at)
                except ValueError:
                    return (
                        "universe_pit_boundary_violation",
                        "known_at",
                        "aware datetime <= data_cutoff",
                        known_at,
                    )
            if (
                not isinstance(known_at, datetime)
                or known_at.tzinfo is None
                or known_at.utcoffset() is None
            ):
                return (
                    "universe_pit_boundary_violation",
                    "known_at",
                    "aware datetime <= data_cutoff",
                    known_at,
                )
            cutoff = self._step_decision_data_cutoffs.get(
                context.step_sequence,
                context.data_cutoff or context.decision_time,
            )
            if known_at > cutoff:
                return (
                    "universe_pit_boundary_violation",
                    "known_at",
                    cutoff,
                    known_at,
                )
        valid_from = read("valid_from")
        valid_to = read("valid_to")
        if isinstance(valid_from, datetime):
            valid_from = valid_from.date()
        if isinstance(valid_to, datetime):
            valid_to = valid_to.date()
        if isinstance(valid_from, date) and context.session_date < valid_from:
            return (
                "universe_pit_boundary_violation",
                "identity_validity_range",
                f"<= {context.session_date.isoformat()}",
                valid_from,
            )
        if isinstance(valid_to, date) and context.session_date >= valid_to:
            return (
                "universe_pit_boundary_violation",
                "identity_validity_range",
                f"< {valid_to.isoformat()}",
                context.session_date,
            )
        return None

    def _final_candidate_scope_rule_issue(
        self,
        candidate: Any | None,
        *,
        instrument_id: UUID,
        bound: _StepBoundUniverse,
    ) -> tuple[str, str, Any, Any] | None:
        """Validate explicit range, rule, exception, and settlement facts."""

        if candidate is None:
            return None
        spec = getattr(candidate, "spec", None) or candidate
        candidate_id = getattr(candidate, "instrument_id", None) or getattr(
            spec, "instrument_id", None
        )
        if candidate_id != instrument_id:
            return (
                "universe_provider_contract_violation",
                "candidate_identity",
                str(instrument_id),
                candidate_id,
            )
        source = self._source_universe_query(bound)
        scope = getattr(source, "market_scope", None) or getattr(
            self._universe_scope_resolution, "market_scope", None
        )
        for field_name, scope_name in (
            ("market", "markets"),
            ("asset_class", "asset_classes"),
            ("exchange", "exchanges"),
            ("currency", "currencies"),
        ):
            allowed = tuple(getattr(scope, scope_name, ()) or ()) if scope else ()
            value = getattr(candidate, field_name, None)
            if value is None:
                value = getattr(spec, field_name, None)
            if allowed and (not isinstance(value, str) or value not in allowed):
                return (
                    "universe_selected_ineligible",
                    "market_scope",
                    allowed,
                    value,
                )
        settlement = getattr(candidate, "settlement_rule_class", None)
        if settlement is None:
            settlement = getattr(spec, "settlement_rule_class", None)
        if not isinstance(settlement, str) or not settlement.strip():
            return (
                "universe_selected_ineligible",
                "settlement_rule_class",
                "explicit settlement rule class",
                settlement,
            )
        def reference_token(value: Any) -> str:
            if isinstance(value, str):
                return value.strip()
            return f"{getattr(value, 'key', None)}@{getattr(value, 'version', None)}"

        expected_rule = getattr(source, "rule", None) or getattr(
            source, "rule_package_reference", None
        )
        actual_rule = getattr(candidate, "rule_package_reference", None) or getattr(
            spec, "rule_package_reference", None
        )
        if expected_rule is not None and actual_rule is None:
            return (
                "universe_selected_ineligible",
                "rule_package",
                reference_token(expected_rule),
                None,
            )
        if expected_rule is not None and actual_rule is not None:
            expected_token = reference_token(expected_rule)
            actual_token = reference_token(actual_rule)
            if expected_token != actual_token:
                return (
                    "universe_selected_ineligible",
                    "rule_package",
                    expected_token,
                    actual_token,
                )
        expected_exception = getattr(source, "rule_exception_set", None)
        actual_exception = getattr(candidate, "rule_exception_reference", None) or getattr(
            spec, "rule_exception_reference", None
        )
        if expected_exception is not None and actual_exception is not None:
            expected_token = reference_token(expected_exception)
            actual_token = reference_token(actual_exception)
            if expected_token != actual_token:
                return (
                    "universe_selected_ineligible",
                    "rule_exception_set",
                    expected_token,
                    actual_token,
                )
        return None

    def _final_validate_targets(
        self,
        target_ids: Sequence[UUID],
        *,
        context: PhaseContext,
        decision: Any,
    ) -> None:
        """Recheck every selected target before the first order is constructed."""

        bound = self._step_universes.get(context.step_sequence)
        if bound is None:
            raise _universe_provider_error(
                "submit phase has no candidate snapshot for this decision step"
            )
        current_candidates = {
            getattr(candidate, "instrument_id"): candidate
            for candidate in bound.candidates
        }
        fixed_ids = self._fixed_authorized_ids()
        formal_dynamic = self._is_formal_dynamic_scope(bound=bound)
        queried_ids = self._step_queried_candidate_ids.get(
            context.step_sequence, frozenset()
        )
        # In formal dynamic/hybrid mode, ``current_candidates`` is only the
        # engine-side snapshot used to serve the strategy view.  Permission
        # comes from the IDs that the strategy actually obtained through its
        # current-step query.  Fixed identities remain independently
        # authorized for holdings/static obligations.
        dynamic_allowed_ids = (
            queried_ids if formal_dynamic else set(current_candidates)
        )
        allowed_ids = fixed_ids | set(dynamic_allowed_ids)
        evaluator = self._step_qualification_ports.get(context.step_sequence)
        if evaluator is None:
            evaluator = self._candidate_evaluator(
                allow_pure_fallback=not formal_dynamic
            )
        if formal_dynamic and evaluator is None:
            self._raise_final_qualification_error(
                "universe_capability_missing",
                instrument_id=UUID(int=0),
                context=context,
                decision=decision,
                candidate=None,
                failed_check="candidate_qualification_port",
                reason_codes=("candidate_qualification_port_missing",),
                expected="provider qualification port",
                actual=None,
            )
        allowed_calendars = set(self._frozen_universe_calendar_ids)
        source = self._source_universe_query(bound)
        if not allowed_calendars:
            allowed_calendars.update(
                self._canonical_calendar_id(value)
                for value in tuple(getattr(source, "allowed_calendar_ids", ()) or ())
                if isinstance(value, str) and value.strip()
            )
        if not allowed_calendars:
            allowed_calendars.update(
                self._canonical_calendar_id(value)
                for value in tuple(
                    getattr(self._universe_scope_resolution, "resolved_calendar_ids", ())
                    or ()
                )
                if isinstance(value, str) and value.strip()
            )
        # Reject unauthorized targets before touching engine market data.  A
        # strategy must first earn dynamic permission through its own query;
        # this keeps even engine-side refreshes outside that permission set.
        for instrument_id in sorted(set(target_ids), key=str):
            if (
                formal_dynamic
                and instrument_id not in fixed_ids
                and instrument_id not in queried_ids
            ):
                self._raise_final_qualification_error(
                    "universe_target_outside_scope",
                    instrument_id=instrument_id,
                    context=context,
                    decision=decision,
                    candidate=current_candidates.get(instrument_id),
                    failed_check="strategy_universe_query",
                    reason_codes=("universe_target_outside_scope",),
                    expected="instrument_id_returned_by_current_strategy_universe_query",
                    actual=str(instrument_id),
                    calendar_id=self._candidate_calendar_id(
                        current_candidates.get(instrument_id),
                        {},
                        self._instrument_facts,
                        instrument_id,
                    ),
                )
            if instrument_id not in allowed_ids:
                self._raise_final_qualification_error(
                    "universe_target_outside_scope",
                    instrument_id=instrument_id,
                    context=context,
                    decision=decision,
                    candidate=current_candidates.get(instrument_id),
                    failed_check="scope",
                    reason_codes=("universe_target_outside_scope",),
                    expected="fixed_or_current_dynamic_scope",
                    actual=str(instrument_id),
                )
        self._refresh_target_market_data(tuple(target_ids), context)
        for instrument_id in sorted(set(target_ids), key=str):
            candidate = current_candidates.get(instrument_id)
            candidate_calendar_id = self._candidate_calendar_id(
                candidate,
                {},
                self._instrument_facts,
                instrument_id,
            )
            # A target returned by the strategy query must have an engine-side
            # candidate row.  Never pass a DTO or fabricate a placeholder to
            # the final qualification port.
            if formal_dynamic and candidate is None and instrument_id not in fixed_ids:
                self._raise_final_qualification_error(
                    "universe_provider_contract_violation",
                    instrument_id=instrument_id,
                    context=context,
                    decision=decision,
                    candidate=None,
                    failed_check="engine_candidate_snapshot",
                    reason_codes=("universe_provider_contract_violation",),
                    expected="complete engine-side candidate",
                    actual=None,
                )
            eligibility_payload: dict[str, Any] = {}
            eligible = True
            eligibility_context = self._candidate_context(
                candidate=candidate,
                instrument_id=instrument_id,
                context=context,
                bound=bound,
            )
            final_candidate = self._resolve_final_candidate(
                candidate=candidate,
                instrument_id=instrument_id,
                context=context,
                bound=bound,
                evaluator=evaluator,
                eligibility_context=eligibility_context,
            )
            if formal_dynamic and final_candidate is None:
                self._raise_final_qualification_error(
                    "universe_provider_contract_violation",
                    instrument_id=instrument_id,
                    context=context,
                    decision=decision,
                    candidate=candidate,
                    failed_check="engine_candidate_qualification_input",
                    reason_codes=("universe_provider_contract_violation",),
                    expected="complete engine-side candidate",
                    actual={
                        "candidate_type": type(candidate).__name__
                        if candidate is not None
                        else None
                    },
                    calendar_id=candidate_calendar_id,
                )
            if formal_dynamic:
                scope_rule_issue = self._final_candidate_scope_rule_issue(
                    final_candidate,
                    instrument_id=instrument_id,
                    bound=bound,
                )
                if scope_rule_issue is not None:
                    (
                        scope_rule_code,
                        scope_rule_check,
                        scope_rule_expected,
                        scope_rule_actual,
                    ) = scope_rule_issue
                    self._raise_final_qualification_error(
                        scope_rule_code,
                        instrument_id=instrument_id,
                        context=context,
                        decision=decision,
                        candidate=final_candidate,
                        failed_check=scope_rule_check,
                        reason_codes=(scope_rule_code,),
                        expected=scope_rule_expected,
                        actual=scope_rule_actual,
                        calendar_id=candidate_calendar_id,
                    )
            pit_issue = self._final_candidate_pit_issues(
                final_candidate,
                {},
                context=context,
            )
            if pit_issue is not None:
                pit_code, pit_check, pit_expected, pit_actual = pit_issue
                self._raise_final_qualification_error(
                    pit_code,
                    instrument_id=instrument_id,
                    context=context,
                    decision=decision,
                    candidate=final_candidate,
                    failed_check=pit_check,
                    reason_codes=(pit_code,),
                    expected=pit_expected,
                    actual=pit_actual,
                    calendar_id=candidate_calendar_id,
                )
            if evaluator is not None:
                evaluator_candidate = (
                    final_candidate if final_candidate is not None else candidate
                )
                try:
                    eligibility = self._invoke_candidate_evaluator(
                        evaluator,
                        candidate=self._prepare_evaluator_candidate(
                            evaluator_candidate,
                            evaluator,
                            calendar_id=self._candidate_calendar_id(
                                evaluator_candidate,
                                {},
                                self._instrument_facts,
                                instrument_id,
                            ),
                        ),
                        instrument_id=instrument_id,
                        eligibility_context=eligibility_context,
                    )
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    if code in {
                        "universe_calendar_not_preflighted",
                        "universe_pit_boundary_violation",
                        "universe_target_outside_scope",
                        "universe_preflight_hash_mismatch",
                        "universe_selected_ineligible",
                        "universe_provider_contract_violation",
                        "universe_capability_missing",
                    }:
                        source_details = getattr(exc, "details", {})
                        source_details = (
                            dict(source_details)
                            if isinstance(source_details, Mapping)
                            else {}
                        )
                        source_reasons = source_details.get("reason_codes", ())
                        if isinstance(source_reasons, str):
                            source_reasons = (source_reasons,)
                        if not isinstance(source_reasons, (list, tuple)):
                            source_reasons = (code,)
                        self._raise_final_qualification_error(
                            code,
                            instrument_id=instrument_id,
                            context=context,
                            decision=decision,
                            candidate=candidate,
                            failed_check=str(
                                source_details.get("failed_check") or code
                            ),
                            reason_codes=tuple(
                                str(item) for item in source_reasons
                            ),
                            expected=source_details.get("expected", True),
                            actual=source_details.get(
                                "actual", {"error_type": type(exc).__name__}
                            ),
                            calendar_id=source_details.get(
                                "calendar_id", candidate_calendar_id
                            ),
                        )
                    self._raise_final_qualification_error(
                        "universe_selected_ineligible",
                        instrument_id=instrument_id,
                        context=context,
                        decision=decision,
                        candidate=candidate,
                        failed_check="candidate_qualification",
                        reason_codes=(code or type(exc).__name__,),
                        expected=True,
                        actual={
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        calendar_id=candidate_calendar_id,
                    )
                eligible, eligibility_payload = self._eligibility_payload(eligibility)
                candidate_calendar_id = self._candidate_calendar_id(
                    final_candidate,
                    eligibility_payload,
                    self._instrument_facts,
                    instrument_id,
                )
                pit_issue = self._final_candidate_pit_issues(
                    final_candidate,
                    eligibility_payload,
                    context=context,
                )
                if pit_issue is not None:
                    pit_code, pit_check, pit_expected, pit_actual = pit_issue
                    self._raise_final_qualification_error(
                        pit_code,
                        instrument_id=instrument_id,
                        context=context,
                        decision=decision,
                        candidate=final_candidate,
                        failed_check=pit_check,
                        reason_codes=(pit_code,),
                        expected=pit_expected,
                        actual=pit_actual,
                        calendar_id=candidate_calendar_id,
                    )
                reported_id = eligibility_payload.get("instrument_id")
                if reported_id is not None:
                    try:
                        reported_id = UUID(str(reported_id))
                    except (TypeError, ValueError):
                        reported_id = None
                    if reported_id != instrument_id:
                        self._raise_final_qualification_error(
                            "universe_provider_contract_violation",
                            instrument_id=instrument_id,
                            context=context,
                            decision=decision,
                            candidate=final_candidate,
                            failed_check="candidate_identity",
                            reason_codes=("universe_provider_contract_violation",),
                            expected=str(instrument_id),
                            actual=eligibility_payload.get("instrument_id"),
                            calendar_id=candidate_calendar_id,
                        )
                if eligible is None:
                    self._raise_final_qualification_error(
                        "universe_provider_contract_violation",
                        instrument_id=instrument_id,
                        context=context,
                        decision=decision,
                        candidate=candidate,
                        failed_check="candidate_qualification_result",
                        reason_codes=("universe_provider_contract_violation",),
                        expected="CandidateEligibility with boolean eligible",
                        actual=eligibility_payload,
                        calendar_id=candidate_calendar_id,
                    )
                if formal_dynamic and eligible:
                    missing_evidence = self._missing_final_evidence(
                        candidate=final_candidate,
                        payload=eligibility_payload,
                        instrument_id=instrument_id,
                        context=context,
                    )
                    if missing_evidence:
                        self._raise_final_qualification_error(
                            "universe_selected_ineligible",
                            instrument_id=instrument_id,
                            context=context,
                            decision=decision,
                            candidate=final_candidate,
                            failed_check="qualification_evidence",
                            reason_codes=(
                                "candidate_qualification_evidence_missing",
                            ),
                            expected={"required_evidence": missing_evidence},
                            actual={
                                "available_evidence": sorted(
                                    name
                                    for name in (
                                        "identity_evidence",
                                        "mapping_evidence",
                                        "rule_evidence",
                                        "market_data_evidence",
                                        "corporate_action_evidence",
                                        "quantity_action_coverage_evidence",
                                        "status_evidence",
                                    )
                                    if self._evidence_present(
                                        self._candidate_evidence_value(
                                            final_candidate,
                                            eligibility_payload,
                                            name,
                                        )
                                    )
                                )
                            },
                            calendar_id=candidate_calendar_id,
                        )
                    market_evidence = self._final_market_data_evidence.get(
                        instrument_id, {}
                    )
                    if not bool(market_evidence.get("complete")):
                        self._raise_final_qualification_error(
                            "universe_selected_ineligible",
                            instrument_id=instrument_id,
                            context=context,
                            decision=decision,
                            candidate=final_candidate,
                            failed_check="market_data_qualification",
                            reason_codes=("candidate_market_data_incomplete",),
                            expected="current-session quote and instrument facts",
                            actual=market_evidence,
                            calendar_id=candidate_calendar_id,
                        )
                if not eligible:
                    reason_codes = eligibility_payload.get("reason_codes", ())
                    if isinstance(reason_codes, str):
                        reason_codes = (reason_codes,)
                    if not isinstance(reason_codes, (list, tuple)):
                        reason_codes = ("candidate_ineligible",)
                    reason_codes = tuple(str(item) for item in reason_codes)
                    reason_to_error_code = {
                        "universe_calendar_not_preflighted": (
                            "universe_calendar_not_preflighted"
                        ),
                        "universe_pit_boundary_violation": (
                            "universe_pit_boundary_violation"
                        ),
                        "universe_target_outside_scope": (
                            "universe_target_outside_scope"
                        ),
                        "universe_preflight_hash_mismatch": (
                            "universe_preflight_hash_mismatch"
                        ),
                    }
                    qualification_code = next(
                        (
                            reason_to_error_code[reason]
                            for reason in reason_codes
                            if reason in reason_to_error_code
                        ),
                        "universe_selected_ineligible",
                    )
                    self._raise_final_qualification_error(
                        qualification_code,
                        instrument_id=instrument_id,
                        context=context,
                        decision=decision,
                        candidate=candidate,
                        failed_check="candidate_qualification",
                        reason_codes=reason_codes,
                        expected=True,
                        actual=eligibility_payload,
                        calendar_id=candidate_calendar_id,
                    )
            else:
                # Some provider ports return a pre-evaluated immutable
                # CandidateEligibility row alongside the strategy DTO.  It
                # is already the canonical result, so honour an explicit
                # negative flag even when no separate callable was injected.
                explicit_eligible = getattr(candidate, "eligible", None)
                if explicit_eligible is False:
                    reason_codes = getattr(candidate, "reason_codes", ()) or ()
                    if isinstance(reason_codes, str):
                        reason_codes = (reason_codes,)
                    self._raise_final_qualification_error(
                        "universe_selected_ineligible",
                        instrument_id=instrument_id,
                        context=context,
                        decision=decision,
                        candidate=candidate,
                        failed_check="candidate_qualification",
                        reason_codes=tuple(str(item) for item in reason_codes)
                        or ("candidate_ineligible",),
                        expected=True,
                        actual={
                            "eligible": False,
                            "reason_codes": tuple(reason_codes),
                        },
                        calendar_id=candidate_calendar_id,
                    )
            calendar_id = self._candidate_calendar_id(
                final_candidate,
                eligibility_payload,
                self._instrument_facts,
                instrument_id,
            )
            # A calendar check is mandatory whenever a formal candidate port
            # or frozen scope is present.  Legacy static fixtures have neither
            # and continue to use their existing InstrumentFacts path.
            if (evaluator is not None or allowed_calendars) and (
                calendar_id is None or calendar_id not in allowed_calendars
            ):
                self._raise_final_qualification_error(
                    "universe_calendar_not_preflighted",
                    instrument_id=instrument_id,
                    context=context,
                    decision=decision,
                    candidate=candidate,
                    failed_check="calendar_permission",
                    reason_codes=("universe_calendar_not_preflighted",),
                    expected=tuple(sorted(allowed_calendars)),
                    actual=calendar_id,
                    calendar_id=calendar_id,
                )
            self._final_qualification_results.append(
                _freeze_runtime_evidence(
                    {
                        "instrument_id": str(instrument_id),
                        "session_date": context.session_date.isoformat(),
                        "decision_time": context.decision_time.isoformat(),
                        "data_cutoff": (
                            self._last_decision_data_cutoff or context.decision_time
                        ).isoformat(),
                        "calendar_id": calendar_id,
                        "eligible": True,
                        "reason_codes": tuple(
                            eligibility_payload.get("reason_codes", ())
                            if isinstance(eligibility_payload, Mapping)
                            else ()
                        ),
                        "evidence_summary": {
                            **dict(eligibility_payload),
                            "engine_market_data_evidence": self._final_market_data_evidence.get(
                                instrument_id, {}
                            ),
                        },
                    }
                )
            )

    def _raise_final_qualification_error(
        self,
        code: str,
        *,
        instrument_id: UUID,
        context: PhaseContext,
        decision: Any,
        candidate: Any | None,
        failed_check: str,
        reason_codes: Sequence[str],
        expected: Any,
        actual: Any,
        calendar_id: str | None = None,
    ) -> None:
        """Raise one stable error carrying the complete FINAL-07 evidence."""

        del candidate  # The candidate object must never enter persisted details.
        from app.backtesting.data.errors import (
            UniverseCapabilityMissingError,
            UniverseCalendarNotPreflightedError,
            UniversePreflightHashMismatchError,
            UniversePitBoundaryViolationError,
            UniverseProviderContractViolationError,
            UniverseSelectedIneligibleError,
            UniverseTargetOutsideScopeError,
        )

        data_cutoff = self._step_decision_data_cutoffs.get(
            context.step_sequence,
            self._last_decision_data_cutoff or context.decision_time,
        )
        details = {
            "instrument_id": str(instrument_id),
            "session_date": context.session_date.isoformat(),
            "decision_time": context.decision_time.isoformat(),
            "data_cutoff": data_cutoff.isoformat(),
            "calendar_id": calendar_id,
            "failed_check": failed_check,
            "reason_codes": list(reason_codes),
            "expected": expected,
            "actual": actual,
            "evidence_summary": {
                "scope_snapshot_hash": self._universe_scope_snapshot_hash,
                "qualification_policy_version": self._universe_eligibility_policy_version,
                "candidate_count": len(self._step_candidates.get(context.step_sequence, ())),
            },
            "decision_id": getattr(decision, "decision_id", None),
        }
        safe_details = _json_safe_runtime_value(details)
        self._last_final_qualification_failure = _freeze_runtime_evidence(
            safe_details
        )
        # Keep the failed target in the in-memory result audit even though no
        # successful ``BacktestRunResult`` is returned after a phase failure.
        # Failure finalizers can consume this immutable evidence without
        # re-running the provider or interpreting the decision a second time.
        self._final_qualification_results.append(
            _freeze_runtime_evidence(
                {
                    **dict(safe_details),
                    "eligible": False,
                }
            )
        )
        message = (
            "策略选中标的未通过订单创建前的 PIT 候选资格复检，"
            f"instrument_id={instrument_id}，failed_check={failed_check}"
        )
        error_types = {
            "universe_capability_missing": UniverseCapabilityMissingError,
            "universe_calendar_not_preflighted": UniverseCalendarNotPreflightedError,
            "universe_preflight_hash_mismatch": UniversePreflightHashMismatchError,
            "universe_pit_boundary_violation": UniversePitBoundaryViolationError,
            "universe_provider_contract_violation": UniverseProviderContractViolationError,
            "universe_target_outside_scope": UniverseTargetOutsideScopeError,
            "universe_selected_ineligible": UniverseSelectedIneligibleError,
        }
        error_cls = error_types.get(code, UniverseSelectedIneligibleError)
        raise error_cls(message, details=safe_details)

    def execution_policy_for(
        self, instrument_id: UUID, effective_at: date | datetime
    ) -> Any:
        """Resolve a runtime execution policy from the frozen snapshot only."""

        bundle = self._rule_snapshot_bundle
        if bundle is None:
            raise DomainValidationError(
                "this runner was not admitted with a rule snapshot bundle"
            )
        bundle.verify_hash()
        effective_date = (
            _aware_datetime(effective_at, "effective_at").date()
            if isinstance(effective_at, datetime)
            else effective_at
        )
        if not isinstance(effective_date, date):
            raise DomainValidationError("effective_at must be a date or datetime")
        cache_key = (instrument_id, effective_date)
        policy = self._rule_policy_cache.get(cache_key)
        if policy is not None:
            return policy
        segment = bundle.segment_for(instrument_id, effective_date)
        from app.backtesting.execution_policy import InstrumentExecutionPolicy

        policy = InstrumentExecutionPolicy.from_rule_snapshot(
            segment,
            package_reference=bundle.rule_package_reference,
        )
        if policy.currency != self._currency:
            raise DomainValidationError(
                f"instrument {instrument_id} rule snapshot currency "
                f"{policy.currency!r} differs from run currency {self._currency!r}"
            )
        self._rule_policy_cache[cache_key] = policy
        self._used_rule_segments[cache_key] = policy.resolution_hash
        return policy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> BacktestRunResult:
        """Run the complete official axis and return the frozen result."""

        return self.run_steps(tuple(self._axis))

    def run_steps(
        self,
        steps: Sequence[TimeStep],
        *,
        next_after_last: TimeStep | None = None,
    ) -> BacktestRunResult:
        """Run the next contiguous slice of the official timeline.

        ``next_after_last`` lets a sliced (chunked) invocation declare the
        true successor of its final step, so chunk boundaries never truncate
        the decide/submit phases.  Chunking changes only data-resource
        lifecycles: sequences, strategy, account, orders, and analyzers are
        never reset between calls.

        The runner accepts only exactly the official steps it expects next:
        every step must be the axis's own step at that sequence (same
        session, timestamps, and metadata -- a forged look-alike is
        rejected), ``next_after_last`` must equal the axis's true successor,
        and starting mid-timeline, replaying a consumed slice, or running
        after completion all fail instead of silently duplicating
        decisions, orders, or events.

        A phase failure leaves already-emitted events in place for audit
        but marks the runner failed: no further slice can be executed on
        the same instance, so partial state can never be extended by a
        retry that would duplicate business effects.  Cross-process retry
        idempotency remains a persistence-layer concern (stable
        event/order identifiers).
        """

        if self._finished:
            raise DomainValidationError(
                "this runner has already consumed the complete official "
                "timeline; create a new run instead of re-executing it"
            )
        if self._failed:
            raise DomainValidationError(
                "this runner failed during an earlier phase and is permanently "
                "stopped; inspect the failure and start a new run instead of "
                "re-executing the failed slice"
            )
        if not _is_step_sequence(steps):
            raise DomainValidationError(
                "steps must be a sequence of TimeStep values"
            )
        try:
            ordered = tuple(steps)
        except Exception as exc:
            # A broken __iter__ can raise anything (TypeError, ValueError,
            # RuntimeError, ...); the runner contract is a stable domain
            # error regardless of the iterator's own failure mode.
            raise DomainValidationError(
                f"steps is not a usable step sequence: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not ordered:
            raise DomainValidationError("run_steps requires at least one step")
        # Type-check every entry before touching any attribute, so malformed
        # input always surfaces as a stable domain error instead of an
        # incidental AttributeError.
        for step in ordered:
            if not isinstance(step, TimeStep):
                raise DomainValidationError("steps entries must be TimeStep")
        if next_after_last is not None and not isinstance(
            next_after_last, TimeStep
        ):
            raise DomainValidationError(
                "next_after_last must be a TimeStep or None"
            )
        if ordered[0].sequence != self._next_expected_step:
            raise DomainValidationError(
                f"expected step {self._next_expected_step} next, but the "
                f"slice starts at {ordered[0].sequence}; runners only accept "
                "the contiguous continuation of their timeline"
            )
        for index, step in enumerate(ordered):
            expected_sequence = self._next_expected_step + index
            if step.sequence != expected_sequence:
                raise DomainValidationError(
                    "steps must continue the timeline without gaps: expected "
                    f"sequence {expected_sequence} at position {index}, got "
                    f"{step.sequence}"
                )
            # Identity, not just numbering: the slice must consist of the
            # axis's own immutable steps, so a forged TimeStep with a valid
            # sequence but altered session data can never execute.
            if step != self._axis.at(expected_sequence):
                raise DomainValidationError(
                    f"step at sequence {expected_sequence} does not match the "
                    "official timeline step; forged or altered steps are "
                    "rejected"
                )
        # The declared successor of the slice's final step must be the
        # axis's true next step (or absent exactly when the slice ends the
        # timeline), otherwise a forged successor could fabricate a
        # non-final day and produce decisions and orders that never
        # officially exist.
        last = ordered[-1]
        true_successor = (
            self._axis.at(last.sequence + 1)
            if last.sequence + 1 < len(self._axis)
            else None
        )
        if next_after_last != true_successor:
            raise DomainValidationError(
                "next_after_last must be the official successor of the "
                f"final step {last.sequence}: expected "
                f"{true_successor.sequence if true_successor is not None else None}, "
                f"got {next_after_last.sequence if next_after_last is not None else None}"
            )
        try:
            self._execute_slice(ordered, next_after_last=next_after_last)
        except Exception:
            # Events already emitted stay on the stream for audit, but the
            # runner is dead: partial state must never be extended by a
            # retry that would duplicate decisions, orders, or cash moves.
            self._failed = True
            raise
        self._next_expected_step = ordered[-1].sequence + 1
        if self._next_expected_step >= len(self._axis):
            self._finished = True
        current_chunk_sequence = self._analysis_chunk_sequence
        self._analysis_chunk_sequence += 1
        # Analysis lifecycle: only a slice that consumed the final official
        # step may finalize; every earlier successful chunk stays partial.
        analysis_status: str | None = None
        analysis_metrics: tuple[Any, ...] = ()
        if self._analysis_engine is not None:
            if self._finished:
                from app.backtesting.analyzers import AnalysisStatus

                self._analysis_engine.finalize(AnalysisStatus.FINAL)
                analysis_metrics = tuple(
                    self._analysis_engine.final_results or ()
                )
                analysis_status = "final"
            else:
                snapshot = self._latest_analysis_snapshot
                if snapshot is not None:
                    analysis_metrics = tuple(
                        snapshot.compute_provisional_results()
                    )
                analysis_status = "partial"
        analysis_chunk_token: str | None = None
        if self._analysis_engine is not None:
            from app.backtesting.analysis_inputs import evidence_digest

            analysis_chunk_token = evidence_digest(
                {
                    "contract": "analysis_chunk_v1",
                    "run_id": self._run_id,
                    "chunk_sequence": current_chunk_sequence,
                    "first_step_sequence": ordered[0].sequence,
                    "last_step_sequence": ordered[-1].sequence,
                    "completed_through_session": ordered[-1].metadata.get(
                        "session_date"
                    ),
                    "input_evidence_signature": (
                        self._analysis_engine.input_evidence_signature()
                    ),
                }
            )
            # Advance checkpoint identity only after the entire chunk has
            # succeeded. A later failed chunk must preserve this exact pair.
            self._last_analysis_chunk_sequence = current_chunk_sequence
            self._last_analysis_chunk_token = analysis_chunk_token
            session_text = ordered[-1].metadata.get("session_date")
            self._last_analysis_completed_session = (
                date.fromisoformat(session_text)
                if isinstance(session_text, str)
                else None
            )
        return BacktestRunResult(
            run_id=self._run_id,
            events=tuple(self._events),
            equity_curve=tuple(self._equity_curve),
            decisions=tuple(self._decisions),
            order_outcomes=tuple(
                OrderOutcomeRecord(
                    order_id=order.order_id,
                    intent_id=order.intent_id,
                    instrument_id=order.instrument_id,
                    side=order.side.value,
                    quantity=Decimal(str(order.quantity)),
                    status=order.status.value,
                    status_reason=order.status_reason,
                    submitted_at=order.submitted_at,
                    valid_from=order.valid_from,
                    valid_until=order.valid_until,
                )
                for order in self._orders
            ),
            final_snapshot=self._portfolio.snapshot(),
            components=self._component_snapshot(),
            analysis_status=analysis_status,
            chunk_sequence=(
                current_chunk_sequence
                if self._analysis_engine is not None
                else None
            ),
            analysis_chunk_token=analysis_chunk_token,
            completed_through_step_sequence=ordered[-1].sequence,
            analysis_metrics=analysis_metrics,
            rule_snapshot_hash=self.rule_snapshot_hash,
            universe_scope_snapshot_hash=self._universe_scope_snapshot_hash,
            universe_eligibility_summary=self._universe_audit_summary(),
            final_qualification_results=tuple(
                self._final_qualification_results
            ),
        )

    def build_analysis_failure_snapshot(self, exc: Exception) -> Any:
        """Freeze the analyzer-relevant facts after a mid-run failure.

        Called by the failure-finalization boundary right after the runner
        stopped; the returned immutable snapshot lets an independent
        transaction persist everything already determined (applied fills,
        valid equity observations, the blocked observation) without
        re-deriving anything from the dead runtime.
        """

        if self._analysis_engine is None:
            raise DomainValidationError(
                "this runner has no analyzer engine; there is no analysis "
                "failure to freeze"
            )
        if not isinstance(exc, BaseException):
            raise DomainValidationError(
                "analysis failure snapshots require the original exception "
                "instance"
            )
        if not self._failed:
            raise DomainValidationError(
                "analysis failure snapshots can only be built after the "
                "runner has actually failed"
            )
        pending: list[BaseException] = [exc]
        visited: set[int] = set()
        has_real_valuation_block = False
        source_exception: BaseException = exc
        phase_exception: PhaseExecutionError | None = None
        while pending and len(visited) < 16:
            current = pending.pop(0)
            if id(current) in visited:
                continue
            visited.add(id(current))
            if isinstance(current, ValuationBlockedError):
                has_real_valuation_block = True
            if isinstance(current, PhaseExecutionError) and phase_exception is None:
                phase_exception = current
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)
        if not has_real_valuation_block:
            raise DomainValidationError(
                "analysis failure snapshots require a real valuation-blocked "
                "exception in the failure chain"
            )
        if phase_exception is not None:
            source_exception = phase_exception
        blocked_observation = self._last_equity_observation
        if blocked_observation is None or blocked_observation.is_valid:
            raise DomainValidationError(
                "analysis failure snapshots require a blocked equity "
                "observation"
            )

        from app.backtesting.analysis_finalization import AnalysisFailureSnapshot
        analysis_snapshot = self._analysis_engine.snapshot()
        failed_step_sequence = getattr(source_exception, "step_sequence", None)
        if not isinstance(failed_step_sequence, int) or isinstance(
            failed_step_sequence, bool
        ):
            failed_step_sequence = self._next_expected_step
        failed_session_date: date | None = None
        if 0 <= failed_step_sequence < len(self._axis):
            session_text = self._axis.at(failed_step_sequence).metadata.get(
                "session_date"
            )
            if isinstance(session_text, str):
                failed_session_date = date.fromisoformat(session_text)
        error_type = getattr(source_exception, "error_type", None)
        if not isinstance(error_type, str):
            error_type = type(source_exception).__name__
        admission_token = getattr(
            getattr(self, "_analysis_admission", None),
            "_capability_token",
            None,
        )
        from app.backtesting.analyzers import _bind_failure_snapshot

        failure_envelope = {
            "error_message": str(source_exception),
            "error_type": error_type,
            "failed_step_sequence": failed_step_sequence,
            "failed_session_date": (
                failed_session_date.isoformat()
                if failed_session_date is not None
                else None
            ),
            "blocked_equity_observation": blocked_observation.evidence_payload(),
            "last_chunk_sequence": self._last_analysis_chunk_sequence,
            "last_chunk_token": self._last_analysis_chunk_token,
            "completed_through_session": (
                self._last_analysis_completed_session.isoformat()
                if self._last_analysis_completed_session is not None
                else None
            ),
            "universe_scope_snapshot_hash": self._universe_scope_snapshot_hash,
            "universe_final_qualification_failure": self._last_final_qualification_failure,
        }

        snapshot_binding = _bind_failure_snapshot(
            admission_token,
            analysis_snapshot,
            failure_envelope,
        )
        snapshot = AnalysisFailureSnapshot(
            run_id=self._run_id,
            failed_step_sequence=failed_step_sequence,
            failed_session_date=failed_session_date,
            error_type=error_type,
            error_message=str(source_exception),
            blocked_equity_observation=(
                self._last_equity_observation
                if self._last_equity_observation is not None
                and not self._last_equity_observation.is_valid
                else None
            ),
            analysis_snapshot=analysis_snapshot,
            admission_token=admission_token,
            formula_signature=analysis_snapshot.formula_signature(),
            input_evidence_signature=analysis_snapshot.input_evidence_signature(),
            valid_day_count=analysis_snapshot.valid_day_count,
            fill_count=analysis_snapshot.fill_count,
            snapshot_binding=snapshot_binding,
            last_chunk_sequence=self._last_analysis_chunk_sequence,
            last_chunk_token=self._last_analysis_chunk_token,
            completed_through_session=self._last_analysis_completed_session,
        )
        # Capture the terminal identity from the immutable snapshot when
        # possible.  The finalizer recomputes it and rejects any mismatch;
        # a computation failure is deliberately left to that boundary so the
        # original metric error is not disguised here.
        try:
            from app.backtesting.analyzers import compute_terminal_fingerprint

            failure_payload = {
                "abort_reason": snapshot.error_message,
                "failed_step_sequence": snapshot.failed_step_sequence,
                "failed_session_date": (
                    snapshot.failed_session_date.isoformat()
                    if snapshot.failed_session_date is not None
                    else None
                ),
                "error_type": snapshot.error_type,
            }
            fingerprint = compute_terminal_fingerprint(
                status="aborted",
                analysis_snapshot=analysis_snapshot,
                results=analysis_snapshot.compute_provisional_results(),
                failure=failure_payload,
            )
            from app.backtesting.analyzers import _bind_failure_terminal_fingerprint

            _bind_failure_terminal_fingerprint(snapshot.admission_token, fingerprint)
            snapshot = replace(snapshot, terminal_fingerprint=fingerprint)
        except Exception:
            pass
        return snapshot

    def _component_snapshot(self) -> Mapping[str, Mapping[str, Any]]:
        """Record the identity of every replaceable component used.

        Persisting this snapshot next to the events makes each run
        auditable: the registered component versions, the nested slippage
        and fee-schedule identities, the accounting policy configuration,
        and the frozen component parameters are all traceable without
        guessing from behavior.
        """

        snapshot: dict[str, Any] = {}

        def record(kind: str, obj: Any, key_attr: str) -> None:
            key = getattr(obj, key_attr, None)
            version = _component_version_of(obj)
            if isinstance(key, str) and isinstance(version, int):
                entry: dict[str, Any] = {"key": key, "version": version}
                # When the component exposes its live parameter snapshot,
                # capture it so the audit record reflects the actual
                # instance configuration instead of a caller claim.
                parameters = getattr(obj, "parameters", None)
                if isinstance(parameters, Mapping):
                    entry["parameters"] = dict(parameters)
                snapshot[kind] = entry

        record("timing_policy", self._timing_policy, "policy_key")
        record("execution_model", self._execution_model, "model_key")
        record(
            "decision_interpreter", self._interpreter, "interpreter_key"
        )
        # Nested execution sub-components when the model exposes them.
        slippage_model = getattr(self._execution_model, "slippage_model", None)
        if slippage_model is not None:
            record("slippage_model", slippage_model, "model_key")
        fee_calculator = getattr(self._execution_model, "fee_calculator", None)
        schedule = getattr(fee_calculator, "schedule", None)
        if schedule is not None and isinstance(
            getattr(schedule, "key", None), str
        ):
            # The audit value of this snapshot depends on capturing the
            # actual money-moving configuration, not just the schedule
            # identity: two runs sharing a key must stay distinguishable
            # when their rates differ.
            snapshot["fee_schedule"] = {
                "key": schedule.key,
                "version": getattr(schedule, "version", None),
                "fee_rules": [
                    {
                        "key": rule.key,
                        "category": rule.category,
                        "side": _enum_value_or_self(rule.side),
                        "rate": rule.rate,
                        "minimum": rule.minimum,
                        "fixed_amount": rule.fixed_amount,
                        "base_measure": _enum_value_or_self(
                            rule.base_measure
                        ),
                        "charge_timing": _enum_value_or_self(
                            rule.charge_timing
                        ),
                        "rule_type": _enum_value_or_self(rule.rule_type),
                        "currency": rule.currency,
                        "rounding_level": _enum_value_or_self(
                            rule.rounding_level
                        ),
                        # rounding_scope groups fill-level rounding, so two
                        # schedules can differ in realized fees while sharing
                        # every other field; it must be part of the audit.
                        "rounding_scope": rule.rounding_scope,
                        "rounding_mode": _enum_value_or_self(
                            rule.rounding_mode
                        ),
                        "rounding_precision": rule.rounding_precision,
                        "applicability": dict(
                            getattr(rule, "applicability", {}) or {}
                        ),
                    }
                    for rule in getattr(schedule, "fee_rules", ())
                ],
            }
        # Accounting configuration is part of the audit identity even
        # though the policy itself is not yet registry-constructed.
        settlement_policy = getattr(self._accounting, "settlement_policy", "")
        snapshot["accounting_policy"] = {
            "key": "accounting_policy",
            "version": 1,
            "currency": self._currency,
            "settlement_policy": getattr(
                settlement_policy, "value", str(settlement_policy)
            ),
        }
        if self._component_parameters:
            snapshot["parameters"] = dict(self._component_parameters)
        if self._rule_snapshot_bundle is not None:
            bundle = self._rule_snapshot_bundle
            snapshot["rule_snapshot"] = {
                "snapshot_hash": bundle.snapshot_hash,
                "rule_package_reference": {
                    "key": bundle.rule_package_reference.key,
                    "version": bundle.rule_package_reference.version,
                },
                "rule_package_semantic_hash": bundle.rule_package_semantic_hash,
                "parser_revision": bundle.parser_revision,
                "exception_set_reference": (
                    None
                    if bundle.exception_set_reference is None
                    else {
                        "key": bundle.exception_set_reference.key,
                        "version": bundle.exception_set_reference.version,
                    }
                ),
                "exception_set_hash": bundle.exception_set_hash,
                "data_cutoff": bundle.data_cutoff,
                "segment_count": len(bundle.instrument_segments),
                "used_segments": tuple(
                    {
                        "instrument_id": str(instrument_id),
                        "effective_at": effective_at.isoformat(),
                        "resolution_hash": resolution_hash,
                    }
                    for (instrument_id, effective_at), resolution_hash in sorted(
                        self._used_rule_segments.items(),
                        key=lambda item: (str(item[0][0]), item[0][1]),
                    )
                ),
            }
        if (
            self._universe_scope_resolution is not None
            or self._universe_scope_snapshot_hash is not None
            or self._step_universes
        ):
            # Candidate scope identity is a component-level audit fact, not
            # a strategy label.  It is kept alongside existing component
            # snapshots so result consumers need no new persistence table.
            snapshot["universe"] = dict(self._universe_audit_summary())
        # The whole snapshot is deep-frozen: nested component records and
        # parameter structures must be as immutable as the events they
        # audit.
        return MappingProxyType(
            {str(key): _freeze_payload(value) for key, value in snapshot.items()}
        )

    def _universe_audit_summary(self) -> Mapping[str, Any]:
        """Project candidate audit evidence onto an existing result JSON shape."""

        resolution = self._universe_scope_resolution
        summary: dict[str, Any] = {
            "scope_snapshot_hash": self._universe_scope_snapshot_hash,
            "qualification_policy_version": self._universe_eligibility_policy_version,
            "resolved_calendar_ids": self._frozen_universe_calendar_ids,
            "filtered_reason_counts": dict(self._filtered_reason_counts),
            "universe_filtered_reason_counts": dict(self._filtered_reason_counts),
            "candidate_count": 0,
            "universe_candidate_count": 0,
            "final_qualification_count": len(self._final_qualification_results),
            "final_rechecks": tuple(self._final_qualification_results),
            "universe_final_rechecks": tuple(self._final_qualification_results),
        }
        if resolution is not None:
            for name in (
                "scope_mode",
                "market_scope",
                "universe_query_policy",
                "capability_summary",
                "source_evidence",
            ):
                value = getattr(resolution, name, None)
                if value is not None:
                    summary[name] = value
        # A provider may expose a pre-computed filtering summary.  It is
        # copied as evidence only; runtime never recomputes or changes its
        # candidate qualification rules.
        for bound in self._step_universes.values():
            source = self._source_universe_query(bound)
            summary["candidate_count"] = max(
                int(summary.get("candidate_count", 0)), len(bound.candidates)
            )
            summary["universe_candidate_count"] = summary["candidate_count"]
            value = self._resolve_attr(
                source,
                (
                    "universe_filter_reason_counts",
                    "filtered_reason_counts",
                    "filter_reason_counts",
                ),
            )
            if not isinstance(value, Mapping):
                value = self._resolve_attr(
                    source,
                    (
                        "filter_summary",
                        "candidate_filter_summary",
                        "universe_filter_summary",
                    ),
                )
            if isinstance(value, Mapping):
                for nested_name in (
                    "reason_counts",
                    "filtered_reason_counts",
                    "universe_filtered_reason_counts",
                ):
                    nested = value.get(nested_name)
                    if isinstance(nested, Mapping):
                        value = nested
                        break
            if isinstance(value, Mapping):
                for reason, count in value.items():
                    try:
                        numeric_count = int(count)
                    except (TypeError, ValueError):
                        continue
                    # Providers may expose either a cumulative count or a
                    # per-step count.  Taking the greatest observed value
                    # avoids double-counting a cumulative source when the
                    # result summary walks multiple step snapshots.
                    self._filtered_reason_counts[str(reason)] = max(
                        self._filtered_reason_counts.get(str(reason), 0),
                        numeric_count,
                    )
                summary["filtered_reason_counts"] = dict(
                    sorted(self._filtered_reason_counts.items())
                )
                summary["universe_filtered_reason_counts"] = dict(
                    sorted(self._filtered_reason_counts.items())
                )
        if self._filter_evidence_records:
            summary["filter_records"] = tuple(
                self._filter_evidence_records
            )
            summary["universe_filter_records"] = tuple(
                self._filter_evidence_records
            )
        summary["final_rechecks"] = tuple(self._final_qualification_results)
        summary["universe_final_rechecks"] = tuple(
            self._final_qualification_results
        )
        return _freeze_runtime_evidence(
            _json_safe_runtime_value(summary)
        )

    # ------------------------------------------------------------------
    # Phase dispatch
    # ------------------------------------------------------------------

    def _execute_slice(
        self,
        ordered: tuple[TimeStep, ...],
        *,
        next_after_last: TimeStep | None,
    ) -> None:
        """Advance every phase of one validated slice.

        ``timing_policy.phases(...)`` runs inside the same wrapping
        boundary as the phases themselves, so a broken policy surfaces
        with step coordinates instead of a bare exception.
        """

        for index, step in enumerate(ordered):
            if index + 1 < len(ordered):
                next_step: TimeStep | None = ordered[index + 1]
            else:
                next_step = next_after_last
            try:
                instructions = self._timing_policy.phases(
                    step, next_step=next_step
                )
                for phase_sequence, instruction in enumerate(
                    instructions, start=1
                ):
                    events = self._advance_phase(
                        step=step,
                        next_step=next_step,
                        instruction=instruction,
                        phase_sequence=phase_sequence,
                    )
                    self._events.extend(events)
            except PhaseExecutionError:
                raise
            except Exception as exc:
                raise PhaseExecutionError(
                    run_id=self._run_id,
                    step_sequence=step.sequence,
                    phase_sequence=0,
                    phase_key="timing_policy",
                    error_type=type(exc).__name__,
                    message=(
                        f"the timing policy failed to produce phase "
                        f"instructions for step {step.sequence}: {exc}"
                    ),
                    error_code=getattr(exc, "code", None),
                    details=getattr(exc, "details", None),
                ) from exc
            self._complete_step(step)

    def _advance_phase(
        self,
        *,
        step: TimeStep,
        next_step: TimeStep | None,
        instruction: TimingInstruction,
        phase_sequence: int,
    ) -> tuple[EventEnvelope, ...]:
        # Everything from session-date parsing through handler execution
        # stays inside one wrapping boundary: a failing view factory or an
        # unreadable step must still surface with phase coordinates.
        handlers: dict[TimingPhase, Callable[[PhaseContext], list[EventEnvelope]]] = {
            TimingPhase.PRE_OPEN_SETTLE: self._phase_settle_restore,
            TimingPhase.OBSERVE: self._phase_observe_open,
            TimingPhase.MATCH: self._phase_match,
            TimingPhase.ACCOUNT: self._phase_account,
            TimingPhase.CASH_ACTIONS: self._phase_cash_effective,
            TimingPhase.VALUE: self._phase_value,
            TimingPhase.ANALYZE: self._phase_analyze,
            TimingPhase.DECIDE: self._phase_decide,
            TimingPhase.SUBMIT: self._phase_submit,
        }
        try:
            session_date_text = step.metadata.get("session_date")
            if not isinstance(session_date_text, str):
                raise DomainValidationError(
                    "step metadata must carry a session_date string"
                )
            session_date = date.fromisoformat(session_date_text)
            phase_view = self._view_factory.for_phase(
                instruction, step, next_step=next_step
            )
            if isinstance(phase_view, EngineDataView):
                # Freeze the explicit trading facts for later phases that
                # receive no engine view of their own.
                self._instrument_facts.update(phase_view.facts_snapshot())
            context = PhaseContext(
                run_id=self._run_id,
                step_sequence=step.sequence,
                phase_sequence=phase_sequence,
                phase_key=instruction.phase.value,
                session_date=session_date,
                timezone=step.timezone,
                decision_time=instruction.timestamp,
                data_cutoff=(
                    instruction.timestamp
                    if instruction.phase is TimingPhase.DECIDE
                    else None
                ),
                effective_from=instruction.effective_from,
                phase_view=phase_view,
            )
            handler = handlers.get(instruction.phase)
            if handler is None:
                raise DomainValidationError(
                    f"the runner has no handler for phase {instruction.phase!r}"
                )
            payloads_events = handler(context)
            envelopes: list[EventEnvelope] = []
            for offset, (event_type, payload) in enumerate(payloads_events):
                event_payload = dict(payload)
                single_snapshot, snapshots = self._event_display_snapshots(
                    event_payload, context
                )
                if single_snapshot is not None:
                    event_payload["display"] = self._display_snapshot_payload(
                        single_snapshot
                    )
                elif snapshots:
                    event_payload["displays"] = tuple(
                        self._display_snapshot_payload(snapshot)
                        for snapshot in snapshots.values()
                    )
                envelopes.append(
                    EventEnvelope(
                        run_id=self._run_id,
                        event_sequence=len(self._events) + offset + 1,
                        step_sequence=context.step_sequence,
                        phase_sequence=context.phase_sequence,
                        phase_key=context.phase_key,
                        event_type=event_type,
                        event_time=context.decision_time,
                        payload=event_payload,
                        display_snapshot=single_snapshot,
                        display_snapshots=snapshots,
                    )
                )
            return tuple(envelopes)
        except PhaseExecutionError:
            raise
        except Exception as exc:
            raise PhaseExecutionError(
                run_id=self._run_id,
                step_sequence=step.sequence,
                phase_sequence=phase_sequence,
                phase_key=instruction.phase.value,
                error_type=type(exc).__name__,
                message=str(exc),
                error_code=getattr(exc, "code", None),
                details=getattr(exc, "details", None),
            ) from exc

    def _event_display_snapshots(
        self, payload: Mapping[str, Any], context: PhaseContext
    ) -> tuple[
        InstrumentDisplaySnapshot | None,
        Mapping[UUID, InstrumentDisplaySnapshot],
    ]:
        """Resolve display identity for every instrument in one event.

        The event timestamp is the effective instant; the phase cutoff (or
        the timestamp itself for engine phases) is the separate knowledge
        cutoff.  Order-expiry and similar events carry only an order id, so
        the runtime resolves that id back to its stable instrument key before
        asking the display provider.  No trading code or candidate snapshot
        participates in this lookup.
        """

        ids: list[UUID] = []

        def add(value: object) -> None:
            if isinstance(value, UUID):
                candidate = value
            elif isinstance(value, str):
                try:
                    candidate = UUID(value)
                except ValueError:
                    return
            else:
                return
            if candidate not in ids:
                ids.append(candidate)

        add(payload.get("instrument_id"))
        values = payload.get("instrument_ids")
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            for value in values:
                add(value)
        targets = payload.get("targets")
        if isinstance(targets, Mapping):
            for value in targets:
                add(value)
        if not ids and payload.get("order_id") is not None:
            order_id = payload.get("order_id")
            for order in self._orders:
                if str(order.order_id) == str(order_id):
                    add(order.instrument_id)
                    break
        if not ids:
            return None, MappingProxyType({})

        provider = self._display_provider
        factory_resolver = getattr(self._view_factory, "display_snapshot", None)
        snapshots: dict[UUID, InstrumentDisplaySnapshot] = {}
        data_cutoff = context.data_cutoff or context.decision_time
        for instrument_id in ids:
            if callable(provider):
                # A runner-level callable is treated as a complete snapshot
                # resolver.  This is useful for factories that already own
                # the provider and keeps the result-model helper out of the
                # runtime's provider-specific surface.
                snapshot = provider(
                    instrument_id,
                    effective_at=context.decision_time,
                    data_cutoff=data_cutoff,
                )
            elif provider is not None:
                from app.backtesting.result_models import resolve_display_snapshot

                snapshot = resolve_display_snapshot(
                    provider,
                    instrument_id,
                    effective_at=context.decision_time,
                    data_cutoff=data_cutoff,
                )
            elif callable(factory_resolver):
                snapshot = factory_resolver(
                    instrument_id,
                    effective_at=context.decision_time,
                    data_cutoff=data_cutoff,
                )
            else:
                snapshot = InstrumentDisplaySnapshot(instrument_id=instrument_id)
            if not isinstance(snapshot, InstrumentDisplaySnapshot):
                raise DomainValidationError(
                    "display resolver must return an InstrumentDisplaySnapshot"
                )
            snapshot.require_matching_instrument(instrument_id, "display_snapshot")
            snapshots[instrument_id] = snapshot
        # A payload carrying ``instrument_id`` is a single-instrument event;
        # a payload carrying ``instrument_ids`` or target keys remains a
        # batch event even when the batch happens to contain one item.
        if (
            len(snapshots) == 1
            and "instrument_id" in payload
            and "instrument_ids" not in payload
            and not isinstance(payload.get("targets"), Mapping)
        ):
            return next(iter(snapshots.values())), MappingProxyType({})
        return None, MappingProxyType(snapshots)

    @staticmethod
    def _display_snapshot_payload(
        snapshot: InstrumentDisplaySnapshot,
    ) -> Mapping[str, object]:
        """Render the immutable snapshot into the event's JSON-safe view."""

        return {
            "instrument_id": str(snapshot.instrument_id),
            "event_trading_code": snapshot.event_trading_code,
            "event_name": snapshot.event_name,
            "event_display_name": snapshot.event_display_name,
        }

    def _emit_pair(
        self, event_type: str, payload: Mapping[str, Any]
    ) -> tuple[str, Mapping[str, Any]]:
        return (event_type, dict(payload))

    # ------------------------------------------------------------------
    # Open-time phases
    # ------------------------------------------------------------------

    def _phase_settle_restore(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        # Settlement release runs per named calendar: each instrument's
        # batch carries its own calendar attribution.
        released: list[UUID] = []
        calendar_ids = sorted(
            {
                batch.calendar_id
                for batch in self._accounting.pending_batches()
            }
        )
        for calendar_id in calendar_ids:
            released.extend(
                self._accounting.settle_pending_before_open_match(
                    self._portfolio,
                    calendar_id=calendar_id,
                    session_date=context.session_date,
                )
            )
        settled = tuple(dict.fromkeys(released))
        if not settled:
            return []
        return [
            self._emit_pair(
                BacktestEventType.SETTLEMENT_RESTORED,
                {
                    "instrument_ids": [str(i) for i in settled],
                    "session_date": context.session_date.isoformat(),
                },
            )
        ]

    def _require_engine_view(self, context: PhaseContext) -> EngineDataView:
        view = context.phase_view
        if not isinstance(view, EngineDataView):
            raise DomainValidationError(
                f"phase {context.phase_key} requires an EngineDataView"
            )
        return view

    def _phase_observe_open(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        # Creating the view already fetched and validated the session's raw
        # quotes; observation itself produces no business fact.
        self._require_engine_view(context)
        return []

    def _phase_match(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        view = self._require_engine_view(context)
        active = [
            order for order in self._orders if order.status is OrderStatus.SUBMITTED
        ]
        if not active:
            self._pending_fills = ()
            return []
        policy_by_instrument: dict[UUID, Any] = {}
        policy_rejections: list[tuple[UUID, str]] = []
        if self._rule_snapshot_bundle is not None:
            from app.backtesting.execution_policy import ExecutionPolicyError

            eligible: list[Order] = []
            for order in active:
                try:
                    policy = self.execution_policy_for(
                        order.instrument_id, context.session_date
                    )
                except (DomainValidationError, ExecutionPolicyError):
                    # Missing/ambiguous coverage is a run-level admission
                    # failure, not an order-level no-fill outcome.
                    raise
                policy_by_instrument[order.instrument_id] = policy
                reason = policy.validate_order(order)
                if reason is None:
                    eligible.append(order)
                    continue
                order.expire(reason)
                policy_rejections.append((order.order_id, reason))
            active = eligible
        market_states = {
            order.instrument_id: (
                view.market_state(
                    order.instrument_id, timestamp=context.decision_time
                )
            )
            for order in active
        }
        if policy_by_instrument:
            from dataclasses import replace as dataclass_replace

            # The market view supplies current session facts, while the
            # trading-critical tick is frozen by the run rule snapshot.
            market_states = {
                instrument_id: dataclass_replace(
                    state,
                    price_tick=policy_by_instrument[instrument_id].price_tick,
                )
                for instrument_id, state in market_states.items()
            }
        match_context = MatchContext.from_portfolio(
            self._portfolio, currency=self._currency
        )
        result = self._execution_model.match(active, market_states, match_context)
        emitted: list[tuple[str, Mapping[str, Any]]] = [
            self._emit_pair(
                BacktestEventType.FILL_CREATED,
                {
                    "fill_id": str(fill.fill_id),
                    "order_id": str(fill.order_id),
                    "instrument_id": str(fill.instrument_id),
                    "side": fill.side.value,
                    "reference_price": fill.reference_price,
                    "execution_price": fill.price,
                    "quantity": fill.quantity,
                    "contract_multiplier": fill.contract_multiplier,
                    "notional": fill.gross_notional,
                    "fees": fill.fees,
                    "fee_breakdown": (
                        {
                            "schedule_key": fill.fee_breakdown.schedule_key,
                            "schedule_version": (
                                fill.fee_breakdown.schedule_version
                            ),
                            "currency": fill.fee_breakdown.currency,
                            "total": fill.fee_breakdown.total,
                            "components": [
                                {
                                    "rule_key": component.rule_key,
                                    "category": component.category,
                                    "base_amount": component.base_amount,
                                    "raw_amount": component.raw_amount,
                                    "amount": component.amount,
                                }
                                for component in fill.fee_breakdown.components
                            ],
                        }
                        if fill.fee_breakdown is not None
                        else None
                    ),
                    "slippage_bps": fill.slippage_bps,
                    "slippage_model_key": fill.slippage_model_key,
                    "slippage_model_version": fill.slippage_model_version,
                    "slippage_model_parameters": (
                        dict(fill.slippage_model_parameters)
                        if fill.slippage_model_parameters is not None
                        else None
                    ),
                    "settlement_lot_id": (
                        str(lot_id)
                        if (lot_id := self._accounting.settlement_lot_for_fill(fill.fill_id))
                        is not None
                        else None
                    ),
                },
            )
            for fill in result.fills
        ]
        emitted.extend(
            self._emit_pair(
                BacktestEventType.ORDER_EXPIRED,
                {"order_id": str(skip.order_id), "reason": skip.reason},
            )
            for skip in result.skipped_orders
        )
        emitted.extend(
            self._emit_pair(
                BacktestEventType.ORDER_EXPIRED,
                {"order_id": str(order_id), "reason": reason},
            )
            for order_id, reason in policy_rejections
        )
        # Daily orders never roll over: anything the model left unprocessed
        # expires immediately with an explicit reason.
        for order in active:
            if order.status is OrderStatus.SUBMITTED:
                order.expire("not_matched_at_open")
                emitted.append(
                    self._emit_pair(
                        BacktestEventType.ORDER_EXPIRED,
                        {"order_id": str(order.order_id), "reason": "not_matched_at_open"},
                    )
                )
        self._pending_fills = result.fills
        return emitted

    def _phase_account(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        emitted: list[tuple[str, Mapping[str, Any]]] = []
        fills, self._pending_fills = self._pending_fills, ()
        for fill in fills:
            application = self._accounting.apply_fill(
                self._portfolio,
                fill,
                settlement_plan=self._settlement_plan_for(fill),
            )
            if not application.applied:
                continue
            from app.backtesting.analyzers import analyzer_decimal_context

            with analyzer_decimal_context():
                self._applied_fees_total += fill.fees
            if self._analysis_engine is not None:
                # The accounting layer has confirmed this fact; the analyzer
                # only aggregates it.  Fill facts carry no PIT cutoff unless
                # their source declares one.
                self._analysis_engine.observe_fill(
                    FillObservation(
                        fact=AppliedFillFact(
                            fill_id=fill.fill_id,
                            run_id=self._run_id,
                            session_date=context.session_date,
                            timestamp=fill.timestamp,
                            instrument_id=fill.instrument_id,
                            side=fill.side.value,
                            fill_price=fill.price,
                            fill_quantity=fill.quantity,
                            contract_multiplier=fill.contract_multiplier,
                            currency=fill.currency,
                            reporting_currency=self._currency,
                            fees=fill.fees,
                            # Preserve the accounting layer's confirmed
                            # notional; the analyzer must not recompute it
                            # under the process-default Decimal context.
                            gross_traded_notional=fill.gross_notional,
                        )
                    )
                )
            self._step_fill_summaries.append(
                FillSummaryDTO(
                    instrument_id=fill.instrument_id,
                    side=fill.side.value,
                    quantity=fill.quantity,
                    price=fill.price,
                )
            )
            emitted.append(
                self._emit_pair(
                    BacktestEventType.FILL_APPLIED,
                    {
                        "fill_id": str(fill.fill_id),
                        "order_id": str(fill.order_id),
                        "instrument_id": str(fill.instrument_id),
                        "applied": True,
                        "cash_delta": application.cash_delta,
                        "realized_pnl_delta": application.realized_pnl_delta,
                        # The lot exists only after the fill is applied,
                        # so the settlement-lot reference is audited here
                        # rather than in the earlier fill_created event.
                        "settlement_lot_id": (
                            str(lot_id)
                            if (
                                lot_id := self._accounting.settlement_lot_for_fill(
                                    fill.fill_id
                                )
                            )
                            is not None
                            else None
                        ),
                    },
                )
            )
        self._portfolio.remove_zero_positions()
        return emitted

    def _settlement_plan_for(self, fill: Fill) -> DeferredSettlementPlan | None:
        """Resolve the calendar-bound T+1 plan for one buy fill.

        The settlement session comes exclusively from the injected named
        calendar gateway for the instrument's own calendar.  There is no
        natural-calendar-day fallback: an unresolvable next open session
        (for example a buy on the final session) fails the run explicitly
        instead of inventing an auditable-looking date.
        """

        if fill.side is not OrderSide.BUY:
            return None
        facts = self._instrument_facts.get(fill.instrument_id)
        if facts is None:
            raise SettlementScheduleError(
                f"no instrument facts are available for {fill.instrument_id}; "
                "a settlement calendar cannot be attributed to this fill"
            )
        trade_session = fill.timestamp.date()
        next_session = self._settlement_calendar.next_open_session(
            facts.calendar_id, after_session=trade_session
        )
        if next_session is None or next_session <= trade_session:
            raise SettlementScheduleError(
                f"calendar {facts.calendar_id!r} has no official open "
                f"session after {trade_session.isoformat()}; the T+1 "
                "settlement of this buy cannot be resolved and natural-"
                "day guesses are not permitted"
            )
        # Pin the calendar definition version the plan was resolved
        # against when the gateway is version-aware; lots carry it into
        # their audit trail.  Gateways without version facts stay None.
        calendar_version = None
        version_resolver = getattr(
            self._settlement_calendar, "calendar_version_for", None
        )
        if callable(version_resolver):
            calendar_version = version_resolver(facts.calendar_id, trade_session)
        return DeferredSettlementPlan(
            calendar_id=facts.calendar_id,
            trade_session=trade_session,
            settlement_session=next_session,
            calendar_version=calendar_version,
        )

    def _phase_cash_effective(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        if self._corporate_actions is None:
            return []
        emitted: list[tuple[str, Mapping[str, Any]]] = []
        today = context.session_date

        # Freeze first: an event whose record date is today completes its
        # entitlement from the post-match portfolio of this session.  The
        # declared derivation rule states explicitly whether unsettled
        # T+1 lots count; nothing about the rule is inferred.
        for declaration in self._dividend_declarations:
            # Source-supplied entitlements are already quantity-frozen, but
            # the derivation rule remains part of the event's audit contract.
            # Validate it here as well so admission-time registration of a
            # pre-frozen event cannot create a second, weaker path.
            if declaration.is_entitlement_frozen:
                _dividend_includes_pending_settlement(declaration)
            already_frozen = (
                declaration.is_entitlement_frozen
                or declaration.event_id in self._completed_dividend_events
            )
            if declaration.record_date == today:
                self._freeze_dividend_entitlement(declaration)
            elif (
                declaration.record_date < today
                and not already_frozen
                and declaration.cash_effective_session_id >= today
            ):
                raise DividendDerivationError(
                    f"dividend event {declaration.event_id} has record "
                    f"date {declaration.record_date.isoformat()} before "
                    f"the run window but no frozen entitlement; the "
                    "source must supply the entitlement quantity"
                )

        # Credit second: due events land after this session's opening
        # match has fully committed (the fixed phase order guarantees it),
        # so dividend cash can never fund the same morning's checks.
        due = sorted(
            (
                event
                for event in self._completed_dividend_events.values()
                if event.cash_effective_session_id == today
            ),
            key=lambda event: (str(event.instrument_id), str(event.event_id)),
        )
        if not due:
            return emitted
        for event in due:
            # The declared effective session must equal the session the
            # run derives from the instrument's calendar and the source
            # arrival date: a source cannot pick its own landing day.
            self._validate_cash_effective_session(event)
        # The whole credit batch is atomic: every event applies to a
        # shadow account first, and the formal state commits only when
        # all of them succeeded.  A failing reversal can therefore never
        # leave an earlier credit of the same batch behind.
        shadow_portfolio = _clone_portfolio(self._portfolio)
        accounting_snapshot = self._accounting._snapshot_internal_state()
        applications: list[tuple[CashDividendEvent, Any]] = []
        try:
            for event in due:
                application = self._accounting.apply_cash_dividend_event(
                    shadow_portfolio,
                    event,
                    session_date=today,
                )
                applications.append((event, application))
        except Exception:
            self._accounting._restore_internal_state(accounting_snapshot)
            raise
        _copy_shadow_into(self._portfolio, shadow_portfolio)
        for event, application in applications:
            if not application.applied:
                continue
            emitted.append(
                self._emit_pair(
                    BacktestEventType.CASH_DIVIDEND_APPLIED,
                    {
                        "dividend_event_id": str(event.event_id),
                        "instrument_id": str(event.instrument_id),
                        "record_date": event.record_date.isoformat(),
                        "cash_effective_session_id": (
                            event.cash_effective_session_id.isoformat()
                        ),
                        "cash_effective_phase": (
                            event.cash_effective_phase.value
                        ),
                        "amount_per_share": event.amount_per_share,
                        "entitlement_quantity": event.entitlement_quantity,
                        "withholding_tax": event.withholding_tax,
                        "currency": event.currency,
                        "derivation_rule_key": event.derivation_rule_key,
                        "derivation_rule_version": event.derivation_rule_version,
                        "quantity": application.quantity,
                        "cash_delta": application.cash_delta,
                    },
                )
            )
        return emitted

    def _freeze_dividend_entitlement(
        self, declaration: CashDividendEvent
    ) -> None:
        """Freeze one open entitlement exactly once, on its record date."""

        if declaration.event_id in self._completed_dividend_events:
            return
        if declaration.is_entitlement_frozen:
            # A source may supply the entitlement directly when the run
            # starts after the record date; register it as completed
            # instead of attempting a second freeze.
            self._completed_dividend_events[declaration.event_id] = (
                declaration
            )
            return
        include_pending = _dividend_includes_pending_settlement(declaration)
        quantity = entitlement_from_portfolio(
            self._portfolio,
            self._accounting,
            instrument_id=declaration.instrument_id,
            include_pending_settlement=include_pending,
        )
        completed = declaration.with_entitlement(quantity)
        self._completed_dividend_events[declaration.event_id] = completed

    def _validate_cash_effective_session(self, event: CashDividendEvent) -> None:
        """Verify the declared effective session against calendar derivation.

        The cash-effective session is not a free parameter: it must equal
        the session this run derives from the instrument's own calendar
        and the event's source arrival date.  A source cannot submit an
        arbitrary landing day and have the account credit it blindly.
        """

        facts = self._instrument_facts.get(event.instrument_id)
        if facts is None:
            raise DividendDerivationError(
                f"dividend event {event.event_id} refers to instrument "
                f"{event.instrument_id} outside the run scope; its "
                "cash-effective session cannot be attributed to a "
                "trading calendar"
            )
        derived = derive_cash_effective_session(
            self._settlement_calendar,
            calendar_id=facts.calendar_id,
            source_arrival_date=event.source_arrival_date,
        )
        if derived != event.cash_effective_session_id:
            raise DividendDerivationError(
                f"dividend event {event.event_id} declares "
                f"cash_effective_session "
                f"{event.cash_effective_session_id.isoformat()} but "
                f"calendar {facts.calendar_id!r} derives "
                f"{derived.isoformat()} from the arrival date "
                f"{event.source_arrival_date.isoformat()}",
                details={
                    "event_id": str(event.event_id),
                    "calendar_id": facts.calendar_id,
                    "declared_session": (
                        event.cash_effective_session_id.isoformat()
                    ),
                    "derived_session": derived.isoformat(),
                },
            )

    # ------------------------------------------------------------------
    # Close-time phases
    # ------------------------------------------------------------------

    def _phase_value(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        view = self._require_engine_view(context)
        # Marks cover the full scope: held positions feed the valuation and
        # the rest feeds next-step order sizing.  Missing closes simply stay
        # absent; accounting blocks a valuation whose positions lack marks.
        marks = view.close_marks()
        valuation = self._accounting.value(
            self._portfolio,
            {i: m for i, m in marks.items() if i in self._portfolio.positions},
            as_of=context.decision_time,
        )
        self._last_marks = dict(marks)
        snapshot = valuation.snapshot
        blocked = self._portfolio.valuation_status is not ValuationStatus.COMPLETE
        # The analyzer observation is built before any control flow: a
        # blocked valuation must still hand its fact (with reason, no
        # equity) to the engine so the aborted finalization can report
        # exactly what was determined.
        if self._analysis_engine is not None:
            from app.backtesting.analysis_inputs import evidence_digest
            from app.backtesting.analyzers import analyzer_decimal_context

            with analyzer_decimal_context():
                cash_total = sum(
                    (
                        balance
                        for balance in snapshot.account.cash_balances.values()
                    ),
                    Decimal("0"),
                )
            data_cutoff_at = self._pit_data_gateway.data_cutoff_at(
                session_date=context.session_date,
                as_of=context.decision_time,
            )
            mark_evidence = view.close_mark_evidence(
                tuple(self._portfolio.positions)
            )
            observation = EquityObservation(
                run_id=self._run_id,
                step_sequence=context.step_sequence,
                session_date=context.session_date,
                as_of=context.decision_time,
                valuation_status="blocked" if blocked else "valid",
                data_cutoff_at=data_cutoff_at,
                reporting_currency=self._currency,
                cash=cash_total,
                equity=None if blocked else snapshot.account.equity,
                cumulative_fees=self._applied_fees_total,
                valuation_reason=(
                    "close valuation blocked by missing marks" if blocked else None
                ),
                evidence_hash=evidence_digest(
                    {
                        "contract": "end_of_day_valuation_marks_v1",
                        "session_date": context.session_date,
                        "data_cutoff_at": data_cutoff_at,
                        "marks": mark_evidence,
                    }
                ),
            )
            self._analysis_engine.observe_equity(observation)
            self._last_equity_observation = observation
        self._equity_curve.append(
            EquitySample(
                step_sequence=context.step_sequence,
                session_date=context.session_date,
                as_of=context.decision_time,
                equity=(
                    snapshot.account.equity
                    if self._portfolio.valuation_status is ValuationStatus.COMPLETE
                    else None
                ),
                valuation_status=self._portfolio.valuation_status.value,
            )
        )
        if self._portfolio.valuation_status is not ValuationStatus.COMPLETE:
            # A blocked valuation means held positions have no close mark.
            # Deciding or submitting against the stale equity would trade on
            # outdated facts, so the run terminates here with coordinates.
            raise ValuationBlockedError(
                f"the close valuation at step {context.step_sequence} "
                f"({context.session_date.isoformat()}) is blocked by missing "
                f"marks for {len(self._portfolio.positions)} held position(s); "
                "the run stops before any further decision or order"
            )
        return [
            self._emit_pair(
                BacktestEventType.PORTFOLIO_VALUED,
                {
                    "session_date": context.session_date.isoformat(),
                    "valuation_status": self._portfolio.valuation_status.value,
                    "market_value": valuation.market_value,
                    "equity": snapshot.account.equity,
                },
            )
        ]

    def _phase_analyze(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        if self._analysis_engine is not None:
            # Freeze the read-only partial snapshot for this step; chunked
            # callers read it without touching engine state.
            self._latest_analysis_snapshot = self._analysis_engine.snapshot()
        if not self._analyzers:
            return []
        snapshot = self._portfolio.snapshot()
        for analyzer in self._analyzers:
            analyzer(snapshot)
        return []

    def _phase_decide(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        # Dynamic candidates are bound once for this decision step, before
        # strategy code runs.  The bound snapshot is immutable and is also
        # the permission set used by the submit-time final check.
        source_override = None
        strategy_view = context.phase_view
        if isinstance(strategy_view, StrategyDataDTO):
            try:
                source_override = strategy_view.universe
            except Exception:
                source_override = None
        bound_universe = self._bind_step_universe(
            context, source_override=source_override
        )
        decision_cutoff = (
            context.data_cutoff or context.decision_time
        )
        self._last_decision_data_cutoff = decision_cutoff
        self._step_decision_data_cutoffs[context.step_sequence] = decision_cutoff
        formal_dynamic = self._is_formal_dynamic_scope(bound=bound_universe)
        qualification_port = self._candidate_evaluator(
            allow_pure_fallback=not formal_dynamic
        )
        if formal_dynamic and qualification_port is None:
            # A formal dynamic/hybrid run cannot treat the provider's
            # context-building snapshot as a qualification proof.  Stop
            # before invoking strategy code when no provider-owned port can
            # recheck the current PIT facts.
            raise _universe_capability_error(
                "formal dynamic/hybrid execution requires a candidate qualification port",
                details={
                    "scope_mode": self._scope_mode_value(
                        self._resolve_attr(
                            self._universe_scope_resolution, ("scope_mode",)
                        )
                    )
                    or self._scope_mode_value(
                        self._resolve_attr(
                            self._source_universe_query(bound_universe),
                            ("scope_mode",),
                        )
                    ),
                    "reason_code": "candidate_qualification_port_missing",
                },
            )
        self._step_qualification_ports[context.step_sequence] = qualification_port
        # The eager provider read above only freezes the complete engine
        # snapshot.  It must not grant dynamic data access before the strategy
        # has actually called its own bound ``universe.query()``.
        if formal_dynamic:
            self._clear_prestrategy_universe_authorization(
                bound_universe.source
                if bound_universe.source is not None
                else source_override
            )
        decision_context = self._build_decision_context(
            context, bound_universe=bound_universe
        )
        decision = self._strategy.on_step(decision_context)
        # The provider may expose filter counts through a chunk-backed query
        # facade; capture them after strategy filters have run as well as at
        # snapshot construction time.
        self._capture_universe_filter_evidence(bound_universe.source)
        decision_id = self._derived_id(f"decision:{context.step_sequence}")
        decision = replace(decision, decision_id=decision_id)
        self._pending_decision = decision
        self._decisions.append(decision)
        return [
            self._emit_pair(
                BacktestEventType.STRATEGY_DECISION_CREATED,
                {
                    "decision_id": str(decision.decision_id),
                    "mode": decision.mode,
                    "targets": {
                        str(key): str(value)
                        for key, value in decision.targets.items()
                    },
                    "reason": decision.reason,
                    "universe_scope_snapshot_hash": self._universe_scope_snapshot_hash,
                    "universe_candidate_count": len(
                        self._step_candidates.get(context.step_sequence, ())
                    ),
                },
            )
        ]

    def _phase_submit(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        decision = self._pending_decision
        if decision is None:
            raise DomainValidationError(
                "submit phase requires a decision from the same step"
            )
        # A queue delay or an in-memory provider revision may change the
        # dynamic scope after ``decide``.  Re-verify the admission snapshot
        # immediately before qualification and order construction.
        self._verify_scope_snapshot(context)
        # Decision payload keys are instrument-id strings; normalize once so
        # the frozen-facts lookups line up with UUID-keyed state.
        try:
            target_ids = {
                key if isinstance(key, UUID) else UUID(str(key))
                for key in dict(decision.targets or {})
            }
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(
                f"decision targets contain an invalid instrument id: {exc}"
            ) from exc
        # FINAL-* is deliberately before both interpretation/sizing and order
        # construction.  All selected targets are checked first, so one bad
        # target can never leave an earlier target's order in ``_orders``.
        self._final_validate_targets(
            tuple(target_ids), context=context, decision=decision
        )
        register_dynamic = getattr(
            self._view_factory, "register_runtime_instrument_ids", None
        )
        if callable(register_dynamic):
            register_dynamic(tuple(target_ids))
        self._pending_decision = None
        instrument_ids = sorted(
            target_ids | set(self._portfolio.positions), key=str
        )
        # Sizing uses the frozen facts observed from this step's engine
        # views, so board lots and other per-instrument specs are exact.
        facts = {
            instrument_id: self._instrument_facts[instrument_id]
            for instrument_id in instrument_ids
            if instrument_id in self._instrument_facts
        }
        if self._rule_snapshot_bundle is not None:
            from dataclasses import replace as dataclass_replace

            # Sizing must use the frozen lot size.  Session facts remain the
            # source of explicit status/calendar values, but a live rule row
            # can never silently replace the run snapshot.
            for instrument_id in instrument_ids:
                policy = self.execution_policy_for(
                    instrument_id, context.session_date
                )
                fact = facts.get(instrument_id)
                if fact is None:
                    raise DomainValidationError(
                        f"instrument facts are missing for {instrument_id}"
                    )
                facts[instrument_id] = dataclass_replace(
                    fact, board_lot=policy.lot_size
                )
        intents = self._interpreter.interpret(
            decision,
            portfolio=self._portfolio,
            equity=self._portfolio.account.equity,
            reference_prices=self._last_marks,
            facts=facts,
        )
        effective_from = context.effective_from
        if effective_from is None:
            raise DomainValidationError(
                "the submit phase requires an effective_from from the timing policy"
            )
        emitted: list[tuple[str, Mapping[str, Any]]] = []
        staged_orders: list[Order] = []
        staged_order_records: list[OrderSummaryDTO] = []
        for index, intent in enumerate(intents, start=0):
            intent = replace(
                intent,
                intent_id=self._derived_id(
                    f"intent:{context.step_sequence}:{index}"
                ),
                valid_from=effective_from,
                valid_until=effective_from,
            )
            order = Order.from_intent(
                intent,
                order_id=self._derived_id(
                    f"order:{context.step_sequence}:{index}"
                ),
                submitted_at=context.decision_time,
            )
            staged_orders.append(order)
            staged_order_records.append(
                OrderSummaryDTO(
                    instrument_id=order.instrument_id,
                    side=order.side.value,
                    quantity=order.quantity,
                    status=order.status.value,
                )
            )
            emitted.append(
                self._emit_pair(
                    BacktestEventType.ORDER_SUBMITTED,
                    {
                        "order_id": str(order.order_id),
                        "intent_id": str(order.intent_id),
                        "instrument_id": str(order.instrument_id),
                        "side": order.side.value,
                        "quantity": order.quantity,
                        "decision_id": str(decision.decision_id),
                        "valid_from": order.valid_from.isoformat(),
                        "valid_until": order.valid_until.isoformat(),
                    },
                )
            )
        # Commit all order objects only after every intent has been converted
        # successfully.  Qualification and conversion failures therefore
        # cannot leave a prefix of this decision's orders in runtime state.
        self._orders.extend(staged_orders)
        self._step_order_records.extend(staged_order_records)
        return emitted

    # ------------------------------------------------------------------
    # Context builders and helpers
    # ------------------------------------------------------------------

    def _build_decision_context(
        self,
        context: PhaseContext,
        *,
        bound_universe: _StepBoundUniverse | None = None,
    ) -> DecisionContext:
        view = context.phase_view
        if not isinstance(view, StrategyDataDTO):
            raise DomainValidationError(
                "the decide phase requires a StrategyDataDTO strategy view"
            )
        clock = DeterministicClockDTO(
            decision_time=context.decision_time,
            session_date=context.session_date,
        )
        if bound_universe is None:
            bound_universe = self._step_universes.get(context.step_sequence)
        if bound_universe is not None:
            # The official strategy-facing contract only accepts
            # InstrumentCandidateDTO values.  A malformed lower-level result
            # is a provider contract violation, never a best-effort fallback.
            if not all(
                isinstance(candidate, InstrumentCandidateDTO)
                for candidate in bound_universe.strategy_candidates
            ):
                raise _universe_provider_error(
                    "bound universe candidates must be InstrumentCandidateDTO values"
                )
            universe = UniverseQueryDTO(bound_universe)
        else:
            universe = self._view_factory.universe()
        return DecisionContext(
            step_sequence=context.step_sequence,
            session_date=context.session_date,
            decision_time=context.decision_time,
            data_cutoff=context.data_cutoff,
            timezone=context.timezone,
            clock=clock,
            portfolio=self._build_portfolio_dto(),
            previous_step=self._previous_step
            or PreviousStepDTO(step_sequence=max(context.step_sequence - 1, 0)),
            data=view,
            universe=universe,
        )

    def _build_portfolio_dto(self) -> PortfolioDTO:
        account = self._portfolio.account
        positions = []
        for instrument_id, position in self._portfolio.positions.items():
            if position.is_zero:
                continue
            candidate = self._identities.get(instrument_id)
            trading_code = (
                candidate.trading_code if candidate is not None else str(instrument_id)
            )
            name = candidate.name if candidate is not None else str(instrument_id)
            display_name = (
                candidate.display_name
                if candidate is not None
                else str(instrument_id)
            )
            positions.append(
                PositionDTO(
                    instrument_id=instrument_id,
                    trading_code=trading_code,
                    name=name,
                    display_name=display_name,
                    side=position.side,
                    quantity=position.quantity,
                    available_quantity=position.available_quantity,
                    average_price=position.average_price or ZERO,
                    mark_price=position.mark_price,
                    realized_pnl=position.realized_pnl,
                    unrealized_pnl=position.unrealized_pnl,
                )
            )
        return PortfolioDTO(
            cash_balances={
                currency: Decimal(str(balance))
                for currency, balance in account.cash_balances.items()
            },
            available_cash=account.available_cash,
            frozen_cash=account.frozen_cash,
            margin_used=account.margin_used,
            margin_available=account.margin_available,
            equity=account.equity,
            positions=tuple(positions),
        )

    def _complete_step(self, step: TimeStep) -> None:
        """Freeze the finished step's order/fill digest for the next decide."""

        self._previous_step = PreviousStepDTO(
            step_sequence=step.sequence,
            orders=tuple(self._step_order_records),
            fills=tuple(self._step_fill_summaries),
        )
        self._step_order_records = []
        self._step_fill_summaries = []

    def _derived_id(self, local_key: str) -> UUID:
        """Deterministic run-scoped identifier from the run namespace."""

        return uuid5(self._namespace, local_key)
