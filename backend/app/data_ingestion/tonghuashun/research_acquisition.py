"""Typed acquisition units for the approved research data, with bounded paging."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.data_ingestion.tonghuashun.contracts import (
    CollectionError, date_ms, exact_json, items, provider_date, validate_source_dates,
    years_before, windows,
)


@dataclass(frozen=True)
class Unit:
    subject: str
    parameters: dict
    day: date | None = None


def code_rows(rows, requested=None, *, unique=True):
    seen = set()
    for row in rows:
        code = row.get('thscode')
        if not isinstance(code, str) or '.' not in code or (requested is not None and code not in requested):
            raise CollectionError('响应包含请求范围以外或不完整的标的代码。')
        if unique and code in seen:
            raise CollectionError('响应包含重复标的，本次未发布。')
        seen.add(code)
    return seen


def check_date(data, day, *, required=False):
    """Date-scoped endpoints must never silently fall back to another date."""
    found = False
    for key in ('date', 'trade_date'):
        if key in data:
            if data[key] != day.isoformat():
                raise CollectionError('响应日期与请求日期不一致。')
            found = True
    if 'date_ms' in data:
        if provider_date(data['date_ms']) != day:
            raise CollectionError('响应日期与请求日期不一致。')
        found = True
    if required and not found:
        raise CollectionError('响应缺少可核对的查询日期。')


def fetch(acq, spec, unit, params, previous, now):
    if spec.kind == 'm3_pool':
        return pool(acq, spec, unit)
    if spec.kind == 'm3_series':
        return series(acq, spec, unit, params, previous, now)
    if spec.kind == 'm3_news':
        return news(acq, spec, unit, previous, now)
    data = acq.read(spec.interface, unit.parameters)
    if spec.key == 'dragon_tiger':
        validate_source_dates(data)
        check_date(data, unit.day, required=True)
        if data.get('board_type') != unit.parameters['board_type']:
            raise CollectionError('龙虎榜响应口径与请求不一致。')
        for key in ('stock_items', 'hot_money_items'):
            if not isinstance(data.get(key), list) or any(not isinstance(r, dict) for r in data[key]):
                raise CollectionError('龙虎榜缺少合法榜单容器。')
        # Retain the native containers, including multiple holding periods per
        # stock; a wrapper keeps version pagination lossless rather than flattening.
        acq.fetched_count += len(data['stock_items']) + len(data['hot_money_items'])
        return {'item': [{'board_type': data['board_type'], 'stock_items': data['stock_items'],
            'hot_money_items': data['hot_money_items']}], 'provider_metadata': {
            k: v for k, v in data.items() if k not in ('stock_items', 'hot_money_items')},
            'requested_start': unit.day.isoformat(), 'requested_end': unit.day.isoformat()}
    rows = items(data, allow_empty=spec.allow_empty)
    if unit.day:
        check_date(data, unit.day, required=spec.key in ('auction_benchmark', 'hot_history'))
        data = {**data, 'requested_start': unit.day.isoformat(), 'requested_end': unit.day.isoformat()}
    if spec.key in ('anomaly_stock', 'anomaly_list', 'hot_history', 'auction_benchmark'):
        code_rows(rows, [unit.subject] if spec.key == 'anomaly_stock' else None, unique=False)
    if spec.key == 'fund_drawdowns':
        if code_rows(rows, [unit.subject]) != {unit.subject}:
            raise CollectionError('基金回撤未返回请求标的。')
    if spec.kind == 'm3_offerings':
        # Offering lists can repeat an identical subscription record. Collapse
        # only exact duplicates; differing windows for one code still fail the
        # identity check below instead of silently choosing a conflicting row.
        unique_rows = {exact_json(row): row for row in rows}
        if len(unique_rows) != len(rows):
            data = {**data, 'item': list(unique_rows.values()),
                    'identical_duplicates_removed': len(rows) - len(unique_rows)}
            rows = data['item']
    if spec.kind in ('m3_period', 'm3_offerings'):
        code_rows(rows)
    if spec.kind in ('m3_snapshot', 'm3_manager'):
        identity = 'manager_id' if spec.kind == 'm3_manager' else 'thscode'
        expected = unit.parameters[identity]
        if any(identity in row and row[identity] != expected for row in rows):
            raise CollectionError('基金资料响应身份与请求不一致。')
    if spec.kind == 'm3_quota':
        if any(row.get('name') != unit.subject for row in rows):
            raise CollectionError('QDII 返回了其他分类。')
        if spec.key == 'fund_quota_list':
            for row in rows:
                if not isinstance(row.get('sub_tab'), list):
                    raise CollectionError('QDII 列表缺少子分类。')
                for tab in row['sub_tab']:
                    if not isinstance(tab, dict) or not isinstance(tab.get('fund_list'), list):
                        raise CollectionError('QDII 子分类缺少基金列表。')
                    if any(not isinstance(fund, dict) for fund in tab['fund_list']):
                        raise CollectionError('QDII 基金记录格式不正确。')
                    code_rows(tab['fund_list'])
        data = {**data, 'category_scope': 'explicit_categories_only'}
    if spec.key == 'limit_ladder':
        window = data.get('window')
        if not isinstance(window, dict) or not isinstance(window.get('date_list'), list):
            raise CollectionError('连板天梯缺少窗口信息。')
        dates = window['date_list']
        if len(dates) != len(set(dates)) or len(dates) > 30:
            raise CollectionError('连板窗口包含重复日期或超过30个交易日。')
        if any(row.get('date') not in dates or not isinstance(row.get('boards'), dict) for row in rows):
            raise CollectionError('连板记录不符合窗口定义。')
        data = {**data, 'coverage': 'provider_rolling_window'}
    return data


def pool(acq, spec, unit):
    rows, seen, expected, envelopes = [], set(), None, []
    for page in range(1, 1001):
        data = acq.read(spec.interface, {**unit.parameters, 'page': page, 'size': 200})
        part = items(data, allow_empty=True)
        pagination = data.get('pagination')
        if not isinstance(pagination, dict):
            raise CollectionError('股票池缺少分页元数据。')
        total = pagination.get('total')
        if type(total) is not int or not 0 <= total <= 200000:
            raise CollectionError('股票池分页总量非法。')
        if expected is not None and total != expected:
            raise CollectionError('股票池分页期间总量发生变化，未发布不完整版本。')
        expected = total
        if pagination.get('page') != page or pagination.get('size') != 200 or len(part) > 200:
            raise CollectionError('股票池分页与请求不一致。')
        check_date(data, unit.day)
        codes = code_rows(part)
        if seen & codes:
            raise CollectionError('股票池跨页出现重复代码。')
        seen.update(codes)
        rows.extend(part)
        envelopes.append({k: v for k, v in data.items() if k != 'item'})
        if len(rows) == total:
            return {'item': rows, 'provider_envelopes': envelopes,
                'requested_start': unit.day.isoformat(), 'requested_end': unit.day.isoformat()}
        if not part or len(rows) > total:
            raise CollectionError('股票池分页提前结束或超过总量。')
    raise CollectionError('股票池分页超过上限。')


def series(acq, spec, unit, params, previous, now):
    end = params.end_date or (now.date() if now.hour >= (23 if spec.key == 'fund_performance_history' else 20) else now.date()-timedelta(days=1))
    earliest = years_before(now.date(), spec.years)
    start = params.start_date or (max(earliest, end - timedelta(days=30))
        if previous and params.mode == 'incremental' else earliest)
    if spec.key == 'rank_trend' and start < earliest:
        raise CollectionError('个股排名走势仅支持近一年范围。')
    old = {r['date_ms']: r for r in (previous or {}).get('item', [])}
    metadata, received = [], 0
    for a, b in windows(start, end, 1):
        query = ({'thscode': unit.subject, 'start_date': a.isoformat(), 'end_date': b.isoformat()}
            if spec.key == 'rank_trend' else {'thscode': unit.subject, 'start': date_ms(a), 'end': date_ms(b)})
        data = acq.read(spec.interface, query)
        part = items(data, allow_empty=True)
        received += len(part)
        seen = set()
        for row in part:
            day = provider_date(row.get('date_ms'))
            if not a <= day <= b or day in seen:
                raise CollectionError('历史指标包含重复日期或范围外数据。')
            if spec.key == 'rank_trend':
                if row.get('thscode') != unit.subject or row.get('date') != day.isoformat():
                    raise CollectionError('排名走势标的或日期不一致。')
            seen.add(day)
            old[row['date_ms']] = row
        metadata.append({k: v for k, v in data.items() if k != 'item'})
    if not received and not spec.allow_empty:
        raise CollectionError('历史业绩范围未返回数据，尚不能确认覆盖。')
    return {'item': [old[k] for k in sorted(old)], 'provider_envelopes': metadata,
        'requested_start': min(start.isoformat(), (previous or {}).get('requested_start', start.isoformat())),
        'requested_end': end.isoformat(), 'coverage': 'observed_rows_only'}


def news(acq, spec, unit, previous, now):
    """Bound the initial window; later catch-up resumes opaque cursors safely.

    Restart from the head after a completed sweep. Pinned/old articles do not
    terminate paging early. A page budget is a partial sweep, not lost history.
    """
    known = {r['id']: r for r in (previous or {}).get('item', [])}
    cursor = (previous or {}).get('resume_cursor')
    cutoff = (date.fromisoformat(previous['window_start']) if cursor and previous else
              now.date() - timedelta(days=90) if not previous else
              date.fromisoformat(previous.get('last_sweep_date', now.date().isoformat())) - timedelta(days=7))
    seen_cursors, seen_ids, collected, envelopes = set(), {}, 0, []
    limited = False
    for _ in range(25):
        query = {'thscode': unit.subject, 'limit': 20}
        if cursor:
            query['offset'] = cursor
        data = acq.read(spec.interface, query)
        part = items(data, allow_empty=True)
        if type(data.get('has_more')) is not bool or len(part) > 20:
            raise CollectionError('基金资讯分页字段非法。')
        for row in part:
            ident = row.get('id')
            if not isinstance(ident, str) or not ident:
                raise CollectionError('资讯分页缺少唯一ID。')
            encoded = exact_json(row)
            if ident in seen_ids:
                if seen_ids[ident] != encoded:
                    raise CollectionError('资讯分页期间同一记录发生变化。')
                continue  # Repeated pinned articles do not consume new rows.
            seen_ids[ident] = encoded
            day = provider_date(row.get('publish_time_ms'))
            if day >= cutoff:
                known[ident] = row
                collected += 1
        envelopes.append({k: v for k, v in data.items() if k != 'item'})
        if not data['has_more']:
            break
        next_cursor = data.get('offset')
        if not part or not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors or next_cursor == cursor:
            raise CollectionError('资讯分页游标未前进。')
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        # The bounded initial bootstrap has a declared 90-day / 500-record
        # scope. Ongoing sweeps persist a cursor instead of losing a busy day.
    else:
        limited = True
    bootstrap = not previous
    partial = limited and not bootstrap
    return {'item': [known[k] for k in sorted(known)], 'provider_envelopes': envelopes,
        'window_start': cutoff.isoformat(), 'requested_start': cutoff.isoformat(),
        'requested_end': now.date().isoformat(), 'resume_cursor': cursor if partial else None,
        'last_sweep_date': (previous or {}).get('last_sweep_date', now.date().isoformat()) if partial else now.date().isoformat(),
        'coverage': 'initial_90_days_or_500_records' if bootstrap else 'cursor_window',
        'scope_truncated': bootstrap and limited,
        'failed_requests': [{'reason': 'page_budget', 'message': '资讯尚有后续页，下批继续。'}] if partial else []}
