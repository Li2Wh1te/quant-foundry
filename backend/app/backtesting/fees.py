"""Fee configuration and deterministic fee calculations.

Fee schedules are configuration objects selected through an explicit account
profile.  A run freezes a complete schedule snapshot before execution, so
later edits to the mutable configuration cannot change a historical run.
Each rule carries its own rounding contract; the calculator never invents a
global rounding policy or consults a platform-wide default.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from app.backtesting.domain import ZERO, DomainValidationError, _decimal, _positive


class FeeError(DomainValidationError):
    """Raised when a fee schedule or fee calculation is invalid."""


class FeeRoundingLevel(StrEnum):
    """Supported calculation scopes for the first fee calculator."""

    FEE_ITEM = "fee_item"
    FILL = "fill"
    ORDER = "order"


class FeeRoundingMode(StrEnum):
    """Decimal rounding modes exposed by a fee rule."""

    UP = "up"
    DOWN = "down"
    HALF_UP = "half_up"


def _side(value: object) -> str:
    """Normalize an order-side value without coupling fee rules to orders."""

    normalized = getattr(value, "value", value)
    if not isinstance(normalized, str) or normalized not in {"buy", "sell"}:
        raise FeeError("side must be buy or sell")
    return normalized


def _round(value: Decimal, mode: FeeRoundingMode, precision: Decimal) -> Decimal:
    """Round a monetary amount to a rule-owned minimum unit."""

    quotient = value / precision
    if mode is FeeRoundingMode.UP:
        rounding = ROUND_CEILING
    elif mode is FeeRoundingMode.DOWN:
        rounding = ROUND_FLOOR
    else:
        rounding = ROUND_HALF_UP
    return quotient.to_integral_value(rounding=rounding) * precision


@dataclass(frozen=True, slots=True)
class FeeRule:
    """One fee component and its optional applicability/rounding contract.

    A draft configuration may be incomplete while an operator is editing it.
    ``validate_for_run`` performs the stricter admission check required before
    a formal backtest starts.  Keeping those two phases separate lets the
    configuration UI save an invalid draft without allowing it to leak into a
    run.
    """

    key: str
    category: str
    side: str | None = None
    rate: Decimal | int | str = ZERO
    minimum: Decimal | int | str = ZERO
    fixed_amount: Decimal | int | str = ZERO
    rounding_level: FeeRoundingLevel | str | None = None
    rounding_scope: str | None = None
    rounding_mode: FeeRoundingMode | str | None = None
    rounding_precision: Decimal | int | str | None = None
    applicability: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise FeeError("fee rule key must be non-blank text")
        if not isinstance(self.category, str) or not self.category.strip():
            raise FeeError("fee rule category must be non-blank text")
        if self.side is not None:
            object.__setattr__(self, "side", _side(self.side))
        rate = _decimal(self.rate, f"fee_rules[{self.key}].rate")
        minimum = _decimal(self.minimum, f"fee_rules[{self.key}].minimum")
        fixed = _decimal(self.fixed_amount, f"fee_rules[{self.key}].fixed_amount")
        if rate < ZERO or minimum < ZERO or fixed < ZERO:
            raise FeeError("fee rule rate, minimum, and fixed_amount must be non-negative")
        object.__setattr__(self, "key", self.key.strip())
        object.__setattr__(self, "category", self.category.strip())
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "fixed_amount", fixed)
        if self.rounding_level is not None:
            try:
                object.__setattr__(
                    self,
                    "rounding_level",
                    FeeRoundingLevel(self.rounding_level),
                )
            except ValueError as exc:
                raise FeeError("fee rule rounding level is unsupported") from exc
        if self.rounding_mode is not None:
            try:
                object.__setattr__(
                    self,
                    "rounding_mode",
                    FeeRoundingMode(self.rounding_mode),
                )
            except ValueError as exc:
                raise FeeError("fee rule rounding mode is unsupported") from exc
        if self.rounding_scope is not None:
            if not isinstance(self.rounding_scope, str) or not self.rounding_scope.strip():
                raise FeeError("fee rule rounding_scope must be non-blank text")
            object.__setattr__(self, "rounding_scope", self.rounding_scope.strip())
        if self.rounding_precision is not None:
            object.__setattr__(
                self,
                "rounding_precision",
                _positive(
                    self.rounding_precision,
                    f"fee_rules[{self.key}].rounding_precision",
                ),
            )
        normalized_applicability: dict[str, str] = {}
        for field_name, value in self.applicability.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise FeeError("fee rule applicability keys must be non-blank text")
            if not isinstance(value, str) or not value.strip():
                raise FeeError("fee rule applicability values must be non-blank text")
            normalized_applicability[field_name.strip()] = value.strip()
        object.__setattr__(
            self,
            "applicability",
            MappingProxyType(normalized_applicability),
        )

    def applies_to(self, side: object) -> bool:
        """Return whether this rule applies to a buy or sell fill."""

        normalized_side = _side(side)
        return self.side is None or self.side == normalized_side

    def applies_to_context(
        self,
        *,
        side: object | None = None,
        context: Mapping[str, str] | None = None,
    ) -> bool:
        """Match order side and optional asset/market applicability facts."""

        if side is not None and not self.applies_to(side):
            return False
        if context is None:
            return True
        return all(context.get(key) == value for key, value in self.applicability.items())

    def validate_for_run(self) -> None:
        """Require every rounding field needed by a formal run."""

        if self.rounding_level is None:
            raise FeeError(f"fee rule {self.key!r} is missing rounding_level")
        if self.rounding_scope is None:
            raise FeeError(f"fee rule {self.key!r} is missing rounding_scope")
        if self.rounding_mode is None:
            raise FeeError(f"fee rule {self.key!r} is missing rounding_mode")
        if self.rounding_precision is None:
            raise FeeError(f"fee rule {self.key!r} is missing rounding_precision")


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Fee rules selected by an account profile.

    ``version`` is an optional compatibility/audit value for the existing
    fill-calculation boundary.  It is not an account revision, is not
    incremented by profile updates, and is not used to resolve a run.  The
    immutable run boundary is ``FeeScheduleSnapshot`` in ``account_profiles``.
    """

    key: str
    fee_rules: tuple[FeeRule, ...]
    version: int | None = None
    metadata: Mapping[str, str] = MappingProxyType({})
    test_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise FeeError("fee schedule key must be non-blank text")
        normalized_key = self.key.strip()
        if normalized_key == "zero_cost" and not self.test_only:
            raise FeeError("zero_cost is reserved for test-only fee schedules")
        if self.version is not None and self.version <= 0:
            raise FeeError("fee schedule version must be positive")
        rules = tuple(self.fee_rules)
        keys = [rule.key for rule in rules]
        if len(keys) != len(set(keys)):
            raise FeeError("fee schedule fee rule keys must be unique")
        object.__setattr__(self, "key", normalized_key)
        object.__setattr__(self, "fee_rules", rules)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def zero_cost(cls) -> "FeeSchedule":
        """Return the explicit test-only zero-cost fixture."""

        return cls(key="zero_cost", fee_rules=(), test_only=True)

    def validate_for_run(self) -> None:
        """Validate that this schedule is eligible for a formal backtest."""

        if self.test_only:
            raise FeeError("test-only fee schedules cannot be used for formal runs")
        for rule in self.fee_rules:
            rule.validate_for_run()

    def snapshot(self) -> "FeeScheduleSnapshot":
        """Return a detached immutable copy for a newly created run."""

        return FeeScheduleSnapshot(
            key=self.key,
            fee_rules=tuple(self.fee_rules),
            metadata=self.metadata,
        )


@dataclass(frozen=True, slots=True)
class FeeScheduleSnapshot:
    """Complete immutable fee configuration captured by one run."""

    key: str
    fee_rules: tuple[FeeRule, ...]
    metadata: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise FeeError("fee schedule snapshot key must be non-blank text")
        rules = tuple(self.fee_rules)
        keys = [rule.key for rule in rules]
        if len(keys) != len(set(keys)):
            raise FeeError("fee schedule snapshot fee rule keys must be unique")
        object.__setattr__(self, "key", self.key.strip())
        object.__setattr__(self, "fee_rules", rules)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def validate_for_run(self) -> None:
        """Apply the same formal-run admission checks to a frozen snapshot."""

        for rule in self.fee_rules:
            rule.validate_for_run()


@dataclass(frozen=True, slots=True)
class FeeComponent:
    """One calculated fee component with its raw and rounded amount."""

    rule_key: str
    category: str
    base_amount: Decimal
    raw_amount: Decimal
    amount: Decimal
    rounding_level: FeeRoundingLevel
    rounding_scope: str
    rounding_mode: FeeRoundingMode
    rounding_precision: Decimal


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """Immutable fee result attached to a fill."""

    schedule_key: str
    schedule_version: int | None
    currency: str
    components: tuple[FeeComponent, ...]

    @property
    def total(self) -> Decimal:
        """Return the sum of all rounded fee components."""

        return sum((component.amount for component in self.components), ZERO)


@dataclass(frozen=True, slots=True)
class FeeCalculator:
    """Calculate fees from a frozen schedule for one fill."""

    schedule: FeeSchedule | FeeScheduleSnapshot

    def calculate(
        self,
        *,
        side: object,
        notional: Decimal | int | str,
        currency: str = "CNY",
    ) -> FeeBreakdown:
        """Calculate applicable components without mutating the schedule."""

        self.schedule.validate_for_run()
        normalized_side = _side(side)
        normalized_notional = _decimal(notional, "notional")
        if normalized_notional < ZERO:
            raise FeeError("notional must be non-negative")
        base = normalized_notional
        if not isinstance(currency, str) or not currency.strip():
            raise FeeError("currency must be non-blank text")
        raw_components: list[tuple[FeeRule, Decimal, Decimal]] = []
        for rule in self.schedule.fee_rules:
            if not rule.applies_to(normalized_side):
                continue
            raw_amount = max(base * rule.rate, rule.minimum) + rule.fixed_amount
            raw_components.append((rule, base, raw_amount))

        components: list[FeeComponent] = []
        fill_groups: dict[tuple[FeeRoundingLevel, str, FeeRoundingMode, Decimal], list[int]] = {}
        for rule, component_base, raw_amount in raw_components:
            # A first-version market order produces one fill, so an
            # order-level rule has the same aggregate as this calculation.
            # The level is preserved in the component for a future multi-fill
            # order aggregator; it is not silently rewritten to ``FILL``.
            amount = _round(raw_amount, rule.rounding_mode, rule.rounding_precision)
            index = len(components)
            components.append(
                FeeComponent(
                    rule_key=rule.key,
                    category=rule.category,
                    base_amount=component_base,
                    raw_amount=raw_amount,
                    amount=amount,
                    rounding_level=rule.rounding_level,
                    rounding_scope=rule.rounding_scope,
                    rounding_mode=rule.rounding_mode,
                    rounding_precision=rule.rounding_precision,
                )
            )
            if rule.rounding_level is FeeRoundingLevel.FILL:
                group_key = (
                    rule.rounding_level,
                    rule.rounding_scope,
                    rule.rounding_mode,
                    rule.rounding_precision,
                )
                fill_groups.setdefault(group_key, []).append(index)

        # A fill-level rule rounds the aggregate for that declared scope.  The
        # rounding difference is assigned to the final component in the group
        # so the persisted component amounts still add up exactly to the total.
        for group_key, indexes in fill_groups.items():
            raw_total = sum((components[index].raw_amount for index in indexes), ZERO)
            level, scope, mode, precision = group_key
            rounded_total = _round(raw_total, mode, precision)
            current_total = sum((components[index].amount for index in indexes), ZERO)
            difference = rounded_total - current_total
            if difference:
                index = indexes[-1]
                component = components[index]
                components[index] = FeeComponent(
                    rule_key=component.rule_key,
                    category=component.category,
                    base_amount=component.base_amount,
                    raw_amount=component.raw_amount,
                    amount=component.amount + difference,
                    rounding_level=level,
                    rounding_scope=scope,
                    rounding_mode=mode,
                    rounding_precision=precision,
                )

        return FeeBreakdown(
            schedule_key=self.schedule.key,
            schedule_version=getattr(self.schedule, "version", None),
            currency=currency.strip().upper(),
            components=tuple(components),
        )
