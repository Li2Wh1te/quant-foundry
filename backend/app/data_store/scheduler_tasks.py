"""One current local update task using the same bounded D02 pipeline as CLI."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator
import time
from sqlalchemy.orm import Session
import structlog

from app.core.config import get_settings
from app.db.session import get_engine
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry

from .adapters.registry import ENTRIES
from .adapters.canonical import NativeInputError
from .availability import require_local_update_ready
from .errors import DataStoreError
from .local_sources import NativeSources, SourceLimits
from .pipeline import PipelineOptions, run_local
from .storage import CurrentStore
from .updates import FATAL
from .continuation import disposition


logger = structlog.get_logger(__name__)
BUSINESS = {entry.spec.name: entry for entry in ENTRIES if entry.business}
BUSINESS_BY_ID = {entry.id: entry.spec.name for entry in ENTRIES if entry.business}
TASK_KEY = "data_store.update_local"
STATE_NAMES = {
    "processed": "处理完成", "processed_with_issues": "处理完成但有问题",
    "empty": "确认当前为空", "incomplete": "处理未完成",
    "updated": "本次已更新", "unchanged": "本次无变化", "continued": "待续作",
    "backoff": "退避中，本次未尝试", "deferred": "预算延后，本次未尝试", "failed": "本次失败",
    "blocked": "来源未变，等待新证据",
}


class LocalUpdateParameters(BaseModel):
    """Dataset names are current catalog identifiers, never legacy run IDs."""

    datasets: list[str] = Field(default_factory=list, max_length=60)
    maximum_passes: int = Field(default=256, ge=1, le=4096)
    pass_seconds: int = Field(default=300, ge=1, le=3600)
    pipeline_spill_bytes: int | None = Field(default=None, ge=1, le=64*1024**3)

    @model_validator(mode="after")
    def validate_datasets(self) -> "LocalUpdateParameters":
        if len(self.datasets) != len(set(self.datasets)) or any(
            dataset not in BUSINESS for dataset in self.datasets
        ):
            raise ValueError("datasets must be unique current catalog identifiers")
        return self


def _task_result(results, *, global_stop=False):
    """Persist bounded execution facts without promoting domain coverage."""
    global_stop = global_stop or any(r.get('outcome')=='backoff' and
        (r.get('refresh') or {}).get('reason') in FATAL for r in results)
    complete = bool(results) and all(r.get('complete') and r.get('qualified') for r in results)
    pending = sum(not (r.get('complete') and r.get('qualified')) for r in results)
    states = [(r.get('refresh') or {}).get('continuation') or disposition(r, now=time.time()) for r in results]
    eligible = [state for state in states if state.get('eligible')]
    counters = ('source_rows','normalized_units','input_failures','committed_partitions',
                'new_issues','resolved','acknowledged_ranges','range_batches','budget_units')
    totals = {key: sum(r.get(key, 0) for r in results) for key in counters}
    advanced = bool(totals['committed_partitions'] or totals['acknowledged_ranges'])
    return dict(totals, entries=len(results), complete=complete, pending=pending,
        checkpoint_advanced=advanced,
        message=(f"本地数据更新{'完成' if complete else '进度及限制已保存'}：涉及 {len(results)} 类数据，"
                 f"起止日期按本地捕获范围处理，读取 {totals['source_rows']} 条、提交 {totals['committed_partitions']} 个分区、"
                 f"确认 {totals['acknowledged_ranges']} 个来源范围，新增问题 {totals['new_issues']} 项、解决 {totals['resolved']} 项，"
                 + ('断点已推进。' if advanced else '本次断点未推进。')
                 + (f"仍有 {pending} 类未合格或待续作。" if pending else '')),
        entries_results=[{key: r.get(key) for key in ('entry_id','complete','qualified','outcome','reason',
            'last_partition','last_generation','refresh',*counters)} for r in results],
        continuation={'version':1,'eligible':bool(eligible) and not global_stop,
            'available_at':min((s['available_at'] for s in eligible),default=None),
            'global_stop':global_stop})


def update_local(context: TaskContext, parameters: LocalUpdateParameters) -> dict:
    engine = get_engine()
    settings = get_settings()
    entries = ([BUSINESS[dataset] for dataset in parameters.datasets]
               if parameters.datasets else list(BUSINESS.values()))
    with Session(engine) as session:
        availability = require_local_update_ready(session)
        # An unsealed source read cannot resume after its deadline. Honor the
        # validated plan bounds during reset_done as well; the shared dispatcher
        # still enforces entry/call deadlines and the unchanged store budgets.
        maximum_passes = parameters.maximum_passes
        pass_seconds = parameters.pass_seconds
        with CurrentStore(
            engine, settings.data_store_root,
            cursor_key=settings.cursor_signing_key.get_secret_value().encode(),
            initialize=availability.fresh_install,
        ) as store:
            sources = NativeSources(
                engine, limits=SourceLimits(pass_seconds=pass_seconds)
            )
            logger.info(
                'data_store_local_update_admitted',
                message=f"本地数据更新：所选 {len(entries)} 类数据的起止日期按本地捕获范围处理，读取 0 条、提交 0 个分区，已通过维护准入，本次断点尚未推进。",
                task_id=str(context.task_id), run_id=str(context.run_id),
                maintenance_phase=availability.phase,
                configured_maximum_passes=parameters.maximum_passes,
                effective_maximum_passes=maximum_passes, effective_pass_seconds=pass_seconds,
            )
            try:
                results = run_local(
                    store, sources, entries=entries,
                    options=PipelineOptions(
                        mode="update", maximum_passes=maximum_passes,
                        pass_seconds=pass_seconds,
                        pipeline_spill_bytes=parameters.pipeline_spill_bytes,
                    ),
                )
            except Exception as error:
                partial=getattr(error,'results',[])
                code=getattr(error,'code',type(error).__name__)
                logger.error(
                    "data_store_local_update_failed",
                    message=(
                        f"本地数据更新：数据类型为所选 {len(entries)} 类，适用起始日期和结束日期均未限定，"
                        f"已处理 {len(partial)} 项、读取 {sum(r.get('source_rows',0) for r in partial)} 条，"
                        f"全局停止原因 {code}，已提交分区仍按当前断点保存，任务未完成。"
                    ),
                    task_id=str(context.task_id), run_id=str(context.run_id),
                    task_type=TASK_KEY, selected_entries=[entry.id for entry in entries],
                    error_type=code, failed=1, entries=partial,
                )
                payload = _task_result(partial, global_stop=True)
                if isinstance(error,(DataStoreError,NativeInputError)):
                    error.task_result = payload
                    raise
                failure = RuntimeError('DATA_STORE_UPDATE_ABORTED:'+type(error).__name__)
                failure.task_result = payload
                raise failure from None
    for result in results:
        logger.info(
            "data_store_local_update",
            message=(
                f"本地数据更新：数据类型 {BUSINESS_BY_ID[result['entry_id']]}，"
                "适用起始日期和结束日期均未限定（按该类本地来源范围处理），"
                f"读取 {result.get('source_rows', 0)} 条、规范化 "
                f"{result.get('normalized_units', 0)} 项、提交 "
                f"{result.get('committed_partitions', 0)} 个分区，"
                f"新增问题 {result.get('new_issues', 0)} 项，"
                f"结果为{STATE_NAMES.get(result.get('outcome'), '待核对')}，"
                + ("本次处理游标已推进。" if result.get('committed_partitions') or result.get('acknowledged_ranges') else "本次处理游标未推进。")
            ),
            task_id=str(context.task_id), run_id=str(context.run_id),
            task_type=TASK_KEY, entry_id=result["entry_id"],
            source_rows=result.get("source_rows", 0),
            normalized_units=result.get('normalized_units',0), input_failures=result.get('input_failures',0),
            new_issues=result.get('new_issues',0), resolved=result.get('resolved',0),
            acknowledged_ranges=result.get('acknowledged_ranges',0), budget_units=result.get('budget_units',0),
            committed_partitions=result.get("committed_partitions", 0),
            complete=result.get("complete", False),
            qualified=result.get("qualified", False),
            outcome=result.get('outcome'), attempted=result.get('attempted'), refresh=result.get('refresh'),
            source_scanned=result.get('source_scanned',False),
            source_metrics=result.get('source_metrics'),incremental=result.get('incremental'),
        )
    complete = all(row.get("complete") and row.get("qualified") for row in results)
    pending = [row for row in results if not (row.get('complete') and row.get('qualified'))]
    ordinary_pending = all(
        row.get('outcome') in {'continued', 'deferred', 'backoff'}
        and row.get('reason') in {
            'PASS_BUDGET_EXCEEDED', 'NATIVE_RANGES_PENDING', 'LOCAL_UPDATE_BUDGET_EXCEEDED',
            'SCRATCH_ADMISSION_DEFERRED', 'WRITE_ADMISSION_DEFERRED', 'RETRY_BACKOFF',
        }
        # Pre-existing whole-domain restrictions stay in the result/read gate.
        # They do not turn an otherwise valid bounded continuation into a new
        # execution failure. New invalid input and actual errors still fail.
        and not any(row.get(key, 0) for key in ('new_issues', 'input_failures'))
        and not (row.get('overflow_restriction') or {}).get('blocking_objects')
        and not (row.get('outcome') == 'backoff' and row.get('refresh', {}).get('reason') in FATAL)
        for row in pending
    )
    if not complete and not ordinary_pending:
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
        failure = RuntimeError("DATA_STORE_UPDATE_INCOMPLETE")
        failure.task_result = _task_result(results)
        raise failure
    if pending:
        logger.info(
            'data_store_local_update_pending',
            message=(f"本次本地更新已保存处理进度，涉及 {len(results)} 类数据，"
                     f"其中 {len(pending)} 类待续作或等待预算；完整覆盖仍未完成。"),
            task_id=str(context.task_id), run_id=str(context.run_id), task_type=TASK_KEY,
            entries=len(results), pending=len(pending), complete=False,
        )
    return _task_result(results)


def register_tasks(registry: TaskRegistry) -> None:
    registry.register(TaskDefinition(
        key=TASK_KEY,
        name="本地数据更新",
        english_name="Local Data Update",
        parameters_model=LocalUpdateParameters,
        handler=update_local,
    ))
