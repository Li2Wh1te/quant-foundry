"""Deterministic bar-open matching: sells first, budgeted buys second.

This module implements the formal opening-match contract for one bar:

* sells are processed one by one in the stable order
  ``instrument_id ASC, submission_sequence ASC, order_id ASC``; every fill
  immediately deducts sellable quantity and adds net proceeds, so a later
  sell always observes the earlier one's updates;
* buy funding is checked against the *slipped execution price* and an
  explicit stateless :class:`FeeQuote` — never a reference price or an
  implicit zero fee;
* when cash falls short, every buy is scaled once by the pro-rata factor
  ``alpha = C / S`` and rounded onto the lot grid; if the rounded demand
  still exceeds cash, orders are reduced one lot at a time in stable
  order with the fee quote recomputed after every step.  The pro-rata
  pass never runs twice;
* a buy scaled down to zero never becomes an order and never receives a
  second budget allocation: it is reported as a :class:`BuyAllocationResult`
  with ``allocation_status = not_submitted``;
* every order leaves the match in a terminal state (``filled``,
  ``partially_filled``, ``expired``, ``rejected``) — ``submitted`` is a
  runtime-only intermediate state — and the unfilled remainder carries its
  own ``remaining_status`` and ``remaining_reason_code``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.backtesting.accounting import Fill, OrderSide
from app.backtesting.domain import ZERO, DomainValidationError, _decimal, _non_negative
from app.backtesting.execution import MarketState, Order, OrderStatus
from app.backtesting.fees import (
    FeeBreakdown,
    FeeCalculator,
    FeeRuleUnresolvedError,
    fee_snapshot_for_rules,
    resolve_instrument_fee_rules,
)
from app.backtesting.reason_codes import MatchReasonCode, ResultStage, StructuredReason
from app.backtesting.session_matching import MatchLedger
from app.backtesting.slippage import (
    SlippageError,
    SlippageModel,
    SlippageResult,
)
from app.strategy_protocol.interpretation import (
    InstrumentExecutionFacts,
    SellOddLotPolicy,
)

__all__ = [
    "ALLOCATION_PHASE_LOT_REDUCTION",
    "ALLOCATION_PHASE_PRO_RATA",
    "ALLOCATION_STATUS_ALLOCATED",
    "ALLOCATION_STATUS_NOT_SUBMITTED",
    "BarOpenMatchResult",
    "BarOpenMatchingError",
    "BarOpenMatchingModel",
    "BuyAllocationResult",
    "FeeQuote",
    "FeeQuoteProvider",
    "OrderUpdateRecord",
    "SkippedOrderRecord",
    "StatelessFeeQuoteProvider",
]


ALLOCATION_PHASE_PRO_RATA = "pro_rata"
ALLOCATION_PHASE_LOT_REDUCTION = "lot_reduction"
ALLOCATION_STATUS_ALLOCATED = "allocated"
ALLOCATION_STATUS_NOT_SUBMITTED = "not_submitted"


class BarOpenMatchingError(DomainValidationError):
    """Raised when the match inputs violate the matching contract."""


def _order_sort_key(order: Order) -> tuple[str, int, str]:
    """Stable batch order shared by the sell and buy stages.

    Matching results never depend on collection order: instrument id
    first, then the in-run submission sequence, then the order identity
    as the last tie breaker.
    """

    return (
        str(order.instrument_id),
        order.submission_sequence if order.submission_sequence is not None else 0,
        str(order.order_id),
    )


@dataclass(frozen=True, slots=True)
class FeeQuote:
    """Immutable, deterministic cost quote for one candidate quantity.

    A provider is a pure function of its inputs: it must not read mutable
    global state, depend on earlier calls, use randomness or the wall
    clock, or assume the fee function is linear in quantity.
    """

    total: Decimal
    currency: str
    components: Mapping[str, Decimal] = MappingProxyType({})
    breakdown: FeeBreakdown | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", _decimal(self.total, "total"))
        if self.total < ZERO:
            raise BarOpenMatchingError("fee quote total must be non-negative")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise BarOpenMatchingError(
                "fee quote currency must be non-blank text"
            )
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(
            self, "components", MappingProxyType(dict(self.components))
        )


class FeeQuoteProvider:
    """Structural contract for stateless cost quoting inside a match.

    Implementations must return identical quotes for identical inputs and
    must support the full quantity, the pro-rata-scaled quantity, the
    per-lot reduced quantity, and the finally filled quantity alike.
    """

    def quote(
        self,
        *,
        side: OrderSide | str,
        quantity: Decimal,
        execution_price: Decimal,
        contract_multiplier: Decimal,
        currency: str,
        instrument_context: object,
        order_context: Mapping[str, object],
    ) -> FeeQuote:
        raise NotImplementedError


class StatelessFeeQuoteProvider(FeeQuoteProvider):
    """Adapter resolving a frozen ``FeeSchedule`` into :class:`FeeQuote`.

    Resolution is fail-closed: every fee category declared by the
    instrument context must resolve to an applicable rule, otherwise
    :class:`FeeRuleUnresolvedError` propagates and the matcher surfaces
    ``COST_QUOTE_UNAVAILABLE`` instead of silently charging zero.
    """

    def __init__(self, *, fee_schedule: object) -> None:
        self._fee_schedule = fee_schedule

    def quote(
        self,
        *,
        side: OrderSide | str,
        quantity: Decimal,
        execution_price: Decimal,
        contract_multiplier: Decimal,
        currency: str,
        instrument_context: object,
        order_context: Mapping[str, object],
    ) -> FeeQuote:
        categories = getattr(instrument_context, "fee_categories", None)
        applicability = getattr(
            instrument_context, "fee_applicability_context", None
        ) or {}
        rules = resolve_instrument_fee_rules(
            self._fee_schedule,
            fee_categories=categories,
            side=side,
            context=dict(applicability),
        )
        calculator = FeeCalculator(
            fee_snapshot_for_rules(self._fee_schedule, rules)
        )
        notional = execution_price * quantity * contract_multiplier
        breakdown = calculator.calculate(
            side=side,
            notional=notional,
            currency=currency,
            quantity=quantity,
        )
        return FeeQuote(
            total=breakdown.total,
            currency=breakdown.currency,
            components={
                component.rule_key: component.amount
                for component in breakdown.components
            },
            breakdown=breakdown,
        )


@dataclass(frozen=True, slots=True)
class OrderUpdateRecord:
    """Final, fully-attributed outcome of one matched order.

    The frozen contract fields ``reason_code`` and
    ``remaining_reason_code`` carry the uniform ``stage/code/details``
    structure: the main reason explains the order outcome,
    ``remaining_reason_code`` explains only why the unfilled remainder
    did not trade.
    """

    order_id: UUID
    intent_id: UUID
    instrument_id: UUID
    side: OrderSide
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    order_status: OrderStatus
    remaining_status: str
    reason_code: StructuredReason | None
    remaining_reason_code: StructuredReason | None


@dataclass(frozen=True, slots=True)
class SkippedOrderRecord:
    """Compact record for an order that traded nothing in this match."""

    order_id: UUID
    instrument_id: UUID
    side: OrderSide
    order_status: OrderStatus
    reason_code: StructuredReason


@dataclass(frozen=True, slots=True)
class BuyAllocationResult:
    """Cash-allocation outcome of one buy order.

    Records exist for every surviving buy; ``allocated_quantity == 0``
    marks a buy that never became an order.  Zero allocations are never
    re-funded from other buys' budgets (``reallocated`` stays ``false``).
    """

    intent_id: UUID
    instrument_id: UUID
    requested_quantity: Decimal
    allocated_quantity: Decimal
    unsubmitted_quantity: Decimal
    full_cash_required: Decimal
    pro_rata_budget: Decimal
    allocation_phase: str | None
    allocation_status: str
    allocation_reason_code: StructuredReason | None
    reallocated: bool = False


@dataclass(frozen=True, slots=True)
class BarOpenMatchResult:
    """Deterministic output of one bar-open match.

    ``unsubmitted_order_ids`` lists buy candidates whose cash allocation
    was scaled to zero: they never become executable orders and the caller
    must atomically remove them from the run's order set so a later batch
    cannot re-match them.
    """

    fills: tuple[Fill, ...]
    order_updates: tuple[OrderUpdateRecord, ...]
    buy_allocation_results: tuple[BuyAllocationResult, ...]
    skipped_or_rejected_orders: tuple[SkippedOrderRecord, ...]
    unsubmitted_order_ids: tuple[UUID, ...] = ()


@dataclass(slots=True)
class _Outcome:
    """Mutable working state for one order inside one match."""

    order: Order
    facts: InstrumentExecutionFacts
    requested_quantity: Decimal
    position_total: Decimal | None = None
    # Planned sell quantity derived from live availability; set during
    # classification and consumed by the pricing/fill stage.
    candidate: Decimal | None = None
    state: MarketState | None = None
    execution_price: Decimal | None = None
    slippage: SlippageResult | None = None
    filled: Decimal = ZERO
    order_status: OrderStatus = OrderStatus.REJECTED
    reason_code: str | None = None
    reason_stage: str = ResultStage.MATCHING.value
    remaining_status: str = "not_executed"
    remaining_reason_code: str | None = None
    details: Mapping[str, object] | None = None
    # Set once the buy passed preflight and its full-quantity cost quote
    # resolved: only such buys participate in the cash allocation and
    # therefore earn a BuyAllocationResult.
    in_cash_allocation: bool = False
    # Buy-allocation bookkeeping.
    full_cash_required: Decimal = ZERO
    pro_rata_budget: Decimal = ZERO
    allocated: Decimal = ZERO
    allocation_phase: str | None = None
    allocation_reason_code: str | None = None


def _floor_to_grid(quantity: Decimal, unit: Decimal) -> Decimal:
    """Floor a positive quantity onto multiples of a positive unit."""

    return (quantity / unit).to_integral_value(rounding=ROUND_FLOOR) * unit


def _respects_precision(value: Decimal, precision: int) -> bool:
    """Whether ``value`` is exactly representable at ``precision`` digits."""

    digits = value.normalize().as_tuple()
    if not isinstance(digits.exponent, int):
        return False
    return digits.exponent >= -precision


class BarOpenMatchingModel:
    """Plan one deterministic opening match over a detached ledger.

    The model never touches the formal portfolio: it mutates only the
    supplied :class:`~app.backtesting.session_matching.MatchLedger`, so
    the caller stays free to commit or discard the plan atomically.
    """

    model_key = "bar_open_match"
    model_version = 1

    def __init__(
        self,
        *,
        slippage_model: SlippageModel,
        fee_quote_provider: FeeQuoteProvider,
    ) -> None:
        self._slippage_model = slippage_model
        self._fee_quote_provider = fee_quote_provider

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def match(
        self,
        *,
        orders: Sequence[Order],
        market_states: Mapping[UUID, MarketState],
        ledger: MatchLedger,
        facts: Mapping[UUID, InstrumentExecutionFacts],
        match_at: datetime,
        position_quantities: Mapping[UUID, Decimal] | None = None,
    ) -> BarOpenMatchResult:
        """Match one batch of one-shot market orders at the bar open.

        ``position_quantities`` maps instruments to their *total* held
        quantity (not just the settlement-available part); full-liquidation
        odd-lot exemptions are judged against it.  A missing entry means
        the total is unknown and the exemption fails closed.
        """

        totals = {
            instrument_id: _non_negative(quantity, "position_quantity")
            for instrument_id, quantity in dict(
                position_quantities or {}
            ).items()
        }
        sequenced = self._sequenced(orders)
        outcomes: dict[UUID, _Outcome] = {}
        for order in sequenced:
            facts_entry = facts.get(order.instrument_id)
            if facts_entry is None:
                raise BarOpenMatchingError(
                    f"no instrument execution facts were supplied for "
                    f"{order.instrument_id}; matching cannot proceed"
                )
            outcomes[order.order_id] = _Outcome(
                order=order,
                facts=facts_entry,
                requested_quantity=order.remaining_quantity,
                position_total=totals.get(order.instrument_id),
            )

        # Sells must be classified and filled one at a time, strictly in
        # order: each sell's candidate derivation and ledger update must
        # observe the previous sell's deduction.  Buys are only classified
        # here; their cash allocation runs after every sell completed.
        buys: list[_Outcome] = []
        for order in sequenced:
            outcome = outcomes[order.order_id]
            if order.side is OrderSide.SELL:
                self._classify_sell(
                    outcome=outcome,
                    market_states=market_states,
                    match_at=match_at,
                    ledger=ledger,
                )
                self._process_sell(outcome, ledger)
            else:
                self._classify(
                    outcome=outcome,
                    market_states=market_states,
                    match_at=match_at,
                )
                buys.append(outcome)
        self._allocate_buys(buys, ledger)

        return self._build_result(outcomes, ledger)

    # ------------------------------------------------------------------
    # Batch admission
    # ------------------------------------------------------------------

    @staticmethod
    def _sequenced(orders: Sequence[Order]) -> list[Order]:
        """Validate sequence and identity uniqueness; return stable order."""

        sequenced: list[Order] = []
        seen: dict[int, UUID] = {}
        seen_order_ids: set[UUID] = set()
        for order in orders:
            if order.status is not OrderStatus.SUBMITTED:
                continue
            if order.submission_sequence is None:
                raise BarOpenMatchingError(
                    f"order {order.order_id} has no submission_sequence; "
                    "matching priority must not depend on collection order"
                )
            duplicate = seen.get(order.submission_sequence)
            if duplicate is not None:
                raise BarOpenMatchingError(
                    f"orders {duplicate} and {order.order_id} share "
                    f"submission_sequence {order.submission_sequence}; "
                    "sequences must be unique within a run"
                )
            if order.order_id in seen_order_ids:
                # The same order id twice would execute the same outcome
                # twice while the result dict keeps only one record —
                # silent double fills.  Fail the batch instead.
                raise BarOpenMatchingError(
                    f"order_id {order.order_id} appears more than once "
                    "in the batch; order identities must be unique "
                    "within a run"
                )
            seen[order.submission_sequence] = order.order_id
            seen_order_ids.add(order.order_id)
            sequenced.append(order)
        return sorted(sequenced, key=_order_sort_key)

    # ------------------------------------------------------------------
    # Shared classification
    # ------------------------------------------------------------------

    def _classify(
        self,
        *,
        outcome: _Outcome,
        market_states: Mapping[UUID, MarketState],
        match_at: datetime,
    ) -> None:
        """Apply order and market preconditions before any pricing work.

        Buy-side evaluation order: order type and validity window, then
        the order's own quantity rules, then market-state gates, then
        slippage — pricing comes strictly last.

        Failures here leave the outcome ``rejected`` with
        ``remaining_status = not_executed``, except validity-window cases
        which expire a legal order that simply cannot trade today.
        """

        order = outcome.order
        if order.order_type.value != "market":
            outcome.reason_code = MatchReasonCode.ORDER_TYPE_NOT_SUPPORTED.value
            return
        if order.valid_from is not None and match_at < order.valid_from:
            outcome.order_status = OrderStatus.EXPIRED
            outcome.reason_code = MatchReasonCode.ORDER_NOT_YET_VALID.value
            outcome.remaining_status = "terminal_unfilled"
            outcome.remaining_reason_code = outcome.reason_code
            return
        if order.valid_until is not None and match_at >= order.valid_until:
            outcome.order_status = OrderStatus.EXPIRED
            outcome.reason_code = MatchReasonCode.ORDER_EXPIRED.value
            outcome.remaining_status = "terminal_unfilled"
            outcome.remaining_reason_code = outcome.reason_code
            return

        # Quantity legality precedes any market or cost work: an illegal
        # order is rejected with its own rule code even when market data
        # or slippage would also fail.
        self._classify_buy_quantity(outcome)
        if outcome.reason_code is not None:
            return

        state = market_states.get(order.instrument_id)
        gate_reason = self._market_gate_reason(order, state)
        if gate_reason is not None:
            outcome.reason_code = gate_reason
            return

        outcome.state = state
        try:
            slippage = self._slippage_model.apply(
                state.open_price,
                order.side,
                price_tick=state.price_tick,
            )
        except SlippageError as exc:
            # A slippage failure is a market/cost precondition failure:
            # surface it as a structured rejection instead of aborting the
            # whole batch with an exception.
            outcome.reason_stage = ResultStage.SLIPPAGE.value
            outcome.reason_code = exc.reason_code.value
            outcome.details = dict(exc.details)
            return
        outcome.execution_price = slippage.execution_price
        outcome.slippage = slippage

    def _classify_sell(
        self,
        *,
        outcome: _Outcome,
        market_states: Mapping[UUID, MarketState],
        match_at: datetime,
        ledger: MatchLedger,
    ) -> None:
        """Classify one sell against its live sellable quantity.

        The frozen sell pipeline branches on real-time availability before
        any market or pricing work:

        * ``requested <= available``: the request's own quantity must be
          legal; a violation rejects the order with ``ORDER_QUANTITY_*`` /
          ``ODD_LOT_*`` codes;
        * ``requested > available``: an availability shortfall — the legal
          candidate quantity is derived from the sellable part instead of
          rejecting the order, and zero candidates expire with their
          ``AVAILABLE_*`` reason.

        Only a positive candidate proceeds to the market-state gates and
        the slippage model.
        """

        order = outcome.order
        if order.order_type.value != "market":
            outcome.reason_code = MatchReasonCode.ORDER_TYPE_NOT_SUPPORTED.value
            return
        if order.valid_from is not None and match_at < order.valid_from:
            outcome.order_status = OrderStatus.EXPIRED
            outcome.reason_code = MatchReasonCode.ORDER_NOT_YET_VALID.value
            outcome.remaining_status = "terminal_unfilled"
            outcome.remaining_reason_code = outcome.reason_code
            return
        if order.valid_until is not None and match_at >= order.valid_until:
            outcome.order_status = OrderStatus.EXPIRED
            outcome.reason_code = MatchReasonCode.ORDER_EXPIRED.value
            outcome.remaining_status = "terminal_unfilled"
            outcome.remaining_reason_code = outcome.reason_code
            return

        facts_entry = outcome.facts
        requested = outcome.requested_quantity
        available = ledger.available_quantities.get(order.instrument_id, ZERO)
        shortfall = requested > available

        if shortfall:
            full_liquidation = (
                outcome.position_total is not None
                and available == outcome.position_total
                and available > ZERO
            )
            candidate, reject_reason = self._capped_candidate(
                base=available,
                facts_entry=facts_entry,
                full_liquidation=full_liquidation,
            )
            if candidate == ZERO:
                outcome.order_status = OrderStatus.EXPIRED
                outcome.reason_code = reject_reason
                outcome.remaining_status = (
                    "terminal_unfilled"
                    if reject_reason
                    == MatchReasonCode.INSUFFICIENT_AVAILABLE_QUANTITY.value
                    else "terminal_unorderable"
                )
                outcome.remaining_reason_code = reject_reason
                outcome.details = {
                    "requested_quantity": str(requested),
                    "available_quantity": str(available),
                }
                return
        else:
            self._classify_sell_quantity(outcome)
            if outcome.reason_code is not None:
                return
            candidate = requested
        outcome.candidate = candidate

        state = market_states.get(order.instrument_id)
        gate_reason = self._market_gate_reason(order, state)
        if gate_reason is not None:
            outcome.reason_code = gate_reason
            return

        outcome.state = state
        try:
            slippage = self._slippage_model.apply(
                state.open_price,
                order.side,
                price_tick=state.price_tick,
            )
        except SlippageError as exc:
            outcome.reason_stage = ResultStage.SLIPPAGE.value
            outcome.reason_code = exc.reason_code.value
            outcome.details = {
                "candidate_quantity": str(candidate),
                **dict(exc.details),
            }
            return
        outcome.execution_price = slippage.execution_price
        outcome.slippage = slippage

    @staticmethod
    def _market_gate_reason(
        order: Order, state: MarketState | None
    ) -> str | None:
        """Map explicit session facts to stable no-fill reject codes."""

        if state is None:
            return MatchReasonCode.MARKET_STATE_MISSING.value
        if state.is_suspended:
            return MatchReasonCode.INSTRUMENT_SUSPENDED.value
        if not state.open_available or state.open_price is None:
            return MatchReasonCode.OPEN_UNAVAILABLE.value
        if order.side is OrderSide.BUY and not state.buy_allowed:
            return MatchReasonCode.BUY_UNAVAILABLE_AT_PRICE_LIMIT.value
        if order.side is OrderSide.SELL and not state.sell_allowed:
            return MatchReasonCode.SELL_UNAVAILABLE_AT_PRICE_LIMIT.value
        return None

    def _classify_buy_quantity(self, outcome: _Outcome) -> None:
        """Reject buys whose own quantity violates the instrument rules."""

        facts_entry = outcome.facts
        quantity = outcome.requested_quantity
        if not _respects_precision(quantity, facts_entry.order_precision):
            outcome.reason_code = (
                MatchReasonCode.ORDER_QUANTITY_PRECISION_INVALID.value
            )
        elif quantity % facts_entry.lot_size != ZERO:
            outcome.reason_code = (
                MatchReasonCode.ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT.value
            )
        elif quantity < facts_entry.minimum_order_quantity:
            outcome.reason_code = (
                MatchReasonCode.ORDER_QUANTITY_BELOW_MINIMUM.value
            )

    def _classify_sell_quantity(self, outcome: _Outcome) -> None:
        """Validate the sell request's own quantity before any matching.

        The boundary with the availability stage is fixed: an illegal
        *request* is a rejected order carrying ``ORDER_QUANTITY_*`` /
        ``ODD_LOT_*`` codes, while ``AVAILABLE_*`` codes are reserved for
        candidates derived from capped availability.  Odd-lot exemptions
        are judged against the total held quantity, never against what
        happens to be settlement-available today.

        An exemption waives exactly its own rule; every independent rule
        (precision, lot, minimum) is still evaluated, in that fixed
        order, even after an earlier exemption applied.
        """

        facts_entry = outcome.facts
        quantity = outcome.requested_quantity
        total = outcome.position_total
        full_liquidation = (
            total is not None and quantity == total and quantity > ZERO
        )
        precision_exempt = full_liquidation and (
            facts_entry.full_liquidation_bypasses_order_precision
        )
        lot_exempt = (
            full_liquidation and facts_entry.full_liquidation_bypasses_lot_size
        ) or (
            facts_entry.sell_odd_lot_policy is SellOddLotPolicy.ALLOW_ODD_LOT
            and facts_entry.odd_lot_bypasses_lot_size
        )

        if not precision_exempt and not _respects_precision(
            quantity, facts_entry.order_precision
        ):
            outcome.reason_code = (
                MatchReasonCode.ORDER_QUANTITY_PRECISION_INVALID.value
            )
        elif not lot_exempt and quantity % facts_entry.lot_size != ZERO:
            outcome.reason_code = self._odd_lot_reason(
                facts_entry, full_liquidation
            )
        elif quantity < facts_entry.minimum_order_quantity:
            outcome.reason_code = (
                MatchReasonCode.ORDER_QUANTITY_BELOW_MINIMUM.value
            )

    @staticmethod
    def _odd_lot_reason(
        facts_entry: InstrumentExecutionFacts, full_liquidation: bool
    ) -> str:
        """Pick the precise odd-lot rejection code for a non-lot sale."""

        if facts_entry.sell_odd_lot_policy is SellOddLotPolicy.ALLOW_ODD_LOT:
            # The policy permits odd sales in principle but the explicit
            # lot-size exemption flag was not declared.
            return MatchReasonCode.ODD_LOT_LOT_SIZE_EXEMPTION_MISSING.value
        if full_liquidation and (
            facts_entry.sell_odd_lot_policy
            is SellOddLotPolicy.ALLOW_FULL_LIQUIDATION_ODD_LOT
        ):
            return MatchReasonCode.ODD_LOT_LOT_SIZE_EXEMPTION_MISSING.value
        if not full_liquidation and (
            facts_entry.sell_odd_lot_policy
            is SellOddLotPolicy.ALLOW_FULL_LIQUIDATION_ODD_LOT
        ):
            return MatchReasonCode.ODD_LOT_NOT_ALLOWED.value
        return MatchReasonCode.ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT.value

    # ------------------------------------------------------------------
    # Sell stage
    # ------------------------------------------------------------------

    def _process_sell(self, outcome: _Outcome, ledger: MatchLedger) -> None:
        """Price and fill one classified sell against the ledger.

        Classification already derived the legal candidate quantity from
        live availability; this stage only quotes, checks net proceeds,
        applies the fill, and updates the ledger immediately so the next
        sell observes it.
        """

        if outcome.reason_code is not None:
            return
        order = outcome.order
        facts_entry = outcome.facts
        requested = outcome.requested_quantity
        candidate = outcome.candidate
        assert candidate is not None
        available = ledger.available_quantities.get(order.instrument_id, ZERO)
        shortfall = requested > available

        assert outcome.execution_price is not None
        execution_price = outcome.execution_price
        gross = execution_price * candidate * facts_entry.contract_multiplier
        try:
            quote = self._quote(
                side=order.side,
                quantity=candidate,
                execution_price=execution_price,
                facts_entry=facts_entry,
                currency=ledger.currency,
            )
        except FeeRuleUnresolvedError:
            outcome.reason_code = MatchReasonCode.COST_QUOTE_UNAVAILABLE.value
            return
        net_proceeds = gross - quote.total
        if net_proceeds < ZERO:
            # A negative-net sale never trades: no quantity deduction, no
            # fee, no cash movement, and no second partial-fill attempt.
            outcome.reason_code = MatchReasonCode.NEGATIVE_NET_PROCEEDS.value
            outcome.details = {
                "candidate_quantity": str(candidate),
                "requested_quantity": str(requested),
                "available_shortfall": shortfall,
                "gross_proceeds": str(gross),
                "fee_total": str(quote.total),
                "net_proceeds": str(net_proceeds),
            }
            return

        fill = self._build_fill(
            order=order,
            outcome=outcome,
            quantity=candidate,
            quote=quote,
        )
        ledger.planned_fills.append(fill)
        ledger.available_quantities[order.instrument_id] = available - candidate
        ledger.available_cash += net_proceeds
        outcome.filled = candidate
        if candidate == requested:
            outcome.order_status = OrderStatus.FILLED
            outcome.remaining_status = "none"
        else:
            # A partial sell is only reachable through an availability
            # shortfall; the remainder is settlement-blocked, not broken.
            outcome.order_status = OrderStatus.PARTIALLY_FILLED
            outcome.remaining_status = "terminal_unfilled"
            outcome.reason_code = (
                MatchReasonCode.INSUFFICIENT_AVAILABLE_QUANTITY.value
            )
            outcome.details = {
                "requested_quantity": str(requested),
                "candidate_quantity": str(candidate),
                "available_quantity": str(available),
            }
            outcome.remaining_reason_code = outcome.reason_code

    def _capped_candidate(
        self,
        *,
        base: Decimal,
        facts_entry: InstrumentExecutionFacts,
        full_liquidation: bool,
    ) -> tuple[Decimal, str]:
        """Derive the largest legal sellable quantity within ``base``.

        Only availability-capped sells reach this path, so all rejection
        codes are ``AVAILABLE_*`` / odd-lot variants.  Precision is
        evaluated before any lot flooring so a precision-corrupt
        availability is reported as such instead of being silently
        rounded onto the grid.
        """

        if base <= ZERO:
            return ZERO, MatchReasonCode.INSUFFICIENT_AVAILABLE_QUANTITY.value
        if base < facts_entry.minimum_order_quantity:
            return ZERO, MatchReasonCode.AVAILABLE_QUANTITY_BELOW_MINIMUM.value

        lot_exempt = (
            full_liquidation and facts_entry.full_liquidation_bypasses_lot_size
        ) or (
            facts_entry.sell_odd_lot_policy is SellOddLotPolicy.ALLOW_ODD_LOT
            and facts_entry.odd_lot_bypasses_lot_size
        )
        precision_exempt = full_liquidation and (
            facts_entry.full_liquidation_bypasses_order_precision
        )

        if not precision_exempt and not _respects_precision(
            base, facts_entry.order_precision
        ):
            reason = MatchReasonCode.AVAILABLE_QUANTITY_PRECISION_INVALID.value
            return (ZERO, reason)

        candidate = base
        if not lot_exempt and candidate % facts_entry.lot_size != ZERO:
            if facts_entry.sell_odd_lot_policy is SellOddLotPolicy.STRICT_LOT:
                lot_reason = MatchReasonCode.AVAILABLE_QUANTITY_NOT_MULTIPLE_OF_LOT.value
            elif (
                facts_entry.sell_odd_lot_policy is SellOddLotPolicy.ALLOW_ODD_LOT
            ):
                lot_reason = (
                    MatchReasonCode.ODD_LOT_LOT_SIZE_EXEMPTION_MISSING.value
                )
            elif full_liquidation:
                lot_reason = (
                    MatchReasonCode.ODD_LOT_LOT_SIZE_EXEMPTION_MISSING.value
                )
            else:
                lot_reason = MatchReasonCode.AVAILABLE_ODD_LOT_NOT_ALLOWED.value
            floored = _floor_to_grid(candidate, facts_entry.lot_size)
            if floored >= facts_entry.minimum_order_quantity and floored > ZERO:
                # Trade the legal portion of the capped availability; the
                # remainder is reported through remaining_reason_code.
                return (floored, lot_reason)
            return (ZERO, lot_reason)
        return (candidate, "")

    # ------------------------------------------------------------------
    # Buy stage
    # ------------------------------------------------------------------

    def _allocate_buys(
        self, outcomes: list[_Outcome], ledger: MatchLedger
    ) -> None:
        """Run the one-pass pro-rata plus per-lot buy allocator."""

        active: list[_Outcome] = []
        for outcome in outcomes:
            if outcome.reason_code is not None:
                continue
            full_required = self._safe_cash_required(
                outcome, outcome.requested_quantity, ledger
            )
            if full_required is None:
                continue  # outcome now carries COST_QUOTE_UNAVAILABLE
            outcome.full_cash_required = full_required
            # From here on the buy participates in the cash-allocation
            # stage and its result is reported as an allocation record.
            outcome.in_cash_allocation = True
            active.append(outcome)

        cash = ledger.available_cash
        total_required = sum(
            (outcome.full_cash_required for outcome in active), ZERO
        )
        if total_required > ZERO:
            for outcome in active:
                outcome.pro_rata_budget = (
                    cash * outcome.full_cash_required / total_required
                )

        if cash >= total_required:
            for outcome in active:
                outcome.allocated = outcome.requested_quantity
        else:
            alpha = cash / total_required
            for outcome in active:
                scaled = _floor_to_grid(
                    alpha * outcome.requested_quantity,
                    outcome.facts.lot_size,
                )
                if scaled < outcome.facts.minimum_order_quantity:
                    scaled = ZERO
                outcome.allocated = scaled
                outcome.allocation_phase = ALLOCATION_PHASE_PRO_RATA
                if scaled == ZERO:
                    outcome.allocation_reason_code = (
                        MatchReasonCode.CASH_ALLOCATION_PRO_RATA_ROUNDED_TO_ZERO.value
                    )
            if self._safe_total_cost(active, ledger) > cash:
                self._reduce_by_lot(active, ledger)

        for outcome in active:
            if outcome.allocated != ZERO:
                self._fill_buy(outcome, ledger)

    def _safe_total_cost(
        self, outcomes: Sequence[_Outcome], ledger: MatchLedger
    ) -> Decimal:
        """Total cash demand of the current allocations.

        A quote failing mid-reduction rejects that buy (allocated to zero)
        and excludes it from the total; the remaining orders continue on a
        deterministic path.
        """

        total = ZERO
        for outcome in outcomes:
            if outcome.allocated == ZERO:
                continue
            cost = self._safe_cash_required(outcome, outcome.allocated, ledger)
            if cost is None:
                outcome.allocated = ZERO
                outcome.allocation_reason_code = (
                    MatchReasonCode.CASH_ALLOCATION_PRO_RATA_ROUNDED_TO_ZERO.value
                )
                continue
            total += cost
        return total

    def _safe_cash_required(
        self, outcome: _Outcome, quantity: Decimal, ledger: MatchLedger
    ) -> Decimal | None:
        """Full cash need at the slipped price with an explicit quote.

        Returns ``None`` and marks the outcome ``COST_QUOTE_UNAVAILABLE``
        when the frozen schedule cannot resolve a rule for the request.
        """

        if quantity == ZERO:
            return ZERO
        assert outcome.execution_price is not None
        gross = (
            outcome.execution_price
            * quantity
            * outcome.facts.contract_multiplier
        )
        try:
            quote = self._quote(
                side=outcome.order.side,
                quantity=quantity,
                execution_price=outcome.execution_price,
                facts_entry=outcome.facts,
                currency=ledger.currency,
            )
        except FeeRuleUnresolvedError:
            outcome.reason_code = MatchReasonCode.COST_QUOTE_UNAVAILABLE.value
            return None
        return gross + quote.total

    def _reduce_by_lot(
        self, outcomes: list[_Outcome], ledger: MatchLedger
    ) -> None:
        """Reduce allocations one lot at a time until demand fits cash.

        Scans run in the stable batch order; every decrement recomputes
        that order's fee quote and the running total, and a buy reduced to
        zero keeps its distinct lot-phase reason code.
        """

        cash = ledger.available_cash
        total = self._safe_total_cost(outcomes, ledger)
        progress = True
        while total > cash and progress:
            progress = False
            for outcome in outcomes:
                if total <= cash:
                    break
                if outcome.allocated == ZERO:
                    continue
                reduced = outcome.allocated - outcome.facts.lot_size
                if reduced < ZERO:
                    reduced = ZERO
                outcome.allocated = reduced
                outcome.allocation_phase = ALLOCATION_PHASE_LOT_REDUCTION
                total = self._safe_total_cost(outcomes, ledger)
                progress = True
                if reduced == ZERO:
                    outcome.allocation_reason_code = (
                        MatchReasonCode.CASH_ALLOCATION_LOT_REDUCTION_TO_ZERO.value
                    )

    def _fill_buy(self, outcome: _Outcome, ledger: MatchLedger) -> None:
        """Fill one funded buy and record its final outcome."""

        order = outcome.order
        quantity = outcome.allocated
        assert outcome.execution_price is not None
        gross = (
            outcome.execution_price
            * quantity
            * outcome.facts.contract_multiplier
        )
        try:
            quote = self._quote(
                side=order.side,
                quantity=quantity,
                execution_price=outcome.execution_price,
                facts_entry=outcome.facts,
                currency=ledger.currency,
            )
        except FeeRuleUnresolvedError:
            outcome.reason_code = MatchReasonCode.COST_QUOTE_UNAVAILABLE.value
            outcome.allocated = ZERO
            return
        total_cost = gross + quote.total
        if total_cost > ledger.available_cash:
            # The allocator guarantees feasibility; reaching this branch
            # means the cost model changed mid-match, which fails closed.
            outcome.allocated = ZERO
            outcome.allocation_phase = ALLOCATION_PHASE_LOT_REDUCTION
            outcome.allocation_reason_code = (
                MatchReasonCode.CASH_ALLOCATION_LOT_REDUCTION_TO_ZERO.value
            )
            outcome.reason_code = MatchReasonCode.INSUFFICIENT_CASH.value
            return
        fill = self._build_fill(
            order=order, outcome=outcome, quantity=quantity, quote=quote
        )
        ledger.planned_fills.append(fill)
        ledger.available_cash -= total_cost
        outcome.filled = quantity
        if quantity == outcome.requested_quantity:
            outcome.order_status = OrderStatus.FILLED
            outcome.remaining_status = "none"
        else:
            # One-shot matching: the unfilled remainder expires instead of
            # rolling to a later session, so it carries its own reason
            # code while the order-level reason explains the shortfall.
            outcome.order_status = OrderStatus.PARTIALLY_FILLED
            outcome.remaining_status = "terminal_unfilled"
            outcome.reason_code = MatchReasonCode.INSUFFICIENT_CASH.value
            outcome.remaining_reason_code = (
                MatchReasonCode.EXPIRED_AFTER_PARTIAL_FILL.value
            )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _quote(
        self,
        *,
        side: OrderSide,
        quantity: Decimal,
        execution_price: Decimal,
        facts_entry: InstrumentExecutionFacts,
        currency: str,
    ) -> FeeQuote:
        return self._fee_quote_provider.quote(
            side=side,
            quantity=quantity,
            execution_price=execution_price,
            contract_multiplier=facts_entry.contract_multiplier,
            currency=currency,
            instrument_context=facts_entry,
            order_context=MappingProxyType({}),
        )

    def _build_fill(
        self,
        *,
        order: Order,
        outcome: _Outcome,
        quantity: Decimal,
        quote: FeeQuote,
    ) -> Fill:
        assert outcome.state is not None
        assert outcome.execution_price is not None
        assert outcome.slippage is not None
        slippage = outcome.slippage
        namespace = uuid5(
            NAMESPACE_URL,
            f"quant-foundry:bar-open-match:{self.model_key}@{self.model_version}",
        )
        return Fill(
            fill_id=uuid5(
                namespace,
                f"{order.order_id}:{outcome.state.timestamp.isoformat()}:{quantity}",
            ),
            order_id=order.order_id,
            instrument_id=order.instrument_id,
            timestamp=outcome.state.timestamp,
            side=order.side,
            price=outcome.execution_price,
            quantity=quantity,
            currency=quote.currency,
            contract_multiplier=outcome.facts.contract_multiplier,
            reference_price=slippage.reference_price,
            fees=quote.total,
            fee_breakdown=quote.breakdown,
            slippage_bps=slippage.slippage_bps,
            slippage_amount=abs(slippage.price_delta) * quantity,
            slippage_model_key=slippage.model_key,
            slippage_model_version=slippage.model_version,
            slippage_model_parameters=slippage.parameters,
        )

    def _build_result(
        self,
        outcomes: Mapping[UUID, _Outcome],
        ledger: MatchLedger,
    ) -> BarOpenMatchResult:
        updates: list[OrderUpdateRecord] = []
        skipped: list[SkippedOrderRecord] = []
        allocations: list[BuyAllocationResult] = []
        unsubmitted_ids: list[UUID] = []
        ordered = sorted(outcomes.values(), key=lambda o: _order_sort_key(o.order))

        for outcome in ordered:
            order = outcome.order
            requested = outcome.requested_quantity
            if (
                order.side is OrderSide.BUY
                and outcome.in_cash_allocation
                and outcome.allocated == ZERO
                and outcome.filled == ZERO
                and outcome.reason_code is None
            ):
                # A buy whose cash allocation was scaled to zero never
                # became an executable order: report it only through its
                # BuyAllocationResult and hand its id back so the caller
                # atomically removes the candidate from the run's order
                # set — a later batch must never re-match it.  No order
                # update and no fabricated terminal state is emitted.
                allocations.append(
                    BuyAllocationResult(
                        intent_id=order.intent_id,
                        instrument_id=order.instrument_id,
                        requested_quantity=requested,
                        allocated_quantity=ZERO,
                        unsubmitted_quantity=requested,
                        full_cash_required=outcome.full_cash_required,
                        pro_rata_budget=outcome.pro_rata_budget,
                        allocation_phase=outcome.allocation_phase,
                        allocation_status=ALLOCATION_STATUS_NOT_SUBMITTED,
                        allocation_reason_code=self._allocation_reason(outcome),
                        reallocated=False,
                    )
                )
                unsubmitted_ids.append(order.order_id)
                continue

            reason = self._reason(outcome)
            if outcome.remaining_reason_code is None and reason is not None:
                # A whole-order rejection copies its reason onto the
                # unexecuted remainder (rejected -> not_executed pairing).
                outcome.remaining_reason_code = outcome.reason_code
            if outcome.remaining_reason_code == outcome.reason_code:
                remaining_reason = reason
            elif outcome.remaining_reason_code is not None:
                remaining_reason = self._remaining_reason(outcome)
            else:
                remaining_reason = None
            if outcome.filled == ZERO:
                skipped.append(
                    SkippedOrderRecord(
                        order_id=order.order_id,
                        instrument_id=order.instrument_id,
                        side=order.side,
                        order_status=outcome.order_status,
                        reason_code=reason
                        or StructuredReason(
                            stage=outcome.reason_stage,
                            code="UNKNOWN",
                        ),
                    )
                )
            # Commit the terminal transition on the runtime order;
            # ``submitted`` never survives a completed match.
            order.status = outcome.order_status
            order.status_reason = outcome.reason_code
            if outcome.filled > ZERO:
                order.filled_quantity = outcome.filled
            updates.append(
                OrderUpdateRecord(
                    order_id=order.order_id,
                    intent_id=order.intent_id,
                    instrument_id=order.instrument_id,
                    side=order.side,
                    requested_quantity=requested,
                    filled_quantity=outcome.filled,
                    remaining_quantity=requested - outcome.filled,
                    order_status=outcome.order_status,
                    remaining_status=outcome.remaining_status,
                    reason_code=reason,
                    remaining_reason_code=remaining_reason,
                )
            )
            if order.side is OrderSide.BUY and outcome.in_cash_allocation:
                # Preflight-rejected buys never entered the allocation
                # stage: they are reported only through their order update,
                # so "not_submitted" keeps its frozen meaning of a budget
                # scaled to zero.
                allocations.append(
                    BuyAllocationResult(
                        intent_id=order.intent_id,
                        instrument_id=order.instrument_id,
                        requested_quantity=requested,
                        allocated_quantity=outcome.allocated,
                        unsubmitted_quantity=requested - outcome.allocated,
                        full_cash_required=outcome.full_cash_required,
                        pro_rata_budget=outcome.pro_rata_budget,
                        allocation_phase=outcome.allocation_phase,
                        allocation_status=(
                            ALLOCATION_STATUS_NOT_SUBMITTED
                            if outcome.allocated == ZERO
                            else ALLOCATION_STATUS_ALLOCATED
                        ),
                        allocation_reason_code=(
                            self._allocation_reason(outcome)
                            if outcome.allocation_reason_code
                            else None
                        ),
                        reallocated=False,
                    )
                )

        return BarOpenMatchResult(
            fills=tuple(ledger.planned_fills),
            order_updates=tuple(updates),
            buy_allocation_results=tuple(allocations),
            skipped_or_rejected_orders=tuple(skipped),
            unsubmitted_order_ids=tuple(unsubmitted_ids),
        )

    @staticmethod
    def _reason(outcome: _Outcome) -> StructuredReason | None:
        """Structured ``stage/code/details`` view of the main reason."""

        if outcome.reason_code is None:
            return None
        return StructuredReason(
            stage=outcome.reason_stage,
            code=outcome.reason_code,
            details=dict(outcome.details or {}),
        )

    @staticmethod
    def _remaining_reason(outcome: _Outcome) -> StructuredReason:
        return StructuredReason(
            stage=outcome.reason_stage,
            code=outcome.remaining_reason_code or "",
            details=dict(outcome.details or {}),
        )

    @staticmethod
    def _allocation_reason(outcome: _Outcome) -> StructuredReason:
        return StructuredReason(
            stage=ResultStage.MATCHING.value,
            code=outcome.allocation_reason_code or "",
            details={
                "requested_quantity": str(outcome.requested_quantity),
                "allocated_quantity": str(outcome.allocated),
                "pro_rata_budget": str(outcome.pro_rata_budget),
            },
        )
