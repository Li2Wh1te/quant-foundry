"""Versioned slippage models for simulated executions.

Slippage is deliberately a pure calculation.  It receives a reference price
and returns an execution price; it does not inspect orders, account state, or
market-data repositories.  That keeps the same model reusable by different
execution policies and makes the frozen model parameters auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from types import MappingProxyType
from typing import Mapping, Protocol

from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    _decimal,
    _positive,
)


class SlippageError(DomainValidationError):
    """Raised when a slippage model receives an invalid calculation input."""


class SlippageModel(Protocol):
    """Structural contract consumed by execution models.

    Keeping this protocol independent of orders lets future asset-specific or
    volume-aware models plug into the same execution boundary.
    """

    def apply(
        self,
        reference_price: Decimal | int | str,
        side: object,
        *,
        price_tick: Decimal | int | str | None = None,
    ) -> SlippageResult:
        """Calculate one adverse execution price."""


def _side(value: object) -> str:
    """Normalize the small side contract without importing order modules."""

    normalized = getattr(value, "value", value)
    if not isinstance(normalized, str) or normalized not in {"buy", "sell"}:
        raise SlippageError("side must be buy or sell")
    return normalized


@dataclass(frozen=True, slots=True)
class SlippageResult:
    """Auditable result of applying one slippage model to one price."""

    reference_price: Decimal
    execution_price: Decimal
    slippage_bps: Decimal
    price_delta: Decimal
    model_key: str
    model_version: int
    parameters: Mapping[str, Decimal | str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference_price", _positive(self.reference_price, "reference_price"))
        object.__setattr__(self, "execution_price", _positive(self.execution_price, "execution_price"))
        object.__setattr__(self, "slippage_bps", _decimal(self.slippage_bps, "slippage_bps"))
        object.__setattr__(self, "price_delta", _decimal(self.price_delta, "price_delta"))
        if self.slippage_bps < ZERO:
            raise SlippageError("slippage_bps must be non-negative")
        if not isinstance(self.model_key, str) or not self.model_key.strip():
            raise SlippageError("model_key must be non-blank text")
        if self.model_version <= 0:
            raise SlippageError("model_version must be positive")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class BpsSlippageModel:
    """Apply basis-point slippage and adverse price-tick rounding.

    ``none@1`` is represented by the same implementation with a zero bps
    value.  It remains explicit in the audit fields instead of using a null
    model to imply zero slippage.
    """

    slippage_bps: Decimal | int | str = ZERO
    price_tick: Decimal | int | str = "0.01"
    model_key: str = "bps"
    model_version: int = 1

    def __post_init__(self) -> None:
        bps = _decimal(self.slippage_bps, "slippage_bps")
        if bps < ZERO:
            raise SlippageError("slippage_bps must be non-negative")
        tick = _positive(self.price_tick, "price_tick")
        if not isinstance(self.model_key, str) or not self.model_key.strip():
            raise SlippageError("model_key must be non-blank text")
        if self.model_version <= 0:
            raise SlippageError("model_version must be positive")
        object.__setattr__(self, "slippage_bps", bps)
        object.__setattr__(self, "price_tick", tick)
        object.__setattr__(self, "model_key", self.model_key.strip())

    @classmethod
    def none(cls, *, price_tick: Decimal | int | str = "0.01") -> "BpsSlippageModel":
        """Build the explicit zero-slippage ``none@1`` model."""

        return cls(slippage_bps=ZERO, price_tick=price_tick, model_key="none")

    @property
    def parameters(self) -> Mapping[str, Decimal | str]:
        """Return an immutable model-parameter snapshot."""

        return MappingProxyType(
            {
                "slippage_bps": self.slippage_bps,
                "price_tick": self.price_tick,
            }
        )

    def apply(
        self,
        reference_price: Decimal | int | str,
        side: object,
        *,
        price_tick: Decimal | int | str | None = None,
    ) -> SlippageResult:
        """Return an adverse, tick-rounded execution price.

        The instrument's PIT ``price_tick`` may override the model's default
        fixture tick.  This keeps the model reusable across instruments while
        retaining a convenient default for isolated unit tests.
        """

        reference = _positive(reference_price, "reference_price")
        normalized_side = _side(side)
        tick = _positive(
            self.price_tick if price_tick is None else price_tick,
            "price_tick",
        )
        bps_factor = self.slippage_bps / Decimal("10000")
        if normalized_side == "buy":
            slipped = reference * (Decimal("1") + bps_factor)
            rounding = ROUND_CEILING
        else:
            slipped = reference * (Decimal("1") - bps_factor)
            if slipped <= ZERO:
                raise SlippageError("slippage makes sell execution price non-positive")
            rounding = ROUND_FLOOR

        tick_count = (slipped / tick).to_integral_value(rounding=rounding)
        execution_price = tick_count * tick
        if execution_price <= ZERO:
            raise SlippageError("rounded execution price must be positive")
        return SlippageResult(
            reference_price=reference,
            execution_price=execution_price,
            slippage_bps=self.slippage_bps,
            price_delta=execution_price - reference,
            model_key=self.model_key,
            model_version=self.model_version,
            parameters=MappingProxyType(
                {
                    "slippage_bps": self.slippage_bps,
                    "price_tick": tick,
                }
            ),
        )
