"""Report adapter behind FoundationService's authentication and token codec."""
from uuid import UUID
from sqlalchemy import select, func, text
import json
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.holdings import DATASET, SERIES
from app.data_foundation.holding_query import ReportRequirement, evaluate, resolve_release
from app.data_foundation.projection import projection_for
from app.data_foundation.resolution import token_hash
from app.data_foundation.work import scope_key
from app.data_foundation.work_models import Work, Head, Candidate, Release, BlockRef
from app.data_foundation.holding_models import ReportBlockMember, OfficialReport
from app.data_foundation.catalog import now
from app.data_foundation.models import SourceRef


def check(svc, request, *, snapshot_hash=None):
    projection = projection_for(svc.session, DATASET, request.contract_version)
    result = evaluate(svc.session, request, svc.owner, check_only=True)
    result.update(resolution_token=None, projection_hash=projection['hash'], projection_version=projection['version'])
    if result['release_id']:
        fixed = request.model_copy(update={'release': UUID(result['release_id'])})
        normalized = fixed.model_dump(mode='json', by_alias=True)
        issued = int(svc.codec.clock())
        payload = {'request': normalized, 'request_hash': digest('request-v1', normalized),
            'release_id': result['release_id'], 'manifest_hash': result['manifest_hash'],
            'projection_hash': projection['hash'], 'issue_epoch': result['issue_state_version'],
            'owner_hash': digest('owner-v1', svc.owner()), 'issued_at': issued, 'expires_at': issued + 900,
            'snapshot_hash': snapshot_hash}
        result['resolution_token'] = svc.codec.sign('resolution', payload)
        result['expires_at'] = issued + 900
        result['request'] = normalized
    result['snapshot_hash'] = snapshot_hash
    return result


def query(svc, page):
    from app.data_foundation.service import PageRequest
    if isinstance(page, ReportRequirement):
        checked = check(svc, page)
        if not checked['resolution_token']:
            return checked
        page = PageRequest(resolution_token=checked['resolution_token'], page_size=page.page_size)
    payload = svc.codec.read(page.resolution_token, 'resolution')
    if payload['owner_hash'] != digest('owner-v1', svc.owner()):
        raise FoundationError('AUTH_CONTEXT_CHANGED', '报告读取凭据不属于当前身份。')
    request = ReportRequirement.model_validate(payload['request'])
    projection = projection_for(svc.session, DATASET, request.contract_version)
    release = resolve_release(svc.session, request)
    if (release is None or str(release.id) != payload['release_id']
            or release.manifest_hash != payload['manifest_hash'] or projection['hash'] != payload['projection_hash']
            or digest('request-v1', payload['request']) != payload['request_hash']):
        raise FoundationError('INVALID_RESOLUTION', '固定报告读取上下文不匹配。')
    after = None
    if page.cursor:
        cursor = svc.codec.read(page.cursor, 'cursor')
        if cursor['resolution_hash'] != token_hash(page.resolution_token):
            raise FoundationError('CURSOR_MISMATCH', '报告游标不属于本次固定读取。')
        after = cursor['after']
    if page.excluded_cursor:
        raise FoundationError('INVALID_REQUIREMENT', '报告请求最多100个对象，限制清单随响应完整返回。')
    result = evaluate(svc.session, request, svc.owner, expected_epoch=payload['issue_epoch'],
                      after=after, page_size=page.page_size)
    result.update(resolution_token=page.resolution_token, expires_at=payload['expires_at'],
                  projection_hash=projection['hash'], projection_version=projection['version'],
                  snapshot_hash=payload.get('snapshot_hash'))
    result['next_cursor'] = svc.codec.sign('cursor', {'resolution_hash': token_hash(page.resolution_token),
                            'after': result['next_key']}) if result['has_more'] else None
    return result


def describe(svc):
    session = svc.session
    scope = scope_key(DATASET, 1, 'default', SERIES)
    head = session.get(Head, scope)
    release = session.get(Release, head.release_id) if head else None
    candidates = session.scalar(select(func.count()).select_from(Candidate).join(Work, Work.id == Candidate.work_id)
                                .where(Work.scope_key == scope))
    quarantined = session.scalar(select(func.count()).select_from(Candidate).join(Work, Work.id == Candidate.work_id)
        .where(Work.scope_key == scope, Candidate.readiness != 'ready'))
    latest_work = session.scalar(select(Work).where(Work.scope_key == scope, Work.kind == 'A')
        .order_by(Work.created_at.desc(), Work.id.desc()).limit(1))
    source_objects = select(Candidate.source_ref_id, Candidate.unit_key).join(Work, Work.id == Candidate.work_id)
    source_objects = source_objects.where(Work.scope_key == scope).distinct().subquery()
    source_count = session.scalar(select(func.count()).select_from(source_objects))
    official_revisions = session.scalar(select(func.count()).select_from(OfficialReport)
        .join(Candidate, Candidate.id == OfficialReport.candidate_id).join(Work, Work.id == Candidate.work_id)
        .where(Work.scope_key == scope))
    rows = session.execute(select(ReportBlockMember.fund_share_id, ReportBlockMember.period_end, OfficialReport.member_count)
        .outerjoin(OfficialReport, OfficialReport.id == ReportBlockMember.official_id)
        .join(BlockRef, BlockRef.block_id == ReportBlockMember.block_id)
        .where(BlockRef.release_id == release.id)).all() if release else []
    return {'dataset': DATASET, 'name': '基金持仓报告', 'version': '1.0', 'profile': 'default', 'series': SERIES,
        'release_id': str(release.id) if release else None, 'manifest_hash': release.manifest_hash if release else None,
        'published_at': release.published_at if release else None, 'candidate_count': candidates,
        'quarantined_count': quarantined, 'candidate_work_id': str(latest_work.id) if latest_work else None,
        'source_report_count': source_count, 'official_revision_count': official_revisions,
        'official_count': sum(r[2] is not None for r in rows), 'manifest_object_count': len(rows),
        'member_count': sum(r[2] or 0 for r in rows),
        'subjects': [{'instrument_id': iid, 'code': code, 'name': code} for iid, code in _subjects(session, scope)],
        'range': {'from': min((str(r[1]) for r in rows), default=None), 'to': max((str(r[1]) for r in rows), default=None)},
        'business_as_of': max((str(r[1]) for r in rows if r[2] is not None), default=None),
        'limitations': ['仅供应商返回的报告范围；未验证全投资组合完整性。', '持仓金额单位未验证。', '历史披露时间未验证。'],
        'support': {'read': 'active', 'update': 'bounded_manual', 'replay': 'see_source_execution', 'time_modes': ['observed'], 'max_objects': 100},
        'as_of': now(), 'view_snapshot_id': session.scalar(text('SELECT pg_current_snapshot()::text'))}


def _subjects(session, scope):
    from app.data_foundation.models import Binding
    records = session.execute(select(Binding.instrument_id, Binding.subject).join(Candidate, Candidate.binding_id == Binding.id)
        .join(Work, Work.id == Candidate.work_id)
        .where(Work.scope_key == scope)).all()
    return sorted({(str(iid), code.removeprefix('fund_share:')) for iid, code in records})
