"""Provider performance observations do not imply known formulas or horizons."""
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert,rows_for
from app.data_foundation.canonical import FoundationError


def converted(native,row,**meta):
 ref=source(native,'F.OF');raw=dict(item=row,collection_scope={'thscode':'F.OF'})|meta
 return convert(ref,rows_for(ref,raw)[0])


def test_return_snapshot_retains_signed_values_rank_zero_and_timestamp_zero():
 body=converted('fund_returns',[dict(return_week='-1.25',rank_week=0,peer_average_week=None)],timestamp=0)['body']
 week=body['periods'][0]
 assert week['reported_return']=='-1.25' and week['reported_rank']==0
 assert week['reported_peer_average'] is None and 'reported_peer_average' in week['reported_fields']
 assert 'reported_rank_total' not in week['reported_fields']
 assert body['reported_timestamp_ms']==0 and body['timestamp_semantics'] is None
 assert body['calculation_formula'] is None and body['effective_period_boundaries'] is None


def test_drawdown_preserves_absence_null_and_zero_separately():
 body=converted('fund_drawdowns',[dict(thscode='F.OF',week=0,month=None)])['body']
 assert body['periods'][0]['reported_drawdown']=='0'
 assert body['periods'][1]['reported_fields']==['reported_drawdown']
 assert body['periods'][2]['reported_fields']==[]
 assert converted('fund_drawdowns',[dict(thscode='F.OF')])['body']['periods'][0]['reported_drawdown'] is None


def test_history_whole_window_sorts_dates_without_filling_unobserved_metrics():
 body=converted('fund_performance_history',[dict(date_ms=1789488000000,rsi_pct='51.1'),dict(date_ms=1789401600000,donchian_channel='-4.5')],coverage='observed_rows_only')['body']
 assert [p['trading_date'] for p in body['points']]==['2026-09-15','2026-09-16']
 assert body['points'][0]['reported_rsi_pct'] is None
 assert body['points'][0]['reported_donchian_channel']=='-4.5'
 assert converted('fund_performance_history',[],coverage='observed_rows_only')['body']['points']==[]


@pytest.mark.parametrize('native,rows,meta',[
 ('fund_returns',[{'rank_week':True}],{}),
 ('fund_returns',[{'return_week':'NaN'}],{}),
 ('fund_returns',[],{}),
 ('fund_drawdowns',[{'thscode':'OTHER.OF'}],{}),
 ('fund_drawdowns',[{}],{'timestamp':True}),
 ('fund_returns',[{}],{'collection_scope':{'thscode':'OTHER.OF'}}),
 ('fund_performance_history',[{'date_ms':1789401600000}]*2,{'coverage':'observed_rows_only'}),
 ('fund_performance_history',[{'date_ms':'bad'}],{'coverage':'observed_rows_only'}),
 ('fund_performance_history',[],{'coverage':'complete'}),
])
def test_ambiguous_or_invalid_observation_is_rejected(native,rows,meta):
 with pytest.raises(FoundationError):converted(native,rows,**meta)
