"""Small Rust float64 research indicators with explicit value/status outputs.

Pandas indexes survive; NumPy arrays and Decimal prices are accepted explicitly
as analysis inputs. The original input is never changed or used for execution.
"""
from __future__ import annotations
from decimal import Decimal
import math
from numbers import Real
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from pandas import DataFrame
from . import _native
from .data import _json, _error


def _values(values):
    if isinstance(values, (str, bytes)) or not hasattr(values, '__len__') or len(values) > 100000:
        raise _error('RESOURCE_LIMIT', '指标需要最多 100000 个一维样本', 'indicator')
    result = []
    for value in values:
        if value is None or value is pd.NA or isinstance(value, (float, np.floating)) and math.isnan(value):
            result.append(None)
        elif isinstance(value, (Real, Decimal)) and not isinstance(value, (bool, np.bool_)):
            number = float(value)
            if not math.isfinite(number):
                raise _error('NUMERIC_RANGE_UNSUPPORTED', '指标只接受有限数值和明确缺失', 'indicator')
            result.append(number)
        else:
            raise _error('INVALID_CONTRACT', '指标需要有限数字、Decimal 或缺失', 'indicator')
    return result


def _integer(value):
    if type(value) is not int or not 1 <= value <= 10000:
        raise _error('RESOURCE_LIMIT', '指标窗口必须为 1 至 10000 的整数', 'indicator')
    return value


def _calculate(kind, values=(), **parameters):
    import json
    rows = json.loads(_native.indicator_json(_json(dict(kind=kind, values=_values(values), **parameters))))
    index = values.index if isinstance(values, pd.Series) else None
    if kind == 'macd':
        data = {}
        for name, points in rows.items():
            data[name] = [p['value'] for p in points]
            data[name+'_status'] = [p['status'] for p in points]
        frame = DataFrame(data, index=index)
    else:
        frame = DataFrame(rows, columns=['value', 'status'], index=index)
        frame['value'] = frame['value'].astype('float64')
    frame.attrs['qf'] = dict(precision='float64', missing_policy='strict_contiguous_no_fill', **parameters)
    return frame


def sma(values: Sequence[float | Decimal | int | None], period: int) -> DataFrame:
    return _calculate('sma', values, period=_integer(period))


def ema(values: Sequence[float | Decimal | int | None], period: int) -> DataFrame:
    return _calculate('ema', values, period=_integer(period))


def rsi(values: Sequence[float | Decimal | int | None], period: int = 14) -> DataFrame:
    return _calculate('rsi', values, period=_integer(period))


def atr(high: Sequence[float | Decimal | int | None], low: Sequence[float | Decimal | int | None],
        close: Sequence[float | Decimal | int | None], period: int = 14) -> DataFrame:
    frame = _calculate('atr', high=_values(high), low=_values(low), close=_values(close), period=_integer(period))
    if isinstance(high, pd.Series):
        frame.index = high.index
    return frame


def macd(values: Sequence[float | Decimal | int | None], fast: int = 12, slow: int = 26, signal: int = 9) -> DataFrame:
    return _calculate('macd', values, fast=_integer(fast), slow=_integer(slow), signal=_integer(signal))


def rolling_std(values: Sequence[float | Decimal | int | None], period: int, ddof: int = 1) -> DataFrame:
    if type(ddof) is not int or not 0 <= ddof < period:
        raise _error('INVALID_CONTRACT', 'ddof 必须为非负整数且小于窗口', 'indicator')
    return _calculate('rolling_std', values, period=_integer(period), ddof=ddof)


def rank(values: Mapping[str, float | Decimal | int | None], ascending: bool = True) -> DataFrame:
    import json
    if not isinstance(values, Mapping) or len(values) > 10000 or type(ascending) is not bool:
        raise _error('RESOURCE_LIMIT', 'rank 需要有界 security→value 映射和明确排序方向', 'indicator')
    if any(type(s) is not str or not s or len(s.encode()) > 128 for s in values):
        raise _error('INVALID_CONTRACT', 'rank 标的标识无效', 'indicator')
    converted = {s: _values([v])[0] for s, v in values.items()}
    result = json.loads(_native.indicator_json(_json(dict(kind='rank', cross_section=converted, ascending=ascending))))
    frame = DataFrame([dict(security=s, **point) for s, point in result.items()], columns=['security', 'value', 'status'])
    frame['value'] = frame['value'].astype('float64')
    frame.attrs['qf'] = dict(precision='float64', ties='average', final_tie_key='security', ascending=ascending)
    return frame


SMA, EMA, RSI, ATR, MACD = sma, ema, rsi, atr, macd
