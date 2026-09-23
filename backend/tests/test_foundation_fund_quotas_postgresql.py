"""Published quota categories retain prior versions and reject unsafe reads."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_fund_quotas import fund,group
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.work_models import Candidate


@pytest.mark.parametrize('native,domain,rows',[
 ('fund_quota_summary','fund.quota_summary_snapshot',[dict(name='requested',buy='23',total='25',total_limit='870.00',unlimited='0')]),
 ('fund_quota_list','fund.quota_list_snapshot',group([fund(),fund('B.OF',classify=None)]))])
def test_complete_reports_empty_observation_and_fixed_history_are_readable(session,native,domain,rows):
    first=release_records(session,setup(session,rows,native,'requested'))
    second=release_records(session,setup(session,[],native,'requested'),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='requested'))
    req=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',subjects=[ident],fields=['reported_groups'])
    old=json.loads(service(session).query_official(req.model_copy(update={'release':first.id})))
    current=json.loads(service(session).query_official(req.model_copy(update={'release':second.id})))
    assert old['request_satisfied'] and len(old['items'][0]['reported_groups'])==1
    assert current['request_satisfied'] and current['items'][0]['reported_groups']==[]
    for changes in [{'fields':['comparable_quota_amounts']},{'fields':['quota_currency']},{'time_mode':'strict_public_pit'}]:
        denied=json.loads(service(session).query_official(req.model_copy(update=changes)))
        assert not denied['request_satisfied'] and not denied['items']


def test_invalid_member_prevents_partial_category_publication(session):
    work,_,_=setup(session,group([fund(),fund('B.OF')|{'quota':1}]),'fund_quota_list','requested')
    candidates=list(session.scalars(select(Candidate).where(Candidate.work_id==work.id)))
    assert len(candidates)==1 and candidates[0].readiness=='quarantined'
