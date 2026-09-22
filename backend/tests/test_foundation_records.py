"""Source-local schema semantics without private data or supplier requests."""
from types import SimpleNamespace
from decimal import Decimal
from datetime import date
import pytest
from pydantic import ValidationError
from app.data_foundation.record_adapters import convert, source_date, rows_for
from app.data_foundation.canonical import FoundationError
from app.data_foundation.record_query import RecordRequirement


def source(dataset, subject):
    return SimpleNamespace(source='tonghuashun', dataset=dataset, subject=subject, representation='ths_observation')


def test_calendar_dates_do_not_invent_exchange_open_flags():
    r = convert(source('calendar', 'calendar'), {'date':'20250331', 'date_ms':1743350400000})
    assert r['body']['calendar_date'] == '2025-03-31'
    assert r['body']['is_open'] is None
    assert r['field_quality']['is_open'] == 'EXCHANGE_SESSION_UNVERIFIED'
    with pytest.raises(FoundationError, match='编码不一致'):
        convert(source('calendar', 'calendar'), {'date':'20250401', 'date_ms':1743350400000})


@pytest.mark.parametrize('value', [True, 1743350400001, 1743350400000.0, Decimal('1743350400000.1'), '20250332'])
def test_dates_reject_lossy_or_non_date_inputs(value):
    with pytest.raises((ValueError, OverflowError)):
        source_date(value)


def test_exact_date_boundaries_and_calendar_encodings():
    assert source_date(Decimal('1743350400000')) == date(2025,3,31)
    assert source_date('20250331') == source_date('2025-03-31')


@pytest.mark.parametrize('dataset,subject,raw,field', [
    ('fund_company','C1',{'company_id':'C1','company_name':'Company','fund_count':0},'company_id'),
    ('fund_manager','M1',{'manager_id':'M1','manager_name':'Manager'},'manager_id'),
    ('fund_profile','F.OF',{'thscode':'F.OF','fund_name':'Fund'},'source_code'),
])
def test_reference_identities_are_namespace_scoped_and_optional_missing_is_explicit(dataset, subject, raw, field):
    r = convert(source(dataset, subject), raw)
    assert r['body'][field] == subject and r['field_quality']
    with pytest.raises(FoundationError, match='主体不一致'):
        convert(source(dataset, 'different'), raw)


def test_directory_keeps_asset_kind_and_rejects_container_conflict():
    r = convert(source('tickers','fund-etf'), {'thscode':'TEST.SH','asset_type':'fund-etf','name':'Name'})
    assert r['subject_kind'] == 'asset:fund-etf'
    with pytest.raises(FoundationError, match='分类'):
        convert(source('tickers','a-share'), {'thscode':'TEST.SH','asset_type':'fund-etf'})
    with pytest.raises(FoundationError):
        rows_for(source('tickers','fund-etf'), {})
    assert rows_for(source('tickers','fund-etf'), {'item':[]}) == []


def test_reference_query_rejects_fake_historical_business_range():
    with pytest.raises(ValidationError):
        RecordRequirement(dataset_id='fund.company',semantic_series_id='tonghuashun-fund.company-observed-v1',
            subjects=['00000000-0000-0000-0000-000000000001'],fields=['company_id'],
            business_range={'from':'2025-01-01','to':'2025-01-02'})
