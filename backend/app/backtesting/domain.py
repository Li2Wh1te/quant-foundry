"""Runtime account, position, and portfolio state for backtests.

The classes in this module intentionally do not represent database rows.  A
backtest mutates the ``*State`` objects in memory while it processes orders
and fills.  At an estimation point it creates an immutable ``*Snapshot``
view, which can then be projected into result tables without exposing the
mutable runtime state to callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping
from uuid import UUID


ZERO = Decimal("0")


class DomainValidationError(ValueError):
    """Raised when a runtime domain object would violate an invariant."""


class PositionSide(StrEnum):
    """Position representation supported by the generic domain model."""

    LONG = "long"
    SHORT = "short"
    NET = "net"


class ValuationStatus(StrEnum):
    """Whether the latest portfolio valuation is complete and usable."""

    COMPLETE = "complete"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


def _decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    """Normalize supported numeric inputs while rejecting binary floats.

    Accepting a float would reintroduce the precision problem that the
    ``Decimal`` domain contract is intended to prevent.  Integers and strings
    are accepted for ergonomic construction of test fixtures and API adapters;
    all values are converted before they enter a state object.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must be Decimal, int, or str; float is unsupported")

    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainValidationError(f"{field_name} must be a valid decimal") from exc

    if not normalized.is_finite():
        raise DomainValidationError(f"{field_name} must be finite")
    return normalized


def _non_negative(value: Decimal | int | str, field_name: str) -> Decimal:
    """Normalize a decimal value that cannot be negative."""

    normalized = _decimal(value, field_name)
    if normalized < ZERO:
        raise DomainValidationError(f"{field_name} must be non-negative")
    return normalized


def _positive(value: Decimal | int | str, field_name: str) -> Decimal:
    """Normalize a decimal value that must be strictly positive."""

    normalized = _decimal(value, field_name)
    if normalized <= ZERO:
        raise DomainValidationError(f"{field_name} must be positive")
    return normalized


def _optional_price(value: Decimal | int | str | None, field_name: str) -> Decimal | None:
    """Normalize an optional market price without treating missing data as zero."""

    if value is None:
        return None
    return _positive(value, field_name)


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    """Require an explicitly timezone-aware timestamp for auditability."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    return value


def _cash_balances(value: Mapping[str, Decimal | int | str]) -> dict[str, Decimal]:
    """Copy and normalize a currency-to-cash mapping.

    The copy is important: callers may reuse a request dictionary, but a
    snapshot must not change when that dictionary is mutated later.
    """

    normalized: dict[str, Decimal] = {}
    for currency, amount in value.items():
        if not isinstance(currency, str) or not currency.strip():
            raise DomainValidationError("cash balance currency must be non-blank text")
        normalized[currency] = _decimal(amount, f"cash_balances[{currency!r}]")
    return normalized


def _validate_available_quantity(quantity: Decimal, available_quantity: Decimal) -> None:
    """Ensure a position never exposes more tradable units than it owns."""

    if available_quantity < ZERO:
        raise DomainValidationError("available_quantity must be non-negative")
    if available_quantity > quantity:
        raise DomainValidationError("available_quantity cannot exceed quantity")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Immutable account view at one portfolio valuation point.

    ``equity`` is supplied by the accounting policy rather than recomputed in
    this data object.  This keeps currency conversion, liabilities, and margin
    rules in one explicit policy instead of hiding financial assumptions here.
    """

    cash_balances: Mapping[str, Decimal]
    available_cash: Decimal
    frozen_cash: Decimal
    margin_used: Decimal
    margin_available: Decimal
    equity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cash_balances",
            MappingProxyType(_cash_balances(self.cash_balances)),
        )
        for field_name in (
            "available_cash",
            "frozen_cash",
            "margin_used",
            "margin_available",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "equity", _decimal(self.equity, "equity"))


@dataclass(slots=True)
class AccountState:
    """Mutable account state owned by one running backtest.

    This object is deliberately runtime-only.  The accounting policy is the
    only layer that should mutate it after applying a fill or a valuation; the
    state itself only validates the resulting values and provides snapshots.
    """

    cash_balances: dict[str, Decimal | int | str]
    available_cash: Decimal | int | str
    frozen_cash: Decimal | int | str
    margin_used: Decimal | int | str
    margin_available: Decimal | int | str
    equity: Decimal | int | str

    def __post_init__(self) -> None:
        self.cash_balances = _cash_balances(self.cash_balances)
        self.available_cash = _non_negative(self.available_cash, "available_cash")
        self.frozen_cash = _non_negative(self.frozen_cash, "frozen_cash")
        self.margin_used = _non_negative(self.margin_used, "margin_used")
        self.margin_available = _non_negative(
            self.margin_available, "margin_available"
        )
        self.equity = _decimal(self.equity, "equity")

    def snapshot(self) -> AccountSnapshot:
        """Return a detached immutable view of the current account state."""

        return AccountSnapshot(
            cash_balances=self.cash_balances,
            available_cash=self.available_cash,
            frozen_cash=self.frozen_cash,
            margin_used=self.margin_used,
            margin_available=self.margin_available,
            equity=self.equity,
        )


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Immutable non-zero position view at a valuation point."""

    instrument_id: UUID
    side: PositionSide
    quantity: Decimal
    available_quantity: Decimal
    average_price: Decimal
    mark_price: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal

    def __post_init__(self) -> None:
        quantity = _positive(self.quantity, "quantity")
        available_quantity = _non_negative(
            self.available_quantity, "available_quantity"
        )
        _validate_available_quantity(quantity, available_quantity)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "available_quantity", available_quantity)
        object.__setattr__(
            self,
            "average_price",
            _positive(self.average_price, "average_price"),
        )
        object.__setattr__(
            self,
            "mark_price",
            _optional_price(self.mark_price, "mark_price"),
        )
        object.__setattr__(
            self, "realized_pnl", _decimal(self.realized_pnl, "realized_pnl")
        )
        object.__setattr__(
            self, "unrealized_pnl", _decimal(self.unrealized_pnl, "unrealized_pnl")
        )


@dataclass(slots=True)
class PositionState:
    """Mutable position state keyed by stable ``instrument_id``."""

    instrument_id: UUID
    side: PositionSide
    quantity: Decimal | int | str = ZERO
    available_quantity: Decimal | int | str = ZERO
    average_price: Decimal | int | str | None = None
    mark_price: Decimal | int | str | None = None
    realized_pnl: Decimal | int | str = ZERO
    unrealized_pnl: Decimal | int | str = ZERO

    def __post_init__(self) -> None:
        self.quantity = _non_negative(self.quantity, "quantity")
        self.available_quantity = _non_negative(
            self.available_quantity, "available_quantity"
        )
        _validate_available_quantity(self.quantity, self.available_quantity)
        if self.quantity == ZERO:
            if self.average_price is not None or self.mark_price is not None:
                raise DomainValidationError(
                    "zero-quantity positions cannot carry average_price or mark_price"
                )
        else:
            if self.average_price is None:
                raise DomainValidationError(
                    "non-zero positions require average_price"
                )
            self.average_price = _positive(self.average_price, "average_price")
            self.mark_price = _optional_price(self.mark_price, "mark_price")
        self.realized_pnl = _decimal(self.realized_pnl, "realized_pnl")
        self.unrealized_pnl = _decimal(self.unrealized_pnl, "unrealized_pnl")

    @property
    def is_zero(self) -> bool:
        """Whether this state is an empty position waiting to be removed."""

        return self.quantity == ZERO

    def snapshot(self) -> PositionSnapshot:
        """Return a snapshot, rejecting zero positions by contract."""

        if self.is_zero:
            raise DomainValidationError("zero positions are not persisted or snapshotted")
        # The branch above proves these optional values are present for a
        # non-zero state; the assertions make that invariant explicit to type
        # checkers and future maintainers.
        assert self.average_price is not None
        return PositionSnapshot(
            instrument_id=self.instrument_id,
            side=self.side,
            quantity=self.quantity,
            available_quantity=self.available_quantity,
            average_price=self.average_price,
            mark_price=self.mark_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
        )


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Immutable, consistently-timestamped account and position view."""

    account: AccountSnapshot
    positions: tuple[PositionSnapshot, ...]
    as_of: datetime
    valuation_status: ValuationStatus

    def __post_init__(self) -> None:
        _aware_datetime(self.as_of, "as_of")
        normalized_positions = tuple(self.positions)
        instrument_ids = [position.instrument_id for position in normalized_positions]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise DomainValidationError(
                "portfolio snapshot cannot contain duplicate instrument_id values"
            )
        if any(position.quantity <= ZERO for position in normalized_positions):
            raise DomainValidationError(
                "portfolio snapshot cannot contain zero or negative positions"
            )
        # Stable ordering makes serialized results and paginated reads
        # deterministic regardless of dictionary insertion order.
        normalized_positions = tuple(
            sorted(normalized_positions, key=lambda position: str(position.instrument_id))
        )
        object.__setattr__(self, "positions", normalized_positions)


@dataclass(slots=True)
class PortfolioState:
    """Mutable in-memory portfolio state for one backtest run."""

    account: AccountState
    as_of: datetime
    positions: dict[UUID, PositionState] = field(default_factory=dict)
    valuation_status: ValuationStatus = ValuationStatus.COMPLETE

    def __post_init__(self) -> None:
        _aware_datetime(self.as_of, "as_of")
        copied_positions: dict[UUID, PositionState] = {}
        for instrument_id, position in self.positions.items():
            if instrument_id != position.instrument_id:
                raise DomainValidationError(
                    "position dictionary key must match position.instrument_id"
                )
            copied_positions[instrument_id] = position
        self.positions = copied_positions

    def put_position(self, position: PositionState) -> None:
        """Insert or replace one position after a fill or corporate action."""

        self.positions[position.instrument_id] = position

    def remove_zero_positions(self) -> None:
        """Remove empty positions so result projections stay compact."""

        self.positions = {
            instrument_id: position
            for instrument_id, position in self.positions.items()
            if not position.is_zero
        }

    def snapshot(self) -> PortfolioSnapshot:
        """Build a detached snapshot containing only non-zero positions."""

        snapshots = tuple(
            position.snapshot()
            for position in self.positions.values()
            if not position.is_zero
        )
        return PortfolioSnapshot(
            account=self.account.snapshot(),
            positions=snapshots,
            as_of=self.as_of,
            valuation_status=self.valuation_status,
        )
