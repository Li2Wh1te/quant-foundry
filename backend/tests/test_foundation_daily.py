"""Provider-unit and request contracts: synthetic inputs, no remote providers."""
from decimal import Decimal, localcontext
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.data_foundation.tushare import optional_amount, map_row, SERIES
from app.data_foundation.governance import choose
from app.data_foundation.query import DataRequirement
from app.data_foundation.canonical import FoundationError


def test_exact_amount_and_unverified_volume():
    with localcontext() as ctx:
        ctx.prec=6
        assert optional_amount('5925661.225') == (Decimal('5925661225'),None)
    row,quality=map_row(dict(source='tushare',ts_code='fixture',trade_date='2026-06-05',open='4.083',high='4.122',low='3.947',close='3.977',amount='5925661.225',vol='14663504.74'),uuid4())
    assert row['volume'] is None and quality=={'volume':'UNIT_UNVERIFIED'}
    assert row['close']==Decimal('3.977')


@pytest.mark.parametrize('value,reason',[(None,'OPTIONAL_FIELD_MISSING'),('-1','OPTIONAL_VALUE_INVALID'),('NaN','OPTIONAL_VALUE_INVALID'),('1e80','PRECISION_OVERFLOW'),('1.000000000001','PRECISION_OVERFLOW'),(1.2,'OPTIONAL_VALUE_INVALID')])
def test_optional_errors_do_not_poison_prices(value,reason):
    row,quality=map_row(dict(source='tushare',ts_code='fixture',trade_date='2026-06-05',open='1',high='1',low='1',close='1',amount=value),uuid4())
    assert row['turnover'] is None and quality['turnover']==reason


def test_core_errors_still_quarantine():
    with pytest.raises(FoundationError):
        map_row(dict(source='tushare',ts_code='fixture',trade_date='2026-06-05',open='1',high='1',low='1',close='2'),uuid4())


def test_whole_row_policy_never_invents_fallback_or_cross_series():
    policy={'series':SERIES,'source_order':['tushare'],'fallback':{'enabled':False},'comparison':{'enabled':False}}
    candidate={'id':'a','source':'tushare','series':SERIES,'ready':True}
    assert choose([candidate],policy)==('select','a','SINGLE_SOURCE')
    assert choose([candidate,{**candidate,'id':'b'}],policy)[0]=='block'
    assert choose([{**candidate,'series':'forward'}],policy)[0]=='gap'
    assert choose([{**candidate,'source':'unapproved'}],policy)[0]=='gap'
    with pytest.raises(FoundationError):choose([candidate],{**policy,'fallback':{'enabled':True}})


def test_request_rejects_silent_mutation_and_unbounded_results():
    request=dict(dataset_id='market.bar.daily',contract_version='1.0',profile_id='default',semantic_series_id=SERIES,
        subjects=[str(uuid4())],business_range={'from':'2026-06-05','to':'2026-08-28'})
    for extra in [{'fields':['close','close']},{'source_priority':['x']},{'business_range':{'from':'2000-01-01','to':'2026-01-01'}}]:
        with pytest.raises(ValidationError):DataRequirement(**(request|extra))
