"""Restartable, bounded typed-record publication for local update orchestration.

Every invocation reconstructs progress from durable work and batch records.
It never scans suppliers, substitutes a latest source, retries terminal failures
silently, or bypasses the existing archive, lease, reconciliation and head gates.
Scheduler discovery can therefore retry a source after a process interruption
without creating a second publication or keeping an in-memory cursor.
"""
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.data_foundation.batch_models import Batch, BatchControl, BatchWork
from app.data_foundation.batches import batch_document, register_batch, attach_work, set_controls
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.catalog import lock_key
from app.data_foundation.execution import verify_execution
from app.data_foundation.record_work import create_normalization, create_governance, register_catalog
from app.data_foundation.record_adapters import dataset_for
from app.data_foundation.reconciliation import reconcile_batch
from app.data_foundation.models import SourceRef
from app.data_foundation.work_models import Work, Head, IssueScope, Release, Attempt
from app.data_foundation.worker import run_once

TERMINAL_FAILURES = {'failed', 'dependency_missing', 'cancelled', 'superseded'}


def register_job(session, *, source_ref_id, execution_id, runtime_digest,
                 archive_root='/app/data/foundation-runtime-archives', preserve_head=False, source_revision=None):
    """Bind one exact source to a verified execution and an initially live batch.

    Only a newly registered batch is unpaused. Calling again after an operator
    pause preserves those controls and returns the same durable batch identity.
    The caller owns the transaction, including rollback on verification failure.
    """
    if not isinstance(preserve_head, bool):
        raise ValueError('Head preservation must be an explicit boolean')
    if source_revision is not None:
        if preserve_head or type(source_revision) is not int or source_revision < 1:
            raise ValueError('Invalid current source revision')
        from app.data_ingestion.models.tonghuashun import TonghuashunCollectionState as State
        source = session.get(SourceRef, source_ref_id)
        if source is None or source.representation != 'ths_observation':
            raise FoundationError('SCOPE_MISMATCH', '当前来源指针仅适用于同花顺固定观察。')
        state = session.scalar(select(State).where(State.dataset == source.dataset, State.subject == source.subject,
            State.variant == source.variant).with_for_update(read=True).execution_options(populate_existing=True))
        if state is None or state.revision != source_revision or state.observation_id != source.observation_id:
            raise FoundationError('SOURCE_CONTEXT_CHANGED', '当前来源指针已变化，须重新发现并固定处理方式。')
    origin, _ = create_normalization(session, source_ref_id=source_ref_id, execution_id=execution_id)
    verify_execution(session, origin, runtime_digest, archive_root)
    # Preserve the original normal-job identity. Historical mode is fixed in
    # the immutable batch event, so a resumed call cannot silently activate it.
    prefix = 'record-update:history:' if preserve_head else 'record-update:'
    if source_revision is not None:
        prefix = f'record-update:current:{source_revision}:'
    event = prefix + digest('record-update-source-v1', [source_ref_id, execution_id])
    lock_key(session, 'batch', event)
    existing = session.scalar(select(Batch).where(Batch.event_key == event))
    if existing is not None:
        return existing
    document, fingerprint = batch_document(session, scope_key=origin.scope_key,
        source_ids=[source_ref_id], start='0001-01-01', end='9999-12-31', purpose='catchup')
    batch = register_batch(session, event_key=event, document=document, expected_hash=fingerprint)
    attach_work(session, batch.id, origin.id)
    set_controls(session, batch.id, pause_a=False, pause_b=False)
    return batch


def _prepare(session, batch_id, *, publish, runtime_digest, archive_root):
    """Choose at most one step under the batch control transaction lock."""
    lock_key(session, 'batch-control', str(batch_id))
    batch = session.get(Batch, batch_id)
    if batch is None or not batch.event_key.startswith('record-update:'):
        raise FoundationError('BATCH_UNAVAILABLE', '持续正式化批次不存在或不属于该执行器。')
    control = session.get(BatchControl, batch_id)
    works = list(session.scalars(select(Work).join(BatchWork, BatchWork.work_id == Work.id)
        .where(BatchWork.batch_id == batch_id)))
    origins, governance = ([work for work in works if work.kind == kind] for kind in ('A', 'B'))
    active_governance = [work for work in governance if work.status != 'superseded']
    if len(origins) != 1 or len(active_governance) > 1:
        raise FoundationError('SCOPE_MISMATCH', '持续正式化批次的固定工作集合无效。')
    origin = origins[0]
    work = active_governance[0] if active_governance else origin
    if governance and not active_governance:
        previous = max(governance, key=lambda row: (row.created_at, str(row.id)))
        attempt = session.scalar(select(Attempt).where(Attempt.work_id == previous.id,
            Attempt.outcome == 'superseded').order_by(Attempt.epoch.desc()).limit(1))
        # Only a changed head/issue context can be replanned automatically.
        # A changed source pointer requires discovery to choose a new fixed
        # current revision or a separate historical job; inputs never mutate.
        if attempt is None or attempt.error_code not in ('HEAD_CHANGED', 'ISSUE_CONTEXT_CHANGED'):
            return dict(status='superseded', work_id=previous.id, batch_id=batch_id)
    if work.status in TERMINAL_FAILURES:
        return dict(status=work.status, work_id=work.id, batch_id=batch_id)
    if work.kind == 'B' and work.status == 'succeeded':
        release = session.scalar(select(Release).where(Release.work_id == work.id, Release.status == 'published'))
        if release is None:
            raise FoundationError('PUBLICATION_MISSING', '治理已结束但未找到对应正式发布，须定位修复。')
        return dict(status='published', work_id=work.id, batch_id=batch_id,
                    release_id=release.id, manifest_hash=release.manifest_hash,
                    head_activated=not batch.event_key.startswith('record-update:history:'))
    verify_execution(session, work, runtime_digest, archive_root)
    if (work.kind == 'A' and work.status != 'succeeded' and control.pause_a) or (work.kind == 'B' and control.pause_b):
        return dict(status='paused', work_id=work.id, batch_id=batch_id)
    if work.kind == 'A' and work.status == 'succeeded':
        if control.pause_b:
            return dict(status='paused', work_id=work.id, batch_id=batch_id)
        source = session.get(SourceRef, origin.source_ref_id)
        _, _, policy = register_catalog(session, dataset_for(source), source.source)
        head, guard = session.get(Head, origin.scope_key), session.get(IssueScope, origin.scope_key)
        work = create_governance(session, normalization_id=origin.id, execution_id=origin.execution_id,
            policy_id=policy.id, parent_release_id=head.release_id if head else None,
            expected_head_revision=head.revision if head else 0, expected_issue_epoch=guard.epoch,
            preserve_head=batch.event_key.startswith('record-update:history:'),
            source_revision=int(batch.event_key.split(':')[2]) if batch.event_key.startswith('record-update:current:') else None)
        attach_work(session, batch_id, work.id)
        if work.status == 'superseded':
            return dict(status='superseded', work_id=work.id, batch_id=batch_id)
    if work.kind == 'B' and work.status == 'awaiting_publication':
        if not publish:
            return dict(status='awaiting_publication', work_id=work.id, batch_id=batch_id)
        result = reconcile_batch(session, batch_id)
        if result.status != 'explained':
            return dict(status=result.status, work_id=work.id, batch_id=batch_id)
        set_controls(session, batch_id, pause_a=False, pause_b=False, allow_publish=True)
    if work.kind == 'B' and control.allow_publish and not publish:
        # A caller without publication authorization must not run an already
        # approved publication unit merely because another caller approved it.
        return dict(status='publication_not_authorized', work_id=work.id, batch_id=batch_id)
    return dict(status='step_ready', work_id=work.id, batch_id=batch_id)


def advance_job(engine, batch_id, *, runtime_digest, steps=1, publish=False,
                archive_root='/app/data/foundation-runtime-archives'):
    """Run a finite number of existing worker units, returning durable progress.

    A separate session advisory lock serializes drivers for this batch without
    holding a database transaction across the worker's own short transactions.
    The worker's global gate remains authoritative; contention yields 'busy'
    rather than falsely completing or skipping the source.
    """
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 100:
        raise ValueError('A pipeline invocation requires 1 to 100 worker steps')
    key = int(digest('record-update-driver', str(batch_id))[:15], 16)
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as leader:
        if not leader.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}):
            return dict(status='busy', batch_id=batch_id, steps=0)
        completed = 0
        try:
            for _ in range(steps):
                with Session(engine) as session, session.begin():
                    result = _prepare(session, batch_id, publish=publish,
                        runtime_digest=runtime_digest, archive_root=archive_root)
                if result['status'] != 'step_ready':
                    return {**result, 'steps': completed}
                if run_once(engine, work_id=result['work_id'], runtime_digest=runtime_digest,
                            archive_root=archive_root) is None:
                    return {**result, 'status': 'busy', 'steps': completed}
                completed += 1
            # Preparing the next durable step is safe even when this invocation
            # exhausts its budget; no worker unit executes beyond the bound.
            with Session(engine) as session, session.begin():
                result = _prepare(session, batch_id, publish=publish,
                        runtime_digest=runtime_digest, archive_root=archive_root)
            return {**result, 'steps': completed}
        finally:
            leader.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})


def main():
    """Expose the same bounded driver used by scheduled update handlers."""
    import argparse
    from uuid import UUID
    from sqlalchemy import func
    from app.db.session import get_engine
    from app.data_foundation.canonical import encode
    from app.data_foundation.record_schemas import schema_for
    from app.data_foundation.work_models import Candidate
    import app.models  # Register the complete ORM graph before resolving FKs.
    parser = argparse.ArgumentParser(description='固定本地来源的可恢复正式化执行器；不扫描或请求供应商。')
    parser.add_argument('command', choices=['register', 'advance'])
    parser.add_argument('--source-ref-id', type=UUID)
    parser.add_argument('--execution-id', type=UUID)
    parser.add_argument('--batch-id', type=UUID)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--steps', type=int, default=1)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--preserve-head', action='store_true', help='登记独立可读的历史发布批次，不替换当前指针')
    parser.add_argument('--source-revision', type=int, help='当前来源的固定修订号，发布前再次核对')
    args = parser.parse_args()
    engine = get_engine()
    if args.command == 'register':
        if not args.source_ref_id or not args.execution_id:
            parser.error('register requires --source-ref-id and --execution-id')
        with Session(engine) as session, session.begin():
            batch_id = register_job(session, source_ref_id=args.source_ref_id,
                execution_id=args.execution_id, runtime_digest=args.runtime,
                preserve_head=args.preserve_head, source_revision=args.source_revision).id
        result = dict(batch_id=batch_id, status='registered', steps=0)
    else:
        if not args.batch_id:
            parser.error('advance requires --batch-id')
        result = advance_job(engine, args.batch_id, runtime_digest=args.runtime,
            steps=args.steps, publish=args.publish)
    with Session(engine) as session:
        origin = session.scalar(select(Work).join(BatchWork, BatchWork.work_id == Work.id)
            .where(BatchWork.batch_id == result['batch_id'], Work.kind == 'A'))
        if origin is not None:
            source = session.get(SourceRef, origin.source_ref_id)
            counts = dict(session.execute(select(Candidate.readiness, func.count())
                .where(Candidate.work_id == origin.id).group_by(Candidate.readiness)).all())
            stamp = source.observed_at.isoformat()
            outcome = {'registered': '已登记', 'step_ready': '待继续', 'published': '已正式发布',
                'failed': '失败待修复', 'dependency_missing': '依赖缺失', 'cancelled': '已取消',
                'superseded': '已被替代', 'paused': '已暂停', 'busy': '执行锁忙，待重试',
                'awaiting_publication': '等待发布授权', 'publication_not_authorized': '本次未授权发布',
                'pending': '对账待完成', 'unexplained': '对账存在未解释差异'}.get(result['status'], '待核对')
            result.update(dataset=dataset_for(source), source_ref_id=source.id, counts=counts,
                observation_start=stamp, observation_end=stamp,
                message=f'{schema_for(dataset_for(source)).name} 来源观察 {stamp} 至 {stamp} 本次结果为{outcome}，'
                f'累计合格 {counts.get("ready", 0)} 个、隔离 {counts.get("quarantined", 0)} 个，'
                f'执行 {result["steps"]} 个单元；'
                + ('历史发布已保存，当前指针保持不变。' if result.get('head_activated') is False
                   else '正式发布检查点已保存。' if result['status'] == 'published'
                   else '正式发布检查点未推进，工作进度已保留。'))
    print(encode(result))


if __name__ == '__main__':
    main()
