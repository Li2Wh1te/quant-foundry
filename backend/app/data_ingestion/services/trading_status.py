"""Daily trading-status (suspension) ingestion primitives."""
from app.data_ingestion.schemas.trading_status import normalize_suspend_row

def fetch_suspend_d(client, **kwargs):
    return client.suspend_d(**kwargs)

def normalize_suspend(rows):
    raw = rows.to_dict("records") if hasattr(rows, "to_dict") else rows
    return [normalize_suspend_row(dict(row)) for row in raw]

def sync_suspend_d(client, *, session=None, checkpoint_repo=None, sync_key=None, scope_key="trading_status", checkpoint_cursor=None, checkpoint_version=None, **kwargs):
    """Fetch, normalize, and optionally persist suspension facts."""
    rows = fetch_suspend_d(client, **kwargs)
    items = normalize_suspend(rows)
    changed = len(items)
    if session is not None:
        from app.data_ingestion.models.trading_calendar import TradingStatusFact
        for item in items:
            existing = session.get(TradingStatusFact, (item.ts_code, item.trade_date))
            if existing is None:
                session.add(TradingStatusFact(ts_code=item.ts_code, trade_date=item.trade_date, status=item.status, source=item.source, raw=item.raw or {}))
            elif existing.status != item.status or existing.raw != (item.raw or {}):
                existing.status, existing.raw = item.status, item.raw or {}
            else:
                changed -= 1
    advanced = False
    if checkpoint_repo is not None and sync_key:
        checkpoint = checkpoint_repo.advance(sync_key=sync_key, scope_key=scope_key, cursor=checkpoint_cursor or {}, expected_version=checkpoint_version)
        advanced = True
    else:
        checkpoint = None
    return {"items": items, "fetched": len(items), "changed": changed,
            "unchanged": len(items)-changed, "failed": 0, "checkpoint_advanced": advanced,
            "checkpoint_scope": "trading_status", "checkpoint_after": checkpoint.cursor if advanced else None,
            "coverage_status": "complete" if items else "unknown",
            "evidence": {"source": "tushare", "endpoint": "suspend_d", "row_count": len(items)}}
