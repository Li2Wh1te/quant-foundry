"""Source narratives and ranks are not public-time or event-verification proofs."""
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.canonical import FoundationError


def converted(native, rows, **metadata):
    ref = source(native, '510300.SH')
    return convert(ref, rows_for(ref, dict(item=rows) | metadata)[0])['body']


def test_news_keeps_intraday_reported_time_and_explicit_scope():
    body = converted('fund_news', [dict(id='a1', title='来源标题', publish_time_ms=1789405200000,
        url='https://example.test/article', top=False)], requested_start='2026-09-01', requested_end='2026-09-20',
        scope_truncated=True, coverage='initial_90_days_or_500_records')
    assert body['articles'][0]['reported_publish_time'] == '2026-09-14T17:00:00Z'
    assert body['articles'][0]['reported_top'] is False
    assert body['reported_scope_truncated'] is True
    assert body['complete_history'] is None and body['verified_market_events'] is None
    assert converted('fund_news', [], requested_start='2026-09-01', requested_end='2026-09-20')['articles'] == []


def test_rank_dates_are_sorted_and_not_assumed_top_thirty():
    body = converted('rank_trend', [dict(thscode='510300.SH', date='2026-09-16', date_ms=1789488000000, rank=1740),
        dict(thscode='510300.SH', date='2026-09-15', date_ms=1789401600000, rank=0)],
        requested_start='2026-09-01', requested_end='2026-09-20')
    assert [p['reported_rank'] for p in body['points']] == [0,1740]
    assert body['selection_formula'] is None


def test_anomaly_reports_remain_distinct_unverified_narratives():
    row = dict(thscode='510300.SH', stock_name='来源名称', tag_name='来源标签', keyword_list=['关键词'], analysis_content='来源叙述')
    body = converted('anomaly_stock', [row,row])
    assert len(body['narratives']) == 2 and body['verified_market_events'] is None
    assert converted('anomaly_stock', [])['narratives'] == []


@pytest.mark.parametrize('native,rows,metadata', [
    ('fund_news', [], {'requested_start':'2026-09-01','requested_end':'2026-09-20','resume_cursor':'pending'}),
    ('fund_news', [{'id':'a1','publish_time_ms':True}], {'requested_start':'2026-09-01','requested_end':'2026-09-20'}),
    ('fund_news', [], {'requested_start':'2026-09-20','requested_end':'2026-09-01'}),
    ('fund_news', [], {'requested_start':'2026-09-01','requested_end':'2026-09-20','collection_scope':{'thscode':'OTHER'}}),
    ('rank_trend', [{'thscode':'OTHER'}], {'requested_start':'2026-09-01','requested_end':'2026-09-20'}),
    ('rank_trend', [{'thscode':'510300.SH','date':'2026-09-16','date_ms':1789401600000}], {'requested_start':'2026-09-01','requested_end':'2026-09-20'}),
    ('anomaly_stock', [{'thscode':'OTHER'}], {}),
])
def test_incomplete_or_wrong_scope_rejected(native, rows, metadata):
    with pytest.raises(FoundationError):converted(native, rows, **metadata)
