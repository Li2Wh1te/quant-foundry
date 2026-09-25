"""Typed source-reported quotes and valuations preserve observation semantics."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from app.data_store.adapters.canonical import NativeInputError

ASSETS = {'stock_quote': 'a-share', 'etf_quote': 'fund-etf',
          'index_quote': 'a-share-index', 'stock_valuation': 'a-share'}
QUOTE_FIELDS = ('open_price', 'high_price', 'low_price', 'last_price', 'prev_price',
                'price_change', 'price_change_ratio_pct', 'price_amplitude_ratio_pct',
                'turnover_ratio_pct', 'volume', 'turnover')
VALUATION_FIELDS = ('pe_ttm', 'pe_mrq', 'pb_mrq', 'ps_ttm', 'pcf_ttm')


def snapshot_body(source, raw):
    from app.data_store.adapters.record_adapters import daily_decimal
    code = source.subject
    rows = raw.get('item')
    if not isinstance(code, str) or not code.strip() or '.' not in code:
        raise NativeInputError('IDENTITY_UNRESOLVED', '行情估值观察缺少明确来源标的代码。')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise NativeInputError('SOURCE_SCHEMA_INVALID', '行情估值观察须恰有一个明确主体记录，空项或重复项整组隔离。')
    row = rows[0]
    if row.get('thscode') != code:
        raise NativeInputError('IDENTITY_CONFLICT', '行情估值记录与固定来源主体不一致。')
    scope = raw.get('collection_scope')
    if scope is not None and (not isinstance(scope, dict) or any(
            scope[key] != code for key in ('thscode', 'thscodes') if key in scope)):
        raise NativeInputError('IDENTITY_CONFLICT', '行情估值采集范围与固定来源主体不一致。')
    stamp = raw.get('timestamp')
    reported_at = None
    if stamp is not None:
        try:
            if isinstance(stamp, bool) or not isinstance(stamp, (int, Decimal)) or Decimal(stamp) != Decimal(stamp).to_integral_value():
                raise ValueError('An exact integer millisecond timestamp is required')
            reported_at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(stamp))
        except (ValueError, OverflowError, InvalidOperation):
            raise NativeInputError('SOURCE_SCHEMA_INVALID', '行情估值来源报告时间不是有效的整数毫秒时间。') from None
    valuation = source.dataset == 'stock_valuation'
    body = dict(source_code=code, asset_type=ASSETS[source.dataset], reported_at=reported_at,
                timestamp_semantics=None, effective_business_date=None)
    quality = dict(timestamp_semantics='PROVIDER_TIMESTAMP_MEANING_UNVERIFIED',
                   effective_business_date='BUSINESS_DATE_UNVERIFIED')
    if stamp is None:
        quality['reported_at'] = 'SOURCE_NULL' if 'timestamp' in raw else 'SOURCE_FIELD_ABSENT'
    for native, target in [('ticker', 'reported_ticker'), ('name', 'reported_name')]:
        value = row.get(native)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise NativeInputError('SOURCE_SCHEMA_INVALID', '行情估值名称或展示代码不是有效文本。')
        body[target] = value
        if value is None:
            quality[target] = 'SOURCE_NULL' if native in row else 'SOURCE_FIELD_ABSENT'
    # Keep the provider timestamp as a reported observation attribute. It may
    # denote response generation, not exchange event time or a financial period;
    # it must never become a business key, PIT public time, or trading date.
    fields = VALUATION_FIELDS if valuation else QUOTE_FIELDS
    try:
        for field in fields:
            target = 'reported_' + field
            value = row.get(field)
            body[target] = None if value is None else daily_decimal(value, precision=64, scale=32,
                signed=valuation or field in ('price_change', 'price_change_ratio_pct'))
            if value is None:
                quality[target] = 'SOURCE_NULL' if field in row else 'SOURCE_FIELD_ABSENT'
    except (ValueError, InvalidOperation):
        raise NativeInputError('CORE_VALUE_INVALID', '行情估值报告量值无效，未将非数值或精度溢出转换为零。') from None
    if any(body['reported_' + field] is not None for field in fields):
        body['reported_value_status'] = 'values_reported'
    else:
        expected = fields if valuation or source.dataset == 'etf_quote' else tuple(
            field for field in fields if field not in ('price_amplitude_ratio_pct', 'turnover_ratio_pct'))
        if not all(field in row and row[field] is None for field in expected):
            raise NativeInputError('CORE_VALUE_INVALID', '行情估值量值字段缺失，无法确认为来源明确返回的空值观察。')
        # An explicit all-null response is a real missing-value observation.
        # It replaces a stale quote without providing any numeric capability;
        # absent fields and malformed values never qualify for this state.
        body['reported_value_status'] = 'explicit_null_report'
    if valuation:
        body.update(valuation_formula=None, financial_period=None)
        quality.update(valuation_formula='VALUATION_FORMULA_UNVERIFIED', financial_period='FINANCIAL_PERIOD_UNVERIFIED')
    else:
        body.update(currency=None, volume_unit=None, turnover_unit=None, executable_quote=None, trading_status=None)
        quality.update(currency='CURRENCY_UNVERIFIED', volume_unit='VOLUME_UNIT_UNVERIFIED',
            turnover_unit='TURNOVER_UNIT_UNVERIFIED', executable_quote='HISTORICAL_OBSERVATION_NOT_EXECUTABLE',
            trading_status='TRADING_STATUS_UNVERIFIED')
    # Zero reports remain explicit provider values, not inferred suspensions or
    # executable prices. Signed valuation ratios and changes retain their sign.
    return body, quality
