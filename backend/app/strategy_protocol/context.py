"""Read-only ``DecisionContext`` and its nested DTOs.

Every object in this module is an immutable value object.  Strategies may read
them but cannot modify the account, positions, orders, fills, or query
conditions: mappings are wrapped in ``MappingProxyType`` and sequences become
tuples.  Monetary amounts, quantities, prices, and weights use ``Decimal``
inside one process and decimal strings across process boundaries; binary
floats are never accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping
from uuid import UUID

from app.backtesting.domain import (
    PositionSide,
    _decimal,
    _non_negative,
    _optional_price,
    _positive,
    _validate_available_quantity,
)

from .contract import InvalidDecisionPayloadError

if TYPE_CHECKING:  # pragma: no cover - import used only for type hints
    from .data_view import StrategyDataDTO, UniverseQueryDTO


def _frozen_mapping(value: Mapping[str, Decimal], field_name: str) -> Mapping[str, Decimal]:
    """Copy a mapping into a read-only proxy with engine-normalized decimals."""

    if not isinstance(value, Mapping):
        raise InvalidDecisionPayloadError(f"{field_name} must be a mapping")
    return MappingProxyType(
        {
            str(key): _decimal(item, f"{field_name}[{key!r}]")
            for key, item in value.items()
        }
    )


@dataclass(frozen=True, slots=True)
class DeterministicClockDTO:
    """Fixed clock for exactly one decision step.

    ``now()`` always returns this step's ``decision_time`` and ``today()``
    returns its ``session_date``.  The clock never reads the operating-system
    wall clock, so repeated calls inside one step are identical and results do
    not depend on real execution speed.
    """

    decision_time: datetime
    session_date: date

    def __post_init__(self) -> None:
        if self.decision_time.tzinfo is None or self.decision_time.utcoffset() is None:
            raise InvalidDecisionPayloadError(
                "decision_time must be timezone-aware"
            )
        if not isinstance(self.session_date, date) or isinstance(
            self.session_date, datetime
        ):
            raise InvalidDecisionPayloadError("session_date must be a calendar date")

    def now(self) -> datetime:
        """Return the fixed decision time of the current step."""

        return self.decision_time

    def today(self) -> date:
        """Return the fixed session date of the current step."""

        return self.session_date


@dataclass(frozen=True, slots=True)
class PositionDTO:
    """Read-only view of one non-zero position at decision time.

    Quantities and prices reuse the engine-wide ``Decimal`` normalization:
    binary floats, booleans, non-finite values, negative amounts, and
    available quantities above the owned quantity are rejected.
    """

    instrument_id: UUID
    trading_code: str
    name: str
    display_name: str
    side: PositionSide
    quantity: Decimal
    available_quantity: Decimal
    average_price: Decimal
    mark_price: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "side", PositionSide(self.side))
        except ValueError as exc:
            raise InvalidDecisionPayloadError("side is not a supported position side") from exc
        quantity = _positive(self.quantity, "quantity")
        available_quantity = _non_negative(
            self.available_quantity, "available_quantity"
        )
        _validate_available_quantity(quantity, available_quantity)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "available_quantity", available_quantity)
        object.__setattr__(
            self, "average_price", _positive(self.average_price, "average_price")
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


@dataclass(frozen=True, slots=True)
class PortfolioDTO:
    """Read-only account and non-zero position snapshot.

    All monetary amounts reuse the engine-wide ``Decimal`` normalization, so
    floats, booleans, non-finite values, and negative cash/margin fields are
    rejected at construction.
    """

    cash_balances: Mapping[str, Decimal]
    available_cash: Decimal
    frozen_cash: Decimal
    margin_used: Decimal
    margin_available: Decimal
    equity: Decimal
    positions: tuple[PositionDTO, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "cash_balances", _frozen_mapping(self.cash_balances, "cash_balances")
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
        normalized_positions = tuple(self.positions)
        ids = [position.instrument_id for position in normalized_positions]
        if len(ids) != len(set(ids)):
            raise InvalidDecisionPayloadError(
                "portfolio cannot contain duplicate instrument_id values"
            )
        # Stable ordering keeps serialized contexts deterministic.
        object.__setattr__(
            self,
            "positions",
            tuple(sorted(normalized_positions, key=lambda p: str(p.instrument_id))),
        )


@dataclass(frozen=True, slots=True)
class OrderSummaryDTO:
    """Read-only summary of one order from the previous step."""

    instrument_id: UUID
    side: str
    quantity: Decimal
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "quantity", _positive(self.quantity, "order quantity")
        )


@dataclass(frozen=True, slots=True)
class FillSummaryDTO:
    """Read-only summary of one fill from the previous step."""

    instrument_id: UUID
    side: str
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "quantity", _positive(self.quantity, "fill quantity")
        )
        object.__setattr__(self, "price", _positive(self.price, "fill price"))


@dataclass(frozen=True, slots=True)
class PreviousStepDTO:
    """Read-only digest of the previous step's order and fill outcomes.

    The DTO intentionally offers no method to resubmit orders or rewrite
    history; it exists purely so a strategy can observe what happened.
    """

    step_sequence: int
    orders: tuple[OrderSummaryDTO, ...] = field(default_factory=tuple)
    fills: tuple[FillSummaryDTO, ...] = field(default_factory=tuple)
    order_statuses: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Complete read-only context handed to one strategy decision.

    The shape is fixed by the official protocol; implementations do not trim
    fields.  ``data`` and ``universe`` expose guarded query facades whose
    conditions strategies cannot change.
    """

    step_sequence: int
    session_date: date
    decision_time: datetime
    data_cutoff: datetime
    timezone: str
    clock: DeterministicClockDTO
    portfolio: PortfolioDTO
    previous_step: PreviousStepDTO
    data: "StrategyDataDTO"
    universe: "UniverseQueryDTO"

    def __post_init__(self) -> None:
        for field_name in ("decision_time", "data_cutoff"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidDecisionPayloadError(
                    f"{field_name} must be timezone-aware"
                )
        if self.clock.now() != self.decision_time:
            raise InvalidDecisionPayloadError(
                "clock decision_time must match the context decision_time"
            )
        if self.clock.today() != self.session_date:
            raise InvalidDecisionPayloadError(
                "clock session_date must match the context session_date"
            )


__all__ = [
    "DecisionContext",
    "DeterministicClockDTO",
    "FillSummaryDTO",
    "OrderSummaryDTO",
    "PortfolioDTO",
    "PositionDTO",
    "PreviousStepDTO",
]
