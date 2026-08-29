"""Scheduler registrations for whole-market ETF adjustment-factor workflows."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.services.etf_adjustment import (
    DEFAULT_RECONCILIATION_LOOKBACK_DAYS,
    sync_etf_adjustment_full,
    sync_etf_adjustment_incremental,
    sync_etf_adjustment_reconciliation,
)
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


logger = structlog.get_logger(__name__)


class EtfAdjustmentSyncParameters(BaseModel):
    """Validated shared parameters for cursor-backed factor sync tasks."""

    model_config = ConfigDict(extra="forbid")

    request_interval_ms: int | None = Field(default=None, ge=0, le=60_000)


class EtfAdjustmentReconciliationParameters(EtfAdjustmentSyncParameters):
    """Validated parameters for a recent-window source-correction check."""

    lookback_trading_days: int = Field(
        default=DEFAULT_RECONCILIATION_LOOKBACK_DAYS,
        ge=1,
        le=250,
    )


def sync_etf_adjustment_incremental_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    """Synchronize every newly completed whole-market factor session."""
    if not isinstance(parameters, EtfAdjustmentSyncParameters):
        raise TypeError(
            "unexpected parameters model for data.sync_etf_adjustment_incremental"
        )
    result = sync_etf_adjustment_incremental(
        TushareClient(get_settings()),
        request_interval_ms=parameters.request_interval_ms,
        data_cutoff=datetime.now(UTC),
    )
    return _result_payload_and_log(
        result,
        event="etf_adjustment_incremental_sync_completed",
        task_label="ETF 复权因子增量",
        context=context,
    )


def sync_etf_adjustment_full_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    """Run or resume the one-cursor full historical factor workflow."""
    if not isinstance(parameters, EtfAdjustmentSyncParameters):
        raise TypeError("unexpected parameters model for data.sync_etf_adjustment_full")
    result = sync_etf_adjustment_full(
        TushareClient(get_settings()),
        request_interval_ms=parameters.request_interval_ms,
        data_cutoff=datetime.now(UTC),
    )
    return _result_payload_and_log(
        result,
        event="etf_adjustment_full_sync_completed",
        task_label="ETF 复权因子全量",
        context=context,
    )


def sync_etf_adjustment_reconciliation_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    """Re-check recent sessions for source corrections without moving a cursor."""
    if not isinstance(parameters, EtfAdjustmentReconciliationParameters):
        raise TypeError(
            "unexpected parameters model for data.sync_etf_adjustment_reconciliation"
        )
    result = sync_etf_adjustment_reconciliation(
        TushareClient(get_settings()),
        lookback_trading_days=parameters.lookback_trading_days,
        request_interval_ms=parameters.request_interval_ms,
        data_cutoff=datetime.now(UTC),
    )
    return _result_payload_and_log(
        result,
        event="etf_adjustment_reconciliation_completed",
        task_label="ETF 复权因子近期校验",
        context=context,
    )


def _result_payload_and_log(
    result: Any, *, event: str, task_label: str, context: TaskContext | None = None
) -> dict[str, Any]:
    """Serialize the result and emit the required Chinese operator summary."""
    payload = asdict(result)
    for field_name in ("start_date", "end_date", "synced_through_date"):
        value = getattr(result, field_name, None)
        payload[field_name] = value.isoformat() if value is not None else None
    payload.setdefault("calendar_ids", ())
    calendar_ids = tuple(payload["calendar_ids"] or ())
    calendar_id = calendar_ids[0] if len(calendar_ids) == 1 else None
    checkpoint_scope = f"calendar_id={calendar_id}" if calendar_id else "calendar_id=*"
    is_reconciliation = "reconciliation" in event
    reconciliation_range = (
        {
            "range_start": payload["start_date"],
            "range_end": payload["end_date"],
        }
        if is_reconciliation and payload["start_date"] and payload["end_date"]
        else None
    )
    checkpoint_advanced = bool(payload["synced_through_date"]) and not is_reconciliation
    checkpoint_after = (
        payload["synced_through_date"] if checkpoint_advanced else None
    )
    action_label = "校验" if is_reconciliation else "采集"
    logger.info(
        event,
        message=(
            f"{task_label}{action_label}完成：{payload['start_date'] or '无起始日期'} 至 "
            f"{payload['end_date'] or '无结束日期'}，完成 {payload['days_completed']} 个交易日，"
            f"拉取 {payload['received']} 条，入库变更 {payload['changed']} 条，"
            f"未变更 {payload['unchanged']} 条，失败 0 条，"
            + (
                f"checkpoint 已推进至 {payload['synced_through_date']}。"
                if checkpoint_advanced
                else "checkpoint 未推进。"
            )
        ),
        title=f"{task_label}完成",
        data_type="etf_adjustment",
        calendar_id=calendar_id,
        fetched_count=payload["received"],
        changed_count=payload["changed"],
        unchanged_count=payload["unchanged"],
        failed_count=0,
        checkpoint_scope=checkpoint_scope,
        checkpoint_before=None,
        checkpoint_after=checkpoint_after,
        checkpoint_advanced=checkpoint_advanced,
        source=TUSHARE_SOURCE,
        source_revision=None,
        reconciliation_range=reconciliation_range,
        task_id=str(context.task_id) if context is not None else None,
        run_id=str(context.run_id) if context is not None else None,
        **payload,
    )
    return payload


def register_tasks(registry: TaskRegistry) -> None:
    """Register the three market-level factor synchronization task types."""
    registry.register(
        TaskDefinition(
            key="data.sync_etf_adjustment_incremental",
            name="ETF复权因子增量采集",
            english_name="Incremental Tushare ETF adjustment factors",
            parameters_model=EtfAdjustmentSyncParameters,
            handler=sync_etf_adjustment_incremental_task,
        )
    )
    registry.register(
        TaskDefinition(
            key="data.sync_etf_adjustment_full",
            name="ETF复权因子全量采集",
            english_name="Full Tushare ETF adjustment factors",
            parameters_model=EtfAdjustmentSyncParameters,
            handler=sync_etf_adjustment_full_task,
        )
    )
    registry.register(
        TaskDefinition(
            key="data.sync_etf_adjustment_reconciliation",
            name="ETF复权因子近期校验",
            english_name="Reconcile recent Tushare ETF adjustment factors",
            parameters_model=EtfAdjustmentReconciliationParameters,
            handler=sync_etf_adjustment_reconciliation_task,
        )
    )
