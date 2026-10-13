"""Shared native code lookup required by existing watchlist mutations."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.models.etf import EtfCode


class EtfDetailQueryRepository:
    """Read a single source-scoped ETF without triggering ingestion work.

    The public operator view is intentionally scoped to the same source as the
    ETF reference-data list.  This prevents a later provider from silently
    combining prices or adjustment factors from incompatible source series.
    """

    def __init__(self, session: Session, *, source: str = TUSHARE_SOURCE) -> None:
        self.session = session
        self.source = source

    def get_code(self, ts_code: str) -> EtfCode | None:
        """Return the selected ETF reference record, if it exists for the source."""
        return self.session.scalar(
            select(EtfCode).where(
                EtfCode.source == self.source,
                EtfCode.ts_code == ts_code,
            )
        )
