"""Read models for the operator-facing trading calendar data page."""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.data_ingestion.constants import TRADE_CALENDAR_SYNC_KEY
from app.data_ingestion.models.sync_checkpoint import DataSyncCheckpoint
from app.data_ingestion.models.trading_calendar import TradingCalendarDay


@dataclass(frozen=True)
class TradingCalendarOverview:
    """Aggregated values shown above the full trading-calendar data table."""

    total_records: int
    exchange_count: int
    open_day_count: int
    start_date: date | None
    end_date: date | None
    last_updated_at: datetime | None
    checkpoints: dict[str, date]


class TradingCalendarQueryRepository:
    """Query the persisted calendar without coupling read paths to ingestion jobs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_days(
        self,
        *,
        exchange: str | None,
        is_open: bool | None,
        start_date: date | None,
        end_date: date | None,
        limit: int,
        offset: int,
    ) -> tuple[list[TradingCalendarDay], int]:
        """Return one stable, filterable page plus the exact matching record count."""
        filters = self._filters(
            exchange=exchange,
            is_open=is_open,
            start_date=start_date,
            end_date=end_date,
        )
        items = self.session.scalars(
            select(TradingCalendarDay)
            .where(*filters)
            .order_by(
                TradingCalendarDay.calendar_date.desc(),
                TradingCalendarDay.exchange.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        total = self.session.scalar(
            select(func.count()).select_from(TradingCalendarDay).where(*filters)
        )
        return items, int(total or 0)

    def overview(self) -> TradingCalendarOverview:
        """Return database coverage and committed cursors for operator status."""
        summary = self.session.execute(
            select(
                func.count(TradingCalendarDay.calendar_date),
                func.count(func.distinct(TradingCalendarDay.exchange)),
                func.count(TradingCalendarDay.calendar_date).filter(
                    TradingCalendarDay.is_open.is_(True)
                ),
                func.min(TradingCalendarDay.calendar_date),
                func.max(TradingCalendarDay.calendar_date),
                func.max(TradingCalendarDay.updated_at),
            )
        ).one()
        checkpoints: dict[str, date] = {}
        persisted_checkpoints = self.session.scalars(
            select(DataSyncCheckpoint).where(
                DataSyncCheckpoint.sync_key == TRADE_CALENDAR_SYNC_KEY
            )
        ).all()
        for checkpoint in persisted_checkpoints:
            scope = checkpoint.scope_key
            exchange = (
                scope.removeprefix("calendar_id=")
                if scope.startswith("calendar_id=")
                else scope.removeprefix("exchange=")
            )
            synced_through_date = checkpoint.cursor.get("synced_through_date")
            if exchange and isinstance(synced_through_date, str):
                try:
                    checkpoints[exchange] = date.fromisoformat(synced_through_date)
                except ValueError:
                    # A malformed legacy cursor must not make the read-only page fail.
                    continue
        return TradingCalendarOverview(
            total_records=int(summary[0] or 0),
            exchange_count=int(summary[1] or 0),
            open_day_count=int(summary[2] or 0),
            start_date=summary[3],
            end_date=summary[4],
            last_updated_at=summary[5],
            checkpoints=checkpoints,
        )

    @staticmethod
    def _filters(
        *,
        exchange: str | None,
        is_open: bool | None,
        start_date: date | None,
        end_date: date | None,
    ) -> list[object]:
        """Build shared predicates so count and page queries cannot drift apart."""
        filters: list[object] = []
        if exchange is not None:
            filters.append(TradingCalendarDay.exchange == exchange)
        if is_open is not None:
            filters.append(TradingCalendarDay.is_open.is_(is_open))
        if start_date is not None:
            filters.append(TradingCalendarDay.calendar_date >= start_date)
        if end_date is not None:
            filters.append(TradingCalendarDay.calendar_date <= end_date)
        return filters
