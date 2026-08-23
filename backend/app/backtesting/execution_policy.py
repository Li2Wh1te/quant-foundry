"""Execution-layer runtime projections of resolved instrument rules.

This module is a *read-only consumer* of the frozen instrument rule
snapshots (``app.instruments.rule_snapshots``).  It never re-parses live
fact tables and never invents production values: every trading-critical
field must be present in the resolved segment, otherwise the policy
object refuses to construct instead of falling back to a platform
default.

Three runtime objects are defined here:

- :class:`InstrumentExecutionPolicy` — the immutable per-instrument
  projection consumed by matching and accounting;
- :class:`SessionContext` — one trading session plus the per-instrument
  market-state facts required before an opening match may proceed;
- :class:`SettlementBoundary` — the externally resolved settlement
  lifecycle event that must be applied before the session's opening
  match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.backtesting.execution import MarketState
from app.instruments.references import VersionedReference
from app.instruments.rules.contracts import StrategyRuleDeclaration


class ExecutionPolicyError(DomainValidationError):
    """Raised when an execution policy or session context is incomplete."""


class SettlementBoundaryPhase(StrEnum):
    """Lifecycle phase of a settlement boundary event."""

    BEFORE_OPEN_MATCH = "before_open_match"


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    """Coerce a resolved rule value into a positive ``Decimal``.

    Accepts ``Decimal``, ``int``, and decimal strings (canonical JSON
    snapshot payloads store decimals as strings).  Floats and booleans
    are rejected: they cannot represent exact rule values.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise ExecutionPolicyError(
            f"{field_name} must be an exact decimal value; float is unsupported"
        )
    if isinstance(value, Decimal):
        normalized = value
    elif isinstance(value, (int, str)):
        try:
            normalized = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ExecutionPolicyError(
                f"{field_name} is not a parsable decimal"
            ) from exc
    else:
        raise ExecutionPolicyError(
            f"{field_name} must be Decimal, int, or str"
        )
    if not normalized.is_finite() or normalized <= 0:
        raise ExecutionPolicyError(f"{field_name} must be a positive decimal")
    return normalized


def _non_negative_int(value: Any, field_name: str) -> int:
    """Coerce a resolved precision value into a non-negative ``int``."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionPolicyError(
            f"{field_name} must be a non-negative integer"
        )
    return value


def _versioned_reference(value: Any, field_name: str) -> VersionedReference:
    """Coerce a resolved reference or its canonical ``{key, version}`` form."""

    if isinstance(value, VersionedReference):
        return value
    if isinstance(value, Mapping):
        try:
            return VersionedReference(key=value.get("key"), version=value.get("version"))
        except DomainValidationError as exc:
            raise ExecutionPolicyError(
                f"{field_name} must be a valid versioned reference"
            ) from exc
    raise ExecutionPolicyError(
        f"{field_name} must be a versioned reference or {{key, version}} mapping"
    )


def _strategy_rule(
    value: Any, field_name: str
) -> StrategyRuleDeclaration | VersionedReference:
    """Coerce a strategy-rule value (declaration or versioned reference)."""

    if isinstance(value, (StrategyRuleDeclaration, VersionedReference)):
        return value
    if isinstance(value, Mapping) and "statements" in value:
        try:
            return StrategyRuleDeclaration(statements=tuple(value["statements"]))
        except DomainValidationError as exc:
            raise ExecutionPolicyError(
                f"{field_name} carries an empty strategy declaration"
            ) from exc
    return _versioned_reference(value, field_name)


def _string_set(value: Any, field_name: str) -> frozenset[str]:
    """Coerce a resolved string set (tuple, list, or JSON array)."""

    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise ExecutionPolicyError(f"{field_name} must be a set of strings")
    members: list[str] = []
    for member in value:
        if not isinstance(member, str) or not member.strip():
            raise ExecutionPolicyError(
                f"{field_name} members must be non-blank strings"
            )
        members.append(member.strip())
    if not members:
        raise ExecutionPolicyError(f"{field_name} must not be empty")
    return frozenset(members)


def _currency_code(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionPolicyError(f"{field_name} must be non-blank text")
    return value.strip().upper()


def _required_text(value: Any, field_name: str) -> str:
    """Require a non-blank string without changing its case."""

    if not isinstance(value, str) or not value.strip():
        raise ExecutionPolicyError(f"{field_name} must be non-blank text")
    return value.strip()


@dataclass(frozen=True, slots=True)
class InstrumentExecutionPolicy:
    """Immutable execution-layer view of one instrument's resolved rules.

    The object is a projection of a frozen rule snapshot segment, not a
    new source of truth: it carries the package reference and the
    segment ``resolution_hash`` so every match decision can be traced
    back to the exact resolved facts.  A policy cannot be constructed
    with any trading-critical field missing — there is no half-complete
    policy and no default-value fallback.
    """

    instrument_id: UUID
    package_reference: VersionedReference
    resolution_hash: str
    currency: str
    price_precision: int
    quantity_precision: int
    price_tick: Decimal
    lot_size: Decimal
    minimum_order_quantity: Decimal
    contract_multiplier: Decimal
    session_template_reference: VersionedReference
    allowed_order_types: frozenset[str]
    fee_categories: frozenset[str]
    settlement_rule_class: str
    sellable_rule: StrategyRuleDeclaration | VersionedReference
    cash_availability_rule: StrategyRuleDeclaration | VersionedReference
    position_availability_rule: StrategyRuleDeclaration | VersionedReference
    price_limit_rule: StrategyRuleDeclaration | VersionedReference

    @classmethod
    def from_rule_snapshot(
        cls,
        segment: Any,
        *,
        package_reference: VersionedReference,
    ) -> "InstrumentExecutionPolicy":
        """Project one frozen instrument rule snapshot segment.

        ``segment`` is an :class:`InstrumentRuleSnapshotSegment`; it is
        accepted untyped here to keep this module import-light, and its
        ``normalized_values`` may be either domain-normalized values or
        the canonical JSON form persisted with the snapshot.
        """

        values = getattr(segment, "normalized_values", None)
        if not isinstance(values, Mapping):
            raise ExecutionPolicyError(
                "rule snapshot segment must carry normalized_values mapping"
            )
        resolution_hash = getattr(segment, "resolution_hash", None)
        if not isinstance(resolution_hash, str) or not resolution_hash.strip():
            raise ExecutionPolicyError(
                "rule snapshot segment must carry a resolution_hash"
            )
        instrument_id = getattr(segment, "instrument_id", None)
        if not isinstance(instrument_id, UUID):
            raise ExecutionPolicyError(
                "rule snapshot segment must carry an instrument_id"
            )
        return cls(
            instrument_id=instrument_id,
            package_reference=package_reference,
            resolution_hash=resolution_hash,
            currency=_currency_code(values.get("currency"), "currency"),
            price_precision=_non_negative_int(
                values.get("price_precision"), "price_precision"
            ),
            quantity_precision=_non_negative_int(
                values.get("quantity_precision"), "quantity_precision"
            ),
            price_tick=_positive_decimal(values.get("price_tick"), "price_tick"),
            lot_size=_positive_decimal(values.get("lot_size"), "lot_size"),
            minimum_order_quantity=_positive_decimal(
                values.get("minimum_order_quantity"), "minimum_order_quantity"
            ),
            contract_multiplier=_positive_decimal(
                values.get("contract_multiplier"), "contract_multiplier"
            ),
            session_template_reference=_versioned_reference(
                values.get("trading_session_template"),
                "trading_session_template",
            ),
            allowed_order_types=_string_set(
                values.get("order_types"), "order_types"
            ),
            fee_categories=_string_set(
                values.get("fee_categories"), "fee_categories"
            ),
            settlement_rule_class=_required_text(
                values.get("settlement_rule_class"), "settlement_rule_class"
            ),
            sellable_rule=_strategy_rule(values.get("sellable_rule"), "sellable_rule"),
            cash_availability_rule=_strategy_rule(
                values.get("cash_availability_rule"), "cash_availability_rule"
            ),
            position_availability_rule=_strategy_rule(
                values.get("position_availability_rule"),
                "position_availability_rule",
            ),
            price_limit_rule=_strategy_rule(
                values.get("price_limit_rule"), "price_limit_rule"
            ),
        )

    def validate_order_type(self, order_type: Any) -> str | None:
        """Return a stable rejection code when the type is not declared.

        The first slice implements ``market`` only; a declared-but-
        unimplemented type is still rejected by the matching engine, so
        this check only verifies the declaration itself.
        """

        normalized = getattr(order_type, "value", order_type)
        if not isinstance(normalized, str) or normalized not in self.allowed_order_types:
            return "ORDER_TYPE_NOT_SUPPORTED"
        return None


@dataclass(frozen=True, slots=True)
class SessionContext:
    """One trading session and the facts required for its opening match.

    ``market_states`` must carry an explicit :class:`MarketState` for
    every instrument the caller intends to match.  A missing state, a
    state whose timestamp disagrees with ``opening_match_at``, or a
    state without the mandatory availability facts blocks the session:
    matching never proceeds on defaults.
    """

    session_id: UUID
    calendar_id: str
    session_date: date
    opening_match_at: datetime
    close_at: datetime
    exchange_open: bool
    market_states: Mapping[UUID, MarketState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, UUID):
            raise ExecutionPolicyError("session_id must be a UUID")
        if not isinstance(self.calendar_id, str) or not self.calendar_id.strip():
            raise ExecutionPolicyError("calendar_id must be non-blank text")
        if not isinstance(self.session_date, date) or isinstance(
            self.session_date, datetime
        ):
            raise ExecutionPolicyError("session_date must be a calendar date")
        _aware_datetime(self.opening_match_at, "opening_match_at")
        _aware_datetime(self.close_at, "close_at")
        if self.close_at <= self.opening_match_at:
            raise ExecutionPolicyError(
                "close_at must be later than opening_match_at"
            )
        if not isinstance(self.exchange_open, bool):
            raise ExecutionPolicyError("exchange_open must be a boolean")
        if not isinstance(self.market_states, Mapping):
            raise ExecutionPolicyError("market_states must be a mapping")
        frozen_states: dict[UUID, MarketState] = {}
        for instrument_id, state in self.market_states.items():
            if not isinstance(instrument_id, UUID):
                raise ExecutionPolicyError(
                    "market_states keys must be instrument UUIDs"
                )
            if not isinstance(state, MarketState):
                raise ExecutionPolicyError(
                    "market_states values must be MarketState instances"
                )
            if state.instrument_id != instrument_id:
                raise ExecutionPolicyError(
                    "market_states key must match state.instrument_id"
                )
            if state.timestamp != self.opening_match_at:
                raise ExecutionPolicyError(
                    f"market state for instrument {instrument_id} has "
                    f"timestamp {state.timestamp.isoformat()} which does "
                    "not match the session opening_match_at "
                    f"{self.opening_match_at.isoformat()}"
                )
            frozen_states[instrument_id] = state
        object.__setattr__(
            self,
            "market_states",
            MappingProxyType(frozen_states),
        )

    def require_market_state(self, instrument_id: UUID) -> MarketState:
        """Return the instrument state or fail the session explicitly."""

        state = self.market_states.get(instrument_id)
        if state is None:
            raise ExecutionPolicyError(
                f"session {self.session_id} has no market state for "
                f"instrument {instrument_id}; matching cannot proceed "
                "with defaults"
            )
        return state


@dataclass(frozen=True, slots=True)
class SettlementBoundary:
    """An externally resolved settlement boundary for one session.

    The boundary is produced by the calendar/settlement resolver; this
    package only consumes it.  ``phase`` is fixed to
    ``before_open_match`` because the first formal settlement category
    releases due quantities immediately before each session's opening
    match.
    """

    boundary_id: UUID
    session_id: UUID
    calendar_id: str
    session_date: date
    phase: SettlementBoundaryPhase | str = (
        SettlementBoundaryPhase.BEFORE_OPEN_MATCH
    )

    def __post_init__(self) -> None:
        if not isinstance(self.boundary_id, UUID):
            raise ExecutionPolicyError("boundary_id must be a UUID")
        if not isinstance(self.session_id, UUID):
            raise ExecutionPolicyError("session_id must be a UUID")
        if not isinstance(self.calendar_id, str) or not self.calendar_id.strip():
            raise ExecutionPolicyError("calendar_id must be non-blank text")
        if not isinstance(self.session_date, date) or isinstance(
            self.session_date, datetime
        ):
            raise ExecutionPolicyError("session_date must be a calendar date")
        try:
            phase = SettlementBoundaryPhase(self.phase)
        except ValueError as exc:
            raise ExecutionPolicyError(
                "phase must be before_open_match"
            ) from exc
        object.__setattr__(self, "phase", phase)


__all__ = [
    "ExecutionPolicyError",
    "InstrumentExecutionPolicy",
    "SessionContext",
    "SettlementBoundary",
    "SettlementBoundaryPhase",
]
