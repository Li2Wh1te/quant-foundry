"""QDII reports preserve category scope and all observed missingness states."""
import pytest
from pydantic import ValidationError
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert,rows_for
from app.data_foundation.canonical import FoundationError


def converted(rows, dataset='fund_quota_list', **metadata):
    ref=source(dataset,'requested')
    return convert(ref,rows_for(ref,dict(item=rows,**metadata))[0])


def fund(code='A.OF', **extra):
    return dict(thscode=code,fund_name='Reported name',quota='10.00',year='0.00',**extra)


def group(rows):
    return [dict(name='requested',sub_tab=[dict(name='all',fund_list=rows)])]


def test_source_display_values_do_not_authorize_currency_or_return_math():
    result=converted(group([fund()]))
    value=result['body']['reported_groups'][0]['subcategories'][0]['funds'][0]
    assert value['reported_quota_text']=='10.00' and value['reported_year_text']=='0.00'
    for name in ['quota_currency','annual_return','comparable_quota_amounts','current_subscription_eligibility']:
        assert result['body'][name] is None and result['field_quality'][name]
    assert result['business_date'] is None


def test_absent_null_and_empty_classification_remain_distinct():
    result=converted(group([fund('A.OF'),fund('B.OF',classify=None),fund('C.OF',classify=[]),fund('D.OF',classify=['C','A'])]))
    values=result['body']['reported_groups'][0]['subcategories'][0]['funds']
    assert [v['classify_presence'] for v in values]==['absent','null','value','value']
    assert [v['reported_classify'] for v in values]==[None,None,[],['C','A']]


def test_members_can_belong_to_multiple_subcategories_without_global_deduplication():
    result=converted([dict(name='requested',sub_tab=[dict(name='tech',fund_list=[fund()]),dict(name='all',fund_list=[fund()])])])
    groups=result['body']['reported_groups'][0]['subcategories']
    assert [g['category_key'] for g in groups]==['all','tech']
    assert sum(len(g['funds']) for g in groups)==2


@pytest.mark.parametrize('dataset',['fund_quota_summary','fund_quota_list'])
def test_explicit_no_report_is_not_zero_quota_or_global_coverage(dataset):
    result=converted([],dataset,collection_scope={'tab':'["requested"]'},category_scope='explicit_categories_only')
    assert result['body']['reported_groups']==[]
    assert result['body']['scope_basis']=='explicit_requested_category_only'
    with pytest.raises(FoundationError):rows_for(source(dataset,'requested'),{})
    with pytest.raises(FoundationError):converted([],dataset,collection_scope={'tab':'["other"]'})
    with pytest.raises(FoundationError):converted([],dataset,collection_scope={'tab':'invalid'})


def test_summary_lexical_values_are_not_coerced_into_guessed_amounts():
    row=dict(name='requested',buy='23',total='25',total_limit='870.00',unlimited='0')
    result=converted([row],'fund_quota_summary')
    metrics=result['body']['reported_groups'][0]
    assert metrics['reported_total_limit_text']=='870.00' and metrics['reported_unlimited_text']=='0'
    with pytest.raises(FoundationError):converted([row|{'total_limit':870}],'fund_quota_summary')
    with pytest.raises(FoundationError):converted([row,row],'fund_quota_summary')


@pytest.mark.parametrize('bad',[None,{},fund()|{'quota':10},fund()|{'year':None},fund()|{'thscode':'A'},
    fund()|{'classify':{}},fund()|{'classify':[1]},fund()|{'fund_name':None}])
def test_bad_child_quarantines_complete_category(bad):
    with pytest.raises((FoundationError,ValidationError)):
        converted(group([fund('GOOD.OF'),bad]))


@pytest.mark.parametrize('rows',[group([fund(),fund()]),[dict(name='other',sub_tab=[])],
    [dict(name='requested',sub_tab=[dict(name='all',fund_list=[]),dict(name='all',fund_list=[])])],
    [dict(name='requested',sub_tab=None)]])
def test_conflicting_identity_or_container_is_never_silently_collapsed(rows):
    with pytest.raises((FoundationError,ValidationError)):
        converted(rows)
