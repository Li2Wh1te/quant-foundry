"""Scheduler registrations for whole-market ETF adjustment-factor workflows."""

from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
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
    )
    return _result_payload_and_log(
        result,
        event="etf_adjustment_incremental_sync_completed",
        task_label="ETF 复权因子增量",
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
    )
    return _result_payload_and_log(
        result,
        event="etf_adjustment_full_sync_completed",
        task_label="ETF 复权因子全量",
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
    )
    return _result_payload_and_log(
        result,
        event="etf_adjustment_reconciliation_completed",
        task_label="ETF 复权因子近期校验",
    )


def _result_payload_and_log(
    result: Any, *, event: str, task_label: str
) -> dict[str, Any]:
    """Serialize the result and emit the required Chinese operator summary."""
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
            f"游标状态为 {payload['synced_through_date'] or '未使用'}。"
        ),
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
