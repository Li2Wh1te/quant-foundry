"""Preserve complete manager performance windows with provider-defined periods."""
from app.data_store.adapters.market_activity import invalid,number,text

FIELDS=('manager_return_pct','peer_return_pct','benchmark_return_pct')
PERIODS=('month','tmonth','year','nowyear','now')


def performance_body(source,raw):
    from app.data_store.adapters.record_adapters import source_date
    try:
        manager,period=source.subject.rsplit('.',1)
        manager=text(manager,required=True)
        if period not in PERIODS:raise ValueError('Unknown period')
    except (ValueError,AttributeError):invalid('经理业绩固定主体缺少明确经理和来源期间。')
    scope=raw.get('collection_scope')
    if not isinstance(scope,dict) or scope.get('manager_id')!=manager or scope.get('range')!=period:
        invalid('经理业绩采集主体或期间与固定来源不一致。')
    rows=raw.get('item')
    if not isinstance(rows,list):invalid('经理业绩缺少完整来源窗口，未将缺项视为空。')
    points=[]
    for row in rows:
        if not isinstance(row,dict):invalid('经理业绩点位不是字段对象。')
        try:day=source_date(row.get('date_ms'))
        except (ValueError,OverflowError,OSError):invalid('经理业绩业务日期无效。')
        point=dict(trading_date=day,reported_fields=[field for field in FIELDS if field in row])
        for field in FIELDS:point['reported_'+field]=number(row.get(field),signed=True)
        points.append(point)
    if len({p['trading_date'] for p in points})!=len(points):invalid('经理业绩窗口存在重复日期，整组保留待修复。')
    points.sort(key=lambda p:p['trading_date'])
    stamp=raw.get('timestamp')
    if stamp is not None and (type(stamp) is not int or stamp<0):invalid('经理业绩报告时间戳无效。')
    # Provider fields named "return_pct" can contain index levels. Retain the
    # exact source values; never derive excess return or infer a common unit.
    body=dict(manager_id=manager,provider_period=period,points=points,reported_timestamp_ms=stamp,
        coverage_basis='source_observation_window',calculation_formula=None,comparable_units=None,
        benchmark_identity=None,excess_return=None,effective_period_boundaries=None,complete_history=None,timestamp_semantics=None)
    quality=dict(calculation_formula='PROVIDER_FORMULA_UNVERIFIED',comparable_units='PROVIDER_UNITS_UNVERIFIED',
        benchmark_identity='BENCHMARK_IDENTITY_UNVERIFIED',excess_return='INCOMPARABLE_REPORTED_SERIES',
        effective_period_boundaries='PROVIDER_PERIOD_CODES_ONLY',complete_history='SOURCE_WINDOW_ONLY',
        timestamp_semantics='PROVIDER_TIMESTAMP_UNVERIFIED')
    if stamp is None:quality['reported_timestamp_ms']='SOURCE_TIMESTAMP_MISSING'
    return body,quality
