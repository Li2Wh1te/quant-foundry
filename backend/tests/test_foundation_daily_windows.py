"""Whole price windows never mix bases or imply complete historical coverage."""
from decimal import Decimal
import pytest
from pydantic import ValidationError
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert,rows_for
from app.data_foundation.canonical import FoundationError


def point(day='2026-09-01',**changes):
    return dict(date_ms=day,open_price='10',high_price='12',low_price='9',close_price='11',volume='0',turnover=None)|changes


def converted(points,dataset='stock_daily',adjust='none',**metadata):
    ref=source(dataset,'A.SH')
    payload=dict(item=points,adjust=adjust,coverage='observed_rows_only')|metadata
    return convert(ref,rows_for(ref,payload)[0])


@pytest.mark.parametrize('native,adjust,domain,asset',[
 ('stock_daily','none','market.stock_daily_window','a-share'),
 ('etf_daily','forward','market.etf_daily_window','fund-etf'),
 ('index_daily','not_applicable','market.index_daily_window','a-share-index')])
def test_each_source_keeps_its_declared_basis_and_local_identity(native,adjust,domain,asset):
    result=converted([point()],native,adjust)
    assert result['dataset']==domain and result['subject_kind']=='asset:'+asset
    assert result['body']['asset_type']==asset and result['body']['reported_adjustment']==adjust
    assert result['business_date'] is None
    for field in ['currency','volume_unit','turnover_unit','adjustment_anchor','adjustment_formula']:
        assert result['body'][field] is None and result['field_quality'][field]


def test_request_envelope_does_not_replace_actual_materialized_date_bounds():
    result=converted([point('2026-09-15'),point('2026-09-01')],requested_start='2016-01-01',requested_end='2026-09-22')
    body=result['body']
    assert body['sequence_start']=='2026-09-01' and body['sequence_end']=='2026-09-15'
    assert body['declared_request_start']=='2016-01-01' and len(body['points'])==2
    assert body['coverage']=='observed_rows_only'
    assert body['points'][0]['reported_volume']=='0' and body['points'][0]['reported_turnover'] is None
    assert converted([point('2026-09-01')],requested_start='2026-09-10')['body']['sequence_start']=='2026-09-01'


def test_exact_local_decimal_values_are_preserved_without_currency_conversion():
    result=converted([point(open_price=Decimal('10.000000000000000000001'),volume=Decimal('1220210'),turnover=Decimal('43462980.6'))])
    value=result['body']['points'][0]
    assert value['reported_open']=='10.000000000000000000001'
    assert value['reported_volume']=='1220210' and value['reported_turnover']=='43462980.6'


@pytest.mark.parametrize('changes',[
 {'open_price':True},{'open_price':10.0},{'open_price':None},{'open_price':'0'},
 {'open_price':'NaN'},{'low_price':'11.5'},{'high_price':'10.5'},
 {'volume':-1},{'turnover':float('inf')},{'date_ms':True},{'date_ms':1788192000001},
 {'thscode':'OTHER.SH'},{'interval':'1m'},{'adjusted':'forward'}])
def test_invalid_point_quarantines_the_entire_series(changes):
    with pytest.raises((FoundationError,ValidationError)):
        converted([point('2026-08-31'),point(**changes)])


@pytest.mark.parametrize('rows',[[],[None],[point(),point()]])
def test_no_silent_empty_or_duplicate_date_repair(rows):
    with pytest.raises(FoundationError):converted(rows)


@pytest.mark.parametrize('metadata',[
 {'coverage':'complete_market'},{'thscode':'OTHER.SH'},
 {'provider_envelopes':[{'thscode':'OTHER.SH'}]},
 {'provider_envelopes':[{'interval':'1m'}]},
 {'provider_envelopes':[{'adjust':'forward'}]},
 {'requested_start':'2026-09-20','requested_end':'2026-09-01'}])
def test_conflicting_container_does_not_change_the_observation_meaning(metadata):
    with pytest.raises((FoundationError,ValidationError)):converted([point()],**metadata)


def test_forward_adjusted_source_cannot_enter_unadjusted_contract():
    with pytest.raises(FoundationError):converted([point()],'etf_daily','none')
    with pytest.raises(FoundationError):rows_for(source('stock_daily','A.SH'),{'adjust':'none'})
