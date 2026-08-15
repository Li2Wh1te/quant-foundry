"""Trading calendar data transfer objects."""

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class TradingCalendarDayInput:
    """One normalized trading calendar record ready for persistence."""

    exchange: str
    calendar_date: date
    is_open: bool
    previous_trading_date: date | None


@dataclass(frozen=True)
class TradeCalendarUpsertResult:
    """Summary of one idempotent trading calendar write."""

    received: int
    changed: int
    unchanged: int


@dataclass(frozen=True)
class DataSyncCheckpointState:
    """A detached generic synchronization checkpoint snapshot."""

    sync_key: str
    scope_key: str
    cursor: dict[str, Any]
    cursor_version: int
    version: int


@dataclass(frozen=True)
class TradeCalendarDateRange:
    """An inclusive calendar-date range for one Tushare request."""

    start_date: date
    end_date: date


@dataclass(frozen=True)
class TradeCalendarSyncResult:
    """Summary of all successfully committed trading calendar ranges."""

    ranges_completed: int
    received: int
    changed: int
    unchanged: int
    synced_through_date: date | None
