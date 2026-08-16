"""ETF adjustment-factor transfer objects."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class EtfAdjustmentFactorInput:
    """One validated source adjustment factor ready for persistence."""

    ts_code: str
    trade_date: date
    adj_factor: Decimal


@dataclass(frozen=True)
class EtfAdjustmentFactorUpsertResult:
    """Summary of one current-factor upsert operation."""

    received: int
    changed: int
    unchanged: int
