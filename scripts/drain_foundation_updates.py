"""Finish fixed update batches in their old runtime before a code deployment.

Run inside the still-deployed backend (or its verified archived image), with
this script mounted read-only and /app on PYTHONPATH. Pause the owning local
formalization schedules through the admin API first and wait for their queued
and running invocations to settle. Supplier collection remains enabled.

A dry run is the default. --publish authorizes bounded advancement through the
existing pipeline's archive, lease, reconciliation, issue and head gates. This
operator driver never changes an execution manifest, cancels an unfinished
work, discovers new observations, or calls a supplier. Repeat until drained;
exit code 2 means deployment must wait or the reported problem needs repair.
"""
import argparse
import sys
from pathlib import Path
from uuid import UUID

# The script can be mounted outside /app. Import the installed application,
# never a copied or reconstructed substitute for the archived implementation.
sys.path.insert(0, str(Path.cwd()))

import app.models  # Register the complete ORM graph before reading work edges.
from sqlalchemy import select, exists, func
from sqlalchemy.orm import Session, aliased
from app.data_foundation.batch_models import Batch, BatchWork
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.catalog import now
from app.data_foundation.models import SourceRef
from app.data_foundation.record_pipeline import advance_job
from app.data_foundation.work_models import Work, Release, Candidate
from app.scheduling.models import ScheduledTask, TaskRun
from app.data_ingestion.tonghuashun.contracts import DATASETS

TASK_TYPE = 'foundation.formalize_local_updates'


def paused_domains(session, execution_id, runtime_digest):
    """Reject live schedulers and a mistaken runtime before touching a batch."""
    tasks = list(session.scalars(select(ScheduledTask).where(
        ScheduledTask.task_type == TASK_TYPE,
        ScheduledTask.parameters['execution_id'].astext == str(execution_id))))
    if not tasks or any(task.state != 'paused' for task in tasks):
        raise FoundationError('UPDATE_TASKS_ACTIVE', '旧执行版本的持续正式化任务必须全部暂停后才能收尾。')
    if any(task.parameters.get('runtime_digest') != runtime_digest for task in tasks):
        raise FoundationError('RUNTIME_MISMATCH', '收尾镜像与已暂停任务的固定运行依赖不一致。')
    active = session.scalar(select(func.count()).select_from(TaskRun).where(
        TaskRun.task_id.in_([task.id for task in tasks]), TaskRun.status.in_(['queued', 'running'])))
    if active:
        raise FoundationError('UPDATE_RUNS_ACTIVE', '持续正式化仍有排队或运行中的调用，暂不能接续部署。')
    return {task.parameters['native_dataset'] for task in tasks}


def pending_batches(session, execution_id, domains):
    """Only scheduled update batches are eligible; full backfills stay live."""
    link = aliased(BatchWork)
    published = exists(select(1).select_from(link).join(Release, Release.work_id == link.work_id)
        .where(link.batch_id == Batch.id, Release.status == 'published'))
    return list(session.execute(select(Batch.id, Work.id.label('origin_id'), SourceRef.dataset,
        SourceRef.observed_at).join(BatchWork, BatchWork.batch_id == Batch.id)
        .join(Work, Work.id == BatchWork.work_id).join(SourceRef, SourceRef.id == Work.source_ref_id)
        .where(Batch.event_key.startswith('record-update:'), Work.kind == 'A',
            Work.execution_id == execution_id, SourceRef.source == 'tonghuashun',
            SourceRef.dataset.in_(domains), ~published)
        .order_by(Batch.created_at, Batch.id)))


def drain(engine, *, execution_id, runtime_digest, max_batches=2, steps=10,
          publish=False, archive_root='/app/data/foundation-runtime-archives'):
    """Advance a bounded slice without replacing any existing normalization."""
    if type(max_batches) is not int or not 1 <= max_batches <= 20:
        raise ValueError('max_batches must be 1 to 20')
    if type(steps) is not int or not 1 <= steps <= 100 or type(publish) is not bool:
        raise ValueError('steps must be 1 to 100 and publish must be explicit')
    with Session(engine) as session:
        domains = paused_domains(session, execution_id, runtime_digest)
        pending = pending_batches(session, execution_id, domains)
    results = []
    for batch in pending[:max_batches] if publish else []:
        # Recheck each boundary so an operator resuming a schedule cannot be
        # silently treated as a successful deployment drain. The pipeline's
        # per-batch mutex additionally serializes any racing driver calls.
        with Session(engine) as session:
            paused_domains(session, execution_id, runtime_digest)
        result = advance_job(engine, batch.id, runtime_digest=runtime_digest,
            steps=steps, publish=True, archive_root=archive_root, lock_wait_seconds=5)
        with Session(engine) as session:
            counts = dict(session.execute(select(Candidate.readiness, func.count())
                .where(Candidate.work_id == batch.origin_id).group_by(Candidate.readiness)).all())
        stamp = batch.observed_at.date().isoformat()
        result.update(dataset=batch.dataset, origin_id=batch.origin_id, counts=counts,
            message=f'{DATASETS[batch.dataset].name} 本地正式化部署前收尾，观察日期 {stamp} 至 {stamp}，'
                f'执行 {result["steps"]} 个单元，合格 {counts.get("ready", 0)} 个，隔离 {counts.get("quarantined", 0)} 个；'
                + ('正式发布检查点已推进。' if result['status'] == 'published' else '正式发布检查点未推进，原工作进度保留。'))
        results.append(result)
    with Session(engine) as session:
        domains = paused_domains(session, execution_id, runtime_digest)
        remaining = pending_batches(session, execution_id, domains)
    return dict(checked_at=now(), execution_id=execution_id, status='drained' if not remaining else 'pending',
        pending_batches=len(remaining), published=sum(item['status'] == 'published' for item in results),
        quarantined=sum(item['counts'].get('quarantined', 0) for item in results), items=results,
        message=f'本地持续正式化部署前收尾，本次发布 {sum(item["status"] == "published" for item in results)} 个批次，'
            f'尚有 {len(remaining)} 个批次未完成；' + ('旧版本工作已收尾，可继续核对部署条件。' if not remaining
                else '尚不能切换执行版本，须沿用原镜像和检查点继续处理。'))


def main():
    from app.db.session import get_engine
    parser = argparse.ArgumentParser(description='沿旧执行版本检查点收尾持续正式化批次；不取消或重建工作。')
    parser.add_argument('--execution-id', type=UUID, required=True)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--max-batches', type=int, default=2)
    parser.add_argument('--steps', type=int, default=10)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    try:
        result = drain(get_engine(), execution_id=args.execution_id, runtime_digest=args.runtime,
            max_batches=args.max_batches, steps=args.steps, publish=args.publish)
    except FoundationError as exc:
        print(encode(dict(status='blocked', code=exc.code, message=str(exc))), flush=True)
        return 2
    print(encode(result), flush=True)
    return 0 if result['status'] == 'drained' else 2


if __name__ == '__main__':
    raise SystemExit(main())
