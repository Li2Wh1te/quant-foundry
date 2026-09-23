"""Leaderboard observations retain periods, repeated occurrences and nested sets."""
import copy
import pytest
from app.data_foundation.record_adapters import convert,rows_for
from app.data_foundation.canonical import FoundationError
from tests.test_foundation_records import source


def document():
    stock=dict(thscode='A.SH',ticker='A',name='Name',range_days=1,concept_list=[],buy_value='10.01',net_value='-2.5')
    return dict(collection_scope=dict(board_type='all',date='2026-09-15'),
        provider_metadata=dict(board_type='all',trade_date='2026-09-15',count=3,stock_count=1),
        item=[dict(board_type='all',stock_items=[stock,stock|{'range_days':3},stock|{'net_value':'0'}],
                   hot_money_items=[dict(name='Source label',buying='-4.5',rows=[stock])])])


def converted(raw):
    ref=source('dragon_tiger','2026-09-15.all')
    return convert(ref,rows_for(ref,raw)[0])


def test_repeated_code_and_period_preserve_each_source_occurrence():
    out=converted(document());body=out['body']
    assert [r['source_order'] for r in body['stocks']]==[0,1,2]
    assert [r['reported_range_days'] for r in body['stocks']]==[1,3,1]
    assert [r['reported_net_value'] for r in body['stocks']]==['-2.5','-2.5','0']
    assert body['hot_money'][0]['reported_buying']=='-4.5'
    assert body['stocks'][0]['reported_sell_value'] is None
    assert body['duplicate_resolution'] is None and body['participant_identity'] is None
    assert out['business_date'].isoformat()=='2026-09-15'


def test_explicit_empty_container_is_distinct_from_missing_container():
    raw=document();raw['item'][0].update(stock_items=[],hot_money_items=[])
    assert converted(raw)['body']['stocks']==[]
    raw['item']=[]
    with pytest.raises(FoundationError):converted(raw)


@pytest.mark.parametrize('mutation',[
 lambda r:r['collection_scope'].update(board_type='org'),
 lambda r:r['provider_metadata'].update(trade_date='2026-09-14'),
 lambda r:r['item'][0]['stock_items'][0].update(range_days=True),
 lambda r:r['item'][0]['stock_items'][0].update(sell_value='NaN'),
 lambda r:r['item'][0]['stock_items'][0].update(concept_list=None),
 lambda r:r['item'][0]['hot_money_items'][0].update(rows=None),
 lambda r:r['item'][0]['stock_items'][0].update(org_buy_num=-1),
])
def test_invalid_nested_evidence_rejects_whole_observation(mutation):
    raw=copy.deepcopy(document());mutation(raw)
    with pytest.raises(FoundationError):converted(raw)
