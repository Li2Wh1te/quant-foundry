"""A date-annotation correction preserves immutable contracts and record keys."""
import json
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.models import Definition
from app.data_foundation.record_models import RecordSubject,RecordBlockMember
from app.data_foundation.work_models import Work,BlockRef
from app.data_foundation import record_work
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.projection import projection_for

DOMAIN='market.popularity_history_snapshot'


def test_date_annotation_minor_leaves_original_definition_and_keys_immutable(session,monkeypatch):
    registered=record_work.register_catalog
    registered(session,DOMAIN,'tonghuashun')
    old=session.scalar(select(Definition).where(Definition.kind=='contract',Definition.name==DOMAIN,Definition.version=='1.0'))
    old_hash,old_json=old.content_hash,old.definition_json
    assert json.loads(old_json)['time_semantics']['business']==[]
    # Construct a real old-contract release without changing an existing work
    # fingerprint or mutating either registered definition.
    def old_registration(session,dataset,source):
        _,series,policy=registered(session,dataset,source)
        return old,series,policy
    with monkeypatch.context() as patch:
        patch.setattr(record_work,'register_catalog',old_registration)
        first=release_records(session,setup(session,[{'thscode':'A.SH','rank':1}],'hot_history','2025-11-27'))
    second=release_records(session,setup(session,[{'thscode':'B.SH','rank':1}],'hot_history','2025-11-27'),first)
    minor=session.get(Definition,session.get(Work,second.work_id).contract_id)
    assert minor.version=='1.1'
    assert json.loads(minor.definition_json)['time_semantics']['business']==['ranking_date']
    assert json.loads(minor.definition_json)['key_fields']==json.loads(old_json)['key_fields']
    session.refresh(old)
    assert (old.content_hash,old.definition_json)==(old_hash,old_json)
    def keys(release):
        return set(session.scalars(select(RecordBlockMember.target_key).join(BlockRef,BlockRef.block_id==RecordBlockMember.block_id).where(BlockRef.release_id==release.id)))
    assert keys(first)==keys(second)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='hot_history:2025-11-27'))
    for version in ['1.0','1.1']:
        spec=projection_for(session,DOMAIN,version)
        assert spec['business_date_field']=='ranking_date' and spec['readable_versions']==['1.0','1.1']
        req=RecordRequirement(dataset_id=DOMAIN,contract_version=version,
            semantic_series_id=f'tonghuashun-{DOMAIN}-observed-v1',subjects=[ident],fields=['members','ranking_date'],
            business_range={'from':'2025-11-27','to':'2025-11-27'})
        for release,expected in [(first,'A.SH'),(second,'B.SH')]:
            result=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
            assert result['request_satisfied'] and result['items'][0]['ranking_date']=='2025-11-27'
            assert result['items'][0]['members'][0]['source_code']==expected
        denied=json.loads(service(session).query_official(req.model_copy(update={'time_mode':'strict_public_pit'})))
        assert not denied['request_satisfied'] and not denied['items']
