"""Explicitly configured local updates; never starts or enables vendor polling."""
from __future__ import annotations
import json
from typing import TYPE_CHECKING
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator
import structlog

from app.data_foundation.canonical import encode
from app.data_foundation.record_adapters import SOURCE_DATASETS
from app.data_foundation.record_updates import advance_updates
from app.data_ingestion.tonghuashun.contracts import DATASETS
from app.db.session import get_engine
if TYPE_CHECKING:
    from app.scheduling.registry import TaskContext, TaskRegistry

logger = structlog.get_logger(__name__)


class LocalUpdateParameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    native_dataset: str = Field(description='已完成固定全量发布的同花顺本地领域键')
    backfill_campaign_id: UUID
    execution_id: UUID
    runtime_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    source_limit: int = Field(default=4, ge=2, le=20)
    steps_per_source: int = Field(default=2, ge=1, le=10)

    @field_validator('native_dataset')
    @classmethod
    def supported_domain(cls, value):
        if ('tonghuashun', value) not in SOURCE_DATASETS:
            raise ValueError('该领域尚无正式化适配器。')
        return value


class FoundationUpdateError(RuntimeError):
    """A classified Chinese summary; source details stay in durable visit rows."""


def summarize(native_dataset, result):
    """Do not confuse a bounded step with completion of a source or a domain."""
    items = result.get('items', [])
    dates = sorted(str(item['observed_at'])[:10] for item in items)
    start, end = (dates[0], dates[-1]) if dates else ('无新增观察', '无新增观察')
    published = sum(item['status'] == 'published' for item in items)
    failed = sum(item['status'] in ('failed', 'dependency_missing', 'cancelled', 'superseded', 'unexplained') for item in items)
    steps = sum(item.get('steps', 0) for item in items)
    quarantined = sum(item.get('candidate_counts', {}).get('quarantined', 0) for item in items)
    busy = sum(item['status'] == 'busy' for item in items)
    name = DATASETS[native_dataset].name
    detail = ('固定全量发布尚未完成，等待范围结算；' if result['status'] == 'waiting_backfill'
              else '同领域执行锁忙，等待重试；' if result['status'] == 'busy' else '')
    message = (f'{name} 本地正式化更新，观察日期 {start} 至 {end}，{detail}'
        f'选中 {len(items)} 个，发布 {published} 个，失败 {failed} 个，隔离 {quarantined} 条，等待工作锁 {busy} 个，执行 {steps} 个单元；'
        + ('正式发布检查点已推进。' if published else '正式发布检查点未推进，已提交工作进度保留。'))
    return dict(**result, selected=len(items), published=published, failed=failed, quarantined=quarantined, busy=busy,
                observation_start=start, observation_end=end, message=message)


def execute(context: TaskContext, parameters: LocalUpdateParameters):
    # This task intentionally has no supplier source_key: reading committed
    # local evidence must also work while a provider is disabled or offline.
    result = summarize(parameters.native_dataset, advance_updates(get_engine(), **parameters.model_dump()))
    result = json.loads(encode(result))
    needs_attention = result['failed'] or result['quarantined']
    event = 'foundation_updates_failed' if needs_attention else 'foundation_updates_advanced'
    result['event'] = event
    log = logger.error if needs_attention else logger.info
    log(event, **{key: value for key, value in result.items() if key != 'event'},
        task_id=str(context.task_id), run_id=str(context.run_id), task_type=context.task_type)
    if needs_attention:
        # The run must be red when any source failed. Complete details remain
        # available in structured logs and per-source durable visit receipts.
        raise FoundationUpdateError(result['message'])
    return result


def register_tasks(registry: TaskRegistry):
    from app.scheduling.registry import TaskDefinition
    registry.register(TaskDefinition(key='foundation.formalize_local_updates',
        name='本地数据持续正式化', english_name='Local Data Formalization Updates',
        parameters_model=LocalUpdateParameters, handler=execute))
