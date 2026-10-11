"""Account catalogue lifecycle and lossless fee serialization."""
from enum import StrEnum
from typing import Any
from app.backtest_service.accounts.fee_types import FeeRule, FeeScheduleSnapshot

class AccountProfileStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    RETIRED = "retired"

def _fee_rule_payload(rule: FeeRule) -> dict[str, Any]:
    """Build the complete serializable representation of one fee rule."""

    return {
        "key": rule.key,
        "category": rule.category,
        "side": rule.side,
        "rate": rule.rate,
        "minimum": rule.minimum,
        "fixed_amount": rule.fixed_amount,
        "rounding_level": rule.rounding_level,
        "rounding_scope": rule.rounding_scope,
        "rounding_mode": rule.rounding_mode,
        "rounding_precision": rule.rounding_precision,
        "applicability": rule.applicability,
    }


def _fee_schedule_payload(schedule: FeeScheduleSnapshot) -> dict[str, Any]:
    """Build the complete serializable representation of a fee snapshot."""

    return {
        "key": schedule.key,
        "version": schedule.version,
        "fee_rules": [_fee_rule_payload(rule) for rule in schedule.fee_rules],
        "metadata": schedule.metadata,
    }
