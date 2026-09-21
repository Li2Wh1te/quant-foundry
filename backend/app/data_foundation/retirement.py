"""Read-only retirement preflight; never change readers or delete history.

An operator records approval and independently verified recovery/history evidence
against the returned reference fingerprint. A changed reference set invalidates
that review. Deployment of a retired reader still requires a reviewed code change;
this module deliberately exposes no runtime retirement or cleanup endpoint.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.data_foundation.canonical import digest
from app.data_foundation.models import Definition
from app.data_foundation.work_models import Work, Release
from app.data_foundation.projection import PROJECTIONS


def review_retirement(session, dataset, version, *, notice_at=None,
                      approval_reference=None, evidence=None, at=None):
    """Evaluate the frozen 30-day policy against live immutable references.

    Failed, cancelled and sealed-but-unpublished work remains a reference. Only
    successful work has fulfilled its execution promise. Approval/evidence are
    operator attestations, not inferred from an elapsed deadline or software age.
    """
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None or (notice_at is not None and notice_at.tzinfo is None):
        raise ValueError('Retirement dates must include a timezone')
    definition = session.scalar(select(Definition).where(
        Definition.kind == 'contract', Definition.name == dataset, Definition.version == version))
    if definition is None:
        raise ValueError('Unknown contract')
    works = list(session.scalars(select(Work).where(Work.contract_id == definition.id).order_by(Work.id)))
    releases = list(session.scalars(select(Release).join(Work, Work.id == Release.work_id)
        .where(Work.contract_id == definition.id, Release.status == 'published').order_by(Release.id)))
    refs = {'contract_hash': definition.content_hash,
            'works': [{'id': str(w.id), 'status': w.status} for w in works],
            'releases': [{'id': str(r.id), 'manifest_hash': r.manifest_hash} for r in releases]}
    fingerprint = digest('retirement-reference-review-v1', refs)
    blockers = []
    if notice_at is None or at < notice_at + timedelta(days=30):
        blockers.append('退役通知尚未满30自然日。')
    if not isinstance(approval_reference, str) or not approval_reference.strip():
        blockers.append('缺少用户明确批准的记录。')
    if any(w.status != 'succeeded' for w in works):
        blockers.append('仍有未完成或待处理工作引用该契约。')
    proof = evidence or {}
    if proof.get('reference_fingerprint') != fingerprint:
        blockers.append('历史与恢复证据未绑定当前引用清单。')
    replacement = PROJECTIONS.get((dataset, proof.get('replacement_version')))
    if (not replacement or replacement.get('read_status') not in ('active', 'frozen')
            or proof.get('replacement_version') == version
            or version not in replacement.get('readable_versions', [proof.get('replacement_version')])):
        blockers.append('缺少已实现且兼容全部保留历史的替代读取入口。')
    for name, title in (('history_verification', '历史读取'), ('restore_verification', '恢复')):
        value = proof.get(name)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            blockers.append(f'缺少{title}验收证据摘要。')
    return {'status': 'blocked' if blockers else 'ready_for_manual_review',
            'message': '契约退役检查未通过，继续保留当前服务。' if blockers else '契约退役前置检查通过，仍需人工审核部署；历史数据保持保留。',
            'dataset': dataset, 'version': version, 'reference_fingerprint': fingerprint,
            'retained_releases': len(releases), 'unfinished_works': sum(w.status != 'succeeded' for w in works),
            'blockers': blockers, 'deletion_allowed': False}
