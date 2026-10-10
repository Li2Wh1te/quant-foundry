# Public strategy contract v1.1. Runtime functions remain unavailable until D05/D10.
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Literal, Mapping, Sequence
from pandas import DataFrame

DecimalLike = Decimal | int | str
TimeLike = datetime | str | int
RecordValue = DecimalLike | float
Frequency = Literal["1d", "1m", "5m", "15m", "30m", "60m", "tick"]

class Position:
    security: str
    quantity: int
    sellable: int
    frozen: int
    average_cost: Decimal
    market_value: Decimal | None

class Portfolio:
    cash: Decimal
    available_cash: Decimal
    frozen_cash: Decimal
    receivables: Decimal
    total_value: Decimal | None
    positions: Mapping[str, Position]

class Context:
    params: Mapping[str, Any]
    state: Any
    portfolio: Portfolio
    current_dt: datetime
    now_ns: int

class MarketOrder: ...
class LimitOrder:
    price: Decimal
    def __init__(self, price: DecimalLike) -> None: ...

class OrderResult:
    accepted: bool
    order_id: str | None
    reason_code: str | None
    message: str
    unchanged: bool
    requested_quantity: int | None
    effective_quantity: int | None

class Order:
    order_id: str
    security: str
    side: Literal["buy", "sell"]
    quantity: int
    filled_quantity: int
    status: str
    submitted_ns: int
    limit_price: Decimal | None
    tif: Literal["day", "gtc"]
    effective_session: str
    reason_code: str | None
    message: str

class Trade:
    trade_id: str
    order_id: str
    security: str
    side: Literal["buy", "sell"]
    quantity: int
    price: Decimal
    fee: Decimal
    execution_time_ns: int

class CurrentQuote:
    security: str
    time_ns: int
    last_price: Decimal | None
    price_time_ns: int | None
    is_stale: bool
    halted: bool | None

class Tick(CurrentQuote):
    channel: str
    sequence: int | str | None
    kind: Literal["trade", "quote"]
    bid: Decimal | None
    ask: Decimal | None
    quantity: int | None
    bid_quantity: int | None
    ask_quantity: int | None

class TradePage:
    items: Sequence[Trade]
    next_cursor: str | None

class BarData:
    time_ns: int
    securities: Sequence[str]

class Filter:
    def __init__(self, field: str, op: Literal["eq", "ne", "lt", "le", "gt", "ge", "in"], value: Any) -> None: ...

class StrategyError(Exception):
    code: str
    scope: Mapping[str, Any]

class _Logger:
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
log: _Logger

def run_daily(func: Callable[[Context], None], *, time: str = "after_close") -> None: ...
def run_weekly(func: Callable[[Context], None], *, trading_day: int = 1, time: str = "after_close") -> None: ...
def run_monthly(func: Callable[[Context], None], *, trading_day: int = 1, time: str = "after_close") -> None: ...
def get_price(securities: str | Sequence[str], *, start: TimeLike | None = None, end: TimeLike | None = None, count: int | None = None, frequency: Frequency | None = None, fields: Sequence[str] = ("close",), adjustment: Literal["none", "pre", "post"] = "none") -> DataFrame: ...
def get_current_data(securities: Sequence[str] | None = None) -> Mapping[str, CurrentQuote]: ...
def get_fundamentals(securities: Sequence[str], *, fields: Sequence[str], as_of: TimeLike | None = None, filters: Sequence[Filter] = (), order_by: Sequence[str] = (), limit: int = 10000) -> DataFrame: ...
def get_valuation(securities: Sequence[str], *, fields: Sequence[str], as_of: TimeLike | None = None) -> DataFrame: ...
def get_index_stocks(index: str, *, as_of: TimeLike | None = None) -> Sequence[str]: ...
def get_industry(securities: Sequence[str], *, as_of: TimeLike | None = None) -> DataFrame: ...
def get_instruments(securities: Sequence[str] | None = None, *, as_of: TimeLike | None = None) -> DataFrame: ...
def order(security: str, quantity: int, *, style: MarketOrder | LimitOrder | None = None, tif: Literal["day", "gtc"] = "day") -> OrderResult: ...
def order_value(security: str, value: DecimalLike, *, style: MarketOrder | LimitOrder | None = None, tif: Literal["day", "gtc"] = "day") -> OrderResult: ...
def order_target(security: str, quantity: int, *, style: MarketOrder | LimitOrder | None = None, tif: Literal["day", "gtc"] = "day") -> OrderResult: ...
def order_target_value(security: str, value: DecimalLike, *, style: MarketOrder | LimitOrder | None = None, tif: Literal["day", "gtc"] = "day") -> OrderResult: ...
def order_target_percent(security: str, percent: DecimalLike, *, style: MarketOrder | LimitOrder | None = None, tif: Literal["day", "gtc"] = "day") -> OrderResult: ...
def cancel_order(order_id: str) -> OrderResult: ...
def get_order(order_id: str) -> Order: ...
def get_open_orders() -> Mapping[str, Order]: ...
def get_trades(*, after: str | None = None, limit: int = 1000) -> TradePage: ...
def record(**values: RecordValue) -> None: ...
def initialize(context: Context) -> None: ...
def before_trading_start(context: Context) -> None: ...
def after_trading_end(context: Context) -> None: ...
def handle_data(context: Context, data: BarData) -> None: ...
def handle_tick(context: Context, tick: Tick) -> None: ...
def on_order(context: Context, order: Order) -> None: ...
def on_trade(context: Context, trade: Trade) -> None: ...
def subscribe(securities: Sequence[str], *, events: Literal["bar", "tick"] = "bar") -> None: ...
