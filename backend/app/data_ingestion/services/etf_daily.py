"""ETF daily-bar retrieval and incremental whole-market synchronization."""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import (
    ETF_DAILY_FULL_SYNC_KEY,
    ETF_DAILY_INCREMENTAL_SYNC_KEY,
)
from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
from app.data_ingestion.repositories.sync_checkpoint import DataSyncCheckpointRepository
from app.data_ingestion.repositories.trading_calendar import TradingCalendarRepository
from app.data_ingestion.request_pacing import tushare_request_pacer
from app.data_ingestion.schemas.etf_daily import (
    EtfDailyBarInput,
    EtfDailyBarUpsertResult,
    EtfDailySyncResult,
)
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.db.session import get_engine

if TYPE_CHECKING:
    from pandas import DataFrame


# Keep this aligned with Tushare's documented single-ETF ``fund_daily`` example.
ETF_DAILY_FIELDS = "trade_date,open,high,low,close,vol,amount"
# The whole-market request needs the provider code to form the database key.
ETF_DAILY_MARKET_FIELDS = f"ts_code,{ETF_DAILY_FIELDS}"
ETF_DAILY_SCOPE_KEY = "market=CN"
TUSHARE_SOURCE = "tushare"
MAX_ETF_DAILY_ROWS = 5_000
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = structlog.get_logger(__name__)


def fetch_etf_daily(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> "DataFrame":
    """Fetch daily ETF data for one code and compact date range.

    This small helper preserves the official Tushare example contract for manual
    investigation and targeted repair. The scheduled synchronization uses the
    date-wide helper below so it needs only one normal request per trading day.
    """
    return client.pro.fund_daily(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fields=ETF_DAILY_FIELDS,
    )


def fetch_etf_daily_for_trade_date(
    client: TushareClient, *, trade_date: str
) -> "DataFrame":
    """Fetch every ETF daily bar reported by Tushare for one trade date."""
    return client.pro.fund_daily(
        trade_date=trade_date, fields=ETF_DAILY_MARKET_FIELDS
    )


def sync_etf_daily_incremental(
    client: TushareClient,
    *,
    calendar_exchange: str,
    initial_start_date: date,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> EtfDailySyncResult:
    """Synchronize newly completed whole-market ETF sessions.

    ``as_of_date`` is inclusive and represents the last date known to have
    completed trading. Omitting it deliberately uses yesterday in Shanghai time,
    preventing a manually triggered task from treating an in-progress session as
    an end-of-day bar.
    """
    completed_through_date = as_of_date or (
        datetime.now(SHANGHAI_TIMEZONE).date() - timedelta(days=1)
    )
    checkpoint = _load_checkpoint(ETF_DAILY_INCREMENTAL_SYNC_KEY)
    start_date = (
        _checkpoint_synced_through_date(checkpoint) + timedelta(days=1)
        if checkpoint is not None
        else initial_start_date
    )
    return _sync_etf_daily_sessions(
        client,
        calendar_exchange=calendar_exchange,
        start_date=start_date,
        target_through_date=completed_through_date,
        checkpoint=checkpoint,
        sync_key=ETF_DAILY_INCREMENTAL_SYNC_KEY,
        event_prefix="etf_daily_incremental_sync",
        task_label="ETF 日线增量",
        request_interval_ms=request_interval_ms,
    )


def sync_etf_daily_full(
    client: TushareClient,
    *,
    calendar_exchange: str,
    initial_start_date: date,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> EtfDailySyncResult:
    """Run or resume one full historical ETF daily-bar verification cycle.

    A new cycle freezes its terminal date before the first request. A failed run
    resumes from its own checkpoint and keeps that terminal date, so it cannot
    chase newly completed trading days indefinitely.
    """
    completed_through_date = as_of_date or (
        datetime.now(SHANGHAI_TIMEZONE).date() - timedelta(days=1)
    )
    checkpoint = _load_checkpoint(ETF_DAILY_FULL_SYNC_KEY)
    if checkpoint is not None and not _full_cycle_is_complete(checkpoint):
        target_through_date = _full_checkpoint_target_date(checkpoint)
        start_date = _checkpoint_synced_through_date(checkpoint) + timedelta(days=1)
    else:
        target_through_date = completed_through_date
        start_date = initial_start_date
        checkpoint = _initialize_full_cycle(
            expected_checkpoint=checkpoint,
            initial_start_date=initial_start_date,
            target_through_date=target_through_date,
        )
    return _sync_etf_daily_sessions(
        client,
        calendar_exchange=calendar_exchange,
        start_date=start_date,
        target_through_date=target_through_date,
        checkpoint=checkpoint,
        sync_key=ETF_DAILY_FULL_SYNC_KEY,
        event_prefix="etf_daily_full_sync",
        task_label="ETF 日线全量",
        request_interval_ms=request_interval_ms,
        full_cycle_target_date=target_through_date,
    )


def sync_etf_daily(
    client: TushareClient,
    *,
    calendar_exchange: str,
    initial_start_date: date,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> EtfDailySyncResult:
    """Backward-compatible alias for incremental ETF daily synchronization."""
    return sync_etf_daily_incremental(
        client,
        calendar_exchange=calendar_exchange,
        initial_start_date=initial_start_date,
        request_interval_ms=request_interval_ms,
        as_of_date=as_of_date,
    )


def _sync_etf_daily_sessions(
    client: TushareClient,
    *,
    calendar_exchange: str,
    start_date: date,
    target_through_date: date,
    checkpoint: DataSyncCheckpointState | None,
    sync_key: str,
    event_prefix: str,
    task_label: str,
    request_interval_ms: int | None,
    full_cycle_target_date: date | None = None,
) -> EtfDailySyncResult:
    """Synchronize a known contiguous session range with one checkpoint scope."""
    settings = get_settings()
    effective_interval_ms = max(
        settings.ingestion_request_interval_ms,
        request_interval_ms or 0,
    )
    trading_dates = _load_open_dates(
        exchange=calendar_exchange,
        start_date=start_date,
        end_date=target_through_date,
    )
    logger.info(
        f"{event_prefix}_planned",
        message=(
            f"{task_label}采集计划：{start_date.isoformat()} 至 "
            f"{target_through_date.isoformat()}，共 {len(trading_dates)} 个交易日，"
            f"请求间隔 {effective_interval_ms} 毫秒。"
        ),
        start_date=start_date.isoformat(),
        end_date=target_through_date.isoformat(),
        days_planned=len(trading_dates),
        request_interval_ms=effective_interval_ms,
    )

    received = changed = unchanged = 0
    for trading_date in trading_dates:
        date_text = trading_date.isoformat()
        logger.info(
            f"{event_prefix}_started",
            message=f"开始采集 {task_label}：{date_text} 至 {date_text}。",
            start_date=date_text,
            end_date=date_text,
        )
        try:
            tushare_request_pacer.wait_for_turn(effective_interval_ms)
            bars = normalize_etf_daily(
                fetch_etf_daily_for_trade_date(
                    client, trade_date=trading_date.strftime("%Y%m%d")
                ),
                expected_trade_date=trading_date,
            )
            if not bars:
                raise ValueError("Tushare returned no ETF daily records")
            write_result, checkpoint = _commit_etf_daily_date(
                bars=bars,
                expected_checkpoint=checkpoint,
                synced_through_date=trading_date,
                sync_key=sync_key,
                full_cycle_target_date=full_cycle_target_date,
            )
        except Exception:
            logger.exception(
                f"{event_prefix}_failed",
                message=(
                    f"{task_label}采集失败：{date_text} 至 {date_text}，"
                    "未入库，游标未推进。"
                ),
                start_date=date_text,
                end_date=date_text,
            )
            raise
        received += write_result.received
        changed += write_result.changed
        unchanged += write_result.unchanged
        logger.info(
            f"{event_prefix}_succeeded",
            message=(
                f"完成采集 {task_label}：{date_text} 至 {date_text}，"
                f"拉取 {write_result.received} 条，入库变更 {write_result.changed} 条，"
                f"未变更 {write_result.unchanged} 条，"
                f"游标已推进至 {checkpoint.cursor['synced_through_date']}。"
            ),
            start_date=date_text,
            end_date=date_text,
            received=write_result.received,
            changed=write_result.changed,
            unchanged=write_result.unchanged,
            synced_through_date=checkpoint.cursor["synced_through_date"],
        )

    return EtfDailySyncResult(
        days_completed=len(trading_dates),
        received=received,
        changed=changed,
        unchanged=unchanged,
        synced_through_date=(
            _checkpoint_synced_through_date(checkpoint)
            if checkpoint is not None
            else None
        ),
    )


def normalize_etf_daily(
    dataframe: "DataFrame", *, expected_trade_date: date
) -> list[EtfDailyBarInput]:
    """Validate one full-market API response before any database write occurs."""
    rows = dataframe.to_dict(orient="records")
    if len(rows) >= MAX_ETF_DAILY_ROWS:
        raise ValueError(
            f"Tushare ETF daily response may be truncated at {MAX_ETF_DAILY_ROWS} rows"
        )
    bars = [_normalize_etf_daily_row(row) for row in rows]
    if any(bar.trade_date != expected_trade_date for bar in bars):
        raise ValueError("Tushare ETF daily response contains another trade date")
    _reject_duplicate_bar_keys(bars)
    return bars


def _load_checkpoint(sync_key: str) -> DataSyncCheckpointState | None:
    with Session(get_engine()) as session:
        return DataSyncCheckpointRepository(session).get(sync_key, ETF_DAILY_SCOPE_KEY)


def _load_open_dates(
    *, exchange: str, start_date: date, end_date: date
) -> list[date]:
    if start_date > end_date:
        return []
    with Session(get_engine()) as session:
        return TradingCalendarRepository(session).list_open_dates(
            exchange=exchange,
            start_date=start_date,
            end_date=end_date,
        )


def _commit_etf_daily_date(
    *,
    bars: list[EtfDailyBarInput],
    expected_checkpoint: DataSyncCheckpointState | None,
    synced_through_date: date,
    sync_key: str,
    full_cycle_target_date: date | None = None,
) -> tuple[EtfDailyBarUpsertResult, DataSyncCheckpointState]:
    """Commit one complete ETF session and its checkpoint atomically."""
    with Session(get_engine()) as session:
        try:
            write_result = EtfDailyBarRepository(session).upsert_bars(
                bars, source=TUSHARE_SOURCE
            )
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=sync_key,
                scope_key=ETF_DAILY_SCOPE_KEY,
                cursor={
                    "synced_through_date": synced_through_date.isoformat(),
                    **(
                        {"target_through_date": full_cycle_target_date.isoformat()}
                        if full_cycle_target_date is not None
                        else {}
                    ),
                },
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


def _initialize_full_cycle(
    *,
    expected_checkpoint: DataSyncCheckpointState | None,
    initial_start_date: date,
    target_through_date: date,
) -> DataSyncCheckpointState:
    """Persist a new full-cycle target before its first source request starts.

    A dedicated initialization transaction makes an interrupted first request
    resumable against the same historical endpoint. The date before the initial
    boundary is only a cursor position; no market-data row is implied by it.
    """
    with Session(get_engine()) as session:
        try:
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=ETF_DAILY_FULL_SYNC_KEY,
                scope_key=ETF_DAILY_SCOPE_KEY,
                cursor={
                    "synced_through_date": (
                        initial_start_date - timedelta(days=1)
                    ).isoformat(),
                    "target_through_date": target_through_date.isoformat(),
                },
                expected_version=(
                    expected_checkpoint.version
                    if expected_checkpoint is not None
                    else None
                ),
            )
            session.commit()
            return checkpoint
        except Exception:
            session.rollback()
            raise


def _normalize_etf_daily_row(row: object) -> EtfDailyBarInput:
    if not isinstance(row, dict):
        raise ValueError("Tushare ETF daily response row is not an object")
    open_price = _parse_non_negative_decimal(row.get("open"), "open")
    high = _parse_non_negative_decimal(row.get("high"), "high")
    low = _parse_non_negative_decimal(row.get("low"), "low")
    close = _parse_non_negative_decimal(row.get("close"), "close")
    if high < low:
        raise ValueError("Tushare ETF daily record has high below low")
    return EtfDailyBarInput(
        ts_code=_parse_required_text(row.get("ts_code"), "ts_code"),
        trade_date=_parse_date(row.get("trade_date")),
        open=open_price,
        high=high,
        low=low,
        close=close,
        vol=_parse_non_negative_decimal(row.get("vol"), "vol"),
        amount=_parse_non_negative_decimal(row.get("amount"), "amount"),
    )


def _parse_required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip() if value is not None else ""
    if normalized.lower() in {"", "nan", "nat", "none"}:
        raise ValueError(f"Tushare ETF daily record has no {field_name}")
    return normalized


def _parse_date(value: object) -> date:
    try:
        return date.fromisoformat(_parse_required_text(value, "trade_date"))
    except ValueError as exc:
        raise ValueError("Tushare ETF daily record has an invalid trade_date") from exc


def _parse_non_negative_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(_parse_required_text(value, field_name))
    except InvalidOperation as exc:
        raise ValueError(
            f"Tushare ETF daily record has an invalid {field_name}"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(
            f"Tushare ETF daily record has an invalid {field_name}"
        )
    return parsed


def _reject_duplicate_bar_keys(bars: list[EtfDailyBarInput]) -> None:
    keys = [(bar.ts_code, bar.trade_date) for bar in bars]
    if len(keys) != len(set(keys)):
        raise ValueError("Tushare ETF daily response contains duplicate ETF bars")


def _checkpoint_synced_through_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("synced_through_date")
    if not isinstance(value, str):
        raise ValueError("ETF daily checkpoint has no synced_through_date")
    return date.fromisoformat(value)


def _full_checkpoint_target_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("target_through_date")
    if not isinstance(value, str):
        raise ValueError("ETF daily full checkpoint has no target_through_date")
    return date.fromisoformat(value)


def _full_cycle_is_complete(checkpoint: DataSyncCheckpointState) -> bool:
    return _checkpoint_synced_through_date(checkpoint) >= _full_checkpoint_target_date(
        checkpoint
    )
