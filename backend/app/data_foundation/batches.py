"""Reviewable, finite business batches with independent A/B controls."""
import json
from uuid import UUID
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.models import SourceRef
from app.data_foundation.work_models import Work, Release, Decision, BlockRef, BlockMember, CandidateManifest
from app.data_foundation.batch_models import Batch, BatchInput, BatchWork, BatchControl, ReleaseContribution


def batch_document(session, *, scope_key, source_ids, start, end, purpose):
    from datetime import date
    if not 1 <= len(source_ids) <= 200 or len(set(source_ids)) != len(source_ids):
        raise ValueError('Batch requires 1 to 200 explicit sources')
    if date.fromisoformat(start) > date.fromisoformat(end) or purpose not in ('backfill','catchup','maintenance','shadow'):
        raise ValueError('Invalid bounded batch scope')
    refs = [session.get(SourceRef, value) for value in sorted(source_ids,key=str)]
    if any(r is None for r in refs):
        raise FoundationError('SOURCE_UNAVAILABLE', '批次包含未固定来源。')
    document = dict(schema=1, scope_key=scope_key, start=start, end=end, purpose=purpose,
        sources=[{'id':str(r.id),'hash':r.content_hash} for r in refs])
    return document, digest('foundation-batch-v1', document)


def register_batch(session, *, event_key, document, expected_hash):
    if set(document) != {'schema','scope_key','start','end','purpose','sources'} or document['schema'] != 1:
        raise ValueError('Invalid batch document')
    actual, fingerprint = batch_document(session, scope_key=document['scope_key'],
        source_ids=[UUID(s['id']) for s in document['sources']], start=document['start'],end=document['end'],purpose=document['purpose'])
    if document != actual or expected_hash != fingerprint:
        raise FoundationError('INPUT_CHANGED', '批次输入与审核摘要不一致。')
    if not isinstance(event_key,str) or not event_key.strip() or len(event_key)>128:
        raise ValueError('Invalid batch event key')
    lock_key(session,'batch',event_key)
    row = session.scalar(select(Batch).where(Batch.event_key == event_key))
    if row:
        if row.manifest_hash != fingerprint:
            raise FoundationError('INPUT_CHANGED', '同一批次事件不能替换输入。')
        return row
    row = Batch(event_key=event_key,manifest_hash=fingerprint,manifest_json=encode(document),scope_key=document['scope_key'],created_at=now())
    session.add(row);session.flush()
    session.add_all([BatchInput(batch_id=row.id,source_ref_id=UUID(s['id'])) for s in document['sources']])
    session.add(BatchControl(batch_id=row.id,pause_a=True,pause_b=True,allow_publish=False));session.flush()
    return row


def attach_work(session, batch_id, work_id):
    lock_key(session,'batch-control',str(batch_id))
    batch, work = session.get(Batch,batch_id),session.get(Work,work_id)
    if batch is None or work is None or batch.scope_key != work.scope_key:
        raise FoundationError('SCOPE_MISMATCH','工作不属于该批次的数据范围。')
    document, params = json.loads(batch.manifest_json),json.loads(work.parameters_json)
    if not document['start'] <= params['start'] <= params['end'] <= document['end']:
        raise FoundationError('SCOPE_MISMATCH','工作业务日期超出固定批次。')
    from app.data_foundation.inputs import input_manifests
    inputs = [work] if work.kind == 'A' else [session.get(Work,m.work_id) for m in input_manifests(session,work=work,eligible_only=False)]
    approved = {UUID(r['id']) for r in document['sources']}
    if any(w.source_ref_id not in approved for w in inputs):
        raise FoundationError('SCOPE_MISMATCH','工作来源不属于已审核批次输入。')
    if session.get(BatchWork,(batch_id,work_id)) is None:
        from sqlalchemy import func
        if session.scalar(select(func.count()).select_from(BatchWork).where(BatchWork.batch_id==batch_id))>=400:
            raise FoundationError('SCOPE_LIMIT','一个有限批次最多关联400个工作，请拆分批次。')
        control=session.get(BatchControl,batch_id)
        # A later publication unit is allowed within the original source/date
        # manifest, but invalidates prior batch approval until re-reconciled.
        control.allow_publish=False
        session.add(BatchWork(batch_id=batch_id,work_id=work_id));session.flush()
    return work


def set_controls(session,batch_id,*,pause_a,pause_b,allow_publish=False):
    lock_key(session,'batch-control',str(batch_id))
    row=session.get(BatchControl,batch_id)
    if row is None:raise FoundationError('BATCH_UNAVAILABLE','业务批次不存在。')
    if allow_publish:
        from app.data_foundation.reconciliation import reconcile_batch
        result=reconcile_batch(session,batch_id)
        if result.status != 'explained':
            raise FoundationError('RECONCILIATION_REQUIRED','批次仍有待处理或未解释差异，不能批准发布。')
    row.pause_a,row.pause_b,row.allow_publish=pause_a,pause_b,allow_publish
    if allow_publish and not pause_b:
        ids=select(BatchWork.work_id).where(BatchWork.batch_id==batch_id)
        for work in session.scalars(select(Work).where(Work.id.in_(ids),Work.status=='awaiting_publication').with_for_update()):
            work.status='queued'

    session.flush()


def may_publish(session,work):
    # All associated batches must agree. Reusing a candidate is safe; sharing a
    # governance work must not let a second batch bypass the first one's guard.
    ids=session.scalars(select(BatchWork.batch_id).where(BatchWork.work_id==work.id).order_by(BatchWork.batch_id)).all()
    for bid in ids:
        lock_key(session,'batch-control',str(bid))
        row=session.get(BatchControl,bid,populate_existing=True)
        if not row.allow_publish or row.pause_b:return False
    return True


def record_contributions(session,release):
    """Derive inherited contributors from actual members, not parent labels."""
    from app.data_foundation.holding_models import ReportBlockMember
    from app.data_foundation.inputs import input_manifests
    work=session.get(Work,release.work_id)
    pairs={(work.id,'governance')}
    pairs.update((m.work_id,'normalization') for m in input_manifests(session,work=work))
    blocks=select(BlockRef.block_id).where(BlockRef.release_id==release.id)
    decision_ids=set(session.scalars(select(BlockMember.decision_id).where(BlockMember.block_id.in_(blocks))))
    decision_ids.update(session.scalars(select(ReportBlockMember.decision_id).where(ReportBlockMember.block_id.in_(blocks))))
    for decision in session.scalars(select(Decision).where(Decision.id.in_(decision_ids))):
        if decision.work_id != work.id:pairs.add((decision.work_id,'inherited'))
    from app.data_foundation.work_models import OfficialBar,Candidate
    from app.data_foundation.holding_models import OfficialReport
    for member_model,official_model in ((BlockMember,OfficialBar),(ReportBlockMember,OfficialReport)):
        origins=session.scalars(select(Candidate.work_id).join(official_model,official_model.candidate_id==Candidate.id)
            .join(member_model,member_model.official_id==official_model.id).where(member_model.block_id.in_(blocks))).all()
        direct={wid for wid,role in pairs if role=='normalization'}
        pairs.update((wid,'inherited') for wid in origins if wid not in direct)
    for wid,role in sorted(pairs,key=lambda x:(str(x[0]),x[1])):
        if session.get(ReleaseContribution,(release.id,wid,role)) is None:
            session.add(ReleaseContribution(release_id=release.id,work_id=wid,role=role))
    session.flush()
