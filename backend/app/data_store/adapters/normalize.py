"""Explicit domain projection onto bounded points and complete business objects."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from pydantic import ValidationError

from ..errors import DataStoreError
from .canonical import NativeInputError
from .contracts import LocalInput, Unit, digest, native_json
from .record_adapters import rows_for, convert, source_date
from .registry import Entry, SERIES, REPORTS, GROUPS

# Actual native key encodings, not inferred timestamp/ID cursors.
POINT_KEYS = {'stock_daily':'date_ms','etf_daily':'date_ms','index_daily':'date_ms',
              'fund_nav':'nav_date','rank_trend':'date_ms','fund_manager_performance':'date_ms',
              'fund_performance_history':'date_ms','fund_news':'id'}
REPORT_KEYS = {'fund_stock_history':'report_key','fund_bond_history':'report_key',
               'stock_indicators':'report','fund_financial_indicators':'end_date_ms',
               'fund_income':'end_date_ms','fund_balance':'end_date_ms',
               'stock_income':'period_end_ms','stock_balance':'period_end_ms','stock_cash_flow':'period_end_ms'}
GROUP_KEYS = {'fund_holdings':'end_date_ms','fund_allocation':'report_date_ms',
              'fund_industry':'report_period','fund_holders':'report_date_ms',
              'fund_top_holders':'report_date_ms','fund_dividends':'ex_dividend_date_ms',
              'stock_actions':'ex_date_ms'}


def object_key(body, keys, *, missing='current'):
    if not keys: return 'current'
    values=[body.get(k) for k in keys]
    if all(v is None for v in values):
        return missing
    result=':'.join('unknown' if v is None else str(v) for v in values)
    return result if len(result.encode())<=240 else 'key:'+digest(values)


def _slices(entry, value):
    """Isolate a true report/day before conversion, not arbitrary physical pages."""
    content=value.content
    if entry.source!='tonghuashun' or not isinstance(content,dict):
        yield value,None,None; return
    key=POINT_KEYS.get(entry.native) or REPORT_KEYS.get(entry.native) or GROUP_KEYS.get(entry.native)
    if key is None or not isinstance(content.get('item'),list):
        yield value,None,None; return
    groups=defaultdict(list)
    for row in content['item']:
        rawkey=row.get(key) if isinstance(row,dict) else None
        if isinstance(rawkey,(dict,list,bool)):
            rawkey=None
        # No source-order comparison ever uses this grouping ordinal.
        groups[str(rawkey) if rawkey is not None else 'unlocated'].append(row)
    if entry.native in ('fund_stock_history','fund_bond_history'):
        directory=content.get('report_directory',{}).get('item')
        if not isinstance(directory,list):
            yield value,None,None; return
        from .periods import report_key
        for r in directory:
            try: groups.setdefault(report_key(r)[0],[])
            except (ValueError,TypeError,AttributeError):
                yield value,None,None; return
    if not groups:
        # An explicit empty series/window is a source status, not a fabricated
        # no-events claim. A complete collection still passes its own adapter.
        return
    for rawkey,rows in groups.items():
        data={**content,'item':rows}
        if entry.native in ('fund_stock_history','fund_bond_history'):
            from .periods import report_key
            data['report_directory']={'item':[d for d in directory if report_key(d)[0]==rawkey]}
            failures=content.get('failed_requests',[])
            if not isinstance(failures,list):
                yield replace(value,content=data),rawkey,key; continue
            def affects(failure):
                params=failure.get('parameters',{}) if isinstance(failure,dict) else {}
                if not {'end_date','report_type'} <= set(params): return True
                return f"{params['end_date']}:{params['report_type']}"==rawkey
            data['failed_requests']=[f for f in failures if affects(f)]
        yield replace(value,content=data),rawkey,key


def _subject(entry,source):
    if entry.native in ('dragon_tiger','hot_list','skyrocket','hot_history','auction_benchmark',
                         'limit_up','limit_down','limit_break','anomaly_list','limit_ladder'):
        return entry.native+':'+source.subject
    return source.subject


def _failed_identity(entry,part,rawkey,rawfield):
    mode=entry.projection[0]
    if mode in ('snapshot','reference') and entry.native not in ('calendar','tickers'):
        if not entry.projection[-1]:return 'current'
    if entry.source=='tushare' and isinstance(part.content,dict):
        row=part.content
        if entry.native=='corporate_action_facts' and row.get('logical_fact_key'):
            return 'event:'+digest(row['logical_fact_key'])
        return object_key(row,entry.projection[-1],missing='unlocated')
    if entry.native in ('stock_income','stock_balance','stock_cash_flow','fund_income','fund_balance','fund_financial_indicators'):
        rows=part.content.get('item',[]) if isinstance(part.content,dict) else []
        if rows and isinstance(rows[0],dict):
            row=rows[0]
            try:
                if entry.native.startswith('stock_'):
                    return object_key({'end':source_date(row['period_end_ms']).isoformat(),'period':row.get('period')},('end','period'))
                return object_key({'end':source_date(row['end_date_ms']).isoformat(),
                                   'start':None if row.get('start_date_ms') is None else source_date(row['start_date_ms']).isoformat()},('end','start'))
            except (ValueError,KeyError,TypeError):return 'unlocated'
    return _failure_key(part,rawkey,rawfield)

def _unit(entry, source, key, body, *, subject=None, rawkey=None, failure=None, quality=None):
    order,token=source.order_ns,source.token
    if rawkey is not None and source.row_basis:
        order,token=source.row_basis.get(str(rawkey),(order,token))
    if rawkey is not None and str(rawkey) in source.unconfirmed_keys:
        failure='SOURCE_CONFIRMATION_UNPROVEN';order=source.order_ns;token=source.token
    rows=() if failure else entry.layout.encode(body)
    if rows and quality:
        encoded=native_json(quality)
        if len(encoded.encode())>32768:
            raise NativeInputError('SOURCE_BUDGET_EXCEEDED','当前字段限制超过有界说明预算。')
        rows=tuple({**r,'quality_json':encoded if r['member_key']=='root' else None} for r in rows)
    return Unit(subject or _subject(entry,source),source.representation_key,key,source.group,order,token,
                rows,True,failure,source.limitations)


def _failure_key(source, rawkey, rawfield):
    if rawkey is None or rawkey=='unlocated': return 'unlocated'
    if rawfield in ('report_key','report','report_period','id'): return str(rawkey)
    try:
        # Native milliseconds are decimal integers; do not cast ISO dates to float.
        return source_date(int(rawkey) if rawkey.lstrip('-').isdigit() and len(rawkey)!=8 else rawkey).isoformat()
    except (ValueError,OverflowError,TypeError): return 'unlocated'


def _table(entry, source):
    raw=source.content
    if not isinstance(raw,dict):
        raise NativeInputError('SOURCE_SCHEMA_INVALID','本地事实行不是对象。')
    if entry.native in ('corporate_action_facts','trading_status_facts'):
        if raw.get('source')!='tushare':
            raise NativeInputError('IDENTITY_CONFLICT','本地事实来源不一致。')
        model=entry.projection[-2]
        body={name:raw[name] for name in model.model_fields if name in raw}
        for name in ('instrument_id',):
            if body.get(name) is not None: body[name]=str(body[name])
        quality=body.get('quality',body.get('quality_status'))
        if quality not in (None,'complete','valid','ready','verified'):
            raise NativeInputError('CORE_VALUE_INVALID','本地事实有未解决质量限制。')
        if entry.native=='corporate_action_facts':
            if not body.get('logical_fact_key') or not body.get('instrument_id'):
                raise NativeInputError('IDENTITY_UNRESOLVED','公司行动缺少真实事件身份。')
            subject=str(body['instrument_id']); key='event:'+digest(body['logical_fact_key'])
        else:
            subject=raw['ts_code']; key=object_key(body,('trade_date','dimension'))
        yield _unit(entry,source,key,body,subject=subject,rawkey=raw.get('logical_fact_key'))
        return
    # The four existing table converters contain explicit units/date/OHLC rules.
    row=convert(source,raw)
    yield _unit(entry,source,object_key(row['body'],entry.projection[-1]),row['body'],subject=row['subject_key'],quality=row['field_quality'])


def _synthetic(entry, source):
    row=source.content
    if not isinstance(row,dict): raise NativeInputError('SOURCE_SCHEMA_INVALID','事件记录缺少显式字段。')
    ns=row.get('event_ns' if entry.native=='tick' else 'start_ns')
    if type(ns) is not int or not -(2**63)<=ns<2**63:
        raise NativeInputError('SOURCE_PRECISION_INVALID','事件时间必须为整数纳秒。')
    if row.get('source_code')!=source.subject or row.get('currency')!='CNY' or row.get('price_basis')!='raw':
        raise NativeInputError('IDENTITY_CONFLICT','合成扩展的明确身份或价格口径不一致。')
    for key in ('price',) if entry.native=='tick' else ('open','high','low','close'):
        value=row.get(key)
        if isinstance(value,(float,bool)) or not isinstance(value,(Decimal,int,str)) or Decimal(value)<=0:
            raise NativeInputError('CORE_VALUE_INVALID','价格必须为精确正数。')
    day=date.fromisoformat(row['trading_date']) if isinstance(row.get('trading_date'),str) else row.get('trading_date')
    if type(day) is not date or not row.get('session') or ':' in row['session']:
        raise NativeInputError('IDENTITY_UNRESOLVED','缺少交易日或会话身份。')
    if entry.native=='tick':
        sequence=row.get('sequence')
        if type(sequence) is not int or not 0<=sequence<2**63 or not row.get('channel') or ':' in row['channel']:
            raise NativeInputError('IDENTITY_UNRESOLVED','逐笔缺少可靠通道/事件序号。')
        key=f"{day}:{row['session']}:{row['channel']}:{sequence:020}"
    else:
        if row.get('frequency') not in ('1m','5m','15m','30m','60m','1h'):
            raise NativeInputError('SOURCE_SCHEMA_INVALID','频率没有明确契约。')
        if not Decimal(row['low'])<=min(Decimal(row['open']),Decimal(row['close']))<=max(Decimal(row['open']),Decimal(row['close']))<=Decimal(row['high']):
            raise NativeInputError('CORE_VALUE_INVALID','OHLC关系错误。')
        key=f"{day}:{row['session']}:{row['frequency']}:{ns+2**63:020}"
    quantity=row.get('quantity' if entry.native=='tick' else 'volume')
    if type(quantity) is not int or not 0<=quantity<2**63:
        raise NativeInputError('CORE_VALUE_INVALID','数量必须为明确非负整数。')
    yield _unit(entry,source,key,row)


def _normalize(entry: Entry, source: LocalInput):
    """Return validated business objects or targeted retryable problems.

    All members are validated before a Unit is yielded. No bad child is dropped
    from a report and no report is assembled from old/new members. A missing
    identity is a scoped error, never an invented occurrence key.
    """
    if not entry.business:
        raise ValueError('Support and import channels must be routed before normalization')
    if (source.source,source.dataset)!=(entry.source,entry.native):
        raise NativeInputError('IDENTITY_CONFLICT','适配器与真实来源不一致。')
    if source.failure:
        yield _unit(entry,source,_failed_identity(entry,source,None,None),{},failure=source.failure); return
    for key in source.withdrawals:
        yield Unit(source.subject,source.representation_key,key,source.group,source.order_ns,
                   source.token,(),withdrawn=True)
    if source.withdrawals and isinstance(source.content,dict) and source.content.get('item')==[]:
        return
    for part,rawkey,rawfield in _slices(entry,source):
        try:
            if isinstance(part.content,dict) and (part.content.get('has_more') is True or
                    part.content.get('next_cursor') not in (None,'') or part.content.get('complete') is False):
                raise NativeInputError('REPORT_INCOMPLETE','物理页尚未拼成完整业务对象。')
            if entry.source=='synthetic':
                yield from _synthetic(entry,part); continue
            if entry.source=='tushare':
                yield from _table(entry,part); continue
            mode,*_,keys=entry.projection
            if entry.native in ('fund_company','fund_manager','fund_profile') and len(part.content.get('item',[]))!=1:
                raise NativeInputError('SOURCE_SCHEMA_INVALID','当前资料必须有一个明确完整对象。')
            records=rows_for(part,part.content)
            for raw in records:
                result=convert(part,raw); body=result['body']
                subject=result['subject_key']
                if mode in ('series','reports','intervals'):
                    path=entry.projection[1]
                    units=body[path]
                    seen=set()
                    for obj in units:
                        key=object_key(obj,keys,missing='unlocated')
                        if key=='unlocated' or key in seen:
                            raise NativeInputError('CORE_VALUE_INVALID','业务键缺失或重复。')
                        seen.add(key)
                    # Full source slice validated first, then all complete units.
                    for obj in units:
                        key=object_key(obj,keys,missing='unlocated')
                        yield _unit(entry,part,key,obj,subject=subject,rawkey=rawkey,quality=result['field_quality'])
                elif mode=='group':
                    path=entry.projection[1]
                    members=body[path]
                    key=object_key(members[0],keys,missing='undated') if members else 'undated'
                    yield _unit(entry,part,key,{**{k:v for k,v in body.items() if k!=path},'members':members},subject=subject,rawkey=rawkey,quality=result['field_quality'])
                else:
                    yield _unit(entry,part,object_key(body,keys),body,subject=subject,rawkey=rawkey,quality=result['field_quality'])
        except (NativeInputError,ValidationError,DataStoreError,ValueError,TypeError,KeyError,OverflowError) as exc:
            code=exc.code if isinstance(exc,(NativeInputError,DataStoreError)) else 'SOURCE_SCHEMA_INVALID'
            yield _unit(entry,part,_failed_identity(entry,part,rawkey,rawfield),{},rawkey=rawkey,failure=code)


def normalize(entry: Entry, source: LocalInput):
    """Duplicate canonical identities within one native input are invalid.

    Reduction is still staged only: nothing is published before all input has
    been enumerated, so a duplicate late in a physical source invalidates its
    exact object, not merely the last member of a partly published report.
    """
    seen=set()
    for unit in _normalize(entry,source):
        if unit.key in seen and not unit.failure:
            yield replace(unit,rows=(),failure='DUPLICATE_BUSINESS_KEY')
        else:
            yield unit
        seen.add(unit.key)
        if len(seen)>1000000:
            raise NativeInputError('SOURCE_BUDGET_EXCEEDED','单个来源的业务键数量超过预算。')
