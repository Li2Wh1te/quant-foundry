"""Read models for the operator-facing ETF reference-data page."""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.data_ingestion.constants import ETF_BASIC_SYNC_KEY, TUSHARE_SOURCE
from app.data_ingestion.models.etf import EtfCode
from app.data_ingestion.models.sync_checkpoint import DataSyncCheckpoint


# Tushare ETF-basic has historically returned both exchange-code conventions.
# The admin API accepts the documented market identifiers while querying every
# equivalent persisted source value, so a source normalization rollout cannot
# make an existing ETF disappear from the operator view.
EXCHANGE_CODE_ALIASES = {
    "SSE": ("SSE", "SH"),
    "SH": ("SSE", "SH"),
    "SZSE": ("SZSE", "SZ"),
    "SZ": ("SZSE", "SZ"),
}


@dataclass(frozen=True)
class EtfOverview:
    """Aggregated values shown above the ETF reference-data table."""

    total_records: int
    exchange_count: int
    listed_count: int
    first_list_date: date | None
    latest_list_date: date | None
    last_updated_at: datetime | None
    refreshed_at: datetime | None
    exchanges: list[str]


class EtfQueryRepository:
    """Query one source's ETF reference snapshot without invoking ingestion."""

    def __init__(self, session: Session, *, source: str = TUSHARE_SOURCE) -> None:
        self.session = session
        self.source = source

    def list_codes(
        self,
        *,
        keyword: str | None,
        exchange: str | None,
        list_status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[EtfCode], int]:
        """Return a stable page and the exact count for the same ETF predicates."""
        filters = self._filters(
            keyword=keyword,
            exchange=exchange,
            list_status=list_status,
        )
        items = self.session.scalars(
            select(EtfCode)
            .where(*filters)
            .order_by(EtfCode.list_date.desc().nullslast(), EtfCode.ts_code.asc())
            .limit(limit)
            .offset(offset)
        ).all()
        total = self.session.scalar(
            select(func.count()).select_from(EtfCode).where(*filters)
        )
        return items, int(total or 0)

    def overview(self) -> EtfOverview:
        """Return source-scoped totals and the last successful refresh marker."""
        summary = self.session.execute(
            select(
                func.count(EtfCode.ts_code),
                func.count(EtfCode.ts_code).filter(EtfCode.list_status == "L"),
                func.min(EtfCode.list_date),
                func.max(EtfCode.list_date),
                func.max(EtfCode.last_seen_at),
            ).where(EtfCode.source == self.source)
        ).one()
        # Report actual source-scoped markets, normalizing legacy aliases before
        # counting so SH/SSE and SZ/SZSE cannot appear as different exchanges.
        exchanges = sorted({
            self._exchange_codes(exchange)[0]
            for exchange in self.session.scalars(
                select(EtfCode.exchange).where(EtfCode.source == self.source).distinct()
            )
        })
        checkpoint = self.session.get(
            DataSyncCheckpoint,
            {"sync_key": ETF_BASIC_SYNC_KEY, "scope_key": "market=CN"},
        )
        return EtfOverview(
            total_records=int(summary[0] or 0),
            exchange_count=len(exchanges),
            listed_count=int(summary[1] or 0),
            first_list_date=summary[2],
            latest_list_date=summary[3],
            last_updated_at=summary[4],
            refreshed_at=self._refresh_timestamp(checkpoint),
            exchanges=exchanges,
        )

    def _filters(
        self,
        *,
        keyword: str | None,
        exchange: str | None,
        list_status: str | None,
    ) -> list[object]:
        """Build predicates once so the list and count always agree."""
        filters: list[object] = [EtfCode.source == self.source]
        if keyword is not None:
            # Match the market workspace's literal substring search across fund,
            # tracked-index and manager fields. Escape LIKE metacharacters so a
            # typed '%' or '_' does not accidentally select the whole catalogue.
            filters.append(or_(
                *(column.icontains(keyword.strip(), autoescape=True) for column in (
                    EtfCode.ts_code, EtfCode.csname, EtfCode.extname, EtfCode.cname,
                    EtfCode.index_code, EtfCode.index_name, EtfCode.mgr_name,
                ))
            ))
        if exchange is not None:
            filters.append(EtfCode.exchange.in_(self._exchange_codes(exchange)))
        if list_status is not None:
            filters.append(EtfCode.list_status == list_status)
        return filters

    @staticmethod
    def _exchange_codes(exchange: str) -> tuple[str, ...]:
        """Expand compatible Shanghai and Shenzhen source exchange codes."""
        normalized = exchange.upper()
        return EXCHANGE_CODE_ALIASES.get(normalized, (normalized,))

    @staticmethod
    def _refresh_timestamp(checkpoint: DataSyncCheckpoint | None) -> datetime | None:
        """Ignore malformed historical cursors instead of breaking a read-only page."""
        if checkpoint is None:
            return None
        refreshed_at = checkpoint.cursor.get("refreshed_at")
        if not isinstance(refreshed_at, str):
            return None
        try:
            return datetime.fromisoformat(refreshed_at)
        except ValueError:
            return None
