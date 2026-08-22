"""Equity curve recording for one backtesting run.

This module turns authoritative single-point valuation results produced by
the accounting/valuation layer into an append-only, auditable equity curve.
It deliberately does not select mark prices, compute fees, or re-apply
accounting rules: every number it records is either supplied by the caller
or derived from the formulas below.

For a valid (complete or degraded) valuation point::

    market_value_t = sum(mark_price_i * quantity_i)   # supplied by caller
    equity_t       = cash_t + market_value_t          # supplied by caller
    total_pnl_t    = equity_t - initial_equity
    nav_t          = equity_t / initial_equity
    period_return_t      = equity_t / equity_(previous_valid) - 1
    cumulative_return_t  = nav_t - 1
    peak_nav_t     = max(nav_0 ... nav_t)
    drawdown_t     = nav_t / peak_nav_t - 1          # negative when below peak

``blocked`` points keep ``cash``, ``cumulative_fees``, and audit fields, but
never carry a current ``equity`` or any equity-derived value, so a stale
account snapshot can never be serialized as a current valuation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    ValuationStatus,
    _aware_datetime,
    _decimal,
    _non_negative,
    _positive,
)

ONE = Decimal("1")


class EquityCurveError(DomainValidationError):
    """Raised when a valuation point cannot be appended to the curve."""


def _optional_reason(value: str | None, field_name: str) -> str | None:
    """Normalize an optional human-readable reason or quality note."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be non-blank text when provided")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ValuationPointInput:
    """Authoritative single-point valuation handed to the curve recorder.

    The accounting/valuation layer owns mark selection, fee handling, and
    equity rules; this object only carries its result across the boundary.
    For a ``blocked`` point the caller must leave ``equity`` and
    ``market_value`` unset, which makes it impossible to accidentally
    propagate a stale account equity into the curve.
    """

    as_of: datetime
    cash: Decimal | int | str
    valuation_status: ValuationStatus
    valuation_reason: str | None = None
    market_value: Decimal | int | str | None = None
    equity: Decimal | int | str | None = None
    cumulative_fees: Decimal | int | str = ZERO

    def __post_init__(self) -> None:
        """Normalize inputs and enforce per-status field contracts."""

        object.__setattr__(self, "as_of", _aware_datetime(self.as_of, "as_of"))
        object.__setattr__(self, "cash", _decimal(self.cash, "cash"))
        object.__setattr__(
            self,
            "cumulative_fees",
            _non_negative(self.cumulative_fees, "cumulative_fees"),
        )
        try:
            status = ValuationStatus(self.valuation_status)
        except ValueError as exc:
            raise DomainValidationError(
                "valuation_status must be complete, degraded, or blocked"
            ) from exc
        object.__setattr__(self, "valuation_status", status)
        object.__setattr__(
            self, "valuation_reason", _optional_reason(self.valuation_reason, "valuation_reason")
        )
        if status is ValuationStatus.BLOCKED:
            if self.equity is not None:
                raise DomainValidationError(
                    "blocked valuations must not carry a current equity"
                )
            if self.market_value is not None:
                raise DomainValidationError(
                    "blocked valuations must not carry a market value"
                )
            if self.valuation_reason is None:
                raise DomainValidationError(
                    "blocked valuations require a locatable valuation_reason"
                )
            return
        # Valid points need an authoritative positive equity so that NAV and
        # return denominators stay well defined for the first policy.
        object.__setattr__(self, "equity", _positive(self.equity, "equity"))
        object.__setattr__(
            self,
            "market_value",
            _non_negative(self.market_value, "market_value"),
        )


@dataclass(frozen=True, slots=True)
class EquityCurvePoint:
    """Immutable equity curve entry for one valuation point.

    Equity-derived fields (``market_value``, ``equity``, ``nav``,
    ``period_return``, ``cumulative_return``, ``drawdown``, ``total_pnl``)
    are ``None`` exactly when ``valuation_status`` is ``blocked``.
    """

    sequence: int
    as_of: datetime
    cash: Decimal
    cumulative_fees: Decimal
    valuation_status: ValuationStatus
    valuation_reason: str | None
    market_value: Decimal | None
    equity: Decimal | None
    nav: Decimal | None
    period_return: Decimal | None
    cumulative_return: Decimal | None
    drawdown: Decimal | None
    total_pnl: Decimal | None

    def __post_init__(self) -> None:
        """Validate invariants so the value object cannot be built invalid."""

        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise DomainValidationError("sequence must be an integer")
        if self.sequence < 0:
            raise DomainValidationError("sequence must be non-negative")
        object.__setattr__(self, "as_of", _aware_datetime(self.as_of, "as_of"))
        object.__setattr__(self, "cash", _decimal(self.cash, "cash"))
        object.__setattr__(
            self,
            "cumulative_fees",
            _non_negative(self.cumulative_fees, "cumulative_fees"),
        )
        try:
            status = ValuationStatus(self.valuation_status)
        except ValueError as exc:
            raise DomainValidationError(
                "valuation_status must be complete, degraded, or blocked"
            ) from exc
        object.__setattr__(self, "valuation_status", status)
        object.__setattr__(
            self, "valuation_reason", _optional_reason(self.valuation_reason, "valuation_reason")
        )
        derived = (
            "market_value",
            "equity",
            "nav",
            "period_return",
            "cumulative_return",
            "drawdown",
            "total_pnl",
        )
        if status is ValuationStatus.BLOCKED:
            for field_name in derived:
                if getattr(self, field_name) is not None:
                    raise DomainValidationError(
                        f"blocked curve points cannot carry {field_name}"
                    )
            if self.valuation_reason is None:
                raise DomainValidationError(
                    "blocked curve points require a valuation_reason"
                )
            return
        for field_name in derived:
            value = getattr(self, field_name)
            if value is None:
                raise DomainValidationError(
                    f"{field_name} is required when valuation_status is not blocked"
                )
            object.__setattr__(self, field_name, _decimal(value, field_name))
        object.__setattr__(self, "market_value", _non_negative(self.market_value, "market_value"))
        # NAV and return denominators stay well defined only when equity is
        # strictly positive; the value object enforces this on its own so the
        # constraint does not depend on the recorder or input path.
        object.__setattr__(self, "equity", _positive(self.equity, "equity"))


class EquityCurveRecorder:
    """Append valid and blocked valuation points onto one equity curve.

    The recorder keeps the ``initial_equity`` baseline, the most recent
    valid (complete or degraded) equity, and the running NAV peak.  It never
    queries prices, recomputes fees, or mutates portfolio state.
    """

    def __init__(self, initial_equity: Decimal | int | str) -> None:
        self._initial_equity = _positive(initial_equity, "initial_equity")
        self._points: list[EquityCurvePoint] = []
        self._last_as_of: datetime | None = None
        self._last_cumulative_fees = ZERO
        self._last_valid_equity: Decimal | None = None
        self._peak_nav: Decimal | None = None

    @property
    def initial_equity(self) -> Decimal:
        """Return the strictly positive NAV baseline of this run."""

        return self._initial_equity

    @property
    def points(self) -> tuple[EquityCurvePoint, ...]:
        """Return recorded points in stable ``(as_of, sequence)`` order."""

        return tuple(self._points)

    @property
    def peak_nav(self) -> Decimal | None:
        """Return the highest NAV seen at a valid point, if any."""

        return self._peak_nav

    @property
    def last_valid_equity(self) -> Decimal | None:
        """Return the most recent valid equity, ignoring blocked points."""

        return self._last_valid_equity

    def record(self, valuation_point: ValuationPointInput) -> EquityCurvePoint:
        """Validate and append one valuation point, returning its curve entry.

        Timestamps must be strictly increasing, so duplicate ``as_of`` values
        are rejected instead of being silently overwritten.  ``blocked``
        points advance the timeline but neither become the next return
        baseline nor update the NAV peak.
        """

        if not isinstance(valuation_point, ValuationPointInput):
            raise EquityCurveError("valuation_point must be a ValuationPointInput")
        if (
            self._last_as_of is not None
            and valuation_point.as_of <= self._last_as_of
        ):
            raise EquityCurveError(
                "valuation as_of must be strictly greater than the previous point"
            )
        if valuation_point.cumulative_fees < self._last_cumulative_fees:
            raise EquityCurveError(
                "cumulative_fees cannot decrease from the previously recorded value"
            )

        sequence = len(self._points)
        if valuation_point.valuation_status is ValuationStatus.BLOCKED:
            point = EquityCurvePoint(
                sequence=sequence,
                as_of=valuation_point.as_of,
                cash=valuation_point.cash,
                cumulative_fees=valuation_point.cumulative_fees,
                valuation_status=valuation_point.valuation_status,
                valuation_reason=valuation_point.valuation_reason,
                market_value=None,
                equity=None,
                nav=None,
                period_return=None,
                cumulative_return=None,
                drawdown=None,
                total_pnl=None,
            )
        else:
            assert valuation_point.equity is not None
            baseline = (
                self._last_valid_equity
                if self._last_valid_equity is not None
                else self._initial_equity
            )
            period_return = valuation_point.equity / baseline - ONE
            nav = valuation_point.equity / self._initial_equity
            cumulative_return = nav - ONE
            peak = nav if self._peak_nav is None else max(self._peak_nav, nav)
            drawdown = nav / peak - ONE
            total_pnl = valuation_point.equity - self._initial_equity
            point = EquityCurvePoint(
                sequence=sequence,
                as_of=valuation_point.as_of,
                cash=valuation_point.cash,
                cumulative_fees=valuation_point.cumulative_fees,
                valuation_status=valuation_point.valuation_status,
                valuation_reason=valuation_point.valuation_reason,
                market_value=valuation_point.market_value,
                equity=valuation_point.equity,
                nav=nav,
                period_return=period_return,
                cumulative_return=cumulative_return,
                drawdown=drawdown,
                total_pnl=total_pnl,
            )
            self._last_valid_equity = valuation_point.equity
            self._peak_nav = peak

        self._points.append(point)
        self._last_as_of = valuation_point.as_of
        self._last_cumulative_fees = valuation_point.cumulative_fees
        return point
