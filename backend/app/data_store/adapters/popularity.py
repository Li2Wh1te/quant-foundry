"""Typed complete ranking lists without inventing a heat formula or PIT time."""
from app.data_store.adapters.canonical import NativeInputError


def popularity_body(source, raw):
    # Provider ranks, including ties, are preserved. Sorting only makes the
    # published set deterministic; list position is never used to invent rank.
    from app.data_store.adapters.record_adapters import source_date, daily_decimal
    historical = source.dataset == 'hot_history'
    scope = raw.get('collection_scope')
    if not isinstance(raw.get('item'), list):
        raise NativeInputError('SOURCE_SCHEMA_INVALID', '排行观察缺少明确的完整成员列表。')
    body = dict(collection_key=source.subject, period='historical_day' if historical else source.subject,
        scope_basis='provider_returned_ranking_list', heat_method=None, historical_public_time=None)
    if historical:
        day = source_date(source.subject)
        if raw.get('date') is None or source_date(raw['date']) != day:
            raise NativeInputError('IDENTITY_CONFLICT', '历史排行响应日期与固定来源日期不一致。')
        for field in ('date_ms', 'requested_start', 'requested_end'):
            if raw.get(field) is not None and source_date(raw[field]) != day:
                raise NativeInputError('IDENTITY_CONFLICT', '历史排行容器存在冲突日期，未发布到其他日期。')
        body['ranking_date'] = day
        expected_scope = ('date', day.isoformat())
    else:
        if source.subject not in ('day', 'hour'):
            raise NativeInputError('IDENTITY_UNRESOLVED', '实时排行缺少已验证的日榜或小时榜分类。')
        expected_scope = ('period', source.subject)
    if scope is not None and (not isinstance(scope, dict) or scope.get(expected_scope[0]) != expected_scope[1]):
        raise NativeInputError('IDENTITY_CONFLICT', '排行采集范围与固定来源主体不一致。')
    members = []
    for row in raw['item']:
        if not isinstance(row, dict):
            raise NativeInputError('SOURCE_SCHEMA_INVALID', '排行存在非对象成员，整组隔离。')
        code = row.get('thscode')
        if not isinstance(code, str) or not code.strip() or '.' not in code:
            raise NativeInputError('IDENTITY_UNRESOLVED', '排行成员缺少来源代码，整组隔离。')
        member = dict(source_code=code, reported_rank=row.get('rank'),
            reported_rank_change=row.get('rank_change'), reported_heat=None)
        for original, target in [('name', 'name'), ('ticker', 'ticker'), ('rank_trend', 'reported_rank_trend')]:
            value = row.get(original)
            if value is not None and not isinstance(value, str):
                raise NativeInputError('SOURCE_SCHEMA_INVALID', '排行成员报告文本类型无效，整组隔离。')
            member[target] = value
        if row.get('heat') is not None:
            member['reported_heat'] = daily_decimal(row['heat'], precision=64, scale=32)
        # Strict rank validation precedes sorting so bools and malformed types
        # cannot be accepted accidentally or trigger a mixed-type comparison.
        if type(member['reported_rank']) is not int or member['reported_rank'] < 1:
            raise NativeInputError('CORE_VALUE_INVALID', '排行成员缺少有效的来源名次，未按数组位置补造。')
        members.append(member)
    body['members'] = sorted(members, key=lambda row: (row['reported_rank'], row['source_code']))
    return body, dict(heat_method='HEAT_METHOD_UNVERIFIED', historical_public_time='HISTORICAL_PUBLIC_TIME_UNVERIFIED')
