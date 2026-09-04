"""ETF daily-bar transfer objects."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib


@dataclass(frozen=True)
class EtfDailyBarInput:
    """One source ETF daily bar ready for persistence.

    The ingestion boundary retains missing and numerically invalid market values
    as typed raw facts.  The adapter, not this DTO, decides whether a bar is
    usable for a backtest.
    """

    ts_code: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    vol: Decimal | None
    amount: Decimal | None


def _canonical_decimal(
    value: Decimal | None, *, quantum: Decimal, places: int
) -> str:
    """Represent a nullable decimal deterministically for source revisions."""
    if value is None:
        return "null"
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), f".{places}f")


def canonical_row_revision(bar: EtfDailyBarInput, *, source: str) -> str:
    """Return deterministic content revision using fixed database precision."""
    values = [
        source,
        "fund_daily@1",
        bar.ts_code,
        bar.trade_date.isoformat(),
        _canonical_decimal(bar.open, quantum=Decimal("0.000001"), places=6),
        _canonical_decimal(bar.high, quantum=Decimal("0.000001"), places=6),
        _canonical_decimal(bar.low, quantum=Decimal("0.000001"), places=6),
        _canonical_decimal(bar.close, quantum=Decimal("0.000001"), places=6),
        _canonical_decimal(bar.vol, quantum=Decimal("0.0001"), places=4),
        _canonical_decimal(bar.amount, quantum=Decimal("0.0001"), places=4),
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
