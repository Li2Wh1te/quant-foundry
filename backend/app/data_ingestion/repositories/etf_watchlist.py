"""Bounded, owner-scoped selections and one latest stored bar per selection."""

from sqlalchemy import delete, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.models.etf import EtfCode
from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_ingestion.models.etf_watchlist import EtfWatchlistEntry


class EtfWatchlistRepository:
    def __init__(self, session: Session, owner_scope: str):
        self.session = session
        self.owner_scope = owner_scope

    def add(self, ts_code: str) -> bool:
        """Idempotent insertion is safe under concurrent requests/retries."""
        result = self.session.execute(
            insert(EtfWatchlistEntry).values(
                owner_scope=self.owner_scope, source=TUSHARE_SOURCE, ts_code=ts_code
            ).on_conflict_do_nothing().returning(EtfWatchlistEntry.ts_code)
        )
        return result.scalar_one_or_none() is not None

    def remove(self, ts_code: str) -> bool:
        """Delete only the authenticated owner's selected relation."""
        result = self.session.execute(
            delete(EtfWatchlistEntry).where(
                EtfWatchlistEntry.owner_scope == self.owner_scope,
                EtfWatchlistEntry.source == TUSHARE_SOURCE,
                EtfWatchlistEntry.ts_code == ts_code,
            ).returning(EtfWatchlistEntry.ts_code)
        )
        return result.scalar_one_or_none() is not None

    def list_entries(self, *, limit: int, offset: int):
        """Limit selections before lateral indexed lookups, avoiding N+1 calls.

        The latest stored session is authoritative even when its close is null:
        selecting an older valid price would conceal a source-data problem.
        Outer joins retain a saved selection when reference data is missing.
        """
        entries = select(EtfWatchlistEntry).where(
            EtfWatchlistEntry.owner_scope == self.owner_scope,
            EtfWatchlistEntry.source == TUSHARE_SOURCE,
        ).order_by(EtfWatchlistEntry.created_at, EtfWatchlistEntry.ts_code).limit(limit).offset(offset).subquery()
        daily = select(
            EtfDailyBar.trade_date, EtfDailyBar.close, EtfDailyBar.pre_close,
            EtfDailyBar.change, EtfDailyBar.pct_chg,
        ).where(
            EtfDailyBar.source == entries.c.source,
            EtfDailyBar.ts_code == entries.c.ts_code,
        ).order_by(EtfDailyBar.trade_date.desc()).limit(1).lateral()
        return self.session.execute(select(
            entries.c.ts_code, entries.c.created_at,
            EtfCode.csname, EtfCode.extname, EtfCode.cname, EtfCode.exchange,
            daily.c.trade_date, daily.c.close, daily.c.pre_close,
            daily.c.change, daily.c.pct_chg,
        ).select_from(entries).outerjoin(EtfCode,
            (EtfCode.source == entries.c.source) & (EtfCode.ts_code == entries.c.ts_code)
        ).outerjoin(daily, true()).order_by(entries.c.created_at, entries.c.ts_code)).mappings().all()
