"""ETF adjustment-factor retrieval and whole-market synchronization."""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import (
    ETF_ADJUSTMENT_FULL_SYNC_KEY,
    ETF_ADJUSTMENT_INCREMENTAL_SYNC_KEY,
    TUSHARE_SOURCE,
)
from app.data_ingestion.repositories.etf import EtfCodeRepository
from app.data_ingestion.repositories.etf_adjustment import (
    EtfAdjustmentFactorRepository,
)
from app.data_ingestion.repositories.sync_checkpoint import DataSyncCheckpointRepository
from app.data_ingestion.repositories.trading_calendar import TradingCalendarRepository
from app.data_ingestion.request_pacing import tushare_request_pacer
from app.data_ingestion.schemas.etf_adjustment import (
    EtfAdjustmentFactorInput,
    EtfAdjustmentFactorUpsertResult,
    EtfAdjustmentSyncResult,
)
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.db.session import get_engine

if TYPE_CHECKING:
    from pandas import DataFrame


ETF_ADJUSTMENT_SCOPE_KEY = "market=CN"
ETF_ADJUSTMENT_CALENDAR_EXCHANGE = "SSE"
ETF_ADJUSTMENT_PAGE_SIZE = 2_000
DEFAULT_RECONCILIATION_LOOKBACK_DAYS = 60
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = structlog.get_logger(__name__)


def fetch_etf_adjustment_factors(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> "DataFrame":
    """Fetch raw ETF adjustment factors for one code and date range.

    This preserves Tushare's documented ``fund_adj`` example for targeted repair.
    Whole-market scheduled work uses ``fetch_etf_adjustment_factors_for_trade_date``
    so one paginated request covers every ETF for a completed trading session.
    """
    return client.pro.fund_adj(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
    )


def fetch_etf_adjustment_factors_for_trade_date(
    client: TushareClient,
    *,
    trade_date: str,
    offset: int,
    limit: int = ETF_ADJUSTMENT_PAGE_SIZE,
) -> "DataFrame":
    """Fetch one paginated whole-market factor page for a trading date."""
    return client.pro.fund_adj(trade_date=trade_date, offset=offset, limit=limit)


def sync_etf_adjustment_factors(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
    request_interval_ms: int | None = None,
) -> EtfAdjustmentFactorUpsertResult:
    """Fetch one code range and atomically upsert its latest source values."""
    effective_interval_ms = _effective_request_interval_ms(request_interval_ms)
    logger.info(
        "etf_adjustment_sync_started",
        message=f"开始采集 ETF 复权因子：{ts_code}，{start_date} 至 {end_date}。",
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
    )
    try:
        tushare_request_pacer.wait_for_turn(effective_interval_ms)
        factors = normalize_etf_adjustment_factors(
            fetch_etf_adjustment_factors(
                client,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            ),
            expected_ts_code=ts_code,
        )
        if not factors:
            raise ValueError("Tushare returned no ETF adjustment factors")
        result = _commit_etf_adjustment_factors(factors)
    except Exception:
        logger.exception(
            "etf_adjustment_sync_failed",
            message=f"采集 ETF 复权因子失败：{ts_code}，{start_date} 至 {end_date}。",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )
        raise
    logger.info(
        "etf_adjustment_sync_succeeded",
        message=(
            f"完成采集 ETF 复权因子：{ts_code}，{start_date} 至 {end_date}，"
            f"拉取 {result.received} 条，入库变更 {result.changed} 条，"
            f"未变更 {result.unchanged} 条。"
        ),
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        received=result.received,
        changed=result.changed,
        unchanged=result.unchanged,
    )
    return result


def sync_etf_adjustment_incremental(
    client: TushareClient,
    *,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> EtfAdjustmentSyncResult:
    """Synchronize all completed market sessions after one market-level cursor."""
    target_through_date = _completed_through_date(as_of_date)
    checkpoint = _load_checkpoint(ETF_ADJUSTMENT_INCREMENTAL_SYNC_KEY)
    start_date = (
        _checkpoint_synced_through_date(checkpoint) + timedelta(days=1)
        if checkpoint is not None
        else target_through_date
    )
    return _sync_market_dates(
        client,
        start_date=start_date,
        target_through_date=target_through_date,
        checkpoint=checkpoint,
        sync_key=ETF_ADJUSTMENT_INCREMENTAL_SYNC_KEY,
        event_prefix="etf_adjustment_incremental_sync",
        task_label="ETF 复权因子增量",
        request_interval_ms=request_interval_ms,
    )


def sync_etf_adjustment_full(
    client: TushareClient,
    *,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> EtfAdjustmentSyncResult:
    """Run or resume a full market-level verification cycle with one cursor."""
    completed_through_date = _completed_through_date(as_of_date)
    checkpoint = _load_checkpoint(ETF_ADJUSTMENT_FULL_SYNC_KEY)
    if checkpoint is not None and not _full_cycle_is_complete(checkpoint):
        target_through_date = _full_checkpoint_target_date(checkpoint)
        start_date = _checkpoint_synced_through_date(checkpoint) + timedelta(days=1)
    else:
        target_through_date = completed_through_date
        start_date = _load_earliest_etf_list_date()
        checkpoint = _initialize_full_cycle(
            expected_checkpoint=checkpoint,
            initial_start_date=start_date,
            target_through_date=target_through_date,
        )
    return _sync_market_dates(
        client,
        start_date=start_date,
        target_through_date=target_through_date,
        checkpoint=checkpoint,
        sync_key=ETF_ADJUSTMENT_FULL_SYNC_KEY,
        event_prefix="etf_adjustment_full_sync",
        task_label="ETF 复权因子全量",
        request_interval_ms=request_interval_ms,
        full_cycle_target_date=target_through_date,
    )


def sync_etf_adjustment_reconciliation(
    client: TushareClient,
    *,
    lookback_trading_days: int = DEFAULT_RECONCILIATION_LOOKBACK_DAYS,
    request_interval_ms: int | None = None,
    as_of_date: date | None = None,
) -> EtfAdjustmentSyncResult:
    """Re-fetch recent market sessions to absorb current source corrections."""
    if lookback_trading_days <= 0:
        raise ValueError("lookback_trading_days must be positive")
    target_through_date = _completed_through_date(as_of_date)
    candidate_start_date = target_through_date - timedelta(
        days=lookback_trading_days * 3
    )
    candidates = _load_open_dates(
        start_date=candidate_start_date,
        end_date=target_through_date,
    )
    trading_dates = candidates[-lookback_trading_days:]
    return _sync_market_dates(
        client,
        start_date=trading_dates[0] if trading_dates else target_through_date,
        target_through_date=target_through_date,
        checkpoint=None,
        sync_key=None,
        event_prefix="etf_adjustment_reconciliation",
        task_label="ETF 复权因子近期校验",
        request_interval_ms=request_interval_ms,
        trading_dates=trading_dates,
    )


def normalize_etf_adjustment_factors(
    dataframe: "DataFrame",
    *,
    expected_ts_code: str | None = None,
    expected_trade_date: date | None = None,
) -> list[EtfAdjustmentFactorInput]:
    """Validate and normalize one Tushare adjustment-factor response page."""
    factors = [_normalize_factor_row(row) for row in dataframe.to_dict(orient="records")]
    if expected_ts_code is not None and any(
        factor.ts_code != expected_ts_code for factor in factors
    ):
        raise ValueError("Tushare ETF adjustment response contains another ts_code")
    if expected_trade_date is not None and any(
        factor.trade_date != expected_trade_date for factor in factors
    ):
        raise ValueError("Tushare ETF adjustment response contains another trade_date")
    _reject_duplicate_factor_keys(factors)
    return factors


def _sync_market_dates(
    client: TushareClient,
    *,
    start_date: date,
    target_through_date: date,
    checkpoint: DataSyncCheckpointState | None,
    sync_key: str | None,
    event_prefix: str,
    task_label: str,
    request_interval_ms: int | None,
    full_cycle_target_date: date | None = None,
    trading_dates: list[date] | None = None,
) -> EtfAdjustmentSyncResult:
    """Fetch complete whole-market pages for each requested completed session."""
    effective_interval_ms = _effective_request_interval_ms(request_interval_ms)
    dates = trading_dates if trading_dates is not None else _load_open_dates(
        start_date=start_date,
        end_date=target_through_date,
    )
    logger.info(
        f"{event_prefix}_planned",
        message=(
            f"{task_label}采集计划：{start_date.isoformat()} 至 "
            f"{target_through_date.isoformat()}，共 {len(dates)} 个交易日，"
            f"请求间隔 {effective_interval_ms} 毫秒。"
        ),
        start_date=start_date.isoformat(),
        end_date=target_through_date.isoformat(),
        days_planned=len(dates),
        request_interval_ms=effective_interval_ms,
    )
    received = changed = unchanged = 0
    for trading_date in dates:
        date_text = trading_date.isoformat()
        logger.info(
            f"{event_prefix}_started",
            message=f"开始采集 {task_label}：{date_text} 至 {date_text}。",
            start_date=date_text,
            end_date=date_text,
        )
        try:
            factors = _fetch_market_factors_for_trade_date(
                client,
                trade_date=trading_date,
                request_interval_ms=effective_interval_ms,
            )
            if not factors:
                raise ValueError("Tushare returned no ETF adjustment factors")
            write_result, checkpoint = _commit_market_trade_date(
                factors=factors,
                expected_checkpoint=checkpoint,
                synced_through_date=trading_date,
                sync_key=sync_key,
                full_cycle_target_date=full_cycle_target_date,
            )
        except Exception:
            logger.exception(
                f"{event_prefix}_failed",
                message=(
                    f"采集 {task_label}失败：{date_text} 至 {date_text}，"
                    "未入库，游标未推进。"
                ),
                start_date=date_text,
                end_date=date_text,
            )
            raise
        received += write_result.received
        changed += write_result.changed
        unchanged += write_result.unchanged
        cursor_message = (
            f"游标已推进至 {checkpoint.cursor['synced_through_date']}。"
            if checkpoint is not None
            else "未使用游标。"
        )
        logger.info(
            f"{event_prefix}_succeeded",
            message=(
                f"完成采集 {task_label}：{date_text} 至 {date_text}，"
                f"拉取 {write_result.received} 条，入库变更 {write_result.changed} 条，"
                f"未变更 {write_result.unchanged} 条，{cursor_message}"
            ),
            start_date=date_text,
            end_date=date_text,
            received=write_result.received,
            changed=write_result.changed,
            unchanged=write_result.unchanged,
            synced_through_date=(
                checkpoint.cursor["synced_through_date"]
                if checkpoint is not None
                else None
            ),
        )
    return EtfAdjustmentSyncResult(
        days_completed=len(dates),
        received=received,
        changed=changed,
        unchanged=unchanged,
        synced_through_date=(
            _checkpoint_synced_through_date(checkpoint)
            if checkpoint is not None
            else (dates[-1] if dates else None)
        ),
    )


def _fetch_market_factors_for_trade_date(
    client: TushareClient,
    *,
    trade_date: date,
    request_interval_ms: int,
) -> list[EtfAdjustmentFactorInput]:
    """Fetch every page for one date before exposing any partial result to writes."""
    offset = 0
    factors: list[EtfAdjustmentFactorInput] = []
    while True:
        tushare_request_pacer.wait_for_turn(request_interval_ms)
        page = normalize_etf_adjustment_factors(
            fetch_etf_adjustment_factors_for_trade_date(
                client,
                trade_date=trade_date.strftime("%Y%m%d"),
                offset=offset,
            ),
            expected_trade_date=trade_date,
        )
        factors.extend(page)
        if len(page) < ETF_ADJUSTMENT_PAGE_SIZE:
            break
        offset += ETF_ADJUSTMENT_PAGE_SIZE
    _reject_duplicate_factor_keys(factors)
    return factors


def _commit_etf_adjustment_factors(
    factors: list[EtfAdjustmentFactorInput],
) -> EtfAdjustmentFactorUpsertResult:
    """Commit all normalized targeted-repair factors in one transaction."""
    with Session(get_engine()) as session:
        try:
            result = EtfAdjustmentFactorRepository(session).upsert_factors(
                factors, source=TUSHARE_SOURCE
            )
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise


def _commit_market_trade_date(
    *,
    factors: list[EtfAdjustmentFactorInput],
    expected_checkpoint: DataSyncCheckpointState | None,
    synced_through_date: date,
    sync_key: str | None,
    full_cycle_target_date: date | None = None,
) -> tuple[EtfAdjustmentFactorUpsertResult, DataSyncCheckpointState | None]:
    """Commit a complete date and, when required, its market cursor atomically."""
    with Session(get_engine()) as session:
        try:
            result = EtfAdjustmentFactorRepository(session).upsert_factors(
                factors, source=TUSHARE_SOURCE
            )
            checkpoint = (
                DataSyncCheckpointRepository(session).advance(
                    sync_key=sync_key,
                    scope_key=ETF_ADJUSTMENT_SCOPE_KEY,
                    cursor={
                        "synced_through_date": synced_through_date.isoformat(),
                        **(
                            {
                                "target_through_date": full_cycle_target_date.isoformat()
                            }
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
                if sync_key is not None
                else None
            )
            session.commit()
            return result, checkpoint
        except Exception:
            session.rollback()
            raise


def _load_checkpoint(sync_key: str) -> DataSyncCheckpointState | None:
    with Session(get_engine()) as session:
        return DataSyncCheckpointRepository(session).get(
            sync_key, ETF_ADJUSTMENT_SCOPE_KEY
        )


def _load_open_dates(*, start_date: date, end_date: date) -> list[date]:
    if start_date > end_date:
        return []
    with Session(get_engine()) as session:
        return TradingCalendarRepository(session).list_open_dates(
            exchange=ETF_ADJUSTMENT_CALENDAR_EXCHANGE,
            start_date=start_date,
            end_date=end_date,
        )


def _load_earliest_etf_list_date() -> date:
    with Session(get_engine()) as session:
        earliest_list_date = EtfCodeRepository(session).earliest_list_date(
            source=TUSHARE_SOURCE
        )
    if earliest_list_date is None:
        raise ValueError(
            "ETF basic data is required before starting a full adjustment sync"
        )
    return earliest_list_date


def _initialize_full_cycle(
    *,
    expected_checkpoint: DataSyncCheckpointState | None,
    initial_start_date: date,
    target_through_date: date,
) -> DataSyncCheckpointState:
    """Freeze one full-run target before its first source request starts."""
    with Session(get_engine()) as session:
        try:
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=ETF_ADJUSTMENT_FULL_SYNC_KEY,
                scope_key=ETF_ADJUSTMENT_SCOPE_KEY,
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


def _completed_through_date(as_of_date: date | None) -> date:
    return as_of_date or (datetime.now(SHANGHAI_TIMEZONE).date() - timedelta(days=1))


def _effective_request_interval_ms(request_interval_ms: int | None) -> int:
    return max(get_settings().ingestion_request_interval_ms, request_interval_ms or 0)


def _normalize_factor_row(row: object) -> EtfAdjustmentFactorInput:
    if not isinstance(row, dict):
        raise ValueError("Tushare ETF adjustment response row is not an object")
    return EtfAdjustmentFactorInput(
        ts_code=_required_text(row.get("ts_code"), "ts_code"),
        trade_date=_parse_date(row.get("trade_date")),
        adj_factor=_parse_positive_decimal(row.get("adj_factor"), "adj_factor"),
    )


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip() if value is not None else ""
    if normalized.lower() in {"", "nan", "nat", "none"}:
        raise ValueError(f"Tushare ETF adjustment record has no {field_name}")
    return normalized


def _parse_date(value: object) -> date:
    try:
        return date.fromisoformat(_required_text(value, "trade_date"))
    except ValueError as exc:
        raise ValueError(
            "Tushare ETF adjustment record has an invalid trade_date"
        ) from exc


def _parse_positive_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(_required_text(value, field_name))
    except InvalidOperation as exc:
        raise ValueError(
            f"Tushare ETF adjustment record has an invalid {field_name}"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(
            f"Tushare ETF adjustment record has an invalid {field_name}"
        )
    return parsed


def _reject_duplicate_factor_keys(
    factors: list[EtfAdjustmentFactorInput],
) -> None:
    keys = [(factor.ts_code, factor.trade_date) for factor in factors]
    if len(keys) != len(set(keys)):
        raise ValueError(
            "Tushare ETF adjustment response contains duplicate ts_code and trade_date"
        )


def _checkpoint_synced_through_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("synced_through_date")
    if not isinstance(value, str):
        raise ValueError("ETF adjustment checkpoint has no synced_through_date")
    return date.fromisoformat(value)


def _full_checkpoint_target_date(checkpoint: DataSyncCheckpointState) -> date:
    value = checkpoint.cursor.get("target_through_date")
    if not isinstance(value, str):
        raise ValueError("ETF adjustment full checkpoint has no target_through_date")
    return date.fromisoformat(value)


def _full_cycle_is_complete(checkpoint: DataSyncCheckpointState) -> bool:
    return _checkpoint_synced_through_date(checkpoint) >= _full_checkpoint_target_date(
        checkpoint
    )
