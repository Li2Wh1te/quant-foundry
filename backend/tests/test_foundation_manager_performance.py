"""Provider manager series remain reported values, not comparable return curves."""
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert,rows_for
from app.data_foundation.canonical import FoundationError


def converted(rows,period='month',**metadata):
 ref=source('fund_manager_performance','M1.'+period)
 raw=dict(item=rows,collection_scope={'manager_id':'M1','range':period},timestamp=0)|metadata
 return convert(ref,rows_for(ref,raw)[0])


def test_exact_reported_series_are_sorted_but_not_subtracted_or_rescaled():
 body=converted([{'date_ms':1789488000000,'manager_return_pct':'2.06530193','peer_return_pct':'3.17783584','benchmark_return_pct':'4548.05'},
                 {'date_ms':1789401600000,'manager_return_pct':'-1.25','benchmark_return_pct':None}])['body']
 assert [p['trading_date'] for p in body['points']]==['2026-09-15','2026-09-16']
 assert body['points'][1]['reported_benchmark_return_pct']=='4548.05'
 assert body['points'][0]['reported_manager_return_pct']=='-1.25'
 assert body['points'][0]['reported_fields']==['manager_return_pct','benchmark_return_pct']
 assert body['excess_return'] is None and body['comparable_units'] is None
 assert body['effective_period_boundaries'] is None and body['reported_timestamp_ms']==0


@pytest.mark.parametrize('period',['month','tmonth','year','nowyear','now'])
def test_explicit_empty_windows_keep_distinct_provider_periods(period):
 body=converted([],period)['body'];assert body['points']==[] and body['provider_period']==period


@pytest.mark.parametrize('rows,period,metadata',[
 ([], 'week', {}),
 ([], 'month', {'collection_scope':{'manager_id':'OTHER','range':'month'}}),
 ([], 'month', {'collection_scope':{'manager_id':'M1','range':'now'}}),
 ([{'date_ms':1789401600000}]*2, 'month', {}),
 ([{'date_ms':'bad'}], 'month', {}),
 ([{'date_ms':1789401600000,'manager_return_pct':'NaN'}], 'month', {}),
 ([], 'month', {'timestamp':True}),
 (None, 'month', {}),
])
def test_ambiguous_or_invalid_whole_windows_are_rejected(rows,period,metadata):
 with pytest.raises(FoundationError):converted(rows,period,**metadata)
