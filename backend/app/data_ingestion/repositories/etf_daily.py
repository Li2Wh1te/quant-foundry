"""PostgreSQL persistence for current authoritative ETF daily bars."""

from collections.abc import Iterable
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_ingestion.models.etf_daily import EtfDailyBar, EtfDailyBarRevisionAudit
from app.data_ingestion.schemas.etf_daily import (
    EtfDailyBarInput,
    EtfDailyBarUpsertResult,
    batch_revision,
    canonical_row_revision,
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
        accepted_at: datetime | None = None,
    ) -> EtfDailyBarUpsertResult:
        """Insert a complete session or overwrite only corrected source values."""
        bars = list(records)
        self._reject_duplicate_keys(bars)
        if not bars:
            return EtfDailyBarUpsertResult(received=0, changed=0, unchanged=0)

        table = EtfDailyBar.__table__
        return self._upsert_revisioned(
            bars, source=source,
            accepted_at=(accepted_at or datetime.now(UTC)),
        )

    def _upsert_revisioned(
        self, bars: list[EtfDailyBarInput], *, source: str, accepted_at: datetime
    ) -> EtfDailyBarUpsertResult:
        """Classify rows and append correction metadata without committing."""
        table = EtfDailyBar.__table__
        revisions = [canonical_row_revision(bar, source=source) for bar in bars]
        batch = batch_revision(revisions)
        counts = {"inserted": 0, "corrected": 0, "metadata_backfilled": 0, "unchanged": 0}
        audit_table = EtfDailyBarRevisionAudit.__table__
        for bar, revision in zip(bars, revisions):
            key = {"source": source, "ts_code": bar.ts_code, "trade_date": bar.trade_date}
            current = self.session.get(EtfDailyBar, (source, bar.ts_code, bar.trade_date))
            values = {
                **key,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "vol": bar.vol,
                "amount": bar.amount,
                "source_revision": revision,
            }
            if current is None:
                self.session.execute(insert(table).values(values))
                counts["inserted"] += 1
                continue
            source_fields = ("open", "high", "low", "close", "vol", "amount")
            changed_fields = sorted(
                field for field in source_fields
                if getattr(current, field) != values[field]
            )
            old_revision = getattr(current, "source_revision", None)
            if old_revision == revision:
                counts["unchanged"] += 1
                continue
            if changed_fields:
                kind = "correction"
                counts["corrected"] += 1
            elif old_revision is None:
                kind = "metadata_backfill"
                counts["metadata_backfilled"] += 1
            else:
                counts["unchanged"] += 1
                continue
            # Update current state while preserving created_at and avoiding wall-clock revisions.
            self.session.execute(
                table.update()
                .where(
                    table.c.source == source,
                    table.c.ts_code == bar.ts_code,
                    table.c.trade_date == bar.trade_date,
                )
                .values(**values, updated_at=func.now())
            )
            if audit_table is not None:
                self.session.execute(
                    insert(audit_table).values(
                        source=source,
                        ts_code=bar.ts_code,
                        trade_date=bar.trade_date,
                        previous_source_revision=old_revision,
                        source_revision=revision,
                        batch_revision=batch,
                        accepted_at=accepted_at,
                        change_kind=kind,
                        changed_fields=changed_fields,
                    )
                )
        changed = counts["inserted"] + counts["corrected"] + counts["metadata_backfilled"]
        return EtfDailyBarUpsertResult(
            received=len(bars), changed=changed, unchanged=counts["unchanged"],
            inserted=counts["inserted"], corrected=counts["corrected"],
            metadata_backfilled=counts["metadata_backfilled"], batch_revision=batch,
            affected_start_date=min(bar.trade_date for bar in bars),
            affected_end_date=max(bar.trade_date for bar in bars),
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
