"""Scheduler task registration for Tushare trading calendar synchronization."""

from dataclasses import asdict
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.services.trade_calendar import sync_trade_calendar
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


class TradeCalendarSyncParameters(BaseModel):
    """Validated scheduler parameters for one exchange's calendar synchronization."""

    model_config = ConfigDict(extra="forbid")

    exchange: str = Field(min_length=1, max_length=16)
    initial_start_date: date
    request_interval_ms: int | None = Field(default=None, ge=0, le=60_000)


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
