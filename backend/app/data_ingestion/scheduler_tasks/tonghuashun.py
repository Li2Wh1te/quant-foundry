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
    return collect(dataset, CollectionParameters.model_validate(parameters.model_dump()),
                   TonghuashunClient.from_settings(get_settings()), get_engine())


class DatasetParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_common_constraints(self):
        CollectionParameters.model_validate(self.model_dump())
        return self


def parameters_model(spec):
    """Expose only parameters the selected endpoint can actually honor."""
    keys = ["refresh_today", "batch_size"]
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
