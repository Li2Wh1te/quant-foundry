"""One current local update task using the same bounded D02 pipeline as CLI."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
import structlog

from app.core.config import get_settings
from app.db.session import get_engine
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry

from .adapters.registry import ENTRIES
from .adapters.canonical import NativeInputError
from .availability import require_ready
from .errors import DataStoreError
from .local_sources import NativeSources, SourceLimits
from .pipeline import PipelineOptions, run_local
from .storage import CurrentStore


logger = structlog.get_logger(__name__)
BUSINESS = {entry.spec.name: entry for entry in ENTRIES if entry.business}
BUSINESS_BY_ID = {entry.id: entry.spec.name for entry in ENTRIES if entry.business}
TASK_KEY = "data_store.update_local"
STATE_NAMES = {
    "processed": "处理完成", "processed_with_issues": "处理完成但有问题",
    "empty": "确认当前为空", "incomplete": "处理未完成",
}


class LocalUpdateParameters(BaseModel):
    """Dataset names are current catalog identifiers, never legacy run IDs."""

    datasets: list[str] = Field(default_factory=list, max_length=60)
    maximum_passes: int = Field(default=256, ge=1, le=4096)
    pass_seconds: int = Field(default=300, ge=1, le=3600)

    @model_validator(mode="after")
    def validate_datasets(self) -> "LocalUpdateParameters":
        if len(self.datasets) != len(set(self.datasets)) or any(
            dataset not in BUSINESS for dataset in self.datasets
        ):
            raise ValueError("datasets must be unique current catalog identifiers")
        return self


def update_local(context: TaskContext, parameters: LocalUpdateParameters) -> dict:
    engine = get_engine()
    with Session(engine) as session:
        availability = require_ready(session)
    settings = get_settings()
    entries = ([BUSINESS[dataset] for dataset in parameters.datasets]
               if parameters.datasets else list(BUSINESS.values()))
    with CurrentStore(
        engine, settings.data_store_root,
        cursor_key=settings.cursor_signing_key.get_secret_value().encode(),
        initialize=availability.fresh_install,
    ) as store:
        sources = NativeSources(
            engine, limits=SourceLimits(pass_seconds=parameters.pass_seconds)
        )
        try:
            results = run_local(
                store, sources, entries=entries,
                options=PipelineOptions(
                    mode="update", maximum_passes=parameters.maximum_passes,
                    pass_seconds=parameters.pass_seconds,
                ),
            )
        except (DataStoreError, NativeInputError) as error:
            logger.error(
                "data_store_local_update_failed",
                message=(
                    f"本地数据更新：数据类型为所选 {len(entries)} 类，适用起始日期和结束日期均未限定，"
                    f"本次失败 1 项（{error.code}），已提交分区仍按当前断点保存，任务未完成。"
                ),
                task_id=str(context.task_id), run_id=str(context.run_id),
                task_type=TASK_KEY, selected_entries=[entry.id for entry in entries],
                error_type=error.code, failed=1,
            )
            raise
    for result in results:
        logger.info(
            "data_store_local_update",
            message=(
                f"本地数据更新：数据类型 {BUSINESS_BY_ID[result['entry_id']]}，"
                "适用起始日期和结束日期均未限定（扫描该类本地来源），"
                f"读取 {result.get('source_rows', 0)} 条、规范化 "
                f"{result.get('normalized_units', 0)} 项、提交 "
                f"{result.get('committed_partitions', 0)} 个分区，"
                f"新增问题 {result.get('new_issues', 0)} 项，"
                f"结果为{STATE_NAMES.get(result.get('state'), '待核对')}，当前断点已保存。"
            ),
            task_id=str(context.task_id), run_id=str(context.run_id),
            task_type=TASK_KEY, entry_id=result["entry_id"],
            source_rows=result.get("source_rows", 0),
            committed_partitions=result.get("committed_partitions", 0),
            complete=result.get("complete", False),
            qualified=result.get("qualified", False),
        )
    complete = all(row.get("complete") and row.get("qualified") for row in results)
    if not complete:
        logger.error(
            "data_store_local_update_incomplete",
            message=(
                f"本地数据更新：涉及 {len(results)} 类数据，适用起始日期和结束日期均未限定，"
                f"读取 {sum(row.get('source_rows', 0) for row in results)} 条、提交 "
                f"{sum(row.get('committed_partitions', 0) for row in results)} 个分区，"
                f"未合格 {sum(not (row.get('complete') and row.get('qualified')) for row in results)} 类，"
                "已提交断点保留，任务未完成。"
            ),
            task_id=str(context.task_id), run_id=str(context.run_id),
            task_type=TASK_KEY, entries=len(results),
            failed=sum(not (row.get("complete") and row.get("qualified")) for row in results),
        )
        raise RuntimeError("DATA_STORE_UPDATE_INCOMPLETE")
    return {
        "message": (
            f"本地数据更新完成：涉及 {len(results)} 类数据，适用起始日期和结束日期均未限定，读取 "
            f"{sum(row.get('source_rows', 0) for row in results)} 条，提交 "
            f"{sum(row.get('committed_partitions', 0) for row in results)} 个分区，"
            "所有处理断点已保存。"
        ),
        "entries": len(results),
        "source_rows": sum(row.get("source_rows", 0) for row in results),
        "committed_partitions": sum(row.get("committed_partitions", 0) for row in results),
        "complete": True,
    }


def register_tasks(registry: TaskRegistry) -> None:
    registry.register(TaskDefinition(
        key=TASK_KEY,
        name="本地数据更新",
        english_name="Local Data Update",
        parameters_model=LocalUpdateParameters,
        handler=update_local,
    ))
