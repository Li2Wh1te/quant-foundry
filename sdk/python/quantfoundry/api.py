"""Public strategy API. All execution and accounting route to Rust."""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Sequence
from . import _native
from .context import (Context, Position, Portfolio, MarketOrder, LimitOrder, OrderResult,
    Order, Trade, TradePage, BarData, _binding, _request, _order, _trade)
from .data import (CurrentQuote, Tick, Filter, get_price, get_current_data, get_fundamentals,
    get_valuation, get_index_stocks, get_industry, get_instruments, _error, _terminal_guard)
from .numeric import decimal_input, _text

DecimalLike = Decimal | int | str
TimeLike = datetime | str | int
RecordValue = DecimalLike | float
Frequency = Literal['1d','1m','5m','15m','30m','60m','tick']
StrategyError = _native.ContractError

def _schedule(func, cadence, trading_day, time):
    context, _ = _binding()
    if not callable(func) or type(trading_day) is not int or not -32768 <= trading_day <= 32767:
        raise _error('INVALID_CONTRACT', '调度需要可调用函数和有界交易日序号')
    # The host owns callable identities; Rust owns registration and timing.
    from ._worker import _host
    _host().register(func, cadence, trading_day, time)

def run_daily(func: Callable[[Context], None], *, time: str = 'after_close') -> None:
    _schedule(func, 'daily', 1, time)

def run_weekly(func: Callable[[Context], None], *, trading_day: int = 1, time: str = 'after_close') -> None:
    _schedule(func, 'weekly', trading_day, time)

def run_monthly(func: Callable[[Context], None], *, trading_day: int = 1, time: str = 'after_close') -> None:
    _schedule(func, 'monthly', trading_day, time)

def subscribe(securities: Sequence[str], *, events: Literal['bar','tick'] = 'bar') -> None:
    _binding()
    if isinstance(securities, (str, bytes)) or not isinstance(securities, Sequence) or len(securities) > 10000:
        raise _error('RESOURCE_LIMIT', '订阅标的必须是有界序列')
    _request('subscribe', {'securities': list(securities), 'events': events})

def _submit(security, kind, value, style, tif):
    _binding()
    if type(security) is not str or not 0 < len(security.encode()) <= 128 or tif not in ('day', 'gtc'):
        raise _error('INVALID_CONTRACT', '标的或订单有效期无效')
    if style is None or isinstance(style, MarketOrder):
        wire_style = {'kind':'market'}
    elif isinstance(style, LimitOrder):
        wire_style = {'kind':'limit','price':_text(style.price)}
    else:
        raise _error('INVALID_CONTRACT', '订单样式需要 MarketOrder 或 LimitOrder')
    if kind in ('quantity','target_quantity'):
        if type(value) is not int or not -(2**63-1) <= value <= 2**63-1:
            raise _error('NUMERIC_RANGE_UNSUPPORTED', '订单数量需要受支持的整数，bool 不属于数量')
        if kind == 'target_quantity' and value < 0:
            raise _error('INVALID_ORDER', '目标数量必须非负')
        side, number = ('sell' if value < 0 else 'buy'), abs(value)
    else:
        number = decimal_input(value)
        if kind.startswith('target') and (number < 0 or kind == 'target_percent' and number > 1):
            raise _error('INVALID_ORDER', '目标金额或比例超出范围')
        side, number = ('sell' if number < 0 else 'buy'), _text(number.copy_abs())
    return OrderResult(**_request('order', {'security':security, 'side':side,
        'value':{'kind':kind,'value':number}, 'style':wire_style, 'tif':tif}))

def order(security: str, quantity: int, *, style: MarketOrder | LimitOrder | None = None, tif: Literal['day','gtc'] = 'day') -> OrderResult:
    return _submit(security, 'quantity', quantity, style, tif)
def order_value(security: str, value: DecimalLike, *, style: MarketOrder | LimitOrder | None = None, tif: Literal['day','gtc'] = 'day') -> OrderResult:
    return _submit(security, 'value', value, style, tif)
def order_target(security: str, quantity: int, *, style: MarketOrder | LimitOrder | None = None, tif: Literal['day','gtc'] = 'day') -> OrderResult:
    return _submit(security, 'target_quantity', quantity, style, tif)
def order_target_value(security: str, value: DecimalLike, *, style: MarketOrder | LimitOrder | None = None, tif: Literal['day','gtc'] = 'day') -> OrderResult:
    return _submit(security, 'target_value', value, style, tif)
def order_target_percent(security: str, percent: DecimalLike, *, style: MarketOrder | LimitOrder | None = None, tif: Literal['day','gtc'] = 'day') -> OrderResult:
    return _submit(security, 'target_percent', percent, style, tif)

def cancel_order(order_id: str) -> OrderResult:
    return OrderResult(**_request('cancel', order_id))
def get_order(order_id: str) -> Order:
    return _order(_request('get_order', order_id))
def get_open_orders() -> Mapping[str, Order]:
    return MappingProxyType({row['order_id']:_order(row) for row in _request('open_orders')})
def get_trades(*, after: str | None = None, limit: int = 1000) -> TradePage:
    _binding()
    if type(limit) is not int or not 1 <= limit <= 10000 or after is not None and (type(after) is not str or not 0 < len(after.encode()) <= 4096):
        raise _error('RESOURCE_LIMIT', '成交分页数量或游标超过预算')
    page = _request('trades', {'after':after, 'limit':limit})
    return TradePage(tuple(_trade(row) for row in page['items']), page['next_cursor'])

def record(**values: RecordValue) -> None:
    context, _ = _binding()
    rows = {}
    for name, value in values.items():
        if type(value) is bool:
            raise _error('INVALID_CONTRACT', '指标值不能是 bool')
        if isinstance(value, Decimal):
            item = {'kind':'decimal','value':_text(decimal_input(value))}
        elif type(value) is int and -(2**63) <= value < 2**63:
            item = {'kind':'integer','value':value}
        elif type(value) is str:
            if value in ('NaN','Infinity','-Infinity'):
                raise _error('NUMERIC_RANGE_UNSUPPORTED', '指标不能使用 NaN/Infinity')
            item = {'kind':'text','value':value}
        elif type(value) is float and isfinite(value):
            item = {'kind':'statistic','value':value}
        else:
            raise _error('NUMERIC_RANGE_UNSUPPORTED', '指标需要有限标量或有界文本')
        rows[name] = item
    _request('emit', {'kind':'record','record':{'time_ns':str(context.now_ns),'values':rows}})

class _Logger:
    def _write(self, level, message):
        context, _ = _binding()
        if type(message) is not str:
            raise _error('INVALID_CONTRACT', '日志需要文本')
        # Bound conversion before sending. Rust enforces the total run budget.
        truncated = len(message) > 4096
        message = message[:4096]
        _request('emit', {'kind':'log','record':{'time_ns':str(context.now_ns),'level':level,
            'message':message,'truncated':truncated}})
    def info(self, message: str) -> None:
        self._write('info', message)
    def warning(self, message: str) -> None:
        self._write('warning', message)
    def error(self, message: str) -> None:
        self._write('error', message)
log = _Logger()

for _name in ('run_daily','run_weekly','run_monthly','subscribe','order','order_value','order_target',
              'order_target_value','order_target_percent','cancel_order','get_order','get_open_orders','get_trades','record'):
    globals()[_name] = _terminal_guard(globals()[_name])
del _name
