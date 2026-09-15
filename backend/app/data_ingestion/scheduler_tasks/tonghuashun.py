"""Independently configurable Tonghuashun tasks, using source admission gates."""

from functools import partial
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from app.core.config import get_settings
from app.data_ingestion.clients.tonghuashun import TonghuashunClient
from app.data_ingestion.tonghuashun.contracts import CollectionParameters, DATASETS
from app.data_ingestion.tonghuashun.service import collect
from app.db.session import get_engine
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


def execute(dataset: str, context: TaskContext, parameters: CollectionParameters):
    from app.data_ingestion.tonghuashun.control import CollectionControl, CollectionYield, active_control
    engine = get_engine()
    monitor = CollectionControl(engine, context.run_id,
        max_requests=parameters.max_requests, max_seconds=parameters.max_seconds)
    token = active_control.set(monitor)
    try:
        result = collect(dataset, CollectionParameters.model_validate(parameters.model_dump()),
                         TonghuashunClient.from_settings(get_settings()), engine)
        monitor.emit(stage="本批结束")
        return result
    except CollectionYield as exc:
        stopped = exc.reason == "stopped"
        stage = "本批已安全停止" if stopped else "本批预算已用完"
        detail = monitor.detail
        message = (f"{DATASETS[dataset].name}{stage}：日期范围 {parameters.start_date or '接口可用起点'} 至 {parameters.end_date or '接口可用终点'}，"
            f"成功 {detail['succeeded']} 个，失败 {detail['failed']} 个，拉取 {detail['fetched_rows']} 条，变更 {detail['changed']} 条；"
            + ("已完成对象的完成标记已推进；" if detail['succeeded'] else "本次未推进对象完成标记；")
            + "未完成范围已保留断点，后续运行继续采集。")
        monitor.emit(stage=stage)
        return {"message": message, "yield_reason": exc.reason, "collection_progress": monitor.detail}
    finally:
        active_control.reset(token)


class DatasetParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_common_constraints(self):
        CollectionParameters.model_validate(self.model_dump())
        return self


def parameters_model(spec):
    """Expose only parameters the selected endpoint can actually honor."""
    keys = ["refresh_today", "batch_size", "max_requests", "max_seconds"]
    if spec.kind.startswith("m3_") or spec.kind == "dump":
        keys.append("mode")
        if spec.assets or spec.kind == "m3_manager":
            keys.append("subjects")
        if spec.assets:
            keys.append("asset_types")
        if spec.kind in ("m3_date", "m3_pool", "m3_series"):
            keys.extend(["start_date", "end_date"])
        if spec.kind == "m3_quota":
            keys.append("quota_tabs")
        return build_parameters_model(spec, keys)

    if spec.kind not in ("calendar", "directory"):
        keys.append("subjects")
    if spec.kind not in ("calendar", "company", "manager", "index_catalog"):
        keys.append("asset_types")
    if spec.kind not in ("calendar", "directory", "index_catalog"):
        keys.append("mode")
    if spec.kind in ("bars", "financials", "indicators", "reports"):
        keys.extend(["start_date", "end_date"])
    return build_parameters_model(spec, keys)


def build_parameters_model(spec, keys):
    fields = {}
    for key in keys:
        field = deepcopy(CollectionParameters.model_fields[key])
        if key == "batch_size":
            field.default = 5 if spec.kind == "reports" else 20
        if key == "asset_types":
            field.default = list(spec.assets)
        annotation = list[Literal[tuple(spec.assets)]] if key == "asset_types" else field.annotation
        if key == "mode" and spec.kind.startswith("m3_") and spec.kind != "m3_series":
            annotation = Literal["incremental", "backfill"]
        if key == "subjects" and spec.key in ("anomaly_stock", "rank_trend"):
            annotation, field = list[str], Field(min_length=1, max_length=10000)
        fields[key] = (annotation, field)
    return create_model(f"Tonghuashun_{spec.key}_Parameters", __base__=DatasetParameters, **fields)


def register_tasks(registry: TaskRegistry):
    for spec in DATASETS.values():
        registry.register(TaskDefinition(key=f"data.ths.{spec.key}", name=spec.name,
            english_name=spec.english_name, source_key="tonghuashun",
            parameters_model=parameters_model(spec), handler=partial(execute, spec.key)))
