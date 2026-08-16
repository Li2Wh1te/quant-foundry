"""ETF adjustment-factor retrieval and current-value synchronization."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.repositories.etf_adjustment import (
    EtfAdjustmentFactorRepository,
)
from app.data_ingestion.request_pacing import tushare_request_pacer
from app.data_ingestion.schemas.etf_adjustment import (
    EtfAdjustmentFactorInput,
    EtfAdjustmentFactorUpsertResult,
)
from app.db.session import get_engine

if TYPE_CHECKING:
    from pandas import DataFrame


logger = structlog.get_logger(__name__)


def fetch_etf_adjustment_factors(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> "DataFrame":
    """Fetch raw ETF adjustment factors for one Tushare code and date range.

    This intentionally mirrors Tushare's documented ``fund_adj`` example. It
    performs one request only; callers should split ranges that exceed the
    provider's per-request row limit.
    """
    return client.pro.fund_adj(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
    )


def sync_etf_adjustment_factors(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
    request_interval_ms: int | None = None,
) -> EtfAdjustmentFactorUpsertResult:
    """Fetch one range and atomically upsert its latest source factor values."""
    settings = get_settings()
    effective_interval_ms = max(
        settings.ingestion_request_interval_ms,
        request_interval_ms or 0,
    )
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


def normalize_etf_adjustment_factors(
    dataframe: "DataFrame", *, expected_ts_code: str
) -> list[EtfAdjustmentFactorInput]:
    """Validate and normalize one Tushare adjustment-factor response."""
    factors = [
        _normalize_factor_row(row)
        for row in dataframe.to_dict(orient="records")
    ]
    if any(factor.ts_code != expected_ts_code for factor in factors):
        raise ValueError("Tushare ETF adjustment response contains another ts_code")
    _reject_duplicate_factor_keys(factors)
    return factors


def _commit_etf_adjustment_factors(
    factors: list[EtfAdjustmentFactorInput],
) -> EtfAdjustmentFactorUpsertResult:
    """Commit all normalized factors in one transaction."""
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
