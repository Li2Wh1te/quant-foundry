"""ETF reference-data retrieval and full-snapshot synchronization."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import ETF_BASIC_SYNC_KEY, TUSHARE_SOURCE
from app.data_ingestion.repositories.etf import EtfCodeRepository
from app.data_ingestion.repositories.sync_checkpoint import DataSyncCheckpointRepository
from app.data_ingestion.request_pacing import tushare_request_pacer
from app.data_ingestion.schemas.etf import (
    EtfBasicSyncResult,
    EtfBasicUpsertResult,
    EtfInstrumentInput,
)
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.db.session import get_engine

if TYPE_CHECKING:
    from pandas import DataFrame


ETF_BASIC_FIELDS = (
    "ts_code,csname,extname,cname,index_code,index_name,setup_date,list_date,"
    "list_status,exchange,mgr_name,custod_name,mgt_fee,etf_type"
)
ETF_BASIC_SCOPE_KEY = "market=CN"
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = structlog.get_logger(__name__)


def fetch_etf_basics(client: TushareClient) -> "DataFrame":
    """Fetch every documented ETF basic field without filtering by listing status.

    Omitting ``list_status`` requests listed, delisted, and pending ETFs, while an
    explicit fields list keeps the database contract stable if Tushare later adds
    unrelated response columns.
    """
    return client.pro.etf_basic(fields=ETF_BASIC_FIELDS)


def sync_etf_basics(
    client: TushareClient,
    *,
    request_interval_ms: int | None = None,
    refreshed_at: datetime | None = None,
) -> EtfBasicSyncResult:
    """Fetch and atomically commit one complete, idempotent ETF reference refresh."""
    settings = get_settings()
    effective_interval_ms = max(
        settings.ingestion_request_interval_ms,
        request_interval_ms or 0,
    )
    checkpoint = _load_checkpoint()
    completed_at = refreshed_at or datetime.now(SHANGHAI_TIMEZONE)
    logger.info(
        "etf_basic_sync_started",
        message=(
            "开始采集 ETF 基础信息：全部上市状态，适用日期不适用，"
            f"请求间隔 {effective_interval_ms} 毫秒。"
        ),
        start_date=None,
        end_date=None,
        request_interval_ms=effective_interval_ms,
    )
    try:
        tushare_request_pacer.wait_for_turn(effective_interval_ms)
        instruments = normalize_etf_basics(fetch_etf_basics(client))
        if not instruments:
            raise ValueError("Tushare returned no ETF basic records")
        write_result, updated_checkpoint = _commit_etf_basics(
            instruments=instruments,
            expected_checkpoint=checkpoint,
            refreshed_at=completed_at,
        )
    except Exception:
        logger.exception(
            "etf_basic_sync_failed",
            message="采集 ETF 基础信息失败：全部上市状态，适用日期不适用。",
            start_date=None,
            end_date=None,
        )
        raise
    logger.info(
        "etf_basic_sync_succeeded",
        message=(
            "完成采集 ETF 基础信息：全部上市状态，适用日期不适用，"
            f"拉取 {write_result.received} 条，入库变更 {write_result.changed} 条，"
            f"未变更 {write_result.unchanged} 条，游标已推进至 "
            f"{updated_checkpoint.cursor['refreshed_at']}。"
        ),
        start_date=None,
        end_date=None,
        received=write_result.received,
        changed=write_result.changed,
        unchanged=write_result.unchanged,
        refreshed_at=updated_checkpoint.cursor["refreshed_at"],
    )
    return EtfBasicSyncResult(
        received=write_result.received,
        changed=write_result.changed,
        unchanged=write_result.unchanged,
        refreshed_at=completed_at,
    )


def normalize_etf_basics(dataframe: "DataFrame") -> list[EtfInstrumentInput]:
    """Normalize Tushare values before they reach strongly typed database columns."""
    return [
        EtfInstrumentInput(
            ts_code=_required_text(row.get("ts_code"), "ts_code"),
            csname=_optional_text(row.get("csname")),
            extname=_optional_text(row.get("extname")),
            cname=_optional_text(row.get("cname")),
            index_code=_optional_text(row.get("index_code")),
            index_name=_optional_text(row.get("index_name")),
            setup_date=_optional_date(row.get("setup_date")),
            list_date=_optional_date(row.get("list_date")),
            list_status=_required_text(row.get("list_status"), "list_status"),
            exchange=_required_text(row.get("exchange"), "exchange"),
            mgr_name=_optional_text(row.get("mgr_name")),
            custod_name=_optional_text(row.get("custod_name")),
            mgt_fee=_optional_decimal(row.get("mgt_fee")),
            etf_type=_optional_text(row.get("etf_type")),
        )
        for row in dataframe.to_dict(orient="records")
    ]


def _load_checkpoint() -> DataSyncCheckpointState | None:
    with Session(get_engine()) as session:
        return DataSyncCheckpointRepository(session).get(
            ETF_BASIC_SYNC_KEY, ETF_BASIC_SCOPE_KEY
        )


def _commit_etf_basics(
    *,
    instruments: list[EtfInstrumentInput],
    expected_checkpoint: DataSyncCheckpointState | None,
    refreshed_at: datetime,
) -> tuple[EtfBasicUpsertResult, DataSyncCheckpointState]:
    """Keep reference changes and the successful full-refresh marker atomic."""
    with Session(get_engine()) as session:
        try:
            write_result = EtfCodeRepository(session).upsert_codes(
                instruments,
                source=TUSHARE_SOURCE,
                observed_at=refreshed_at,
            )
            checkpoint = DataSyncCheckpointRepository(session).advance(
                sync_key=ETF_BASIC_SYNC_KEY,
                scope_key=ETF_BASIC_SCOPE_KEY,
                cursor={"refreshed_at": refreshed_at.isoformat()},
                expected_version=(
                    expected_checkpoint.version if expected_checkpoint is not None else None
                ),
            )
            session.commit()
            return write_result, checkpoint
        except Exception:
            session.rollback()
            raise


def _required_text(value: Any, field_name: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError(f"Tushare ETF basic record has no {field_name}")
    return normalized


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return None if normalized.lower() in {"", "nan", "nat", "none"} else normalized


def _optional_date(value: Any) -> date | None:
    normalized = _optional_text(value)
    return date.fromisoformat(normalized) if normalized else None


def _optional_decimal(value: Any) -> Decimal | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Tushare ETF basic record has an invalid mgt_fee") from exc
