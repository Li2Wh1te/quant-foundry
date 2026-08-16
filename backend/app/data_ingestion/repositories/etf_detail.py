"""Read models for one operator-selected ETF and its time-series data."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.models.etf import EtfCode
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.models.etf_daily import EtfDailyBar


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

    def list_daily_bars(
        self,
        *,
        ts_code: str,
        start_date: date | None,
        end_date: date | None,
        limit: int,
    ) -> list[EtfDailyBar]:
        """Return oldest-first raw bars so callers can draw an ordered chart."""
        filters: list[object] = [
            EtfDailyBar.source == self.source,
            EtfDailyBar.ts_code == ts_code,
        ]
        if start_date is not None:
            filters.append(EtfDailyBar.trade_date >= start_date)
        if end_date is not None:
            filters.append(EtfDailyBar.trade_date <= end_date)
        return self.session.scalars(
            select(EtfDailyBar)
            .where(*filters)
            .order_by(EtfDailyBar.trade_date.asc())
            .limit(limit)
        ).all()

    def list_adjustment_factors(
        self,
        *,
        ts_code: str,
        start_date: date | None,
        end_date: date | None,
        limit: int,
    ) -> list[EtfAdjustmentFactor]:
        """Return oldest-first factors aligned with daily-bar chart dates."""
        filters: list[object] = [
            EtfAdjustmentFactor.source == self.source,
            EtfAdjustmentFactor.ts_code == ts_code,
        ]
        if start_date is not None:
            filters.append(EtfAdjustmentFactor.trade_date >= start_date)
        if end_date is not None:
            filters.append(EtfAdjustmentFactor.trade_date <= end_date)
        return self.session.scalars(
            select(EtfAdjustmentFactor)
            .where(*filters)
            .order_by(EtfAdjustmentFactor.trade_date.asc())
            .limit(limit)
        ).all()
