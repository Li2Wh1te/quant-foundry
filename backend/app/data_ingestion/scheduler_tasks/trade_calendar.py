"""Scheduler task registration for Tushare trading calendar synchronization."""

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.services.trade_calendar import sync_trade_calendar
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


logger = structlog.get_logger(__name__)


class TradeCalendarSyncParameters(BaseModel):
    """Validated scheduler parameters for one exchange's calendar synchronization."""

    model_config = ConfigDict(extra="forbid")

    exchange: str = Field(min_length=1, max_length=16)
    initial_start_date: date
    request_interval_ms: int | None = Field(default=None, ge=0, le=60_000)

    @field_validator("initial_start_date", mode="before")
    @classmethod
    def parse_tushare_compact_date(cls, value: Any) -> Any:
        """Accept Tushare's compact YYYYMMDD date format before Pydantic parsing.

        A digit-only eight-character value would otherwise be interpreted as a Unix
        timestamp by Pydantic's generic date parser, even though Tushare documents
        calendar dates in YYYYMMDD format. Other inputs retain Pydantic's standard
        date validation, including the ISO-8601 date format used by persisted tasks.
        """
        if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
            return value
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError(
                "initial_start_date must be a valid YYYYMMDD or ISO-8601 date"
            ) from exc


def sync_trade_calendar_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    """Run one incremental trading calendar synchronization task."""
    if not isinstance(parameters, TradeCalendarSyncParameters):
        raise TypeError("unexpected parameters model for data.sync_trade_calendar")
    result = sync_trade_calendar(
        TushareClient(get_settings()),
        exchange=parameters.exchange,
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
        "trade_calendar_sync_completed",
        message=(
            f"交易日历采集完成：{parameters.exchange}，"
            f"完成 {payload['ranges_completed']} 个分段，拉取 {payload['received']} 条，"
            f"入库变更 {payload['changed']} 条，未变更 {payload['unchanged']} 条，"
            f"同步至 {payload['synced_through_date'] or '无须同步'}。"
        ),
        exchange=parameters.exchange,
        **payload,
    )
    return payload


def register_tasks(registry: TaskRegistry) -> None:
    """Register Tushare trading calendar synchronization with the scheduler."""
    registry.register(
        TaskDefinition(
            key="data.sync_trade_calendar",
            name="交易日历采集",
            english_name="Sync Tushare trade calendar",
            parameters_model=TradeCalendarSyncParameters,
            handler=sync_trade_calendar_task,
        )
    )
