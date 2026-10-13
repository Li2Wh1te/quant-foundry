"""Preserved shared native catalogue and explicit retired market read URLs.

Collection writers/services remain intact. Current market reads use the sole
market service. Legacy external readers receive an authenticated migration error.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.data_ingestion.repositories.etf_query import EtfQueryRepository
from app.db.session import get_db_session

router = APIRouter(prefix="/api/admin/data-collections", tags=["admin-data"])

class EtfCodeResponse(BaseModel):
    """One source-scoped ETF code displayed in the admin data table."""

    model_config = ConfigDict(from_attributes=True)

    # Return the persisted stable identity, never infer one from a mutable code.
    # This current catalogue label does not replace point-in-time preflight.
    instrument_id: UUID = Field(validation_alias="etf_id")
    ts_code: str
    csname: str | None
    extname: str | None
    cname: str | None
    index_code: str | None
    index_name: str | None
    list_date: date | None
    list_status: str
    exchange: str
    mgr_name: str | None
    mgt_fee: Decimal | None
    etf_type: str | None
    updated_at: datetime


class EtfPageResponse(BaseModel):
    """A paginated ETF result that preserves the matching total."""

    items: list[EtfCodeResponse]
    total: int
    limit: int
    offset: int


def _retired_market_read() -> NoReturn:
    raise HTTPException(status_code=410, detail={
        "code": "LEGACY_MARKET_RETIRED",
        "message": "市场读取已迁移到当前正式服务；旧入口不读取或回退采集行情。",
        "replacement": "/api/admin/market",
    })


@router.get("/trading-calendar", status_code=410)
@router.get("/trading-calendar/overview", status_code=410)
@router.get("/etfs/overview", status_code=410)
def retired_market_overview():
    _retired_market_read()


@router.get("/trading-calendar/{exchange}/{calendar_date}", status_code=410)
def retired_calendar_day(exchange: str, calendar_date: str):
    _retired_market_read()


@router.get("/etfs", response_model=EtfPageResponse)
def list_etfs(
    session: Annotated[Session, Depends(get_db_session)],
    keyword: str | None = Query(default=None, min_length=1, max_length=128),
    exchange: str | None = Query(default=None, min_length=1, max_length=16),
    list_status: str | None = Query(default=None, min_length=1, max_length=8),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    instrument_id: UUID | None = None,
) -> EtfPageResponse:
    """Shared native catalogue lookup for watchlist selection and diagnostics."""
    items, total = EtfQueryRepository(session).list_codes(
        keyword=keyword,
        exchange=exchange,
        list_status=list_status,
        limit=limit,
        offset=offset,
        instrument_id=instrument_id,
    )
    return EtfPageResponse(
        items=[EtfCodeResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/etfs/{ts_code}", status_code=410)
@router.get("/etfs/{ts_code}/daily-bars", status_code=410)
@router.get("/etfs/{ts_code}/adjustment-factors", status_code=410)
def retired_etf_market_read(ts_code: str):
    _retired_market_read()
