"""Bounded news, reported anomaly narratives and source ranking windows.

News timestamps remain attributed provider timestamps. Links are stored as
reported strings and never fetched by normalization. Neither prose nor rankings
are converted into verified market events, investment signals or public PIT.
"""
from datetime import datetime, timedelta, timezone
from app.data_foundation.market_activity import invalid, text, texts
from app.data_foundation.distributions import reported_day
from app.data_foundation.fund_ownership import counter

DOMAINS = {'fund_news': 'fund.news_window', 'anomaly_stock': 'market.stock_anomaly_window',
           'rank_trend': 'market.rank_trend_window'}
NEWS_TEXTS = ('content_type', 'title', 'summary', 'source', 'url', 'image_url', 'author')


def boolean(value):
    if value is not None and type(value) is not bool:
        invalid('资讯来源布尔标志无效。')
    return value


def narrative_body(source, raw):
    rows = raw.get('item')
    if not isinstance(rows, list):
        invalid('资讯或排名观察缺少明确完整列表。')
    scope = raw.get('collection_scope')
    if scope is not None and (not isinstance(scope, dict) or scope.get('thscode') != source.subject):
        invalid('资讯或排名观察采集主体与固定来源不一致。')
    if raw.get('failed_requests') or raw.get('resume_cursor'):
        invalid('资讯或排名观察仍有未完成请求，未将部分请求视为完整窗口。')
    body = dict(source_code=text(source.subject, required=True),
                coverage_basis='source_observation_window', complete_history=None,
                verified_market_events=None)
    quality = dict(complete_history='SOURCE_WINDOW_ONLY', verified_market_events='PROVIDER_REPORTS_NOT_VERIFIED_EVENTS')
    if source.dataset == 'fund_news':
        members = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - {'id', *NEWS_TEXTS, 'publish_time_ms', 'top'}:
                invalid('基金资讯含未声明字段或无效成员。')
            item = dict(provider_article_id=text(row.get('id'), required=True))
            for name in NEWS_TEXTS:
                item['reported_'+name] = text(row.get(name))
            stamp = counter(row.get('publish_time_ms'))
            if stamp is None:
                invalid('基金资讯缺少来源发布时间。')
            try:
                item['reported_publish_time'] = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=stamp)
            except (OverflowError, ValueError):
                invalid('基金资讯来源发布时间超出支持范围。')
            item['reported_top'] = boolean(row.get('top'))
            item['reported_fields'] = [name for name in (*NEWS_TEXTS, 'publish_time_ms', 'top') if name in row]
            members.append(item)
        ids = [item['provider_article_id'] for item in members]
        if len(ids) != len(set(ids)):
            invalid('基金资讯窗口存在重复文章标识。')
        start, end = reported_day(raw.get('requested_start')), reported_day(raw.get('requested_end'))
        if start is None or end is None or start > end:
            invalid('基金资讯缺少有效的明确采集日期范围。')
        body.update(articles=members, requested_start=start, requested_end=end,
                    reported_coverage=text(raw.get('coverage')), reported_scope_truncated=boolean(raw.get('scope_truncated')))
    elif source.dataset == 'rank_trend':
        points = []
        for row in rows:
            if not isinstance(row, dict) or row.get('thscode') != source.subject:
                invalid('排名走势成员主体与固定来源不一致。')
            day = reported_day(row.get('date_ms'))
            if day is None or reported_day(row.get('date')) != day:
                invalid('排名走势来源日期不一致。')
            points.append(dict(trading_date=day, reported_ticker=text(row.get('ticker')), reported_rank=counter(row.get('rank'))))
        if len({p['trading_date'] for p in points}) != len(points):
            invalid('排名走势包含重复日期。')
        points.sort(key=lambda p:p['trading_date'])
        start, end = reported_day(raw.get('requested_start')), reported_day(raw.get('requested_end'))
        if start is None or end is None or start > end or any(not start <= p['trading_date'] <= end for p in points):
            invalid('排名走势点位超出明确采集范围。')
        body.update(points=points, requested_start=start, requested_end=end, selection_formula=None)
        quality['selection_formula'] = 'PROVIDER_RANK_FORMULA_UNVERIFIED'
    else:
        members = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get('thscode') != source.subject:
                invalid('异动叙述主体与固定来源不一致。')
            members.append(dict(source_order=index, reported_name=text(row.get('stock_name')),
                                reported_tag=text(row.get('tag_name')), reported_keywords=texts(row.get('keyword_list')),
                                reported_analysis=text(row.get('analysis_content'))))
        body['narratives'] = members
    return body, quality
