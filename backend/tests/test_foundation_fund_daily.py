"""Exact supplier-unit daily bars without inferred identity or public time."""
from decimal import Decimal,localcontext
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from app.data_foundation.record_adapters import convert,daily_decimal
from app.data_foundation.canonical import FoundationError


def source():
    return SimpleNamespace(source='tushare',dataset='etf_daily',subject='scope',representation='local_table_baseline')


def bar(**changes):
    return dict(source='tushare',ts_code='TEST.SH',trade_date='2026-01-02',open='1.234567',high='2',low='1',close='1.5',
        vol='123.4567',amount='0.0123',pre_close='1.6',change='-0.1',pct_chg='-6.25') | changes


def test_daily_native_units_and_negative_changes_are_exact():
    with localcontext() as context:
        context.prec=3
        result=convert(source(),bar())
    assert result['body']['open']=='1.234567'
    assert result['body']['volume_lots']=='123.4567'
    assert result['body']['turnover_thousand_yuan']=='0.0123'
    assert result['body']['change_percent']=='-6.25'
    assert result['body']['change_yuan']=='-0.1'
    assert result['body']['price_basis']=='provider_reported'
    assert result['subject_kind']=='asset:fund-etf'


@pytest.mark.parametrize('value',[None,True,1.2,'NaN','Infinity','0','-1','1.1234567','100000000000000','1E999999999','1E-999999999'])
def test_daily_core_price_invalidity_never_becomes_a_value(value):
    with pytest.raises(FoundationError):
        convert(source(),bar(open=value))


@pytest.mark.parametrize('changes',[{'high':'1.1'},{'low':'1.6'},{'low':'3','high':'2'}])
def test_incoherent_ohlc_is_rejected(changes):
    with pytest.raises(ValidationError):
        convert(source(),bar(**changes))


def test_optional_units_distinguish_zero_missing_and_invalid():
    result=convert(source(),bar(vol='0',amount=None,pre_close='0',change=None,pct_chg='NaN'))
    assert result['body']['volume_lots']=='0' and 'volume_lots' not in result['field_quality']
    assert result['body']['turnover_thousand_yuan'] is None
    assert result['field_quality']=={'turnover_thousand_yuan':'MISSING','previous_close':'INVALID_DECIMAL',
        'change_yuan':'MISSING','change_percent':'INVALID_DECIMAL'}
    assert daily_decimal(Decimal('-0E999999999'),precision=24,scale=4)=='0'


def test_source_namespace_is_not_taken_from_an_unverified_row():
    with pytest.raises(FoundationError,match='命名空间'):
        convert(source(),bar(source='other'))
