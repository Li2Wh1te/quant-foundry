"""Scheduler task registration for Tushare ETF reference-data synchronization."""

from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field
import structlog

from app.core.config import get_settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.services.etf import sync_etf_basics
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


logger = structlog.get_logger(__name__)


class EtfBasicSyncParameters(BaseModel):
    """Validated scheduler parameters for a complete ETF reference-data refresh."""

    model_config = ConfigDict(extra="forbid")

    request_interval_ms: int | None = Field(default=None, ge=0, le=60_000)


def sync_etf_basics_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, object]:
    """Run one full ETF reference-data synchronization task."""
    if not isinstance(parameters, EtfBasicSyncParameters):
        raise TypeError("unexpected parameters model for data.sync_etf_basics")
    result = sync_etf_basics(
        TushareClient(get_settings()),
        request_interval_ms=parameters.request_interval_ms,
    )
    payload = asdict(result)
    payload["refreshed_at"] = result.refreshed_at.isoformat()
    logger.info(
        "etf_basic_sync_completed",
        message=(
            "ETF 基础信息采集完成：全部上市状态，适用日期不适用，"
            f"拉取 {payload['received']} 条，入库变更 {payload['changed']} 条，"
            f"未变更 {payload['unchanged']} 条，游标已推进至 "
            f"{payload['refreshed_at']}。"
        ),
        start_date=None,
        end_date=None,
        **payload,
    )
    return payload


def register_tasks(registry: TaskRegistry) -> None:
    """Register Tushare ETF reference-data synchronization with the scheduler."""
    registry.register(
        TaskDefinition(
            key="data.sync_etf_basics",
            name="ETF基础信息采集",
            english_name="Sync Tushare ETF basics",
            parameters_model=EtfBasicSyncParameters,
            handler=sync_etf_basics_task,
        )
    )
