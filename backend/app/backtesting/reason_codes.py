"""Frozen reason-code groups shared by decision interpretation and matching.

Every machine-readable outcome carries the uniform structure ``stage``,
``code``, and ``details`` (see :class:`StructuredReason`).  The three
enumerations below are closed vocabularies: the decision interpreter only
emits :class:`DecisionReasonCode`, the bar-open matcher only emits
:class:`MatchReasonCode`, and slippage models only emit
:class:`SlippageReasonCode`.

The codes are part of the auditable run contract.  Renaming a member would
rewrite historical results, so new situations must add new members instead
of reusing or reinterpreting existing ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class ResultStage(StrEnum):
    """Pipeline stage that produced a structured reason."""

    DECISION = "decision"
    MATCHING = "matching"
    SLIPPAGE = "slippage"


class DecisionReasonCode(StrEnum):
    """Reason codes emitted by the decision interpreter stage.

    ``TARGET_SCOPE_INCOMPLETE`` deliberately does not exist here: an
    incomplete target scope must be rejected upstream, before a decision
    enters the interpreter.
    """

    INVALID_WEIGHT = "INVALID_WEIGHT"
    WEIGHT_SUM_EXCEEDED = "WEIGHT_SUM_EXCEEDED"
    DECISION_SNAPSHOT_INCOMPLETE = "DECISION_SNAPSHOT_INCOMPLETE"
    DECISION_SNAPSHOT_STALE = "DECISION_SNAPSHOT_STALE"
    DECISION_SNAPSHOT_CONFLICTED = "DECISION_SNAPSHOT_CONFLICTED"
    DECISION_SNAPSHOT_INVALID = "DECISION_SNAPSHOT_INVALID"
    INSTRUMENT_RULE_MISSING = "INSTRUMENT_RULE_MISSING"
    TARGET_QUANTITY_NOT_ORDERABLE = "TARGET_QUANTITY_NOT_ORDERABLE"


class MatchReasonCode(StrEnum):
    """Reason codes emitted by the bar-open matching stage."""

    ORDER_TYPE_NOT_SUPPORTED = "ORDER_TYPE_NOT_SUPPORTED"
    ORDER_NOT_YET_VALID = "ORDER_NOT_YET_VALID"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_QUANTITY_PRECISION_INVALID = "ORDER_QUANTITY_PRECISION_INVALID"
    ORDER_QUANTITY_BELOW_MINIMUM = "ORDER_QUANTITY_BELOW_MINIMUM"
    ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT = "ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT"
    ODD_LOT_NOT_ALLOWED = "ODD_LOT_NOT_ALLOWED"
    AVAILABLE_QUANTITY_PRECISION_INVALID = "AVAILABLE_QUANTITY_PRECISION_INVALID"
    AVAILABLE_QUANTITY_BELOW_MINIMUM = "AVAILABLE_QUANTITY_BELOW_MINIMUM"
    AVAILABLE_QUANTITY_NOT_MULTIPLE_OF_LOT = (
        "AVAILABLE_QUANTITY_NOT_MULTIPLE_OF_LOT"
    )
    AVAILABLE_ODD_LOT_NOT_ALLOWED = "AVAILABLE_ODD_LOT_NOT_ALLOWED"
    MARKET_STATE_MISSING = "MARKET_STATE_MISSING"
    OPEN_UNAVAILABLE = "OPEN_UNAVAILABLE"
    INSTRUMENT_SUSPENDED = "INSTRUMENT_SUSPENDED"
    BUY_UNAVAILABLE_AT_PRICE_LIMIT = "BUY_UNAVAILABLE_AT_PRICE_LIMIT"
    SELL_UNAVAILABLE_AT_PRICE_LIMIT = "SELL_UNAVAILABLE_AT_PRICE_LIMIT"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_AVAILABLE_QUANTITY = "INSUFFICIENT_AVAILABLE_QUANTITY"
    # A partially filled order never rolls its remainder over: the
    # unfilled part expires after the one-shot match under its own code.
    EXPIRED_AFTER_PARTIAL_FILL = "expired_after_partial_fill"
    NEGATIVE_NET_PROCEEDS = "NEGATIVE_NET_PROCEEDS"
    CASH_ALLOCATION_PRO_RATA_ROUNDED_TO_ZERO = (
        "CASH_ALLOCATION_PRO_RATA_ROUNDED_TO_ZERO"
    )
    CASH_ALLOCATION_LOT_REDUCTION_TO_ZERO = (
        "CASH_ALLOCATION_LOT_REDUCTION_TO_ZERO"
    )
    ODD_LOT_LOT_SIZE_EXEMPTION_MISSING = "ODD_LOT_LOT_SIZE_EXEMPTION_MISSING"
    COST_QUOTE_UNAVAILABLE = "COST_QUOTE_UNAVAILABLE"


class SlippageReasonCode(StrEnum):
    """Reason codes attached to slippage calculation failures."""

    INVALID_SLIPPAGE_CONFIGURATION = "INVALID_SLIPPAGE_CONFIGURATION"
    INVALID_REFERENCE_PRICE = "INVALID_REFERENCE_PRICE"
    INVALID_PRICE_TICK = "INVALID_PRICE_TICK"
    NON_POSITIVE_EXECUTION_PRICE = "NON_POSITIVE_EXECUTION_PRICE"


class InterpretationAuditCode(StrEnum):
    """Non-rejecting audit/information codes of the interpretation stage.

    These are deliberately outside the frozen ``DecisionReasonCode``
    vocabulary: they never act as a decision's rejection reason, they only
    record auditable facts (omitted positions, excluded corporate-action
    cash) or protocol-level anomalies surfaced through the dedicated
    ``protocol_reason`` field of the interpretation result.
    """

    OMITTED_POSITION_ZERO_TARGET = "OMITTED_POSITION_ZERO_TARGET"
    CORPORATE_ACTION_CASH_NOT_CREDITED = "CORPORATE_ACTION_CASH_NOT_CREDITED"
    UNKNOWN_DECISION_MODE = "UNKNOWN_DECISION_MODE"
    INVALID_TARGETS_PAYLOAD = "INVALID_TARGETS_PAYLOAD"


@dataclass(frozen=True, slots=True)
class StructuredReason:
    """One structured outcome reason: stage, machine code, and details.

    ``details`` is deep-frozen into an immutable mapping so reasons can be
    embedded in results and event payloads without later mutation.
    """

    stage: ResultStage | str
    code: str
    details: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", ResultStage(self.stage))
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("code must be non-blank text")
        object.__setattr__(
            self,
            "details",
            MappingProxyType(dict(self.details)),
        )

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly projection used by results and event payloads."""

        return {
            "stage": ResultStage(self.stage).value,
            "code": self.code,
            "details": dict(self.details),
        }


__all__ = [
    "DecisionReasonCode",
    "InterpretationAuditCode",
    "MatchReasonCode",
    "ResultStage",
    "SlippageReasonCode",
    "StructuredReason",
]
