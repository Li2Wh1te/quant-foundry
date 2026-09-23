"""Advance committed Tushare row changes without an ID/date watermark.

The table journal is local and append-only. Discovery requires the fixed table
bootstrap receipt and the original nonempty chunks' actual business releases.
Each selected event gets an independent bounded work batch. A current-value
race becomes a separately readable historical release on the next visit.
"""
import json
from uuid import UUID

import structlog
from sqlalchemy import and_, cast, exists, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session, aliased

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.models import Baseline, SourceRef
from app.data_foundation.table_intake import TABLE_SOURCES, MANIFEST_DATASET
from app.data_foundation.table_bootstrap import RECEIPT_DATASET
from app.data_foundation.scope_settlement import TABLE_NAMES
from app.data_foundation.table_updates import freeze_change, is_current_change
from app.data_foundation.update_models import TableChange, TableChangeVisit
from app.data_foundation.record_adapters import SOURCE_DATASETS
from app.data_foundation import record_work
from app.data_foundation.record_pipeline import register_job, advance_job
from app.data_foundation.work import scope_key
from app.data_foundation.work_models import Work, Candidate, CandidateManifest, Release
from app.data_foundation.batch_models import BatchWork

logger = structlog.get_logger(__name__)


def ready_capture(session, *, capture_ref_id, native_dataset):
    """A capture alone is not a receipt for its nonempty business chunks."""
    capture = session.get(SourceRef, capture_ref_id)
    if capture is None or capture.source != 'tushare' or capture.dataset != MANIFEST_DATASET:
        raise FoundationError('SOURCE_INVALID', '本地表更新缺少固定全量捕获。')
    event = digest('local-table-bootstrap-v1', [capture_ref_id, native_dataset])
    receipt = session.scalar(select(SourceRef).join(Baseline, Baseline.id == SourceRef.baseline_id).where(
        Baseline.source == 'foundation', Baseline.dataset == RECEIPT_DATASET,
        Baseline.event_key == event))
    if receipt is None:
        return dict(status='waiting_bootstrap', pending_backfill=None, items=[])
    from app.data_foundation.source_refs import read_source
    manifest = read_source(session, capture.id)[0]
    group = next((item for item in manifest['tables'] if item['dataset'] == native_dataset), None)
    if group is None:
        raise FoundationError('SOURCE_INVALID', '本地表更新领域不在固定捕获清单。')
    if group['rows'] == 0:
        # Empty tables are settled by a separate typed scope receipt. New
        # arrivals can then flow once bootstrap has sealed its zero denominator.
        from app.data_foundation.scope_settlement import DATASET as EMPTY_DATASET, DOMAIN as EMPTY_DOMAIN
        from app.data_foundation.full_formalization import published_sources
        empty_event = digest('empty-local-scope-v1', ['tushare', native_dataset, group['table'], capture.id])
        empty_ref = session.scalar(select(SourceRef).join(Baseline, Baseline.id == SourceRef.baseline_id).where(
            Baseline.source == 'foundation', Baseline.dataset == EMPTY_DATASET,
            Baseline.event_key == empty_event))
        if empty_ref is None or empty_ref.id not in published_sources(session, [empty_ref.id],
                                                                       'foundation', EMPTY_DOMAIN):
            return dict(status='waiting_empty_settlement', pending_backfill=0, items=[])
        return dict(status='ready', pending_backfill=0, items=[])
    domain = SOURCE_DATASETS.get(('tushare', native_dataset))
    if domain is None:
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '非空本地表缺少正式业务领域契约。')
    ids = [UUID(item['source_ref_id']) for item in group['chunks']]
    origin, governance = aliased(Work), aliased(Work)
    scope_key_value = scope_key(domain, 1, 'default', record_work.series_for(domain, 'tushare'))
    quarantined = exists(select(1).select_from(Candidate).where(
        Candidate.work_id == origin.id, Candidate.readiness != 'ready'))
    published = select(origin.source_ref_id).join(CandidateManifest, CandidateManifest.work_id == origin.id)\
        .join(governance, governance.candidate_manifest_id == CandidateManifest.id)\
        .join(Release, Release.work_id == governance.id).where(
            origin.source_ref_id.in_(ids), origin.kind == 'A', origin.status == 'succeeded',
            Release.scope_key == scope_key_value, Release.status == 'published', ~quarantined).distinct()
    settled = set(session.scalars(published))
    pending = len(ids) - len(settled)
    return dict(status='waiting_backfill' if pending else 'ready',
                pending_backfill=pending, items=[])


def discover(session, *, native_dataset, capture_ref_id, execution_id, limit=10):
    if type(limit) is not int or not 2 <= limit <= 20:
        raise ValueError('Discovery limit must be 2 to 20 changes')
    if ('tushare', native_dataset) not in SOURCE_DATASETS:
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '本地表更新尚无正式业务领域契约。')
    gate = ready_capture(session, capture_ref_id=capture_ref_id, native_dataset=native_dataset)
    if gate['status'] != 'ready':
        return gate
    visit = aliased(TableChangeVisit)
    origin, governance = aliased(Work), aliased(Work)
    scope = scope_key(SOURCE_DATASETS[('tushare', native_dataset)], 1, 'default',
                      record_work.series_for(SOURCE_DATASETS[('tushare', native_dataset)], 'tushare'))
    bad = exists(select(1).select_from(Candidate).where(
        Candidate.work_id == origin.id, Candidate.readiness != 'ready'))
    receipt = exists(select(1).select_from(origin).join(CandidateManifest, CandidateManifest.work_id == origin.id)
        .join(governance, governance.candidate_manifest_id == CandidateManifest.id)
        .join(Release, Release.work_id == governance.id).where(
            origin.source_ref_id == visit.source_ref_id, origin.kind == 'A', origin.status == 'succeeded',
            Release.scope_key == scope, Release.status == 'published', ~bad))
    rows = session.execute(select(TableChange.id, TableChange.observed_at,
        visit.source_ref_id, visit.visited_at).outerjoin(visit, and_(
            visit.change_id == TableChange.id, visit.execution_id == execution_id)).where(
            TableChange.dataset == native_dataset, ~receipt).order_by(
            visit.visited_at.asc().nulls_first(), TableChange.observed_at, TableChange.id).limit(limit)).all()
    return dict(status='ready', pending_backfill=0, items=[dict(change_id=row.id,
        observed_at=row.observed_at, source_ref_id=row.source_ref_id) for row in rows])


def advance_changes(engine, *, native_dataset, capture_ref_id, execution_id,
                    runtime_digest, archive_root='/app/data/foundation-runtime-archives',
                    source_limit=10, steps_per_source=2, before_source=None):
    if type(steps_per_source) is not int or not 1 <= steps_per_source <= 10:
        raise ValueError('Each change requires 1 to 10 bounded steps')
    key = int(digest('table-change-discovery-lock-v1', native_dataset)[:15], 16)
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as leader:
        if not leader.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}):
            return dict(status='busy', items=[])
        try:
            with Session(engine) as session:
                found = discover(session, native_dataset=native_dataset,
                    capture_ref_id=capture_ref_id, execution_id=execution_id, limit=source_limit)
            if found['status'] != 'ready':
                return found
            results = []
            for item in found['items']:
                if before_source is not None:
                    before_source()
                change_id = item['change_id']
                with Session(engine) as session, session.begin():
                    session.execute(insert(TableChangeVisit).values(change_id=change_id,
                        execution_id=execution_id, visited_at=now(), visits=1,
                        result_json=encode(dict(status='running'))).on_conflict_do_update(
                        index_elements=['change_id', 'execution_id'],
                        set_=dict(visited_at=now(), visits=TableChangeVisit.visits + 1,
                                  result_json=encode(dict(status='running')))))
                try:
                    with Session(engine) as session, session.begin():
                        source = freeze_change(session, change_id=change_id, execution_id=execution_id)
                        current = is_current_change(session, source, change_id)
                        batch = register_job(session, source_ref_id=source.id, execution_id=execution_id,
                            runtime_digest=runtime_digest, archive_root=archive_root,
                            preserve_head=not current, table_change_id=change_id if current else None)
                        batch_id = batch.id
                        source_id = source.id
                    result = advance_job(engine, batch_id, runtime_digest=runtime_digest,
                        archive_root=archive_root, steps=steps_per_source, publish=True,
                        lock_wait_seconds=5)
                    with Session(engine) as session:
                        result['candidate_counts'] = dict(session.execute(select(Candidate.readiness, func.count())
                            .join(Work, Work.id == Candidate.work_id).join(BatchWork, BatchWork.work_id == Work.id)
                            .where(BatchWork.batch_id == batch_id, Work.kind == 'A')
                            .group_by(Candidate.readiness)).all())
                    if result['status'] == 'published' and result['candidate_counts'].get('quarantined', 0):
                        result['status'] = 'quarantined'
                except FoundationError as exc:
                    logger.error('foundation_table_update_rejected', native_dataset=native_dataset,
                        change_id=change_id, error_code=exc.code,
                        message=f'{TABLE_NAMES[native_dataset]}本地表变化，观察日期 {item["observed_at"].date()} 至 {item["observed_at"].date()}，失败 1 条；正式发布检查点未推进。')
                    result = dict(status='failed', error_code=exc.code,
                                  error_type=type(exc).__name__, message=str(exc))
                    source_id = item['source_ref_id']
                except Exception as exc:
                    logger.exception('foundation_table_update_failed', native_dataset=native_dataset,
                        change_id=change_id, error_type=type(exc).__name__,
                        message=f'{TABLE_NAMES[native_dataset]}本地表变化，观察日期 {item["observed_at"].date()} 至 {item["observed_at"].date()}，失败 1 条；正式发布检查点未推进。')
                    result = dict(status='failed', error_code='UPDATE_UNEXPECTED',
                                  error_type=type(exc).__name__)
                    source_id = item['source_ref_id']
                with Session(engine) as session, session.begin():
                    visit_row = session.get(TableChangeVisit, (change_id, execution_id))
                    visit_row.source_ref_id = source_id
                    visit_row.result_json = encode(result)
                results.append(dict(**item, **result))
            return dict(status='advanced', pending_backfill=0, items=results)
        finally:
            leader.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
