"""Reported unlabeled preference vectors must never become named allocations."""
import copy
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert,rows_for
from app.data_foundation.canonical import FoundationError


def document():
 return dict(collection_scope={'manager_id':'M1'},timestamp=0,item=[dict(industry_preferences=[
     dict(report_tag='2026Q2',percent=['0.1',0,None,'0.2','0.3','0.1','0.3'],total_fund_scale='123.45')],
     investment_idea='Provider commentary',representative_fund_thscode='F.OF',representative_fund_ticker='F',
     representative_fund_name='Fund',total_fund_scale=None)])


def converted(raw):
 ref=source('fund_manager_style','M1');return convert(ref,rows_for(ref,raw)[0])


def test_preserves_vector_positions_and_source_labels_without_normalization():
 body=converted(document())['body'];period=body['preferences'][0]
 assert period['reported_vector']==['0.1','0',None,'0.2','0.3','0.1','0.3']
 assert period['reported_period_tag']=='2026Q2' and period['reported_total_fund_scale']=='123.45'
 assert body['industry_taxonomy'] is None and body['normalized_industry_allocation'] is None
 assert body['reported_investment_idea']=='Provider commentary' and body['verified_investment_strategy'] is None
 assert body['reported_timestamp_ms']==0


def test_explicit_empty_preferences_remain_valid_but_absence_does_not():
 raw=document();raw['item'][0]['industry_preferences']=[]
 assert converted(raw)['body']['preferences']==[]
 del raw['item'][0]['industry_preferences']
 with pytest.raises(FoundationError):converted(raw)


@pytest.mark.parametrize('mutation',[
 lambda r:r['collection_scope'].update(manager_id='OTHER'),
 lambda r:r.update(timestamp=True),
 lambda r:r.update(item=[]),
 lambda r:r['item'][0].update(investment_idea=12),
 lambda r:r['item'][0]['industry_preferences'][0].update(percent=[0]*6),
 lambda r:r['item'][0]['industry_preferences'][0].update(percent=[0]*6+['NaN']),
 lambda r:r['item'][0]['industry_preferences'][0].update(total_fund_scale=-1),
 lambda r:r['item'][0]['industry_preferences'].append(copy.deepcopy(r['item'][0]['industry_preferences'][0])),
])
def test_invalid_complete_observation_is_rejected(mutation):
 raw=document();mutation(raw)
 with pytest.raises(FoundationError):converted(raw)
