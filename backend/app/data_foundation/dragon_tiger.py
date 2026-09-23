"""Preserve complete reported leaderboard hierarchies without inferred identities."""
from app.data_foundation.market_activity import invalid, member, number, text

SIGNED = ('change','net_rate','net_value','org_net_rate','org_net_value',
          'hot_money_net_rate','hot_money_net_value','hot_money_item_net_rate','hot_money_item_net_value')
UNSIGNED = ('amount','buy_value','sell_value')
COUNTERS = ('hot_rank','org_buy_num','org_sell_num')


def integer(value, *, minimum=0, required=False):
    if value is None and not required:
        return None
    if type(value) is not int or value < minimum:
        invalid('龙虎榜来源计数或期间不是明确有效整数。')
    return value


def stocks(rows):
    if not isinstance(rows,list):
        invalid('龙虎榜股票明细不是明确完整列表。')
    result=[]
    for index,row in enumerate(rows):
        out=member(row)
        # Neither stock code nor code/period uniquely identifies source rows.
        # Preserve each occurrence; never sum, drop, or reconcile disagreements.
        out.update(source_order=index,reported_range_days=integer(row.get('range_days'),minimum=1,required=True),
                   reported_limit_reason=text(row.get('limit_reason')))
        for field in SIGNED+UNSIGNED:
            out['reported_'+field]=number(row.get(field),signed=field in SIGNED)
        for field in COUNTERS:
            out['reported_'+field]=integer(row.get(field))
        concepts=row.get('concept_list')
        if not isinstance(concepts,list) or any(not isinstance(c,dict) for c in concepts):
            invalid('龙虎榜概念标签不是明确来源列表。')
        out['reported_concepts']=[text(c.get('name'),required=True) for c in concepts]
        result.append(out)
    return result


def dragon_tiger_body(source,raw):
    from app.data_foundation.record_adapters import source_date
    if not isinstance(raw,dict):
        invalid('龙虎榜缺少完整观察容器。')
    try:
        day_text,board=source.subject.rsplit('.',1)
        day=source_date(day_text)
        if board not in ('all','org','hot_money'):
            raise ValueError('Unknown board')
    except (ValueError,AttributeError):
        invalid('龙虎榜主体缺少明确日期和榜单类别。')
    scope=raw.get('collection_scope');meta=raw.get('provider_metadata')
    if not isinstance(scope,dict) or not isinstance(meta,dict):
        invalid('龙虎榜缺少明确采集范围或来源元数据。')
    rows=raw.get('item')
    if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):
        invalid('龙虎榜必须有一个完整榜单容器，未将缺项视为空榜单。')
    container=rows[0]
    if any(obj.get('board_type')!=board for obj in (scope,meta,container)):
        invalid('龙虎榜榜单类别与固定主体不一致。')
    try:
        for value in (scope.get('date'),meta.get('trade_date'),meta.get('timestamp'),raw.get('requested_start'),raw.get('requested_end')):
            if value is not None and source_date(value)!=day:
                raise ValueError('Conflicting date')
    except (ValueError,OverflowError,OSError):
        invalid('龙虎榜业务日期与采集范围不一致。')
    stock_items=stocks(container.get('stock_items'))
    hot=container.get('hot_money_items')
    if not isinstance(hot,list):
        invalid('龙虎榜游资明细不是明确列表。')
    hot_items=[]
    for index,row in enumerate(hot):
        if not isinstance(row,dict):
            invalid('龙虎榜游资明细不是字段对象。')
        hot_items.append(dict(source_order=index,reported_name=text(row.get('name'),required=True),
            reported_buying=number(row.get('buying'),signed=True),stocks=stocks(row.get('rows'))))
    body=dict(collection_key=source.subject,trading_date=day,board_type=board,stocks=stock_items,hot_money=hot_items,
              reported_count=integer(meta.get('count')),reported_stock_count=integer(meta.get('stock_count')),
              complete_market_coverage=None,currency=None,amount_units=None,ratio_formula=None,
              participant_identity=None,duplicate_resolution=None,period_start=None)
    quality=dict(complete_market_coverage='SOURCE_LIST_ONLY',currency='CURRENCY_UNVERIFIED',
                 amount_units='AMOUNT_UNITS_UNVERIFIED',ratio_formula='PROVIDER_RATIO_FORMULA_UNVERIFIED',
                 participant_identity='SOURCE_LABEL_NOT_VERIFIED_IDENTITY',duplicate_resolution='SOURCE_OCCURRENCES_PRESERVED',
                 period_start='REPORTED_DAY_COUNT_WITHOUT_CALENDAR_ANCHOR')
    return body,quality
