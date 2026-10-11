"""Minimal shared calendar fact reader for corporate-action ingestion.

Preserves the existing direct definitions/fact queries. No snapshot, axis
registration, preflight, execution admission or mutable reader cache remains.
"""
from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.calendars.facts import CalendarDefinition, CalendarSessionFact, normalize_calendar_id
from app.calendars.models import CalendarDefinitionRecord, CalendarSessionFactRecord
from app.calendars.repository import _definition, _session
from app.data_contracts.errors import InvalidDataRequestError

class SqlCalendarFactProvider:
    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise InvalidDataRequestError("session must be a SQLAlchemy Session")
        self.session = session

    def definitions(self, calendar_id: str) -> tuple[CalendarDefinition, ...]:
        canonical = normalize_calendar_id(calendar_id)
        rows = self.session.scalars(
            select(CalendarDefinitionRecord).where(
                CalendarDefinitionRecord.calendar_id == canonical
            )
        ).all()
        return tuple(_definition(row) for row in rows)

    def fact_candidates(self, calendar_id: str, day: date) -> tuple[CalendarSessionFact, ...]:
        canonical = normalize_calendar_id(calendar_id)
        rows = self.session.scalars(
            select(CalendarSessionFactRecord).where(
                CalendarSessionFactRecord.calendar_id == canonical,
                CalendarSessionFactRecord.session_date == day,
            )
        ).all()
        return tuple(_session(row) for row in rows)

    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None:
        candidates = self.fact_candidates(calendar_id, day)
        return candidates[-1] if candidates else None

