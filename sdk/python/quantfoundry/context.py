"""Immutable materialized DTOs over the single Rust account and callback lease."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import MappingProxyType, SimpleNamespace
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo
from .data import _error, _json
from .numeric import price_input

_ACTIVE: ContextVar[tuple[Context, Any] | None] = ContextVar('qf_strategy_callback', default=None)

def _binding(context=None):
    active = _ACTIVE.get()
    if active is None:
        raise _error('CAPABILITY_UNAVAILABLE', '策略 API 只能在有效回调中调用', 'strategy_callback')
    owner, bridge = active
    if context is not None and context is not owner:
        raise _error('INVALID_CONTRACT', '上下文不属于当前运行', 'strategy_callback')
    bridge.check()
    return owner, bridge

def _request(operation, body=None):
    _, bridge = _binding()
    encoded, payload = bridge.request(operation, _json(body))
    if payload:
        raise _error('INVALID_CONTRACT', '策略结果包含未声明的数据缓冲')
    return json.loads(encoded)

def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value

@dataclass(frozen=True, slots=True)
class Position:
    security: str
    quantity: int
    sellable: int
    frozen: int
    average_cost: Decimal
    market_value: Decimal | None
    cost_basis_total: Decimal

@dataclass(frozen=True, slots=True)
class Portfolio:
    cash: Decimal
    available_cash: Decimal
    frozen_cash: Decimal
    receivables: Decimal
    total_value: Decimal | None
    positions: Mapping[str, Position]

@dataclass(frozen=True, slots=True)
class MarketOrder:
    pass

@dataclass(frozen=True, slots=True, init=False)
class LimitOrder:
    price: Decimal
    def __init__(self, price):
        object.__setattr__(self, 'price', price_input(price))

@dataclass(frozen=True, slots=True)
class OrderResult:
    accepted: bool
    order_id: str | None
    reason_code: str | None
    message: str
    unchanged: bool
    requested_quantity: int | None
    effective_quantity: int | None

@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    security: str
    side: Literal['buy', 'sell']
    quantity: int
    filled_quantity: int
    status: str
    submitted_ns: int
    limit_price: Decimal | None
    tif: Literal['day', 'gtc']
    effective_session: str
    reason_code: str | None
    message: str

@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    order_id: str
    security: str
    side: Literal['buy', 'sell']
    quantity: int
    price: Decimal
    fee: Decimal
    execution_time_ns: int

@dataclass(frozen=True, slots=True)
class TradePage:
    items: Sequence[Trade]
    next_cursor: str | None

@dataclass(frozen=True, slots=True)
class BarData:
    time_ns: int
    securities: Sequence[str]

def _order(row):
    return Order(**{**{key:row[key] for key in Order.__dataclass_fields__}, 'submitted_ns': int(row['submitted_ns']),
                    'limit_price': None if row['limit_price'] is None else Decimal(row['limit_price'])})

def _trade(row):
    return Trade(**{**row, 'execution_time_ns': int(row['execution_time_ns']),
                    'price': Decimal(row['price']), 'fee': Decimal(row['fee'])})

def _portfolio(row):
    positions = {}
    for security, source in row['positions'].items():
        positions[security] = Position(**{**source, 'average_cost': Decimal(source['average_cost']),
            'cost_basis_total': Decimal(source['cost_basis_total']),
            'market_value': None if source['market_value'] is None else Decimal(source['market_value'])})
    return Portfolio(**{**row, **{key: Decimal(row[key]) for key in ('cash','available_cash','frozen_cash','receivables')},
        'total_value': None if row['total_value'] is None else Decimal(row['total_value']),
        'positions': MappingProxyType(positions)})

class Context:
    __slots__ = ('_params', '_state', '_now_ns', '_timezone')
    def __init__(self, params, exchange_timezone):
        self._params, self._state = _freeze(params), SimpleNamespace()
        self._now_ns, self._timezone = 0, ZoneInfo(exchange_timezone)

    @property
    def params(self) -> Mapping[str, Any]:
        return self._params
    @property
    def state(self) -> Any:
        return self._state
    @state.setter
    def state(self, value: Any) -> None:
        _binding(self)
        self._state = value
    @property
    def now_ns(self) -> int:
        _binding(self)
        return self._now_ns
    @property
    def current_dt(self) -> datetime:
        # Human display can truncate sub-microsecond precision; now_ns cannot.
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=self.now_ns // 1000)).astimezone(self._timezone)
    @property
    def portfolio(self) -> Portfolio:
        _binding(self)
        return _portfolio(_request('portfolio'))
