"""D11 exact formal factor observations and the shared arithmetic boundary.

E69 currently declares neither a cumulative formula/coverage policy nor an
original normalized anchor. Observations are readable without claiming those
facts. No implicit forward fill, first-row anchor, supplier or S3 admission.
"""
from __future__ import annotations

from datetime import date
import json

from app.data_store.errors import DataStoreError
from .bindings import ETF_DAILY, ETF_FACTORS
from .contracts import FactorPage, FactorRow, Notice, PriceBasis
from .errors import MarketError
from .service import IdentityQuery, parse_instrument_key
from .series import MIN_DATE, ONE_DAY, read_observations

FACTOR_POLICY = 'tushare-fund-adj-observations@1'
FACTOR_NOTE = '本地正式 fund_adj 因子原值；累计公式、逐日覆盖和原始归一化基准尚未核验，不能据此宣称前/后复权可用。当前修订数据，不恢复历史当时所知。'


def adjusted_prices(values: list[tuple[str, str, str]]) -> list[str]:
    """One arithmetic implementation; call only AFTER policy/coverage checks.

    This is an internal pure calculation boundary, never a successful provider
    or an HTTP policy override. S3 keeps its own stricter visibility checks.
    """
    if len(values) > 8000:
        raise DataStoreError('QUERY_BUDGET_EXCEEDED')
    try:
        from quantfoundry import _native
        transform = _native.adjust_prices_json
    except (ImportError, AttributeError):
        raise MarketError('CAPABILITY_UNAVAILABLE') from None
    try:
        return json.loads(transform(json.dumps(values, separators=(',', ':'))))
    except _native.ContractError as error:
        if getattr(error, 'code', None) == 'RESOURCE_LIMIT':
            raise DataStoreError('QUERY_BUDGET_EXCEEDED') from None
        if getattr(error, 'code', None) == 'NUMERIC_RANGE_UNSUPPORTED':
            raise MarketError('NUMERIC_RANGE_UNSUPPORTED') from None
        raise DataStoreError('INVALID_VALUE') from None


def factors(service, key, *, series_key=ETF_DAILY, start: date, end: date,
            as_of=None, limit=512, cursor=None) -> FactorPage:
    parse_instrument_key(key)
    if series_key != ETF_DAILY:
        raise MarketError('CAPABILITY_UNAVAILABLE')
    as_of = as_of or end
    if start < MIN_DATE or start > end or end > as_of or as_of.year > 9998 or not 1 <= limit <= 2000:
        raise DataStoreError('INVALID_VALUE')
    conditions = dict(operation='factors', instrument_key=key, series_key=series_key,
        start=start, end=end, as_of=as_of, limit=limit, policy=FACTOR_POLICY)
    with service.read_scope((ETF_FACTORS,), conditions=conditions,
            identity=IdentityQuery(instrument_keys=(key,), start=start, end=end), cursor=cursor) as scope:
        segments = scope.mapping_segments(key)
        before = end + ONE_DAY
        if scope.position:
            try:
                if set(scope.position) != {'before'}:
                    raise ValueError()
                before = date.fromisoformat(scope.position['before'])
                if not start < before <= end + ONE_DAY:
                    raise ValueError()
            except (ValueError, TypeError):
                raise DataStoreError('INVALID_CURSOR') from None
        rows = []
        for segment in reversed(segments):
            low, high = max(start, segment.valid_from), min(end, before - ONE_DAY,
                segment.valid_to - ONE_DAY if segment.valid_to else end)
            if low > high:
                continue
            for row in read_observations(scope, ETF_FACTORS, segment.source_code, low, high):
                if row['factor_basis'] != 'tushare_fund_adj':
                    raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
                rows.append(FactorRow(trade_date=row['trade_date'], factor=row['factor']))
                if len(rows) > limit:
                    break
            if len(rows) > limit:
                break
        more = len(rows) > limit
        rows = rows[:limit]
        meta = scope.meta(scope=f'{key} · 因子原值 · {start} 至 {min(end, before - ONE_DAY)}',
            business_as_of=max((row.trade_date for row in rows), default=None), limitations=[
                Notice(code='FACTOR_ANCHOR_UNVERIFIED', message=FACTOR_NOTE),
                Notice(code='FACTOR_COVERAGE_UNVERIFIED', message='仅返回实际因子日期，不补齐缺日或推断分段有效；该表独立于价格读取。')])
        result = FactorPage(items=list(reversed(rows)),
            next_cursor=scope.make_cursor({'before': rows[-1].trade_date.isoformat()}) if more else None,
            basis=PriceBasis(adjustment='none', policy=FACTOR_POLICY, as_of=as_of, anchor=None, note=FACTOR_NOTE), meta=meta)
        scope.response(result)
    return result
