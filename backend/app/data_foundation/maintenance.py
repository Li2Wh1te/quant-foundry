"""Dependency closure includes inherited members and retained official values."""
import json
from sqlalchemy import select, or_
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import register_dependencies, lock_key, now
from app.data_foundation.models import DependencyEntry, Definition
from app.data_foundation.work_models import Work, Candidate, Decision, BlockRef, BlockMember, OfficialBar, Release
from app.data_foundation.holding_models import ReportBlockMember, OfficialReport
from app.data_foundation.batch_models import MaintenancePlan, MaintenanceTarget


MAX_IMPACT_ROWS = 10000


def bounded_rows(session, statement, *, scalar=False):
    """Reject oversized closures rather than return a misleading partial plan."""
    result = session.scalars(statement.limit(MAX_IMPACT_ROWS + 1)) if scalar else session.execute(statement.limit(MAX_IMPACT_ROWS + 1))
    rows = result.all()
    if len(rows) > MAX_IMPACT_ROWS:
        raise FoundationError('IMPACT_SCOPE_TOO_LARGE', '依赖影响超过本次核对上限，请缩小依赖范围后重试；未生成部分维护计划。')
    return rows


def impact(session, dependencies):
    if not 1 <= len(dependencies) <= 100:
        raise ValueError('Maintenance requires explicit bounded dependencies')
    conditions=[]
    policies=[]
    for entry in dependencies:
        if set(entry) != {'kind','id'} or entry['kind'] not in ('source_ref_id','binding_id','execution_id','definition_id'):
            raise ValueError('Invalid maintenance dependency')
        conditions.append(getattr(DependencyEntry,entry['kind'])==entry['id'])
        if entry['kind']=='definition_id':policies.append(entry['id'])
    dep_ids=select(DependencyEntry.manifest_id).where(or_(*conditions))
    direct_sources=[e['id'] for e in dependencies if e['kind']=='source_ref_id']
    works=bounded_rows(session,select(Work).where(or_(Work.dependency_id.in_(dep_ids),Work.source_ref_id.in_(direct_sources),
        Work.policy_id.in_(policies),Work.contract_id.in_(policies),
        Work.execution_id.in_([e['id'] for e in dependencies if e['kind']=='execution_id']))),scalar=True)
    wids={w.id for w in works}
    candidate_ids=select(Candidate.id).where(or_(Candidate.work_id.in_(wids),Candidate.dependency_id.in_(dep_ids),
        Candidate.source_ref_id.in_(direct_sources),Candidate.binding_id.in_([e['id'] for e in dependencies if e['kind']=='binding_id'])))
    from app.data_foundation.work_models import CandidateEntry
    from app.data_foundation.batch_models import CandidateInputEntry
    affected_manifests=select(CandidateEntry.manifest_id).where(CandidateEntry.candidate_id.in_(candidate_ids))
    affected_sets=select(CandidateInputEntry.input_set_id).where(CandidateInputEntry.manifest_id.in_(affected_manifests))
    wids.update(bounded_rows(session,select(Work.id).where(or_(Work.candidate_manifest_id.in_(affected_manifests),Work.candidate_input_set_id.in_(affected_sets))),scalar=True))
    decision_ids=set(bounded_rows(session,select(Decision.id).where(or_(Decision.work_id.in_(wids),Decision.selected_candidate_id.in_(candidate_ids))),scalar=True))
    official_bars=select(OfficialBar.id).where(OfficialBar.decision_id.in_(decision_ids))
    official_reports=select(OfficialReport.id).where(OfficialReport.decision_id.in_(decision_ids))
    # A retained value has a new decision but the same original official object.
    # Include both edges so policy/input changes are visible through inheritance.
    bars=bounded_rows(session,select(BlockRef.release_id,BlockMember.instrument_id,BlockMember.trade_date,BlockMember.decision_id)
        .join(BlockMember,BlockMember.block_id==BlockRef.block_id)
        .where(or_(BlockMember.decision_id.in_(decision_ids),BlockMember.official_id.in_(official_bars))))
    reports=bounded_rows(session,select(BlockRef.release_id,ReportBlockMember.target_key,ReportBlockMember.decision_id)
        .join(ReportBlockMember,ReportBlockMember.block_id==BlockRef.block_id)
        .where(or_(ReportBlockMember.decision_id.in_(decision_ids),ReportBlockMember.official_id.in_(official_reports))))
    affected={}
    for release_id,iid,day,did in bars:
        affected.setdefault(release_id,[]).append(dict(kind='daily_key',key=f'{iid}/{day}',decision_id=str(did)))
    for release_id,key,did in reports:
        affected.setdefault(release_id,[]).append(dict(kind='whole_report',key=key,decision_id=str(did)))
    # Missing key-level evidence is never interpreted as zero impact. Preserve a
    # conservative scope target for direct outputs, including empty releases.
    for release in bounded_rows(session,select(Release).where(Release.work_id.in_(wids)),scalar=True):
        affected.setdefault(release.id,[dict(kind='scope',key=release.scope_key,reason='KEY_EVIDENCE_UNAVAILABLE')])
    return dict(work_ids=sorted(map(str,wids)),candidate_ids=sorted(map(str,bounded_rows(session,candidate_ids,scalar=True))),
        targets=[dict(release_id=str(rid),keys=sorted(keys,key=lambda k:(k['kind'],k['key']))) for rid,keys in sorted(affected.items(),key=lambda x:str(x[0]))],
        snapshot_usage='not_registered',expansion='whole_report_or_explicit_key; scope_when_unknown')


def register_maintenance(session,dependencies):
    from uuid import UUID
    result=impact(session,dependencies)
    dependency=register_dependencies(session,[{e['kind']:e['id'],'purpose':'maintenance-trigger'} for e in dependencies])
    fingerprint=digest('maintenance-plan-v1',{'dependencies':dependency.manifest_hash,'impact':result})
    lock_key(session,'maintenance-plan',fingerprint)
    old=session.scalar(select(MaintenancePlan).where(MaintenancePlan.fingerprint==fingerprint))
    if old:return old
    row=MaintenancePlan(fingerprint=fingerprint,dependency_id=dependency.id,evidence_json=encode(result),created_at=now())
    session.add(row);session.flush()
    session.add_all([MaintenanceTarget(plan_id=row.id,release_id=UUID(t['release_id']),reason_json=encode(t['keys'])) for t in result['targets']]);session.flush()
    return row
