"""Ranking membership, dates and provider-only quantities stay explicit."""
from decimal import Decimal
import pytest
from pydantic import ValidationError
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.canonical import FoundationError
from tests.test_foundation_records import source


def converted(rows, dataset='hot_list', subject='hour', **metadata):
    ref = source(dataset, subject)
    values = rows_for(ref, dict(item=rows, **metadata))
    assert len(values) == 1
    return convert(ref, values[0])


def test_rank_order_is_reported_not_array_position_and_ties_survive():
    result = converted([{'thscode':'B.SH','rank':3,'heat':Decimal('0.0000000000000000001'),'rank_change':-2,'rank_trend':'down'},
                        {'thscode':'A.SH','rank':3,'heat':'107859.4'}])
    members = result['body']['members']
    assert [r['source_code'] for r in members] == ['A.SH','B.SH']
    assert [r['reported_rank'] for r in members] == [3,3]
    assert members[1]['reported_heat'] == '0.0000000000000000001'
    assert members[1]['reported_rank_change'] == -2
    assert result['body']['heat_method'] is None and result['business_date'] is None
    assert result['field_quality']['heat_method'] == 'HEAT_METHOD_UNVERIFIED'


@pytest.mark.parametrize('dataset',['hot_list','skyrocket'])
def test_period_scope_and_explicit_empty_are_not_complete_market_claims(dataset):
    result = converted([],dataset,'day',collection_scope={'period':'day'})
    assert result['body']['members'] == []
    assert result['body']['scope_basis'] == 'provider_returned_ranking_list'
    with pytest.raises(FoundationError):
        converted([],dataset,'day',collection_scope={'period':'hour'})
    with pytest.raises(FoundationError):
        rows_for(source(dataset,'day'),{})
    with pytest.raises(FoundationError):
        converted([],dataset,'week')


@pytest.mark.parametrize('bad',[{},None,{'thscode':'B.SH','rank':True},{'thscode':'B.SH','rank':'1'},
    {'thscode':'B.SH','rank':0},{'thscode':'B.SH','rank':1,'heat':float('inf')},
    {'thscode':'B.SH','rank':1,'heat':Decimal('NaN')},{'thscode':'B.SH','rank':1,'heat':'-1'},
    {'thscode':'B.SH','rank':1,'rank_change':True},{'thscode':'B.SH','rank':1,'name':42}])
def test_bad_member_never_publishes_its_valid_sibling(bad):
    with pytest.raises((FoundationError,ValueError,ValidationError)):
        converted([{'thscode':'A.SH','rank':1},bad])


def test_duplicate_source_code_is_rejected_without_discarding_a_row():
    with pytest.raises(ValidationError):
        converted([{'thscode':'A.SH','rank':1},{'thscode':'A.SH','rank':2}])


def test_historical_business_date_is_independent_of_observation_time():
    result=converted([{'thscode':'A.SH','rank':1}], 'hot_history','2025-11-27',
        date='2025-11-27',date_ms=1764172800000,requested_start='2025-11-27',
        requested_end='2025-11-27',collection_scope={'date':'2025-11-27'})
    assert result['business_date'].isoformat()=='2025-11-27'
    assert result['body']['period']=='historical_day'
    assert result['body']['members'][0]['reported_heat'] is None
    assert result['body']['historical_public_time'] is None


@pytest.mark.parametrize('metadata',[{}, {'date':'2025-11-26'}, {'date':'2025-11-27','date_ms':1764172800001},
    {'date':'2025-11-27','requested_end':'2025-11-28'}, {'date':'2025-11-27','collection_scope':{'date':'2025-11-26'}}])
def test_history_requires_matching_source_and_container_dates(metadata):
    with pytest.raises((FoundationError,ValueError)):
        converted([], 'hot_history','2025-11-27',**metadata)
