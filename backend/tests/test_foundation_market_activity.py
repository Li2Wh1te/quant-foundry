"""Source activity lists are atomic and never imply verified market coverage."""
import copy
import pytest
from pydantic import ValidationError
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert,rows_for
from app.data_foundation.canonical import FoundationError
from app.data_foundation.market_activity import GROUPS


def converted(native,rows,subject='2026-09-15',**metadata):
    ref=source(native,subject)
    return convert(ref,rows_for(ref,dict(item=rows)|metadata)[0])


def member(**changes):
    return dict(thscode='A.SH',ticker='DISPLAY',name='Name')|changes


def ladder():
    days=[dict(date=day,boards={g:[] for g in GROUPS}) for day in ['2026-09-15','2026-09-14']]
    days[0]['boards']['two_board']=[member(board_num=2,seal_nextday=None,sign_level=0)]
    return days,dict(coverage='provider_rolling_window',window=dict(length=2,
        date_list=['2026-09-15','2026-09-14'],board_caps={g:2 for g in GROUPS}))


def test_final_auction_preserves_signed_unmatched_and_missing_fields():
    result=converted('stock_auction',[member(auction_unmatched='-57.25',auction_price='0')],'A.SH',
        auction_phase='closed',data_status='final',timestamp=1789972911861,collection_scope={'thscodes':'A.SH'})
    body=result['body'];assert body['reported_auction_unmatched']=='-57.25' and body['reported_auction_price']=='0'
    assert body['reported_auction_volume'] is None and result['field_quality']['reported_auction_volume']=='SOURCE_FIELD_ABSENT'
    assert body['reported_phase']=='closed' and body['effective_business_date'] is None
    assert result['business_date'] is None and body['executable_quote'] is None


@pytest.mark.parametrize('native', ['auction_benchmark','limit_up','limit_down','limit_break'])
def test_dated_lists_keep_membership_empty_sets_and_explicit_scope(native):
    row=member(auction_pct='-0.1',tags=['source tag'],last_price='12',price_change_ratio_pct='-1.2')
    out=converted(native,[row],date='2026-09-15',requested_start='2026-09-15',requested_end='2026-09-15',
        collection_scope={'date_ms':1789401600000})
    assert out['business_date'].isoformat()=='2026-09-15' and out['body']['trading_date']=='2026-09-15'
    assert out['body']['members'][0]['source_code']=='A.SH'
    assert out['body']['complete_market_coverage'] is None
    assert converted(native,[])['body']['members']==[]
    with pytest.raises(FoundationError):converted(native,[row,row])
    with pytest.raises(FoundationError):converted(native,[row],collection_scope={'date':'2026-09-14'})


def test_anomaly_narratives_keep_repeated_subject_occurrences_unverified():
    row=dict(thscode='A.SH',stock_name='Name',tag_name='source tag',keyword_list=['keyword'],analysis_content='Source narrative')
    out=converted('anomaly_list',[row,row|{'analysis_content':'Another narrative'}],'market')
    assert [r['source_order'] for r in out['body']['members']]==[0,1]
    assert out['body']['members'][1]['reported_analysis']=='Another narrative'
    assert out['body']['factual_verification'] is None and out['body']['event_timestamps'] is None
    assert converted('anomaly_list',[],'market')['body']['members']==[]
    with pytest.raises(FoundationError):converted('anomaly_list',[row],'OTHER')


def test_ladder_keeps_reported_caps_and_dates_without_extending_coverage():
    days,metadata=ladder();out=converted('limit_ladder',days,'market',**metadata)
    assert [d['trading_date'] for d in out['body']['days']]==['2026-09-14','2026-09-15']
    assert out['body']['days'][1]['reported_groups']['two_board'][0]['reported_seal_nextday'] is None
    assert out['body']['declared_board_caps']['two_board']==2
    assert out['body']['complete_market_coverage'] is None
    with pytest.raises(FoundationError):converted('limit_ladder',days[:1],'market',**metadata)
    broken=copy.deepcopy(days);broken[0]['boards'].pop('two_board')
    with pytest.raises(FoundationError):converted('limit_ladder',broken,'market',**metadata)
    broken=copy.deepcopy(days);broken[0]['boards']['two_board']*=2
    with pytest.raises(FoundationError):converted('limit_ladder',broken,'market',**metadata)


@pytest.mark.parametrize('native,rows,metadata',[
 ('stock_auction',[member()],dict(auction_phase='open',data_status='final')),
 ('stock_auction',[member(thscode='OTHER.SH')],dict(auction_phase='closed',data_status='final')),
 ('stock_auction',[member(auction_volume=-1)],dict(auction_phase='closed',data_status='final')),
 ('stock_auction',[member()],dict(auction_phase='closed',data_status='final',timestamp=True)),
 ('limit_up',[member(limit_up_time='25:00')],{}),
 ('limit_up',[member(is_st=1)],{}),
 ('limit_up',[member(continue_day_cnt='2')],{}),
 ('limit_down',[member(first_limit_time='9:30')],{}),
 ('limit_break',[member(open_times=-1)],{}),
 ('limit_break',[member(turnover='NaN')],{}),
 ('auction_benchmark',[member(tags='not a list')],{}),
 ('anomaly_list',[dict(thscode='A.SH',keyword_list='bad')],{}),
])
def test_invalid_member_or_scope_isolates_whole_observation(native,rows,metadata):
    subject='A.SH' if native=='stock_auction' else 'market' if native=='anomaly_list' else '2026-09-15'
    with pytest.raises((FoundationError,ValidationError)):converted(native,rows,subject,**metadata)
