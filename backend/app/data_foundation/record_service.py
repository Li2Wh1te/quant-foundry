"""Typed-record adapter behind FoundationService's authentication and token codec."""
from uuid import UUID
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.record_query import RecordRequirement, evaluate
from app.data_foundation.projection import resolve_projected_release as resolve_release
from app.data_foundation.projection import projection_for
from app.data_foundation.resolution import token_hash
from app.data_foundation.work import scope_key
from app.data_foundation.work_models import Head, Release


def check(svc, request, *, snapshot_hash=None):
    projection = projection_for(svc.session, request.dataset_id, request.contract_version)
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
    if isinstance(page, RecordRequirement):
        checked = check(svc, page)
        if not checked['resolution_token']:
            return checked
        page = PageRequest(resolution_token=checked['resolution_token'], page_size=page.page_size)
    payload = svc.codec.read(page.resolution_token, 'resolution')
    if payload['owner_hash'] != digest('owner-v1', svc.owner()):
        raise FoundationError('AUTH_CONTEXT_CHANGED', '领域读取凭据不属于当前身份。')
    request = RecordRequirement.model_validate(payload['request'])
    projection = projection_for(svc.session, request.dataset_id, request.contract_version)
    release = resolve_release(svc.session, request)
    if (release is None or str(release.id) != payload['release_id']
            or release.manifest_hash != payload['manifest_hash'] or projection['hash'] != payload['projection_hash']
            or digest('request-v1', payload['request']) != payload['request_hash']):
        raise FoundationError('INVALID_RESOLUTION', '固定领域读取上下文不匹配。')
    after = None
    if page.cursor:
        cursor = svc.codec.read(page.cursor, 'cursor')
        if cursor['resolution_hash'] != token_hash(page.resolution_token):
            raise FoundationError('CURSOR_MISMATCH', '领域游标不属于本次固定读取。')
        after = cursor['after']
    if page.excluded_cursor:
        raise FoundationError('INVALID_REQUIREMENT', '领域请求最多1000个对象，限制清单随响应完整返回。')
    result = evaluate(svc.session, request, svc.owner, expected_epoch=payload['issue_epoch'],
                      after=after, page_size=page.page_size)
    result.update(resolution_token=page.resolution_token, expires_at=payload['expires_at'],
                  projection_hash=projection['hash'], projection_version=projection['version'],
                  snapshot_hash=payload.get('snapshot_hash'))
    result['next_cursor'] = svc.codec.sign('cursor', {'resolution_hash': token_hash(page.resolution_token),
                            'after': result['next_key']}) if result['has_more'] else None
    return result



def describe(svc, dataset):
    from sqlalchemy import select, func
    from app.data_foundation.catalog import now
    from app.data_foundation.models import Definition
    from app.data_foundation.record_models import RecordBlockMember
    from app.data_foundation.work_models import BlockRef, Candidate, Work
    from app.data_foundation.record_schemas import schema_for
    from app.data_foundation.record_work import series_for
    from app.data_foundation.record_adapters import SOURCE_DATASETS
    schema = schema_for(dataset)
    projection = projection_for(svc.session, dataset, '1.0')
    series = []
    for source in sorted({s for (s, _), d in SOURCE_DATASETS.items() if d == dataset}):
        name = series_for(dataset, source)
        head = svc.session.get(Head, scope_key(dataset, 1, 'default', name))
        release = svc.session.get(Release, head.release_id) if head else None
        count, first, last = svc.session.execute(select(func.count(), func.min(RecordBlockMember.business_date),
            func.max(RecordBlockMember.business_date)).join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
            .where(BlockRef.release_id == release.id, RecordBlockMember.state == 'value')).one() if release else (0, None, None)
        series.append(dict(series=name, source=source, release_id=release.id if release else None,
            official_count=count, range={'from': first, 'to': last},
            manifest_hash=release.manifest_hash if release else None, published_at=release.published_at if release else None))
    candidates = svc.session.scalar(select(func.count()).select_from(Candidate).join(Work, Work.id == Candidate.work_id)
        .join(Definition, Definition.id == Work.contract_id).where(Definition.name == dataset))
    return dict(dataset=dataset, name=schema.name, kind='typed_records', version='1.0', profile='default', series=series,
        as_of=now(), candidate_count=candidates, official_count=sum(s['official_count'] for s in series),
        business_as_of=max((s['range']['to'] for s in series if s['range']['to']), default=None),
        fields=[dict(key=k, required=k in schema.core_fields) for k in schema.body.model_fields],
        contract=projection['definition'], projection={k: v for k, v in projection.items() if k != 'definition'},
        limitations=list(schema.limitations), coverage='published_keys_only',
        support=dict(read='active', update='bounded_local_versions', time_modes=['observed'], max_objects=1000))


def subjects(svc, dataset, series, release_id, *, after=None, search='', limit=50):
    """Discover identities within one immutable manifest, without reading values.

    Keyset pagination is scoped by the explicit release and semantic series.
    Only source-local identity keys are exposed; restricted canonical names and
    values still require the ordinary capability check and signed read token.
    """
    from types import SimpleNamespace
    from sqlalchemy import select, exists
    from app.data_foundation.record_models import RecordSubject, RecordBlockMember
    from app.data_foundation.work_models import BlockRef
    owner = svc.owner()
    if not owner:
        raise FoundationError('AUTH_REQUIRED', '主体目录读取需要有效身份。')
    release = resolve_release(svc.session, SimpleNamespace(dataset_id=dataset, contract_version='1.0',
        profile_id='default', semantic_series_id=series, release=release_id))
    statement = select(RecordSubject).where(exists(select(1).select_from(RecordBlockMember)
        .join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id).where(
            BlockRef.release_id == release.id, RecordBlockMember.subject_id == RecordSubject.id)))
    if after:
        statement = statement.where(RecordSubject.id > after)
    if search:
        statement = statement.where(RecordSubject.source_key.icontains(search, autoescape=True))
    rows = svc.session.scalars(statement.order_by(RecordSubject.id).limit(limit + 1)).all()
    if svc.owner() != owner:
        raise FoundationError('AUTH_CONTEXT_CHANGED', '主体目录读取身份已失效。')
    return dict(release_id=release.id, items=[dict(id=r.id, source=r.source, source_key=r.source_key,
        kind=r.kind) for r in rows[:limit]], next_after=rows[limit - 1].id if len(rows) > limit else None)
