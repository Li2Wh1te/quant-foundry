"""PostgreSQL persistence for current authoritative ETF daily bars."""

from collections.abc import Iterable
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_ingestion.schemas.etf_daily import (
    EtfDailyBarInput,
    EtfDailyBarUpsertResult,
)


class EtfDailyBarRepository:
    """Upsert raw daily bars without managing the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_bars(
        self,
        records: Iterable[EtfDailyBarInput],
        *,
        source: str,
    ) -> EtfDailyBarUpsertResult:
        """Insert a complete session or overwrite only corrected source values."""
        bars = list(records)
        self._reject_duplicate_keys(bars)
        if not bars:
            return EtfDailyBarUpsertResult(received=0, changed=0, unchanged=0)

        table = EtfDailyBar.__table__
        statement = insert(table).values(
            [
                {
                    "source": source,
                    "ts_code": bar.ts_code,
                    "trade_date": bar.trade_date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "vol": bar.vol,
                    "amount": bar.amount,
                }
                for bar in bars
            ]
        )
        excluded = statement.excluded
        source_columns = ("open", "high", "low", "close", "vol", "amount")
        changed_fields = or_(
            *(
                getattr(table.c, column).is_distinct_from(getattr(excluded, column))
                for column in source_columns
            )
        )
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.source, table.c.ts_code, table.c.trade_date],
            set_={
                **{column: getattr(excluded, column) for column in source_columns},
                "updated_at": func.now(),
            },
            where=changed_fields,
        ).returning(table.c.ts_code)
        changed = len(self.session.execute(statement).all())
        return EtfDailyBarUpsertResult(
            received=len(bars), changed=changed, unchanged=len(bars) - changed
        )

    def list_bars(
        self,
        *,
        source: str,
        ts_code: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[EtfDailyBar, ...]:
        """Read source bars for one code without changing transaction state.

        The source code is deliberately explicit here: PIT adapters call this
        method for one resolved mapping segment and must never fall back to the
        latest ``EtfCode`` association.  Date predicates are inclusive to
        match the persisted daily-bar primary key and the generic DateRange
        contract.
        """

        filters: list[object] = [
            EtfDailyBar.source == source,
            EtfDailyBar.ts_code == ts_code,
        ]
        if start_date is not None:
            filters.append(EtfDailyBar.trade_date >= start_date)
        if end_date is not None:
            filters.append(EtfDailyBar.trade_date <= end_date)
        statement = (
            select(EtfDailyBar)
            .where(*filters)
            .order_by(EtfDailyBar.trade_date.asc())
        )
        return tuple(self.session.scalars(statement).all())

    @staticmethod
    def _reject_duplicate_keys(bars: list[EtfDailyBarInput]) -> None:
        keys = [(bar.ts_code, bar.trade_date) for bar in bars]
        if len(keys) != len(set(keys)):
            raise ValueError("ETF daily input contains duplicate ts_code and trade_date")
