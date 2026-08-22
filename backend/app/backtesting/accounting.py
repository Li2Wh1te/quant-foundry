"""Accounting rules for the first low-frequency backtesting slice.

The runner owns a mutable :class:`PortfolioState`; this module is the only
domain service in the first version that turns an immutable fill fact into
cash and position changes.  It intentionally keeps persistence, order
submission, fee schedule selection, and slippage calculation outside the
accounting boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from app.backtesting.domain import (
    ZERO,
    AccountState,
    DomainValidationError,
    PortfolioSnapshot,
    PortfolioState,
    PositionSide,
    PositionState,
    ValuationStatus,
    _aware_datetime,
    _decimal,
    _positive,
    _validate_available_quantity,
)
from app.backtesting.fees import FeeBreakdown


class AccountingError(DomainValidationError):
    """Raised when a fill cannot be applied under the active policy."""


class InsufficientCashError(AccountingError):
    """Raised when a buy fill would exceed the account's available cash."""


class UnsupportedAccountingError(AccountingError):
    """Raised when the first policy receives an unsupported position state."""


class OrderSide(StrEnum):
    """The two sides supported by the first long-only accounting policy."""

    BUY = "buy"
    SELL = "sell"


class SettlementPolicy(StrEnum):
    """When newly bought units become available for sale."""

    SAME_DAY = "same_day"
    T_PLUS_ONE_BEFORE_OPEN_MATCH = "t_plus_1_before_open_match"
    # Retained as an in-memory fixture compatibility value.  Formal runs
    # should use the explicit versioned settlement category above.
    T_PLUS_ONE = "t_plus_1"


def _currency(value: str) -> str:
    """Normalize a currency code while keeping the policy single-currency."""

    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError("currency must be non-blank text")
    return value.strip().upper()


@dataclass(frozen=True, slots=True)
class Fill:
    """One immutable simulated execution fact consumed by accounting.

    ``price`` is the final execution price.  A reference price is retained for
    auditability, but it never participates in cash or position calculations.
    Fees are calculated before accounting and attached as an immutable
    ``FeeBreakdown`` when the execution pipeline is used.  The scalar ``fees``
    field remains as a compatibility boundary for low-level fixtures.
    """

    fill_id: UUID
    order_id: UUID
    instrument_id: UUID
    timestamp: datetime
    side: OrderSide
    price: Decimal | int | str
    quantity: Decimal | int | str
    fees: Decimal | int | str = ZERO
    currency: str = "CNY"
    reference_price: Decimal | int | str | None = None
    fee_breakdown: FeeBreakdown | None = None
    slippage_bps: Decimal | int | str | None = None
    slippage_amount: Decimal | int | str | None = None
    slippage_model_key: str | None = None
    slippage_model_version: int | None = None
    slippage_model_parameters: Mapping[str, Decimal | str] | None = None

    def __post_init__(self) -> None:
        """Normalize all numeric values and reject invalid execution facts."""

        _aware_datetime(self.timestamp, "timestamp")
        try:
            normalized_side = OrderSide(self.side)
        except ValueError as exc:
            raise DomainValidationError("side must be buy or sell") from exc
        object.__setattr__(self, "side", normalized_side)
        object.__setattr__(self, "price", _positive(self.price, "price"))
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        object.__setattr__(self, "fees", _decimal(self.fees, "fees"))
        if self.fees < ZERO:
            raise DomainValidationError("fees must be non-negative")
        object.__setattr__(self, "currency", _currency(self.currency))
        if self.fee_breakdown is not None:
            if self.fee_breakdown.currency != self.currency:
                raise DomainValidationError(
                    "fee breakdown currency must match fill currency"
                )
            if self.fees not in {ZERO, self.fee_breakdown.total}:
                raise DomainValidationError(
                    "fees must match the fee breakdown total"
                )
            object.__setattr__(self, "fees", self.fee_breakdown.total)
        if self.reference_price is not None:
            object.__setattr__(
                self,
                "reference_price",
                _positive(self.reference_price, "reference_price"),
            )
        if self.slippage_bps is not None:
            normalized_bps = _decimal(self.slippage_bps, "slippage_bps")
            if normalized_bps < ZERO:
                raise DomainValidationError("slippage_bps must be non-negative")
            object.__setattr__(self, "slippage_bps", normalized_bps)
        if self.slippage_amount is not None:
            normalized_amount = _decimal(self.slippage_amount, "slippage_amount")
            if normalized_amount < ZERO:
                raise DomainValidationError("slippage_amount must be non-negative")
            object.__setattr__(self, "slippage_amount", normalized_amount)
        if self.slippage_model_key is not None:
            if not isinstance(self.slippage_model_key, str) or not self.slippage_model_key.strip():
                raise DomainValidationError("slippage_model_key must be non-blank text")
            object.__setattr__(self, "slippage_model_key", self.slippage_model_key.strip())
        if self.slippage_model_version is not None and self.slippage_model_version <= 0:
            raise DomainValidationError("slippage_model_version must be positive")
        if self.slippage_model_parameters is not None:
            object.__setattr__(
                self,
                "slippage_model_parameters",
                MappingProxyType(dict(self.slippage_model_parameters)),
            )

    @property
    def notional(self) -> Decimal:
        """Return gross execution value before fees."""

        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class FillApplication:
    """Audit-friendly result of attempting to apply one fill."""

    fill_id: UUID
    applied: bool
    cash_delta: Decimal
    realized_pnl_delta: Decimal


@dataclass(frozen=True, slots=True)
class ValuationResult:
    """Result of one mark-to-market pass over the in-memory portfolio."""

    snapshot: PortfolioSnapshot
    market_value: Decimal | None


@dataclass(slots=True)
class AccountingPolicy:
    """Apply fills and value a single-currency, long-only portfolio.

    The policy keeps fill ids and pending T+1 units in runtime memory.  A
    future resumable runner can persist these facts alongside its run state;
    keeping them here now makes duplicate-fill protection and settlement
    semantics explicit without introducing a database table prematurely.
    """

    currency: str = "CNY"
    settlement_policy: SettlementPolicy = SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH
    _processed_fill_ids: set[UUID] = field(default_factory=set, init=False)
    _pending_settlement: dict[UUID, Decimal] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Normalize policy inputs before the first fill is processed."""

        self.currency = _currency(self.currency)
        try:
            self.settlement_policy = SettlementPolicy(self.settlement_policy)
        except ValueError as exc:
            raise DomainValidationError(
                "settlement_policy must be same_day, t_plus_1_before_open_match, or t_plus_1"
            ) from exc

    @property
    def _uses_deferred_settlement(self) -> bool:
        """Whether buys settle at the next session's pre-match boundary."""

        return self.settlement_policy in {
            SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH,
            SettlementPolicy.T_PLUS_ONE,
        }

    def apply_fill(self, portfolio: PortfolioState, fill: Fill) -> FillApplication:
        """Apply a fill atomically, or raise without changing portfolio state.

        A duplicate fill id is an idempotent no-op.  For buys, the available
        cash check includes fees and rejects the whole fill when it fails.  The
        method never creates partial fills and never silently clips a quantity.
        """

        if fill.fill_id in self._processed_fill_ids:
            return FillApplication(fill.fill_id, False, ZERO, ZERO)
        if fill.currency != self.currency:
            raise AccountingError(
                f"fill currency {fill.currency!r} does not match policy currency "
                f"{self.currency!r}"
            )
        if fill.timestamp < portfolio.as_of:
            raise AccountingError("fill timestamp cannot precede portfolio as_of")

        account = portfolio.account
        if self.currency not in account.cash_balances:
            raise AccountingError(
                f"account has no cash balance for currency {self.currency!r}"
            )
        current_cash = account.cash_balances[self.currency]
        if current_cash < ZERO:
            raise AccountingError("long-only cash account cannot start with negative cash")
        if account.available_cash != current_cash - account.frozen_cash:
            raise AccountingError(
                "available_cash must equal cash minus frozen_cash for the policy currency"
            )
        existing = portfolio.positions.get(fill.instrument_id)
        if existing is not None and existing.side is not PositionSide.LONG:
            raise UnsupportedAccountingError(
                "the first accounting policy supports long positions only"
            )

        if fill.side is OrderSide.BUY:
            return self._apply_buy(portfolio, fill, existing, current_cash)
        return self._apply_sell(portfolio, fill, existing, current_cash)

    def settle_pending(self, portfolio: PortfolioState) -> tuple[UUID, ...]:
        """Release all pending purchases at the next settlement boundary.

        The trading calendar owns the exact next-session date.  The runner
        calls this method at that boundary, so the policy does not guess over
        weekends or exchange holidays.
        """

        if not self._uses_deferred_settlement:
            return ()

        settled: list[UUID] = []
        for instrument_id, quantity in tuple(self._pending_settlement.items()):
            position = portfolio.positions.get(instrument_id)
            if position is None:
                raise AccountingError(
                    "pending settlement refers to a missing position"
                )
            new_available = position.available_quantity + quantity
            _validate_available_quantity(position.quantity, new_available)
            position.available_quantity = new_available
            settled.append(instrument_id)
            del self._pending_settlement[instrument_id]
        return tuple(sorted(settled, key=str))

    def value(
        self,
        portfolio: PortfolioState,
        marks: Mapping[UUID, Decimal | int | str],
        *,
        as_of: datetime,
    ) -> ValuationResult:
        """Mark every non-zero position and compute single-currency equity.

        Missing marks produce a blocked valuation and never become a zero
        price.  In that case ``market_value`` is ``None`` and the previous
        equity is left untouched because it is not a valid current valuation.
        """

        _aware_datetime(as_of, "as_of")
        if as_of < portfolio.as_of:
            raise AccountingError("valuation as_of cannot precede portfolio as_of")

        normalized_marks = {
            instrument_id: _positive(price, f"marks[{instrument_id}]")
            for instrument_id, price in marks.items()
        }
        missing = False
        market_value = ZERO
        for instrument_id, position in portfolio.positions.items():
            if position.side is not PositionSide.LONG:
                raise UnsupportedAccountingError(
                    "the first accounting policy supports long positions only"
                )
            mark = normalized_marks.get(instrument_id)
            if mark is None:
                position.mark_price = None
                position.unrealized_pnl = ZERO
                missing = True
                continue
            position.mark_price = mark
            position.unrealized_pnl = (mark - position.average_price) * position.quantity
            market_value += mark * position.quantity

        portfolio.as_of = as_of
        if missing:
            portfolio.valuation_status = ValuationStatus.BLOCKED
            return ValuationResult(portfolio.snapshot(), None)

        portfolio.account.equity = (
            portfolio.account.cash_balances[self.currency] + market_value
        )
        portfolio.valuation_status = ValuationStatus.COMPLETE
        return ValuationResult(portfolio.snapshot(), market_value)

    def _apply_buy(
        self,
        portfolio: PortfolioState,
        fill: Fill,
        existing: PositionState | None,
        current_cash: Decimal,
    ) -> FillApplication:
        """Prepare and commit one complete long buy."""

        cash_required = fill.notional + fill.fees
        if cash_required > portfolio.account.available_cash:
            raise InsufficientCashError(
                f"buy requires {cash_required} {self.currency}, but only "
                f"{portfolio.account.available_cash} is available"
            )

        if existing is None or existing.is_zero:
            new_quantity = fill.quantity
            new_average = cash_required / new_quantity
            new_available = (
                fill.quantity
                if not self._uses_deferred_settlement
                else ZERO
            )
            new_position = PositionState(
                instrument_id=fill.instrument_id,
                side=PositionSide.LONG,
                quantity=new_quantity,
                available_quantity=new_available,
                average_price=new_average,
            )
        else:
            old_cost = existing.average_price * existing.quantity
            new_quantity = existing.quantity + fill.quantity
            new_average = (old_cost + cash_required) / new_quantity
            available_increment = (
                fill.quantity
                if not self._uses_deferred_settlement
                else ZERO
            )
            new_available = existing.available_quantity + available_increment
            new_position = PositionState(
                instrument_id=fill.instrument_id,
                side=PositionSide.LONG,
                quantity=new_quantity,
                available_quantity=new_available,
                average_price=new_average,
                # A fill changes the position after the last valuation.  The
                # old mark must not be presented as a current mark until the
                # next explicit valuation pass.
                mark_price=None,
                realized_pnl=existing.realized_pnl,
                unrealized_pnl=ZERO,
            )

        # All validation above happens before mutating the live state.  This
        # keeps an insufficient-cash or malformed-fill failure atomic.
        account = portfolio.account
        account.cash_balances[self.currency] = current_cash - cash_required
        account.available_cash -= cash_required
        portfolio.positions[fill.instrument_id] = new_position
        if self._uses_deferred_settlement:
            self._pending_settlement[fill.instrument_id] = (
                self._pending_settlement.get(fill.instrument_id, ZERO) + fill.quantity
            )
        self._processed_fill_ids.add(fill.fill_id)
        portfolio.as_of = fill.timestamp
        portfolio.valuation_status = ValuationStatus.DEGRADED
        return FillApplication(fill.fill_id, True, -cash_required, ZERO)

    def _apply_sell(
        self,
        portfolio: PortfolioState,
        fill: Fill,
        existing: PositionState | None,
        current_cash: Decimal,
    ) -> FillApplication:
        """Prepare and commit one complete long sell."""

        if existing is None or existing.is_zero:
            raise AccountingError("cannot sell an instrument without a position")
        if fill.quantity > existing.available_quantity:
            raise AccountingError(
                "sell quantity exceeds available_quantity under the settlement policy"
            )

        realized_delta = (
            fill.notional - fill.fees - (existing.average_price * fill.quantity)
        )
        remaining_quantity = existing.quantity - fill.quantity
        remaining_available = existing.available_quantity - fill.quantity
        if remaining_quantity == ZERO:
            # The fill carries the realized result; the zero position is not
            # retained in result projections by design.
            new_position = None
        else:
            new_position = PositionState(
                instrument_id=fill.instrument_id,
                side=PositionSide.LONG,
                quantity=remaining_quantity,
                available_quantity=remaining_available,
                average_price=existing.average_price,
                mark_price=None,
                realized_pnl=existing.realized_pnl + realized_delta,
                unrealized_pnl=ZERO,
            )

        cash_delta = fill.notional - fill.fees
        account = portfolio.account
        account.cash_balances[self.currency] = current_cash + cash_delta
        account.available_cash += cash_delta
        if new_position is None:
            del portfolio.positions[fill.instrument_id]
        else:
            portfolio.positions[fill.instrument_id] = new_position
        self._processed_fill_ids.add(fill.fill_id)
        portfolio.as_of = fill.timestamp
        portfolio.valuation_status = ValuationStatus.DEGRADED
        return FillApplication(fill.fill_id, True, cash_delta, realized_delta)
