"""D11 exact formal factor observations and the shared arithmetic boundary.

E69 currently declares neither a cumulative formula/coverage policy nor an
original normalized anchor. Observations are readable without claiming those
facts. No implicit forward fill, first-row anchor, supplier or S3 admission.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json

from app.data_store.errors import DataStoreError
from .bindings import ETF_DAILY, ETF_FACTORS
from .contracts import Anchor, FactorPage, FactorRow, Notice, PriceBasis
from .errors import MarketError
from .service import IdentityQuery, parse_instrument_key
from .series import MIN_DATE, ONE_DAY, read_observations
from .policies import price_policy

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


class AdjustmentContext:
    """One fixed current policy/anchor and exact-day factor cache per scope."""
    def __init__(self, scope, segments, policy, adjustment, as_of, identity_start):
        self.scope, self.segments, self.policy, self.as_of = scope, segments, policy, as_of
        self.values, self.ranges = {}, []
        if policy.factor_key is None:
            raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
        # Verify the installed shared boundary without compiling or installing.
        adjusted_prices([])
        if adjustment == 'pre':
            from .series import _calendar
            low = max(identity_start, MIN_DATE, as_of - timedelta(days=31))
            sessions = _calendar(scope, segments, low, as_of)
            position = as_of
            while position >= low:
                if position not in sessions:
                    raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
                if sessions[position]:
                    break
                position -= ONE_DAY
            if position < low:
                raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
            self._load(position, position)
            if position not in self.values:
                raise MarketError('FACTOR_UNAVAILABLE')
            anchor = Anchor(date=position, factor=self.values[position])
        else:
            anchor = policy.post_anchor(as_of)
        if Decimal(anchor.factor) <= 0:
            raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
        self.basis = PriceBasis(adjustment=adjustment, policy=policy.adjustment_policy,
            as_of=as_of, anchor=anchor, note=policy.adjustment_note)

    def _load(self, low, high):
        self.scope.check()
        if high > self.as_of:
            raise DataStoreError('INVALID_VALUE')
        unread, position = [], low
        for begin, end in sorted(self.ranges):
            if end < position or begin > high:
                continue
            if position < begin:
                unread.append((position, min(high, begin - ONE_DAY)))
            position = max(position, end + ONE_DAY)
        if position <= high:
            unread.append((position, high))
        for begin, end in unread:
            for segment in self.segments:
                start = max(begin, segment.valid_from)
                finish = min(end, segment.valid_to - ONE_DAY if segment.valid_to else end)
                if start > finish:
                    continue
                for row in read_observations(self.scope, self.policy.factor_key, segment.source_code, start, finish):
                    if row['factor_basis'] != 'tushare_fund_adj':
                        raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
                    prior = self.values.get(row['trade_date'])
                    if prior is not None and prior != row['factor']:
                        raise DataStoreError('DATA_CHANGED')
                    self.values[row['trade_date']] = row['factor']
            self.ranges.append((begin, end))

    def prefetch(self, rows):
        low, high = min(row['trade_date'] for row in rows), max(row['trade_date'] for row in rows)
        self._load(low, high)

    def transform(self, rows):
        self.prefetch(rows)
        if any(row['trade_date'] not in self.values for row in rows):
            raise MarketError('FACTOR_UNAVAILABLE')
        fields = ('open', 'high', 'low', 'close')
        values = adjusted_prices([(row[name], self.values[row['trade_date']], self.basis.anchor.factor)
            for row in rows for name in fields])
        if any(Decimal(value) <= 0 for value in values):
            raise MarketError('NUMERIC_RANGE_UNSUPPORTED')
        result = [dict(row) for row in rows]
        for index, row in enumerate(result):
            row.update(zip(fields, values[index * 4:index * 4 + 4]))
        return result


def factors(service, key, *, series_key=ETF_DAILY, start: date, end: date,
            as_of=None, limit=512, cursor=None) -> FactorPage:
    parse_instrument_key(key)
    policy = price_policy(service.settings, series_key)
    factor_key = policy.factor_key or ETF_FACTORS
    as_of = as_of or end
    if start < MIN_DATE or start > end or end > as_of or as_of.year > 9998 or not 1 <= limit <= 2000:
        raise DataStoreError('INVALID_VALUE')
    conditions = dict(operation='factors', instrument_key=key, series_key=series_key,
        start=start, end=end, as_of=as_of, limit=limit, policy=FACTOR_POLICY, policy_token=policy.token_for('none'))
    with service.read_scope((factor_key,), conditions=conditions,
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
            for row in read_observations(scope, factor_key, segment.source_code, low, high):
                if row['factor_basis'] != 'tushare_fund_adj':
                    raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
                rows.append(FactorRow(trade_date=row['trade_date'], factor=row['factor']))
                if len(rows) > limit:
                    break
            if len(rows) > limit:
                break
        more = len(rows) > limit
        rows = rows[:limit]
        note = policy.adjustment_note if policy.factor_key else FACTOR_NOTE
        meta = scope.meta(scope=f'{key} · 因子原值 · {start} 至 {min(end, before - ONE_DAY)}',
            business_as_of=max((row.trade_date for row in rows), default=None), limitations=[
                Notice(code='TEST_CONTRACT_ONLY' if policy.factor_key else 'FACTOR_ANCHOR_UNVERIFIED', message=note),
                Notice(code='FACTOR_COVERAGE_UNVERIFIED', message='仅返回实际因子日期，不补齐缺日或推断分段有效；该表独立于价格读取。')])
        result = FactorPage(items=list(reversed(rows)),
            next_cursor=scope.make_cursor({'before': rows[-1].trade_date.isoformat()}) if more else None,
            basis=PriceBasis(adjustment='none', policy=FACTOR_POLICY, as_of=as_of, anchor=None, note=note), meta=meta)
        scope.response(result)
    return result
