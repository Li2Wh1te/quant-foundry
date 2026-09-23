"""Reported snapshots never fabricate event time, executable quotes or formulas."""
from decimal import Decimal
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert,rows_for
from app.data_foundation.canonical import FoundationError


def quote(**changes):
    return dict(thscode='A.SH',ticker='DISPLAY',last_price='10.5',open_price='10',
        high_price='11',low_price='9',prev_price='10.7',price_change='-0.2',
        price_change_ratio_pct='-1.869158',volume=0,turnover=None)|changes


def converted(row=None,native='stock_quote',**metadata):
    ref=source(native,'A.SH');raw=dict(item=[quote() if row is None else row],timestamp=1790005915123,total=100)|metadata
    return convert(ref,rows_for(ref,raw)[0])


@pytest.mark.parametrize('native,asset',[('stock_quote','a-share'),('etf_quote','fund-etf'),('index_quote','a-share-index')])
def test_quote_identity_decimal_and_unverified_time_semantics(native,asset):
    result=converted(native=native)
    body=result['body']
    assert result['dataset']==f'market.{native}_snapshot' and result['subject_kind']=='asset:'+asset
    assert body['reported_ticker']=='DISPLAY' and body['source_code']=='A.SH'
    assert body['reported_price_change']=='-0.2' and body['reported_volume']=='0'
    assert body['reported_turnover'] is None and result['field_quality']['reported_turnover']=='SOURCE_NULL'
    assert result['field_quality']['reported_turnover_ratio_pct']=='SOURCE_FIELD_ABSENT'
    assert body['reported_at']=='2026-09-21T15:51:55.123000Z'
    assert result['business_date'] is None
    for field in ['effective_business_date','timestamp_semantics','currency','volume_unit','turnover_unit','executable_quote','trading_status']:
        assert body[field] is None and result['field_quality'][field]


def test_signed_valuation_zero_and_exact_precision_are_reported_not_recomputed():
    result=converted(dict(thscode='A.SH',pe_ttm='-1.25',pb_mrq='0',pcf_ttm=Decimal('1.00000000000000000001')),native='stock_valuation')
    body=result['body'];assert body['reported_pe_ttm']=='-1.25' and body['reported_pb_mrq']=='0'
    assert body['reported_pcf_ttm']=='1.00000000000000000001'
    assert body['financial_period'] is None and body['valuation_formula'] is None
    assert result['field_quality']['reported_ps_ttm']=='SOURCE_FIELD_ABSENT'


def test_zero_quote_is_not_inferred_to_be_suspension_or_executable_price():
    body=converted(quote(last_price=0,open_price=0,high_price=0,low_price=0))['body']
    assert body['reported_last_price']=='0' and body['trading_status'] is None and body['executable_quote'] is None


@pytest.mark.parametrize('changes',[
 {'thscode':'OTHER.SH'},{'last_price':True},{'last_price':1.5},{'last_price':'NaN'},
 {'last_price':'-1'},{'volume':'-1'},{'turnover':'Infinity'},{'name':12},{'ticker':''}])
def test_bad_identity_or_numeric_value_is_isolated(changes):
    with pytest.raises(FoundationError):converted(quote(**changes))


@pytest.mark.parametrize('metadata',[
 {'timestamp':True},{'timestamp':'2026-09-21'},{'timestamp':Decimal('1.5')},
 {'timestamp':10**30},{'collection_scope':{'thscodes':'OTHER.SH'}},
 {'collection_scope':[]},{'item':[]},{'item':[quote(),quote()]}, {'item':[None]}])
def test_invalid_container_never_becomes_an_empty_or_mixed_observation(metadata):
    with pytest.raises(FoundationError):converted(**metadata)


def test_all_missing_valuation_is_not_an_official_shell_record():
    with pytest.raises(FoundationError):converted(dict(thscode='A.SH'),native='stock_valuation')


def test_explicit_all_null_quote_is_a_missing_value_observation_not_zero():
    row={field:None for field in ['open_price','high_price','low_price','last_price','prev_price',
        'price_change','price_change_ratio_pct','volume','turnover']}
    result=converted(row|dict(thscode='A.SH'))
    assert result['body']['reported_value_status']=='explicit_null_report'
    assert result['body']['reported_last_price'] is None
    assert result['field_quality']['reported_last_price']=='SOURCE_NULL'
    with pytest.raises(FoundationError):converted(dict(thscode='A.SH',last_price=None))


def test_missing_reported_timestamp_is_never_filled_with_collection_time():
    result=converted(timestamp=None)
    assert result['body']['reported_at'] is None and result['field_quality']['reported_at']=='SOURCE_NULL'
    assert result['body']['reported_last_price']=='10.5' and result['business_date'] is None
