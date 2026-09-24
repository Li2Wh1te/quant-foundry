"""Bounded settlement of fixed Tushare chunks, independent of decoder upgrades."""
import json
from uuid import UUID
from sqlalchemy import select, exists
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.performance_models import TableSettlementVisit, TableSettlementReceipt
from app.data_foundation.work_models import Work, Candidate, CandidateManifest, Release


def _record(session, visit, rows):
    if rows:
        session.execute(insert(TableSettlementReceipt).values([
            dict(capture_ref_id=visit.capture_ref_id, scope_key=visit.scope_key,
                 source_ref_id=sid, release_id=rid, created_at=now()) for sid, rid in rows])
            .on_conflict_do_nothing(index_elements=['capture_ref_id', 'scope_key', 'source_ref_id']))


def probe_page(session, *, capture_ref_id, native_dataset, scope_key, source_ids, limit=200):
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError('Settlement probe limit must be 1 to 2000')
    lock_key(session, 'table-settlement', [str(capture_ref_id), scope_key])
    fixed = sorted(set(source_ids), key=str)
    if len(fixed) != len(source_ids):
        raise FoundationError('SOURCE_INVALID', '固定表捕获含重复分块。')
    document = encode([str(sid) for sid in fixed])
    visit = session.get(TableSettlementVisit, (capture_ref_id, scope_key), populate_existing=True)
    if visit is None:
        visit = TableSettlementVisit(capture_ref_id=capture_ref_id, scope_key=scope_key,
            native_dataset=native_dataset, source_ids_json=document, cursor_id=None, checked_at=now())
        session.add(visit)
        session.flush()
    if visit.source_ids_json != document or visit.native_dataset != native_dataset:
        raise FoundationError('SETTLEMENT_INVALID', '本地表结算与固定捕获不一致。')
    accepted = set(session.scalars(select(TableSettlementReceipt.source_ref_id).where(
        TableSettlementReceipt.capture_ref_id == capture_ref_id, TableSettlementReceipt.scope_key == scope_key)))
    if not accepted <= set(fixed):
        raise FoundationError('SETTLEMENT_INVALID', '本地表结算包含固定范围以外的来源。')
    remaining = [sid for sid in fixed if sid not in accepted]
    selected = [sid for sid in remaining if visit.cursor_id is None or sid > visit.cursor_id][:limit]
    if not selected:
        selected = remaining[:limit]
    if selected:
        origin, governance = aliased(Work), aliased(Work)
        bad = exists(select(1).select_from(Candidate).where(Candidate.work_id == origin.id, Candidate.readiness != 'ready'))
        query = select(origin.source_ref_id, Release.id).join(CandidateManifest, CandidateManifest.work_id == origin.id)\
            .join(governance, governance.candidate_manifest_id == CandidateManifest.id)\
            .join(Release, Release.work_id == governance.id).where(origin.source_ref_id.in_(selected),
                origin.kind == 'A', origin.status == 'succeeded', Release.scope_key == scope_key,
                Release.status == 'published', ~governance.parameters_json.contains('OLDER_SOURCE_RETAINED'), ~bad)
        found = {}
        for sid, rid in session.execute(query):
            found.setdefault(sid, rid)
        _record(session, visit, list(found.items()))
        accepted.update(found)
        visit.cursor_id = selected[-1]
    visit.checked_at = now()
    session.flush()
    return len(fixed)-len(accepted)


def record_publication(session, source, release):
    visits = list(session.scalars(select(TableSettlementVisit).where(
        TableSettlementVisit.native_dataset == source.dataset, TableSettlementVisit.scope_key == release.scope_key)
        .order_by(TableSettlementVisit.capture_ref_id)))
    if not visits:
        return
    governance = session.get(Work, release.work_id)
    if 'OLDER_SOURCE_RETAINED' in governance.parameters_json:
        return
    manifest = session.get(CandidateManifest, governance.candidate_manifest_id)
    bad = session.scalar(select(exists().where(Candidate.work_id == manifest.work_id, Candidate.readiness != 'ready')))
    if bad:
        return
    for visit in visits:
        if str(source.id) in json.loads(visit.source_ids_json):
            lock_key(session, 'table-settlement', [str(visit.capture_ref_id), release.scope_key])
            _record(session, visit, [(source.id, release.id)])
