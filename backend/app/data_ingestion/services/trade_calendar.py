"""Trading calendar fetching, yearly planning, and incremental synchronization."""

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
import structlog

from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.repositories.sync_checkpoint import DataSyncCheckpointRepository
from app.data_ingestion.repositories.trading_calendar import TradingCalendarRepository
from app.data_ingestion.request_pacing import tushare_request_pacer
from app.data_ingestion.schemas.trading_calendar import (
    DataSyncCheckpointState,
    TradeCalendarDateRange,
    TradeCalendarSyncResult,
    TradeCalendarUpsertResult,
    TradingCalendarDayInput,
)
from app.core.config import get_settings
from app.db.session import get_engine

if TYPE_CHECKING:
    from pandas import DataFrame


def fetch_trade_calendar(
    client: TushareClient,
    *,
    start_date: str,
    end_date: str,
    exchange: str = "",
) -> "DataFrame":
    """Fetch the Tushare trading calendar for the requested date range."""
    return client.pro.trade_cal(
        exchange=exchange,
        start_date=start_date,
        end_date=end_date,
    )


TRADE_CALENDAR_SYNC_KEY = "tushare.trade_calendar"
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = structlog.get_logger(__name__)


def sync_trade_calendar(
    client: TushareClient,
    *,
    exchange: str,
    initial_start_date: date,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> TradeCalendarSyncResult:
    """Incrementally synchronize one exchange's calendar in yearly requests."""
    settings = get_settings()
    effective_interval_ms = max(
        settings.ingestion_request_interval_ms,
        request_interval_ms or 0,
    )
    scope_key = f"exchange={exchange}"
    checkpoint = _load_checkpoint(TRADE_CALENDAR_SYNC_KEY, scope_key)
    ranges = plan_trade_calendar_year_ranges(
        checkpoint=checkpoint,
        initial_start_date=initial_start_date,
        as_of_date=as_of_date or datetime.now(SHANGHAI_TIMEZONE).date(),
    )
    logger.info(
        "trade_calendar_sync_planned",
        message=(
            f"交易日历采集计划：{exchange}，共 {len(ranges)} 个分段，"
            f"请求间隔 {effective_interval_ms} 毫秒。"
        ),
        exchange=exchange,
        ranges_planned=len(ranges),
        request_interval_ms=effective_interval_ms,
        checkpoint_synced_through=(
            _checkpoint_synced_through_date(checkpoint).isoformat()
            if checkpoint is not None
            else None
        ),
    )

    received = changed = unchanged = 0
    for date_range in ranges:
        range_fields = {
            "exchange": exchange,
            "start_date": date_range.start_date.isoformat(),
            "end_date": date_range.end_date.isoformat(),
        }
        logger.info(
            "trade_calendar_range_started",
            message=(
                f"开始采集 {exchange} 交易日历："
                f"{date_range.start_date.isoformat()} 至 {date_range.end_date.isoformat()}。"
            ),
            **range_fields,
        )
        try:
            tushare_request_pacer.wait_for_turn(effective_interval_ms)
            dataframe = fetch_trade_calendar(
                client,
                exchange=exchange,
                start_date=date_range.start_date.strftime("%Y%m%d"),
                end_date=date_range.end_date.strftime("%Y%m%d"),
            )
            days = normalize_trade_calendar(dataframe)
            _validate_trade_calendar_range(days, exchange=exchange, date_range=date_range)
            write_result, checkpoint = _commit_trade_calendar_range(
                days=days,
                sync_key=TRADE_CALENDAR_SYNC_KEY,
                scope_key=scope_key,
                expected_checkpoint=checkpoint,
                synced_through_date=date_range.end_date,
            )
        except Exception:
            logger.exception(
                "trade_calendar_range_failed",
                message=(
                    f"采集 {exchange} 交易日历失败："
                    f"{date_range.start_date.isoformat()} 至 {date_range.end_date.isoformat()}。"
                ),
                **range_fields,
            )
            raise
        received += write_result.received
        changed += write_result.changed
        unchanged += write_result.unchanged
        logger.info(
            "trade_calendar_range_succeeded",
            message=(
                f"完成采集 {exchange} 交易日历："
                f"{date_range.start_date.isoformat()} 至 {date_range.end_date.isoformat()}，"
                f"拉取 {write_result.received} 条，入库变更 {write_result.changed} 条，"
                f"未变更 {write_result.unchanged} 条，"
                f"游标已推进至 {checkpoint.cursor['synced_through_date']}。"
            ),
            **range_fields,
            received=write_result.received,
            changed=write_result.changed,
            unchanged=write_result.unchanged,
            synced_through_date=checkpoint.cursor["synced_through_date"],
        )

    return TradeCalendarSyncResult(
        ranges_completed=len(ranges),
        received=received,
        changed=changed,
        unchanged=unchanged,
        synced_through_date=(
            _checkpoint_synced_through_date(checkpoint) if checkpoint is not None else None
        ),
    )


def plan_trade_calendar_year_ranges(
    *,
    checkpoint: DataSyncCheckpointState | None,
    initial_start_date: date,
    as_of_date: date,
) -> list[TradeCalendarDateRange]:
    """Plan inclusive ranges that end at calendar-year boundaries."""
    start_date = (
        _checkpoint_synced_through_date(checkpoint) + timedelta(days=1)
        if checkpoint is not None
        else initial_start_date
    )
    if start_date > as_of_date:
        return []

    ranges: list[TradeCalendarDateRange] = []
    while start_date <= as_of_date:
        end_date = min(date(start_date.year, 12, 31), as_of_date)
        ranges.append(
            TradeCalendarDateRange(start_date=start_date, end_date=end_date)
        )
        start_date = end_date + timedelta(days=1)
    return ranges


def normalize_trade_calendar(dataframe: "DataFrame") -> list[TradingCalendarDayInput]:
    """Convert the Tushare trade_cal result into database-ready records."""
    return [
        TradingCalendarDayInput(
            exchange=str(row["exchange"]),
            calendar_date=_parse_date(row["cal_date"]),
            is_open=str(row["is_open"]) == "1",
            previous_trading_date=_parse_optional_date(row.get("pretrade_date")),
        )
        for row in dataframe.to_dict(orient="records")
    ]


def _parse_date(value: Any) -> date:
    return date.fromisoformat(str(value))


def _parse_optional_date(value: Any) -> date | None:
    if value is None or str(value).strip() in {"", "nan", "NaT"}:
        return None
    return _parse_date(value)


def _load_checkpoint(
    sync_key: str, scope_key: str
) -> DataSyncCheckpointState | None:
    with Session(get_engine()) as session:
        return DataSyncCheckpointRepository(session).get(sync_key, scope_key)


def _commit_trade_calendar_range(
    *,
    days: list[TradingCalendarDayInput],
    sync_key: str,
    scope_key: str,
    expected_checkpoint: DataSyncCheckpointState | None,
    synced_through_date: date,
) -> tuple[TradeCalendarUpsertResult, DataSyncCheckpointState]:
    with Session(get_engine()) as session:
        try:
            write_result = TradingCalendarRepository(session).upsert_days(days)
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=sync_key,
                scope_key=scope_key,
                cursor={"synced_through_date": synced_through_date.isoformat()},
                expected_version=(
                    expected_checkpoint.version
                    if expected_checkpoint is not None
                    else None
                ),
            )
            session.commit()
            return write_result, checkpoint
        except Exception:
            session.rollback()
            raise


def _checkpoint_synced_through_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("synced_through_date")
    if not isinstance(value, str):
        raise ValueError("trade calendar checkpoint has no synced_through_date")
    return date.fromisoformat(value)


def _validate_trade_calendar_range(
    days: list[TradingCalendarDayInput],
    *,
    exchange: str,
    date_range: TradeCalendarDateRange,
) -> None:
    expected_dates = {
        date_range.start_date + timedelta(days=offset)
        for offset in range((date_range.end_date - date_range.start_date).days + 1)
    }
    if any(day.exchange != exchange for day in days):
        raise ValueError("Tushare returned a trade calendar row for another exchange")
    if {day.calendar_date for day in days} != expected_dates:
        raise ValueError("Tushare returned an incomplete trade calendar range")
