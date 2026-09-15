"""User-approved defaults; existing plans are changed explicitly through the API."""

def priority(dataset, mode='incremental'):
    if dataset in ('tickers', 'calendar', 'index_catalog'):
        return 100
    if mode == 'reconcile':
        return 0
    if mode == 'backfill' or dataset == 'stock_daily_dump':
        return 10
    if dataset in ('etf_daily', 'stock_daily', 'index_daily', 'fund_nav', 'stock_actions',
                   'stock_recent_dump', 'stock_actions_dump', 'stock_quote', 'etf_quote', 'index_quote'):
        return 80
    return 50


def tune_templates(templates, dataset, kind):
    for item in templates:
        params = item.setdefault('parameters', {})
        params.update(max_requests=40, max_seconds=180)
        params['batch_size'] = min(params.get('batch_size', 100), 5 if kind == 'reports' else 20)
        item['priority'] = priority(dataset, params.get('mode', 'incremental'))
    return templates
