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


def projection_for(session, dataset, version):
    definition=session.scalar(select(Definition).where(Definition.kind=='contract',Definition.name==dataset,Definition.version==version))
    spec=PROJECTIONS.get((dataset,version))
    if definition is None or spec is None:
        raise FoundationError('INVALID_REQUIREMENT','所选契约没有已实现的正式读取投影。')
    if spec['read_status']=='retired':
        raise FoundationError('CONTRACT_RETIRED','该契约普通读取已退役，请查看支持说明。')
    payload={**spec,'contract_hash':definition.content_hash}
    return {**payload,'hash':digest('reader-projection-v1',{k:v for k,v in payload.items() if k!='read_status'}),'definition':json.loads(definition.definition_json)}
