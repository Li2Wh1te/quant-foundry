"""Complete source activity sets retain scope, missing values and reported text."""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from app.data_foundation.canonical import FoundationError

DOMAINS = {'stock_auction':'market.auction_snapshot', 'auction_benchmark':'market.auction_benchmark_snapshot',
    'limit_up':'market.limit_up_snapshot', 'limit_down':'market.limit_down_snapshot',
    'limit_break':'market.limit_break_snapshot', 'anomaly_list':'market.anomaly_snapshot',
    'limit_ladder':'market.limit_ladder_window'}
GROUPS = ('two_board','three_board','four_board','five_board','six_board','seven_over')
AUCTION_NUMBERS = ('auction_amount','auction_pct','auction_price','auction_turnover_pct','auction_unmatched',
    'auction_volume','auction_volume_ratio','auction_yesterday_ratio_pct','float_market_cap','last_price','open_price','pre_close_price')


def invalid(message):
    raise FoundationError('SOURCE_SCHEMA_INVALID', message)


def text(value, *, required=False):
    if value is None and not required:return None
    if not isinstance(value,str) or (required and not value.strip()):invalid('市场活动字段缺少明确的来源文本或代码。')
    return value


def number(value, *, signed=False):
    from app.data_foundation.record_adapters import daily_decimal
    if value is None:return None
    try:return daily_decimal(value,precision=64,scale=32,signed=signed)
    except (ValueError,InvalidOperation):invalid('市场活动报告数值无效，未转换成零或推算其他单位。')


def texts(value):
    if not isinstance(value,list):invalid('市场活动文本集合不是明确列表。')
    return [text(v,required=True) for v in value]


def member(row):
    if not isinstance(row,dict):invalid('市场活动成员不是字段对象，整组隔离。')
    code=text(row.get('thscode'),required=True)
    return dict(source_code=code,reported_ticker=text(row.get('ticker')),reported_name=text(row.get('name')))


def unique_members(rows):
    keys=[row['source_code'] for row in rows]
    if len(keys)!=len(set(keys)):invalid('市场活动集合包含重复来源主体，未静默去重。')
    # Preserve reported list order without pretending it is an economic rank.
    return rows


def business_day(source, raw):
    from app.data_foundation.record_adapters import source_date
    try:
        day=source_date(source.subject)
        for field in ('date','date_ms','requested_start','requested_end'):
            if raw.get(field) is not None and source_date(raw[field])!=day:raise ValueError('Conflicting date')
        scope=raw.get('collection_scope')
        if scope is not None:
            if not isinstance(scope,dict):raise ValueError('Invalid scope')
            for field in ('date','date_ms'):
                if field in scope and source_date(scope[field])!=day:raise ValueError('Conflicting scope date')
        return day
    except (ValueError,OverflowError,OSError):invalid('市场活动业务日期与固定来源或采集范围不一致。')


def activity_body(source, raw):
    rows=raw.get('item')
    if not isinstance(rows,list):invalid('市场活动缺少明确完整列表，未将缺项当作空集合。')
    native=source.dataset;quality={}
    if native in ('anomaly_list','limit_ladder') and (source.subject!='market' or raw.get('collection_scope') not in (None,{})):
        invalid('市场集合观察的固定主体或采集范围不一致。')
    if native=='stock_auction':
        if len(rows)!=1:invalid('竞价终态必须只有一个明确主体。')
        row=rows[0];body=member(row)
        if body['source_code']!=source.subject:invalid('竞价主体与固定来源不一致。')
        scope=raw.get('collection_scope')
        if scope is not None and (not isinstance(scope,dict) or scope.get('thscodes')!=source.subject):invalid('竞价采集范围不一致。')
        if raw.get('auction_phase')!='closed' or raw.get('data_status')!='final':invalid('竞价来源不是已确认的终态观察。')
        body.update(asset_type='a-share',reported_phase='closed',reported_status='final',reported_at=None,
            currency=None,quantity_units=None,auction_formula=None,executable_quote=None,effective_business_date=None)
        quality.update(currency='CURRENCY_UNVERIFIED',quantity_units='QUANTITY_UNITS_UNVERIFIED',
            auction_formula='AUCTION_FORMULA_UNVERIFIED',executable_quote='HISTORICAL_OBSERVATION_NOT_EXECUTABLE',
            effective_business_date='BUSINESS_DATE_UNVERIFIED')
        stamp=raw.get('timestamp')
        if stamp is not None:
            try:
                if isinstance(stamp,bool) or not isinstance(stamp,(int,Decimal)) or Decimal(stamp)!=Decimal(stamp).to_integral_value():raise ValueError('Invalid timestamp')
                body['reported_at']=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(milliseconds=int(stamp))
            except (ValueError,OverflowError,InvalidOperation):invalid('竞价来源报告时间无效。')
        else:quality['reported_at']='SOURCE_TIMESTAMP_MISSING'
        for field in AUCTION_NUMBERS:
            body['reported_'+field]=number(row.get(field),signed=field in ('auction_pct','auction_unmatched'))
            if row.get(field) is None:quality['reported_'+field]='SOURCE_NULL' if field in row else 'SOURCE_FIELD_ABSENT'
        return body,quality
    if native=='anomaly_list':
        members=[]
        for index,row in enumerate(rows):
            if not isinstance(row,dict):invalid('异动叙述不是字段对象。')
            members.append(dict(source_order=index,source_code=text(row.get('thscode'),required=True),
                reported_name=text(row.get('stock_name')),reported_tag=text(row.get('tag_name')),
                reported_keywords=texts(row.get('keyword_list')),reported_analysis=text(row.get('analysis_content'))))
        # A stock may have several independent source narratives. Occurrence is
        # retained; source commentary is never upgraded to a verified event.
        return dict(collection_key=source.subject,members=members,scope_basis='source_reported_narratives_only',
            factual_verification=None,event_timestamps=None,complete_market_coverage=None),dict(
            factual_verification='SOURCE_NARRATIVE_NOT_VERIFIED_FACT',event_timestamps='EVENT_TIMES_UNVERIFIED',
            complete_market_coverage='SOURCE_LIST_ONLY')
    if native=='limit_ladder':
        return ladder_body(source,raw,rows)
    body=dict(collection_key=source.subject,trading_date=business_day(source,raw),
        scope_basis='source_reported_list_only',complete_market_coverage=None)
    quality['complete_market_coverage']='SOURCE_LIST_ONLY'
    members=[]
    for row in rows:
        out=member(row)
        if native=='auction_benchmark':
            out.update(reported_auction_pct=number(row.get('auction_pct'),signed=True),reported_tags=texts(row.get('tags')))
        else:
            numbers={'limit_up':('last_price','max_seal_money','price_change_ratio_pct','seal_money'),
                'limit_down':('last_price','price_change_ratio_pct','turnover_ratio_pct'),
                'limit_break':('last_price','price_change_ratio_pct','turnover','turnover_ratio_pct')}[native]
            for field in numbers:out['reported_'+field]=number(row.get(field),signed=field=='price_change_ratio_pct')
            simple={'limit_up':('continue_day_cnt','continue_day_text','is_new','is_st','limit_up_reason','limit_up_time'),
                'limit_down':('first_limit_time','last_limit_time'),'limit_break':('open_times',)}[native]
            for field in simple:
                value=row.get(field)
                if field.endswith('_time') and value is not None and (not isinstance(value,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value)):
                    invalid('涨跌停来源时间文本不是有效小时分钟，未推算事件时间戳。')
                out['reported_'+field]=value
        members.append(out)
    body['members']=unique_members(members)
    if native=='auction_benchmark':
        body['selection_formula']=None;quality['selection_formula']='BENCHMARK_SELECTION_UNVERIFIED'
    else:
        body.update(currency=None,amount_units=None,exchange_limit_rule=None,event_timestamps=None)
        quality.update(currency='CURRENCY_UNVERIFIED',amount_units='AMOUNT_UNITS_UNVERIFIED',
            exchange_limit_rule='EXCHANGE_RULE_UNVERIFIED',event_timestamps='REPORTED_CLOCK_TEXT_ONLY')
    return body,quality


def ladder_body(source,raw,rows):
    from app.data_foundation.record_adapters import source_date
    window=raw.get('window')
    if raw.get('coverage')!='provider_rolling_window' or not isinstance(window,dict):invalid('连板梯队缺少明确来源窗口声明。')
    caps=window.get('board_caps');declared=window.get('date_list')
    if not isinstance(caps,dict) or set(caps)!=set(GROUPS) or not isinstance(declared,list):invalid('连板梯队类别上限或日期清单不完整。')
    try:declared_days=[source_date(day) for day in declared]
    except (ValueError,OverflowError,OSError):invalid('连板梯队声明日期无效。')
    if type(window.get('length')) is not int or window['length']!=len(declared_days) or len(set(declared_days))!=len(declared_days):invalid('连板梯队声明窗口长度或日期清单冲突。')
    days=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('boards'),dict) or set(row['boards'])!=set(GROUPS):invalid('连板梯队日期或类别结构不完整。')
        try:day=source_date(row.get('date'))
        except (ValueError,OverflowError,OSError):invalid('连板梯队成员日期无效。')
        groups={}
        for group in GROUPS:
            if not isinstance(row['boards'][group],list):invalid('连板梯队成员类别不是明确列表。')
            members=[]
            for value in row['boards'][group]:
                out=member(value)
                out.update(reported_board_num=value.get('board_num'),reported_seal_nextday=value.get('seal_nextday'),
                    reported_sign_level=value.get('sign_level'))
                members.append(out)
            groups[group]=unique_members(members)
        days.append(dict(trading_date=day,reported_groups=groups))
    if sorted(day['trading_date'] for day in days)!=sorted(declared_days):invalid('连板梯队实际日期与声明清单不一致。')
    days.sort(key=lambda day:day['trading_date'])
    return dict(collection_key=source.subject,coverage='provider_rolling_window',days=days,declared_board_caps=caps,
        complete_market_coverage=None,seal_nextday_meaning=None,sign_level_meaning=None),dict(
        complete_market_coverage='PROVIDER_CAPPED_ROLLING_WINDOW',seal_nextday_meaning='PROVIDER_CODE_UNVERIFIED',
        sign_level_meaning='PROVIDER_CODE_UNVERIFIED')
