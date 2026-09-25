"""Materialize the two deployed NAV categories without guessed financial units."""
from decimal import InvalidOperation

from app.data_store.adapters.canonical import NativeInputError


def nav_body(source, raw):
    # Import at call time because adapters dispatch into this domain module.
    from app.data_store.adapters.record_adapters import daily_decimal, source_date
    key = source.subject
    if not isinstance(key, str) or not key.strip():
        raise NativeInputError('IDENTITY_UNRESOLVED', '基金净值窗口缺少明确的固定来源主体。')
    rows = raw.get('points')
    if raw.get('coverage') != 'provider_rolling_window' or not isinstance(rows, list) or not rows:
        raise NativeInputError('SOURCE_SCHEMA_INVALID', '基金净值缺少明确的滚动窗口或非空完整序列，整组隔离。')
    points = []
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Non-object NAV point')
            if row.get('thscode') not in (None, key):
                raise NativeInputError('IDENTITY_CONFLICT', '基金净值成员主体与固定来源主体不一致，整组隔离。')
            # This is a bounded exact-decimal transport contract, not a supplier
            # precision claim. No rounding or multiplication changes the value.
            point = {'nav_date': source_date(row.get('nav_date'))}
            for native, target in (('unit_nav', 'reported_unit_nav'), ('adj_nav', 'reported_adjusted_nav')):
                value = row.get(native)
                point[target] = None if value is None else daily_decimal(value, precision=64, scale=32)
            if point['reported_unit_nav'] is None and point['reported_adjusted_nav'] is None:
                raise ValueError('Missing both NAV categories')
            points.append(point)
        points.sort(key=lambda point: point['nav_date'])
        if len({point['nav_date'] for point in points}) != len(points):
            raise ValueError('Duplicate NAV date')
    except (ValueError, InvalidOperation, OverflowError, OSError):
        raise NativeInputError('CORE_VALUE_INVALID', '基金净值存在无效日期、重复日期或非精确数值，整组隔离且不拼接旧序列。') from None
    # Collector observed_start/end describe the latest fetched response, which
    # can be shorter than a merged materialization. Compute stored coverage from
    # all points instead of misrepresenting those response metadata fields.
    body = dict(source_code=key, coverage='provider_rolling_window',
        value_basis='tonghuashun_nav_type_unit_adj', points=points,
        sequence_start=points[0]['nav_date'], sequence_end=points[-1]['nav_date'],
        currency=None, share_basis=None, adjustment_formula=None, cumulative_nav=None)
    quality = dict(currency='UNIT_UNVERIFIED', share_basis='UNIT_UNVERIFIED',
        adjustment_formula='ADJUSTMENT_FORMULA_UNVERIFIED', cumulative_nav='NOT_REPORTED')
    return body, quality
