"""Approved local-research collections; excluded services are not registered."""


# Times are collection eligibility boundaries, not a claim that the vendor has
# finalized a trading day. Each saved version retains the actual source time.
def build_specs(Dataset, FUND_TYPES):
    return (
        Dataset('stock_quote', '同花顺A股行情快照', 'Tonghuashun A-share quote snapshots', 'a-share.prices.snapshot', ('a-share',), 'm3_quote', 'close'),
        Dataset('stock_auction', '同花顺A股竞价终态', 'Tonghuashun final auction snapshots', 'a-share.auction.snapshot', ('a-share',), 'm3_quote', 'auction'),
        Dataset('auction_benchmark', '同花顺竞价基准', 'Tonghuashun auction benchmarks', 'a-share.auction.short-term-benchmark', (), 'm3_date', 'auction'),
        Dataset('anomaly_list', '同花顺异动原因列表', 'Tonghuashun anomaly reasons', 'a-share.special-data.anomaly-analysis-list', (), 'm3_global', 'close_evening', allow_empty=True),
        Dataset('anomaly_stock', '同花顺指定股票异动原因', 'Tonghuashun selected-stock anomaly reasons', 'a-share.special-data.anomaly-analysis-stock', ('a-share',), 'm3_selected', 'close_evening', allow_empty=True),
        Dataset('skyrocket', '同花顺飙升榜', 'Tonghuashun rising popularity lists', 'a-share.special-data.skyrocket-list', (), 'm3_period', 'market_hours', allow_empty=True),
        Dataset('hot_list', '同花顺热股榜', 'Tonghuashun popular stock lists', 'a-share.special-data.hot-stock-list', (), 'm3_period', 'market_hours', allow_empty=True),
        Dataset('hot_history', '同花顺历史热股排行', 'Tonghuashun historical popularity lists', 'a-share.special-data.hot-stock-list-history', (), 'm3_date', 'daily', years=1, allow_empty=True),
        Dataset('rank_trend', '同花顺指定股票排名走势', 'Tonghuashun selected-stock ranking history', 'a-share.special-data.hot-stock-rank-trend', ('a-share',), 'm3_series', 'daily', years=1, allow_empty=True),
        Dataset('limit_up', '同花顺涨停股票池', 'Tonghuashun limit-up pools', 'a-share.special-data.limit-up-pool', (), 'm3_pool', 'daily', allow_empty=True),
        Dataset('limit_down', '同花顺跌停股票池', 'Tonghuashun limit-down pools', 'a-share.special-data.limit-down-pool', (), 'm3_pool', 'daily', allow_empty=True),
        Dataset('limit_break', '同花顺炸板股票池', 'Tonghuashun broken-limit pools', 'a-share.special-data.limit-break-pool', (), 'm3_pool', 'daily', allow_empty=True),
        Dataset('limit_ladder', '同花顺连板天梯', 'Tonghuashun consecutive-limit ladders', 'a-share.special-data.limit-up-ladder', (), 'm3_global', 'daily', allow_empty=True),
        Dataset('dragon_tiger', '同花顺龙虎榜', 'Tonghuashun dragon-tiger lists', 'a-share.special-data.dragon-tiger-list', (), 'm3_date', 'daily', years=1, allow_empty=True),
        Dataset('etf_quote', '同花顺ETF行情快照', 'Tonghuashun ETF quote snapshots', 'fund.market.snapshot', ('fund-etf',), 'm3_quote', 'close'),
        Dataset('fund_returns', '同花顺基金区间收益', 'Tonghuashun fund period returns', 'fund.performance.returns', FUND_TYPES, 'm3_snapshot', 'late'),
        Dataset('fund_drawdowns', '同花顺基金最大回撤', 'Tonghuashun fund drawdowns', 'fund.performance.drawdowns', FUND_TYPES, 'm3_snapshot', 'late'),
        Dataset('fund_performance_history', '同花顺基金历史业绩指标', 'Tonghuashun fund historical performance', 'fund.performance.indicators-historical', FUND_TYPES, 'm3_series', 'late', years=5),
        Dataset('fund_manager_style', '同花顺基金经理投资风格', 'Tonghuashun manager investment style', 'fund.managers.investment-style', (), 'm3_manager', 'weekly', identity='manager_id'),
        Dataset('fund_manager_performance', '同花顺基金经理业绩', 'Tonghuashun manager performance', 'fund.managers.performance', (), 'm3_manager', 'weekly', identity='manager_id'),
        Dataset('fund_news', '同花顺基金资讯', 'Tonghuashun fund news', 'fund.news.article-list', FUND_TYPES, 'm3_news', 'daily', allow_empty=True),
        Dataset('fund_offerings', '同花顺基金募集列表', 'Tonghuashun fund offerings', 'fund.offerings.list', (), 'm3_offerings', 'morning_evening', allow_empty=True),
        Dataset('fund_quota_summary', '同花顺QDII额度汇总', 'Tonghuashun QDII quota summaries', 'fund.quota.summary', (), 'm3_quota', 'morning_evening', allow_empty=True),
        Dataset('fund_quota_list', '同花顺QDII额度列表', 'Tonghuashun QDII quota lists', 'fund.quota.list', (), 'm3_quota', 'morning_evening', allow_empty=True),
        Dataset('stock_daily_dump', '同花顺A股十年日线批量导入', 'Tonghuashun ten-year daily Parquet import', 'dump.market-dumps.daily-k.download-url', (), 'dump', 'manual'),
        Dataset('stock_recent_dump', '同花顺A股近期日线批量导入', 'Tonghuashun recent daily Parquet import', 'dump.market-dumps.daily-k-10d.download-url', (), 'dump', 'daily'),
        Dataset('stock_actions_dump', '同花顺A股复权事件批量导入', 'Tonghuashun corporate action Parquet import', 'dump.market-dumps.adjustment-factors.download-url', (), 'dump', 'daily'),
    )
SELECTED = {'anomaly_stock', 'rank_trend'}
DATES = {'auction_benchmark', 'hot_history', 'limit_up', 'limit_down', 'limit_break', 'dragon_tiger'}
TRADE_ONLY = {'stock_quote', 'etf_quote', 'stock_auction', 'anomaly_list', 'anomaly_stock', 'limit_ladder'}
SLOTS = {'close': (930,), 'auction': (566,), 'daily': (1200,), 'late': (1380,),
         'close_evening': (930, 1200), 'morning_evening': (540, 1200)}


def templates(spec):
    """Selected-symbol requests and full imports require deliberate scope."""
    if spec.key in SELECTED:
        return []
    params = {'batch_size': 100}
    if spec.kind == 'm3_quota':
        # These two codes appear in the official examples. They are a declared
        # starting scope, never presented as an exhaustive category dictionary.
        params['quota_tabs'] = ['nazhi100', 'remen']
    if spec.kind == 'dump':
        params['mode'] = 'backfill' if spec.frequency == 'manual' else 'incremental'
    priority = 20 if spec.frequency in ('close', 'auction', 'close_evening', 'market_hours') else 10
    if spec.kind == 'dump':
        priority = -10
    result = [{'name': spec.name, 'task_type': 'data.ths.' + spec.key, 'parameters': params,
        'description': '分批续作，按约定日切/时段检查；保留实际数据时间及覆盖范围。',
        'schedule': {'type': 'cron', 'expression': '6-59/10 * * * *' if spec.frequency == 'auction' else '*/10 * * * *', 'timezone': 'Asia/Shanghai'},
        'priority': priority, 'concurrency_limit': 1, 'overlap_policy': 'skip'}]
    if spec.kind in ('m3_date', 'm3_pool', 'm3_series'):
        result.append({**result[0], 'name': spec.name + '历史回补',
            'parameters': {**params, 'mode': 'backfill'}, 'priority': -10})
    if spec.kind == 'm3_series':
        result.append({**result[0], 'name': spec.name + '历史核对',
            'parameters': {**params, 'mode': 'reconcile'}, 'priority': -10})
    return result
