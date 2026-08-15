"""Authenticated admin APIs for inspecting persisted ingestion data."""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.data_ingestion.repositories.trading_calendar_query import (
    TradingCalendarQueryRepository,
)
from app.db.session import get_db_session


router = APIRouter(prefix="/api/admin/data-collections", tags=["admin-data"])


class TradingCalendarDayResponse(BaseModel):
    """One persisted calendar day for the database table."""

    model_config = ConfigDict(from_attributes=True)

    exchange: str
    calendar_date: date
    is_open: bool
    previous_trading_date: date | None
    updated_at: datetime


class TradingCalendarPageResponse(BaseModel):
    """A pagination envelope that preserves exact filter totals."""

    items: list[TradingCalendarDayResponse]
    total: int
    limit: int
    offset: int


class TradingCalendarOverviewResponse(BaseModel):
    """Coverage and checkpoint information displayed as collection status."""

    total_records: int
    exchange_count: int
    open_day_count: int
    start_date: date | None
    end_date: date | None
    last_updated_at: datetime | None
    checkpoints: dict[str, date]


@router.get("/trading-calendar", response_model=TradingCalendarPageResponse)
def list_trading_calendar_days(
    session: Annotated[Session, Depends(get_db_session)],
    exchange: str | None = Query(default=None, min_length=1, max_length=16),
    is_open: bool | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TradingCalendarPageResponse:
    """List all stored calendar rows with server-side filters and pagination."""
    items, total = TradingCalendarQueryRepository(session).list_days(
        exchange=exchange,
        is_open=is_open,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    return TradingCalendarPageResponse(
        items=[TradingCalendarDayResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/trading-calendar/overview", response_model=TradingCalendarOverviewResponse
)
def get_trading_calendar_overview(
    session: Annotated[Session, Depends(get_db_session)],
) -> TradingCalendarOverviewResponse:
    """Return current database coverage and every committed exchange checkpoint."""
    overview = TradingCalendarQueryRepository(session).overview()
    return TradingCalendarOverviewResponse(
        total_records=overview.total_records,
        exchange_count=overview.exchange_count,
        open_day_count=overview.open_day_count,
        start_date=overview.start_date,
        end_date=overview.end_date,
        last_updated_at=overview.last_updated_at,
        checkpoints=overview.checkpoints,
    )
