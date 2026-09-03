"""Deterministic low-frequency order execution for the backtesting kernel.

The first execution model handles one market order attempt at the next bar
open.  It only produces immutable ``Fill`` facts; the accounting policy remains
the owner of persistent cash and position mutations.  A local match context is
used to reserve cash and available units while a batch is being planned, so a
later order sees earlier fills without coupling the execution model to the
accounting implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence
from uuid import UUID, uuid5

from app.backtesting.accounting import Fill, OrderSide
from app.backtesting.data.errors import freeze_json
from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    PortfolioState,
    _aware_datetime,
    _non_negative,
    _positive,
)
from app.backtesting.fees import FeeBreakdown, FeeCalculator
from app.backtesting.slippage import SlippageResult, SlippageModel


class ExecutionError(DomainValidationError):
    """Raised when an order cannot be represented by an execution model."""


class ExecutionModel(Protocol):
    """Structural contract for converting submitted orders into fill facts."""

    def match(
        self,
        orders: Sequence[Order],
        market_states: Mapping[UUID, MarketState],
        context: MatchContext,
    ) -> MatchResult:
        """Attempt the current batch without mutating account state."""


class OrderType(StrEnum):
    """Order types supported by the first bar execution model."""

    MARKET = "market"


class OrderStatus(StrEnum):
    """Lifecycle states retained on the mutable runtime order.

    ``submitted`` is a runtime-only intermediate state: a completed match
    always ends in ``filled``, ``partially_filled``, ``expired``, or
    ``rejected``.
    """

    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class RemainingStatus(StrEnum):
    """State of the unfilled remainder of one order.

    The remainder is never compressed into an alias of ``order_status``:
    a partial fill must be able to state *why* the remaining quantity did
    not trade, and an unsubmitted allocation must be distinguishable from
    an order that was rejected before execution.
    """

    NONE = "none"
    TERMINAL_UNFILLED = "terminal_unfilled"
    TERMINAL_UNORDERABLE = "terminal_unorderable"
    NOT_EXECUTED = "not_executed"
    NOT_SUBMITTED = "not_submitted"


class PriceLimitStatus(StrEnum):
    """Explicit market-state labels; they never infer side availability."""

    NONE = "none"
    UP = "up"
    DOWN = "down"


def _side(value: object) -> OrderSide:
    """Normalize a side while keeping all public objects enum-backed."""

    try:
        return OrderSide(getattr(value, "value", value))
    except ValueError as exc:
        raise ExecutionError("side must be buy or sell") from exc


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A normalized execution request produced by a decision interpreter."""

    intent_id: UUID
    instrument_id: UUID
    side: OrderSide
    quantity: Decimal | int | str
    valid_from: datetime
    valid_until: datetime | None = None
    decision_id: UUID | None = None

    def __post_init__(self) -> None:
        _aware_datetime(self.valid_from, "valid_from")
        if self.valid_until is not None:
            _aware_datetime(self.valid_until, "valid_until")
            if self.valid_until < self.valid_from:
                raise ExecutionError("valid_until cannot precede valid_from")
        object.__setattr__(self, "side", _side(self.side))
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))


@dataclass(slots=True)
class Order:
    """Mutable runtime order whose state is advanced by an execution model."""

    order_id: UUID
    intent_id: UUID
    instrument_id: UUID
    side: OrderSide
    quantity: Decimal | int | str
    submitted_at: datetime
    order_type: OrderType = OrderType.MARKET
    filled_quantity: Decimal | int | str = ZERO
    status: OrderStatus = OrderStatus.SUBMITTED
    status_reason: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    # Stable in-run submission ordinal.  Matching priority must never
    # depend on caller-supplied collection order or on random UUIDs, so
    # batch matching sorts by this sequence; ``None`` is only tolerated
    # for legacy direct constructions and makes the order ineligible for
    # sequenced batch matching.
    submission_sequence: int | None = None

    def __post_init__(self) -> None:
        _aware_datetime(self.submitted_at, "submitted_at")
        if self.valid_from is not None:
            _aware_datetime(self.valid_from, "valid_from")
        if self.valid_until is not None:
            _aware_datetime(self.valid_until, "valid_until")
        if self.valid_from is not None and self.valid_until is not None:
            if self.valid_until < self.valid_from:
                raise ExecutionError("valid_until cannot precede valid_from")
        object.__setattr__(self, "side", _side(self.side))
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        object.__setattr__(self, "filled_quantity", _non_negative(self.filled_quantity, "filled_quantity"))
        if self.filled_quantity > self.quantity:
            raise ExecutionError("filled_quantity cannot exceed quantity")
        if self.submission_sequence is not None and (
            isinstance(self.submission_sequence, bool)
            or not isinstance(self.submission_sequence, int)
            or self.submission_sequence < 0
        ):
            raise ExecutionError(
                "submission_sequence must be a non-negative integer when provided"
            )
        try:
            self.order_type = OrderType(self.order_type)
            self.status = OrderStatus(self.status)
        except ValueError as exc:
            raise ExecutionError("order_type or status is unsupported") from exc

    @classmethod
    def from_intent(
        cls,
        intent: OrderIntent,
        *,
        order_id: UUID,
        submitted_at: datetime,
        submission_sequence: int | None = None,
    ) -> "Order":
        """Create a standard market order from an intent."""

        return cls(
            order_id=order_id,
            intent_id=intent.intent_id,
            instrument_id=intent.instrument_id,
            side=intent.side,
            quantity=intent.quantity,
            submitted_at=submitted_at,
            valid_from=intent.valid_from,
            valid_until=intent.valid_until,
            submission_sequence=submission_sequence,
        )

    @property
    def remaining_quantity(self) -> Decimal:
        """Return the quantity not yet filled."""

        return self.quantity - self.filled_quantity

    def expire(self, reason: str) -> None:
        """Mark a one-shot order as not filled with an auditable reason."""

        self.status = OrderStatus.EXPIRED
        self.status_reason = reason


class SubmissionSequenceAllocator:
    """Run-global, monotonic ``submission_sequence`` source.

    Sequences are unique within one ``run_id``, start at 1, never reset
    between shards of the same run, and are strictly increasing.  A shard
    that resumes a split run seeds the allocator with the last sequence
    already consumed by earlier shards via ``resume_after``.

    Retries must reuse the original ordinal instead of consuming a new
    one: :meth:`sequence_for` memoizes one sequence per stable retry key,
    so re-submitting the same intent cannot duplicate or skip ordinals.

    The retry-key map lives in process memory, so a restarted process or
    a fresh shard must restore it explicitly through
    ``restored_sequences``; otherwise the same retry key would be treated
    as new and receive a second ordinal.
    """

    def __init__(
        self,
        *,
        run_id: str,
        resume_after: int = 0,
        restored_sequences: Mapping[str, int] | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ExecutionError("run_id must be non-blank text")
        if (
            isinstance(resume_after, bool)
            or not isinstance(resume_after, int)
            or resume_after < 0
        ):
            raise ExecutionError("resume_after must be a non-negative integer")
        self._run_id = run_id
        self._last_sequence = resume_after
        self._sequences_by_key: dict[str, int] = {}
        seen_values: dict[int, str] = {}
        for key, value in dict(restored_sequences or {}).items():
            if not isinstance(key, str) or not key:
                raise ExecutionError(
                    "restored retry keys must be non-empty strings"
                )
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or value > resume_after
            ):
                raise ExecutionError(
                    f"restored sequence for retry key {key!r} must be an "
                    f"integer in [1, {resume_after}]"
                )
            duplicate = seen_values.get(value)
            if duplicate is not None:
                raise ExecutionError(
                    f"retry keys {duplicate!r} and {key!r} both map to "
                    f"sequence {value}; restored mappings must be injective"
                )
            seen_values[value] = key
            self._sequences_by_key[key] = value

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def last_sequence(self) -> int:
        """The highest sequence handed out so far (``0`` before first use)."""

        return self._last_sequence

    def sequences_snapshot(self) -> Mapping[str, int]:
        """Return the retry-key mapping for durable persistence.

        The persistence layer stores this mapping alongside
        :attr:`last_sequence` and feeds both back through the constructor
        when a new process or shard resumes the run.
        """

        return MappingProxyType(dict(self._sequences_by_key))

    def next_sequence(self) -> int:
        """Allocate the next unused sequence of the run."""

        self._last_sequence += 1
        return self._last_sequence

    def sequence_for(self, retry_key: str) -> int:
        """Return the memoized sequence for ``retry_key``, allocating once.

        ``retry_key`` must be the stable identity whose resubmission has to
        keep its original ordinal (for example ``intent_id``).  The same
        key always maps to the same sequence; distinct keys never share
        one.
        """

        if not isinstance(retry_key, str) or not retry_key:
            raise ExecutionError("retry_key must be non-empty text")
        existing = self._sequences_by_key.get(retry_key)
        if existing is not None:
            return existing
        sequence = self.next_sequence()
        self._sequences_by_key[retry_key] = sequence
        return sequence


@dataclass(frozen=True, slots=True)
class MarketState:
    """PIT market facts required before an opening match may proceed.

    The boolean fields keep their historical fixture defaults; the formal
    path never relies on them: :func:`market_state_from_execution_facts`
    sets every field explicitly from normalized session facts and records
    that provenance in ``facts_basis``.
    """

    instrument_id: UUID
    timestamp: datetime
    open_price: Decimal | int | str | None
    price_tick: Decimal | int | str
    is_suspended: bool = False
    open_available: bool = True
    buy_allowed: bool = True
    sell_allowed: bool = True
    price_limit_status: PriceLimitStatus | str = PriceLimitStatus.NONE
    status_reason: str | None = None
    # Deep-frozen provenance of a formal construction: per-dimension fact
    # states, applicability, and evidence.  ``None`` marks fixture use.
    facts_basis: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _aware_datetime(self.timestamp, "timestamp")
        object.__setattr__(self, "price_tick", _positive(self.price_tick, "price_tick"))
        if self.open_price is not None:
            object.__setattr__(self, "open_price", _positive(self.open_price, "open_price"))
        try:
            object.__setattr__(
                self,
                "price_limit_status",
                PriceLimitStatus(self.price_limit_status),
            )
        except ValueError as exc:
            raise ExecutionError("price_limit_status is unsupported") from exc
        if self.facts_basis is not None:
            if not isinstance(self.facts_basis, Mapping):
                raise ExecutionError("facts_basis must be a mapping when provided")
            frozen = freeze_json(dict(self.facts_basis), "facts_basis")
            assert isinstance(frozen, Mapping)
            object.__setattr__(self, "facts_basis", frozen)


@dataclass(slots=True)
class MatchContext:
    """Mutable planning balances used only while one batch is matched."""

    currency: str
    available_cash: Decimal
    available_quantities: dict[UUID, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ExecutionError("currency must be non-blank text")
        self.currency = self.currency.strip().upper()
        self.available_cash = _non_negative(self.available_cash, "available_cash")
        self.available_quantities = {
            instrument_id: _non_negative(quantity, "available_quantity")
            for instrument_id, quantity in self.available_quantities.items()
        }

    @classmethod
    def from_portfolio(cls, portfolio: PortfolioState, *, currency: str = "CNY") -> "MatchContext":
        """Build a detached planning context from the current portfolio."""

        return cls(
            currency=currency,
            available_cash=portfolio.account.available_cash,
            available_quantities={
                instrument_id: position.available_quantity
                for instrument_id, position in portfolio.positions.items()
            },
        )


@dataclass(frozen=True, slots=True)
class SkippedOrder:
    """Reason an order produced no fill in the current opening match."""

    order_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Deterministic output of one opening-match attempt."""

    fills: tuple[Fill, ...]
    skipped_orders: tuple[SkippedOrder, ...]


@dataclass(frozen=True, slots=True)
class BarMarketExecutionModel:
    """Match one-shot market orders at the next bar open.

    Orders are processed with sells first and then by stable instrument/order
    identifiers.  A buy that cannot be funded in full is expired as a whole;
    there is no hidden proportional allocation or partial cash fill.
    """

    slippage_model: SlippageModel
    fee_calculator: FeeCalculator
    model_key: str = "bar_market"
    model_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.model_key, str) or not self.model_key.strip():
            raise ExecutionError("execution model key must be non-blank text")
        if self.model_version <= 0:
            raise ExecutionError("execution model version must be positive")

    def match(
        self,
        orders: Sequence[Order],
        market_states: Mapping[UUID, MarketState],
        context: MatchContext,
    ) -> MatchResult:
        """Produce fills while updating only the detached match context."""

        ordered = sorted(
            orders,
            key=lambda order: (
                # side_stage: sells release proceeds before buys compete.
                0 if order.side is OrderSide.SELL else 1,
                str(order.instrument_id),
                # In-run submission ordinal; legacy orders without one
                # sort after sequenced ones deterministically instead of
                # depending on collection order.
                (0, order.submission_sequence)
                if order.submission_sequence is not None
                else (1, 0),
                str(order.order_id),
            ),
        )
        fills: list[Fill] = []
        skipped: list[SkippedOrder] = []
        for order in ordered:
            if order.status is not OrderStatus.SUBMITTED:
                skipped.append(SkippedOrder(order.order_id, "order_not_submitted"))
                continue
            state = market_states.get(order.instrument_id)
            reason = self._preflight_order(order, state)
            if reason is not None:
                order.expire(reason)
                skipped.append(SkippedOrder(order.order_id, reason))
                continue
            assert state is not None
            assert state.open_price is not None

            slippage = self.slippage_model.apply(
                state.open_price,
                order.side,
                price_tick=state.price_tick,
            )
            quantity = order.remaining_quantity
            notional = slippage.execution_price * quantity
            breakdown = self.fee_calculator.calculate(
                side=order.side,
                notional=notional,
                currency=context.currency,
            )
            total_cost = notional + breakdown.total
            available_quantity = context.available_quantities.get(order.instrument_id, ZERO)
            if order.side is OrderSide.SELL and quantity > available_quantity:
                reason = "insufficient_available_quantity"
            elif order.side is OrderSide.BUY and total_cost > context.available_cash:
                reason = "insufficient_cash"
            else:
                fill = self._build_fill(order, state, slippage, breakdown, quantity)
                fills.append(fill)
                order.filled_quantity += quantity
                order.status = OrderStatus.FILLED
                order.status_reason = None
                if order.side is OrderSide.SELL:
                    context.available_quantities[order.instrument_id] = available_quantity - quantity
                    context.available_cash += notional - breakdown.total
                else:
                    context.available_cash -= total_cost
                continue

            order.expire(reason)
            skipped.append(SkippedOrder(order.order_id, reason))

        return MatchResult(tuple(fills), tuple(skipped))

    @staticmethod
    def _preflight_order(order: Order, state: MarketState | None) -> str | None:
        """Return a stable no-fill reason before any price or fee calculation."""

        if order.order_type is not OrderType.MARKET:
            return "unsupported_order_type"
        if state is None:
            return "market_state_missing"
        if order.valid_from is not None and state.timestamp < order.valid_from:
            return "order_not_yet_valid"
        if order.valid_until is not None and state.timestamp > order.valid_until:
            return "order_expired"
        if state.is_suspended:
            return "instrument_suspended"
        if not state.open_available or state.open_price is None:
            return "open_unavailable"
        if order.side is OrderSide.BUY and not state.buy_allowed:
            return "buy_unavailable_at_price_limit"
        if order.side is OrderSide.SELL and not state.sell_allowed:
            return "sell_unavailable_at_price_limit"
        return None

    def _build_fill(
        self,
        order: Order,
        state: MarketState,
        slippage: SlippageResult,
        breakdown: FeeBreakdown,
        quantity: Decimal,
    ) -> Fill:
        """Build a deterministic, fully audited fill fact."""

        fill_id = uuid5(
            order.order_id,
            f"{self.model_key}@{self.model_version}:{state.timestamp.isoformat()}:{quantity}",
        )
        return Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            instrument_id=order.instrument_id,
            timestamp=state.timestamp,
            side=order.side,
            reference_price=slippage.reference_price,
            price=slippage.execution_price,
            quantity=quantity,
            fees=breakdown.total,
            currency=breakdown.currency,
            fee_breakdown=breakdown,
            slippage_bps=slippage.slippage_bps,
            slippage_amount=abs(slippage.price_delta) * quantity,
            slippage_model_key=slippage.model_key,
            slippage_model_version=slippage.model_version,
            slippage_model_parameters=slippage.parameters,
        )
