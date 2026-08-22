"""Single-run backtest specification and its validation.

The objects here own the per-run inputs that deliberately do NOT live on
``BacktestAccountProfile``: the inclusive date range, initial cash, and
initial positions.  Reusing an account profile never changes these values;
nothing in this module imports or reads account profiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence
from uuid import UUID

from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    PositionSide,
    _decimal,
    _non_negative,
    _positive,
    _validate_available_quantity,
)


def _plain_date(value: date, field_name: str) -> date:
    """Require a plain calendar date, rejecting timezone-carrying datetimes.

    Backtest boundaries are inclusive calendar dates; a ``datetime`` would
    smuggle in a time-of-day that has no meaning for the daily domain.
    """

    if isinstance(value, datetime):
        raise DomainValidationError(
            f"{field_name} must be a calendar date, not a datetime"
        )
    if not isinstance(value, date):
        raise DomainValidationError(f"{field_name} must be a calendar date")
    return value


@dataclass(frozen=True, slots=True)
class InitialPositionInput:
    """One user-supplied opening position for a single backtest run.

    ``instrument_id`` is the stable identity and must not be replaced by a
    trading symbol.  ``average_price`` is the user-provided cost basis; it is
    kept separate from any valuation price resolved later by preflight.
    Zero-quantity rows are accepted here so callers may pass whole portfolio
    dumps; ``BacktestSpec`` normalizes them away.
    """

    instrument_id: UUID
    side: PositionSide
    quantity: Decimal | int | str
    available_quantity: Decimal | int | str
    average_price: Decimal | int | str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError(
                "initial position instrument_id must be a UUID (stable instrument identity)"
            )
        try:
            side = PositionSide(self.side)
        except ValueError as exc:
            raise DomainValidationError(
                f"initial position side must be one of "
                f"{[member.value for member in PositionSide]}"
            ) from exc
        object.__setattr__(self, "side", side)

        quantity = _non_negative(self.quantity, "quantity")
        available_quantity = _non_negative(
            self.available_quantity, "available_quantity"
        )
        _validate_available_quantity(quantity, available_quantity)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "available_quantity", available_quantity)

        if self.average_price is not None:
            # The cost basis must be positive whenever it is provided at all;
            # whether it is required is decided by the quantity check below.
            object.__setattr__(
                self,
                "average_price",
                _positive(self.average_price, "average_price"),
            )
        if quantity > ZERO and self.average_price is None:
            raise DomainValidationError(
                "non-zero initial positions require an explicit average_price"
            )


def _normalize_initial_positions(
    positions: Sequence[InitialPositionInput],
) -> tuple[InitialPositionInput, ...]:
    """Validate uniqueness, drop zero-quantity rows, and sort stably."""

    seen: set[UUID] = set()
    for position in positions:
        if position.instrument_id in seen:
            raise DomainValidationError(
                "duplicate initial position "
                f"instrument_id {position.instrument_id}"
            )
        seen.add(position.instrument_id)

    # Zero-quantity rows carry no opening exposure, so they are ignored after
    # normalization and never enter per-instrument preflight or results.
    non_zero = [position for position in positions if position.quantity > ZERO]
    return tuple(sorted(non_zero, key=lambda item: str(item.instrument_id)))


@dataclass(frozen=True, slots=True)
class BacktestSpec:
    """Validated configuration for exactly one backtest run.

    The date range is inclusive on both ends.  ``dynamic_universe`` records
    that the run uses a dynamic candidate universe; it never reduces the
    mandatory preflight scope, which always covers every non-zero initial
    position regardless of this flag.
    """

    start_date: date
    end_date: date
    initial_cash: Decimal | int | str
    initial_positions: Sequence[InitialPositionInput]
    dynamic_universe: bool = False

    def __post_init__(self) -> None:
        start_date = _plain_date(self.start_date, "start_date")
        end_date = _plain_date(self.end_date, "end_date")
        if start_date > end_date:
            raise DomainValidationError("start_date cannot be after end_date")
        object.__setattr__(self, "start_date", start_date)
        object.__setattr__(self, "end_date", end_date)

        initial_cash = _non_negative(self.initial_cash, "initial_cash")
        object.__setattr__(self, "initial_cash", initial_cash)

        object.__setattr__(
            self, "initial_positions", _normalize_initial_positions(self.initial_positions)
        )

    @property
    def non_zero_initial_positions(self) -> tuple[InitialPositionInput, ...]:
        """Mandatory fixed preflight subjects, in stable sorted order."""

        return self.initial_positions
