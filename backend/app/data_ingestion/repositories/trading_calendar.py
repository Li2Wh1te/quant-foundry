"""PostgreSQL persistence for trading calendar data."""

from collections.abc import Iterable

from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_ingestion.models.trading_calendar import TradingCalendarDay
from app.data_ingestion.schemas.trading_calendar import (
    TradeCalendarUpsertResult,
    TradingCalendarDayInput,
)


class TradingCalendarRepository:
    """Write normalized trading calendar records without managing transactions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_days(
        self, records: Iterable[TradingCalendarDayInput]
    ) -> TradeCalendarUpsertResult:
        """Insert new calendar days and update only materially changed records."""
        days = list(records)
        self._reject_duplicate_keys(days)
        if not days:
            return TradeCalendarUpsertResult(received=0, changed=0, unchanged=0)

        table = TradingCalendarDay.__table__
        values = [
            {
                "exchange": day.exchange,
                "calendar_date": day.calendar_date,
                "is_open": day.is_open,
                "previous_trading_date": day.previous_trading_date,
            }
            for day in days
        ]
        statement = insert(table).values(values)
        excluded = statement.excluded
        changed_fields = or_(
            table.c.is_open.is_distinct_from(excluded.is_open),
            table.c.previous_trading_date.is_distinct_from(
                excluded.previous_trading_date
            ),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.exchange, table.c.calendar_date],
            set_={
                "is_open": excluded.is_open,
                "previous_trading_date": excluded.previous_trading_date,
                "updated_at": func.now(),
            },
            where=changed_fields,
        ).returning(table.c.exchange, table.c.calendar_date)
        changed = len(self.session.execute(statement).all())
        return TradeCalendarUpsertResult(
            received=len(days),
            changed=changed,
            unchanged=len(days) - changed,
        )

    @staticmethod
    def _reject_duplicate_keys(days: list[TradingCalendarDayInput]) -> None:
        seen: set[tuple[str, object]] = set()
        for day in days:
            key = (day.exchange, day.calendar_date)
            if key in seen:
                raise ValueError(
                    "trading calendar input contains duplicate exchange and calendar_date"
                )
            seen.add(key)
