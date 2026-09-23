"""Source-local portfolios retain occurrences without bypassing reviewed identities."""
import copy
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.canonical import FoundationError


def payload(asset='stock'):
    period = dict(start_date_ms=1767196800000, end_date_ms=1774886400000,
                  report_type='quarter', report_type_name='季度')
    member = dict(thscode='600001' if asset == 'stock' else 'Z00524', ticker='600001',
                  asset_type=asset, end_date_ms=1774886400000, report_type='季度',
                  hold_ratio='2.12345678', market_value='123456789.123456', rank=None)
    return dict(item=[dict(report_key='2026-03-31:quarter', report=period,
                          data=dict(item=[member, copy.deepcopy(member)], timestamp=0))],
                report_directory=dict(item=[period]), current_provider_directory=dict(item=[period]),
                historical_revision_evidence=False)


def converted(native, raw):
    ref = source(native, '160105.SZ')
    return convert(ref, rows_for(ref, raw)[0])['body']


@pytest.mark.parametrize('native,asset', [('fund_stock_history', 'stock'), ('fund_bond_history', 'bond')])
def test_historical_periods_and_duplicate_members_preserve_source_scope(native, asset):
    body = converted(native, payload(asset))
    report = body['reports'][0]
    assert report['period_start'] == '2026-01-01' and report['period_end'] == '2026-03-31'
    assert len(report['members']) == 2 and report['reported_timestamp_ms'] == 0
    assert report['members'][0]['reported_hold_ratio'] == '2.12345678'
    assert report['members'][0]['reported_market_value'] == '123456789.123456'
    assert report['members'][1]['source_order'] == 1
    assert body['resolved_instrument_identities'] is None and body['portfolio_complete'] is None
    assert body['transport_complete'] is None and body['as_filed_history'] is None


def test_current_holdings_keep_different_report_dates_and_exact_values():
    body = converted('fund_holdings', dict(item=[dict(thscode='600001.SH', asset_type='stock',
        position_capital='123.45', position_count='10.5', hold_ratio='2',
        publish_date_ms=1789401600000)], concentration_ratio='0.7', timestamp=0))
    assert body['members'][0]['source_member_code'] == '600001.SH'
    assert body['members'][0]['reported_position_count'] == '10.5'
    assert body['reported_concentration_ratio'] == '0.7'
    assert converted('fund_holdings', {'item': []})['members'] == []


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'period', 'asset', 'pages', 'failure', 'bad_number'])
def test_incomplete_historical_collections_are_not_published_as_empty(mutation):
    raw = payload(); report = raw['item'][0]; member = report['data']['item'][0]
    if mutation == 'missing':raw['item'] = []
    elif mutation == 'duplicate':raw['item'].append(copy.deepcopy(report))
    elif mutation == 'period':member['end_date_ms'] = 1782748800000
    elif mutation == 'asset':member['asset_type'] = 'bond'
    elif mutation == 'pages':report['data']['has_more'] = True
    elif mutation == 'failure':raw['failed_requests'] = [{'error': 'failed'}]
    elif mutation == 'bad_number':member['hold_ratio'] = 'NaN'
    with pytest.raises(FoundationError):converted('fund_stock_history', raw)


def test_explicit_empty_directory_can_publish_empty_history():
    body = converted('fund_stock_history', dict(item=[], report_directory=dict(item=[])))
    assert body['reports'] == [] and body['current_provider_directory'] is None


def test_fund_of_funds_without_security_code_preserves_named_occurrence():
    body = converted('fund_holdings', dict(item=[dict(thscode=None, ticker=None,
        stock_name='Reported foreign fund', asset_type='fund', hold_ratio='8.39')],
        total_fund_ratio_pct='8.39', total_bond_ratio_pct='12'))
    assert body['members'][0]['source_member_code'] is None
    assert body['members'][0]['reported_stock_name'] == 'Reported foreign fund'
    assert body['reported_total_fund_ratio_pct'] == '8.39'
    assert body['reported_total_bond_ratio_pct'] == '12'
    assert body['resolved_instrument_identities'] is None
