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
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
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
from app.instruments.domain import InstrumentDisplay, InstrumentDisplayProvider


def _require_sha256_signature(value: str, field_name: str) -> str:
    """Validate the canonical lowercase SHA-256 evidence identifier shape."""

    normalized = _required_text(value, field_name)
    if (
        len(normalized) != 71
        or not normalized.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in normalized[7:])
    ):
        raise DomainValidationError(
            f"{field_name} must be sha256:<64 lowercase hex digits>"
        )
    return normalized


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


def _reject_preflight_sensitive_keys(value: Any, field_name: str) -> None:
    """Reject raw credentials/tokens from persisted preflight evidence.

    Digests and capability labels are safe audit values; raw token material,
    credentials, and secrets are not.  This recursive guard is intentionally
    key based because the result DTO only accepts JSON-shaped evidence and
    must fail before SQLAlchemy gets a chance to persist it.
    """

    forbidden = {
        "token",
        "raw_token",
        "access_token",
        "credential",
        "credentials",
        "secret",
        "password",
        "api_key",
        "access_key",
    }

    def visit(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized = str(key).strip().lower()
                if normalized in forbidden:
                    raise DomainValidationError(
                        f"{field_name} must not contain raw credential/token field {path}.{key}"
                    )
                visit(child, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value, field_name)


def _formal_timeline_payload(
    value: Mapping[str, Any], field_name: str
) -> Mapping[str, Any]:
    """Validate and canonicalize one persisted FormalSessionTimeline payload.

    Summary rows are an output of the admission boundary, but callers can
    still construct DTOs directly. Re-validating the DTO shape here prevents
    a hand-written sessions/hash pair from being persisted as if it were the
    coordinator-issued timeline. ``None`` remains supported by this helper's
    caller for legacy summaries created before the timeline column existed.
    """

    if not isinstance(value, Mapping):
        raise DomainValidationError(f"{field_name} must be a mapping")
    expected_keys = {"contract", "sessions", "timeline_hash"}
    if set(value) != expected_keys:
        raise DomainValidationError(
            f"{field_name} must contain exactly contract, sessions, and timeline_hash"
        )
    if value["contract"] != "formal_timeline_v1":
        raise DomainValidationError(
            f"{field_name}.contract must be 'formal_timeline_v1'"
        )
    sessions = value["sessions"]
    if not isinstance(sessions, (list, tuple)):
        raise DomainValidationError(f"{field_name}.sessions must be a sequence")
    parsed_sessions: list[date] = []
    for index, item in enumerate(sessions):
        if isinstance(item, date) and not isinstance(item, datetime):
            parsed_sessions.append(item)
            continue
        if not isinstance(item, str):
            raise DomainValidationError(
                f"{field_name}.sessions[{index}] must be an ISO calendar date"
            )
        try:
            parsed_sessions.append(date.fromisoformat(item))
        except ValueError as exc:
            raise DomainValidationError(
                f"{field_name}.sessions[{index}] is not an ISO calendar date"
            ) from exc
    from app.backtesting.analysis_inputs import FormalSessionTimeline

    timeline = FormalSessionTimeline(
        tuple(parsed_sessions), timeline_hash=value["timeline_hash"]
    )
    return _json_payload(timeline.as_payload(), field_name)


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
    def from_display(cls, display: InstrumentDisplay) -> "InstrumentDisplaySnapshot":
        """Freeze the display fields of a point-in-time-valid display object."""

        return cls(
            instrument_id=display.instrument_id,
            event_trading_code=display.trading_code,
            event_name=display.name,
            event_display_name=display.display_name,
        )

    def require_matching_instrument(self, instrument_id: UUID, field_name: str) -> None:
        """Reject snapshots belonging to a different instrument."""

        if self.instrument_id != instrument_id:
            raise DomainValidationError(
                f"{field_name} snapshot instrument_id must match the row instrument_id"
            )


def resolve_display_snapshot(
    provider: InstrumentDisplayProvider,
    instrument_id: UUID,
    *,
    effective_at: datetime,
    data_cutoff: datetime,
) -> InstrumentDisplaySnapshot:
    """Resolve display fields from a provider for one market instant.

    ``effective_at`` selects the market instant the display info must be
    valid at; ``data_cutoff`` limits the knowledge the provider may use.
    The two timestamps are intentionally separate parameters — never merge
    them back into one ambiguous ``as_of``.  A missing display is not an
    error: asset protocols may not expose display fields, so the snapshot
    simply stays empty while ``instrument_id`` keeps carrying the identity.
    Result repositories depend on the caller (or this helper) rather than
    on any concrete market-data client.
    """

    _aware_datetime(effective_at, "effective_at")
    _aware_datetime(data_cutoff, "data_cutoff")
    display = provider.resolve_display(
        _uuid(instrument_id, "instrument_id"),
        effective_at=effective_at,
        data_cutoff=data_cutoff,
    )
    if display is None:
        return InstrumentDisplaySnapshot(instrument_id=instrument_id)
    if display.instrument_id != instrument_id:
        raise DomainValidationError("provider returned a mismatched instrument_id")
    return InstrumentDisplaySnapshot.from_display(display)


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


class AnalysisSummaryStatus(StrEnum):
    """Run-level analysis lifecycle persisted on the summary table."""

    PARTIAL = "partial"
    FINAL = "final"
    ABORTED = "aborted"


class AnalyzerState(StrEnum):
    """How a metric row's analyzer identity relates to the registry.

    ``legacy`` marks rows written before analyzer identity existed;
    ``unknown`` marks rows whose declared identity no longer resolves in
    the current ComponentRegistry; ``registered`` marks resolvable rows.
    """

    LEGACY = "legacy"
    UNKNOWN = "unknown"
    REGISTERED = "registered"


@lru_cache(maxsize=1)
def _default_component_registry():
    from app.backtesting.registry import build_default_component_registry

    return build_default_component_registry()


def resolve_analyzer_state(
    analyzer_key: str | None,
    analyzer_version: int | None,
    metric_key: str | None = None,
    formula_version: str | None = None,
) -> AnalyzerState:
    """Classify a metric row's analyzer identity against the registry.

    Rows without identity are legacy data; rows whose declared identity no
    longer resolves are unknown; everything else is registered.
    """

    if analyzer_key is None or analyzer_version is None:
        return AnalyzerState.LEGACY
    try:
        entry = _default_component_registry().resolve(analyzer_key, analyzer_version)
    except Exception:
        return AnalyzerState.UNKNOWN
    if getattr(entry, "component_kind", None) != "analyzer":
        return AnalyzerState.UNKNOWN
    if metric_key is not None and formula_version is not None:
        try:
            from app.backtesting.analyzers import frozen_output_contract_for

            declared = {
                (item.metric_key, item.formula_version)
                for item in frozen_output_contract_for(
                    analyzer_key, analyzer_version
                )
            }
        except Exception:
            return AnalyzerState.UNKNOWN
        if (metric_key, formula_version) not in declared:
            return AnalyzerState.UNKNOWN
    return AnalyzerState.REGISTERED


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
    # The existing JSON column is also the audit surface for structured
    # candidate qualification evidence.  Legacy callers may keep supplying
    # plain strings; final PIT rechecks add JSON mappings without requiring a
    # candidate-specific table or migration.
    validation_issues: Sequence[str | Mapping[str, Any]] = ()
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
        if self.validation_issues is None or isinstance(
            self.validation_issues, (str, bytes, bytearray)
        ):
            raise DomainValidationError("validation_issues must be a sequence")
        normalized_issues: list[Any] = []
        for index, issue in enumerate(self.validation_issues):
            if isinstance(issue, str):
                if not issue.strip():
                    raise DomainValidationError(
                        "validation_issues must contain non-blank text"
                    )
                normalized_issues.append(issue.strip())
                continue
            if isinstance(issue, Mapping):
                frozen_issue = _frozen_json(
                    issue, f"validation_issues[{index}]"
                )
                if not isinstance(frozen_issue, Mapping):
                    raise DomainValidationError(
                        "validation_issues mapping entries must be JSON objects"
                    )
                normalized_issues.append(frozen_issue)
                continue
            raise DomainValidationError(
                "validation_issues entries must be non-blank text or JSON mappings"
            )
        if any(issue is None for issue in normalized_issues):
            raise DomainValidationError("validation_issues must contain non-blank text")
        object.__setattr__(self, "validation_issues", tuple(normalized_issues))
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
    currency: str = "CNY"
    contract_multiplier: Decimal | int | str = "1"
    gross_notional: Decimal | int | str | None = None
    fee_breakdown: Mapping[str, Any] | None = None
    settlement_calendar_id: str | None = None
    settlement_due_session: date | None = None
    settlement_boundary_id: str | None = None
    # Stable identity of the pending-settlement lot this buy fill produced;
    # sells carry no lot and stay ``None``.
    settlement_lot_id: UUID | None = None

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
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise DomainValidationError("currency must be non-blank text")
        object.__setattr__(self, "currency", self.currency.strip().upper())
        multiplier = _decimal(self.contract_multiplier, "contract_multiplier")
        if multiplier <= ZERO:
            raise DomainValidationError("contract_multiplier must be positive")
        object.__setattr__(self, "contract_multiplier", multiplier)
        if self.gross_notional is None:
            object.__setattr__(
                self,
                "gross_notional",
                _positive(self.price, "price")
                * _positive(self.quantity, "quantity")
                * multiplier,
            )
        else:
            declared = _decimal(self.gross_notional, "gross_notional")
            derived = (
                _positive(self.price, "price")
                * _positive(self.quantity, "quantity")
                * multiplier
            )
            if declared != derived:
                raise DomainValidationError(
                    f"declared gross_notional {declared} does not match "
                    f"price x quantity x contract_multiplier {derived}"
                )
            object.__setattr__(self, "gross_notional", derived)
        if self.fee_breakdown is not None:
            object.__setattr__(
                self, "fee_breakdown", _frozen_json(self.fee_breakdown, "fee_breakdown")
            )
        for field_name in (
            "settlement_calendar_id",
            "settlement_boundary_id",
        ):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, _optional_text(value, field_name))
        if self.settlement_due_session is not None:
            if not isinstance(self.settlement_due_session, date) or isinstance(
                self.settlement_due_session, datetime
            ):
                raise DomainValidationError(
                    "settlement_due_session must be a calendar date"
                )
        if self.settlement_lot_id is not None and not isinstance(
            self.settlement_lot_id, UUID
        ):
            raise DomainValidationError(
                "settlement_lot_id must be a UUID when provided"
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
    period_return: Decimal | int | str | None = None
    total_pnl: Decimal | int | str | None = None
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
        for name in (
            "cash",
            "market_value",
            "equity",
            "period_return",
            "total_pnl",
            "cumulative_return",
            "drawdown",
        ):
            object.__setattr__(
                self, name, _optional_decimal(getattr(self, name), name)
            )
        blocked = self.valuation_status is ValuationStatus.BLOCKED
        derived = (
            "market_value",
            "equity",
            "period_return",
            "total_pnl",
            "cumulative_return",
            "drawdown",
        )
        # Normalize text BEFORE validating so blank strings cannot pose as a
        # present reason (they normalize to None).
        normalized_reason = _optional_text(
            self.valuation_reason, "valuation_reason"
        )
        if blocked:
            if any(getattr(self, name) is not None for name in derived):
                raise DomainValidationError(
                    "blocked valuation points cannot carry equity-derived values"
                )
            if self.cash is None:
                raise DomainValidationError(
                    "blocked valuation points must still carry the cash balance"
                )
            if normalized_reason is None:
                raise DomainValidationError(
                    "blocked valuation points require a valuation_reason"
                )
        else:
            for name in ("cash", *derived):
                if getattr(self, name) is None:
                    raise DomainValidationError(
                        f"{name} is required for {self.valuation_status.value} valuations"
                    )
        object.__setattr__(self, "valuation_reason", normalized_reason)

    @property
    def cursor_sort_key(self) -> tuple[datetime, int]:
        return (self.as_of, self.sequence)


@dataclass(frozen=True, slots=True)
class BacktestMetricRecord:
    """One metric value (logical ``backtest_metrics`` row).

    A missing value is expressed as ``value = None`` plus a mandatory
    ``unavailable_reason``; metrics are never faked with zero.  New rows
    carry their producing analyzer identity as a pair; rows without any
    identity are legacy data and are never backfilled from ``metric_key``.
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
    analyzer_key: str | None = None
    analyzer_version: int | None = None
    analyzer_metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "metric_key", _required_text(self.metric_key, "metric_key"))
        object.__setattr__(
            self, "formula_version", _required_text(self.formula_version, "formula_version")
        )
        if len(self.metric_key) > 100:
            raise DomainValidationError("metric_key must not exceed 100 characters")
        if len(self.formula_version) > 64:
            raise DomainValidationError("formula_version must not exceed 64 characters")
        object.__setattr__(self, "value", _optional_decimal(self.value, "value"))
        object.__setattr__(self, "unit", _optional_text(self.unit, "unit"))
        if self.unit is not None and len(self.unit) > 32:
            raise DomainValidationError("unit must not exceed 32 characters")
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
        if self.risk_free_rate_note is not None and len(self.risk_free_rate_note) > 200:
            raise DomainValidationError(
                "risk_free_rate_note must not exceed 200 characters"
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
        # Analyzer identity is strictly paired: both fields present (new
        # rows) or both absent (legacy rows).
        has_key = self.analyzer_key is not None
        has_version = self.analyzer_version is not None
        if has_key != has_version:
            raise DomainValidationError(
                "analyzer_key and analyzer_version must be provided together"
            )
        if self.analyzer_key is not None:
            normalized_key = _required_text(self.analyzer_key, "analyzer_key")
            if len(normalized_key) > 100:
                raise DomainValidationError("analyzer_key must not exceed 100 characters")
            object.__setattr__(self, "analyzer_key", normalized_key)
        if has_version and (
            isinstance(self.analyzer_version, bool)
            or not isinstance(self.analyzer_version, int)
            or self.analyzer_version <= 0
        ):
            raise DomainValidationError("analyzer_version must be a positive integer")
        if self.analyzer_metadata is not None:
            if not isinstance(self.analyzer_metadata, Mapping):
                raise DomainValidationError(
                    "analyzer_metadata must be a mapping when provided"
                )
            object.__setattr__(
                self,
                "analyzer_metadata",
                _json_payload(self.analyzer_metadata, "analyzer_metadata"),
            )

    @property
    def analyzer_state(self) -> AnalyzerState:
        """Registry-relative state of this row's analyzer identity."""

        return resolve_analyzer_state(
            self.analyzer_key,
            self.analyzer_version,
            self.metric_key,
            self.formula_version,
        )

    @property
    def cursor_sort_key(self) -> tuple[str, str]:
        return (self.metric_key, self.formula_version)


@dataclass(frozen=True, slots=True)
class BacktestAnalysisSummaryRecord:
    """Run-level analyzer summary (logical ``backtest_analysis_summaries`` row).

    ``partial`` rows are progress checkpoints and never carry a
    finalization time; ``final``/``aborted`` are terminal states whose
    writes the repository refuses to overwrite with conflicting content.
    """

    run_id: UUID
    status: AnalysisSummaryStatus | str
    analyzer_snapshot: Mapping[str, Any] | None = None
    # Explicit immutable formal-session contract; this is kept as a JSON
    # value at the persistence boundary so summary readers do not need to
    # reconstruct it from analyzer_snapshot.
    formal_timeline: Mapping[str, Any] | None = None
    formula_signature: str = ""
    input_evidence_signature: str = ""
    reporting_currency: str = "CNY"
    initial_equity: Decimal | int | str | None = None
    valid_day_count: int | None = None
    candidate_return_count: int | None = None
    fill_count: int | None = None
    gross_traded_notional: Decimal | int | str | None = None
    cumulative_fees: Decimal | int | str | None = None
    rate_snapshot: Mapping[str, Any] | Sequence[Any] | None = None
    rate_snapshot_hash: str | None = None
    rate_source_versions: Mapping[str, Any] | None = None
    missing_ranges: Sequence[Any] | None = None
    last_chunk_sequence: int | None = None
    last_chunk_token: str | None = None
    completed_through_session: date | None = None
    abort_reason: str | None = None
    failed_step_sequence: int | None = None
    terminal_fingerprint: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finalized_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "status",
            _enum(self.status, AnalysisSummaryStatus, "status"),
        )
        object.__setattr__(
            self,
            "analyzer_snapshot",
            _json_payload(self.analyzer_snapshot, "analyzer_snapshot"),
        )
        object.__setattr__(
            self,
            "formal_timeline",
            (
                _formal_timeline_payload(self.formal_timeline, "formal_timeline")
                if self.formal_timeline is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "formula_signature",
            _require_sha256_signature(self.formula_signature, "formula_signature"),
        )
        object.__setattr__(
            self,
            "input_evidence_signature",
            _require_sha256_signature(
                self.input_evidence_signature, "input_evidence_signature"
            ),
        )
        if not isinstance(self.reporting_currency, str) or not (
            normalized := self.reporting_currency.strip()
        ):
            raise DomainValidationError(
                "reporting_currency must be non-blank text"
            )
        object.__setattr__(self, "reporting_currency", normalized.upper())
        for name in ("initial_equity", "gross_traded_notional", "cumulative_fees"):
            object.__setattr__(
                self, name, _optional_decimal(getattr(self, name), name)
            )
        # Summary monetary columns are stored in NUMERIC(38,18). Keep
        # producer metadata as exact formula evidence, but normalize E0 and
        # accounting aggregates at this explicit persistence-shape boundary.
        from app.backtesting.analyzers import quantize_for_persistence

        for name in (
            "initial_equity",
            "gross_traded_notional",
            "cumulative_fees",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, quantize_for_persistence(value))
        for name in (
            "valid_day_count",
            "candidate_return_count",
            "fill_count",
            "last_chunk_sequence",
            "failed_step_sequence",
        ):
            object.__setattr__(
                self, name, _optional_int(getattr(self, name), name)
            )
            if getattr(self, name) is not None and getattr(self, name) < 0:
                raise DomainValidationError(f"{name} must be non-negative")
        if self.initial_equity is not None and self.initial_equity <= 0:
            raise DomainValidationError("initial_equity must be strictly positive")
        if self.gross_traded_notional is not None and self.gross_traded_notional < 0:
            raise DomainValidationError("gross_traded_notional must be non-negative")
        if self.cumulative_fees is not None and self.cumulative_fees < 0:
            raise DomainValidationError("cumulative_fees must be non-negative")
        if (
            self.candidate_return_count is not None
            and self.valid_day_count is not None
            and self.candidate_return_count > self.valid_day_count
        ):
            raise DomainValidationError(
                "candidate_return_count must not exceed valid_day_count"
            )
        if self.rate_source_versions is not None:
            if not isinstance(self.rate_source_versions, Mapping):
                raise DomainValidationError("rate_source_versions must be a mapping")
            object.__setattr__(
                self,
                "rate_source_versions",
                _frozen_json(self.rate_source_versions, "rate_source_versions"),
            )
        if self.rate_snapshot is not None:
            frozen = _frozen_json(self.rate_snapshot, "rate_snapshot")
            object.__setattr__(self, "rate_snapshot", frozen)
        if self.missing_ranges is not None:
            if isinstance(self.missing_ranges, (str, bytes, bytearray)):
                raise DomainValidationError("missing_ranges must be a sequence")
            normalized_ranges: list[Mapping[str, Any]] = []
            previous_end: date | None = None
            for item in self.missing_ranges:
                if not isinstance(item, Mapping) or set(item) != {
                    "start_session",
                    "end_session",
                }:
                    raise DomainValidationError(
                        "missing_ranges entries must contain start_session and end_session"
                    )
                try:
                    start = date.fromisoformat(item["start_session"])
                    end = date.fromisoformat(item["end_session"])
                except (TypeError, ValueError) as exc:
                    raise DomainValidationError(
                        "missing_ranges sessions must be ISO calendar dates"
                    ) from exc
                if end < start:
                    raise DomainValidationError(
                        "missing_ranges end_session must not precede start_session"
                    )
                if previous_end is not None and start <= previous_end:
                    raise DomainValidationError(
                        "missing_ranges must be strictly ordered and non-overlapping"
                    )
                previous_end = end
                normalized_ranges.append(
                    MappingProxyType(
                        {
                            "start_session": start.isoformat(),
                            "end_session": end.isoformat(),
                        }
                    )
                )
            object.__setattr__(self, "missing_ranges", tuple(normalized_ranges))
        object.__setattr__(
            self,
            "rate_snapshot_hash",
            _optional_text(self.rate_snapshot_hash, "rate_snapshot_hash"),
        )
        if self.rate_snapshot_hash is not None:
            object.__setattr__(
                self,
                "rate_snapshot_hash",
                _require_sha256_signature(
                    self.rate_snapshot_hash, "rate_snapshot_hash"
                ),
            )
        object.__setattr__(
            self,
            "last_chunk_token",
            _optional_text(self.last_chunk_token, "last_chunk_token"),
        )
        checkpoint_fields = (
            self.last_chunk_sequence,
            self.last_chunk_token,
            self.completed_through_session,
        )
        checkpoint_present = tuple(value is not None for value in checkpoint_fields)
        if any(checkpoint_present) and not all(checkpoint_present):
            raise DomainValidationError(
                "chunk sequence, token, and completed session must be provided together"
            )
        if self.last_chunk_token is not None and (
            len(self.last_chunk_token) != 71
            or not self.last_chunk_token.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in self.last_chunk_token[7:]
            )
        ):
            raise DomainValidationError(
                "last_chunk_token must be sha256:<64 lowercase hex digits>"
            )
        object.__setattr__(
            self,
            "terminal_fingerprint",
            _optional_text(self.terminal_fingerprint, "terminal_fingerprint"),
        )
        if self.terminal_fingerprint is not None and (
            len(self.terminal_fingerprint) != 71
            or not self.terminal_fingerprint.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in self.terminal_fingerprint[7:]
            )
        ):
            raise DomainValidationError(
                "terminal_fingerprint must be sha256:<64 lowercase hex digits>"
            )
        if self.completed_through_session is not None:
            if isinstance(self.completed_through_session, datetime) or not isinstance(
                self.completed_through_session, date
            ):
                raise DomainValidationError(
                    "completed_through_session must be a calendar date"
                )
        if self.status in (
            AnalysisSummaryStatus.PARTIAL,
            AnalysisSummaryStatus.FINAL,
        ) and not all(checkpoint_present):
            raise DomainValidationError(
                f"{self.status.value} summaries require a complete successful checkpoint"
            )
        object.__setattr__(
            self, "abort_reason", _optional_text(self.abort_reason, "abort_reason")
        )
        for name in ("created_at", "updated_at", "finalized_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _aware_datetime(value, name)
                )
        aborted = self.status is AnalysisSummaryStatus.ABORTED
        if aborted != (self.abort_reason is not None):
            raise DomainValidationError(
                "abort_reason is required exactly when status is aborted"
            )
        if aborted or self.status is AnalysisSummaryStatus.FINAL:
            if self.finalized_at is None:
                raise DomainValidationError(
                    f"{self.status.value} summaries require finalized_at"
                )
            if self.terminal_fingerprint is None:
                raise DomainValidationError(
                    f"{self.status.value} summaries require the coordinator-issued "
                    "terminal_fingerprint"
                )
        elif self.finalized_at is not None:
            raise DomainValidationError(
                "partial summaries must not carry finalized_at"
            )
        elif self.terminal_fingerprint is not None:
            raise DomainValidationError(
                "partial summaries must not carry terminal_fingerprint"
            )


@dataclass(frozen=True, slots=True)
class BacktestDataPreflightRecord:
    """One run-level data preflight report (``backtest_data_preflight`` row)."""

    run_id: UUID
    phase: DataPhase
    status: str
    report_hash: str
    hash_schema_version: int = 1
    capabilities: Mapping[str, Any] | None = None
    calendar_summary: Mapping[str, Any] | None = None
    session_summary: Mapping[str, Any] | None = None
    pit_status: str | None = None
    coverage: Mapping[str, Any] | None = None
    source_revisions: Mapping[str, Any] | None = None
    # Phase 2a keeps these run-bound labels in the existing preflight JSON
    # projection.  They are DTO fields as well, so admission/session callers
    # cannot accidentally persist an unlabeled internal report.
    run_kind: str = "backtest_run"
    preflight_profile_key: str = "formal"
    preflight_profile_version: int = 1
    admission_report_hash: str | None = None
    session_report_hash: str | None = None
    hash_match: bool | None = None
    report_diff: Sequence[Mapping[str, Any]] | None = None
    failure_phase: str | None = None
    fixture_sources: Mapping[str, Any] | None = None
    scope_summary: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run_id"))
        object.__setattr__(self, "phase", _enum(self.phase, DataPhase, "phase"))
        object.__setattr__(self, "status", _required_text(self.status, "status"))
        object.__setattr__(
            self, "report_hash", _required_text(self.report_hash, "report_hash")
        )
        if isinstance(self.hash_schema_version, bool) or self.hash_schema_version not in (1, 2):
            raise DomainValidationError("hash_schema_version must be 1 or 2")
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
        run_kind = _required_text(self.run_kind, "run_kind")
        if run_kind not in {"backtest_run", "internal_link_acceptance"}:
            raise DomainValidationError("run_kind is not supported by the result contract")
        object.__setattr__(self, "run_kind", run_kind)
        profile_key = _required_text(
            self.preflight_profile_key, "preflight_profile_key"
        )
        if profile_key not in {"formal", "internal_link_acceptance"}:
            raise DomainValidationError("preflight_profile_key is not supported")
        object.__setattr__(self, "preflight_profile_key", profile_key)
        if (
            isinstance(self.preflight_profile_version, bool)
            or not isinstance(self.preflight_profile_version, int)
            or self.preflight_profile_version < 1
        ):
            raise DomainValidationError(
                "preflight_profile_version must be a positive integer"
            )
        if run_kind == "internal_link_acceptance" and (
            profile_key != "internal_link_acceptance"
            or self.preflight_profile_version != 1
        ):
            raise DomainValidationError(
                "internal_link_acceptance runs require profile internal_link_acceptance@1"
            )
        if run_kind == "backtest_run" and (
            profile_key != "formal" or self.preflight_profile_version != 1
        ):
            raise DomainValidationError(
                "formal runs require profile formal@1"
            )
        for name in ("admission_report_hash", "session_report_hash", "failure_phase"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if self.hash_match is not None and not isinstance(self.hash_match, bool):
            raise DomainValidationError("hash_match must be a boolean when provided")
        if self.report_diff is None:
            object.__setattr__(self, "report_diff", ())
        else:
            if isinstance(self.report_diff, (str, bytes)):
                raise DomainValidationError("report_diff must be a sequence of mappings")
            normalized_diff = tuple(
                _json_payload(item, "report_diff entry")
                for item in self.report_diff
            )
            object.__setattr__(self, "report_diff", normalized_diff)
        for name in ("fixture_sources", "scope_summary"):
            object.__setattr__(self, name, _json_payload(getattr(self, name), name))

        # The service stores only bounded evidence in these fields.  Reject
        # raw token/credential keys at the DTO boundary so a caller cannot
        # accidentally turn an audit JSON column into a secret sink.
        for name in (
            "capabilities",
            "calendar_summary",
            "session_summary",
            "coverage",
            "source_revisions",
            "fixture_sources",
            "scope_summary",
        ):
            _reject_preflight_sensitive_keys(getattr(self, name), name)

    @property
    def cursor_sort_key(self) -> tuple[str]:
        return (self.phase.value,)

    @property
    def preflight_profile(self) -> str:
        """Return the stable ``key@version`` profile reference."""

        return f"{self.preflight_profile_key}@{self.preflight_profile_version}"

    @property
    def preflight_metadata(self) -> Mapping[str, Any]:
        """Return the machine metadata used for visibility enforcement."""

        return MappingProxyType(
            {
                "run_kind": self.run_kind,
                "preflight_profile_key": self.preflight_profile_key,
                "preflight_profile_version": self.preflight_profile_version,
                "preflight_profile": self.preflight_profile,
                "qualification_hash": self.report_hash,
                "admission_report_hash": self.admission_report_hash,
                "session_report_hash": self.session_report_hash,
                "hash_match": self.hash_match,
                "report_diff": self.report_diff,
                "failure_phase": self.failure_phase,
                "fixture_sources": self.fixture_sources,
                "scope_summary": self.scope_summary,
            }
        )


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
    "AnalysisSummaryStatus",
    "AnalyzerState",
    "ChunkValidationStatus",
    "DataPhase",
    "DataQualityStatus",
    "DecisionValidationStatus",
    "BacktestDataChunkRecord",
    "BacktestDecisionRecord",
    "AnalysisSummaryStatus",
    "AnalyzerState",
    "BacktestAnalysisSummaryRecord",
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
