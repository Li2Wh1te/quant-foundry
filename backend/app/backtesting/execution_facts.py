"""Normalization of trading-status facts into session execution facts.

Rule packages declare three capability dimensions — ``suspension``,
``opening_availability``, and ``price_limit_tradability`` — as either
``required`` or ``not_applicable``.  Before any order matching, the
generic :class:`TradingStatus` facts of one instrument session are
normalized here into an explicit, typed
:class:`InstrumentSessionExecutionFacts` object so the execution model
never interprets free-form status strings or JSON attributes.

Hard rules:

* a ``not_applicable`` dimension is accepted only from the frozen
  capability declaration — a missing fact never becomes ``false``/``true``
  defaults;
* every fact consumed must cover the session date, be declared
  ``complete``, carry knowledge-time evidence with ``known_at <=
  data_cutoff``, and name its dimension;
* conflicting or unknown status values are stable blocking errors, and
  nothing is ever guessed from OHLC shapes or missing bars;
* required dimensions without a complete covering fact produce stable
  per-dimension blocking codes for the preflight report.

Status vocabulary contract (the ``status`` field plus the mandatory
``dimension`` attribute key):

============================  ===========================================
dimension                     accepted statuses / required attributes
============================  ===========================================
suspension                    ``tradable``, ``suspended``
opening_availability          ``available``, ``unavailable``
price_limit_tradability       ``none``/``up``/``down`` plus explicit
                              boolean attributes ``buy_allowed`` and
                              ``sell_allowed``
============================  ===========================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Mapping, Sequence
from uuid import UUID

from app.backtesting.data.errors import freeze_json
from app.backtesting.data.facts import TradingStatus
from app.backtesting.data.requests import QualityStatus
from app.backtesting.domain import _aware_datetime

__all__ = [
    "CAPABILITY_DIMENSION_SUSPENSION",
    "CAPABILITY_DIMENSION_OPENING_AVAILABILITY",
    "CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY",
    "DirectionalAvailability",
    "ExecutionFactIssue",
    "InstrumentSessionExecutionFacts",
    "OpeningState",
    "PriceLimitState",
    "SuspensionState",
    "evaluate_execution_facts",
    "market_state_from_execution_facts",
]


CAPABILITY_DIMENSION_SUSPENSION = "suspension"
CAPABILITY_DIMENSION_OPENING_AVAILABILITY = "opening_availability"
CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY = "price_limit_tradability"

#: The three dimensions every formal rule package must declare explicitly.
ALL_DIMENSIONS = (
    CAPABILITY_DIMENSION_SUSPENSION,
    CAPABILITY_DIMENSION_OPENING_AVAILABILITY,
    CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY,
)

#: Stable blocking codes, one per required dimension.
DIMENSION_MISSING_CODES = {
    CAPABILITY_DIMENSION_SUSPENSION: "trading_status_fact_missing",
    CAPABILITY_DIMENSION_OPENING_AVAILABILITY: (
        "opening_availability_fact_missing"
    ),
    CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY: (
        "price_limit_tradability_fact_missing"
    ),
}

#: Chinese one-line summaries used in operator-facing issue messages.
_DIMENSION_LABELS_ZH = {
    CAPABILITY_DIMENSION_SUSPENSION: "停牌状态",
    CAPABILITY_DIMENSION_OPENING_AVAILABILITY: "开盘可用性",
    CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY: "涨跌停方向性可成交性",
}


class SuspensionState(StrEnum):
    """Normalized suspension state of one instrument session."""

    SUSPENDED = "suspended"
    TRADABLE = "tradable"
    NOT_APPLICABLE = "not_applicable"


class OpeningState(StrEnum):
    """Normalized opening availability of one instrument session."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class DirectionalAvailability(StrEnum):
    """Directional tradability under a price limit."""

    YES = "yes"
    NO = "no"
    NOT_APPLICABLE = "not_applicable"


class PriceLimitState(StrEnum):
    """Price-limit side state; never inferred from OHLC shapes."""

    NONE = "none"
    UP = "up"
    DOWN = "down"
    NOT_APPLICABLE = "not_applicable"


#: Accepted raw ``TradingStatus.status`` values per dimension.
_STATUS_VOCABULARY = {
    CAPABILITY_DIMENSION_SUSPENSION: {"suspended", "tradable"},
    CAPABILITY_DIMENSION_OPENING_AVAILABILITY: {"available", "unavailable"},
    CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY: {"none", "up", "down"},
}


@dataclass(frozen=True, slots=True)
class ExecutionFactIssue:
    """One stable, operator-reportable normalization problem."""

    code: str
    dimension: str
    message: str
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen = freeze_json(
            dict(self.details) if isinstance(self.details, Mapping) else {},
            "details",
        )
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "details", frozen)


@dataclass(frozen=True, slots=True)
class InstrumentSessionExecutionFacts:
    """Typed execution facts for one instrument on one session date.

    Every field is explicit: there are no implicit ``False``/``True``
    defaults anywhere in this object.  ``not_applicable`` values always
    originate from the frozen capability declaration, never from absent
    facts.  ``evidence`` deep-freezes the per-dimension provenance plus
    the applicability declarations for preflight reports and snapshots.
    """

    instrument_id: UUID
    calendar_id: str
    session_date: date
    suspension_state: SuspensionState
    opening_state: OpeningState
    buy_allowed: DirectionalAvailability
    sell_allowed: DirectionalAvailability
    price_limit_status: PriceLimitState
    evidence: Mapping[str, object]


def evaluate_execution_facts(
    instrument_id: UUID,
    *,
    calendar_id: str,
    session_date: date,
    applicability: Mapping[str, str],
    status_facts: Sequence[TradingStatus],
    data_cutoff: datetime,
    rule_package_reference: str | None = None,
) -> tuple[InstrumentSessionExecutionFacts | None, tuple[ExecutionFactIssue, ...]]:
    """Normalize one session's facts into typed states or blocking issues.

    Business-level fact problems never raise: they come back as stable
    :class:`ExecutionFactIssue` values so a preflight report can list all
    broken dimensions at once.  Only caller contract violations (wrong
    types) raise.  With no issues, the returned facts object is fully
    populated — including explicit ``not_applicable`` markers — and its
    evidence mapping is safe to embed in reports and run snapshots.
    """

    _aware_datetime(data_cutoff, "data_cutoff")
    if not isinstance(instrument_id, UUID):
        raise ValueError("instrument_id must be a UUID")
    if not isinstance(calendar_id, str) or not calendar_id.strip():
        raise ValueError("calendar_id must be non-blank text")
    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise ValueError("session_date must be a calendar date")
    if not isinstance(applicability, Mapping):
        raise ValueError("applicability must be a mapping")

    issues: list[ExecutionFactIssue] = []

    # The applicability declaration itself must be explicit for every
    # dimension; a missing declaration fails closed.
    declarations: dict[str, str] = {}
    for dimension in ALL_DIMENSIONS:
        declared = applicability.get(dimension)
        if declared not in ("required", "not_applicable"):
            issues.append(
                ExecutionFactIssue(
                    code="trading_status_declaration_missing",
                    dimension=dimension,
                    message=(
                        f"能力维度 {dimension} 缺少 required 或 "
                        "not_applicable 的显式声明，正式运行阻断"
                    ),
                    details={
                        "instrument_id": str(instrument_id),
                        "session_date": session_date.isoformat(),
                    },
                )
            )
            continue
        declarations[dimension] = str(declared)

    # Bucket the visible covering facts per dimension.  Facts without
    # knowledge-time evidence or learned after the cutoff do not exist
    # for this session.
    covering: dict[str, list[TradingStatus]] = {dim: [] for dim in ALL_DIMENSIONS}
    for fact in status_facts:
        if not isinstance(fact, TradingStatus):
            raise ValueError("status_facts entries must be TradingStatus")
        if fact.instrument_id != instrument_id:
            # Facts belonging to another instrument must never leak into
            # this session's gating: a complete foreign fact would wrongly
            # release or wrongly expire orders.
            issues.append(
                ExecutionFactIssue(
                    code="trading_status_fact_instrument_mismatch",
                    dimension=str(fact.attributes.get("dimension")),
                    message=(
                        "交易状态事实属于其他标的，不能用于当前标的的"
                        "会话事实门禁，正式运行阻断"
                    ),
                    details={
                        "instrument_id": str(instrument_id),
                        "fact_instrument_id": str(fact.instrument_id),
                        "session_date": session_date.isoformat(),
                    },
                )
            )
            continue
        if not _covers_session(fact, session_date):
            continue
        known_at = fact.evidence.known_at
        if known_at is None or known_at > data_cutoff:
            continue
        dimension = fact.attributes.get("dimension")
        if dimension not in ALL_DIMENSIONS:
            issues.append(
                ExecutionFactIssue(
                    code="trading_status_fact_conflict",
                    dimension=str(dimension),
                    message=(
                        "交易状态事实缺少合法的 dimension 标记，无法归入"
                        "任何能力维度，正式运行阻断"
                    ),
                    details={
                        "instrument_id": str(instrument_id),
                        "session_date": session_date.isoformat(),
                        "status": fact.status,
                    },
                )
            )
            continue
        if fact.evidence.quality_status is not QualityStatus.COMPLETE:
            issues.append(
                ExecutionFactIssue(
                    code="trading_status_fact_not_complete",
                    dimension=dimension,
                    message=(
                        f"能力维度 {_DIMENSION_LABELS_ZH[dimension]}"
                        f"（{dimension}）的事实质量标记不是 complete，"
                        "正式运行阻断"
                    ),
                    details={
                        "instrument_id": str(instrument_id),
                        "session_date": session_date.isoformat(),
                        "quality_status": fact.evidence.quality_status.value,
                        "source": fact.evidence.source,
                    },
                )
            )
            continue
        covering[dimension].append(fact)

    # Resolve each declared dimension to exactly one normalized state.
    states: dict[str, object] = {}
    evidence: dict[str, object] = {}
    for dimension in ALL_DIMENSIONS:
        declared = declarations.get(dimension)
        if declared is None:
            continue  # declaration issue already recorded above
        facts = covering[dimension]
        if not facts:
            if declared == "required":
                issues.append(
                    ExecutionFactIssue(
                        code=DIMENSION_MISSING_CODES[dimension],
                        dimension=dimension,
                        message=(
                            f"能力维度 {_DIMENSION_LABELS_ZH[dimension]}"
                            f"（{dimension}）声明为 required，但会话 "
                            f"{session_date.isoformat()} 没有可见的完整事实，"
                            "正式运行阻断"
                        ),
                        details={
                            "instrument_id": str(instrument_id),
                            "session_date": session_date.isoformat(),
                            "applicability": declared,
                            "rule_package_reference": rule_package_reference,
                        },
                    )
                )
                # Absence never becomes not_applicable: the session stays
                # blocked until a real fact arrives.
            else:
                _apply_not_applicable(dimension, states)
                evidence[dimension] = {
                    "applicability": "not_applicable",
                    "rule_package_reference": rule_package_reference,
                }
            continue

        if declared == "not_applicable":
            # A declared-not-applicable dimension must not be silently
            # overridden by facts that arrived anyway: the inputs disagree
            # with the frozen declaration, which fails closed.
            issues.append(
                ExecutionFactIssue(
                    code="trading_status_fact_conflict",
                    dimension=dimension,
                    message=(
                        f"能力维度 {_DIMENSION_LABELS_ZH[dimension]}"
                        f"（{dimension}）已声明为 not_applicable，"
                        "但输入中仍携带该维度的事实，正式运行阻断"
                    ),
                    details={
                        "instrument_id": str(instrument_id),
                        "session_date": session_date.isoformat(),
                        "applicability": declared,
                        "statuses": sorted(fact.status for fact in facts),
                    },
                )
            )
            continue

        parsed = [
            _parse_fact_value(dimension, fact, issues) for fact in facts
        ]
        distinct = {value for value in parsed if value is not None}
        if len(distinct) > 1:
            issues.append(
                ExecutionFactIssue(
                    code="trading_status_fact_conflict",
                    dimension=dimension,
                    message=(
                        f"能力维度 {_DIMENSION_LABELS_ZH[dimension]}"
                        f"（{dimension}）在会话 {session_date.isoformat()} "
                        "存在相互冲突的事实，正式运行阻断"
                    ),
                    details={
                        "instrument_id": str(instrument_id),
                        "session_date": session_date.isoformat(),
                        "values": sorted(str(value) for value in distinct),
                    },
                )
            )
            continue
        if not distinct:
            continue  # parse-level issues already recorded above
        chosen = next(iter(distinct))
        source_fact = facts[0]
        record: dict[str, object] = {
            "applicability": "required",
            "status": source_fact.status,
            "source": source_fact.evidence.source,
            "source_revision": source_fact.evidence.source_revision,
            "observed_at": source_fact.evidence.observed_at.isoformat(),
            "known_at": (
                source_fact.evidence.known_at.isoformat()
                if source_fact.evidence.known_at is not None
                else None
            ),
            "quality_status": source_fact.evidence.quality_status.value,
            "rule_package_reference": rule_package_reference,
        }
        evidence[dimension] = record
        if dimension == CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY:
            # The parsed value is the whole (status, buy, sell) triple, so
            # directional disagreement between facts is a conflict above
            # and the winning triple can never disagree internally.
            limit_state, buy_state, sell_state = chosen
            states[dimension] = limit_state
            states["buy_allowed"] = buy_state
            states["sell_allowed"] = sell_state
            record["buy_allowed"] = buy_state.value
            record["sell_allowed"] = sell_state.value
        else:
            states[dimension] = chosen

    if issues:
        return None, tuple(issues)

    assert set(states) >= {
        CAPABILITY_DIMENSION_SUSPENSION,
        CAPABILITY_DIMENSION_OPENING_AVAILABILITY,
        CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY,
        "buy_allowed",
        "sell_allowed",
    }, "all dimensions must resolve before a formal facts object exists"

    frozen_evidence = freeze_json(evidence, "evidence")
    assert isinstance(frozen_evidence, Mapping)
    facts_result = InstrumentSessionExecutionFacts(
        instrument_id=instrument_id,
        calendar_id=calendar_id,
        session_date=session_date,
        suspension_state=states[CAPABILITY_DIMENSION_SUSPENSION],
        opening_state=states[CAPABILITY_DIMENSION_OPENING_AVAILABILITY],
        buy_allowed=states["buy_allowed"],
        sell_allowed=states["sell_allowed"],
        price_limit_status=states[
            CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY
        ],
        evidence=frozen_evidence,
    )
    return facts_result, ()


def _covers_session(fact: TradingStatus, session_date: date) -> bool:
    """Half-open validity check of one fact against the session date."""

    if fact.valid_from > session_date:
        return False
    return fact.valid_to is None or session_date < fact.valid_to


def _parse_fact_value(
    dimension: str, fact: TradingStatus, issues: list[ExecutionFactIssue]
) -> object | None:
    """Parse one covering fact into its comparable value, or record issues."""

    if fact.status not in _STATUS_VOCABULARY[dimension]:
        issues.append(
            ExecutionFactIssue(
                code="trading_status_fact_conflict",
                dimension=dimension,
                message=(
                    f"能力维度 {_DIMENSION_LABELS_ZH[dimension]}"
                    f"（{dimension}）出现未知状态取值 {fact.status!r}，"
                    "正式运行阻断"
                ),
                details={
                    "instrument_id": str(fact.instrument_id),
                    "status": fact.status,
                },
            )
        )
        return None
    if dimension == CAPABILITY_DIMENSION_SUSPENSION:
        return SuspensionState(fact.status)
    if dimension == CAPABILITY_DIMENSION_OPENING_AVAILABILITY:
        return OpeningState(fact.status)
    # Price-limit facts must carry both directional booleans explicitly.
    attributes = fact.attributes
    if not all(
        isinstance(attributes.get(flag), bool)
        for flag in ("buy_allowed", "sell_allowed")
    ):
        issues.append(
            ExecutionFactIssue(
                code="trading_status_fact_not_complete",
                dimension=dimension,
                message=(
                    "涨跌停方向性可成交性事实缺少显式的 buy_allowed / "
                    "sell_allowed 布尔取值，正式运行阻断"
                ),
                details={
                    "instrument_id": str(fact.instrument_id),
                    "status": fact.status,
                },
            )
        )
        return None
    # The whole (status, buy, sell) triple participates in conflict
    # detection: two facts with the same limit status but different
    # directional availability are a conflict, not a first-write-wins.
    return (
        PriceLimitState(fact.status),
        DirectionalAvailability.YES if attributes["buy_allowed"] else DirectionalAvailability.NO,
        DirectionalAvailability.YES if attributes["sell_allowed"] else DirectionalAvailability.NO,
    )


def _apply_not_applicable(dimension: str, states: dict[str, object]) -> None:
    """Record an explicitly declared not-applicable dimension."""

    if dimension == CAPABILITY_DIMENSION_SUSPENSION:
        states[dimension] = SuspensionState.NOT_APPLICABLE
    elif dimension == CAPABILITY_DIMENSION_OPENING_AVAILABILITY:
        states[dimension] = OpeningState.NOT_APPLICABLE
    else:
        states[dimension] = PriceLimitState.NOT_APPLICABLE
        # A declared-not-applicable limit dimension carries no directional
        # gating at all; matching must not consult these flags.
        states["buy_allowed"] = DirectionalAvailability.NOT_APPLICABLE
        states["sell_allowed"] = DirectionalAvailability.NOT_APPLICABLE


def market_state_from_execution_facts(
    facts: InstrumentSessionExecutionFacts,
    *,
    open_price: Decimal | int | str | None,
    price_tick: Decimal | int | str,
    timestamp: datetime,
) -> "MarketState":
    """Build a :class:`MarketState` explicitly from normalized facts.

    Every boolean field is set from the typed fact states — the dataclass
    fixture defaults are never relied upon — and the full evidence is
    carried in ``facts_basis`` for run snapshots.

    "Not applicable" is the opposite of "not tradable": a dimension the
    frozen capability declaration marks ``not_applicable`` imposes no
    gate at all, so it maps to the permissive boolean while its
    applicability stays visible in ``facts_basis``.  Only an explicit
    negative fact (suspended / unavailable / direction denied) closes a
    gate.  The opening price is passed through unchanged: when it is
    ``None`` despite an available opening state, matching expires the
    order as ``open_unavailable`` rather than falling back to any other
    price source.
    """

    from app.backtesting.execution import MarketState, PriceLimitStatus

    if not isinstance(facts, InstrumentSessionExecutionFacts):
        raise ValueError("facts must be an InstrumentSessionExecutionFacts")

    price_limit_status = {
        PriceLimitState.NONE: PriceLimitStatus.NONE,
        PriceLimitState.UP: PriceLimitStatus.UP,
        PriceLimitState.DOWN: PriceLimitStatus.DOWN,
        # No declared gate means no price-limit status to report either.
        PriceLimitState.NOT_APPLICABLE: PriceLimitStatus.NONE,
    }[facts.price_limit_status]

    # The provenance record leads with the locating identity so a
    # persisted MarketState is auditable on its own.
    facts_basis: dict[str, object] = {
        "instrument_id": str(facts.instrument_id),
        "calendar_id": facts.calendar_id,
        "session_date": facts.session_date.isoformat(),
    }
    facts_basis.update(dict(facts.evidence))

    return MarketState(
        instrument_id=facts.instrument_id,
        timestamp=timestamp,
        open_price=open_price,
        price_tick=price_tick,
        is_suspended=facts.suspension_state is SuspensionState.SUSPENDED,
        # UNAVAILABLE is the only closing state; NOT_APPLICABLE never blocks.
        open_available=facts.opening_state is not OpeningState.UNAVAILABLE,
        buy_allowed=facts.buy_allowed is not DirectionalAvailability.NO,
        sell_allowed=facts.sell_allowed is not DirectionalAvailability.NO,
        price_limit_status=price_limit_status,
        status_reason=None,
        facts_basis=facts_basis,
    )
