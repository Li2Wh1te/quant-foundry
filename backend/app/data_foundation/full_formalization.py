"""One restartable driver for complete fixed local-domain publication.

The driver executes work; it is not a monitoring service. Its only checkpoints
are the existing immutable source, batch, work and release records. Restarting
reconstructs the remainder from verified publication edges. It neither fetches
suppliers nor cancels, rewrites or force-retries terminal work. Fixed backfills
use a distinct batch purpose so a later update-runtime handoff cannot drain an
entire historical backfill accidentally.
"""
import argparse
import json
from collections import Counter, deque
from time import sleep
from uuid import UUID

import structlog
from sqlalchemy import select, func, exists
from sqlalchemy.orm import Session, aliased

from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.intake_models import CampaignScan, Scan, ScanSeal, ScanFailure, ScanEntry, IntakeItem
from app.data_foundation.models import SourceRef
from app.data_foundation.work_models import Work, Candidate, CandidateManifest, Release
from app.data_foundation.batch_models import BatchWork
from app.data_foundation.record_adapters import SOURCE_DATASETS
from app.data_foundation import record_work
from app.data_foundation.record_pipeline import register_job, advance_job
from app.data_foundation.work import scope_key
from app.data_ingestion.tonghuashun.contracts import DATASETS

logger = structlog.get_logger(__name__)


def published_sources(session, source_ids, source, dataset):
    """Only actual releases without quarantined children settle business input."""
    origin, governance = aliased(Work), aliased(Work)
    scope = scope_key(dataset, 1, 'default', record_work.series_for(dataset, source))
    quarantined = exists(select(1).select_from(Candidate).where(
        Candidate.work_id == origin.id, Candidate.readiness != 'ready'))
    query = select(origin.source_ref_id).join(CandidateManifest, CandidateManifest.work_id == origin.id)\
        .join(governance, governance.candidate_manifest_id == CandidateManifest.id)\
        .join(Release, Release.work_id == governance.id).where(origin.source_ref_id.in_(source_ids),
            origin.kind == 'A', origin.status == 'succeeded', Release.scope_key == scope,
            Release.status == 'published', ~quarantined,
            ~governance.parameters_json.contains(record_work.OLDER_SOURCE_RETAINED)).distinct()
    return set(session.scalars(query))


def plan_campaign(session, campaign_id, datasets=None):
    """Read a sealed full denominator; refuse missing or unfinished captures."""
    admitted = {native for source, native in SOURCE_DATASETS if source == 'tonghuashun'}
    selected = sorted(admitted if datasets is None else set(datasets))
    if not selected or not set(selected) <= admitted:
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '全量执行包含未安装的领域或空范围。')
    groups = []
    for native in selected:
        link = session.get(CampaignScan, (campaign_id, native))
        if link is None:
            raise FoundationError('BACKFILL_REQUIRED', '全量执行缺少领域固定捕获清单。')
        scan, seal = session.get(Scan, link.scan_id), session.get(ScanSeal, link.scan_id)
        failures = session.scalar(select(func.count()).select_from(ScanFailure).where(ScanFailure.scan_id == link.scan_id))
        if scan.status != 'completed' or seal is None or failures:
            raise FoundationError('CAPTURE_INCOMPLETE', '全量来源捕获未完整结算，不能启动发布。')
        rows = list(session.execute(select(SourceRef.id, SourceRef.observed_at)
            .join(IntakeItem, IntakeItem.source_ref_id == SourceRef.id)
            .join(ScanEntry, ScanEntry.item_id == IntakeItem.id)
            .where(ScanEntry.scan_id == scan.id, SourceRef.source == 'tonghuashun', SourceRef.dataset == native)
            .order_by(SourceRef.observed_at, SourceRef.id)))
        ids = [row.id for row in rows]
        if len(ids) != seal.target_count or len(set(ids)) != len(ids):
            raise FoundationError('CAPTURE_COUNT_MISMATCH', '全量固定来源分母不一致，不能截取或补造来源。')
        published = published_sources(session, ids, 'tonghuashun', SOURCE_DATASETS[('tonghuashun', native)]) if ids else set()
        groups.append(dict(native_dataset=native, fixed_versions=len(ids), already_published=len(published),
                           sources=[row for row in rows if row.id not in published],
                           start=rows[0].observed_at.date().isoformat() if rows else None,
                           end=rows[-1].observed_at.date().isoformat() if rows else None))
    return groups


def advance_source(engine, source_id, *, execution_id, runtime_digest, archive_root, steps=10, publish=False, preserve_head=False):
    """One finite restartable slice, preserving terminal errors and controls."""
    with Session(engine) as session, session.begin():
        batch = register_job(session, source_ref_id=source_id, execution_id=execution_id,
                             runtime_digest=runtime_digest, archive_root=archive_root, purpose='backfill', preserve_head=preserve_head)
        batch_id = batch.id
    result = advance_job(engine, batch_id, runtime_digest=runtime_digest, archive_root=archive_root,
                         steps=steps, publish=publish, lock_wait_seconds=1)
    with Session(engine) as session:
        counts = dict(session.execute(select(Candidate.readiness, func.count())
            .join(Work, Work.id == Candidate.work_id).join(BatchWork, BatchWork.work_id == Work.id)
            .where(BatchWork.batch_id == batch_id, Work.kind == 'A').group_by(Candidate.readiness)).all())
        governance = session.get(Work, result['work_id']) if result['status'] == 'published' else None
        requires_history = bool(governance and not preserve_head and any(
            action.get('reason') == record_work.OLDER_SOURCE_RETAINED
            for action in json.loads(governance.parameters_json).get('actions', [])))
    if result['status'] == 'published' and counts.get('quarantined', 0):
        # An empty/partial release does not prove this source's business values
        # were accepted. Keep its release receipt but surface the quarantine.
        result['status'] = 'quarantined'
    return dict(result, candidate_counts=counts, requires_history=requires_history)


def run_campaign(engine, *, campaign_id, execution_id, runtime_digest, archive_root,
                 datasets=None, publish=False, steps=10, staged_generations=None, freeze_units=False,
                 minimum_free_bytes=10 * 1024**3, table_capture_ref_id=None):
    """Rotate whole domains and finish submitted work without external polling."""
    if not publish:
        raise ValueError('Full publication requires explicit publish authorization')
    from app.data_foundation.staged_inputs import require_space, verify_runtime, freeze_dump, freeze_request_units
    require_space(archive_root, minimum_free_bytes)
    from app.data_foundation.scope_settlement import freeze_empty_scan, freeze_empty_tables, DOMAIN as EMPTY_DOMAIN, TABLE_NAMES
    def scope_group(native, ref, provider):
        done = published_sources(session, [ref.id], 'foundation', EMPTY_DOMAIN)
        return dict(native_dataset=native, fixed_versions=0, already_published=0,
                    scope_receipt=True, scope_receipts_published=len(done),
                    result_key=f'{provider}:{native}:empty', display_name=(DATASETS[native].name if provider == 'tonghuashun' else TABLE_NAMES[native]) + '固定本地空范围',
                    sources=[] if done else [ref], start=ref.observed_at.date().isoformat(),
                    end=ref.observed_at.date().isoformat())
    with Session(engine) as session, session.begin():
        verify_runtime(session, execution_id, runtime_digest, archive_root)
        groups = plan_campaign(session, campaign_id, datasets)
        for index, group in enumerate(groups):
            if group['fixed_versions'] == 0:
                require_space(archive_root, minimum_free_bytes)
                ref = freeze_empty_scan(session, campaign_id=campaign_id,
                    native_dataset=group['native_dataset'], execution_id=execution_id)
                groups[index] = scope_group(group['native_dataset'], ref, 'tonghuashun')
        if table_capture_ref_id is not None:
            require_space(archive_root, minimum_free_bytes)
            for native, ref in freeze_empty_tables(session, capture_ref_id=table_capture_ref_id, execution_id=execution_id):
                groups.append(scope_group(native, ref, 'tushare'))
        # Detach the minimal work locators before committing. ORM receipt
        # objects would otherwise expire when this planning session closes.
        from types import SimpleNamespace
        for group in groups:
            group['sources'] = [SimpleNamespace(id=row.id, observed_at=row.observed_at) for row in group['sources']]
    staging = []
    unresolved_units = None
    for native, generation in (staged_generations or {}).items():
        with Session(engine) as session, session.begin():
            frozen = freeze_dump(session, dataset=native, generation=generation, execution_id=execution_id,
                                 before_write=lambda: require_space(archive_root, minimum_free_bytes))
            rows = list(session.execute(select(SourceRef.id, SourceRef.observed_at).where(
                SourceRef.id.in_(frozen['source_ref_ids'])).order_by(SourceRef.observed_at, SourceRef.id)))
            done = published_sources(session, frozen['source_ref_ids'], 'tonghuashun',
                SOURCE_DATASETS[('tonghuashun', frozen['source_dataset'])]) if rows else set()
            groups.append(dict(native_dataset=frozen['source_dataset'], staged_channel=native,
                fixed_versions=len(rows), already_published=len(done), preserve_head=True,
                sources=[row for row in rows if row.id not in done],
                start=min((row.observed_at.date().isoformat() for row in rows), default=None),
                end=max((row.observed_at.date().isoformat() for row in rows), default=None)))
            staging.append(frozen)
    if freeze_units:
        with Session(engine) as session, session.begin():
            unresolved_units = freeze_request_units(session, execution_id=execution_id,
                                                     before_write=lambda: require_space(archive_root, minimum_free_bytes))
    queue = deque()
    # Interleave domains so a long NAV/history family cannot starve short ones.
    for ordinal in range(max((len(group['sources']) for group in groups), default=0)):
        for group in groups:
            if ordinal < len(group['sources']):
                queue.append((group, group['sources'][ordinal]))
    for group in groups:
        group.setdefault('result_key', group.get('staged_channel', group['native_dataset']))
        group.setdefault('display_name', DATASETS[group['native_dataset']].name if group['native_dataset'] in DATASETS else group['native_dataset'])
    counts = {group['result_key']: Counter() for group in groups}
    history_pending = set()
    dates = sorted(value for group in groups for value in (group['start'], group['end']) if value)
    start, end = (dates[0], dates[-1]) if dates else ('无固定观察', '无固定观察')
    logger.info('foundation_full_started', campaign_id=str(campaign_id), execution_id=str(execution_id),
                selected=len(queue), message=f'本地全量正式化已启动，观察日期 {start} 至 {end}，覆盖 {len(groups)} 个领域、待处理 {len(queue)} 个固定来源；尚未推进发布检查点。')
    while queue:
        group, source = queue.popleft()
        preserve_head = group.get('preserve_head', False) or source.id in history_pending
        require_space(archive_root, minimum_free_bytes)
        try:
            result = advance_source(engine, source.id, execution_id=execution_id, runtime_digest=runtime_digest,
                                    archive_root=archive_root, steps=steps, publish=True,
                                    preserve_head=preserve_head)
        except FoundationError as exc:
            result = dict(status='failed', error_code=exc.code, message=str(exc))
        except Exception as exc:
            # Individual unexpected failures must not discard the remainder.
            # Work/batch evidence stays durable for later investigation.
            result = dict(status='failed', error_code=type(exc).__name__)
            logger.exception('foundation_full_source_failed', source_ref_id=str(source.id),
                             message=f"{group['display_name']} 全量执行异常，观察日期 {source.observed_at.date()} 至 {source.observed_at.date()}，失败 1 个来源；检查点未推进，原证据保留，其余来源继续处理。")
        if result['status'] in ('busy', 'step_ready'):
            queue.append((group, source))
            if result['status'] == 'busy':
                sleep(0.25)
            continue
        if (not preserve_head and result['status'] == 'failed'
                and result.get('error_code') in ('SOURCE_OLDER_THAN_PARENT_NONVALUE',
                                                  'SOURCE_ORDER_AMBIGUOUS')):
            history_pending.add(source.id)
            queue.append((group, source))
            continue
        if result['status'] == 'published' and result.get('requires_history') and not preserve_head:
            # The current release kept newer overlapping keys. Complete a
            # separate immutable historical release for this older source;
            # a restart rediscovers it until that second receipt exists.
            history_pending.add(source.id)
            queue.append((group, source))
            continue
        native = group['native_dataset']
        counts[group['result_key']][result['status']] += 1
        day = source.observed_at.date().isoformat()
        candidate_counts = result.get('candidate_counts', {})
        state_name = {'published':'已发布','quarantined':'已隔离','paused':'已暂停','failed':'执行失败',
                      'dependency_missing':'运行依赖缺失','cancelled':'已取消','superseded':'执行上下文已改变',
                      'unexplained':'对账未通过','pending':'等待对账'}.get(result['status'], '尚未完成')
        message = (f'{group['display_name']} 全量正式化，观察日期 {day} 至 {day}，'
                   f'来源结果为{state_name}，合格 {candidate_counts.get("ready", 0)} 条、'
                   f'隔离 {candidate_counts.get("quarantined", 0)} 条；'
                   + ('正式发布检查点已推进。' if result['status'] == 'published' else '该来源未完成业务发布，原检查点及问题保留。'))
        logger.info('foundation_full_source_processed', native_dataset=native, source_ref_id=str(source.id),
                    execution_id=str(execution_id), result=json_safe(result), message=message)
    results = [{key:value for key,value in group.items() if key != 'sources'} |
               {'results': dict(counts[group['result_key']])} for group in groups]
    unresolved = sum(sum(value for status,value in counter.items() if status != 'published') for counter in counts.values())
    unresolved += (unresolved_units or {}).get('fixed_units', 0)
    result = dict(status='completed_with_issues' if unresolved else 'completed', campaign_id=campaign_id,
                  execution_id=execution_id, domains=results, staged_inputs=staging, unresolved_request_units=unresolved_units,
                  unresolved_sources=unresolved,
                  message=f'本地全量执行已遍历观察日期 {start} 至 {end} 的 {len(groups)} 个领域，本次发布 {sum(c["published"] for c in counts.values())} 个来源，未解决 {unresolved} 个；已成功发布的检查点和问题证据已保存。')
    logger.info('foundation_full_finished', **json_safe(result))
    return result


def json_safe(value):
    import json
    return json.loads(encode(value))


def main():
    import app.models  # Register every ORM edge before planning the campaign.
    from app.db.session import get_engine
    parser = argparse.ArgumentParser(description='一次提交全部已固定领域，自动接续正式发布；不访问供应商。')
    parser.add_argument('--campaign-id', type=UUID, required=True)
    parser.add_argument('--execution-id', type=UUID, required=True)
    parser.add_argument('--table-capture-ref-id', type=UUID)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--archive-root', default='/app/data/foundation-runtime-archives')
    parser.add_argument('--dataset', action='append')
    parser.add_argument('--steps', type=int, default=10, choices=range(1,101))
    parser.add_argument('--staged-generation', action='append', default=[], metavar='DATASET=UUID')
    parser.add_argument('--preserve-request-units', action='store_true')
    parser.add_argument('--minimum-free-gib', type=int, default=10, choices=range(0, 10001))
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    staged = {}
    for value in args.staged_generation:
        native, generation = value.split('=', 1)
        if native in staged:
            parser.error('暂存导入渠道不能重复指定')
        staged[native] = UUID(generation)
    if not args.publish:
        with Session(get_engine()) as session:
            groups = plan_campaign(session, args.campaign_id, args.dataset)
        print(encode(dict(status='planned_not_published', domains=[
            {key:value for key,value in group.items() if key != 'sources'} |
            {'remaining_versions': len(group['sources'])} for group in groups])))
        return
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    logging_runtime = configure_logging(get_settings())
    try:
        result = run_campaign(get_engine(), campaign_id=args.campaign_id, execution_id=args.execution_id,
                              runtime_digest=args.runtime, archive_root=args.archive_root, datasets=args.dataset,
                              steps=args.steps, publish=True, staged_generations=staged, freeze_units=args.preserve_request_units,
                              minimum_free_bytes=args.minimum_free_gib * 1024**3, table_capture_ref_id=args.table_capture_ref_id)
        print(encode(result))
        if result['unresolved_sources']:
            raise SystemExit(2)
    except FoundationError as exc:
        logger.error('foundation_full_stopped', campaign_id=str(args.campaign_id),
                     execution_id=str(args.execution_id), error_code=exc.code,
                     message=f'本地全量执行未完成，已提交的范围与发布计数保留在持久检查点中；停止原因：{exc}')
        raise SystemExit(2) from None
    finally:
        logging_runtime.stop()


if __name__ == '__main__':
    main()
