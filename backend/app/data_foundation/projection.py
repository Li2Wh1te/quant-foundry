"""Executable reader registry independent from provider decoding runtimes.

A projection hash pins ordering, representation and null semantics. Additional
contracts require an explicit reader entry, not an opportunistic newest version.
"""
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.models import Definition
import json

PROJECTIONS={('market.bar.daily','1.0'):{'version':'daily-1','fields':
    ['open','high','low','close','volume','turnover'],'sort':['trade_date','instrument_id'],
    'decimal':'canonical-string','nulls':'unavailable-field','read_status':'active'}}


PROJECTIONS[('fund.holdings_report','1.0')] = {
    'version': 'holdings-1', 'fields': ['hold_ratio','market_value','period_change_ratio','rank'],
    'sort': ['period_end','fund_share_id','report_type','period_start','scope_kind','series','target_key'],
    'member_sort': ['member_ordinal'], 'decimal':'canonical-string',
    'nulls':'unavailable-field', 'read_status':'active'}


def projection_for(session, dataset, version):
    definition=session.scalar(select(Definition).where(Definition.kind=='contract',Definition.name==dataset,Definition.version==version))
    spec=PROJECTIONS.get((dataset,version))
    if definition is None or spec is None:
        raise FoundationError('INVALID_REQUIREMENT','所选契约没有已实现的正式读取投影。')
    if spec['read_status']=='retired':
        raise FoundationError('CONTRACT_RETIRED','该契约普通读取已退役，请查看支持说明。')
    payload={**spec,'contract_hash':definition.content_hash}
    return {**payload,'hash':digest('reader-projection-v1',{k:v for k,v in payload.items() if k!='read_status'}),'definition':json.loads(definition.definition_json)}


def resolve_projected_release(session, request):
    """Compatibility is an explicit executable reader declaration.

    A minor version may opt into reading older immutable releases. Major heads
    always remain disjoint, even if two adapters happen to share SQL storage.
    Resolving latest performs the same checks as resolving an explicit ID.
    """
    from app.data_foundation.work import scope_key
    from app.data_foundation.work_models import Head, Release, Work
    spec = projection_for(session, request.dataset_id, request.contract_version)
    major = int(request.contract_version.split('.')[0])
    scope = scope_key(request.dataset_id, major, request.profile_id, request.semantic_series_id)
    if request.release == 'latest':
        head = session.get(Head, scope)
        if head is None:
            return None
        release = session.get(Release, head.release_id)
    else:
        release = session.get(Release, request.release)
    if release is None or release.status != 'published':
        raise FoundationError('RELEASE_UNAVAILABLE', '指定正式发布不存在或尚未发布。')
    contract = session.get(Definition, session.get(Work, release.work_id).contract_id)
    if (release.scope_key != scope or contract.name != request.dataset_id
            or contract.version not in spec.get('readable_versions', [request.contract_version])):
        raise FoundationError('CONTRACT_RELEASE_MISMATCH', '读取投影与所选正式发布的契约或语义不兼容。')
    return release
