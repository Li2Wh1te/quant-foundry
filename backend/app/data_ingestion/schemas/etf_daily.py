"""ETF daily-bar transfer objects."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class EtfDailyBarInput:
    """One validated raw ETF daily bar ready for persistence."""

    ts_code: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vol: Decimal
    amount: Decimal


@dataclass(frozen=True)
class EtfDailyBarUpsertResult:
    """Summary of one atomic daily-bar write."""

    received: int
    changed: int
    unchanged: int


@dataclass(frozen=True)
class EtfDailySyncResult:
    """Summary of all fully committed ETF daily-bar sessions."""

    days_completed: int
    received: int
    changed: int
    unchanged: int
    synced_through_date: date | None
    start_date: date | None = None
    end_date: date | None = None
    calendar_ids: tuple[str, ...] = ()
