"""Shared corporate-action ingestion next-session query; no settlement accounting."""
from __future__ import annotations
from datetime import date, datetime, timedelta
from app.calendars.facts import NamedCalendarFactProvider
from app.data_contracts.errors import CalendarContractError
_MAX_NEXT_SESSION_LOOKAHEAD_DAYS = 366

class SettlementCalendarUnresolvedError(CalendarContractError):
    """Calendar facts needed to resolve the next session are missing."""

    code = "settlement_calendar_unresolved"


class SettlementNextSessionMissingError(CalendarContractError):
    """No open session exists within the bounded lookahead horizon."""

    code = "settlement_next_session_missing"


class NextOpenSessionGateway:
    """Adapter resolving next sessions from the named-calendar axis.

    The adapter scans day by day over the explicit per-day session facts
    of exactly the requested ``calendar_id``.  A missing fact is a hard
    error: silence would silently turn an unresolved calendar into a
    sequence of closed days.
    """

    def __init__(self, provider: NamedCalendarFactProvider) -> None:
        self._provider = provider

    def next_open_session(
        self, calendar_id: str, after_session: date
    ) -> date | None:
        horizon_end = after_session + timedelta(
            days=_MAX_NEXT_SESSION_LOOKAHEAD_DAYS
        )
        definitions = list(self._provider.definitions(calendar_id))
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
            # Anomalous providers must not turn foreign-calendar or
            # wrong-date facts into settlement days.  The fact's
            # definition version must also resolve to *exactly one*
            # registered definition of this calendar that is valid on the
            # fact's date — the same rule the calendar axis applies.
            if (
                fact.calendar_id != calendar_id
                or fact.session_date != day
            ):
                raise SettlementCalendarUnresolvedError(
                    "the calendar provider returned an inconsistent "
                    "session fact; the next settlement session cannot be "
                    "resolved",
                    details={
                        "calendar_id": calendar_id,
                        "requested_fact_date": day.isoformat(),
                        "fact_calendar_id": fact.calendar_id,
                        "fact_session_date": fact.session_date.isoformat(),
                        "fact_definition_version": fact.definition_version,
                    },
                )
            applicable = [
                definition
                for definition in definitions
                if definition.calendar_id == calendar_id
                and definition.definition_version == fact.definition_version
                and definition.applies_to(fact.session_date)
            ]
            if len(applicable) != 1:
                raise SettlementCalendarUnresolvedError(
                    "the session fact's definition version does not "
                    "resolve to exactly one definition valid on the fact "
                    "date; the next settlement session cannot be resolved",
                    details={
                        "calendar_id": calendar_id,
                        "fact_session_date": fact.session_date.isoformat(),
                        "fact_definition_version": fact.definition_version,
                        "applicable_definition_count": len(applicable),
                    },
                )
            if fact.is_open:
                # An open day without any effective trading session cannot
                # host an opening match: the calendar axis would report it
                # as unresolved_session, so it must never become a
                # settlement day either.
                definition = applicable[0]
                effective_sessions = (
                    fact.sessions_override
                    if fact.sessions_override is not None
                    else definition.default_sessions
                )
                if not effective_sessions:
                    raise SettlementCalendarUnresolvedError(
                        "the open session fact resolves to no trading "
                        "sessions; the next settlement session cannot be "
                        "resolved",
                        details={
                            "calendar_id": calendar_id,
                            "fact_session_date": fact.session_date.isoformat(),
                            "fact_definition_version": (
                                fact.definition_version
                            ),
                            "has_sessions_override": (
                                fact.sessions_override is not None
                            ),
                        },
                    )
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

    def calendar_version_for(
        self, calendar_id: str, session_date: date
    ) -> str:
        """Return the definition version governing ``session_date``.

        The same consistency rules as the session scan apply: the fact
        must belong to the requested calendar and date, and its
        definition version must resolve to exactly one registered
        definition valid on that date.
        """

        fact = self._provider.fact(calendar_id, session_date)
        if (
            fact is None
            or fact.calendar_id != calendar_id
            or fact.session_date != session_date
        ):
            raise SettlementCalendarUnresolvedError(
                "no consistent session fact exists for the requested "
                "calendar and date; the calendar version cannot be "
                "resolved",
                details={
                    "calendar_id": calendar_id,
                    "session_date": session_date.isoformat(),
                },
            )
        applicable = [
            definition
            for definition in self._provider.definitions(calendar_id)
            if definition.calendar_id == calendar_id
            and definition.definition_version == fact.definition_version
            and definition.applies_to(fact.session_date)
        ]
        if len(applicable) != 1:
            raise SettlementCalendarUnresolvedError(
                "the session fact's definition version does not resolve "
                "to exactly one definition valid on the date; the "
                "calendar version cannot be resolved",
                details={
                    "calendar_id": calendar_id,
                    "session_date": session_date.isoformat(),
                    "fact_definition_version": fact.definition_version,
                    "applicable_definition_count": len(applicable),
                },
            )
        return applicable[0].definition_version
