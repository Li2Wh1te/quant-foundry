"""Scheduler registrations for incremental and full Tushare ETF daily syncing."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.services.etf_daily import (
    sync_etf_daily_full,
    sync_etf_daily_incremental,
)
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


logger = structlog.get_logger(__name__)


class EtfDailySyncParameters(BaseModel):
    """Shared validated parameters for whole-market ETF daily-bar workflows."""

    model_config = ConfigDict(extra="forbid")

    request_interval_ms: int | None = Field(default=None, ge=0, le=60_000)

    @model_validator(mode="before")
    @classmethod
    def discard_removed_legacy_parameters(cls, value: Any) -> Any:
        """Accept existing tasks while omitting obsolete fields from new schemas.

        Earlier task definitions required an operator-selected start date and a
        calendar exchange. Full runs now derive the first date from ETF reference
        data, and both workflows resolve the applicable named calendar from
        point-in-time instrument identity facts.
        """
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.pop("initial_start_date", None)
        normalized.pop("calendar_exchange", None)
        return normalized


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
        request_interval_ms=parameters.request_interval_ms,
        data_cutoff=datetime.now(UTC),
    )
    return _result_payload_and_log(
        result,
        event="etf_daily_incremental_sync_completed",
        task_label="ETF 日线增量",
        context=context,
    )


def sync_etf_daily_full_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    """Run one complete historical ETF daily-bar verification cycle."""
    if not isinstance(parameters, EtfDailySyncParameters):
        raise TypeError("unexpected parameters model for data.sync_etf_daily_full")
    result = sync_etf_daily_full(
        TushareClient(get_settings()),
        request_interval_ms=parameters.request_interval_ms,
        data_cutoff=datetime.now(UTC),
    )
    return _result_payload_and_log(
        result,
        event="etf_daily_full_sync_completed",
        task_label="ETF 日线全量",
        context=context,
    )


def _result_payload_and_log(
    result: Any, *, event: str, task_label: str, context: TaskContext | None = None
) -> dict[str, Any]:
    """Serialize dates and emit the required operator-facing Chinese summary."""
    payload = asdict(result)
    for field_name in (
        "start_date",
        "end_date",
        "synced_through_date",
        "affected_start_date",
        "affected_end_date",
    ):
        value = getattr(result, field_name, None)
        payload[field_name] = value.isoformat() if value is not None else None
    payload.setdefault("calendar_ids", ())
    calendar_ids = tuple(payload["calendar_ids"] or ())
    calendar_id = calendar_ids[0] if len(calendar_ids) == 1 else None
    checkpoint_scope = f"calendar_id={calendar_id}" if calendar_id else "calendar_id=*"
    logger.info(
        event,
        message=(
            f"{task_label}采集完成：{payload['start_date'] or '无起始日期'} 至 "
            f"{payload['end_date'] or '无结束日期'}，完成 {payload['days_completed']} 个交易日，"
            f"拉取 {payload['received']} 条，入库变更 {payload['changed']} 条，"
            f"新增 {payload.get('inserted', 0)} 条，修订 {payload.get('corrected', 0)} 条，"
            f"元数据补全 {payload.get('metadata_backfilled', 0)} 条，"
            f"未变更 {payload['unchanged']} 条，失败 0 条，"
            f"revision {payload.get('batch_revision') or '无'}，影响范围 "
            f"{payload.get('affected_start_date') or payload['start_date'] or '无'} 至 "
            f"{payload.get('affected_end_date') or payload['end_date'] or '无'}，"
            f"checkpoint 已推进至 {payload['synced_through_date'] or '无须推进'}。"
        ),
        title=f"{task_label}采集完成",
        data_type="etf_daily",
        calendar_id=calendar_id,
        fetched_count=payload["received"],
        changed_count=payload["changed"],
        unchanged_count=payload["unchanged"],
        failed_count=0,
        checkpoint_scope=checkpoint_scope,
        checkpoint_before=None,
        checkpoint_after=payload["synced_through_date"],
        checkpoint_advanced=bool(payload["synced_through_date"]),
        source=TUSHARE_SOURCE,
        # Propagate the revision generated by the current sync batch so
        # operator-facing structured logs retain auditable source provenance.
        source_revision=payload.get("batch_revision"),
        inserted_count=payload.get("inserted", 0),
        corrected_count=payload.get("corrected", 0),
        metadata_backfilled_count=payload.get("metadata_backfilled", 0),
        # Keep the exact affected date range from this batch instead of
        # dropping it at the scheduler boundary.
        reconciliation_range={
            "range_start": payload.get("affected_start_date"),
            "range_end": payload.get("affected_end_date"),
        },
        task_id=str(context.task_id) if context is not None else None,
        run_id=str(context.run_id) if context is not None else None,
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
