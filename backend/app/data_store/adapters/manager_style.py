"""Retain reported manager style vectors without inventing industry labels."""
from app.data_store.adapters.market_activity import invalid,number,text


def style_body(source,raw):
    manager=text(source.subject,required=True)
    scope=raw.get('collection_scope')
    if not isinstance(scope,dict) or scope.get('manager_id')!=manager:
        invalid('经理风格采集主体与固定来源不一致。')
    rows=raw.get('item')
    if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):
        invalid('经理风格必须保留一个明确完整观察对象。')
    row=rows[0];preferences=row.get('industry_preferences')
    if not isinstance(preferences,list):invalid('经理风格偏好窗口不是明确列表。')
    periods=[];tags=set()
    for index,entry in enumerate(preferences):
        if not isinstance(entry,dict):invalid('经理风格偏好成员不是字段对象。')
        tag=text(entry.get('report_tag'),required=True)
        if tag in tags:invalid('经理风格报告标签重复，未合并冲突观察。')
        tags.add(tag);vector=entry.get('percent')
        if not isinstance(vector,list) or len(vector)!=7:
            invalid('经理风格来源向量不是七个明确位置，未猜测缺失类别。')
        # Source position is stable evidence, but no supplied taxonomy binds it
        # to named industries. Preserve zero/null without rescaling to 100%.
        periods.append(dict(source_order=index,reported_period_tag=tag,
            reported_vector=[number(v) for v in vector],reported_total_fund_scale=number(entry.get('total_fund_scale'))))
    stamp=raw.get('timestamp')
    if stamp is not None and (type(stamp) is not int or stamp<0):invalid('经理风格来源时间戳无效。')
    body=dict(manager_id=manager,preferences=periods,reported_timestamp_ms=stamp,
        reported_investment_idea=text(row.get('investment_idea')),
        reported_representative_fund_code=text(row.get('representative_fund_thscode')),
        reported_representative_fund_ticker=text(row.get('representative_fund_ticker')),
        reported_representative_fund_name=text(row.get('representative_fund_name')),
        reported_total_fund_scale=number(row.get('total_fund_scale')),
        industry_taxonomy=None,normalized_industry_allocation=None,scale_currency=None,scale_unit=None,
        period_boundaries=None,timestamp_semantics=None,verified_investment_strategy=None)
    quality=dict(industry_taxonomy='PROVIDER_VECTOR_LABELS_UNVERIFIED',normalized_industry_allocation='PROVIDER_VECTOR_UNITS_UNVERIFIED',
        scale_currency='CURRENCY_UNVERIFIED',scale_unit='SCALE_UNIT_UNVERIFIED',period_boundaries='SOURCE_PERIOD_TAG_ONLY',
        timestamp_semantics='PROVIDER_TIMESTAMP_UNVERIFIED',verified_investment_strategy='SOURCE_NARRATIVE_ONLY')
    for field in ('reported_timestamp_ms','reported_investment_idea','reported_representative_fund_code',
                  'reported_representative_fund_ticker','reported_representative_fund_name','reported_total_fund_scale'):
        if body[field] is None:quality[field]='SOURCE_VALUE_MISSING'
    return body,quality
