"""Discover committed local observations and advance bounded update batches.

    No permanent UUID/time watermark is used: smaller IDs and backdated arrivals
    remain discoverable. Historical observations get independently readable
    releases; only an exact current source revision may activate the head.
"""
from sqlalchemy import select, func, exists, cast, and_, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session, aliased
import structlog

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.update_models import UpdateVisit
from app.data_foundation.intake_models import CampaignScan, Scan, ScanSeal, ScanTarget, ScanFailure
from app.data_foundation.models import SourceRef
from app.data_foundation.work_models import Work, Candidate, CandidateManifest, Release
from app.data_foundation.batch_models import BatchWork
from app.data_foundation.record_adapters import SOURCE_DATASETS
from app.data_foundation import record_work
from app.data_foundation.work import scope_key
from app.data_foundation.record_pipeline import register_job, advance_job
from app.data_foundation.source_refs import register_observation
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation, TonghuashunCollectionState as State
from app.data_ingestion.tonghuashun.contracts import CollectionError, DATASETS

logger = structlog.get_logger(__name__)


def publication_receipts(native_dataset):
    """Use real published work edges, including releases that preserve the head."""
    domain = SOURCE_DATASETS.get(('tonghuashun', native_dataset))
    if domain is None:
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '该本地领域尚无正式化适配器，不能启用自动更新。')
    scope = scope_key(domain, 1, 'default', record_work.series_for(domain, 'tonghuashun'))
    origin, governance = aliased(Work), aliased(Work)
    return select(SourceRef.observation_id.label('observation_id'),
        cast(governance.parameters_json, JSONB)['source_revision'].as_integer().label('source_revision'))\
        .select_from(SourceRef).join(origin, origin.source_ref_id == SourceRef.id)\
        .join(CandidateManifest, CandidateManifest.work_id == origin.id)\
        .join(governance, governance.candidate_manifest_id == CandidateManifest.id)\
        .join(Release, Release.work_id == governance.id)\
        .where(SourceRef.source == 'tonghuashun', SourceRef.dataset == native_dataset,
            SourceRef.observation_id.is_not(None), origin.status == 'succeeded',
            Release.scope_key == scope, Release.status == 'published').subquery()


def discover(session, *, native_dataset, backfill_campaign_id, execution_id, limit=10):
    """Wait for the fixed backfill, then inspect both current and old arrivals.

    Half of each finite selection is reserved for historical versions to avoid
    starving them behind a continually changing current source pointer.
    """
    if type(limit) is not int or not 2 <= limit <= 20:
        raise ValueError('Discovery limit must be 2 to 20 sources')
    receipts = publication_receipts(native_dataset)
    link = session.get(CampaignScan, (backfill_campaign_id, native_dataset))
    if link is None:
        raise FoundationError('BACKFILL_REQUIRED', '持续更新缺少该领域的固定全量捕获依据。')
    scan, seal = session.get(Scan, link.scan_id), session.get(ScanSeal, link.scan_id)
    failures = session.scalar(select(func.count()).select_from(ScanFailure).where(ScanFailure.scan_id == scan.id))
    pending = session.scalar(select(func.count()).select_from(ScanTarget).where(ScanTarget.scan_id == scan.id,
        ~exists(select(1).select_from(receipts).where(receipts.c.observation_id == ScanTarget.observation_id))))
    if scan.status != 'completed' or seal is None or failures or pending:
        return dict(status='waiting_backfill', pending_backfill=pending, scan_failures=failures, items=[])
    current = list(session.execute(select(Observation.id, State.revision, Observation.observed_at)
        .join(State, State.observation_id == Observation.id)
        .outerjoin(UpdateVisit, and_(UpdateVisit.observation_id == Observation.id,
            UpdateVisit.execution_id == execution_id, UpdateVisit.source_revision == State.revision))
        .where(Observation.dataset == native_dataset, State.dataset == native_dataset,
            State.subject == Observation.subject, State.variant == Observation.variant,
            ~exists(select(1).select_from(receipts).where(receipts.c.observation_id == Observation.id,
                receipts.c.source_revision == State.revision)))
        .order_by(UpdateVisit.visited_at.asc().nulls_first(), Observation.observed_at, Observation.id).limit(limit // 2)))
    historical = list(session.execute(select(Observation.id, Observation.observed_at)
        .outerjoin(UpdateVisit, and_(UpdateVisit.observation_id == Observation.id,
            UpdateVisit.execution_id == execution_id, UpdateVisit.source_revision == 0))
        .where(Observation.dataset == native_dataset,
            ~exists(select(1).select_from(State).where(State.dataset == Observation.dataset,
                State.subject == Observation.subject, State.variant == Observation.variant,
                State.observation_id == Observation.id)),
            ~exists(select(1).select_from(receipts).where(receipts.c.observation_id == Observation.id)))
        .order_by(UpdateVisit.visited_at.asc().nulls_first(), Observation.observed_at, Observation.id).limit(limit-len(current))))
    items = [dict(observation_id=row.id, source_revision=row.revision, observed_at=row.observed_at,
                  preserve_head=False) for row in current]
    items += [dict(observation_id=row.id, source_revision=None, observed_at=row.observed_at,
                   preserve_head=True) for row in historical]
    return dict(status='ready', pending_backfill=0, scan_failures=0, items=items)


def advance_updates(engine, *, native_dataset, backfill_campaign_id, execution_id,
                    runtime_digest, source_limit=10, steps_per_source=2,
                    archive_root='/app/data/foundation-runtime-archives', before_source=None):
    """Serialize discovery per domain while leaving all worker steps bounded.

    Session locks are released on process death. Visit rotation is committed
    before processing, independently from mutable source pointers and failures.
    """
    key = int(digest('record-update-discovery-lock-v1', native_dataset)[:15], 16)
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as leader:
        if not leader.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}):
            return dict(status='busy', items=[])
        try:
            return _advance_updates(engine, native_dataset=native_dataset,
                backfill_campaign_id=backfill_campaign_id, execution_id=execution_id,
                runtime_digest=runtime_digest, source_limit=source_limit,
                steps_per_source=steps_per_source, archive_root=archive_root, before_source=before_source)
        finally:
            leader.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})


def _advance_updates(engine, *, native_dataset, backfill_campaign_id, execution_id,
                     runtime_digest, source_limit, steps_per_source, archive_root, before_source):
    """Each source has independent transactions and retains errors for retry.

    The scheduler persists this result with its run. A source-context race is
    rediscovered on the next run; it cannot roll back another source's release.
    Terminal pipeline failures remain explicit rather than being called success.
    """
    if type(steps_per_source) is not int or not 1 <= steps_per_source <= 10:
        raise ValueError('Each update source requires 1 to 10 bounded steps')
    with Session(engine) as session:
        found = discover(session, native_dataset=native_dataset,
            backfill_campaign_id=backfill_campaign_id, execution_id=execution_id, limit=source_limit)
    if found['status'] != 'ready':
        return found
    results = []
    for item in found['items']:
        if before_source is not None:
            before_source()
        identity = dict(observation_id=item['observation_id'], execution_id=execution_id,
                        source_revision=item['source_revision'] or 0)
        # A crash after this commit leaves an explicit interrupted visit. It
        # does not become a publication receipt or permanently skip the source.
        with Session(engine) as session, session.begin():
            session.execute(insert(UpdateVisit).values(**identity, visited_at=now(), visits=1,
                result_json=encode(dict(status='running'))).on_conflict_do_update(
                    index_elements=['observation_id', 'execution_id', 'source_revision'],
                    set_=dict(visited_at=now(), visits=UpdateVisit.visits + 1,
                              result_json=encode(dict(status='running')))))
        try:
            with Session(engine) as session, session.begin():
                source = register_observation(session, item['observation_id'], execution_id)
                batch_id = register_job(session, source_ref_id=source.id, execution_id=execution_id,
                    runtime_digest=runtime_digest, archive_root=archive_root,
                    preserve_head=item['preserve_head'], source_revision=item['source_revision']).id
            result = advance_job(engine, batch_id, runtime_digest=runtime_digest,
                steps=steps_per_source, publish=True, archive_root=archive_root)
            with Session(engine) as session:
                result['candidate_counts'] = dict(session.execute(
                    select(Candidate.readiness, func.count()).join(Work, Work.id == Candidate.work_id)
                    .join(BatchWork, BatchWork.work_id == Work.id)
                    .where(BatchWork.batch_id == batch_id, Work.kind == 'A')
                    .group_by(Candidate.readiness)).all())
        except (FoundationError, CollectionError) as exc:
            result = dict(status='failed', error_code=getattr(exc, 'code', 'SOURCE_INVALID'),
                          error_type=type(exc).__name__, message=str(exc))
        except Exception as exc:
            # Unexpected defects remain actionable and retain their trace in
            # structured logs, but cannot roll back another source's release.
            result = dict(status='failed', error_code='UPDATE_UNEXPECTED',
                error_type=type(exc).__name__, message='本地正式化更新失败，该来源已保留待修复重试。')
            logger.exception('foundation_update_source_failed', native_dataset=native_dataset,
                observation_id=str(item['observation_id']), error_type=type(exc).__name__,
                message=f'{DATASETS[native_dataset].name} 来源观察 {item["observed_at"]} 至 {item["observed_at"]} 正式化失败 1 个；检查点未推进。')
        with Session(engine) as session, session.begin():
            visit = session.get(UpdateVisit, tuple(identity.values()))
            visit.result_json = encode(result)
        results.append({**item, **result})
    return dict(status='advanced', pending_backfill=0, scan_failures=0, items=results)
