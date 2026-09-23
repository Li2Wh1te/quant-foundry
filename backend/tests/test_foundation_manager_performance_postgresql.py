"""Official manager series separate periods and replace complete observations."""
import json
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Candidate


def create(session,rows,period='month'):
 return setup(session,rows,'fund_manager_performance','M1.'+period,collection_scope={'manager_id':'M1','range':period})


def test_periods_stay_distinct_and_empty_replacement_preserves_history(session):
 row={'date_ms':1789401600000,'manager_return_pct':'2','benchmark_return_pct':'4548.05'}
 first=release_records(session,create(session,[row]))
 other=release_records(session,create(session,[row],'year'),first)
 current=release_records(session,create(session,[]),other)
 ids={s.source_key:s.id for s in session.scalars(select(RecordSubject))}
 assert ids['M1.month']!=ids['M1.year']
 req=RecordRequirement(dataset_id='fund.manager_performance_window',semantic_series_id='tonghuashun-fund.manager_performance_window-observed-v1',subjects=[ids['M1.month']],fields=['points','provider_period'])
 for release,count in [(first,1),(current,0)]:
  out=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
  assert out['request_satisfied'] and len(out['items'][0]['points'])==count
 out=json.loads(service(session).query_official(req.model_copy(update={'release':current.id,'subjects':[ids['M1.year']]})))
 assert out['request_satisfied'] and len(out['items'][0]['points'])==1
 for changes in [dict(fields=['excess_return']),dict(fields=['benchmark_identity']),dict(fields=['comparable_units']),dict(time_mode='strict_public_pit')]:
  out=json.loads(service(session).query_official(req.model_copy(update=changes)))
  assert not out['request_satisfied'] and not out['items']


def test_bad_child_quarantines_entire_window(session):
 work,_,_=create(session,[{'date_ms':1789401600000},{'date_ms':1789488000000,'peer_return_pct':'Infinity'}])
 candidates=list(session.scalars(select(Candidate).where(Candidate.work_id==work.id)))
 assert len(candidates)==1 and candidates[0].readiness=='quarantined'
