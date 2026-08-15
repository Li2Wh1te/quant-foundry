"""ETF reference-data transfer objects."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class EtfInstrumentInput:
    """One fully normalized ETF reference record ready for persistence."""

    ts_code: str
    csname: str | None
    extname: str | None
    cname: str | None
    index_code: str | None
    index_name: str | None
    setup_date: date | None
    list_date: date | None
    list_status: str
    exchange: str
    mgr_name: str | None
    custod_name: str | None
    mgt_fee: Decimal | None
    etf_type: str | None


@dataclass(frozen=True)
class EtfBasicUpsertResult:
    """Summary of one idempotent ETF reference-data write."""

    received: int
    changed: int
    unchanged: int


@dataclass(frozen=True)
class EtfBasicSyncResult:
    """Summary of one complete ETF reference-data refresh."""

    received: int
    changed: int
    unchanged: int
    refreshed_at: datetime
