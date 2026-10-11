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

from app.backtest_service.config_values import ZERO, DomainValidationError, _decimal, _positive
from app.backtest_service.accounts.fee_types import FeeBaseMeasure, FeeChargeTiming, FeeError, FeeRoundingLevel, FeeRoundingMode, FeeRule, FeeRuleType, FeeRuleUnresolvedError, FeeSchedule, FeeScheduleSnapshot, FeeSide, _rule_digest_payload, _rule_side, _schedule_digest, _side






















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
