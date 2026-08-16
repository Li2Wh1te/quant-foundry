"""Public immutable values and validation errors for strategy data access."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping


DAILY_BAR_FIELDS = frozenset({"open", "high", "low", "close", "vol", "amount"})


class FutureDataAccessError(ValueError):
    """Raised when a strategy asks for data later than its visible boundary."""


class InvalidDataQueryError(ValueError):
    """Raised when a strategy data query has an invalid or ambiguous window."""


class NoVisibleSessionError(RuntimeError):
    """Raised when a before-open context cannot find a completed session."""


@dataclass(frozen=True)
class DailyBar:
    """One raw daily bar with only the fields requested by the strategy.

    The mapping intentionally contains no implicit fills.  A missing source bar
    stays absent from the returned sequence instead of being substituted with a
    previous value that could conceal a listing, suspension, or data-quality gap.
    """

    ts_code: str
    trade_date: date
    values: Mapping[str, Decimal]


@dataclass(frozen=True)
class AdjustmentFactor:
    """One source-provided ETF adjustment factor visible to the strategy."""

    ts_code: str
    trade_date: date
    adj_factor: Decimal


@dataclass(frozen=True)
class EtfCandidate:
    """A minimally described ETF candidate with time-safe reference fields only."""

    code: str
    exchange: str
    list_date: date
