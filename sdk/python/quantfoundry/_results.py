"""Bounded business result pages. Reading never downloads or caches all history."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
import json
import math
from types import MappingProxyType
from typing import Any, Mapping
from .data import _error

KINDS = frozenset(('summary', 'equity', 'orders', 'trades', 'positions', 'records', 'logs'))
MAX_PAGE_ITEMS = 10_000
MAX_MESSAGE_BYTES = 1024 * 1024
_MONEY = frozenset(('equity','cash','available_cash','frozen_cash','receivables','total_value','cost_basis_total',
    'average_cost','market_value','price','limit_price','fee','initial_equity','terminal_equity','buy_notional',
    'sell_notional','total_notional','fill_fees','trading_fees','realized_pnl','dividend_income','dividend_tax','risk_free_rate','benchmark_value'))

def validate_result_request(kind: str, limit: int, cursor: str | None) -> None:
    if not isinstance(kind,str) or kind not in KINDS or isinstance(limit, bool) or not isinstance(limit,int) or not 1 <= limit <= MAX_PAGE_ITEMS:
        raise _error('INVALID_CONTRACT', '结果类型或分页大小无效')
    if cursor is not None and (not isinstance(cursor,str) or not cursor or len(cursor.encode('utf-8')) > 4096):
        raise _error('INVALID_CONTRACT', '结果游标无效')

def _load(text: str) -> dict[str, Any]:
    if not isinstance(text,str) or len(text.encode('utf-8')) > MAX_MESSAGE_BYTES:
        raise _error('RESOURCE_LIMIT', '结果控制消息超过预算')
    try:
        def constant(value):
            raise ValueError('nonfinite statistic')
        def finite(value):
            result = float(value)
            if not math.isfinite(result): raise ValueError("nonfinite statistic")
            return result
        value = json.loads(text,parse_constant=constant,parse_float=finite)
        if not isinstance(value,dict): raise ValueError('result object required')
        return value
    except (ValueError,RecursionError) as error:
        raise _error('INVALID_CONTRACT', '结果消息无效') from error

def _freeze(value, key=None, parent=None):
    if value is None: return None
    if key in ('time_ns','execution_time_ns','submitted_ns','price_time_ns','result_sequence') and not isinstance(value,(dict,list)):
        if not isinstance(value,str): raise _error('INVALID_CONTRACT','精确时间或序号必须为字符串')
        return int(value)
    if key in _MONEY and parent != 'counts' and not isinstance(value,(dict,list)):
        if not isinstance(value,str): raise _error('INVALID_CONTRACT','精确金额必须为十进制字符串')
        result = Decimal(value)
        if not result.is_finite(): raise _error('INVALID_CONTRACT','精确金额必须有限')
        return result
    if isinstance(value,dict):
        if value.get('kind') == 'decimal':
            exact = Decimal(value['value'])
            if not exact.is_finite(): raise _error('INVALID_CONTRACT','记录金额必须有限')
            value = {**value, 'value': exact}
        return MappingProxyType({name:_freeze(item,name,key) for name,item in value.items()})
    if isinstance(value,list): return tuple(_freeze(item) for item in value)
    return value

@dataclass(frozen=True)
class ResultPage:
    items: tuple[Mapping[str, Any], ...]
    next_cursor: str | None
    partial: bool
    notices: Mapping[str, Any]
    schema_id: str

class RunResults:
    """Own a native result handle; each call returns one bounded page.

    Temporary worker results release on close/drop. D13 supplies the authorized
    retained-history reader; this object exposes no file path or credentials.
    """
    def __init__(self, native):
        self._native = native
        self._closed = False
    def _check(self):
        if self._closed: raise _error('CANCELLED', '结果读取已关闭')
    def summary_json(self) -> str:
        self._check()
        text = self._native.summary_json()
        _load(text)
        return text
    def page_json(self, kind: str, after: str | None = None, limit: int = 1000) -> str:
        self._check()
        validate_result_request(kind,limit,after)
        if kind == 'summary': return self.summary_json()
        text = self._native.page_json(kind,after,limit)
        value = _load(text)
        if not isinstance(value.get('items'),list) or len(value['items']) > limit:
            raise _error('INVALID_CONTRACT', '结果页超过请求信用')
        validate_result_request(kind,limit,value.get('next_cursor'))
        return text
    def summary(self) -> Mapping[str, Any]:
        return _freeze(_load(self.summary_json()))
    def page(self, *, kind: str = 'equity', limit: int = 100, cursor: str | None = None) -> ResultPage:
        if kind == 'summary': raise _error('INVALID_CONTRACT', '摘要请使用summary')
        value = _load(self.page_json(kind,cursor,limit))
        return ResultPage(tuple(_freeze(item) for item in value['items']),value.get('next_cursor'),
            value['partial'],_freeze(value.get('notices',{})),value['schema_id'])
    def close(self) -> None:
        if not self._closed:
            self._native.close()
            self._closed = True
    def __enter__(self):
        self._check()
        return self
    def __exit__(self, *args): self.close()
