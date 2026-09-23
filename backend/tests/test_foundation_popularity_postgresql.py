"""Ranking snapshots replace complete lists and keep historical reads fixed."""
import json
from datetime import datetime,timezone
from uuid import uuid4
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_sources import execution
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from app.data_ingestion.tonghuashun.contracts import exact_json,content_hash
from app.data_foundation.source_refs import register_observation
from app.data_foundation.record_work import create_normalization,validate_release
from app.data_foundation.work import claim
from app.data_foundation.bars import normalize_batch
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.work_models import Candidate


def setup(session,rows,dataset,subject,**metadata):
    ex=execution(session)
    payload={'item':rows,**metadata}
    if dataset=='hot_history':payload['date']=subject
    observation=Observation(id=uuid4(),dataset=dataset,subject=subject,variant='default',
        observed_at=datetime.now(timezone.utc),request_json='{}',data_json=exact_json(payload),
        content_hash=content_hash(payload),row_count=len(rows),chain_depth=0)
    session.add(observation);session.flush()
    ref=register_observation(session,observation.id,ex.id)
    work,policy=create_normalization(session,source_ref_id=ref.id,execution_id=ex.id)
    lease=claim(session,work_id=work.id)
    normalize_batch(session,work.id,lease.lease_epoch)
    return work,ex,policy


@pytest.mark.parametrize('dataset,subject,domain',[
    ('hot_list','hour','market.popularity_snapshot'),
    ('skyrocket','day','market.rising_popularity_snapshot'),
    ('hot_history','2025-11-27','market.popularity_history_snapshot')])
def test_all_members_replace_and_previous_release_remains_readable(session,dataset,subject,domain):
    first=release_records(session,setup(session,[{'thscode':'A.SH','rank':2},{'thscode':'B.SH','rank':1}],dataset,subject))
    second=release_records(session,setup(session,[{'thscode':'A.SH','rank':1}],dataset,subject),first)
    third=release_records(session,setup(session,[],dataset,subject),second)
    validate_release(session,third)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key==f'{dataset}:{subject}'))
    extra={'business_range':{'from':subject,'to':subject}} if dataset=='hot_history' else {}
    request=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',
        subjects=[ident],fields=['members'],**extra)
    for release,expected in [(first,['B.SH','A.SH']),(second,['A.SH']),(third,[])]:
        result=json.loads(service(session).query_official(request.model_copy(update={'release':release.id})))
        assert result['request_satisfied']
        assert [m['source_code'] for m in result['items'][0]['members']]==expected
    for changes in [{'time_mode':'strict_public_pit'},{'fields':['heat_method']}]:
        result=json.loads(service(session).query_official(request.model_copy(update=changes)))
        assert not result['request_satisfied'] and not result['items']


def test_invalid_member_quarantines_the_whole_source_observation(session):
    work,_,_=setup(session,[{'thscode':'A.SH','rank':1},{'thscode':'B.SH'}],'hot_list','hour')
    candidates=list(session.scalars(select(Candidate).where(Candidate.work_id==work.id)))
    assert len(candidates)==1 and candidates[0].readiness=='quarantined'
