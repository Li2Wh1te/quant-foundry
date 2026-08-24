"""Fee configuration and deterministic fee calculations.

Fee schedules are configuration objects selected through an explicit account
profile.  A run freezes a complete schedule snapshot before execution, so
later edits to the mutable configuration cannot change a historical run.
Each rule carries its own rounding contract; the calculator never invents a
global rounding policy or consults a platform-wide default.
"""

from __future__ import annotations

import hashlib
import json
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


class FeeBaseMeasure(StrEnum):
    """Declared calculation base of one fee rule.

    ``gross_notional`` uses ``execution_price × quantity ×
    contract_multiplier`` as computed by the caller; ``quantity`` uses
    the filled quantity itself; ``fixed`` charges only the declared
    fixed amount.
    """

    GROSS_NOTIONAL = "gross_notional"
    QUANTITY = "quantity"
    FIXED = "fixed"


class FeeRuleType(StrEnum):
    """Fee rule shapes admitted by the first formal slice.

    Only the simple ``max(rate_fee, minimum) + fixed_amount`` shape
    exists.  Rebates, tiered rates, fee caps, and waivers are structurally
    impossible here; declaring such a type is rejected with a stable
    error instead of being silently misread as a simple rule.
    """

    SIMPLE_RATE = "simple_rate"


class FeeChargeTiming(StrEnum):
    """When a fee component is settled.

    The first slice settles every fee atomically at fill time; async
    settlement is explicitly out of scope.
    """

    ON_FILL = "on_fill"


class FeeSide(StrEnum):
    """Direction a fee rule applies to."""

    BUY = "buy"
    SELL = "sell"
    BOTH = "both"


class FeeRuleUnresolvedError(FeeError):
    """A required fee category/direction has no rule in the snapshot.

    ``code`` is the stable machine reason surfaced to order lifecycle
    handling; the formal path must never fall back to a zero fee.
    """

    code = "fee_rule_unresolved"


def _side(value: object) -> str:
    """Normalize an order-side value without coupling fee rules to orders."""

    normalized = getattr(value, "value", value)
    if not isinstance(normalized, str) or normalized not in {"buy", "sell"}:
        raise FeeError("side must be buy or sell")
    return normalized


def _rule_side(value: str | None) -> str | None:
    """Normalize a rule-side declaration (``buy``/``sell``/``both``/``None``)."""

    if value is None:
        return None
    normalized = getattr(value, "value", value)
    if not isinstance(normalized, str) or normalized not in {"buy", "sell", "both"}:
        raise FeeError("rule side must be buy, sell, both, or None")
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
    base_measure: FeeBaseMeasure | str = FeeBaseMeasure.GROSS_NOTIONAL
    charge_timing: FeeChargeTiming | str = FeeChargeTiming.ON_FILL
    rule_type: FeeRuleType | str = FeeRuleType.SIMPLE_RATE
    currency: str | None = None
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
        object.__setattr__(self, "side", _rule_side(self.side))
        try:
            object.__setattr__(
                self, "base_measure", FeeBaseMeasure(self.base_measure)
            )
        except ValueError as exc:
            raise FeeError("fee rule base_measure is unsupported") from exc
        try:
            object.__setattr__(
                self, "charge_timing", FeeChargeTiming(self.charge_timing)
            )
        except ValueError as exc:
            raise FeeError(
                "only on_fill fee charge timing is supported in the first slice"
            ) from exc
        try:
            object.__setattr__(self, "rule_type", FeeRuleType(self.rule_type))
        except ValueError as exc:
            raise FeeError(
                f"fee rule type {self.rule_type!r} is unsupported: rebates, "
                "tiered rates, fee caps, and waivers are not supported in "
                "the first slice",
            ) from exc
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
        if self.currency is not None:
            if not isinstance(self.currency, str) or not self.currency.strip():
                raise FeeError(
                    f"fee rule {self.key!r} currency must be non-blank text"
                )
            object.__setattr__(
                self, "currency", self.currency.strip().upper()
            )
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
        return self.side is None or self.side in (normalized_side, FeeSide.BOTH)

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
        if (
            self.base_measure is FeeBaseMeasure.FIXED
            and (self.rate > ZERO or self.minimum > ZERO)
        ):
            # Fixed-amount rules ignore rate/minimum by declaration; a
            # configured nonzero value would silently never charge.
            raise FeeError(
                f"fee rule {self.key!r} declares a fixed base but also "
                "configures a rate or minimum; such components are "
                "ignored by declaration and the configuration is rejected"
            )

    def require_currency_compatible(self, currency: str) -> None:
        """Fail closed when the rule currency conflicts with the fill."""

        if self.currency is not None and self.currency != currency:
            raise FeeRuleUnresolvedError(
                f"fee rule {self.key!r} is declared in {self.currency} "
                f"but the fill currency is {currency}"
            )


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
            version=self.version,
        )


@dataclass(frozen=True, slots=True)
class FeeScheduleSnapshot:
    """Complete immutable fee configuration captured by one run.

    ``version`` carries the source schedule's audit version so restricted
    per-instrument selections still report where their rules came from in
    :attr:`FeeBreakdown.schedule_version`.
    """

    key: str
    fee_rules: tuple[FeeRule, ...]
    metadata: Mapping[str, str] = MappingProxyType({})
    version: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise FeeError("fee schedule snapshot key must be non-blank text")
        if self.version is not None and self.version <= 0:
            raise FeeError("fee schedule snapshot version must be positive")
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
        quantity: Decimal | int | str | None = None,
    ) -> FeeBreakdown:
        """Calculate applicable components without mutating the schedule.

        ``notional`` is the gross notional already computed by the caller
        as ``execution_price × quantity × contract_multiplier``.  Rules
        declaring a ``quantity`` base measure receive the filled
        quantity; a missing quantity for such a rule is a hard error, so
        the formal path can never silently charge a wrong base.
        """

        self.schedule.validate_for_run()
        normalized_side = _side(side)
        normalized_notional = _decimal(notional, "notional")
        if normalized_notional < ZERO:
            raise FeeError("notional must be non-negative")
        normalized_quantity: Decimal | None = None
        if quantity is not None:
            normalized_quantity = _decimal(quantity, "quantity")
            if normalized_quantity < ZERO:
                raise FeeError("quantity must be non-negative")
        normalized_currency = currency.strip().upper()
        if not isinstance(currency, str) or not currency.strip():
            raise FeeError("currency must be non-blank text")
        raw_components: list[tuple[FeeRule, Decimal, Decimal]] = []
        for rule in self.schedule.fee_rules:
            if not rule.applies_to(normalized_side):
                continue
            rule.require_currency_compatible(normalized_currency)
            if rule.base_measure is FeeBaseMeasure.GROSS_NOTIONAL:
                base = normalized_notional
                raw_amount = max(base * rule.rate, rule.minimum) + rule.fixed_amount
            elif rule.base_measure is FeeBaseMeasure.QUANTITY:
                if normalized_quantity is None:
                    raise FeeRuleUnresolvedError(
                        f"fee rule {rule.key!r} charges on quantity but the "
                        "calculation received no fill quantity"
                    )
                base = normalized_quantity
                raw_amount = max(base * rule.rate, rule.minimum) + rule.fixed_amount
            else:
                # Fixed-amount rules ignore rate/minimum by declaration.
                base = ZERO
                raw_amount = rule.fixed_amount
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


def resolve_instrument_fee_rules(
    schedule: FeeSchedule | FeeScheduleSnapshot,
    *,
    fee_categories: frozenset[str] | set[str],
    side: object,
    context: Mapping[str, str] | None = None,
) -> tuple[FeeRule, ...]:
    """Select schedule rules for one instrument's declared fee categories.

    The selection is fail-closed: every category declared by the
    instrument must resolve to at least one rule applicable to the
    requested side, otherwise :class:`FeeRuleUnresolvedError` is raised.
    Categories the instrument did not declare are never charged, so
    callers must calculate with the returned subset (see
    :func:`fee_snapshot_for_rules`) instead of the full schedule.
    """

    if not fee_categories:
        raise FeeRuleUnresolvedError(
            "instrument declares no fee categories; the formal path "
            "cannot fall back to zero fees"
        )
    normalized_side = _side(side)
    selected: list[FeeRule] = []
    for category in sorted(fee_categories):
        matches = [
            rule
            for rule in schedule.fee_rules
            if rule.category == category
            and rule.applies_to_context(
                side=normalized_side, context=context
            )
        ]
        if not matches:
            raise FeeRuleUnresolvedError(
                f"no fee rule resolves for required category {category!r} "
                f"on side {normalized_side} in schedule {schedule.key!r}"
            )
        selected.extend(matches)
    return tuple(selected)


def fee_snapshot_for_rules(
    schedule: FeeSchedule | FeeScheduleSnapshot,
    rules: tuple[FeeRule, ...],
) -> FeeScheduleSnapshot:
    """Build a same-identity snapshot restricted to the given rules.

    Keeping the original schedule ``key`` and ``version`` preserves the
    audit trail in :class:`FeeBreakdown` while ensuring undeclared
    categories can never be charged by a later calculation over the full
    schedule.
    """

    return FeeScheduleSnapshot(
        key=schedule.key,
        fee_rules=rules,
        metadata=dict(getattr(schedule, "metadata", {}) or {}),
        version=getattr(schedule, "version", None),
    )


def _rule_digest_payload(rule: FeeRule) -> dict[str, object]:
    """Canonical JSON-safe representation of one rule for content hashing."""

    return {
        "key": rule.key,
        "category": rule.category,
        "side": rule.side,
        "rate": format(rule.rate, "f"),
        "minimum": format(rule.minimum, "f"),
        "fixed_amount": format(rule.fixed_amount, "f"),
        "base_measure": FeeBaseMeasure(rule.base_measure).value,
        "charge_timing": FeeChargeTiming(rule.charge_timing).value,
        "rule_type": FeeRuleType(rule.rule_type).value,
        "currency": rule.currency,
        "rounding_level": (
            FeeRoundingLevel(rule.rounding_level).value
            if rule.rounding_level is not None
            else None
        ),
        "rounding_scope": rule.rounding_scope,
        "rounding_mode": (
            FeeRoundingMode(rule.rounding_mode).value
            if rule.rounding_mode is not None
            else None
        ),
        "rounding_precision": (
            format(rule.rounding_precision, "f")
            if rule.rounding_precision is not None
            else None
        ),
        "applicability": {
            str(k): str(v)
            for k, v in sorted(
                rule.applicability.items(), key=lambda item: str(item[0])
            )
        },
    }


def _schedule_digest(snapshot: FeeScheduleSnapshot) -> str:
    """Content digest used to detect conflicting duplicate registrations."""

    encoded = json.dumps(
        {
            "key": snapshot.key,
            "version": snapshot.version,
            "metadata": {
                str(k): str(v)
                for k, v in sorted(
                    snapshot.metadata.items(), key=lambda item: str(item[0])
                )
            },
            "fee_rules": [_rule_digest_payload(rule) for rule in snapshot.fee_rules],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FeeScheduleVersionRegistry:
    """Immutable ``(key, version)`` registry of fee-schedule snapshots.

    Fee schedules use immutable versions: a configuration change creates a
    new version and never overwrites a historical one.  Registering the
    same key and version twice is only accepted when the complete rule
    content is identical (an idempotent replay); any content difference
    raises instead of rewriting history.  Lookups return detached frozen
    snapshots, so later registry changes cannot leak into an already
    resolved account binding.
    """

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, int], FeeScheduleSnapshot] = {}
        self._digests: dict[tuple[str, int], str] = {}

    def register(
        self,
        schedule: FeeSchedule | FeeScheduleSnapshot,
        *,
        version: int,
    ) -> FeeScheduleSnapshot:
        """Register one immutable version and return its stored snapshot."""

        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
        ):
            raise FeeError("fee schedule version must be a positive integer")
        snapshot = schedule if isinstance(schedule, FeeScheduleSnapshot) else schedule.snapshot()
        snapshot.validate_for_run()
        if snapshot.version is None:
            # Stamp the registered version onto the frozen snapshot so
            # every audit consumer sees the same version identity.
            snapshot = FeeScheduleSnapshot(
                key=snapshot.key,
                fee_rules=snapshot.fee_rules,
                metadata=snapshot.metadata,
                version=version,
            )
        elif snapshot.version != version:
            raise FeeError(
                f"fee schedule {snapshot.key!r} carries version "
                f"{snapshot.version} but was registered as version "
                f"{version}"
            )
        digest = _schedule_digest(snapshot)
        key = (snapshot.key.strip(), version)
        existing = self._snapshots.get(key)
        if existing is not None:
            if self._digests[key] != digest:
                raise FeeError(
                    f"fee schedule {snapshot.key!r} version {version} is "
                    "already registered with different content; fee "
                    "versions are immutable"
                )
            return existing
        self._snapshots[key] = snapshot
        self._digests[key] = digest
        return snapshot

    def get(self, key: str, version: int) -> FeeScheduleSnapshot:
        """Return the frozen snapshot or fail without a fallback."""

        lookup = (key.strip(), version)
        try:
            return self._snapshots[lookup]
        except KeyError as exc:
            raise FeeError(
                f"fee schedule {key!r} version {version} is not registered"
            ) from exc
