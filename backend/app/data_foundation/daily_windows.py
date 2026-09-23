"""Whole THS price observations keep adjustment bases and dates separate."""
from decimal import InvalidOperation
from app.data_foundation.canonical import FoundationError

BASES = {'stock_daily': ('a-share', 'none'), 'etf_daily': ('fund-etf', 'forward'),
         'index_daily': ('a-share-index', 'not_applicable')}


def daily_window_body(source, raw):
    from app.data_foundation.record_adapters import source_date, daily_decimal
    code = source.subject
    if not isinstance(code, str) or not code.strip() or '.' not in code:
        raise FoundationError('IDENTITY_UNRESOLVED', '日线窗口缺少明确的来源标的代码。')
    asset, adjustment = BASES[source.dataset]
    rows = raw.get('item')
    if raw.get('adjust') != adjustment or raw.get('coverage') != 'observed_rows_only':
        raise FoundationError('SOURCE_SCHEMA_INVALID', '日线调整口径或观察范围不符合已验证来源契约。')
    if not isinstance(rows, list) or not rows:
        raise FoundationError('SOURCE_SCHEMA_INVALID', '日线窗口缺少明确的非空完整序列，未补造零值行情。')
    if raw.get('thscode') not in (None, code):
        raise FoundationError('IDENTITY_CONFLICT', '日线容器标的与固定来源不一致。')
    envelopes = raw.get('provider_envelopes', [])
    if not isinstance(envelopes, list):
        raise FoundationError('SOURCE_SCHEMA_INVALID', '日线来源响应容器类型无效。')
    for envelope in envelopes:
        if not isinstance(envelope, dict) or envelope.get('thscode') not in (None, code) or envelope.get('interval') not in (None, '1d'):
            raise FoundationError('IDENTITY_CONFLICT', '日线响应标的或频率与固定来源不一致。')
        if envelope.get('adjust') not in (None, adjustment):
            raise FoundationError('SOURCE_SCHEMA_INVALID', '日线响应存在冲突调整口径，未拼接不同复权基准。')
    points = []
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Non-object price point')
            if row.get('thscode') not in (None, code):
                raise FoundationError('IDENTITY_CONFLICT', '日线成员标的与固定来源不一致，整组隔离。')
            if row.get('interval') not in (None, '1d') or any(row.get(field) not in (None, adjustment) for field in ('adjust', 'adjusted')):
                raise FoundationError('SOURCE_SCHEMA_INVALID', '日线成员频率或调整口径冲突，整组隔离。')
            point = dict(trade_date=source_date(row.get('date_ms')))
            for field in ('open', 'high', 'low', 'close'):
                point['reported_' + field] = daily_decimal(row.get(field + '_price'), precision=64, scale=32, positive=True)
            for field in ('volume', 'turnover'):
                value = row.get(field)
                point['reported_' + field] = None if value is None else daily_decimal(value, precision=64, scale=32)
            points.append(point)
        points.sort(key=lambda p: p['trade_date'])
        if len({p['trade_date'] for p in points}) != len(points):
            raise ValueError('Duplicate business date')
        requested = {target: None if raw.get(native) is None else source_date(raw[native]) for native, target in
            [('requested_start', 'declared_request_start'), ('requested_end', 'declared_request_end')]}
    except (ValueError, InvalidOperation, OverflowError, OSError):
        raise FoundationError('CORE_VALUE_INVALID', '日线窗口存在无效日期、重复日期或非精确数值，整组隔离。') from None
    # The stored series may merge several acquisitions or bulk imports. Bounds
    # come from every materialized point, not the most recent request envelope;
    # neither bounds nor a ten-year request prove missing dates were covered.
    body = dict(source_code=code, asset_type=asset, reported_adjustment=adjustment, coverage='observed_rows_only',
        points=points, sequence_start=points[0]['trade_date'], sequence_end=points[-1]['trade_date'], **requested,
        currency=None, volume_unit=None, turnover_unit=None, adjustment_anchor=None, adjustment_formula=None)
    quality = dict(currency='CURRENCY_UNVERIFIED', volume_unit='VOLUME_UNIT_UNVERIFIED', turnover_unit='TURNOVER_UNIT_UNVERIFIED',
        adjustment_anchor='ADJUSTMENT_ANCHOR_UNVERIFIED', adjustment_formula='ADJUSTMENT_FORMULA_UNVERIFIED')
    quality.update({field:'MISSING' for field,value in requested.items() if value is None})
    return body, quality
