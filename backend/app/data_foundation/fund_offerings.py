"""Complete offering sets with exact source-reported subscription instants."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data_foundation.canonical import FoundationError


def reported_instant(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError('Exact integer epoch milliseconds required')
    number = Decimal(value)
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError('Invalid epoch milliseconds')
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(number))


def offering_body(source, raw):
    key, rows = source.subject, raw.get('members')
    if key not in ('active', 'upcoming'):
        raise FoundationError('IDENTITY_UNRESOLVED', '募集列表缺少已验证的来源筛选分类。')
    if not isinstance(rows, list):
        raise FoundationError('SOURCE_SCHEMA_INVALID', '募集列表缺少明确的完整成员集合。')
    members = []
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Non-object offering member')
            code, ticker = row.get('thscode'), row.get('ticker')
            if not isinstance(code, str) or not code.strip() or '.' not in code:
                raise ValueError('Missing source code')
            if ticker is not None and (not isinstance(ticker, str) or not ticker.strip()):
                raise ValueError('Invalid ticker text')
            members.append(dict(source_code=code, ticker=ticker,
                reported_subscription_start=reported_instant(row.get('subscription_start_ms')),
                reported_subscription_end=reported_instant(row.get('subscription_end_ms'))))
    except (ValueError, OverflowError):
        raise FoundationError('CORE_VALUE_INVALID', '募集列表存在无效主体或时间编码，整组隔离，未丢弃成员或截断日内时间。') from None
    body = dict(collection_key=key, selection_basis='provider_subscription_filter',
        members=sorted(members, key=lambda row: row['source_code']), current_subscription_eligibility=None)
    return body, dict(current_subscription_eligibility='CURRENT_ELIGIBILITY_UNVERIFIED')
