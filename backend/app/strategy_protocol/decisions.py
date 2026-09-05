"""Immutable strategy decisions and the extensible decision-mode registry.

The public :class:`StrategyDecision` value object and the
:class:`DecisionModeRegistry` are intentionally not hard-coded to the two
first-version modes.  Future modes such as ``target_positions`` or
``order_intents`` can be registered without changing this module; only the
registration site decides which modes exist today.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Callable
from uuid import UUID, uuid4

from app.backtesting.domain import DomainValidationError

from .contract import (
    STRATEGY_CONTRACT_VERSION,
    InvalidDecisionPayloadError,
    MissingDecisionModeError,
    UnknownDecisionModeError,
    UnknownInstrumentError,
)

TARGET_WEIGHTS_MODE = "target_weights"
HOLD_MODE = "hold"


def _require_aware_datetime(value: datetime, field_name: str) -> datetime:
    """Reuse the engine-wide timezone-awareness rule for decision timestamps."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidDecisionPayloadError(
            f"{field_name} must be a timezone-aware datetime"
        )
    return value


def _decimal_target(value: object, key: str) -> Decimal:
    """Convert one weight to ``Decimal``, rejecting float/bool/invalid input.

    Weights cross the process boundary as decimal strings.  ``float`` and
    ``bool`` are rejected outright because binary floats cannot represent the
    weights exactly and would silently corrupt portfolio arithmetic.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise InvalidDecisionPayloadError(
            f"targets[{key!r}] must be a decimal string or Decimal; "
            "float values are unsupported"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidDecisionPayloadError(
                f"targets[{key!r}] must be finite"
            )
        return value
    if not isinstance(value, (str, int)):
        raise InvalidDecisionPayloadError(
            f"targets[{key!r}] must be a decimal string or Decimal"
        )
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidDecisionPayloadError(
            f"targets[{key!r}] is not a valid decimal value"
        ) from exc
    if not normalized.is_finite():
        raise InvalidDecisionPayloadError(f"targets[{key!r}] must be finite")
    return normalized


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """Immutable outcome of one strategy decision step.

    The decision only expresses intent.  Orders, fills, fees, slippage, and
    account updates are produced by later engine stages, never by this object.
    """

    step_sequence: int
    decision_time: datetime
    mode: str
    targets: Mapping[str, Decimal] = field(default_factory=dict)
    reason: str | None = None
    decision_id: UUID = field(default_factory=uuid4)
    contract_version: int = STRATEGY_CONTRACT_VERSION
    # These fields are populated by the runtime audit boundary.  Keeping them
    # on the immutable decision value lets failed strategy calls use the same
    # persistence path as successful decisions without inventing a second
    # decision object hierarchy.
    validation_status: str = "accepted"
    validation_issues: tuple[str | Mapping[str, object], ...] = ()
    duration_ms: Decimal | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.contract_version != STRATEGY_CONTRACT_VERSION:
            raise InvalidDecisionPayloadError(
                "contract_version must equal the current official protocol version"
            )
        if not isinstance(self.step_sequence, int) or isinstance(
            self.step_sequence, bool
        ):
            raise InvalidDecisionPayloadError("step_sequence must be an integer")
        _require_aware_datetime(self.decision_time, "decision_time")
        if not isinstance(self.mode, str) or not self.mode:
            raise InvalidDecisionPayloadError("mode must be a non-empty string")
        normalized_targets = {
            str(key): _decimal_target(value, str(key))
            for key, value in dict(self.targets).items()
        }
        object.__setattr__(self, "targets", MappingProxyType(normalized_targets))


TargetWeightsValidator = Callable[[Mapping[str, object], set[UUID]], dict[str, Decimal]]


def validate_target_weights_payload(
    targets: Mapping[str, object], known_instrument_ids: set[UUID]
) -> dict[str, Decimal]:
    """Validate a full target-weights mapping against the run's identities.

    Existing positions that are absent from the mapping are interpreted as a
    zero target by the interpreter stage; the payload itself does not need to
    list them.  No weight-sum rule is enforced here: such a rule is a domain
    contract that has not been approved yet.
    """

    if not isinstance(targets, Mapping):
        raise InvalidDecisionPayloadError("targets must be an object")
    normalized: dict[str, Decimal] = {}
    for key, value in targets.items():
        if not isinstance(key, str):
            raise InvalidDecisionPayloadError("targets keys must be instrument_id strings")
        try:
            instrument_id = UUID(key)
        except ValueError as exc:
            raise UnknownInstrumentError(
                f"targets key {key!r} is not a valid instrument_id"
            ) from exc
        if instrument_id not in known_instrument_ids:
            raise UnknownInstrumentError(
                f"targets reference unknown instrument_id {key!r}"
            )
        normalized[key] = _decimal_target(value, key)
    return normalized


def validate_hold_payload(
    targets: object, known_instrument_ids: set[UUID]
) -> dict[str, Decimal]:
    """Require that a hold decision carries no non-empty target mapping."""

    if targets is None:
        return {}
    if isinstance(targets, Mapping) and not targets:
        return {}
    raise InvalidDecisionPayloadError('mode "hold" must not carry targets')


class DecisionModeRegistry:
    """Registry of supported decision modes and their payload validators.

    Keeping validators in a registry means adding ``target_positions`` or
    ``order_intents`` later is a registration change, not an engine rewrite.
    """

    def __init__(self) -> None:
        self._validators: dict[str, TargetWeightsValidator] = {}

    def register(self, mode: str, validator: TargetWeightsValidator) -> None:
        """Register one decision mode with its payload validator."""

        if not isinstance(mode, str) or not mode:
            raise ValueError("decision mode must be a non-empty string")
        if mode in self._validators:
            raise ValueError(f"decision mode {mode!r} is already registered")
        self._validators[mode] = validator

    def modes(self) -> tuple[str, ...]:
        """Return the currently registered mode names."""

        return tuple(sorted(self._validators))

    def is_registered(self, mode: str) -> bool:
        """Whether the mode can be used by a decision right now."""

        return mode in self._validators

    def validate(
        self,
        payload: Mapping[str, object],
        *,
        known_instrument_ids: set[UUID],
    ) -> tuple[str, dict[str, Decimal]]:
        """Validate a raw decision payload and return ``(mode, targets)``.

        Raises the protocol errors from :mod:`contract` for every rejected
        shape so callers can surface one consistent failure taxonomy.
        """

        if not isinstance(payload, Mapping):
            raise InvalidDecisionPayloadError(
                "decision must be an object; floats and other scalars are unsupported"
            )
        mode = payload.get("mode")
        if mode is None:
            raise MissingDecisionModeError('decision requires a "mode" field')
        if not isinstance(mode, str):
            raise InvalidDecisionPayloadError('"mode" must be a string')
        if mode not in self._validators:
            raise UnknownDecisionModeError(
                f'unknown decision mode {mode!r}; registered modes: '
                f'{", ".join(self.modes())}'
            )
        # Only the fields each mode understands are forwarded. Extra fields are
        # ignored rather than silently treated as trading instructions.
        targets = payload.get("targets")
        validated = self._validators[mode](targets, known_instrument_ids)
        return mode, validated


def build_default_registry() -> DecisionModeRegistry:
    """Create the first-version registry with exactly two registered modes."""

    registry = DecisionModeRegistry()
    registry.register(TARGET_WEIGHTS_MODE, validate_target_weights_payload)
    registry.register(HOLD_MODE, validate_hold_payload)
    return registry


__all__ = [
    "HOLD_MODE",
    "TARGET_WEIGHTS_MODE",
    "DecisionModeRegistry",
    "StrategyDecision",
    "build_default_registry",
    "validate_hold_payload",
    "validate_target_weights_payload",
    # Re-exported so adapter code can catch one domain error type.
    "DomainValidationError",
]
