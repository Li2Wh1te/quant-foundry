"""Session-scoped opening-match batching with atomic account commit.

This module implements the fixed formal event order for one opening
match:

1. read the :class:`~app.backtesting.execution_policy.SessionContext`;
2. open a shadow-account transaction (a detached copy of the portfolio
   plus a restorable snapshot of the accounting policy internals);
3. apply the session's settlement boundary *before* any matching;
4. filter orders by the half-open ``[valid_from, valid_until)`` window;
5. validate order types and quantity constraints per instrument policy;
6. plan sells (releasing proceeds into staged cash), then buys — both
   stages ordered by the stable ``submission_sequence``;
7. apply every planned fill to the shadow account;
8. only after every application succeeds, commit account, positions,
   orders, and fills at once.

Any failure while applying the plans aborts the whole batch: formal
cash, positions, order states, and fill facts remain untouched and an
``ACCOUNTING_BATCH_ABORTED`` error is raised.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Mapping, Sequence
from uuid import UUID, uuid5

from app.backtesting.accounting import (
    AccountingError,
    AccountingPolicy,
    DeferredSettlementPlan,
    Fill,
    OrderSide,
    PortfolioState,
    SettlementRelease,
)
from app.backtesting.domain import (
    AccountState,
    DomainValidationError,
    PositionState,
    ZERO,
)
from app.backtesting.execution import (
    MarketState,
    Order,
    OrderStatus,
)
from app.backtesting.execution_policy import (
    ExecutionPolicyError,
    InstrumentExecutionPolicy,
    SessionContext,
    SettlementBoundary,
    SettlementBoundaryPhase,
)
from app.backtesting.fees import (
    FeeBreakdown,
    FeeCalculator,
    FeeRuleUnresolvedError,
    fee_snapshot_for_rules,
    resolve_instrument_fee_rules,
)
from app.backtesting.slippage import SlippageModel

__all__ = [
    "ACCOUNTING_BATCH_ABORTED",
    "AccountingBatchAbortedError",
    "MatchLedger",
    "MatchOrderReason",
    "OpeningMatchBatchResult",
    "OpeningMatchService",
    "PlannedOrderUpdate",
]

#: Stable machine code emitted when a batch cannot be committed.
ACCOUNTING_BATCH_ABORTED = "ACCOUNTING_BATCH_ABORTED"


class MatchOrderReason(StrEnum):
    """Stable terminal/non-terminal reason codes for order outcomes.

    The values are the frozen machine contract from the rule design;
    human-readable summaries live with persistence, never here.
    """

    ORDER_NOT_YET_VALID = "ORDER_NOT_YET_VALID"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_TYPE_NOT_SUPPORTED = "ORDER_TYPE_NOT_SUPPORTED"
    ORDER_QUANTITY_PRECISION_INVALID = "ORDER_QUANTITY_PRECISION_INVALID"
    ORDER_QUANTITY_BELOW_MINIMUM = "ORDER_QUANTITY_BELOW_MINIMUM"
    ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT = "ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT"
    INSTRUMENT_SUSPENDED = "INSTRUMENT_SUSPENDED"
    OPEN_UNAVAILABLE = "OPEN_UNAVAILABLE"
    BUY_UNAVAILABLE_AT_PRICE_LIMIT = "BUY_UNAVAILABLE_AT_PRICE_LIMIT"
    SELL_UNAVAILABLE_AT_PRICE_LIMIT = "SELL_UNAVAILABLE_AT_PRICE_LIMIT"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_AVAILABLE_QUANTITY = "INSUFFICIENT_AVAILABLE_QUANTITY"
    FEE_RULE_UNRESOLVED = "FEE_RULE_UNRESOLVED"
    MARKET_STATE_MISSING = "MARKET_STATE_MISSING"


class SessionMatchError(DomainValidationError):
    """Raised when a session cannot be matched under the formal rules."""

    code = "session_match_error"


class SettlementBoundaryMismatchError(SessionMatchError):
    """The supplied boundary does not belong to the matched session."""

    code = "settlement_boundary_mismatch"


class AccountingBatchAbortedError(AccountingError):
    """A planned batch failed inside the shadow account; nothing committed."""

    code = ACCOUNTING_BATCH_ABORTED


@dataclass(frozen=True, slots=True)
class PlannedOrderUpdate:
    """One deterministic order-state transition planned by the batch."""

    order_id: UUID
    instrument_id: UUID
    new_status: OrderStatus
    reason: str | None
    filled_quantity: Decimal


def _respects_precision(value: Decimal, precision: int) -> bool:
    """Whether ``value`` is exactly representable at ``precision`` digits."""

    tuple_digits = value.normalize().as_tuple()
    if not isinstance(tuple_digits.exponent, int):
        return False
    return tuple_digits.exponent >= -precision


def _is_tick_multiple(value: Decimal, tick: Decimal) -> bool:
    """Whether ``value`` is an exact integer multiple of ``tick``."""

    quotient = value / tick
    return quotient == quotient.to_integral_value()


def _clone_portfolio(portfolio: PortfolioState) -> PortfolioState:
    """Detach a deep, mutable copy of the portfolio for shadow use."""

    account = portfolio.account
    cloned_account = AccountState(
        cash_balances=dict(account.cash_balances),
        available_cash=account.available_cash,
        frozen_cash=account.frozen_cash,
        margin_used=account.margin_used,
        margin_available=account.margin_available,
        equity=account.equity,
    )
    cloned_positions = {
        instrument_id: dataclasses.replace(position)
        for instrument_id, position in portfolio.positions.items()
    }
    clone = PortfolioState(
        account=cloned_account,
        as_of=portfolio.as_of,
        positions=cloned_positions,
        valuation_status=portfolio.valuation_status,
    )
    return clone


def _copy_shadow_into(formal: PortfolioState, shadow: PortfolioState) -> None:
    """Copy committed shadow values back onto the formal state objects."""

    formal.account.cash_balances.clear()
    formal.account.cash_balances.update(shadow.account.cash_balances)
    formal.account.available_cash = shadow.account.available_cash
    formal.account.frozen_cash = shadow.account.frozen_cash
    formal.account.margin_used = shadow.account.margin_used
    formal.account.margin_available = shadow.account.margin_available
    formal.account.equity = shadow.account.equity
    formal.positions.clear()
    formal.positions.update(shadow.positions)
    formal.as_of = shadow.as_of
    formal.valuation_status = shadow.valuation_status


@dataclass(slots=True)
class MatchLedger:
    """Detached planning balances used only while one batch is matched.

    ``reserved_cash`` and ``reserved_quantities`` are resources already
    claimed by planned orders inside the current opening match; they are
    staging values, never persistent account freezes.  The first slice's
    market orders do not freeze unknown amounts at submission time, so
    both reservations start empty and grow only while plans are made.
    """

    currency: str
    cash_balance_snapshot: Decimal
    available_cash: Decimal
    reserved_cash: Decimal = ZERO
    available_quantities: dict[UUID, Decimal] = field(default_factory=dict)
    reserved_quantities: dict[UUID, Decimal] = field(default_factory=dict)
    planned_fills: list[Fill] = field(default_factory=list)
    planned_order_updates: list[PlannedOrderUpdate] = field(default_factory=list)

    @classmethod
    def from_portfolio(
        cls,
        portfolio: PortfolioState,
        *,
        currency: str,
    ) -> "MatchLedger":
        """Seed the ledger from the shadow portfolio after settlement."""

        return cls(
            currency=currency,
            cash_balance_snapshot=portfolio.account.cash_balances[currency],
            available_cash=portfolio.account.available_cash,
            available_quantities={
                instrument_id: position.available_quantity
                for instrument_id, position in portfolio.positions.items()
            },
        )


@dataclass(frozen=True, slots=True)
class OpeningMatchBatchResult:
    """Deterministic output of one committed opening-match batch."""

    batch_id: UUID
    session_id: UUID
    opening_match_at: datetime
    fills: tuple[Fill, ...]
    skipped_orders: tuple[tuple[UUID, str], ...]
    order_updates: tuple[PlannedOrderUpdate, ...]
    settlement_release: SettlementRelease | None


@dataclass(frozen=True, slots=True)
class OpeningMatchService:
    """Plan and atomically commit one session's opening-market batch.

    The service consumes frozen rule snapshots (via
    :class:`InstrumentExecutionPolicy`), the frozen fee schedule
    snapshot of the run, and the externally resolved settlement
    boundary.  It never mutates formal state before every planned fill
    has been applied to a shadow account.
    """

    slippage_model: SlippageModel
    fee_schedule: object  # FeeSchedule | FeeScheduleSnapshot

    def run_opening_match(
        self,
        *,
        session: SessionContext,
        orders: Sequence[Order],
        policies: Mapping[UUID, InstrumentExecutionPolicy],
        portfolio: PortfolioState,
        accounting: AccountingPolicy,
        currency: str | None = None,
        settlement_boundary: SettlementBoundary | None = None,
        settlement_plan_factory: Callable[[Fill], DeferredSettlementPlan]
        | None = None,
    ) -> OpeningMatchBatchResult:
        """Execute the fixed event order and commit or abort as a whole."""

        if not session.exchange_open:
            raise SessionMatchError(
                f"session {session.session_id} is not open; no match may run"
            )
        run_currency = (
            currency.strip().upper()
            if currency is not None
            else accounting.currency
        )
        for instrument_id, policy in policies.items():
            if policy.currency != run_currency:
                raise SessionMatchError(
                    f"instrument {instrument_id} resolves to currency "
                    f"{policy.currency} which differs from the run "
                    f"currency {run_currency}"
                )

        batch_id = uuid5(
            session.session_id,
            "opening-match:" + ":".join(
                str(order.order_id) for order in self._sequenced(orders)
            ),
        )

        # --- Shadow transaction: snapshot everything the batch touches.
        shadow_portfolio = _clone_portfolio(portfolio)
        accounting_snapshot = accounting._snapshot_internal_state()

        try:
            release = self._apply_settlement_boundary(
                session=session,
                shadow_portfolio=shadow_portfolio,
                accounting=accounting,
                boundary=settlement_boundary,
            )

            ledger = MatchLedger.from_portfolio(
                shadow_portfolio, currency=run_currency
            )
            skipped: list[tuple[UUID, str]] = []
            updates: list[PlannedOrderUpdate] = []
            pending_orders: list[Order] = []

            # Validity window, type, and quantity validation happen before
            # any market-dependent work; nothing here can fail halfway.
            for order in self._sequenced(orders):
                outcome = self._validate_order(order, session, policies)
                if outcome is not None:
                    reason, terminal = outcome
                    skipped.append((order.order_id, reason))
                    if terminal:
                        updates.append(
                            PlannedOrderUpdate(
                                order_id=order.order_id,
                                instrument_id=order.instrument_id,
                                new_status=(
                                    OrderStatus.EXPIRED
                                    if reason == MatchOrderReason.ORDER_EXPIRED
                                    else OrderStatus.REJECTED
                                ),
                                reason=reason,
                                filled_quantity=order.filled_quantity,
                            )
                        )
                    continue
                pending_orders.append(order)

            sells = [
                order
                for order in pending_orders
                if order.side is OrderSide.SELL
            ]
            buys = [
                order for order in pending_orders if order.side is OrderSide.BUY
            ]
            for order in sells:
                self._plan_order(
                    order=order,
                    session=session,
                    policy=policies[order.instrument_id],
                    ledger=ledger,
                    skipped=skipped,
                    updates=updates,
                )
            for order in buys:
                self._plan_order(
                    order=order,
                    session=session,
                    policy=policies[order.instrument_id],
                    ledger=ledger,
                    skipped=skipped,
                    updates=updates,
                )

            # Apply every planned fill to the shadow account.  Any raise
            # below propagates to the abort handler; the formal portfolio
            # has not been touched at this point.
            uses_deferred = accounting._uses_deferred_settlement
            for fill in ledger.planned_fills:
                plan = None
                if fill.side is OrderSide.BUY and uses_deferred:
                    if settlement_plan_factory is None:
                        raise SessionMatchError(
                            "deferred settlement requires a settlement "
                            "plan factory for buy fills"
                        )
                    plan = settlement_plan_factory(fill)
                    self._validate_settlement_plan(plan, session)
                accounting.apply_fill(shadow_portfolio, fill, settlement_plan=plan)
        except Exception as exc:
            # Abort: restore accounting internals captured before the
            # batch and leave the formal portfolio untouched.
            accounting._restore_internal_state(accounting_snapshot)
            if isinstance(exc, (AccountingBatchAbortedError, SessionMatchError)):
                raise
            raise AccountingBatchAbortedError(
                f"opening-match batch {batch_id} was aborted before commit: {exc}",
                details={
                    "batch_id": str(batch_id),
                    "session_id": str(session.session_id),
                    "abort_reason": type(exc).__name__,
                },
            ) from exc

        # --- Commit: orders first get their planned transitions, then the
        # committed shadow values replace the formal account/positions.
        update_by_order = {update.order_id: update for update in updates}
        committed_updates: list[PlannedOrderUpdate] = []
        for order in orders:
            update = update_by_order.get(order.order_id)
            if update is None:
                continue
            order.status = update.new_status
            order.status_reason = update.reason
            if update.new_status is OrderStatus.FILLED:
                order.filled_quantity = update.filled_quantity
            committed_updates.append(update)
        _copy_shadow_into(portfolio, shadow_portfolio)

        return OpeningMatchBatchResult(
            batch_id=batch_id,
            session_id=session.session_id,
            opening_match_at=session.opening_match_at,
            fills=tuple(ledger.planned_fills),
            skipped_orders=tuple(skipped),
            order_updates=tuple(committed_updates),
            settlement_release=release,
        )

    @staticmethod
    def _sequenced(orders: Sequence[Order]) -> list[Order]:
        """Order the batch by stable ``submission_sequence``.

        Orders without a sequence cannot be ordered deterministically
        and are rejected instead of silently falling back to UUIDs or
        caller-supplied collection order.
        """

        sequenced: list[Order] = []
        seen_sequences: dict[int, Order] = {}
        for order in orders:
            if order.status is not OrderStatus.SUBMITTED:
                # Already terminal from an earlier batch; no fees, no cash,
                # no state change, and no re-processing here.
                continue
            if order.submission_sequence is None:
                raise SessionMatchError(
                    f"order {order.order_id} has no submission_sequence; "
                    "batch priority must not depend on collection order "
                    "or random identifiers"
                )
            duplicate = seen_sequences.get(order.submission_sequence)
            if duplicate is not None:
                # Duplicate ordinals would silently fall back to caller
                # collection order inside sort; reject the batch instead.
                raise SessionMatchError(
                    f"orders {duplicate.order_id} and {order.order_id} "
                    f"share submission_sequence {order.submission_sequence}; "
                    "submission sequences must be unique within a batch"
                )
            seen_sequences[order.submission_sequence] = order
            sequenced.append(order)
        return sorted(sequenced, key=lambda order: order.submission_sequence)

    @staticmethod
    def _validate_order(
        order: Order,
        session: SessionContext,
        policies: Mapping[UUID, InstrumentExecutionPolicy],
    ) -> tuple[str, bool] | None:
        """Return ``(reason, terminal)`` or ``None`` when admissible.

        Terminal outcomes already produce a planned state transition;
        non-terminal ones (an order whose window has not opened yet)
        keep the order in ``SUBMITTED`` with no fees, cash, position,
        or terminal-state change.
        """

        policy = policies.get(order.instrument_id)
        if policy is None:
            raise SessionMatchError(
                f"no frozen execution policy was supplied for instrument "
                f"{order.instrument_id}; matching cannot proceed"
            )
        opening = session.opening_match_at
        # Half-open [valid_from, valid_until): reaching valid_until
        # expires the order, arriving before valid_from keeps it.
        if order.valid_from is not None and opening < order.valid_from:
            return (MatchOrderReason.ORDER_NOT_YET_VALID, False)
        if order.valid_until is not None and opening >= order.valid_until:
            return (MatchOrderReason.ORDER_EXPIRED, True)
        type_reason = policy.validate_order_type(order.order_type)
        if type_reason is not None:
            return (type_reason, True)
        quantity = order.remaining_quantity
        if not _respects_precision(quantity, policy.quantity_precision):
            return (MatchOrderReason.ORDER_QUANTITY_PRECISION_INVALID, True)
        if quantity < policy.minimum_order_quantity:
            return (MatchOrderReason.ORDER_QUANTITY_BELOW_MINIMUM, True)
        if quantity % policy.lot_size != ZERO:
            return (MatchOrderReason.ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT, True)
        return None

    def _plan_order(
        self,
        *,
        order: Order,
        session: SessionContext,
        policy: InstrumentExecutionPolicy,
        ledger: MatchLedger,
        skipped: list[tuple[UUID, str]],
        updates: list[PlannedOrderUpdate],
    ) -> None:
        """Plan one fill against the ledger, or expire/reject the order."""

        def skip(reason: str, *, status: OrderStatus = OrderStatus.EXPIRED) -> None:
            skipped.append((order.order_id, reason))
            updates.append(
                PlannedOrderUpdate(
                    order_id=order.order_id,
                    instrument_id=order.instrument_id,
                    new_status=status,
                    reason=reason,
                    filled_quantity=order.filled_quantity,
                )
            )

        try:
            state = session.require_market_state(order.instrument_id)
        except ExecutionPolicyError:
            skip(MatchOrderReason.MARKET_STATE_MISSING)
            return
        market_reason = self._market_state_reason(order, state)
        if market_reason is not None:
            skip(market_reason)
            return

        slippage = self.slippage_model.apply(
            state.open_price,
            order.side,
            price_tick=state.price_tick,
        )
        if not self._price_is_rule_compliant(slippage.execution_price, policy):
            # A price violating the frozen tick/precision rules means the
            # session facts are corrupt; this aborts the batch instead of
            # silently expiring orders on bad data.
            raise SessionMatchError(
                f"execution price {slippage.execution_price} violates the "
                f"frozen precision/tick rules of instrument "
                f"{order.instrument_id}"
            )

        quantity = order.remaining_quantity
        gross_notional = (
            slippage.execution_price
            * quantity
            * policy.contract_multiplier
        )
        breakdown = self._calculate_fees(
            order.side, policy, gross_notional, quantity, self.fee_schedule
        )
        if isinstance(breakdown, str):
            skip(breakdown, status=OrderStatus.REJECTED)
            return
        available_quantity = ledger.available_quantities.get(
            order.instrument_id, ZERO
        )
        if order.side is OrderSide.SELL:
            if quantity > available_quantity:
                skip(MatchOrderReason.INSUFFICIENT_AVAILABLE_QUANTITY)
                return
            proceeds = gross_notional - breakdown.total
            # Reserve the sold units immediately so a later same-batch
            # sell of freshly planned units is impossible.
            ledger.available_quantities[order.instrument_id] = (
                available_quantity - quantity
            )
            ledger.reserved_quantities[order.instrument_id] = (
                ledger.reserved_quantities.get(order.instrument_id, ZERO)
                + quantity
            )
            # Sell proceeds (net of fees) fund later buys in this batch.
            ledger.available_cash += proceeds
        else:
            required = gross_notional + breakdown.total
            if required > ledger.available_cash:
                skip(MatchOrderReason.INSUFFICIENT_CASH)
                return
            ledger.available_cash -= required
            ledger.reserved_cash += required
        fill = self._build_fill(
            order=order,
            state=state,
            execution_price=slippage.execution_price,
            reference_price=slippage.reference_price,
            slippage_bps=slippage.slippage_bps,
            model_key=slippage.model_key,
            model_version=slippage.model_version,
            parameters=slippage.parameters,
            quantity=quantity,
            breakdown=breakdown,
            multiplier=policy.contract_multiplier,
            session=session,
        )
        ledger.planned_fills.append(fill)
        updates.append(
            PlannedOrderUpdate(
                order_id=order.order_id,
                instrument_id=order.instrument_id,
                new_status=OrderStatus.FILLED,
                reason=None,
                filled_quantity=order.filled_quantity + quantity,
            )
        )

    @staticmethod
    def _validate_settlement_plan(
        plan: DeferredSettlementPlan,
        session: SessionContext,
    ) -> None:
        """Bind every factory-produced plan to the matched session.

        A plan pointing at another calendar or another trade date would
        misplace the T+1 release; the batch is refused instead of
        accepting the drifted plan.
        """

        if not isinstance(plan, DeferredSettlementPlan):
            raise SessionMatchError(
                "settlement plan factory must return DeferredSettlementPlan "
                f"instances, got {type(plan).__name__}"
            )
        if plan.calendar_id != session.calendar_id:
            raise SessionMatchError(
                f"settlement plan calendar {plan.calendar_id} does not "
                f"match session calendar {session.calendar_id}"
            )
        if plan.trade_session != session.session_date:
            raise SessionMatchError(
                f"settlement plan trade_session {plan.trade_session} does "
                f"not match session date {session.session_date}"
            )

    @staticmethod
    def _market_state_reason(
        order: Order,
        state: MarketState,
    ) -> str | None:
        """Map explicit session facts to a stable no-fill reason."""

        if state.is_suspended:
            return MatchOrderReason.INSTRUMENT_SUSPENDED
        if not state.open_available or state.open_price is None:
            return MatchOrderReason.OPEN_UNAVAILABLE
        if order.side is OrderSide.BUY and not state.buy_allowed:
            return MatchOrderReason.BUY_UNAVAILABLE_AT_PRICE_LIMIT
        if order.side is OrderSide.SELL and not state.sell_allowed:
            return MatchOrderReason.SELL_UNAVAILABLE_AT_PRICE_LIMIT
        return None

    @staticmethod
    def _price_is_rule_compliant(
        price: Decimal,
        policy: InstrumentExecutionPolicy,
    ) -> bool:
        """Enforce precision and tick rules from the PIT policy alone."""

        if price <= ZERO:
            return False
        if not _respects_precision(price, policy.price_precision):
            return False
        return _is_tick_multiple(price, policy.price_tick)

    @staticmethod
    def _calculate_fees(
        side: OrderSide,
        policy: InstrumentExecutionPolicy,
        gross_notional: Decimal,
        quantity: Decimal,
        fee_schedule: object,
    ) -> FeeBreakdown | str:
        """Resolve category rules for the side, or return a reject reason."""

        try:
            rules = resolve_instrument_fee_rules(
                fee_schedule,
                fee_categories=policy.fee_categories,
                side=side,
                # Applicability-bearing rules filter strictly against the
                # instrument's resolved facts; a rule declaring facts the
                # policy does not carry can never apply (fail closed).
                context=dict(policy.fee_applicability_context),
            )
        except FeeRuleUnresolvedError:
            return MatchOrderReason.FEE_RULE_UNRESOLVED
        restricted = fee_snapshot_for_rules(fee_schedule, rules)
        calculator = FeeCalculator(restricted)
        try:
            return calculator.calculate(
                side=side,
                notional=gross_notional,
                currency=policy.currency,
                quantity=quantity,
            )
        except FeeRuleUnresolvedError:
            return MatchOrderReason.FEE_RULE_UNRESOLVED

    def _build_fill(
        self,
        *,
        order: Order,
        state: MarketState,
        execution_price: Decimal,
        reference_price: Decimal,
        slippage_bps: Decimal,
        model_key: str,
        model_version: int,
        parameters: Mapping[str, Decimal | str],
        quantity: Decimal,
        breakdown: FeeBreakdown,
        multiplier: Decimal,
        session: SessionContext,
    ) -> Fill:
        """Build one deterministic, fully audited planned fill fact."""

        fill_id = uuid5(
            batch_namespace(session.session_id),
            f"{order.order_id}:{state.timestamp.isoformat()}:{quantity}",
        )
        return Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            instrument_id=order.instrument_id,
            timestamp=state.timestamp,
            side=order.side,
            reference_price=reference_price,
            price=execution_price,
            quantity=quantity,
            fees=breakdown.total,
            currency=breakdown.currency,
            contract_multiplier=multiplier,
            fee_breakdown=breakdown,
            slippage_bps=slippage_bps,
            # Slippage amount is a cash value and therefore includes the
            # instrument's frozen contract multiplier.
            slippage_amount=(
                abs(execution_price - reference_price)
                * quantity
                * multiplier
            ),
            slippage_model_key=model_key,
            slippage_model_version=model_version,
            slippage_model_parameters=parameters,
        )

    @staticmethod
    def _apply_settlement_boundary(
        *,
        session: SessionContext,
        shadow_portfolio: PortfolioState,
        accounting: AccountingPolicy,
        boundary: SettlementBoundary | None,
    ) -> SettlementRelease | None:
        """Release due quantities strictly before the opening match."""

        uses_deferred = accounting._uses_deferred_settlement
        if not uses_deferred:
            # No deferred settlement: nothing to release, but a supplied
            # boundary still has to identify this session exactly.
            if boundary is not None:
                if boundary.phase is not SettlementBoundaryPhase.BEFORE_OPEN_MATCH:
                    raise SettlementBoundaryMismatchError(
                        f"boundary {boundary.boundary_id} phase "
                        f"{boundary.phase.value} cannot apply to an opening match"
                    )
                if boundary.session_id != session.session_id:
                    raise SettlementBoundaryMismatchError(
                        f"boundary {boundary.boundary_id} belongs to session "
                        f"{boundary.session_id}, not {session.session_id}"
                    )
            return None
        pending = accounting.pending_batches()
        due_here = any(
            batch.calendar_id == session.calendar_id
            and batch.settlement_session == session.session_date
            for batch in pending
        )
        if boundary is None:
            if uses_deferred and due_here:
                raise SessionMatchError(
                    f"session {session.session_id} has settlement batches "
                    "due but no settlement boundary was supplied; refusing "
                    "to match before the release"
                )
            return None
        if boundary.phase is not SettlementBoundaryPhase.BEFORE_OPEN_MATCH:
            raise SettlementBoundaryMismatchError(
                f"boundary {boundary.boundary_id} phase "
                f"{boundary.phase.value} cannot apply to an opening match"
            )
        if boundary.session_id != session.session_id:
            raise SettlementBoundaryMismatchError(
                f"boundary {boundary.boundary_id} belongs to session "
                f"{boundary.session_id}, not {session.session_id}"
            )
        if boundary.calendar_id != session.calendar_id:
            raise SettlementBoundaryMismatchError(
                f"boundary {boundary.boundary_id} calendar "
                f"{boundary.calendar_id} differs from session calendar "
                f"{session.calendar_id}"
            )
        if boundary.session_date != session.session_date:
            raise SettlementBoundaryMismatchError(
                f"boundary {boundary.boundary_id} date "
                f"{boundary.session_date} differs from session date "
                f"{session.session_date}"
            )
        released = accounting.settle_pending_before_open_match(
            shadow_portfolio,
            calendar_id=session.calendar_id,
            session_date=session.session_date,
            boundary_id=str(boundary.boundary_id),
        )
        history = accounting.releases()
        return history[-1] if history else None


def batch_namespace(session_id: UUID) -> UUID:
    """Deterministic UUIDv5 namespace bound to one session."""

    return uuid5(session_id, "opening-match-fills")
