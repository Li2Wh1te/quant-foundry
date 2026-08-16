"""Scheduler registrations for incremental and full Tushare ETF daily syncing."""

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.services.etf_daily import (
    sync_etf_daily_full,
    sync_etf_daily_incremental,
)
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


logger = structlog.get_logger(__name__)


class EtfDailySyncParameters(BaseModel):
    """Shared validated parameters for whole-market ETF daily-bar workflows."""

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


def sync_etf_daily_incremental_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    """Run the low-latency ETF daily-bar incremental workflow."""
    if not isinstance(parameters, EtfDailySyncParameters):
        raise TypeError(
            "unexpected parameters model for data.sync_etf_daily_incremental"
        )
    result = sync_etf_daily_incremental(
        TushareClient(get_settings()),
        calendar_exchange=parameters.calendar_exchange,
        initial_start_date=parameters.initial_start_date,
        request_interval_ms=parameters.request_interval_ms,
    )
    return _result_payload_and_log(
        result,
        event="etf_daily_incremental_sync_completed",
        task_label="ETF 日线增量",
    )


def sync_etf_daily_full_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    """Run one complete historical ETF daily-bar verification cycle."""
    if not isinstance(parameters, EtfDailySyncParameters):
        raise TypeError("unexpected parameters model for data.sync_etf_daily_full")
    result = sync_etf_daily_full(
        TushareClient(get_settings()),
        calendar_exchange=parameters.calendar_exchange,
        initial_start_date=parameters.initial_start_date,
        request_interval_ms=parameters.request_interval_ms,
    )
    return _result_payload_and_log(
        result,
        event="etf_daily_full_sync_completed",
        task_label="ETF 日线全量",
    )


def _result_payload_and_log(
    result: Any, *, event: str, task_label: str
) -> dict[str, Any]:
    """Serialize dates and emit the required operator-facing Chinese summary."""
    payload = asdict(result)
    payload["synced_through_date"] = (
        result.synced_through_date.isoformat()
        if result.synced_through_date is not None
        else None
    )
    logger.info(
        event,
        message=(
            f"{task_label}采集完成：完成 {payload['days_completed']} 个交易日，"
            f"拉取 {payload['received']} 条，入库变更 {payload['changed']} 条，"
            f"未变更 {payload['unchanged']} 条，"
            f"游标已推进至 {payload['synced_through_date'] or '无须同步'}。"
        ),
        **payload,
    )
    return payload


def register_tasks(registry: TaskRegistry) -> None:
    """Register independently checkpointed ETF daily-bar task types."""
    registry.register(
        TaskDefinition(
            key="data.sync_etf_daily_incremental",
            name="ETF日线增量采集",
            english_name="Incremental Tushare ETF daily bars",
            parameters_model=EtfDailySyncParameters,
            handler=sync_etf_daily_incremental_task,
        )
    )
    registry.register(
        TaskDefinition(
            key="data.sync_etf_daily_full",
            name="ETF日线全量采集",
            english_name="Full Tushare ETF daily bars",
            parameters_model=EtfDailySyncParameters,
            handler=sync_etf_daily_full_task,
        )
    )
