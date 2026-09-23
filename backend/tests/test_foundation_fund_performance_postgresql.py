"""Actual publication replaces performance observations and preserves history."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Candidate


@pytest.mark.parametrize('native,domain,old,new,field,metadata',[
 ('fund_returns','fund.return_snapshot',[{'return_week':'-2.5'}],[{'return_week':None}],'periods',{}),
 ('fund_drawdowns','fund.drawdown_snapshot',[{'week':0}],[{}],'periods',{}),
 ('fund_performance_history','fund.performance_window',[{'date_ms':1789401600000,'rsi_pct':'50'}],[],'points',{'coverage':'observed_rows_only'}),
])
def test_complete_replacement_with_null_or_empty_observation(session,native,domain,old,new,field,metadata):
 create=lambda rows:setup(session,rows,native,'F.OF',collection_scope={'thscode':'F.OF'},**metadata)
 first=release_records(session,create(old));second=release_records(session,create(new),first)
 ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='F.OF'))
 req=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',subjects=[ident],fields=[field])
 historical=json.loads(service(session).query_official(req.model_copy(update={'release':first.id})))
 current=json.loads(service(session).query_official(req.model_copy(update={'release':second.id})))
 assert historical['request_satisfied'] and current['request_satisfied']
 before=historical['items'][0][field];after=current['items'][0][field]
 if field=='points':assert len(before)==1 and after==[]
 else:
  value='reported_return' if native=='fund_returns' else 'reported_drawdown'
  assert before[0][value] is not None and after[0][value] is None
 for change in [dict(fields=['calculation_formula']),dict(fields=['complete_history']),dict(time_mode='strict_public_pit')]:
  result=json.loads(service(session).query_official(req.model_copy(update=change)))
  assert not result['request_satisfied'] and not result['items']


def test_malformed_history_point_quarantines_whole_window(session):
 work,_,_=setup(session,[{'date_ms':1789401600000},{'date_ms':1789488000000,'rsi_pct':'NaN'}],
     'fund_performance_history','F.OF',collection_scope={'thscode':'F.OF'},coverage='observed_rows_only')
 candidates=list(session.scalars(select(Candidate).where(Candidate.work_id==work.id)))
 assert len(candidates)==1 and candidates[0].readiness=='quarantined'
