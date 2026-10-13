"""Shared source-scoped native catalogue for watchlist selection/diagnostics."""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.models.etf import EtfCode


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
        instrument_id: UUID | None = None,
    ) -> tuple[list[EtfCode], int]:
        """Return a stable page and the exact count for the same ETF predicates."""
        filters = self._filters(
            keyword=keyword,
            exchange=exchange,
            list_status=list_status,
        )
        if instrument_id is not None:
            filters.append(EtfCode.etf_id == instrument_id)
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
