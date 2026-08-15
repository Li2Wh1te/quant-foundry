"""Scheduler task registration for Tushare ETF daily-bar synchronization."""

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.services.etf_daily import sync_etf_daily
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


logger = structlog.get_logger(__name__)


class EtfDailySyncParameters(BaseModel):
    """Validated parameters for chronological whole-market ETF daily syncing."""

    model_config = ConfigDict(extra="forbid")

    calendar_exchange: str = Field(min_length=1, max_length=16)
    initial_start_date: date
    request_interval_ms: int | None = Field(default=None, ge=0, le=60_000)

    @field_validator("initial_start_date", mode="before")
    @classmethod
    def parse_tushare_compact_date(cls, value: Any) -> Any:
        """Accept the compact date format documented by Tushare."""
        if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
            return value
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError(
                "initial_start_date must be a valid YYYYMMDD or ISO-8601 date"
            ) from exc


def sync_etf_daily_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    """Run one whole-market ETF daily-bar synchronization task."""
    if not isinstance(parameters, EtfDailySyncParameters):
        raise TypeError("unexpected parameters model for data.sync_etf_daily")
    result = sync_etf_daily(
        TushareClient(get_settings()),
        calendar_exchange=parameters.calendar_exchange,
        initial_start_date=parameters.initial_start_date,
        request_interval_ms=parameters.request_interval_ms,
    )
    payload = asdict(result)
    payload["synced_through_date"] = (
        result.synced_through_date.isoformat()
        if result.synced_through_date is not None
        else None
    )
    logger.info(
        "etf_daily_sync_completed",
        message=(
            "ETF 日线采集完成："
            f"完成 {payload['days_completed']} 个交易日，拉取 {payload['received']} 条，"
            f"入库变更 {payload['changed']} 条，未变更 {payload['unchanged']} 条，"
            f"游标已推进至 {payload['synced_through_date'] or '无须同步'}。"
        ),
        **payload,
    )
    return payload


def register_tasks(registry: TaskRegistry) -> None:
    """Register Tushare ETF daily-bar synchronization with the scheduler."""
    registry.register(
        TaskDefinition(
            key="data.sync_etf_daily",
            name="ETF日线采集",
            english_name="Sync Tushare ETF daily bars",
            parameters_model=EtfDailySyncParameters,
            handler=sync_etf_daily_task,
        )
    )
