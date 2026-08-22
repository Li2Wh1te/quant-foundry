"""Immutable result-record DTOs for backtest runs.

Each class in this module is one validated, immutable row of the logical
result tables (steps, decisions, orders, order updates, fills, positions,
equity curve, metrics, data preflight reports, and data chunks).  The DTOs
are the boundary between the backtesting engine and persistence/query
layers: mutable runtime objects (``AccountState``, ``Order``, ...) are never
exposed through results.

Contracts enforced here:

- ``run_id`` and every business entity id are mandatory UUIDs;
- timestamps must be timezone-aware ``datetime`` values;
- money, prices, quantities, and ratios are ``Decimal``; binary ``float``
  is rejected everywhere, including nested JSON structures;
- instrument-bearing rows carry a point-in-time
  :class:`InstrumentDisplaySnapshot` keyed by the stable ``instrument_id``;
- no ETF-specific field (ts_code, fund type, creation/redemption state,
  ...) exists on any result object, so fictional assets, stocks, futures,
  and ETFs share the same DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import UUID

from app.backtesting.accounting import OrderSide
from app.backtesting.domain import (
    DomainValidationError,
    PositionSide,
    ValuationStatus,
    _aware_datetime,
    _decimal,
    _non_negative,
    _optional_price,
    _positive,
)
from app.backtesting.instrument_specs import InstrumentSpec, InstrumentSpecProvider


ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Shared normalization helpers
# ---------------------------------------------------------------------------


def _uuid(value: UUID, field_name: str) -> UUID:
    """Require a real UUID; identity fields are never free-form strings."""

    if not isinstance(value, UUID):
        raise DomainValidationError(f"{field_name} must be a UUID")
    return value


def _optional_uuid(value: UUID | None, field_name: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, field_name)


def _optional_text(value: str | None, field_name: str) -> str | None:
    """Normalize optional text; blank strings are treated as missing."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be text when provided")
    normalized = value.strip()
    return normalized or None


def _required_text(value: str, field_name: str) -> str:
    normalized = _optional_text(value, field_name)
    if normalized is None:
        raise DomainValidationError(f"{field_name} must be non-blank text")
    return normalized


def _sequence(value: int, field_name: str) -> int:
    """Require a non-negative, non-boolean integer sequence number."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an integer")
    if value < 0:
        raise DomainValidationError(f"{field_name} must be non-negative")
    return value


def _optional_int(value: int | None, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an integer when provided")
    return value


def _optional_decimal(value: Decimal | int | str | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field_name)


def _frozen_json(value: Any, field_name: str) -> Any:
    """Deep-freeze a JSON-like structure into immutable containers.

    Binary floats are rejected so persisted JSON can never lose precision;
    numeric values that are not exact integers must be passed as strings or
    ``Decimal``-free structures (serialize weights as strings, for example).
    """

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        # Keep exactness: JSON payloads carry decimals as canonical strings.
        return str(_decimal(value, field_name))
    if isinstance(value, float):
        raise DomainValidationError(
            f"{field_name} must not contain binary floats; use strings or integers"
        )
    if isinstance(value, Mapping):
        frozen_items: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DomainValidationError(f"{field_name} keys must be strings")
            frozen_items[key] = _frozen_json(item, f"{field_name}[{key!r}]")
        return MappingProxyType(frozen_items)
    if isinstance(value, (list, tuple)):
        return tuple(
            _frozen_json(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        )
    raise DomainValidationError(
        f"{field_name} must be a JSON-compatible structure (str/int/bool/None/"
        "mapping/sequence)"
    )


def _json_payload(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, Any]:
    """Freeze a mapping payload, defaulting to an empty read-only mapping."""

    if value is None:
        return MappingProxyType({})
    frozen = _frozen_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise DomainValidationError(f"{field_name} must be a mapping")
    return frozen


def _enum(value: StrEnum, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    try:
        return enum_type(getattr(value, "value", value))
    except ValueError as exc:
        allowed = [member.value for member in enum_type]
        raise DomainValidationError(
            f"{field_name} must be one of {allowed}"
        ) from exc


# ---------------------------------------------------------------------------
# Instrument display snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentDisplaySnapshot:
    """Point-in-time display fields frozen when a result row is written.

    ``instrument_id`` is the only association key.  The display fields are
    optional for every asset protocol and are never used to substitute the
    stable identity.  Historical queries read these frozen values; they must
    not be refreshed from today's catalogue.
    """

    instrument_id: UUID
    event_trading_code: str | None = None
    event_name: str | None = None
    event_display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _uuid(self.instrument_id, "instrument_id"))
        object.__setattr__(
            self,
            "event_trading_code",
            _optional_text(self.event_trading_code, "event_trading_code"),
        )
        object.__setattr__(
            self, "event_name", _optional_text(self.event_name, "event_name")
        )
        object.__setattr__(
            self,
            "event_display_name",
            _optional_text(self.event_display_name, "event_display_name"),
        )

    @classmethod
    def from_spec(cls, spec: InstrumentSpec) -> "InstrumentDisplaySnapshot":
        """Freeze the display fields of a query-time-valid spec."""

        return cls(
            instrument_id=spec.instrument_id,
            event_trading_code=spec.trading_code,
            event_name=spec.name,
            event_display_name=spec.display_name,
        )

    def require_matching_instrument(self, instrument_id: UUID, field_name: str) -> None:
        """Reject snapshots belonging to a different instrument."""

        if self.instrument_id != instrument_id:
            raise DomainValidationError(
                f"{field_name} snapshot instrument_id must match the row instrument_id"
            )


def resolve_display_snapshot(
    provider: InstrumentSpecProvider,
    instrument_id: UUID,
    *,
    as_of: datetime,
) -> InstrumentDisplaySnapshot:
    """Resolve display fields from a provider at the event time.

    A missing spec is not an error: asset protocols may not expose display
    fields, so the snapshot simply stays empty while ``instrument_id`` keeps
    carrying the identity.  Result repositories depend on the caller (or
    this helper) rather than on any concrete market-data client.
    """

    _aware_datetime(as_of, "as_of")
    spec = provider.resolve(_uuid(instrument_id, "instrument_id"), as_of=as_of)
    if spec is None:
        return InstrumentDisplaySnapshot(instrument_id=instrument_id)
    if spec.instrument_id != instrument_id:
        raise DomainValidationError("provider returned a mismatched instrument_id")
    return InstrumentDisplaySnapshot.from_spec(spec)


# ---------------------------------------------------------------------------
# Result enums (stable persisted values)
# ---------------------------------------------------------------------------


class StepPhase(StrEnum):
    """Coarse phase of one time step; the registry may grow without breaks."""

    DATA = "data"
    DECISION = "decision"
    EXECUTION = "execution"
    VALUATION = "valuation"
    FINALIZE = "finalize"


class DataQualityStatus(StrEnum):
    """Whether the step's input data passed quality checks."""

    OK = "ok"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class DecisionValidationStatus(StrEnum):
    """Outcome of validating one strategy decision."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ResultOrderStatus(StrEnum):
    """Lifecycle states persisted for result orders."""

    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DataPhase(StrEnum):
    """Run-level data phases shared by preflight reports and chunks."""

    ADMISSION = "admission"
    SESSION = "session"


class ChunkValidationStatus(StrEnum):
    """Validation outcome of one bounded data chunk."""

    PASSED = "passed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Result DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BacktestStepRecord:
    """One time step of a run (logical ``backtest_steps`` row)."""

    run_id: UUID
    step_sequence: int
    time_start: datetime
    time_end: datetime
    data_cutoff_at: datetime
    phase: StepPhase
    data_quality: DataQualityStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "step_sequence", _sequence(self.step_sequence, "step_sequence"))
        object.__setattr__(self, "time_start", _aware_datetime(self.time_start, "time_start"))
        object.__setattr__(self, "time_end", _aware_datetime(self.time_end, "time_end"))
        object.__setattr__(
            self, "data_cutoff_at", _aware_datetime(self.data_cutoff_at, "data_cutoff_at")
        )
        if self.time_start > self.time_end:
            raise DomainValidationError("time_start cannot be after time_end")
        object.__setattr__(self, "phase", _enum(self.phase, StepPhase, "phase"))
        object.__setattr__(
            self, "data_quality", _enum(self.data_quality, DataQualityStatus, "data_quality")
        )

    @property
    def cursor_sort_key(self) -> tuple[int]:
        return (self.step_sequence,)


@dataclass(frozen=True, slots=True)
class BacktestDecisionRecord:
    """One strategy decision (logical ``backtest_decisions`` row)."""

    run_id: UUID
    decision_id: UUID
    step_sequence: int
    decision_time: datetime
    mode: str
    validation_status: DecisionValidationStatus
    targets: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    validation_issues: Sequence[str] = ()
    duration_ms: Decimal | int | str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "decision_id", _uuid(self.decision_id, "decision_id"))
        object.__setattr__(self, "step_sequence", _sequence(self.step_sequence, "step_sequence"))
        object.__setattr__(
            self, "decision_time", _aware_datetime(self.decision_time, "decision_time")
        )
        object.__setattr__(self, "mode", _required_text(self.mode, "mode"))
        object.__setattr__(
            self,
            "validation_status",
            _enum(
                self.validation_status,
                DecisionValidationStatus,
                "validation_status",
            ),
        )
        object.__setattr__(self, "targets", _json_payload(self.targets, "targets"))
        normalized_issues = tuple(
            issue if isinstance(issue, str) and issue.strip() else None
            for issue in self.validation_issues
        )
        if any(issue is None for issue in normalized_issues):
            raise DomainValidationError("validation_issues must contain non-blank text")
        object.__setattr__(self, "validation_issues", normalized_issues)
        object.__setattr__(
            self, "duration_ms", _optional_decimal(self.duration_ms, "duration_ms")
        )
        object.__setattr__(self, "error", _optional_text(self.error, "error"))

    @property
    def cursor_sort_key(self) -> tuple[int, datetime, UUID]:
        return (self.step_sequence, self.decision_time, self.decision_id)


@dataclass(frozen=True, slots=True)
class BacktestOrderRecord:
    """One standard order (logical ``backtest_orders`` row)."""

    run_id: UUID
    order_id: UUID
    instrument_id: UUID
    display: InstrumentDisplaySnapshot
    side: OrderSide
    order_type: str
    quantity: Decimal | int | str
    status: ResultOrderStatus
    submitted_at: datetime
    intent_id: UUID | None = None
    price: Decimal | int | str | None = None
    filled_quantity: Decimal | int | str = ZERO
    status_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "order_id", _uuid(self.order_id, "order_id"))
        object.__setattr__(self, "instrument_id", _uuid(self.instrument_id, "instrument_id"))
        if not isinstance(self.display, InstrumentDisplaySnapshot):
            raise DomainValidationError("display must be an InstrumentDisplaySnapshot")
        self.display.require_matching_instrument(self.instrument_id, "display")
        try:
            side = OrderSide(getattr(self.side, "value", self.side))
        except ValueError as exc:
            raise DomainValidationError("side must be buy or sell") from exc
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "order_type", _required_text(self.order_type, "order_type"))
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        object.__setattr__(
            self,
            "status",
            _enum(self.status, ResultOrderStatus, "status"),
        )
        object.__setattr__(
            self, "submitted_at", _aware_datetime(self.submitted_at, "submitted_at")
        )
        object.__setattr__(self, "intent_id", _optional_uuid(self.intent_id, "intent_id"))
        object.__setattr__(self, "price", _optional_price(self.price, "price"))
        object.__setattr__(
            self, "filled_quantity", _non_negative(self.filled_quantity, "filled_quantity")
        )
        if self.filled_quantity > self.quantity:
            raise DomainValidationError("filled_quantity cannot exceed quantity")
        object.__setattr__(
            self, "status_reason", _optional_text(self.status_reason, "status_reason")
        )

    @property
    def cursor_sort_key(self) -> tuple[datetime, UUID]:
        return (self.submitted_at, self.order_id)


@dataclass(frozen=True, slots=True)
class BacktestOrderUpdateRecord:
    """One order status transition (logical ``backtest_order_updates`` row)."""

    run_id: UUID
    order_id: UUID
    update_sequence: int
    new_status: ResultOrderStatus
    updated_at: datetime
    old_status: ResultOrderStatus | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "order_id", _uuid(self.order_id, "order_id"))
        object.__setattr__(
            self, "update_sequence", _sequence(self.update_sequence, "update_sequence")
        )
        object.__setattr__(
            self,
            "new_status",
            _enum(self.new_status, ResultOrderStatus, "new_status"),
        )
        object.__setattr__(
            self, "updated_at", _aware_datetime(self.updated_at, "updated_at")
        )
        if self.old_status is not None:
            object.__setattr__(
                self,
                "old_status",
                _enum(self.old_status, ResultOrderStatus, "old_status"),
            )
        object.__setattr__(self, "reason", _optional_text(self.reason, "reason"))

    @property
    def cursor_sort_key(self) -> tuple[datetime, UUID, int]:
        return (self.updated_at, self.order_id, self.update_sequence)


@dataclass(frozen=True, slots=True)
class BacktestFillRecord:
    """One simulated fill fact (logical ``backtest_fills`` row)."""

    run_id: UUID
    fill_id: UUID
    order_id: UUID
    instrument_id: UUID
    display: InstrumentDisplaySnapshot
    side: OrderSide
    timestamp: datetime
    price: Decimal | int | str
    quantity: Decimal | int | str
    fees: Decimal | int | str = ZERO
    reference_price: Decimal | int | str | None = None
    slippage_bps: Decimal | int | str | None = None
    slippage_amount: Decimal | int | str | None = None
    slippage_model_key: str | None = None
    slippage_model_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "fill_id", _uuid(self.fill_id, "fill_id"))
        object.__setattr__(self, "order_id", _uuid(self.order_id, "order_id"))
        object.__setattr__(self, "instrument_id", _uuid(self.instrument_id, "instrument_id"))
        if not isinstance(self.display, InstrumentDisplaySnapshot):
            raise DomainValidationError("display must be an InstrumentDisplaySnapshot")
        self.display.require_matching_instrument(self.instrument_id, "display")
        try:
            side = OrderSide(getattr(self.side, "value", self.side))
        except ValueError as exc:
            raise DomainValidationError("side must be buy or sell") from exc
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "timestamp", _aware_datetime(self.timestamp, "timestamp"))
        object.__setattr__(self, "price", _positive(self.price, "price"))
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        object.__setattr__(self, "fees", _non_negative(self.fees, "fees"))
        object.__setattr__(
            self, "reference_price", _optional_price(self.reference_price, "reference_price")
        )
        object.__setattr__(
            self, "slippage_bps", _optional_decimal(self.slippage_bps, "slippage_bps")
        )
        object.__setattr__(
            self,
            "slippage_amount",
            _optional_decimal(self.slippage_amount, "slippage_amount"),
        )
        object.__setattr__(
            self,
            "slippage_model_key",
            _optional_text(self.slippage_model_key, "slippage_model_key"),
        )
        object.__setattr__(
            self,
            "slippage_model_version",
            _optional_int(self.slippage_model_version, "slippage_model_version"),
        )

    @property
    def cursor_sort_key(self) -> tuple[datetime, UUID]:
        return (self.timestamp, self.fill_id)


@dataclass(frozen=True, slots=True)
class BacktestPositionRecord:
    """One non-zero position at a valuation point (``backtest_positions`` row).

    Zero-quantity rows are rejected by construction: a missing row at a
    valuation point means the instrument is flat, and no zero row is ever
    synthesized.
    """

    run_id: UUID
    as_of: datetime
    instrument_id: UUID
    display: InstrumentDisplaySnapshot
    side: PositionSide
    quantity: Decimal | int | str
    available_quantity: Decimal | int | str
    average_price: Decimal | int | str
    realized_pnl: Decimal | int | str
    unrealized_pnl: Decimal | int | str
    mark_price: Decimal | int | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "as_of", _aware_datetime(self.as_of, "as_of"))
        object.__setattr__(self, "instrument_id", _uuid(self.instrument_id, "instrument_id"))
        if not isinstance(self.display, InstrumentDisplaySnapshot):
            raise DomainValidationError("display must be an InstrumentDisplaySnapshot")
        self.display.require_matching_instrument(self.instrument_id, "display")
        try:
            side = PositionSide(getattr(self.side, "value", self.side))
        except ValueError as exc:
            allowed = [member.value for member in PositionSide]
            raise DomainValidationError(f"side must be one of {allowed}") from exc
        object.__setattr__(self, "side", side)
        quantity = _positive(self.quantity, "quantity")
        available_quantity = _non_negative(
            self.available_quantity, "available_quantity"
        )
        if available_quantity > quantity:
            raise DomainValidationError(
                "available_quantity cannot exceed quantity"
            )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "available_quantity", available_quantity)
        object.__setattr__(
            self, "average_price", _positive(self.average_price, "average_price")
        )
        object.__setattr__(
            self, "mark_price", _optional_price(self.mark_price, "mark_price")
        )
        object.__setattr__(
            self, "realized_pnl", _decimal(self.realized_pnl, "realized_pnl")
        )
        object.__setattr__(
            self, "unrealized_pnl", _decimal(self.unrealized_pnl, "unrealized_pnl")
        )

    @property
    def cursor_sort_key(self) -> tuple[datetime, UUID, str]:
        return (self.as_of, self.instrument_id, self.side.value)


@dataclass(frozen=True, slots=True)
class BacktestEquityCurveRecord:
    """One account valuation point (logical ``backtest_equity_curve`` row).

    Field presence follows the same rule as the runtime equity curve:
    equity-derived values exist exactly when the valuation is not blocked.
    """

    run_id: UUID
    sequence: int
    as_of: datetime
    valuation_status: ValuationStatus
    cumulative_fees: Decimal | int | str = ZERO
    cash: Decimal | int | str | None = None
    market_value: Decimal | int | str | None = None
    equity: Decimal | int | str | None = None
    cumulative_return: Decimal | int | str | None = None
    drawdown: Decimal | int | str | None = None
    valuation_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "sequence", _sequence(self.sequence, "sequence"))
        object.__setattr__(self, "as_of", _aware_datetime(self.as_of, "as_of"))
        object.__setattr__(
            self,
            "valuation_status",
            _enum(self.valuation_status, ValuationStatus, "valuation_status"),
        )
        object.__setattr__(
            self, "cumulative_fees", _non_negative(self.cumulative_fees, "cumulative_fees")
        )
        for name in ("cash", "market_value", "equity", "cumulative_return", "drawdown"):
            object.__setattr__(
                self, name, _optional_decimal(getattr(self, name), name)
            )
        blocked = self.valuation_status is ValuationStatus.BLOCKED
        derived = ("market_value", "equity", "cumulative_return", "drawdown")
        if blocked:
            missing_value = any(
                getattr(self, name) is not None for name in ("market_value", "equity")
            )
            if missing_value:
                raise DomainValidationError(
                    "blocked valuation points cannot carry equity-derived values"
                )
        else:
            for name in ("cash", *derived):
                if getattr(self, name) is None:
                    raise DomainValidationError(
                        f"{name} is required for {self.valuation_status.value} valuations"
                    )
        object.__setattr__(
            self,
            "valuation_reason",
            _optional_text(self.valuation_reason, "valuation_reason"),
        )

    @property
    def cursor_sort_key(self) -> tuple[datetime, int]:
        return (self.as_of, self.sequence)


@dataclass(frozen=True, slots=True)
class BacktestMetricRecord:
    """One metric value (logical ``backtest_metrics`` row).

    A missing value is expressed as ``value = None`` plus a mandatory
    ``unavailable_reason``; metrics are never faked with zero.
    """

    run_id: UUID
    metric_key: str
    formula_version: str
    value: Decimal | int | str | None = None
    unit: str | None = None
    annualization_factor: Decimal | int | str | None = None
    risk_free_rate_note: str | None = None
    sample_count: int | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "metric_key", _required_text(self.metric_key, "metric_key"))
        object.__setattr__(
            self, "formula_version", _required_text(self.formula_version, "formula_version")
        )
        object.__setattr__(self, "value", _optional_decimal(self.value, "value"))
        object.__setattr__(self, "unit", _optional_text(self.unit, "unit"))
        object.__setattr__(
            self,
            "annualization_factor",
            _optional_decimal(self.annualization_factor, "annualization_factor"),
        )
        object.__setattr__(
            self,
            "risk_free_rate_note",
            _optional_text(self.risk_free_rate_note, "risk_free_rate_note"),
        )
        object.__setattr__(
            self, "sample_count", _optional_int(self.sample_count, "sample_count")
        )
        if self.sample_count is not None and self.sample_count < 0:
            raise DomainValidationError("sample_count must be non-negative")
        object.__setattr__(
            self,
            "unavailable_reason",
            _optional_text(self.unavailable_reason, "unavailable_reason"),
        )
        if (self.value is None) != (self.unavailable_reason is not None):
            raise DomainValidationError(
                "metrics must either carry a value or an unavailable_reason, never both"
            )

    @property
    def cursor_sort_key(self) -> tuple[str, str]:
        return (self.metric_key, self.formula_version)


@dataclass(frozen=True, slots=True)
class BacktestDataPreflightRecord:
    """One run-level data preflight report (``backtest_data_preflight`` row)."""

    run_id: UUID
    phase: DataPhase
    status: str
    report_hash: str
    capabilities: Mapping[str, Any] | None = None
    calendar_summary: Mapping[str, Any] | None = None
    session_summary: Mapping[str, Any] | None = None
    pit_status: str | None = None
    coverage: Mapping[str, Any] | None = None
    source_revisions: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "phase", _enum(self.phase, DataPhase, "phase"))
        object.__setattr__(self, "status", _required_text(self.status, "status"))
        object.__setattr__(
            self, "report_hash", _required_text(self.report_hash, "report_hash")
        )
        object.__setattr__(
            self, "pit_status", _optional_text(self.pit_status, "pit_status")
        )
        for name in (
            "capabilities",
            "calendar_summary",
            "session_summary",
            "coverage",
            "source_revisions",
        ):
            object.__setattr__(
                self, name, _json_payload(getattr(self, name), name)
            )

    @property
    def cursor_sort_key(self) -> tuple[str]:
        return (self.phase.value,)


@dataclass(frozen=True, slots=True)
class BacktestDataChunkRecord:
    """One bounded data chunk (logical ``backtest_data_chunks`` row)."""

    run_id: UUID
    phase: DataPhase
    chunk_sequence: int
    time_start: datetime
    time_end: datetime
    chunk_strategy_version: str
    token_digest: str
    validation_status: ChunkValidationStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "phase", _enum(self.phase, DataPhase, "phase"))
        object.__setattr__(
            self, "chunk_sequence", _sequence(self.chunk_sequence, "chunk_sequence")
        )
        object.__setattr__(self, "time_start", _aware_datetime(self.time_start, "time_start"))
        object.__setattr__(self, "time_end", _aware_datetime(self.time_end, "time_end"))
        if self.time_start > self.time_end:
            raise DomainValidationError("time_start cannot be after time_end")
        object.__setattr__(
            self,
            "chunk_strategy_version",
            _required_text(self.chunk_strategy_version, "chunk_strategy_version"),
        )
        object.__setattr__(
            self, "token_digest", _required_text(self.token_digest, "token_digest")
        )
        object.__setattr__(
            self,
            "validation_status",
            _enum(self.validation_status, ChunkValidationStatus, "validation_status"),
        )
        if self.started_at is not None:
            object.__setattr__(
                self, "started_at", _aware_datetime(self.started_at, "started_at")
            )
        if self.finished_at is not None:
            object.__setattr__(
                self, "finished_at", _aware_datetime(self.finished_at, "finished_at")
            )
        object.__setattr__(
            self, "failure_reason", _optional_text(self.failure_reason, "failure_reason")
        )
        if self.validation_status is ChunkValidationStatus.FAILED:
            if self.failure_reason is None:
                raise DomainValidationError("failed chunks require a failure_reason")
        elif self.failure_reason is not None:
            raise DomainValidationError("passed chunks cannot carry a failure_reason")

    @property
    def cursor_sort_key(self) -> tuple[str, int]:
        return (self.phase.value, self.chunk_sequence)


__all__ = [
    "ChunkValidationStatus",
    "DataPhase",
    "DataQualityStatus",
    "DecisionValidationStatus",
    "BacktestDataChunkRecord",
    "BacktestDecisionRecord",
    "BacktestEquityCurveRecord",
    "BacktestFillRecord",
    "BacktestMetricRecord",
    "BacktestOrderRecord",
    "BacktestOrderUpdateRecord",
    "BacktestPositionRecord",
    "BacktestDataPreflightRecord",
    "BacktestStepRecord",
    "InstrumentDisplaySnapshot",
    "ResultOrderStatus",
    "StepPhase",
    "resolve_display_snapshot",
]
