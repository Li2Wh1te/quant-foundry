"""Explicit adapters from fixed source evidence to typed reference/calendar rows."""
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_foundation.canonical import FoundationError
from app.data_foundation.record_schemas import validate_body

SOURCE_DATASETS = {
    ('tonghuashun', 'tickers'): 'instrument.reference',
    ('tonghuashun', 'fund_company'): 'fund.company',
    ('tonghuashun', 'fund_manager'): 'fund.manager',
    ('tonghuashun', 'fund_profile'): 'fund.profile',
    ('tonghuashun', 'calendar'): 'market.calendar',
    ('tushare', 'etf_directory'): 'instrument.reference',
    ('tushare', 'exchange_calendar'): 'market.calendar',
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
    if dataset == 'instrument.reference':
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
    business_date = date.fromisoformat(canonical['calendar_date']) if dataset == 'market.calendar' else None
    return dict(dataset=dataset, subject_kind=kind, subject_key=key, business_date=business_date,
        body=canonical, field_quality=quality)
