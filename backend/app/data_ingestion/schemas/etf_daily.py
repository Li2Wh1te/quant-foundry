"""ETF daily-bar transfer objects."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib


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


def canonical_row_revision(bar: EtfDailyBarInput, *, source: str) -> str:
    """Return deterministic content revision using fixed database precision."""
    values = [
        source,
        "fund_daily@1",
        bar.ts_code,
        bar.trade_date.isoformat(),
        f"{bar.open.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP):.6f}",
        f"{bar.high.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP):.6f}",
        f"{bar.low.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP):.6f}",
        f"{bar.close.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP):.6f}",
        f"{bar.vol.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}",
        f"{bar.amount.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}",
    ]
    digest = hashlib.sha256("|".join(values).encode()).hexdigest()
    return f"derived:{source}:fund_daily_row@1:sha256:{digest}"


def batch_revision(revisions: list[str]) -> str:
    """Hash sorted row revisions to identify one provider response batch."""
    return hashlib.sha256("|".join(sorted(revisions)).encode()).hexdigest()


@dataclass(frozen=True)
class EtfDailyBarUpsertResult:
    """Summary of one atomic daily-bar write."""

    received: int
    changed: int
    unchanged: int
    inserted: int = 0
    corrected: int = 0
    metadata_backfilled: int = 0
    batch_revision: str | None = None
    affected_start_date: date | None = None
    affected_end_date: date | None = None


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
    inserted: int = 0
    corrected: int = 0
    metadata_backfilled: int = 0
    batch_revision: str | None = None
    affected_start_date: date | None = None
    affected_end_date: date | None = None
