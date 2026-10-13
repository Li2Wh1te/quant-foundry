"""D10 current ETF observations and bounded complete-bucket display pages.

E70 stays provider_reported in the formal store. RAW_POLICY is a display
interpretation evidenced in ops/s2-d10/RESULT.md, never an execution admission.
No factor, supplier, collection repository or backtest provider is used here.
"""
from __future__ import annotations

import base64
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from typing import Iterator
from zoneinfo import ZoneInfo

from app.data_store.errors import DataStoreError
from app.data_store.readers import Query
from .bindings import ETF_CATALOG, ETF_DAILY, EXCHANGE_CALENDAR
from .contracts import (Adjustment, BarPage, BarWindow, Capability, Coverage, MarketBar,
                        Notice, Period, PriceBasis, SeriesOption)
from .errors import MarketError
from .service import IdentityQuery, MarketReadService, parse_instrument_key

RAW_POLICY = 'tushare-fund-daily-unadjusted@1'
RAW_NOTE = '本地正式 fund_daily 成交价，不施加复权；当前修订数据，不恢复历史当时所知。'
ONE_DAY = timedelta(days=1)
MIN_DATE = date(1900, 1, 1)


def bucket_bounds(day: date, period: Period) -> tuple[date, date]:
    """Missing daily -> calendar-period extension; S3 minute resampling is separate."""
    if period == 'day':
        return day, day
    if period == 'week':
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    if period == 'month':
        return day.replace(day=1), day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return day.replace(month=1, day=1), day.replace(month=12, day=31)


def aggregate_bucket(rows: list[dict], period: Period, *, start: date, end: date,
                     sessions: dict[date, bool] | None) -> tuple[MarketBar, list[date] | None]:
    """Pure exact OHLC extension, reusable by D11 AFTER its checked transform.

    A missing session is never filled, nor inferred to be a suspension. Full
    calendar facts are required for complete; partial and unknown stay distinct.
    """
    rows = sorted(rows, key=lambda row: row['trade_date'])
    first, last = rows[0], rows[-1]
    begin, finish = bucket_bounds(first['trade_date'], period)
    days = {row['trade_date'] for row in rows}
    required_start, required_end = max(begin, start), min(finish, end)
    dates = [required_start + timedelta(days=i) for i in range((required_end - required_start).days + 1)]
    missing = None if sessions is None or any(day not in sessions for day in dates) else sorted(
        day for day in dates if sessions[day] and day not in days)
    if sessions is not None and any(sessions.get(day) is False for day in days):
        raise DataStoreError('DATA_RESTRICTED')
    completion = ('partial' if begin < start or finish > end or missing else
                  'unknown' if missing is None else 'complete')
    with localcontext() as context:
        context.prec = 80  # bounded admitted prices/quantities + <=366 daily sums
        values = {name: [Decimal(row[name]) if row.get(name) is not None else None for row in rows]
                  for name in ('open', 'high', 'low', 'close', 'volume_lots', 'turnover_thousand_yuan')}
        for index in range(len(rows)):
            o, h, l, c = (values[name][index] for name in ('open', 'high', 'low', 'close'))
            if any(v is None or not v.is_finite() or v <= 0 for v in (o, h, l, c)) or h < max(o, c, l) or l > min(o, c):
                raise DataStoreError('DATA_RESTRICTED')
        def total(name):
            if any(value is None for value in values[name]):
                return None
            if any(not value.is_finite() or value < 0 for value in values[name]):
                raise DataStoreError('DATA_RESTRICTED')
            return format(sum(values[name], Decimal(0)), 'f')
        result = MarketBar(bucket_start=begin, bucket_end=finish,
            first_trade_date=first['trade_date'], last_trade_date=last['trade_date'],
            open=first['open'], high=format(max(values['high']), 'f'), low=format(min(values['low']), 'f'),
            close=last['close'], volume=total('volume_lots'), amount=total('turnover_thousand_yuan'),
            completion=completion)
    return result, missing


def _months(start, end, descending=True):
    day = end.replace(day=1) if descending else start.replace(day=1)
    while start.replace(day=1) <= day <= end:
        yield day
        if descending:
            if day.year == 1 and day.month == 1:
                break
            day = (day - ONE_DAY).replace(day=1)
        else:
            if day.month == 12:
                day = date(day.year + 1, 1, 1)
            else:
                day = day.replace(month=day.month + 1)


def _read(scope, binding_key, query):
    page = scope.read(binding_key, query)
    # Explicit stream work budget, in addition to D08 cumulative rows/bytes.
    scope._series_scan_bytes = getattr(scope, '_series_scan_bytes', 0) + page.metrics.current_read_bytes
    scope._series_files = getattr(scope, '_series_files', 0) + page.metrics.files_read
    if scope._series_scan_bytes > scope.store.limits.query_scan_bytes or scope._series_files > scope.store.limits.query_files:
        raise DataStoreError('QUERY_BUDGET_EXCEEDED')
    return page


def read_observations(scope, binding_key, subject, start, end) -> Iterator[dict]:
    """Read complete scalar objects in descending business-date order.

    Twelve actual formal partitions per input batch. Existing per-read row,
    scan/file budgets AND D08 cumulative input/output/deadline remain enforced.
    """
    binding = scope.service.bindings[binding_key]
    # These installed observations are scalar root objects. A changed layout
    # must refuse instead of accidentally decoding a partial complex object.
    if len(binding.entry.layout.models) != 1 or binding.entry.layout.children:
        raise DataStoreError('REBUILD_REQUIRED')
    months = list(_months(start, end))
    for offset in range(0, len(months), 12):
        chunk = months[offset:offset + 12]
        lower = max(start, chunk[-1])
        upper = min(end, bucket_bounds(chunk[0], 'month')[1])
        prefix = (binding.representation, subject)
        query = Query(partitions=tuple(binding.partition(subject, day.isoformat()) for day in chunk),
            lower=(*prefix, lower.isoformat()), upper=(*prefix, upper.isoformat() + '\x00'),
            page_size=min(1000, scope.store.limits.query_rows), descending=True,
            filters=(('representation', '=', binding.representation),), require_qualified=True)
        while True:
            page = _read(scope, binding_key, query)
            for item in scope.objects(binding_key, page):
                day = item[binding.business_key]
                if type(day) is str:
                    day = date.fromisoformat(day)
                item[binding.business_key] = day
                if item.get('source_code', item.get('exchange_scope')) != subject or not lower <= day <= upper:
                    raise DataStoreError('FILE_INVALID')
                yield item
            if not page.next_cursor:
                break
            query = Query(
                partitions=query.partitions, lower=query.lower, upper=query.upper, page_size=query.page_size,
                descending=True, filters=query.filters, cursor=page.next_cursor, require_qualified=True)


def _history_start(service, key, end):
    # This metadata-only discovery selects an existing evidenced lifespan. The
    # actual price session recaptures/validates the complete selected mapping.
    with service.read_scope((ETF_DAILY,), conditions={'operation': 'history-bound', 'key': key, 'end': end},
            identity=IdentityQuery(instrument_keys=(key,), start=MIN_DATE, end=end)) as scope:
        starts = [row['valid_from'] for row in scope.captured['identity']]
        if not starts:
            raise MarketError('IDENTITY_UNAVAILABLE')
        return min(starts)


def _metadata(scope, subject):
    binding = scope.service.bindings[ETF_CATALOG]
    prefix = (binding.representation, subject, subject)
    page = _read(scope, ETF_CATALOG, Query(partitions=(binding.partition(subject, subject),),
        lower=prefix, upper=(*prefix[:2], subject + '\x00'), page_size=2,
        filters=(('representation', '=', binding.representation),), require_qualified=True))
    rows = scope.objects(ETF_CATALOG, page)
    if len(rows) != 1 or page.next_cursor:
        raise MarketError('CAPABILITY_UNAVAILABLE')
    return rows[0]


def _calendar(scope, segments, start, end):
    if not hasattr(scope, '_series_exchange'):
        exchanges = {_metadata(scope, segment.source_code)['exchange'] for segment in segments}
        aliases = {'SH': 'SSE', 'SSE': 'SSE', 'SZ': 'SZSE', 'SZSE': 'SZSE'}
        if len(exchanges) != 1 or next(iter(exchanges)) not in aliases:
            raise MarketError('CAPABILITY_UNAVAILABLE')
        scope._series_exchange = aliases[next(iter(exchanges))]
        scope._series_calendar = {}
        scope._series_calendar_ranges = []
    unread = []
    position = start
    for low, high in sorted(scope._series_calendar_ranges):
        if high < position or low > end:
            continue
        if position < low:
            unread.append((position, min(end, low - ONE_DAY)))
        position = max(position, high + ONE_DAY)
    if position <= end:
        unread.append((position, end))
    for low, high in unread:
        for row in read_observations(scope, EXCHANGE_CALENDAR, scope._series_exchange, low, high):
            if row['scope_basis'] != 'declared_exchange':
                raise MarketError('CAPABILITY_UNAVAILABLE')
            scope._series_calendar[row['calendar_date']] = row['is_open']
        scope._series_calendar_ranges.append((low, high))
    return scope._series_calendar


def _untrusted_position(cursor):
    # Only bounded ISO coordinates are recovered to reconstruct conditions.
    # D08 authenticates the full token BEFORE any price/calendar file read.
    try:
        if not cursor or len(cursor) > 8192:
            raise ValueError()
        raw = base64.b64decode(cursor, altchars=b'-_', validate=True)
        position = json.loads(raw[:-32])['position']
        if type(position) is not dict or set(position) != {'before', 'read_start', 'end', 'as_of'}:
            raise ValueError()
        return {name: date.fromisoformat(value) for name, value in position.items()}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise DataStoreError('INVALID_CURSOR') from None


def bars(service: MarketReadService, key: str, *, series_key=ETF_DAILY, period: Period='day',
         adjustment: Adjustment='none', start=None, end=None, as_of=None, limit=512, cursor=None) -> BarPage:
    parse_instrument_key(key)
    if series_key != ETF_DAILY:
        raise MarketError('CAPABILITY_UNAVAILABLE')
    if adjustment != 'none':
        raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
    resumed = _untrusted_position(cursor) if cursor else None
    end = end or (resumed['end'] if resumed else as_of or datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date())
    as_of = as_of or (resumed['as_of'] if resumed else end)
    if end > as_of or start and start > end or not 1 <= limit <= 2000 or end < MIN_DATE or as_of.year > 9998:
        raise DataStoreError('INVALID_VALUE')
    read_start = start or (resumed['read_start'] if resumed else _history_start(service, key, end))
    if read_start > end:
        raise DataStoreError('INVALID_CURSOR' if cursor else 'INVALID_VALUE')
    keys = (ETF_DAILY,) if period == 'day' else (ETF_DAILY, ETF_CATALOG, EXCHANGE_CALENDAR)
    conditions = {'instrument_key': key, 'series_key': series_key, 'period': period, 'adjustment': adjustment,
                  'start': start, 'read_start': read_start, 'end': end, 'as_of': as_of, 'limit': limit,
                  'policy': RAW_POLICY}
    identity = IdentityQuery(instrument_keys=(key,), start=read_start, end=end)
    with service.read_scope(keys, conditions=conditions, identity=identity, cursor=cursor) as scope:
        segments = scope.mapping_segments(key)
        before = end + ONE_DAY
        if scope.position:
            before = resumed['before']
            if not read_start < before <= end + ONE_DAY:
                raise DataStoreError('INVALID_CURSOR')
        stop = min(end, before - ONE_DAY)
        output = []
        pending = []
        observed_dates = set()
        def emit():
            if not pending:
                return
            # Stream one complete OHLC bucket. Calendar completeness is
            # checked once over the selected output span, on the same scope.
            result, _missing = aggregate_bucket(pending, period, start=read_start, end=end, sessions=None)
            output.append(result)
        for segment in reversed(segments):
            low, high = max(read_start, segment.valid_from), min(stop, (segment.valid_to - ONE_DAY) if segment.valid_to else stop)
            if low > high:
                continue
            for item in read_observations(scope, ETF_DAILY, segment.source_code, low, high):
                if item['price_basis'] != 'provider_reported' or item['price_unit'] != 'yuan':
                    raise MarketError('PRICE_BASIS_UNVERIFIED')
                if pending and bucket_bounds(item['trade_date'], period)[0] != bucket_bounds(pending[0]['trade_date'], period)[0]:
                    emit()
                    pending = []
                    if len(output) > limit:
                        break
                pending.append(item)
                observed_dates.add(item['trade_date'])
            if len(output) > limit:
                break
        if len(output) <= limit:
            emit()
        more = len(output) > limit
        output = output[:limit]
        notices = [Notice(code='CURRENT_REVISIONS', message=RAW_NOTE),
                   Notice(code='INDICATOR_WARMUP', message='指标只使用有界已加载输出；左端不足参数窗口时不可计算。')]
        # Include empty buckets between observed outputs, and the requested
        # edges on the final page. Missing whole weeks must not vanish.
        coverage_start = max(read_start, output[-1].bucket_start) if output and more else read_start
        sessions = _calendar(scope, segments, coverage_start, stop) if period != 'day' else None
        known_missing = set()
        if sessions is not None:
            for bar in output:
                low, high = max(bar.bucket_start, read_start), min(bar.bucket_end, end)
                days = [low + timedelta(days=i) for i in range((high - low).days + 1)]
                if any(sessions.get(day) is False for day in observed_dates if low <= day <= high):
                    raise DataStoreError('DATA_RESTRICTED')
                missing = {day for day in days if sessions.get(day) is True and day not in observed_dates}
                known_missing.update(missing)
                if missing:
                    # A missing required daily bar also leaves its quantities
                    # unknown. Observed OHLC remains a labelled partial bucket.
                    bar.volume = bar.amount = None
                bar.completion = ('partial' if bar.bucket_start < read_start or bar.bucket_end > end or missing else
                                  'complete' if all(day in sessions for day in days) else 'unknown')
            known_missing.update(day for day, opened in sessions.items() if opened and
                coverage_start <= day <= stop and day not in observed_dates)
        date_count = (stop - coverage_start).days + 1
        verified = sessions is not None and all(coverage_start + timedelta(days=i) in sessions for i in range(date_count))
        missing_dates = sorted(known_missing) if verified else None
        if missing_dates is not None and len(missing_dates) > 2000:
            raise DataStoreError('QUERY_BUDGET_EXCEEDED')
        if known_missing:
            notices.append(Notice(code='MISSING_DATES', message=f'正式日历要求的 {len(known_missing)} 个日期缺少行情；未推成停牌或补值。'))
        if any(bar.volume is None for bar in output):
            notices.append(Notice(code='VOLUME_MISSING', message='部分成交量缺失，价格仍可查看，量能指标不可用。'))
        if any(bar.completion == 'partial' for bar in output):
            notices.append(Notice(code='PARTIAL_BUCKET', message='部分周期被查询/回放截止截断，或缺少正式日历要求的日线；未补值。'))
        meta = scope.meta(scope=f'{key} · {period} · {read_start} 至 {stop}',
            business_as_of=max((bar.last_trade_date for bar in output), default=None), limitations=notices)
        meta.coverage = Coverage(verified=verified, state='partial' if missing_dates else 'verified' if verified else 'unknown',
            missing_dates=missing_dates, note='已按本页正式日历日期核验；缺日不解释为停牌。' if verified else '未核验全部必要业务日期，不能由已扫描日期证明完整。')
        result = BarPage(items=list(reversed(output)), series_key=series_key, instrument_key=key, period=period,
            next_cursor=scope.make_cursor({'before': output[-1].bucket_start.isoformat(), 'read_start': read_start.isoformat(),
                'end': end.isoformat(), 'as_of': as_of.isoformat()}) if more else None,
            units=service.bindings[ETF_DAILY].declared_units, window=BarWindow(start=start, end=end, as_of=as_of),
            basis=PriceBasis(adjustment='none', policy=RAW_POLICY, as_of=as_of, note=RAW_NOTE), meta=meta)
        scope.response(result)
    return result


def series(service, key):
    parse_instrument_key(key)
    end = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
    start = _history_start(service, key, end)
    with service.read_scope((ETF_DAILY,), conditions={'operation': 'series', 'key': key, 'start': start,
            'end': end, 'policy': RAW_POLICY}, identity=IdentityQuery(instrument_keys=(key,), start=start, end=end)) as scope:
        segments = scope.mapping_segments(key)
        observed = False
        for segment in reversed(segments):
            low, high = max(start, segment.valid_from), min(end, segment.valid_to - ONE_DAY if segment.valid_to else end)
            if low <= high:
                row = next(read_observations(scope, ETF_DAILY, segment.source_code, low, high), None)
                if row is not None:
                    if row['price_basis'] != 'provider_reported' or row['price_unit'] != 'yuan':
                        raise MarketError('PRICE_BASIS_UNVERIFIED')
                    observed = True
                    break
        capability = Capability(name='raw', availability='available' if observed else 'unavailable',
            request_checked=True, metadata_known=True, limitations=[
                Notice(code='CURRENT_REVISIONS', message=RAW_NOTE),
                Notice(code='REQUEST_CHECK_REQUIRED', message='具体日期质量逐请求核查；周/月/年完整性另需正式交易所资料与日历。')])
        option = SeriesOption(series_key=ETF_DAILY, label='ETF 不复权日线 · 本地正式数据', instrument_key=key,
            periods=['day', 'week', 'month', 'year'], adjustments=['none'], price_basis='unadjusted',
            adjustment_capabilities={'none': capability,
                'pre': Capability(name='pre', availability='unavailable', request_checked=False,
                    limitations=[Notice(code='FACTOR_ANCHOR_UNVERIFIED', message='复权接线由独立因子模块提供。')]),
                'post': Capability(name='post', availability='unavailable', request_checked=False,
                    limitations=[Notice(code='FACTOR_ANCHOR_UNVERIFIED', message='复权接线由独立因子模块提供。')])},
            units=service.bindings[ETF_DAILY].declared_units, capability=capability)
        scope.response(option)
    return [option]
