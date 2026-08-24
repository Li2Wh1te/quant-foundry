"""Formal ``long_only_target_weights@1`` decision interpreter.

The interpreter converts one validated :class:`StrategyDecision` plus the
actual decision-time portfolio snapshot into order intents.  Its frozen
contract:

* only ``target_weights`` and ``hold`` decisions are accepted;
* ``targets`` is the complete target portfolio — every existing position
  omitted from the mapping is interpreted as a zero target;
* quantities are sized from the *unadjusted market close visible at the
  D-day decision time*.  The D+1 open and any adjusted-price series are
  unreachable here, so no future price can leak into sizing;
* ``notional = price × quantity × contract_multiplier``; ``lot_size``
  only constrains quantity legality, never notional;
* rejection is whole-decision: if any single instrument cannot form a
  legal target order, the entire decision is rejected and **no** order
  intent is produced (no partial acceptance);
* snapshot consistency states map one-to-one onto
  :class:`~app.backtesting.reason_codes.DecisionReasonCode` snapshot
  codes; when several problems exist the main code follows the fixed
  priority ``invalid > conflicted > stale > incomplete`` and every
  problem is kept in ``issues[]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.backtesting.accounting import OrderSide
from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    _aware_datetime,
    _decimal,
    _non_negative,
    _positive,
)
from app.backtesting.execution import OrderIntent
from app.backtesting.reason_codes import (
    DecisionReasonCode,
    InterpretationAuditCode,
    ResultStage,
    StructuredReason,
)

__all__ = [
    "CorporateActionCashStatus",
    "CorporateActionSnapshot",
    "DecisionInterpretationResult",
    "DecisionStatus",
    "InstrumentExecutionFacts",
    "InstrumentInterpretation",
    "INTERPRETER_KEY_LONG_ONLY_TARGET_WEIGHTS",
    "LongOnlyTargetWeightsInterpreter",
    "PortfolioDecisionSnapshot",
    "SellOddLotPolicy",
    "SnapshotConsistencyStatus",
    "WeightBoundaryStatus",
]


INTERPRETER_KEY_LONG_ONLY_TARGET_WEIGHTS = "long_only_target_weights"
INTERPRETER_VERSION = 1

#: Priority used to choose the main reason code when several snapshot
#: problems exist at once.  Earlier entries win.
_SNAPSHOT_PROBLEM_PRIORITY: tuple[str, ...] = (
    "invalid",
    "conflicted",
    "stale",
    "incomplete",
)

#: One-to-one mapping from a snapshot problem to its decision reason code.
_SNAPSHOT_PROBLEM_CODES: dict[str, DecisionReasonCode] = {
    "incomplete": DecisionReasonCode.DECISION_SNAPSHOT_INCOMPLETE,
    "stale": DecisionReasonCode.DECISION_SNAPSHOT_STALE,
    "conflicted": DecisionReasonCode.DECISION_SNAPSHOT_CONFLICTED,
    "invalid": DecisionReasonCode.DECISION_SNAPSHOT_INVALID,
}


class CorporateActionCashStatus(StrEnum):
    """Actual cash state of corporate actions at the decision instant.

    Only ``credited`` cash may enter D-day decision equity.  Any other
    state means the cash fact is not usable for sizing.
    """

    CREDITED = "credited"
    NOT_CREDITED = "not_credited"
    UNKNOWN = "unknown"


class SnapshotConsistencyStatus(StrEnum):
    """Consistency of the decision snapshot against the account truth."""

    CONSISTENT = "consistent"
    INCOMPLETE = "incomplete"
    STALE = "stale"
    CONFLICTED = "conflicted"
    INVALID = "invalid"


class SellOddLotPolicy(StrEnum):
    """Frozen sell odd-lot policy of one instrument."""

    STRICT_LOT = "strict_lot"
    ALLOW_ODD_LOT = "allow_odd_lot"
    ALLOW_FULL_LIQUIDATION_ODD_LOT = "allow_full_liquidation_odd_lot"


class DecisionStatus(StrEnum):
    """Terminal interpretation status of one decision."""

    ACCEPTED = "accepted"
    ACCEPTED_NOOP = "accepted_noop"
    REJECTED = "rejected"


class WeightBoundaryStatus(StrEnum):
    """Structured record of where the weight sum sits against one."""

    UNDER_INVESTED = "under_invested"
    FULLY_INVESTED = "fully_invested"
    OVER_WITHIN_TOLERANCE = "over_within_tolerance"
    EXCEEDED = "exceeded"


@dataclass(frozen=True, slots=True)
class CorporateActionSnapshot:
    """Actual corporate-action cash state at the decision instant."""

    cash_status: CorporateActionCashStatus | str

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "cash_status", CorporateActionCashStatus(self.cash_status)
            )
        except ValueError as exc:
            raise DomainValidationError(
                "corporate-action cash status must be credited, "
                "not_credited, or unknown"
            ) from exc


@dataclass(frozen=True, slots=True)
class PortfolioDecisionSnapshot:
    """The actual account state a D-day decision is sized against.

    ``positions`` maps instrument ids to current total quantities; the
    interpreter never consults settlement-limited availability because
    truncating sells by availability is explicitly not an interpreter
    responsibility.

    ``consistency_problems`` lists detected snapshot problems using the
    frozen labels ``incomplete``, ``stale``, ``conflicted``, and
    ``invalid``.  An empty tuple means ``consistent``.
    """

    decision_snapshot_at: datetime
    cash: Decimal | int | str
    equity: Decimal | int | str
    valuation_status: str
    corporate_action_snapshot: CorporateActionSnapshot
    positions: Mapping[UUID, Decimal | int | str] = field(
        default_factory=dict
    )
    consistency_problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _aware_datetime(
            self.decision_snapshot_at, "decision_snapshot_at"
        )
        object.__setattr__(self, "cash", _non_negative(self.cash, "cash"))
        object.__setattr__(
            self, "equity", _non_negative(self.equity, "equity")
        )
        if not isinstance(self.valuation_status, str) or (
            not self.valuation_status.strip()
        ):
            raise DomainValidationError(
                "valuation_status must be non-blank text"
            )
        if not isinstance(self.corporate_action_snapshot, CorporateActionSnapshot):
            raise DomainValidationError(
                "corporate_action_snapshot must be a CorporateActionSnapshot"
            )
        normalized_positions: dict[UUID, Decimal] = {}
        for instrument_id, quantity in dict(self.positions).items():
            if not isinstance(instrument_id, UUID):
                raise DomainValidationError(
                    "positions keys must be instrument UUIDs"
                )
            normalized_positions[instrument_id] = _non_negative(
                quantity, f"positions[{instrument_id}]"
            )
        object.__setattr__(
            self, "positions", MappingProxyType(normalized_positions)
        )
        problems = tuple(self.consistency_problems)
        for problem in problems:
            if problem not in _SNAPSHOT_PROBLEM_CODES:
                raise DomainValidationError(
                    f"unknown snapshot consistency problem {problem!r}"
                )
        object.__setattr__(self, "consistency_problems", problems)

    @property
    def consistency_status(self) -> SnapshotConsistencyStatus:
        """Resolve the main consistency state under the frozen priority."""

        if not self.consistency_problems:
            return SnapshotConsistencyStatus.CONSISTENT
        for candidate in _SNAPSHOT_PROBLEM_PRIORITY:
            if candidate in self.consistency_problems:
                return SnapshotConsistencyStatus(candidate)
        # Unreachable: __post_init__ validates every label.
        return SnapshotConsistencyStatus.INVALID


@dataclass(frozen=True, slots=True)
class InstrumentExecutionFacts:
    """Per-instrument execution rules consumed by interpretation.

    Quantity precision, order precision, lot size, the minimum order
    quantity, and the sell odd-lot policy are independent declarations;
    none of them implies another.  Exemption flags missing at
    construction default to ``false`` per the frozen contract.
    """

    instrument_id: UUID
    holding_precision: int
    order_precision: int
    lot_size: Decimal | int | str
    minimum_order_quantity: Decimal | int | str
    sell_odd_lot_policy: SellOddLotPolicy | str
    contract_multiplier: Decimal | int | str
    odd_lot_bypasses_lot_size: bool = False
    full_liquidation_bypasses_lot_size: bool = False
    full_liquidation_bypasses_order_precision: bool = False
    # Fee-resolution context consumed by the stateless FeeQuote provider:
    # every declared category must resolve to an applicable rule or cost
    # quoting fails closed.
    fee_categories: frozenset[str] = frozenset()
    fee_applicability_context: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        for name in ("holding_precision", "order_precision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DomainValidationError(f"{name} must be a non-negative integer")
        try:
            object.__setattr__(
                self, "sell_odd_lot_policy", SellOddLotPolicy(self.sell_odd_lot_policy)
            )
        except ValueError as exc:
            raise DomainValidationError(
                "sell_odd_lot_policy must be strict_lot, allow_odd_lot, or "
                "allow_full_liquidation_odd_lot"
            ) from exc
        object.__setattr__(self, "lot_size", _positive(self.lot_size, "lot_size"))
        object.__setattr__(
            self,
            "minimum_order_quantity",
            _positive(self.minimum_order_quantity, "minimum_order_quantity"),
        )
        object.__setattr__(
            self,
            "contract_multiplier",
            _positive(self.contract_multiplier, "contract_multiplier"),
        )
        categories = frozenset(
            category.strip() for category in self.fee_categories
        )
        if "" in categories:
            raise DomainValidationError(
                "fee_categories entries must be non-blank text"
            )
        object.__setattr__(self, "fee_categories", categories)
        context = {
            str(key).strip(): str(value).strip()
            for key, value in dict(self.fee_applicability_context).items()
        }
        object.__setattr__(
            self, "fee_applicability_context", MappingProxyType(context)
        )
        for name in (
            "odd_lot_bypasses_lot_size",
            "full_liquidation_bypasses_lot_size",
            "full_liquidation_bypasses_order_precision",
        ):
            if not isinstance(getattr(self, name), bool):
                raise DomainValidationError(f"{name} must be an explicit boolean")


@dataclass(frozen=True, slots=True)
class InstrumentInterpretation:
    """Structured per-instrument explanation of one interpretation run."""

    instrument_id: UUID
    target_weight: Decimal
    unadjusted_market_close: Decimal | None
    contract_multiplier: Decimal
    target_value: Decimal
    raw_quantity: Decimal | None
    target_quantity: Decimal | None
    current_quantity: Decimal
    delta: Decimal | None
    order_side: OrderSide | None
    order_quantity: Decimal | None
    orderable: bool
    issues: tuple[StructuredReason, ...] = ()


@dataclass(frozen=True, slots=True)
class DecisionInterpretationResult:
    """Structured outcome of interpreting one strategy decision.

    ``protocol_reason`` carries decision-protocol anomalies (for example
    an unknown mode) that the frozen snapshot reason-code vocabulary has
    no code for; the interpreter treats them as whole-decision rejections
    without touching the snapshot consistency fields.  Formal runs are
    expected to reject unknown modes upstream at payload validation.
    """

    decision_status: DecisionStatus | str
    snapshot_consistency_status: SnapshotConsistencyStatus | str
    weight_sum: Decimal
    weight_sum_tolerance: Decimal
    weight_boundary_status: WeightBoundaryStatus | str | None
    cash_weight: Decimal | None
    issues: tuple[StructuredReason, ...] = ()
    warnings: tuple[StructuredReason, ...] = ()
    instrument_results: tuple[InstrumentInterpretation, ...] = ()
    order_intents: tuple[OrderIntent, ...] = ()
    protocol_reason: StructuredReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_status", DecisionStatus(self.decision_status)
        )
        object.__setattr__(
            self,
            "snapshot_consistency_status",
            SnapshotConsistencyStatus(self.snapshot_consistency_status),
        )
        if self.weight_boundary_status is not None:
            object.__setattr__(
                self,
                "weight_boundary_status",
                WeightBoundaryStatus(self.weight_boundary_status),
            )


def _floor_to_grid(quantity: Decimal, unit: Decimal) -> Decimal:
    """Floor ``quantity`` onto multiples of ``unit`` (both positive)."""

    return (quantity / unit).to_integral_value(rounding=ROUND_FLOOR) * unit


def _respects_precision(value: Decimal, precision: int) -> bool:
    """Whether ``value`` is exactly representable at ``precision`` digits."""

    digits = value.normalize().as_tuple()
    if not isinstance(digits.exponent, int):
        return False
    return digits.exponent >= -precision


def _precision_unit(precision: int) -> Decimal:
    """Smallest positive quantity expressible at ``precision`` digits."""

    return Decimal(1).scaleb(-precision)


class LongOnlyTargetWeightsInterpreter:
    """The first formal long-only target-weight interpreter.

    Registered identity: ``long_only_target_weights@1``.  The single
    parameter ``weight_sum_tolerance`` bounds how far above one the weight
    sum may sit before the whole decision is rejected; weights are never
    normalized automatically.
    """

    interpreter_key = INTERPRETER_KEY_LONG_ONLY_TARGET_WEIGHTS
    interpreter_version = INTERPRETER_VERSION

    def __init__(
        self, *, weight_sum_tolerance: Decimal | int | str = ZERO
    ) -> None:
        tolerance = _decimal(weight_sum_tolerance, "weight_sum_tolerance")
        if tolerance < ZERO:
            raise DomainValidationError(
                "weight_sum_tolerance must be non-negative"
            )
        self._weight_sum_tolerance = tolerance

    @property
    def weight_sum_tolerance(self) -> Decimal:
        return self._weight_sum_tolerance

    @property
    def parameters(self) -> Mapping[str, Decimal]:
        return MappingProxyType(
            {"weight_sum_tolerance": self._weight_sum_tolerance}
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def interpret(
        self,
        decision: object,
        *,
        snapshot: PortfolioDecisionSnapshot,
        facts: Mapping[UUID, InstrumentExecutionFacts],
        unadjusted_market_closes: Mapping[UUID, Decimal | int | str],
        allowed_instrument_ids: set[UUID] | None = None,
    ) -> DecisionInterpretationResult:
        """Interpret one decision, or reject it as a whole.

        ``facts`` must cover every targeted instrument *and* every existing
        position (an omitted position becomes a zero target whose sell
        legality still depends on its rules).
        """

        mode = getattr(decision, "mode", None)
        # Keep the raw payload untouched: coercing falsy values to {}
        # here would let malformed payloads (None, [], "", 0, False) slip
        # past the type check below as empty target sets.
        raw_targets = getattr(decision, "targets", {})
        decision_time = getattr(decision, "decision_time", None)

        problems = list(snapshot.consistency_problems)
        issues: list[StructuredReason] = [
            StructuredReason(
                stage=ResultStage.DECISION,
                code=_SNAPSHOT_PROBLEM_CODES[problem].value,
                details={"problem": problem},
            )
            for problem in sorted(
                problems, key=_SNAPSHOT_PROBLEM_PRIORITY.index
            )
        ]

        # Corporate-action cash must be in its actual state: only credited
        # cash participates in decision equity, and an unknown state makes
        # the whole snapshot unusable for sizing.
        cash_status = snapshot.corporate_action_snapshot.cash_status
        if cash_status is CorporateActionCashStatus.UNKNOWN:
            issues.append(
                StructuredReason(
                    stage=ResultStage.DECISION,
                    code=DecisionReasonCode.DECISION_SNAPSHOT_INVALID.value,
                    details={
                        "corporate_action_cash_status": (
                            CorporateActionCashStatus.UNKNOWN.value
                        )
                    },
                )
            )
            problems.append("invalid")

        main_problem = self._main_problem(problems)
        effective_status = (
            SnapshotConsistencyStatus(main_problem)
            if main_problem is not None
            else SnapshotConsistencyStatus.CONSISTENT
        )

        # Audit trail: not-credited corporate-action cash is excluded from
        # decision equity by construction; every result path must say so
        # instead of relying on the caller having sized equity correctly.
        warnings: list[StructuredReason] = []
        if cash_status is CorporateActionCashStatus.NOT_CREDITED:
            warnings.append(
                StructuredReason(
                    stage=ResultStage.DECISION,
                    code=InterpretationAuditCode.CORPORATE_ACTION_CASH_NOT_CREDITED.value,
                    details={
                        "corporate_action_cash_status": (
                            CorporateActionCashStatus.NOT_CREDITED.value
                        ),
                        "note": "pending corporate-action cash excluded "
                        "from decision equity",
                    },
                )
            )

        if main_problem is not None:
            return self._rejected(
                reason_code=_SNAPSHOT_PROBLEM_CODES[main_problem],
                issues=tuple(issues),
                snapshot=snapshot,
                consistency_status=effective_status,
                warnings=tuple(warnings),
            )

        # Only the two first-version decision modes are interpretable; any
        # other mode is a decision-protocol violation, not a snapshot
        # problem: it rejects through the dedicated protocol_reason field
        # instead of borrowing DECISION_SNAPSHOT_INVALID.  Formal runs are
        # expected to reject unknown modes at payload validation already;
        # this is the interpreter's fail-closed backstop.
        if mode != "hold" and mode != "target_weights":
            return DecisionInterpretationResult(
                decision_status=DecisionStatus.REJECTED,
                snapshot_consistency_status=effective_status,
                weight_sum=ZERO,
                weight_sum_tolerance=self._weight_sum_tolerance,
                weight_boundary_status=None,
                cash_weight=None,
                warnings=tuple(warnings),
                protocol_reason=StructuredReason(
                    stage=ResultStage.DECISION,
                    code=InterpretationAuditCode.UNKNOWN_DECISION_MODE.value,
                    details={"mode": str(mode)},
                ),
            )

        # hold is a legal no-op and never produces order intents; it is
        # admitted only after the snapshot passed the consistency gate and
        # before targets-payload validation, because it consumes no
        # targets at all (its payload is validated upstream).
        if mode == "hold":
            return DecisionInterpretationResult(
                decision_status=DecisionStatus.ACCEPTED_NOOP,
                snapshot_consistency_status=effective_status,
                weight_sum=ZERO,
                weight_sum_tolerance=self._weight_sum_tolerance,
                weight_boundary_status=None,
                cash_weight=None,
                warnings=tuple(warnings),
            )

        # The targets payload must be a mapping for the target-weights
        # mode: anything else — including falsy values such as None,
        # [], "", 0, or False — is a decision-protocol violation, not a
        # snapshot problem, and rejects through protocol_reason instead
        # of being silently read as an empty target set.
        if not isinstance(raw_targets, Mapping):
            return DecisionInterpretationResult(
                decision_status=DecisionStatus.REJECTED,
                snapshot_consistency_status=effective_status,
                weight_sum=ZERO,
                weight_sum_tolerance=self._weight_sum_tolerance,
                weight_boundary_status=None,
                cash_weight=None,
                warnings=tuple(warnings),
                protocol_reason=StructuredReason(
                    stage=ResultStage.DECISION,
                    code=InterpretationAuditCode.INVALID_TARGETS_PAYLOAD.value,
                    details={
                        "mode": str(mode),
                        "targets_type": type(raw_targets).__name__,
                    },
                ),
            )

        # ---- Weight validation stage ---------------------------------
        normalized_targets, target_issues = self._normalize_targets(raw_targets)
        issues.extend(target_issues)
        scope_issues = self._scope_issues(
            normalized_targets, allowed_instrument_ids
        )
        issues.extend(scope_issues)
        weight_issues = [
            issue
            for issue in target_issues
            if issue.code == DecisionReasonCode.INVALID_WEIGHT.value
        ]
        malformed_target_issues = [
            issue
            for issue in target_issues
            if issue.code == DecisionReasonCode.INSTRUMENT_RULE_MISSING.value
        ]
        if weight_issues or scope_issues or malformed_target_issues:
            primary = (
                weight_issues
                or malformed_target_issues
                or scope_issues
            )[0]
            return self._rejected(
                reason_code=DecisionReasonCode(primary.code),
                issues=tuple(issues),
                snapshot=snapshot,
                instrument_results=self._instrument_results_for_rejection(
                    normalized_targets, snapshot.positions
                ),
                warnings=tuple(warnings),
            )

        weight_sum = sum(normalized_targets.values(), ZERO)
        boundary = self._boundary_status(weight_sum)
        if boundary is WeightBoundaryStatus.EXCEEDED:
            issues.append(
                StructuredReason(
                    stage=ResultStage.DECISION,
                    code=DecisionReasonCode.WEIGHT_SUM_EXCEEDED.value,
                    details={
                        "weight_sum": str(weight_sum),
                        "weight_sum_tolerance": str(self._weight_sum_tolerance),
                        "limit": str(Decimal("1") + self._weight_sum_tolerance),
                    },
                )
            )
            return self._rejected(
                reason_code=DecisionReasonCode.WEIGHT_SUM_EXCEEDED,
                issues=tuple(issues),
                snapshot=snapshot,
                weight_sum=weight_sum,
                boundary=boundary,
                warnings=tuple(warnings),
            )

        # ---- Target sizing stage -------------------------------------
        omitted = sorted(
            instrument_id
            for instrument_id in snapshot.positions
            if instrument_id not in normalized_targets
            and snapshot.positions[instrument_id] != ZERO
        )
        warnings.extend(
            StructuredReason(
                stage=ResultStage.DECISION,
                code=InterpretationAuditCode.OMITTED_POSITION_ZERO_TARGET.value,
                details={"instrument_id": str(instrument_id)},
            )
            for instrument_id in omitted
        )

        considered = sorted(
            set(normalized_targets) | set(snapshot.positions), key=str
        )
        instrument_results: list[InstrumentInterpretation] = []
        intents: list[OrderIntent] = []
        sizing_issues: list[StructuredReason] = []
        for instrument_id in considered:
            facts_entry = facts.get(instrument_id)
            close = unadjusted_market_closes.get(instrument_id)
            result, intent, per_instrument_issues = self._interpret_instrument(
                instrument_id=instrument_id,
                weight=normalized_targets.get(instrument_id, ZERO),
                current_quantity=snapshot.positions.get(instrument_id, ZERO),
                facts_entry=facts_entry,
                close=close,
                equity=snapshot.equity,
                decision_time=decision_time,
            )
            instrument_results.append(result)
            issues.extend(per_instrument_issues)
            sizing_issues.extend(per_instrument_issues)
            if intent is not None:
                intents.append(intent)

        if sizing_issues:
            return self._rejected(
                reason_code=self._primary_sizing_code(sizing_issues),
                issues=tuple(issues),
                snapshot=snapshot,
                weight_sum=weight_sum,
                boundary=boundary,
                instrument_results=tuple(instrument_results),
                warnings=tuple(warnings),
            )

        if not intents:
            status = DecisionStatus.ACCEPTED_NOOP
        else:
            status = DecisionStatus.ACCEPTED
        cash_weight = max(Decimal("1") - min(weight_sum, Decimal("1")), ZERO)
        return DecisionInterpretationResult(
            decision_status=status,
            snapshot_consistency_status=effective_status,
            weight_sum=weight_sum,
            weight_sum_tolerance=self._weight_sum_tolerance,
            weight_boundary_status=boundary,
            cash_weight=cash_weight,
            warnings=tuple(warnings),
            instrument_results=tuple(instrument_results),
            order_intents=tuple(intents),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _main_problem(
        problems: Sequence[str],
    ) -> str | None:
        for candidate in _SNAPSHOT_PROBLEM_PRIORITY:
            if candidate in problems:
                return candidate
        return None

    @staticmethod
    def _primary_sizing_code(
        issues: Sequence[StructuredReason],
    ) -> DecisionReasonCode:
        """Pick the deterministic main code among per-instrument issues."""

        # Sizing issues are already appended in stable instrument order;
        # the first one is the main reason, later ones stay in issues[].
        for issue in issues:
            try:
                return DecisionReasonCode(issue.code)
            except ValueError:
                continue
        return DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE

    def _normalize_targets(
        self, raw_targets: object
    ) -> tuple[dict[UUID, Decimal], list[StructuredReason]]:
        targets: dict[UUID, Decimal] = {}
        issues: list[StructuredReason] = []
        items = dict(raw_targets).items() if isinstance(raw_targets, Mapping) else []
        for key, value in items:
            try:
                instrument_id = (
                    key if isinstance(key, UUID) else UUID(str(key))
                )
            except ValueError:
                issues.append(
                    StructuredReason(
                        stage=ResultStage.DECISION,
                        code=DecisionReasonCode.INSTRUMENT_RULE_MISSING.value,
                        details={
                            "targets_key": str(key),
                            # The frozen reason-code vocabulary has no
                            # dedicated code for malformed input; keep the
                            # mapping but distinguish the cause in details.
                            "reason": "invalid_instrument_id_format",
                        },
                    )
                )
                continue
            try:
                weight = _decimal(value, f"targets[{key}]")
            except (DomainValidationError, TypeError):
                issues.append(
                    StructuredReason(
                        stage=ResultStage.DECISION,
                        code=DecisionReasonCode.INVALID_WEIGHT.value,
                        details={
                            "instrument_id": str(instrument_id),
                            "reason": "not_a_finite_decimal",
                        },
                    )
                )
                continue
            if weight < ZERO or weight > Decimal("1"):
                issues.append(
                    StructuredReason(
                        stage=ResultStage.DECISION,
                        code=DecisionReasonCode.INVALID_WEIGHT.value,
                        details={
                            "instrument_id": str(instrument_id),
                            "weight": str(weight),
                            "reason": "outside_zero_one_range",
                        },
                    )
                )
                continue
            targets[instrument_id] = weight
        return targets, issues

    def _scope_issues(
        self,
        targets: Mapping[UUID, Decimal],
        allowed_instrument_ids: set[UUID] | None,
    ) -> list[StructuredReason]:
        if allowed_instrument_ids is None:
            return []
        return [
            StructuredReason(
                stage=ResultStage.DECISION,
                code=DecisionReasonCode.INSTRUMENT_RULE_MISSING.value,
                details={
                    "instrument_id": str(instrument_id),
                    "reason": "outside_allowed_scope",
                },
            )
            for instrument_id in sorted(set(targets) - set(allowed_instrument_ids), key=str)
        ]

    def _boundary_status(
        self, weight_sum: Decimal
    ) -> WeightBoundaryStatus:
        limit = Decimal("1") + self._weight_sum_tolerance
        if weight_sum > limit:
            return WeightBoundaryStatus.EXCEEDED
        if weight_sum == Decimal("1"):
            return WeightBoundaryStatus.FULLY_INVESTED
        if weight_sum > Decimal("1"):
            return WeightBoundaryStatus.OVER_WITHIN_TOLERANCE
        return WeightBoundaryStatus.UNDER_INVESTED

    def _interpret_instrument(
        self,
        *,
        instrument_id: UUID,
        weight: Decimal,
        current_quantity: Decimal,
        facts_entry: InstrumentExecutionFacts | None,
        close: Decimal | int | str | None,
        equity: Decimal,
        decision_time: datetime | None,
    ) -> tuple[
        InstrumentInterpretation, OrderIntent | None, list[StructuredReason]
    ]:
        """Size one instrument, or record why it cannot be ordered."""

        issues: list[StructuredReason] = []
        # Initialized before ``fail`` may run: the closure reads this cell
        # even on early failures that precede close-price parsing.
        close_value: Decimal | None = None

        def fail(
            code: DecisionReasonCode, details: Mapping[str, object]
        ) -> tuple[
            InstrumentInterpretation, OrderIntent | None,
            list[StructuredReason],
        ]:
            issue = StructuredReason(
                stage=ResultStage.DECISION,
                code=code.value,
                details={
                    "instrument_id": str(instrument_id),
                    **details,
                },
            )
            issues.append(issue)
            result = InstrumentInterpretation(
                instrument_id=instrument_id,
                target_weight=weight,
                unadjusted_market_close=close_value,
                contract_multiplier=multiplier,
                target_value=target_value,
                raw_quantity=None,
                target_quantity=None,
                current_quantity=current_quantity,
                delta=None,
                order_side=None,
                order_quantity=None,
                orderable=False,
                issues=(issue,),
            )
            return result, None, issues

        multiplier = (
            facts_entry.contract_multiplier
            if facts_entry is not None
            else Decimal("1")
        )
        target_value = equity * weight

        if facts_entry is None:
            return fail(
                DecisionReasonCode.INSTRUMENT_RULE_MISSING,
                {"reason": "execution_facts_missing"},
            )
        # A corrupt close price must produce a structured whole-decision
        # rejection, never an exception leaking out of interpretation.
        close_value = None
        if close is not None:
            try:
                close_value = _decimal(close, "unadjusted_market_close")
            except (DomainValidationError, TypeError) as exc:
                return fail(
                    DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE,
                    {
                        "reason": "invalid_unadjusted_market_close",
                        "unadjusted_market_close": str(close),
                        "error": str(exc),
                    },
                )
        if current_quantity < ZERO:
            return fail(
                DecisionReasonCode.DECISION_SNAPSHOT_INVALID,
                {
                    "reason": "negative_current_position",
                    "current_quantity": str(current_quantity),
                },
            )
        if not _respects_precision(
            current_quantity, facts_entry.holding_precision
        ):
            return fail(
                DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE,
                {
                    "reason": "current_position_holding_precision_invalid",
                    "current_quantity": str(current_quantity),
                    "holding_precision": facts_entry.holding_precision,
                },
            )

        if weight == ZERO:
            # Omitted positions and explicit zero targets: full liquidation.
            target_quantity = ZERO
        else:
            if close_value is None or close_value <= ZERO:
                return fail(
                    DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE,
                    {
                        "reason": (
                            "missing_unadjusted_market_close"
                            if close_value is None
                            else "invalid_unadjusted_market_close"
                        ),
                        "unadjusted_market_close": (
                            None if close_value is None else str(close_value)
                        ),
                    },
                )
            raw_quantity = (
                target_value / close_value / multiplier
            )
            lot = facts_entry.lot_size
            target_quantity = _floor_to_grid(raw_quantity, lot)
            if not _respects_precision(
                target_quantity, facts_entry.order_precision
            ):
                target_quantity = _floor_to_grid(
                    target_quantity,
                    _precision_unit(facts_entry.order_precision),
                )

        delta = target_quantity - current_quantity
        side: OrderSide | None = None
        order_quantity: Decimal | None = None
        if delta > ZERO:
            side = OrderSide.BUY
            order_quantity = delta
            detail = self._buy_orderability_detail(delta, facts_entry)
            if detail is not None:
                return fail(
                    DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE,
                    detail,
                )
        elif delta < ZERO:
            side = OrderSide.SELL
            order_quantity = -delta
            detail = self._sell_orderability_detail(
                order_quantity, current_quantity, facts_entry
            )
            if detail is not None:
                return fail(
                    DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE,
                    detail,
                )

        intent: OrderIntent | None = None
        if side is not None and order_quantity is not None:
            assert decision_time is not None, (
                "validated decisions always carry a decision_time"
            )
            intent = OrderIntent(
                intent_id=uuid5(
                    _interpreter_namespace(),
                    f"{self.interpreter_key}@{self.interpreter_version}:"
                    f"{decision_time.isoformat()}:{instrument_id}",
                ),
                instrument_id=instrument_id,
                side=side,
                quantity=order_quantity,
                valid_from=decision_time,
            )

        result = InstrumentInterpretation(
            instrument_id=instrument_id,
            target_weight=weight,
            unadjusted_market_close=close_value,
            contract_multiplier=multiplier,
            target_value=target_value,
            raw_quantity=(
                target_value / close_value / multiplier
                if weight != ZERO and close_value is not None and close_value > ZERO
                else None
            ),
            target_quantity=target_quantity,
            current_quantity=current_quantity,
            delta=delta,
            order_side=side,
            order_quantity=order_quantity,
            orderable=True,
        )
        return result, intent, issues

    @staticmethod
    def _buy_orderability_detail(
        quantity: Decimal, facts_entry: InstrumentExecutionFacts
    ) -> dict[str, object] | None:
        """Return failure details for an unorderable buy, else ``None``."""

        if not _respects_precision(quantity, facts_entry.order_precision):
            return {
                "reason": "order_precision_invalid",
                "quantity": str(quantity),
                "order_precision": facts_entry.order_precision,
            }
        if quantity % facts_entry.lot_size != ZERO:
            return {
                "reason": "not_multiple_of_lot",
                "quantity": str(quantity),
                "lot_size": str(facts_entry.lot_size),
            }
        if quantity < facts_entry.minimum_order_quantity:
            return {
                "reason": "below_minimum_order_quantity",
                "quantity": str(quantity),
                "minimum_order_quantity": str(
                    facts_entry.minimum_order_quantity
                ),
            }
        return None

    @staticmethod
    def _sell_orderability_detail(
        quantity: Decimal,
        current_quantity: Decimal,
        facts_entry: InstrumentExecutionFacts,
    ) -> dict[str, object] | None:
        """Return failure details for an unorderable sell, else ``None``.

        A sell is a full liquidation exactly when it disposes of the whole
        remaining holding.  Odd-lot exemptions apply only when their
        dedicated flag is declared; a policy alone never waives ``lot_size``.
        """

        full_liquidation = quantity == current_quantity
        if not _respects_precision(quantity, facts_entry.order_precision):
            if full_liquidation and (
                facts_entry.full_liquidation_bypasses_order_precision
            ):
                pass
            else:
                return {
                    "reason": "order_precision_invalid",
                    "quantity": str(quantity),
                    "order_precision": facts_entry.order_precision,
                }
        if quantity % facts_entry.lot_size != ZERO:
            exemption_declared = (
                full_liquidation
                and facts_entry.full_liquidation_bypasses_lot_size
            ) or (
                facts_entry.sell_odd_lot_policy
                is SellOddLotPolicy.ALLOW_ODD_LOT
                and facts_entry.odd_lot_bypasses_lot_size
            )
            if not exemption_declared:
                return {
                    "reason": "odd_lot_lot_size_exemption_missing"
                    if (
                        facts_entry.sell_odd_lot_policy
                        is SellOddLotPolicy.ALLOW_ODD_LOT
                        or full_liquidation
                    )
                    else "not_multiple_of_lot",
                    "quantity": str(quantity),
                    "lot_size": str(facts_entry.lot_size),
                    "full_liquidation": full_liquidation,
                }
        if quantity < facts_entry.minimum_order_quantity:
            return {
                "reason": "below_minimum_order_quantity",
                "quantity": str(quantity),
                "minimum_order_quantity": str(
                    facts_entry.minimum_order_quantity
                ),
            }
        return None

    def _rejected(
        self,
        *,
        reason_code: DecisionReasonCode,
        issues: tuple[StructuredReason, ...],
        snapshot: PortfolioDecisionSnapshot,
        consistency_status: SnapshotConsistencyStatus | None = None,
        weight_sum: Decimal = ZERO,
        boundary: WeightBoundaryStatus | None = None,
        instrument_results: tuple[InstrumentInterpretation, ...] = (),
        warnings: tuple[StructuredReason, ...] = (),
    ) -> DecisionInterpretationResult:
        """Build a whole-decision rejection: no order intent survives."""

        return DecisionInterpretationResult(
            decision_status=DecisionStatus.REJECTED,
            snapshot_consistency_status=(
                consistency_status
                if consistency_status is not None
                else snapshot.consistency_status
            ),
            weight_sum=weight_sum,
            weight_sum_tolerance=self._weight_sum_tolerance,
            weight_boundary_status=boundary,
            cash_weight=None,
            issues=issues,
            warnings=warnings,
            instrument_results=instrument_results,
            order_intents=(),
        )

    def _instrument_results_for_rejection(
        self,
        targets: Mapping[UUID, Decimal],
        positions: Mapping[UUID, Decimal],
    ) -> tuple[InstrumentInterpretation, ...]:
        """Minimal per-instrument records for early-stage rejections."""

        considered = sorted(set(targets) | set(positions), key=str)
        return tuple(
            InstrumentInterpretation(
                instrument_id=instrument_id,
                target_weight=targets.get(instrument_id, ZERO),
                unadjusted_market_close=None,
                contract_multiplier=Decimal("1"),
                target_value=targets.get(instrument_id, ZERO),
                raw_quantity=None,
                target_quantity=None,
                current_quantity=positions.get(instrument_id, ZERO),
                delta=None,
                order_side=None,
                order_quantity=None,
                orderable=False,
            )
            for instrument_id in considered
        )


_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "quant-foundry:decision-interpreter:long_only_target_weights",
)


def _interpreter_namespace() -> UUID:
    return _NAMESPACE
