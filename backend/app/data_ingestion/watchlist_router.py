"""Authenticated personal ETF watchlists and explicit daily-price semantics."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal, require_api_token
from app.data_ingestion.repositories.etf_detail import EtfDetailQueryRepository
from app.data_ingestion.repositories.etf_watchlist import EtfWatchlistRepository
from app.db.session import get_db_session

router = APIRouter(prefix="/api/admin/etf-watchlist", tags=["admin-data"])
Code = Annotated[str, Path(pattern=r"^\d{6}\.(SH|SZ)$")]


class WatchlistItem(BaseModel):
    ts_code: str
    name: str | None
    exchange: str | None
    created_at: datetime
    trade_date: date | None
    close: Decimal | None
    pre_close: Decimal | None
    change: Decimal | None
    pct_chg: Decimal | None
    price_basis: Literal["raw_daily_close"] = "raw_daily_close"
    message: str


class WatchlistPage(BaseModel):
    items: list[WatchlistItem]
    limit: int
    offset: int
    has_more: bool


class WatchlistMutation(BaseModel):
    changed: bool
    message: str


def present_item(row) -> WatchlistItem:
    """Unknown/invalid source facts stay unknown; zero change stays zero."""
    close = row["close"]
    valid_close = close is not None and close.is_finite() and close > 0
    pre_close = row["pre_close"]
    valid_previous = pre_close is not None and pre_close.is_finite() and pre_close > 0
    change = row["change"]
    pct = row["pct_chg"]
    valid_change = valid_close and valid_previous and all(
        value is not None and value.is_finite() for value in (change, pct)
    )
    if row["trade_date"] is None:
        message = "尚未采集该 ETF 的日线行情，请检查采集任务。"
    elif not valid_close:
        message = f"{row['trade_date']} 的收盘价缺失或无效，请检查数据源。"
    elif not valid_change:
        message = f"显示 {row['trade_date']} 的收盘价；数据源涨跌字段缺失，待日线重新采集。"
    else:
        message = f"显示 {row['trade_date']} 的收盘价与数据源涨跌数据，非实时行情。"
    return WatchlistItem(
        ts_code=row["ts_code"], name=row["csname"] or row["extname"] or row["cname"],
        exchange=row["exchange"], created_at=row["created_at"], trade_date=row["trade_date"],
        close=close if valid_close else None,
        pre_close=pre_close if valid_previous else None,
        change=change if valid_change else None, pct_chg=pct if valid_change else None,
        message=message,
    )


@router.get("", response_model=WatchlistPage)
def list_watchlist(
    session: Annotated[Session, Depends(get_db_session)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_api_token)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rows = EtfWatchlistRepository(session, principal.owner_scope).list_entries(limit=limit + 1, offset=offset)
    return WatchlistPage(items=[present_item(row) for row in rows[:limit]],
                         limit=limit, offset=offset, has_more=len(rows) > limit)


@router.put("/{ts_code}", response_model=WatchlistMutation)
def add_watchlist(
    ts_code: Code,
    session: Annotated[Session, Depends(get_db_session)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_api_token)],
):
    if EtfDetailQueryRepository(session).get_code(ts_code) is None:
        raise HTTPException(status_code=404, detail="该 ETF 基础资料不存在，无法添加自选。")
    changed = EtfWatchlistRepository(session, principal.owner_scope).add(ts_code)
    session.commit()
    return WatchlistMutation(changed=changed, message=f"ETF {ts_code} 已添加自选。" if changed else f"ETF {ts_code} 已在自选中。")


@router.delete("/{ts_code}", response_model=WatchlistMutation)
def remove_watchlist(
    ts_code: Code,
    session: Annotated[Session, Depends(get_db_session)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_api_token)],
):
    changed = EtfWatchlistRepository(session, principal.owner_scope).remove(ts_code)
    session.commit()
    return WatchlistMutation(changed=changed, message=f"ETF {ts_code} 已移出自选。" if changed else f"ETF {ts_code} 不在自选中。")
