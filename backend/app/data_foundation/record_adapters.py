"""Explicit adapters from fixed source evidence to typed reference/calendar rows."""
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.data_foundation.canonical import FoundationError, normalized
from app.data_foundation.record_schemas import validate_body, schema_for

SOURCE_DATASETS = {
    ('tonghuashun', 'tickers'): 'instrument.reference',
    ('tonghuashun', 'fund_company'): 'fund.company',
    ('tonghuashun', 'fund_manager'): 'fund.manager',
    ('tonghuashun', 'fund_profile'): 'fund.profile',
    ('tonghuashun', 'fund_nav'): 'fund.nav_snapshot',
    ('tonghuashun', 'hot_list'): 'market.popularity_snapshot',
    ('tonghuashun', 'skyrocket'): 'market.rising_popularity_snapshot',
    ('tonghuashun', 'hot_history'): 'market.popularity_history_snapshot',
    ('tonghuashun', 'fund_quota_summary'): 'fund.quota_summary_snapshot',
    ('tonghuashun', 'fund_quota_list'): 'fund.quota_list_snapshot',
    ('tonghuashun', 'fund_offerings'): 'fund.offering_snapshot',
    ('tonghuashun', 'fund_manager_experience'): 'fund.manager_experience',
    ('tonghuashun', 'calendar'): 'market.calendar',
    ('tonghuashun', 'index_catalog'): 'index.category_snapshot',
    ('tonghuashun', 'index_constituents'): 'index.constituent_snapshot',
    ('tushare', 'etf_directory'): 'instrument.reference',
    ('tushare', 'exchange_calendar'): 'market.calendar',
    ('tushare', 'etf_adjustment_factors'): 'market.adjustment_factor',
    ('tushare', 'etf_daily'): 'market.fund_daily',
}


def dataset_for(source):
    try:
        return SOURCE_DATASETS[(source.source, source.dataset)]
    except KeyError:
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '该来源的类型化领域适配器尚未安装。') from None


def rows_for(source, content):
    if source.representation == 'local_table_baseline':
        rows = content
    else:
        if not isinstance(content, dict):
            raise FoundationError('SOURCE_SCHEMA_INVALID', '固定来源缺少完整对象容器。')
        rows = content.get('item')
    if not isinstance(rows, list):
        raise FoundationError('SOURCE_SCHEMA_INVALID', '固定来源缺少明确的记录列表，未将缺项当作空结果。')
    if source.source == 'tonghuashun' and source.dataset == 'fund_manager_experience' and len(rows) != 1:
        raise FoundationError('SOURCE_SCHEMA_INVALID', '经理经历须有一个明确的任职集合容器，未猜测缺失或重复容器。')
    if source.source == 'tonghuashun' and source.dataset == 'fund_nav':
        # Keep the full observation atomic. A malformed point must not silently
        # disappear, and a new rolling window must not inherit older points.
        if not isinstance(content, dict):
            raise FoundationError('SOURCE_SCHEMA_INVALID', '基金净值必须保留完整来源窗口容器。')
        if content.get('thscode') not in (None, source.subject):
            raise FoundationError('IDENTITY_CONFLICT', '基金净值容器主体与固定来源主体不一致。')
        return [{'points': rows, 'coverage': content.get('coverage')}]
    if source.source == 'tonghuashun' and source.dataset in ('hot_list', 'skyrocket', 'hot_history'):
        return [dict(content)]
    if source.source == 'tonghuashun' and source.dataset in ('fund_quota_summary', 'fund_quota_list'):
        return [dict(content)]
    if source.source == 'tonghuashun' and source.dataset == 'fund_offerings':
        if not isinstance(content, dict):
            raise FoundationError('SOURCE_SCHEMA_INVALID', '募集列表须保留完整来源容器。')
        scope = content.get('collection_scope')
        if scope is not None and (not isinstance(scope, dict) or scope.get('subscribe') != source.subject):
            raise FoundationError('IDENTITY_CONFLICT', '募集列表筛选分类与固定来源主体不一致。')
        return [{'members': rows}]
    if source.source == 'tonghuashun' and source.dataset in ('index_catalog', 'index_constituents'):
        # Preserve the entire fixed set as one atomic candidate, including an
        # explicitly empty set. A corrupt child quarantines the collection;
        # publishing only its valid siblings would invent membership removals.
        return [{'members': rows}]
    return rows


def source_date(value):
    """Interpret the deployed source date encoding, never a public timestamp."""
    if isinstance(value, bool):
        raise ValueError('Boolean is not a date')
    if isinstance(value, (int, Decimal)):
        if Decimal(value) != Decimal(value).to_integral_value():
            raise ValueError('Fractional source timestamp')
        decoded = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(value))).astimezone(ZoneInfo('Asia/Shanghai'))
        if (decoded.hour, decoded.minute, decoded.second, decoded.microsecond) != (0, 0, 0, 0):
            raise ValueError('Timestamp is not a Shanghai date boundary')
        return decoded.date()
    if isinstance(value, str):
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, '%Y%m%d').date()
        return date.fromisoformat(value)
    raise ValueError('Unsupported date representation')


def daily_decimal(value, *, precision, scale, positive=False, signed=False):
    """Preserve captured NUMERIC values exactly, rejecting lossy coercion.

    Canonical digit checks do not depend on the active Decimal context
    and do not round a value into the published database precision contract.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('Exact decimal value required')
    number = Decimal(value)
    if not number.is_finite() or (positive and number <= 0) or (not signed and number < 0):
        raise ValueError('Decimal value outside admitted range')
    if number and (number.adjusted() >= precision-scale or number.adjusted() < -scale):
        raise ValueError('Decimal value exceeds persisted precision')
    encoded = normalized(number) if number else '0'
    whole, _, fraction = encoded.lstrip('-').partition('.')
    if len(whole) > precision-scale or len(fraction) > scale:
        raise ValueError('Decimal value exceeds persisted precision')
    return encoded


def convert(source, raw):
    """Return one canonical record and explicit unavailable-field reasons.

    Identity comes only from named identity fields, never row position or a
    guessed stock-code suffix. Container-level failure isolation belongs to the
    caller; optional missing/invalid attributes do not turn an identity into 0.
    """
    dataset = dataset_for(source)
    if not isinstance(raw, dict):
        raise FoundationError('SOURCE_SCHEMA_INVALID', '来源记录不是字段对象。')
    quality = {}
    def text(name, target=None, required=False):
        field = target or name
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value
        if required:
            raise FoundationError('IDENTITY_UNRESOLVED', '记录缺少明确的来源主体标识。')
        quality[field] = 'MISSING' if value is None else 'INVALID_TEXT'
        return None
    def day(name, target):
        value = raw.get(name)
        try:
            return source_date(value) if value is not None else None
        except (ValueError, OverflowError, OSError):
            quality[target] = 'INVALID_DATE'
            return None
        finally:
            if value is None:
                quality[target] = 'MISSING'
    if source.source == 'tonghuashun' and source.dataset in ('hot_list', 'skyrocket', 'hot_history'):
        from app.data_foundation.popularity import popularity_body
        body, quality = popularity_body(source, raw)
        key, kind = f'{source.dataset}:{source.subject}', 'stock_rank_list'
    elif source.source == 'tonghuashun' and source.dataset in ('fund_quota_summary', 'fund_quota_list'):
        from app.data_foundation.fund_quotas import quota_body
        body, quality = quota_body(source, raw)
        key, kind = source.subject, 'qdii_summary_category' if source.dataset == 'fund_quota_summary' else 'qdii_list_category'
    elif dataset == 'fund.offering_snapshot':
        from app.data_foundation.fund_offerings import offering_body
        body, quality = offering_body(source, raw)
        key, kind = source.subject, 'fund_offering_filter'
    elif dataset == 'fund.nav_snapshot':
        from app.data_foundation.fund_nav import nav_body
        body, quality = nav_body(source, raw)
        key, kind = source.subject, 'fund_share'
    elif dataset == 'fund.manager_experience':
        from app.data_foundation.manager_experience import experience_body
        body, quality = experience_body(source, raw)
        key, kind = source.subject, 'fund_manager'
    elif dataset in ('index.category_snapshot', 'index.constituent_snapshot'):
        key = source.subject
        if not isinstance(key, str) or not key.strip():
            raise FoundationError('IDENTITY_UNRESOLVED', '指数集合缺少固定来源主体。')
        category = dataset == 'index.category_snapshot'
        if category and key not in ('cn_concept', 'region', 'tszs', 'industry'):
            raise FoundationError('IDENTITY_UNRESOLVED', '指数分类标签不在已验证的来源范围内。')
        members = raw.get('members')
        if not isinstance(members, list):
            raise FoundationError('SOURCE_SCHEMA_INVALID', '指数集合必须包含明确的成员列表。')
        converted = []
        for member in members:
            if not isinstance(member, dict):
                raise FoundationError('SOURCE_SCHEMA_INVALID', '指数集合包含非对象成员，整组隔离。')
            code = member.get('thscode')
            if not isinstance(code, str) or not code.strip() or '.' not in code:
                raise FoundationError('IDENTITY_UNRESOLVED', '指数集合成员缺少来源代码，整组隔离。')
            # Optional source text may be absent, but a present malformed value
            # must not be silently dropped from a formally published member.
            optional = {}
            for field in ('name', 'ticker'):
                value = member.get(field)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise FoundationError('SOURCE_SCHEMA_INVALID', '指数集合成员文本类型无效，整组隔离。')
                optional[field] = value
            converted.append(dict(source_code=code, **optional))
        body = dict(collection_key=key, collection_kind='category' if category else 'index',
            membership_basis='provider_reported_snapshot', members=sorted(converted, key=lambda row: row['source_code']))
        kind = 'index_category' if category else 'asset:a-share-index'
    elif dataset == 'instrument.reference':
        tushare = source.source == 'tushare'
        key = text('ts_code' if tushare else 'thscode', 'source_code', True)
        asset_type = 'fund-etf' if tushare else text('asset_type', required=True)
        body = dict(source_code=key, asset_type=asset_type,
            name=text('csname' if tushare else 'name', 'name'), exchange=text('exchange'),
            currency=None if tushare else text('currency'), listing_date=day('list_date', 'listing_date'),
            end_date=day('end_date', 'end_date'))
        if tushare:
            quality['currency'] = 'MISSING'
        elif source.subject != asset_type:
            raise FoundationError('IDENTITY_CONFLICT', '目录资产分类与固定来源范围不一致。')
        kind = 'asset:' + asset_type
    elif dataset == 'fund.company':
        key = text('company_id', required=True)
        count = raw.get('fund_count')
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            quality['fund_count'] = 'MISSING' if count is None else 'INVALID_COUNT'
            count = None
        body = dict(company_id=key, name=text('company_name', 'name'), company_type=text('company_type'),
            established_date=day('established_date_ms', 'established_date'), fund_count=count)
        quality['scale'] = 'UNIT_UNVERIFIED'
        kind = 'fund_company'
    elif dataset == 'fund.manager':
        key = text('manager_id', required=True)
        body = dict(manager_id=key, name=text('manager_name', 'name'), company_id=text('company_id'),
            company_name=text('company_name'), degree=text('degree'), resume=text('resume'))
        quality.update(annual_return_pct='UNIT_UNVERIFIED', maximum_return_pct='UNIT_UNVERIFIED')
        kind = 'fund_manager'
    elif dataset == 'fund.profile':
        key = text('thscode', 'source_code', True)
        body = dict(source_code=key, name=text('fund_name', 'name'), company_id=text('company_id'),
            manager_name=text('manager_name'), management_name=text('mgmt_name', 'management_name'),
            established_date=day('estab_date', 'established_date'))
        quality.update(unit_nav='UNIT_UNVERIFIED', fund_scale='UNIT_UNVERIFIED')
        kind = 'fund_share'
    elif dataset == 'market.fund_daily':
        key = text('ts_code', 'source_code', True)
        if raw.get('source') != 'tushare':
            raise FoundationError('IDENTITY_CONFLICT', '日线记录来源与固定来源命名空间不一致。')
        body = dict(source_code=key, trade_date=source_date(raw.get('trade_date')),
            price_unit='yuan', price_basis='provider_reported')
        for field in ('open', 'high', 'low', 'close'):
            try:
                body[field] = daily_decimal(raw.get(field), precision=20, scale=6, positive=True)
            except (ValueError, InvalidOperation):
                raise FoundationError('CORE_VALUE_INVALID', '日线核心价格缺失、无效或超出精度，未补造价格。') from None
        for source_field, target, precision, scale, positive, signed in (
                ('vol', 'volume_lots', 24, 4, False, False),
                ('amount', 'turnover_thousand_yuan', 24, 4, False, False),
                ('pre_close', 'previous_close', 20, 6, True, False),
                ('change', 'change_yuan', 20, 6, False, True),
                ('pct_chg', 'change_percent', 20, 6, False, True)):
            value = raw.get(source_field)
            try:
                body[target] = daily_decimal(value, precision=precision, scale=scale, positive=positive, signed=signed)
            except (ValueError, InvalidOperation):
                body[target] = None
                quality[target] = 'MISSING' if value is None else 'INVALID_DECIMAL'
        kind = 'asset:fund-etf'
    elif dataset == 'market.adjustment_factor':
        key = text('ts_code', 'source_code', True)
        factor = raw.get('adj_factor')
        # Reject binary floats and booleans rather than silently losing source
        # precision. Captured PostgreSQL NUMERIC values are Decimal or strings.
        if isinstance(factor, bool) or not isinstance(factor, (str, int, Decimal)):
            raise FoundationError('CORE_VALUE_INVALID', '复权因子缺少精确十进制表示。')
        try:
            number = Decimal(factor)
        except InvalidOperation:
            raise FoundationError('CORE_VALUE_INVALID', '复权因子不是有效十进制值。') from None
        if not number.is_finite() or number <= 0:
            raise FoundationError('CORE_VALUE_INVALID', '复权因子必须为有限正数。')
        body = dict(source_code=key, trade_date=source_date(raw.get('trade_date')),
            factor=normalized(number), factor_basis='tushare_fund_adj', anchor_date=None)
        quality['anchor_date'] = 'ADJUSTMENT_ANCHOR_UNVERIFIED'
        kind = 'asset:fund-etf'
    else:
        if source.source == 'tushare':
            key = text('exchange', required=True)
            business_date = source_date(raw.get('calendar_date'))
            opened = raw.get('is_open')
            if not isinstance(opened, bool):
                raise FoundationError('CORE_VALUE_INVALID', '交易日期缺少明确的布尔开闭市状态。')
            basis = 'declared_exchange'
        else:
            # N0-03 proves the reported date set and deployed scheduling use,
            # but does not establish exchange-specific open/closed semantics.
            key = source.subject
            business_date = source_date(raw.get('date_ms') if raw.get('date_ms') is not None else raw.get('date'))
            if raw.get('date_ms') is not None and raw.get('date') is not None and source_date(raw['date']) != business_date:
                raise FoundationError('CORE_VALUE_INVALID', '交易日期两种编码不一致，未选择其中一种覆盖冲突。')
            opened, basis = None, 'provider_reported_dates'
            quality['is_open'] = 'EXCHANGE_SESSION_UNVERIFIED'
            quality['unlisted_dates'] = 'UNKNOWN_NOT_CLOSED'
        body = dict(exchange_scope=key, calendar_date=business_date, is_open=opened, scope_basis=basis)
        kind = 'calendar'
    if source.source == 'tonghuashun' and source.dataset in ('fund_profile', 'fund_company', 'fund_manager') and key != source.subject:
        raise FoundationError('IDENTITY_CONFLICT', '来源容器主体与记录主体不一致，未跨主体发布。')
    canonical = validate_body(dataset, body)
    date_field = schema_for(dataset).date_field
    business_date = date.fromisoformat(canonical[date_field]) if date_field else None
    return dict(dataset=dataset, subject_kind=kind, subject_key=key, business_date=business_date,
        body=canonical, field_quality=quality)
