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
from types import MappingProxyType
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.backtesting.accounting import (
    AccountingPolicy,
    DeferredSettlementPlan,
    Fill,
    OrderSide,
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

    def __post_init__(self) -> None:
        _aware_datetime(self.event_time, "event_time")
        if isinstance(self.event_sequence, bool) or not isinstance(
            self.event_sequence, int
        ):
            raise DomainValidationError("event_sequence must be an integer")
        object.__setattr__(self, "payload", _freeze_payload(self.payload))


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
    ) -> None:
        self._strategy_view = strategy_view
        self._engine_market_data = engine_market_data
        self._adjustment_gate = adjustment_gate
        self._scope_instrument_ids = tuple(dict.fromkeys(scope_instrument_ids))
        self._universe_dto = UniverseQueryDTO(universe_query)

    def universe(self) -> UniverseQueryDTO:
        return self._universe_dto

    def candidate_identities(self) -> dict[UUID, InstrumentCandidateDTO]:
        """PIT candidate identity table keyed by stable instrument id."""

        return {
            candidate.instrument_id: candidate
            for candidate in self._universe_dto.query()
        }

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
            return StrategyDataDTO(
                self._strategy_view,
                data_cutoff=instruction.timestamp,
                adjustment_gate=self._adjustment_gate,
            )
        session_date = date.fromisoformat(step.metadata["session_date"])
        quotes = self._engine_market_data.session_quotes(
            self._scope_instrument_ids, session_date
        )
        facts = self._engine_market_data.instrument_facts(
            self._scope_instrument_ids
        )
        return EngineDataView(
            quotes=quotes, facts=facts, session_date=session_date
        )


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
    """Immutable aggregate of one completed run."""

    run_id: str
    events: tuple[EventEnvelope, ...]
    equity_curve: tuple[EquitySample, ...]
    decisions: tuple[Any, ...]
    order_outcomes: tuple[OrderOutcomeRecord, ...]
    final_snapshot: PortfolioSnapshot
    components: Mapping[str, Mapping[str, Any]] = MappingProxyType({})


class DeterministicBacktestRunner:
    """Drive the registered timing policy over the axis, phase by phase.

    The runner owns only mutable runtime state (portfolio, orders, event
    counter).  Every business mutation flows through injected components:
    orders become intents only in ``submit``, fills are produced solely by
    the :class:`ExecutionModel`, and the account changes exclusively through
    :meth:`AccountingPolicy.apply_fill`, explicit settlement restoration,
    and cash-dividend accounting events.
    """

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
        self._run_id = run_id
        self._axis = axis
        self._timing_policy = timing_policy
        self._view_factory = view_factory
        self._strategy = strategy
        self._interpreter = interpreter
        self._execution_model = execution_model
        self._accounting = accounting
        self._corporate_actions = corporate_actions
        self._analyzers = tuple(analyzers)
        self._currency = currency.upper()
        self._settlement_calendar = settlement_calendar
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
        self._finished = False
        self._failed = False

        self._portfolio = initial_portfolio
        self._orders: list[Order] = []
        self._pending_fills: tuple[Fill, ...] = ()
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
        # Digest pieces of the most recently completed step, consumed by the
        # next decide phase's PreviousStepDTO.
        self._previous_step: PreviousStepDTO | None = None
        self._step_order_records: list[OrderSummaryDTO] = []
        self._step_fill_summaries: list[FillSummaryDTO] = []
        self._identities: dict[UUID, InstrumentCandidateDTO] = (
            view_factory.candidate_identities()
        )
        self._namespace = uuid5(
            NAMESPACE_URL, f"quant-foundry:backtest-run:{self._run_id}"
        )

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
        )

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
        # The whole snapshot is deep-frozen: nested component records and
        # parameter structures must be as immutable as the events they
        # audit.
        return MappingProxyType(
            {str(key): _freeze_payload(value) for key, value in snapshot.items()}
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
            ) from exc
        return tuple(
            EventEnvelope(
                run_id=self._run_id,
                event_sequence=len(self._events) + offset + 1,
                step_sequence=context.step_sequence,
                phase_sequence=context.phase_sequence,
                phase_key=context.phase_key,
                event_type=event_type,
                event_time=context.decision_time,
                payload=payload,
            )
            for offset, (event_type, payload) in enumerate(payloads_events)
        )

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
        market_states = {
            order.instrument_id: view.market_state(
                order.instrument_id, timestamp=context.decision_time
            )
            for order in active
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
        if not self._analyzers:
            return []
        snapshot = self._portfolio.snapshot()
        for analyzer in self._analyzers:
            analyzer(snapshot)
        return []

    def _phase_decide(
        self, context: PhaseContext
    ) -> list[tuple[str, Mapping[str, Any]]]:
        decision_context = self._build_decision_context(context)
        decision = self._strategy.on_step(decision_context)
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
        self._pending_decision = None
        # Decision payload keys are instrument-id strings; normalize once so
        # the frozen-facts lookups line up with UUID-keyed state.
        try:
            target_ids = {
                key if isinstance(key, UUID) else UUID(str(key))
                for key in dict(decision.targets or {})
            }
        except ValueError as exc:
            raise DomainValidationError(
                f"decision targets contain an invalid instrument id: {exc}"
            ) from exc
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
            self._orders.append(order)
            self._step_order_records.append(
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
        return emitted

    # ------------------------------------------------------------------
    # Context builders and helpers
    # ------------------------------------------------------------------

    def _build_decision_context(
        self, context: PhaseContext
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
            universe=self._view_factory.universe(),
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
