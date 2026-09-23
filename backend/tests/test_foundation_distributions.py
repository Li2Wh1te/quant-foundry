"""Distribution contracts retain exact occurrences and reject invented payments."""
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.canonical import FoundationError


def converted(dataset, rows, **metadata):
    ref = source(dataset, '160105.SZ' if dataset == 'fund_dividends' else '000001.SZ')
    raw = dict(item=rows) | metadata
    return convert(ref, rows_for(ref, raw)[0])


def test_fund_dividend_retains_occurrences_dates_and_unknown_tax_basis():
    row = dict(ex_dividend_date_ms=1789401600000, per_ten_cash_before_tax='0.550000001',
               per_ten_cash_after_tax=None, progress='2')
    result = converted('fund_dividends', [row, row], dividend_count=99, dividend_total='2.4556', timestamp=0)
    body = result['body']
    assert body['source_code'] == '160105.SZ'
    assert body['reported_dividend_count'] == 99
    assert body['reported_timestamp_ms'] == 0
    assert [e['source_order'] for e in body['events']] == [0, 1]
    event = body['events'][0]
    assert event['reported_ex_dividend_date'] == '2026-09-15'
    assert event['reported_per_ten_cash_before_tax'] == '0.550000001'
    assert event['reported_per_ten_cash_after_tax'] is None
    assert event['reported_progress_code'] == '2'
    assert 'per_ten_cash_after_tax' in event['reported_fields']
    assert 'payment_date_ms' not in event['reported_fields']
    assert body['payment_execution'] is None and body['normalized_cashflow'] is None
    assert body['tax_basis'] is None and body['currency'] is None


def test_bulk_actions_keep_negative_bonus_zero_and_reported_allotment():
    body = converted('stock_actions', [dict(ex_date_ms=1789401600000, ticker='000001',
        thscode='000001.SZ', dividend_per_share=0, per_share_bonus='-0.2',
        allotment_ratio='0.3', allotment_price='9.750000001', currency='CNY')],
        thscode='000001.SZ', ticker='000001', adjust='none')['body']
    event = body['events'][0]
    assert event['reported_dividend_per_share'] == '0'
    assert event['reported_per_share_bonus'] == '-0.2'
    assert event['reported_allotment_price'] == '9.750000001'
    assert event['reported_currency'] == 'CNY'
    assert body['event_identity'] is None and body['payment_execution'] is None


@pytest.mark.parametrize('native', ['fund_dividends', 'stock_actions'])
def test_explicit_empty_window_can_replace_old_events(native):
    assert converted(native, [])['body']['events'] == []


@pytest.mark.parametrize('native,rows,metadata', [
    ('fund_dividends', None, {}),
    ('fund_dividends', [{'unknown_amount': '1'}], {}),
    ('fund_dividends', [{'payment_date_ms': True}], {}),
    ('fund_dividends', [{'per_ten_cash_before_tax': 'NaN'}], {}),
    ('fund_dividends', [], {'dividend_count': True}),
    ('fund_dividends', [], {'timestamp': -1}),
    ('stock_actions', [{'thscode': 'OTHER'}], {}),
    ('stock_actions', [{'ticker': 'OTHER'}], {'ticker': '000001'}),
    ('stock_actions', [], {'thscode': 'OTHER'}),
    ('stock_actions', [{'ex_date_ms': 1}], {}),
])
def test_invalid_member_rejects_whole_window(native, rows, metadata):
    with pytest.raises(FoundationError):
        converted(native, rows, **metadata)
