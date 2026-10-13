"""D09: bounded, read-only projections of the declared E67/E68 inputs.

Catalog keys identify an admitted source-directory record, never a newly minted
global instrument identity. History must still resolve an existing trusted key.
No supplier, collection repository, execution gate or persistent index is used.
"""
from datetime import date, timedelta
import re
import time

from sqlalchemy import text

from app.data_store.errors import DataStoreError
from app.data_store.readers import Query
from .bindings import ETF_CATALOG, ETF_DAILY, ETF_FACTORS, EXCHANGE_CALENDAR
from .contracts import (
    CalendarDay, CalendarResult, Capability, Instrument, InstrumentDetail,
    InstrumentPage, MarketOverview, Notice,
)
from .errors import MarketError
from .service import IdentityQuery, MarketReadService, parse_instrument_key

CATALOG_KEY_PREFIX = ETF_CATALOG + ':'
EXCHANGES = {'SSE': ('SSE', 'SH'), 'SZSE': ('SZSE', 'SZ')}
UNKNOWN_FIELDS = Notice(code='METADATA_FIELDS_UNKNOWN',
    message='当前正式目录未提供上市状态、跟踪指数、管理人及管理费率；这些字段保持未知。')
SOURCE_IDENTITY = Notice(code='SOURCE_DIRECTORY_IDENTITY',
    message='资料键仅标识本目录的来源代码记录；历史图表须另行核验已有可信标的身份。')


def _code(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value):
        raise DataStoreError('INVALID_VALUE')
    return value


def _exchange(value: str) -> str:
    if value not in EXCHANGES:
        raise DataStoreError('INVALID_VALUE')
    return value


def _catalog(value: str):
    if value != ETF_CATALOG:
        raise MarketError('CAPABILITY_UNAVAILABLE')


def _partitions(scope, key):
    """Only bounded formal inventory metadata; never native table rows."""
    scope.check()
    binding = scope.service.bindings[key]
    with scope.store.catalog.transaction() as connection:
        remaining = max(1, int((scope.deadline - time.monotonic()) * 1000))
        connection.execute(text("SELECT set_config('statement_timeout',:limit,true)"),
            {'limit': str(min(5000, remaining))})
        values = connection.execute(text('SELECT DISTINCT partition_key FROM data_store_files '
            'WHERE dataset=:dataset ORDER BY partition_key LIMIT :limit'),
            {'dataset': binding.entry.spec.name, 'limit': scope.store.limits.query_partitions + 1}).scalars().all()
    scope.check()
    if len(values) > scope.store.limits.query_partitions:
        raise DataStoreError('QUERY_BUDGET_EXCEEDED')
    return tuple(values) or (binding.partition('', 'current'),)


def _objects(scope, key, *, partitions=None, filters=(), lower=None, upper=None):
    """These two declared scalar models contain exactly one root per object.

    Finish every bounded current page, then decode complete objects. Internal
    store cursors never reach the browser; the domain cursor signs the final
    stable business ordering and every filter. Cumulative limits stay in scope.
    """
    binding = scope.service.bindings[key]
    partitions = partitions or _partitions(scope, key)
    cursor = None
    result = []
    while True:
        page = scope.read(key, Query(partitions=partitions, lower=lower, upper=upper,
            page_size=min(1000, scope.store.limits.query_rows), cursor=cursor,
            require_qualified=True,
            filters=(('representation', '=', binding.representation), *filters)))
        if any(row['member_key'] != 'root' for row in page.rows):
            raise DataStoreError('FILE_INVALID')
        result.extend(scope.objects(key, page))
        cursor = page.next_cursor
        if cursor is None:
            return result


def _records(scope, exchange=''):
    binding = scope.service.bindings[ETF_CATALOG]
    filters = ((binding.columns['exchange'], 'in', EXCHANGES[exchange]),) if exchange else ()
    objects = _objects(scope, ETF_CATALOG, filters=filters)
    if not objects:
        # No selected namespace is not evidence of an empty available catalog.
        # An exchange can legitimately have no matches in a readable catalog;
        # prove the declared namespace exists with one bounded formal read.
        if exchange:
            page = scope.read(ETF_CATALOG, Query(partitions=_partitions(scope, ETF_CATALOG),
                page_size=1, require_qualified=True,
                filters=(('representation', '=', binding.representation),)))
            probe = scope.objects(ETF_CATALOG, page)
            if probe and probe[0]['asset_type'] == 'fund-etf':
                return []
        raise MarketError('CAPABILITY_UNAVAILABLE')
    seen = set()
    for item in objects:
        code = _code(item['source_code'])
        if item['asset_type'] != 'fund-etf' or code in seen:
            raise DataStoreError('FILE_INVALID')
        seen.add(code)
    return sorted(objects, key=lambda item: (item['source_code'], item['exchange'] or ''))


def _instrument(item, *, instrument_key=None):
    # Do not infer exchange/status from a code suffix, end date or clock.
    exchange = item['exchange']
    exchange = next((key for key, aliases in EXCHANGES.items() if exchange in aliases), exchange)
    stable = instrument_key is not None
    identity_notice = Notice(code='CURRENT_IDENTITY_ONLY',
        message='已核验当前代码映射；任意历史窗口的映射和行情许可仍须独立检查。') if stable else SOURCE_IDENTITY
    capabilities = [
        Capability(name='metadata', availability='available', request_checked=True,
            metadata_known=True, limitations=[UNKNOWN_FIELDS]),
        Capability(name='identity_resolution', availability='available' if stable else 'unknown',
            request_checked=stable, metadata_known=stable, limitations=[identity_notice]),
    ]
    for name in (ETF_DAILY, ETF_FACTORS):
        capabilities.append(Capability(name=name, availability='unknown', request_checked=False,
            limitations=[Notice(code='REQUEST_CHECK_REQUIRED',
                message='资料可读不代表价格或因子可读；请在对应标的和日期请求中单独检查。')]))
    return Instrument(instrument_key=instrument_key or CATALOG_KEY_PREFIX + item['source_code'],
        symbol=item['source_code'], name=item['name'], asset_type='etf', exchange=exchange,
        list_date=item['listing_date'], capabilities=capabilities)


def instruments(service: MarketReadService, *, catalog_key=ETF_CATALOG, q='', exchange='',
                list_status='', limit=50, cursor=None) -> InstrumentPage:
    _catalog(catalog_key)
    if (type(q) is not str or len(q) > 128 or re.search(r'[\x00-\x1f]', q)
            or type(limit) is not int or not 1 <= limit <= 200
            or list_status not in ('', 'L', 'D', 'P')):
        raise DataStoreError('INVALID_VALUE')
    if exchange:
        _exchange(exchange)
    if list_status:
        # E67 cannot establish this filter. An empty match set would be false.
        raise MarketError('CAPABILITY_UNAVAILABLE')
    q = q.strip()
    conditions = dict(operation='catalog-list', catalog_key=catalog_key, q=q,
        exchange=exchange, list_status=list_status, limit=limit, ordering='source-code-exchange-v1')
    with service.read_scope((ETF_CATALOG,), conditions=conditions, cursor=cursor) as scope:
        records = _records(scope, exchange)
        # All admitted source records in this selection are processed before
        # substring search, total and business pagination. No client filtering.
        matches = [item for item in records if not q or q.casefold() in item['source_code'].casefold()
                   or q.casefold() in (item['name'] or '').casefold()]
        offset = 0
        if scope.position is not None:
            if (set(scope.position) != {'offset'} or type(scope.position['offset']) is not int
                    or not 0 < scope.position['offset'] < len(matches)
                    or scope.position['offset'] % limit):
                raise DataStoreError('INVALID_CURSOR')
            offset = scope.position['offset']
        items = [_instrument(item) for item in matches[offset:offset + limit]]
        next_cursor = scope.make_cursor({'offset': offset + limit}) if offset + limit < len(matches) else None
        result = InstrumentPage(catalog_key=catalog_key, items=items, total=len(matches),
            next_cursor=next_cursor, meta=scope.meta(
                scope='当前ETF来源目录；全部已读取资料中的本次搜索及交易所范围',
                limitations=[UNKNOWN_FIELDS, SOURCE_IDENTITY]))
        scope.response(result)
    return result


def instrument(service: MarketReadService, instrument_key: str) -> InstrumentDetail:
    stable = not instrument_key.startswith(CATALOG_KEY_PREFIX)
    identity = IdentityQuery(instrument_keys=(instrument_key,)) if stable else None
    if not stable:
        code = _code(instrument_key.removeprefix(CATALOG_KEY_PREFIX))
    else:
        parse_instrument_key(instrument_key)
    binding = service.bindings[ETF_CATALOG]
    with service.read_scope((ETF_CATALOG,), conditions={'operation': 'catalog-detail',
            'instrument_key': instrument_key}, identity=identity) as scope:
        if stable:
            segments = scope.mapping_segments(instrument_key)
            if len(segments) != 1:
                raise MarketError('IDENTITY_AMBIGUOUS')
            code = segments[0].source_code
        prefix = (binding.representation, code, code)
        objects = _objects(scope, ETF_CATALOG, partitions=(binding.partition(code, code),),
            lower=prefix, upper=(*prefix[:2], code + '\x00'))
        if not objects:
            raise MarketError('IDENTITY_UNKNOWN')
        if len(objects) != 1 or objects[0]['source_code'] != code or objects[0]['asset_type'] != 'fund-etf':
            raise DataStoreError('FILE_INVALID')
        result = InstrumentDetail(instrument=_instrument(objects[0],
            instrument_key=instrument_key if stable else None), meta=scope.meta(
                scope='当前ETF正式基础资料', limitations=[UNKNOWN_FIELDS,
                    Notice(code='CURRENT_IDENTITY_ONLY', message='当前身份资料不证明历史价格可读。') if stable else SOURCE_IDENTITY]))
        scope.response(result)
    return result


def overview(service: MarketReadService, catalog_key=ETF_CATALOG) -> MarketOverview:
    _catalog(catalog_key)
    with service.read_scope((ETF_CATALOG,), conditions={'operation': 'catalog-overview',
            'catalog_key': catalog_key}) as scope:
        items = [_instrument(item) for item in _records(scope)]
        dates = [item.list_date for item in items if item.list_date is not None]
        result = MarketOverview(catalog_key=catalog_key, total_instruments=len(items),
            listed_count=None, exchanges=sorted({item.exchange for item in items if item.exchange}),
            first_list_date=min(dates) if dates else None, latest_list_date=max(dates) if dates else None,
            meta=scope.meta(scope='当前ETF来源目录全部已读取业务记录；上市日期范围不代表行情覆盖',
                limitations=[UNKNOWN_FIELDS, SOURCE_IDENTITY]))
        scope.response(result)
    return result


def calendar(service: MarketReadService, *, exchange: str, start: date, end: date) -> CalendarResult:
    _exchange(exchange)
    if (type(start) is not date or type(end) is not date or start > end
            or start < date(1900, 1, 1) or end > date(2199, 12, 31)):
        raise DataStoreError('INVALID_VALUE')
    if (end - start).days >= 366:
        raise DataStoreError('QUERY_BUDGET_EXCEEDED')
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    binding = service.bindings[EXCHANGE_CALENDAR]
    partitions = tuple(sorted({binding.partition(exchange, day.isoformat()) for day in dates}))
    prefix = (binding.representation, exchange, start.isoformat())
    with service.read_scope((EXCHANGE_CALENDAR,), conditions={'operation': 'calendar',
            'exchange': exchange, 'start': start, 'end': end}) as scope:
        records = _objects(scope, EXCHANGE_CALENDAR, partitions=partitions,
            lower=prefix, upper=(binding.representation, exchange, end.isoformat() + '\x00'),
            filters=((binding.columns['exchange_scope'], '=', exchange),))
        facts = {}
        for item in records:
            day = date.fromisoformat(item['calendar_date'])
            if (day in facts or item['exchange_scope'] != exchange or not start <= day <= end
                    or item['scope_basis'] != 'declared_exchange'):
                raise DataStoreError('FILE_INVALID')
            facts[day] = item['is_open']
        result = CalendarResult(items=[CalendarDay(date=day, exchange=exchange,
            state='unknown' if facts.get(day) is None else 'open' if facts[day] else 'closed') for day in dates],
            meta=scope.meta(scope=f'{exchange} 正式日期事实 {start.isoformat()} 至 {end.isoformat()}（含端点）',
                business_as_of=max(facts) if facts else None, limitations=[
                    Notice(code='CALENDAR_NEIGHBORS_UNKNOWN', message='该正式模型未提供前后交易日依据；未记录日期保持未知。')]))
        scope.response(result)
    return result
