"""Missing periods and shared holder codes must not collapse source occurrences."""
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.canonical import FoundationError


def converted(native, rows, **metadata):
    ref = source(native, '160105.SZ')
    return convert(ref, rows_for(ref, dict(item=rows) | metadata)[0])['body']


def test_unknown_allocation_dates_keep_order_without_normalizing_ratios():
    body = converted('fund_allocation', [{'stock_ratio_pct': '120', 'bond_ratio_pct': '-2', 'report_date_ms': None},
                                         {'stock_ratio_pct': 0}], timestamp=0)
    assert len(body['members']) == 2 and body['reported_timestamp_ms'] == 0
    assert body['members'][0]['reported_stock_ratio_pct'] == '120'
    assert body['members'][0]['reported_bond_ratio_pct'] == '-2'
    assert body['members'][0]['reported_report_date'] is None
    assert body['members'][1]['reported_stock_ratio_pct'] == '0'
    assert body['normalized_allocation'] is None


def test_holder_category_code_is_not_a_unique_person():
    body = converted('fund_top_holders', [{'holder_code': '015001', 'holder_name': '甲', 'rank': 1},
                                          {'holder_code': '015001', 'holder_name': '乙', 'rank': 2}], limit=10)
    assert [m['reported_holder_name'] for m in body['members']] == ['甲', '乙']
    assert body['resolved_holder_identity'] is None and body['reported_limit'] == 10


def test_industry_period_labels_and_duplicate_occurrences_are_preserved():
    row = {'industry_name': '医药', 'report_period': '2026Q2', 'ratio_pct': '9.1234567'}
    body = converted('fund_industry', [row, row])
    assert body['members'][0]['reported_report_period'] == '2026Q2'
    assert body['members'][1]['reported_ratio_pct'] == '9.1234567'
    assert body['industry_taxonomy'] is None


@pytest.mark.parametrize('native', ['fund_allocation', 'fund_industry', 'fund_holders', 'fund_top_holders'])
def test_empty_sets_remain_explicit(native):
    assert converted(native, [])['members'] == []


@pytest.mark.parametrize('native,row,metadata', [
    ('fund_holders', {'unknown_ratio': '1'}, {}),
    ('fund_holders', {'holder_amount': True}, {}),
    ('fund_holders', {'holder_amount': -1}, {}),
    ('fund_top_holders', {'rank': '2'}, {}),
    ('fund_top_holders', {'holder_name': 42}, {}),
    ('fund_industry', {'ratio_pct': 'NaN'}, {}),
    ('fund_allocation', {'report_date_ms': 1}, {}),
    ('fund_allocation', {}, {'timestamp': True}),
    ('fund_allocation', {}, {'thscode': 'OTHER'}),
])
def test_ambiguous_fields_quarantine_instead_of_coercing(native, row, metadata):
    with pytest.raises(FoundationError):
        converted(native, [row], **metadata)
