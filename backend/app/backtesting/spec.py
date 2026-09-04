"""Single-run backtest specification and its validation.

The immutable spec owns every client-selected input for one run: strategy
revision/parameters, date and data scope, opening portfolio, account reference,
slippage selection, and optional randomness. Resolved storage snapshots remain
on ``RunBinding``; this module imports no repository or mutable account state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _freeze_json(value: Any) -> Any:
    """Detach and freeze JSON-like run input before it enters a binding."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ComponentSelection:
    """Exact registered component identity selected for one run."""

    key: str
    version: int
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise DomainValidationError("component key must be non-blank text")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version <= 0
        ):
            raise DomainValidationError("component version must be a positive integer")
        if not isinstance(self.parameters, Mapping):
            raise DomainValidationError("component parameters must be an object")
        object.__setattr__(self, "key", self.key.strip())
        object.__setattr__(self, "parameters", _freeze_json(self.parameters))


def _default_slippage_model() -> ComponentSelection:
    return ComponentSelection("none", 1, {"price_tick": "0.01"})


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
    # Client-selected scope and behavior are kept on the immutable spec. The
    # binding later adds the resolved strategy, account, data and component
    # snapshots, preserving both what was requested and what actually ran.
    instrument_ids: Sequence[UUID] = ()
    exchanges: Sequence[str] = ("SSE", "SZSE")
    strategy_price_bases: Sequence[str] = ("raw",)
    strategy_revision_id: UUID | None = None
    strategy_parameters: Mapping[str, Any] | None = None
    account_profile_id: UUID | None = None
    slippage_model: ComponentSelection = field(default_factory=_default_slippage_model)
    random_seed: int | None = None
    # These values are part of the run input even though the first engine
    # implementation currently supports only domestic daily data.  Keeping
    # them on the immutable spec prevents a future default from changing the
    # meaning of an already-created run.
    currency: str = "CNY"
    timezone: str = "Asia/Shanghai"
    frequency: str = "1d"
    warmup_sessions: int = 0

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
        if not isinstance(self.dynamic_universe, bool):
            raise DomainValidationError("dynamic_universe must be a boolean")

        instrument_ids = tuple(self.instrument_ids)
        if any(not isinstance(item, UUID) for item in instrument_ids):
            raise DomainValidationError("instrument_ids must contain stable UUIDs")
        if len(set(instrument_ids)) != len(instrument_ids):
            raise DomainValidationError("instrument_ids must not contain duplicates")
        object.__setattr__(
            self, "instrument_ids", tuple(sorted(instrument_ids, key=str))
        )

        if not isinstance(self.exchanges, Sequence) or isinstance(
            self.exchanges, (str, bytes)
        ):
            raise DomainValidationError("exchanges must be a sequence")
        exchanges = tuple(
            dict.fromkeys(str(exchange).strip().upper() for exchange in self.exchanges)
        )
        if not exchanges or any(not exchange for exchange in exchanges):
            raise DomainValidationError("exchanges must contain non-blank names")
        object.__setattr__(self, "exchanges", exchanges)

        price_bases = tuple(
            dict.fromkeys(str(value).strip().lower() for value in self.strategy_price_bases)
        )
        if not price_bases or any(value not in {"raw", "qfq", "hfq"} for value in price_bases):
            raise DomainValidationError(
                "strategy_price_bases must contain raw, qfq, or hfq"
            )
        object.__setattr__(self, "strategy_price_bases", price_bases)

        for field_name in ("strategy_revision_id", "account_profile_id"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, UUID):
                raise DomainValidationError(f"{field_name} must be a UUID or null")
        if self.strategy_parameters is not None:
            if not isinstance(self.strategy_parameters, Mapping):
                raise DomainValidationError("strategy_parameters must be an object or null")
            object.__setattr__(
                self, "strategy_parameters", _freeze_json(self.strategy_parameters)
            )
        if not isinstance(self.slippage_model, ComponentSelection):
            raise DomainValidationError("slippage_model must be a ComponentSelection")
        if self.random_seed is not None and (
            isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int)
        ):
            raise DomainValidationError("random_seed must be an integer or null")

        if not isinstance(self.currency, str) or not self.currency.strip():
            raise DomainValidationError("currency must be non-blank text")
        object.__setattr__(self, "currency", self.currency.strip().upper())
        if self.frequency != "1d":
            raise DomainValidationError("frequency must be 1d")
        if (
            isinstance(self.warmup_sessions, bool)
            or not isinstance(self.warmup_sessions, int)
            or self.warmup_sessions < 0
        ):
            raise DomainValidationError("warmup_sessions must be a non-negative integer")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DomainValidationError("timezone must be an IANA time-zone name") from exc
        object.__setattr__(self, "timezone", self.timezone)

    @property
    def non_zero_initial_positions(self) -> tuple[InitialPositionInput, ...]:
        """Mandatory fixed preflight subjects, in stable sorted order."""

        return self.initial_positions
