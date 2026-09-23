"""Actual style releases preserve earlier windows and reject unsupported claims."""
import json
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_manager_style import document
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Candidate


def create(session,raw):
 return setup(session,raw['item'],'fund_manager_style','M1',collection_scope=raw['collection_scope'],timestamp=raw['timestamp'])


def test_actual_empty_replacement_retains_historical_vector_and_denies_taxonomy(session):
 raw=document();first=release_records(session,create(session,raw))
 raw['item'][0]['industry_preferences']=[];second=release_records(session,create(session,raw),first)
 ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='M1'))
 req=RecordRequirement(dataset_id='fund.manager_style_snapshot',semantic_series_id='tonghuashun-fund.manager_style_snapshot-observed-v1',subjects=[ident],fields=['preferences'])
 for release,count in [(first,1),(second,0)]:
  out=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
  assert out['request_satisfied'] and len(out['items'][0]['preferences'])==count
  if count:assert out['items'][0]['preferences'][0]['reported_vector'][2] is None
 for changes in [dict(fields=['industry_taxonomy']),dict(fields=['normalized_industry_allocation']),dict(fields=['verified_investment_strategy']),dict(time_mode='strict_public_pit')]:
  out=json.loads(service(session).query_official(req.model_copy(update=changes)))
  assert not out['request_satisfied'] and not out['items']


def test_invalid_vector_is_quarantined_as_one_complete_observation(session):
 raw=document();raw['item'][0]['industry_preferences'][0]['percent']=[0]*6
 work,_,_=create(session,raw)
 candidates=list(session.scalars(select(Candidate).where(Candidate.work_id==work.id)))
 assert len(candidates)==1 and candidates[0].readiness=='quarantined'
