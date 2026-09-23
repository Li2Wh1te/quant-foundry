"""Reported statement windows never infer single-quarter or as-filed values."""
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.financial_windows import DOMAINS, NUMBERS
from app.data_foundation.canonical import FoundationError


def converted(native, rows, **metadata):
    ref = source(native, '000001.OF' if native.startswith('fund_') else '000001.SZ')
    return convert(ref, rows_for(ref, dict(item=rows) | metadata)[0])['body']


@pytest.mark.parametrize('native', list(DOMAINS))
def test_signed_values_occurrences_and_empty_window_remain_explicit(native):
    key = NUMBERS[native][0]
    body = converted(native, [{key: '-1.123456789'}, {key: 0}, {key: None}, {}])
    assert [row['reported_'+key] for row in body['reports']] == ['-1.123456789', '0', None, None]
    assert key in body['reports'][2]['reported_fields']
    assert key not in body['reports'][3]['reported_fields']
    assert body['single_period_values'] is None and body['as_filed_history'] is None
    assert converted(native, [])['reports'] == []


def test_stock_report_date_is_not_historical_public_availability():
    body = converted('stock_income', [dict(thscode='000001.SZ', period='quarterly', fiscal_year=2026,
        fiscal_period='Q2', period_end_ms=1782748800000, report_date_ms=1789401600000,
        net_profit='12.345', currency='CNY')], period='quarterly', historical_revision_evidence=False)
    report = body['reports'][0]
    assert report['reported_period_end'] == '2026-06-30'
    assert report['reported_report_date'] == '2026-09-15'
    assert report['reported_net_profit'] == '12.345'
    assert body['reported_historical_revision_evidence'] is False and body['as_filed_history'] is None
    assert body['normalized_currency'] is None


@pytest.mark.parametrize('row,metadata', [
    ({'thscode': 'OTHER'}, {}), ({'net_profit': 'Infinity'}, {}),
    ({'period_end_ms': True}, {}), ({'fiscal_year': 2026.5}, {}),
    ({'unexpected_metric': 1}, {}), ({'period': 'annual'}, {'period': 'quarterly'}),
    ({}, {'historical_revision_evidence': 'false'}), ({}, {'timestamp': True}),
])
def test_unadmitted_financial_fields_reject_whole_window(row, metadata):
    with pytest.raises(FoundationError):
        converted('stock_income', [row], **metadata)
