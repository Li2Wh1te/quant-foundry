"""Scheduler task registration for Tushare trading calendar synchronization."""

from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.services.trade_calendar import (
    SHANGHAI_TIMEZONE,
    resolve_named_trade_calendar_context,
    sync_named_trade_calendar,
    sync_trade_calendar,
)
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
    now = datetime.now(UTC)
    metadata = resolve_named_trade_calendar_context(
        parameters.exchange,
        effective_day=parameters.initial_start_date,
        data_cutoff=now,
    )
    result = sync_named_trade_calendar(
        TushareClient(get_settings()),
        calendar_id=metadata.calendar_id,
        initial_start_date=parameters.initial_start_date,
        registry=metadata.registry,
        definition=metadata.definition,
        source_priority=metadata.source_priority,
        source_revision=metadata.source_revision,
        request_interval_ms=parameters.request_interval_ms,
        as_of_date=now.astimezone(SHANGHAI_TIMEZONE).date(),
        observed_at=now,
        known_at=now,
    )
    payload = asdict(result)
    for field_name in ("start_date", "end_date", "synced_through_date"):
        value = getattr(result, field_name, None)
        payload[field_name] = value.isoformat() if value is not None else None
    logger.info(
        "trade_calendar_sync_completed",
        message=(
            f"交易日历采集完成：{parameters.exchange}，"
            f"{payload['start_date'] or '无起始日期'} 至 {payload['end_date'] or '无结束日期'}，"
            f"完成 {payload['ranges_completed']} 个分段，拉取 {payload['received']} 条，"
            f"入库变更 {payload['changed']} 条，未变更 {payload['unchanged']} 条，失败 0 条，"
            f"checkpoint 已推进至 {payload['synced_through_date'] or '无须推进'}。"
        ),
        title="交易日历采集完成",
        exchange=parameters.exchange,
        data_type="trading_calendar",
        fetched_count=payload["received"],
        changed_count=payload["changed"],
        unchanged_count=payload["unchanged"],
        failed_count=0,
        checkpoint_scope=f"calendar_id={payload.get('calendar_id')}",
        checkpoint_before=None,
        checkpoint_after=payload.get("synced_through_date"),
        checkpoint_advanced=bool(payload["synced_through_date"]),
        source=TUSHARE_SOURCE,
        source_revision=None,
        reconciliation_range=None,
        task_id=str(context.task_id),
        run_id=str(context.run_id),
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
