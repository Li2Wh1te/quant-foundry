"""T+1-before-open-match settlement: calendar resolution and formal gate.

The first formal settlement category is ``t_plus_1_before_open_match``:
units bought in one trading session become sellable right before the
opening match of the instrument calendar's *next open session*.  The
next settlement session is resolved through an explicit trading-calendar
gateway — never by adding a natural day, never with a default exchange
calendar, and never postponed because the instrument itself is
suspended.

This module owns three concerns:

* the settlement-calendar gateway protocol and the adapter over the
  named-calendar axis provider;
* construction of immutable :class:`DeferredSettlementPlan` values that
  travel with every deferred buy fill into accounting;
* the formal-run admission gate: only ``t1_before_open_match`` (the rule
  package key of ``t_plus_1_before_open_match``) may enter a formal run;
  ``same_day``, legacy ``t_plus_1``, and unknown classes are blocked.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from app.backtesting.accounting import (
    DeferredSettlementPlan,
    SettlementBoundaryMissedError,
    SettlementError,
    SettlementPolicy,
)
from app.backtesting.calendar_axis import CalendarAxisDataProvider

__all__ = [
    "FORMAL_SETTLEMENT_RULE_CLASS",
    "CalendarAxisSettlementGateway",
    "SettlementBoundaryMissedError",
    "SettlementCalendarGateway",
    "SettlementCalendarUnresolvedError",
    "SettlementNextSessionMissingError",
    "SettlementRuleClass",
    "UnsupportedSettlementRuleError",
    "require_formal_settlement_policy",
    "settlement_plan_for_fill",
    "settlement_policy_for_rule_class",
]


class SettlementRuleClass(StrEnum):
    """Rule-package settlement classes and their engine policy mapping."""

    T1_BEFORE_OPEN_MATCH = "t1_before_open_match"
    SAME_DAY = "same_day"
    # Legacy in-memory fixture class; never acceptable in a formal run.
    T_PLUS_ONE = "t_plus_1"


FORMAL_SETTLEMENT_RULE_CLASS = SettlementRuleClass.T1_BEFORE_OPEN_MATCH
"""The only settlement class admitted by formal-run preflight."""

_SETTLEMENT_RULE_CLASS_TO_POLICY = {
    SettlementRuleClass.T1_BEFORE_OPEN_MATCH: (
        SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH
    ),
    SettlementRuleClass.SAME_DAY: SettlementPolicy.SAME_DAY,
    SettlementRuleClass.T_PLUS_ONE: SettlementPolicy.T_PLUS_ONE,
}

#: Bounded forward search for the next open session; long enough for any
#: real exchange holiday cluster, short enough to fail loudly instead of
#: scanning forever when calendar facts end.
_MAX_NEXT_SESSION_LOOKAHEAD_DAYS = 366


class UnsupportedSettlementRuleError(SettlementError):
    """A non-formal settlement class tried to enter a formal run."""

    code = "settlement_rule_unsupported"


class SettlementCalendarUnresolvedError(SettlementError):
    """Calendar facts needed to resolve the next session are missing."""

    code = "settlement_calendar_unresolved"


class SettlementNextSessionMissingError(SettlementError):
    """No open session exists within the bounded lookahead horizon."""

    code = "settlement_next_session_missing"


class SettlementCalendarGateway(Protocol):
    """Structural source of next-open-session answers for one calendar."""

    def next_open_session(self, calendar_id: str, after_session: date) -> date | None:
        """Return the first open session strictly after ``after_session``.

        Implementations return ``None`` only when the calendar has no
        facts beyond the horizon; missing intermediate facts must raise
        :class:`SettlementCalendarUnresolvedError` instead of being read
        as closed days.
        """
        ...


class CalendarAxisSettlementGateway:
    """Adapter resolving next sessions from the named-calendar axis.

    The adapter scans day by day over the explicit per-day session facts
    of exactly the requested ``calendar_id``.  A missing fact is a hard
    error: silence would silently turn an unresolved calendar into a
    sequence of closed days.
    """

    def __init__(self, provider: CalendarAxisDataProvider) -> None:
        self._provider = provider

    def next_open_session(
        self, calendar_id: str, after_session: date
    ) -> date | None:
        horizon_end = after_session + timedelta(
            days=_MAX_NEXT_SESSION_LOOKAHEAD_DAYS
        )
        day = after_session + timedelta(days=1)
        while day <= horizon_end:
            fact = self._provider.fact(calendar_id, day)
            if fact is None:
                raise SettlementCalendarUnresolvedError(
                    f"no session fact exists for calendar {calendar_id!r} "
                    f"on {day.isoformat()}; the next settlement session "
                    "cannot be resolved",
                    details={
                        "calendar_id": calendar_id,
                        "missing_fact_date": day.isoformat(),
                    },
                )
            if fact.is_open:
                return day
            day += timedelta(days=1)
        raise SettlementNextSessionMissingError(
            f"calendar {calendar_id!r} has no open session within "
            f"{_MAX_NEXT_SESSION_LOOKAHEAD_DAYS} days after "
            f"{after_session.isoformat()}",
            details={
                "calendar_id": calendar_id,
                "after_session": after_session.isoformat(),
            },
        )


def settlement_plan_for_fill(
    gateway: SettlementCalendarGateway,
    *,
    calendar_id: str,
    trade_session: date,
) -> DeferredSettlementPlan:
    """Resolve the immutable settlement plan for one buy fill session.

    Instrument suspension never shifts the answer: the settlement date is
    a property of the exchange calendar alone.
    """

    if not isinstance(trade_session, date) or isinstance(trade_session, datetime):
        raise SettlementError("trade_session must be a calendar date")
    next_session = gateway.next_open_session(calendar_id, trade_session)
    if next_session is None:
        raise SettlementNextSessionMissingError(
            f"calendar {calendar_id!r} returned no next open session after "
            f"{trade_session.isoformat()}",
            details={
                "calendar_id": calendar_id,
                "trade_session": trade_session.isoformat(),
            },
        )
    return DeferredSettlementPlan(
        calendar_id=calendar_id,
        trade_session=trade_session,
        settlement_session=next_session,
    )


def settlement_policy_for_rule_class(
    rule_class: str | SettlementRuleClass,
) -> SettlementPolicy | None:
    """Map a rule-package settlement class onto its engine policy.

    Unknown classes return ``None`` so preflight can report them as
    unknown instead of silently choosing a default.
    """

    try:
        resolved = SettlementRuleClass(rule_class)
    except ValueError:
        return None
    return _SETTLEMENT_RULE_CLASS_TO_POLICY[resolved]


def require_formal_settlement_policy(policy: SettlementPolicy) -> None:
    """Block any settlement policy other than T+1-before-open-match.

    The legacy ``t_plus_1`` member stays constructible for old in-memory
    fixtures but can never pass this gate, and neither can ``same_day``
    or any future category.
    """

    if policy is not SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH:
        raise UnsupportedSettlementRuleError(
            f"formal runs accept only "
            f"'{FORMAL_SETTLEMENT_RULE_CLASS.value}' settlement, got "
            f"'{policy.value}'",
            details={
                "formal_rule_class": FORMAL_SETTLEMENT_RULE_CLASS.value,
                "requested_policy": policy.value,
            },
        )
