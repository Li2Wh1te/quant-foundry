"""Stable public research API. D10 supplies a callback, D12 the run-bound IPC.

All values/visibility/indicators are checked in Rust. No database, Arrow parser,
source identifiers, evaluation, supplier access or engine implementation here.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
import json
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

import pandas as pd
from pandas import DataFrame

from . import _native
from .numeric import nanoseconds, _text
from ._transport import TransportError

TimeLike = datetime | str | int
Frequency = Literal['1d', '1m', '5m', '15m', '30m', '60m', 'tick']
FIELDS = {
    'fundamentals': ('revenue', 'net_profit', 'total_assets', 'total_liabilities', 'eps', 'roe'),
    'valuation': ('pe_ratio', 'pb_ratio', 'market_cap', 'circulating_market_cap'),
    'industry': ('industry_code', 'industry_name', 'taxonomy'),
    'instruments': ('name', 'exchange', 'asset_type', 'currency', 'listing_date', 'end_date'),
    'index_stocks': ('member',), 'adjustment': ('factor',), 'status': ('halted',),
}
PRICE_FIELDS = ('open', 'high', 'low', 'close')
TICK_FIELDS = ('price', 'bid', 'ask', 'quantity', 'bid_quantity', 'ask_quantity', 'kind', 'channel', 'sequence')
BAR_FIELDS = (*PRICE_FIELDS, 'quantity', 'interval_start_ns', 'interval_end_ns', 'is_partial', 'status')


def _error(code, message, operation='research_data', scope=None):
    error = _native.ContractError(message)
    error.code, error.message, error.operation, error.scope = code, message, operation, scope or {}
    return error


def _json(value):
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError, RecursionError):
        raise _error('INVALID_CONTRACT', '研究参数必须是有界结构化值') from None
    if len(text.encode()) > 1024*1024:
        raise _error('RESOURCE_LIMIT', '研究参数超过消息预算')
    return text


@dataclass(frozen=True)
class Filter:
    field: str
    op: Literal['eq', 'ne', 'lt', 'le', 'gt', 'ge', 'in']
    value: Any

    def __post_init__(self):
        if type(self.field) is not str or self.op not in ('eq', 'ne', 'lt', 'le', 'gt', 'ge', 'in'):
            raise _error('INVALID_CONTRACT', '筛选只支持白名单字段和结构化比较')
        value = self.value
        if self.op == 'in':
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) > 128:
                raise _error('RESOURCE_LIMIT', 'in 筛选需要最多 128 个结构化值')
            value = tuple(value)
            for v in value:
                _filter_value(v)
        else:
            _filter_value(value)
        object.__setattr__(self, 'value', value)


def _filter_value(value):
    if type(value) is bool:
        return 'true' if value else 'false'
    if isinstance(value, (Decimal, int)) and not isinstance(value, bool):
        result = _text(value)
    elif type(value) is str:
        result = value
    else:
        raise _error('INVALID_CONTRACT', '筛选值需要 Decimal、整数、布尔或明确字符串')
    if len(result.encode()) > 128 or result in ('NaN', 'Infinity', '-Infinity'):
        raise _error('NUMERIC_RANGE_UNSUPPORTED', '筛选值超出精确表示范围')
    return result


@dataclass(frozen=True)
class CurrentQuote:
    security: str
    time_ns: int
    last_price: Decimal | None
    price_time_ns: int | None
    is_stale: bool
    halted: bool | None


@dataclass(frozen=True)
class Tick(CurrentQuote):
    channel: str
    sequence: int | str | None
    kind: Literal['trade', 'quote']
    bid: Decimal | None
    ask: Decimal | None
    quantity: int | None
    bid_quantity: int | None
    ask_quantity: int | None


@dataclass(frozen=True)
class _MarketRoute:
    binding: str
    fields: tuple[str, ...]


class _DataSession:
    """Trusted wiring seam; public functions never accept routes/paths/SQL.

    D10 enters _callback for each initialize/event/timer/notification boundary.
    D12 supplies Client over the sole _transport, D04/D13 choose accepted routes.
    """
    def __init__(self, client, *, universe, frequency, market_routes, research_routes=None,
                 calendar=(), max_rows=10000, max_bytes=64*1024*1024, cache_bytes=8*1024*1024):
        if (type(max_rows) is not int or not 1 <= max_rows <= 100000
                or type(max_bytes) is not int or not 1 <= max_bytes <= 64*1024*1024
                or type(cache_bytes) is not int or not 1 <= cache_bytes <= max_bytes):
            raise _error('RESOURCE_LIMIT', '研究会话预算无效')
        self.client, self.universe, self.frequency = client, tuple(universe), frequency
        self.allowed_securities = frozenset(universe)
        self.market_routes, self.research_routes = dict(market_routes), dict(research_routes or {})
        self.calendar = json.loads(_json(calendar))
        self.max_rows, self.max_bytes, self.cache_bytes = max_rows, max_bytes, cache_bytes
        self.cache, self.cache_size = {}, 0
        self.batch_rows = 1024
        self.units, self.limitations = {}, []

    def call(self, op, body):
        try:
            return self.client.call(op, body)
        except TransportError as error:
            self.cache.clear()
            self.cache_size = 0
            raise _error(error.code, error.message, error.operation, error.scope) from None

    def check(self):
        response = self.call('check', {})
        if response.body.get('check') != 'unchanged':
            raise _error('DATA_CHANGED', '研究依赖状态不一致')
        self.batch_rows = response.body['limits']['batch_rows']
        return _json(response.body.get('context'))

    def stream(self, binding, request, view, *, research=False):
        result = self.call('open_stream', dict(binding=binding, request=request, max_rows=min(self.batch_rows, self.max_rows)))
        stream_id = result.body.get('stream_id')
        if type(stream_id) is not str or not 0 < len(stream_id.encode()) <= 128:
            raise _error('INVALID_CONTRACT', '数据流标识无效')
        try:
            while True:
                batch = self.call('next', dict(stream_id=stream_id))
                if batch.status == 'eof':
                    break
                self.limitations.extend(v for v in batch.body.get('limitations', ()) if v not in self.limitations)
                if research:
                    for name, (_, unit) in batch.body['fields'].items():
                        self.units[name] = unit
                else:
                    for f in request['fields']:
                        if 'quantity' in f:
                            self.units[f] = batch.body['quantity_unit']
                        elif f in (*PRICE_FIELDS, 'price', 'bid', 'ask'):
                            self.units[f] = batch.body['price_currency']
                        elif f in ('interval_start_ns', 'interval_end_ns'):
                            self.units[f] = batch.body['time_unit']
                (view.push_research if research else view.push_market)(_json(batch.body), batch.payload)
        except BaseException:
            # A native decode/budget failure is terminal, and must release the
            # parent stream/locks immediately, without relying on Python GC.
            if not self.client.channel.closed:
                try:
                    self.call('close_stream', dict(stream_id=stream_id))
                except (_native.ContractError, OSError):
                    self.client.channel.close()
            raise


@dataclass
class _Callback:
    session: _DataSession
    boundary: dict
    view: Any
    published_quotes: Mapping | None = None


_ACTIVE: ContextVar[_Callback | None] = ContextVar('qf_read_callback', default=None)


@contextmanager
def _callback(session: _DataSession, boundary: Mapping, *, published_quotes: Mapping | None = None):
    if _ACTIVE.get() is not None:
        raise _error('INVALID_CONTRACT', '不能递归进入策略读取回调')
    boundary = json.loads(_json(boundary))
    view = _native.ReadView(_json(boundary), session.max_rows, session.max_bytes)
    if published_quotes is not None and (not isinstance(published_quotes, Mapping) or len(published_quotes)>10000):
        view.expire()
        raise _error('RESOURCE_LIMIT', '当前行情内存视图超过标的预算')
    active = _Callback(session, boundary, view, published_quotes)
    token = _ACTIVE.set(active)
    try:
        yield active
    finally:
        view.expire()
        _ACTIVE.reset(token)


def _active():
    active = _ACTIVE.get()
    if active is None:
        raise _error('CAPABILITY_UNAVAILABLE', '数据 API 只能在有效的模拟回调中读取')
    active.view.check()
    return active


def _securities(value, active, *, default=False, single=False):
    if value is None and default:
        value = active.session.universe
    if single and type(value) is str:
        value = [value]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) > 10000:
        raise _error('RESOURCE_LIMIT', '标的集合必须有界')
    if any(type(s) is not str or not s or len(s.encode()) > 128 for s in value) or len(set(value)) != len(value):
        raise _error('INVALID_CONTRACT', '标的集合包含无效/重复标识')
    return sorted(value)


def _time(value, active, *, start=False):
    if value is None:
        return int(active.boundary['now_ns'])
    if type(value) is int:
        return nanoseconds(value)
    if type(value) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        sessions = [s for s in active.session.calendar if s['date'] == value]
        if len(sessions) != 1:
            raise _error('RULE_UNAVAILABLE', '该日期没有唯一的已接受市场会话边界')
        return int(sessions[0]['session']['open_ns' if start else 'close_ns'])
    if not isinstance(value, (str, datetime)) or isinstance(value, bool):
        raise _error('INVALID_CONTRACT', '时间需要 UTC 纳秒整数或有时区 datetime/ISO 字符串')
    try:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            raise ValueError()
        return nanoseconds(int(stamp.value))
    except (ValueError, OverflowError, TypeError):
        raise _error('INVALID_CONTRACT', '时间缺少时区或超出 UTC 纳秒范围') from None


def _at(value, active):
    result = _time(value, active)
    if result > int(active.boundary['now_ns']):
        raise _error('LOOKAHEAD_FORBIDDEN', '查询时点晚于模拟时间')
    return result


def _frame(rows, columns, *, limitations=(), requested=None, units=None, adjustment='none'):
    # dtype=object prevents Pandas from converting quantities/nulls or Decimal
    # originals to float. Only the mandatory ns column is int64.
    frame = DataFrame(rows, columns=columns, dtype=object)
    if 'time_ns' in columns:
        frame['time_ns'] = frame['time_ns'].astype('int64')
    qf = dict(actual_scope=dict(start_ns=None if frame.empty else str(int(frame['time_ns'].min())),
        end_ns=None if frame.empty else str(int(frame['time_ns'].max())),
        securities=[] if frame.empty else sorted(frame['security'].unique().tolist())),
        limitations=list(dict.fromkeys(limitations)), requested_scope=requested or {},
        units=units or {}, adjustment=adjustment, precision='decimal_original', coverage='observed_rows_not_coverage')
    frame.attrs['qf'] = qf
    return frame


def _market_fetch(active, securities, start, end, count, source, route, fields):
    now = int(active.boundary['now_ns'])
    requests = []
    # Per-security count must be applied AFTER the complete publication-key
    # clipping. At now, request the current ns separately so prefetched later
    # ticks cannot evict the last visible event from the historical count.
    historical_end = end if end < now else end-1
    if historical_end >= -(2**63) and (start is None or start <= historical_end):
        requests.append(dict(securities=securities, fields=fields, frequency=source,
            start_ns=None if count is not None else str(start), end_ns=str(historical_end),
            count_per_security=count, adjustment='none'))
    if end == now:
        requests.append(dict(securities=securities, fields=fields, frequency=source,
            start_ns=str(now), end_ns=str(now), count_per_security=None, adjustment='none'))
    for request in requests:
        active.session.stream(route.binding, request, active.view)


def _research_fetch(active, kind, securities, fields, at):
    binding = active.session.research_routes.get(kind)
    if binding is None:
        raise _error('CAPABILITY_UNAVAILABLE', '缺少已接受公开时间/身份/生效期的正式研究映射',
                     scope={'kind': kind})
    active.session.stream(binding, dict(kind=kind, securities=securities, fields=list(fields), as_of_ns=str(at)),
                          active.view, research=True)


def get_price(securities: str | Sequence[str], *, start: TimeLike | None = None, end: TimeLike | None = None,
              count: int | None = None, frequency: Frequency | None = None, fields: Sequence[str] = ('close',),
              adjustment: Literal['none', 'pre', 'post'] = 'none') -> DataFrame:
    active = _active()
    securities = _securities(securities, active, single=True)
    end = _at(end, active)
    start = None if start is None else _time(start, active, start=True)
    frequency = frequency or active.session.frequency
    allowed = TICK_FIELDS if frequency == 'tick' else BAR_FIELDS
    if frequency not in ('1d', '1m', '5m', '15m', '30m', '60m', 'tick') or adjustment not in ('none', 'pre', 'post'):
        raise _error('CAPABILITY_UNAVAILABLE', '频率或复权方式不受支持')
    if isinstance(fields, str) or not fields or len(fields) > 128 or len(set(fields)) != len(fields) or any(f not in allowed for f in fields):
        raise _error('CAPABILITY_UNAVAILABLE', '请求字段与频率不匹配，Tick 需要明确 price/bid/ask 等字段')
    if (start is None) == (count is None) or start is not None and start > end:
        raise _error('INVALID_CONTRACT', 'start 和 count 必须二选一且范围有序')
    if count is not None and (type(count) is not int or not 1 <= count <= 10000 or count*len(securities) > active.session.max_rows):
        raise _error('RESOURCE_LIMIT', '每标的 count 或横截面窗口超限')
    request = dict(securities=securities, fields=list(fields), frequency=frequency, start_ns=None if start is None else str(start),
                   end_ns=str(end), count_per_security=count, adjustment=adjustment)
    columns = ['security', 'time_ns', *fields]
    if not securities:
        return _frame([], columns, requested=request, adjustment=adjustment)
    dependencies = active.session.check()
    key = ('price', _json(active.boundary), _json(request), dependencies)
    encoded = active.session.cache.get(key)
    if encoded is None:
        active.view.reset()
        active.session.units, active.session.limitations = {}, []
        route = active.session.market_routes.get(frequency)
        source = frequency
        if route is None and frequency not in ('1d', 'tick'):
            candidates = [f for f in active.session.market_routes if f not in ('1d', 'tick')
                          and int(f[:-1]) < int(frequency[:-1]) and int(frequency[:-1]) % int(f[:-1]) == 0]
            if candidates:
                source = max(candidates, key=lambda f: int(f[:-1]))
                route = active.session.market_routes[source]
        if route is None:
            if frequency != '1d' or 'price' not in active.session.research_routes:
                raise _error('CAPABILITY_UNAVAILABLE', '所需行情频率/单位/公开边界尚未具备正式映射')
            _research_fetch(active, 'price', securities, list(fields), end)
            active.view.research_prices(_json(request))
        else:
            selected = list(PRICE_FIELDS)+['quantity'] if source != frequency else [f for f in fields if f in route.fields]
            if source == frequency and any(f not in route.fields and f not in ('kind', 'channel', 'sequence', 'interval_start_ns', 'interval_end_ns', 'is_partial', 'status') for f in fields):
                raise _error('CAPABILITY_UNAVAILABLE', '正式入口未提供请求字段')
            if not selected:
                selected = [route.fields[0]]
            factor = int(frequency[:-1])//int(source[:-1]) if source != frequency else 1
            source_count = None if count is None else (count+1)*factor if factor>1 else count
            if source_count is not None and source_count*len(securities) > active.session.max_rows:
                raise _error('RESOURCE_LIMIT', '降采样输入窗口超过预算')
            _market_fetch(active, securities, start, end, source_count, source, route, selected)
        if adjustment != 'none':
            _research_fetch(active, 'adjustment', securities, ['factor'], end)
        rows = json.loads(active.view.prices_json(_json(request), source, _json(active.session.calendar)))
        encoded = json.dumps(dict(rows=rows, units=active.session.units, limitations=active.session.limitations), ensure_ascii=False)
        dependencies = active.session.check()
        key = ('price', _json(active.boundary), _json(request), dependencies)
        if len(encoded.encode()) <= active.session.cache_bytes:
            if len(active.session.cache) >= 32 or active.session.cache_size+len(encoded.encode()) > active.session.cache_bytes:
                active.session.cache.clear(); active.session.cache_size = 0
            active.session.cache[key] = encoded
            active.session.cache_size += len(encoded.encode())
    stored = json.loads(encoded)
    rows = stored['rows']
    for row in rows:
        for f in fields:
            if row[f] is not None and f in (*PRICE_FIELDS, 'price', 'bid', 'ask'):
                row[f] = Decimal(row[f])
            elif row[f] is not None and f in ('quantity', 'bid_quantity', 'ask_quantity', 'sequence', 'interval_start_ns', 'interval_end_ns'):
                row[f] = int(row[f])
    frame = _frame(rows, columns, requested=request, adjustment=adjustment,
        limitations=(*stored['limitations'], 'current_revision_not_historical_snapshot', 'research_prices_are_not_matching_prices'),
        units={f: stored['units'][f] for f in fields if f in stored['units']})
    if count is not None:
        counts = frame.groupby('security').size().to_dict()
        frame.attrs['qf']['short_window'] = {s: counts.get(s, 0) for s in securities if counts.get(s, 0)<count}
    return frame


def get_current_data(securities: Sequence[str] | None = None) -> Mapping[str, CurrentQuote]:
    active = _active()
    securities = _securities(securities, active, default=True)
    if not securities:
        return MappingProxyType({})
    if active.published_quotes is not None:
        # D10 supplies its already published, accepted Rust quote snapshot and
        # complete D03 keys. This path performs no parent IPC per tick. Values
        # are frozen materialized DTOs; the live read is still lease checked.
        result = {}
        if any(s not in active.session.allowed_securities for s in securities):
            raise _error('DATA_RESTRICTED', '当前行情标的超出运行集合')
        for security in securities:
            item = active.published_quotes.get(security)
            quote = CurrentQuote(security, int(active.boundary['now_ns']), None, None, True, None)
            if item is not None:
                source, key = item
                if not isinstance(source, CurrentQuote) or source.security!=security or key['security']!=security:
                    raise _error('INVALID_CONTRACT', '当前行情身份与已发布键不一致')
                if active.view.visible_key_json(_json(key)):
                    if (type(source.time_ns) is not int or source.time_ns!=int(key['time_ns'])
                            or source.price_time_ns is not None and (type(source.price_time_ns) is not int
                            or source.price_time_ns>int(active.boundary['now_ns']))):
                        raise _error('LOOKAHEAD_FORBIDDEN', '当前行情字段晚于已发布边界')
                    if isinstance(source, Tick) and (source.channel!=key['identity']['channel']
                            or (None if source.sequence is None else str(source.sequence)) != key['identity']['sequence']
                            or source.kind not in ('trade', 'quote')):
                        raise _error('INVALID_CONTRACT', '当前 Tick 身份与已发布键不一致')
                    for f in ('last_price','bid','ask'):
                        value=getattr(source,f,None)
                        if value is not None and (not isinstance(value,Decimal) or not value.is_finite()):
                            raise _error('INVALID_CONTRACT', '当前行情原价必须是精确 Decimal')
                    for f in ('quantity','bid_quantity','ask_quantity'):
                        value=getattr(source,f,None)
                        if value is not None and (type(value) is not int or value<0):
                            raise _error('INVALID_CONTRACT', '当前行情数量必须是整数或未知')
                    price_time = source.price_time_ns if source.last_price is not None else source.time_ns
                    known = source.last_price is not None or getattr(source,'bid',None) is not None or getattr(source,'ask',None) is not None
                    quote = replace(source,is_stale=not known or price_time is None or price_time<int(active.boundary['now_ns']))
            result[security] = quote
        return MappingProxyType(result)
    route = active.session.market_routes.get(active.session.frequency)
    if route is None:
        raise _error('CAPABILITY_UNAVAILABLE', '当前报价尚无已接受行情映射')
    dependencies = active.session.check()
    cache_key = ('current', _json(active.boundary), _json(securities), dependencies)
    encoded = active.session.cache.get(cache_key)
    if encoded is not None:
        return _quotes(json.loads(encoded))
    active.view.reset()
    fields = [f for f in route.fields if f in (*PRICE_FIELDS, *TICK_FIELDS) and f not in ('kind', 'channel', 'sequence')]
    _market_fetch(active, securities, None, int(active.boundary['now_ns']), 1, active.session.frequency, route, fields)
    if 'status' in active.session.research_routes:
        _research_fetch(active, 'status', securities, ['halted'], int(active.boundary['now_ns']))
    encoded = active.view.current_json(_json(securities))
    rows = json.loads(encoded)
    dependencies = active.session.check()
    cache_key = ('current', _json(active.boundary), _json(securities), dependencies)
    if active.session.cache_size+len(encoded.encode()) <= active.session.cache_bytes and len(active.session.cache)<32:
        active.session.cache[cache_key] = encoded
        active.session.cache_size += len(encoded.encode())
    return _quotes(rows)


def _quotes(rows):
    result = {}
    for security, row in rows.items():
        for f in ('time_ns', 'price_time_ns', 'quantity', 'bid_quantity', 'ask_quantity', 'sequence'):
            if f in row and row[f] is not None:
                row[f] = int(row[f])
        for f in ('last_price', 'bid', 'ask'):
            if f in row and row[f] is not None:
                row[f] = Decimal(row[f])
        result[security] = (Tick if 'kind' in row else CurrentQuote)(**row)
    return MappingProxyType(result)


def _research(kind, securities, fields, as_of, filters=(), order_by=(), limit=10000):
    active = _active(); securities = _securities(securities, active)
    at = _at(as_of, active)
    if (isinstance(fields, str) or not fields or len(fields) > 128 or len(set(fields)) != len(fields)
            or any(f not in FIELDS[kind] for f in fields)):
        raise _error('CAPABILITY_UNAVAILABLE', '研究字段不在白名单内')
    if type(limit) is not int or not 1 <= limit <= 10000 or len(filters)>32 or isinstance(order_by, str) or len(order_by)>16:
        raise _error('RESOURCE_LIMIT', '研究筛选/排序/limit 超过预算')
    structured = []
    for f in filters:
        if not isinstance(f, Filter) or f.field not in fields:
            raise _error('INVALID_CONTRACT', '筛选需要所选白名单字段的 Filter')
        structured.append(dict(field=f.field, op=f.op, value=[_filter_value(v) for v in f.value] if f.op == 'in' else _filter_value(f.value)))
    if any(type(f) is not str or f.lstrip('-') not in (*fields, 'security') for f in order_by):
        raise _error('INVALID_CONTRACT', '排序只能使用所选字段，-field 为降序')
    columns = ['security', 'time_ns', 'instrument_id', 'report_period', *fields]
    request = dict(kind=kind, securities=securities, fields=list(fields), as_of_ns=str(at), filters=structured, order_by=list(order_by), limit=limit)
    if not securities:
        return _frame([], columns, requested=request)
    dependencies = active.session.check()
    cache_key = ('research', _json(active.boundary), _json(request), dependencies)
    encoded = active.session.cache.get(cache_key)
    if encoded is None:
        active.view.reset()
        _research_fetch(active, kind, securities, list(fields), at)
        encoded = active.view.research_json(_json(request))
        dependencies = active.session.check()
        cache_key = ('research', _json(active.boundary), _json(request), dependencies)
        size = len(encoded.encode())
        if size <= active.session.cache_bytes:
            if len(active.session.cache)>=32 or active.session.cache_size+size>active.session.cache_bytes:
                active.session.cache.clear(); active.session.cache_size = 0
            active.session.cache[cache_key] = encoded
            active.session.cache_size += size
    rows = json.loads(encoded)
    units = {}
    for row in rows:
        for f in fields:
            cell = row[f]
            if cell is None or cell['value'] is None:
                row[f] = None
                continue
            units[f] = cell['unit']
            row[f] = (Decimal(cell['value']) if cell['kind'] == 'decimal' else int(cell['value']) if cell['kind'] == 'integer'
                      else cell['value'] == 'true' if cell['kind'] == 'boolean' else cell['value'])
    return _frame(rows, columns, requested=request, units=units, limitations=('current_revision_not_historical_snapshot',))


def get_fundamentals(securities: Sequence[str], *, fields: Sequence[str], as_of: TimeLike | None = None,
                     filters: Sequence[Filter] = (), order_by: Sequence[str] = (), limit: int = 10000) -> DataFrame:
    return _research('fundamentals', securities, fields, as_of, filters, order_by, limit)


def get_valuation(securities: Sequence[str], *, fields: Sequence[str], as_of: TimeLike | None = None) -> DataFrame:
    return _research('valuation', securities, fields, as_of)


def get_index_stocks(index: str, *, as_of: TimeLike | None = None) -> Sequence[str]:
    frame = _research('index_stocks', [index], ['member'], as_of)
    return tuple(sorted(set(frame['member'].dropna().tolist())))


def get_industry(securities: Sequence[str], *, as_of: TimeLike | None = None) -> DataFrame:
    return _research('industry', securities, FIELDS['industry'], as_of)


def get_instruments(securities: Sequence[str] | None = None, *, as_of: TimeLike | None = None) -> DataFrame:
    return _research('instruments', _active().session.universe if securities is None else securities, FIELDS['instruments'], as_of)
