"""Typed provider performance observations preserve unknown formulas and windows."""
from app.data_foundation.market_activity import invalid,number,text

PERIODS=('week','month','tmonth','hyear','year','twoyear','tyear','fyear','now','nowyear')
HISTORY_FIELDS=('donchian_channel','rsi_pct','track_index_pe_ttm_five_year_percentile')


def count(value):
    if value is None:return None
    if type(value) is not int or value<0:invalid('基金绩效排名不是明确非负整数。')
    return value


def performance_body(source,raw):
    from app.data_foundation.record_adapters import source_date
    code=text(source.subject,required=True)
    scope=raw.get('collection_scope')
    if not isinstance(scope,dict) or scope.get('thscode')!=code:
        invalid('基金绩效采集主体与固定来源不一致。')
    rows=raw.get('item')
    if not isinstance(rows,list):invalid('基金绩效缺少完整来源列表。')
    body=dict(source_code=code,asset_type='fund',calculation_formula=None,comparable_units=None,
        effective_period_boundaries=None,complete_history=None)
    quality=dict(calculation_formula='PROVIDER_FORMULA_UNVERIFIED',comparable_units='PROVIDER_UNITS_UNVERIFIED',
        effective_period_boundaries='PROVIDER_PERIOD_CODES_ONLY',complete_history='OBSERVED_SOURCE_SCOPE_ONLY')
    if source.dataset=='fund_performance_history':
        if raw.get('coverage')!='observed_rows_only':invalid('基金历史指标缺少明确观察覆盖声明。')
        points=[]
        for row in rows:
            if not isinstance(row,dict):invalid('基金历史指标成员不是字段对象。')
            try:day=source_date(row.get('date_ms'))
            except (ValueError,OverflowError,OSError):invalid('基金历史指标日期无效。')
            point=dict(trading_date=day,reported_fields=[f for f in HISTORY_FIELDS if f in row])
            for field in HISTORY_FIELDS:point['reported_'+field]=number(row.get(field),signed=True)
            points.append(point)
        if len({p['trading_date'] for p in points})!=len(points):invalid('基金历史指标日期重复，未静默合并。')
        points.sort(key=lambda p:p['trading_date'])
        body.update(points=points,coverage='observed_rows_only')
        return body,quality
    if len(rows)!=1 or not isinstance(rows[0],dict):invalid('基金区间绩效须有一个明确观察对象。')
    row=rows[0]
    if row.get('thscode',code)!=code:invalid('基金绩效成员主体不一致。')
    stamp=raw.get('timestamp')
    if stamp is not None and (type(stamp) is not int or stamp<0):invalid('基金绩效报告时间戳不是明确非负毫秒值。')
    body.update(reported_ticker=text(row.get('ticker')),reported_timestamp_ms=stamp,timestamp_semantics=None,periods=[])
    quality['timestamp_semantics']='PROVIDER_TIMESTAMP_UNVERIFIED'
    if stamp is None:quality['reported_timestamp_ms']='SOURCE_TIMESTAMP_MISSING'
    for period in PERIODS:
        out=dict(provider_period=period)
        if source.dataset=='fund_returns':
            mappings={'reported_return':'return_'+period,'reported_peer_average':'peer_average_'+period,
                      'reported_rank':'rank_'+period,'reported_rank_total':'rank_total_'+period}
        else:mappings={'reported_drawdown':period}
        out['reported_fields']=[target for target,field in mappings.items() if field in row]
        for target,field in mappings.items():
            out[target]=count(row.get(field)) if target in ('reported_rank','reported_rank_total') else number(row.get(field),signed=True)
        body['periods'].append(out)
    return body,quality
