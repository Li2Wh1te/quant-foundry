"""The single, time-bounded database gateway exposed through strategy contexts."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.models.etf import EtfCode
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_ingestion.models.trading_calendar import TradingCalendarDay
from app.strategy_data.schemas import (
    DAILY_BAR_FIELDS,
    AdjustmentFactor,
    DailyBar,
    EtfCandidate,
    FutureDataAccessError,
    InvalidDataQueryError,
)


# Tushare has used both the documented exchange names and their shorter forms.
# Keeping the aliases here makes a strategy universe query behave consistently
# with the existing operator-facing ETF query without exposing that repository.
EXCHANGE_CODE_ALIASES = {
    "SSE": ("SSE", "SH"),
    "SH": ("SSE", "SH"),
    "SZSE": ("SZSE", "SZ"),
    "SZ": ("SZSE", "SZ"),
}


@dataclass(frozen=True)
class _ResolvedWindow:
    """An already validated closed date range used by one portal query."""

    start_date: date
    end_date: date


class StrategyDataPortal:
    """Query only data that was visible at the context's decision boundary.

    This class owns the SQLAlchemy session and the private visibility boundary.
    Strategy code receives only facade objects from ``StrategyDataContext`` and
    therefore cannot opt out of the predicates applied here.
    """

    def __init__(
        self,
        session: Session,
        *,
        visible_through_date: date,
        source: str = TUSHARE_SOURCE,
        calendar_exchange: str = "SSE",
    ) -> None:
        self._session = session
        self._visible_through_date = visible_through_date
        self._source = source
        self._calendar_exchange = calendar_exchange

    @staticmethod
    def previous_open_session(
        session: Session, *, session_date: date, exchange: str
    ) -> date | None:
        """Find the latest completed session without reading the current day.

        A before-open context deliberately asks only for calendar rows strictly
        before ``session_date``.  It never relies on the current date's calendar
        row, which keeps the visibility rule independent of future calendar data.
        """
        return session.scalar(
            select(func.max(TradingCalendarDay.calendar_date)).where(
                TradingCalendarDay.exchange == exchange,
                TradingCalendarDay.is_open.is_(True),
                TradingCalendarDay.calendar_date < session_date,
            )
        )

    def daily_bars(
        self,
        codes: Sequence[str],
        fields: Sequence[str],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
    ) -> list[DailyBar]:
        """Return chronologically ordered raw ETF bars inside a safe window."""
        normalized_codes = self._normalize_codes(codes)
        normalized_fields = self._normalize_bar_fields(fields)
        window = self._resolve_window(
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )
        if not normalized_codes or window is None:
            return []

        rows = self._session.scalars(
            select(EtfDailyBar)
            .where(
                EtfDailyBar.source == self._source,
                EtfDailyBar.ts_code.in_(normalized_codes),
                EtfDailyBar.trade_date >= window.start_date,
                EtfDailyBar.trade_date <= window.end_date,
            )
            .order_by(EtfDailyBar.trade_date.asc(), EtfDailyBar.ts_code.asc())
        ).all()
        self._assert_no_future_dates(row.trade_date for row in rows)
        return [
            DailyBar(
                ts_code=row.ts_code,
                trade_date=row.trade_date,
                values=MappingProxyType(
                    {
                        field: self._decimal_field(row, field)
                        for field in normalized_fields
                    }
                ),
            )
            for row in rows
        ]

    def adjustment_factors(
        self,
        codes: Sequence[str],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
    ) -> list[AdjustmentFactor]:
        """Return source adjustment factors subject to the same safe window."""
        normalized_codes = self._normalize_codes(codes)
        window = self._resolve_window(
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )
        if not normalized_codes or window is None:
            return []

        rows = self._session.scalars(
            select(EtfAdjustmentFactor)
            .where(
                EtfAdjustmentFactor.source == self._source,
                EtfAdjustmentFactor.ts_code.in_(normalized_codes),
                EtfAdjustmentFactor.trade_date >= window.start_date,
                EtfAdjustmentFactor.trade_date <= window.end_date,
            )
            .order_by(
                EtfAdjustmentFactor.trade_date.asc(),
                EtfAdjustmentFactor.ts_code.asc(),
            )
        ).all()
        self._assert_no_future_dates(row.trade_date for row in rows)
        return [
            AdjustmentFactor(
                ts_code=row.ts_code,
                trade_date=row.trade_date,
                adj_factor=row.adj_factor,
            )
            for row in rows
        ]

    def calendar_sessions(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
        exchange: str | None = None,
    ) -> list[date]:
        """Return open sessions in ascending order without future dates."""
        calendar_exchange = exchange or self._calendar_exchange
        window = self._resolve_window(
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
            calendar_exchange=calendar_exchange,
        )
        if window is None:
            return []
        sessions = list(
            self._session.scalars(
                select(TradingCalendarDay.calendar_date)
                .where(
                    TradingCalendarDay.exchange == calendar_exchange,
                    TradingCalendarDay.is_open.is_(True),
                    TradingCalendarDay.calendar_date >= window.start_date,
                    TradingCalendarDay.calendar_date <= window.end_date,
                )
                .order_by(TradingCalendarDay.calendar_date.asc())
            )
        )
        self._assert_no_future_dates(sessions)
        return sessions

    def eligible_etfs(
        self,
        *,
        exchanges: Iterable[str] | None = None,
        min_history_sessions: int = 1,
        require_bar_on_cutoff: bool = True,
    ) -> list[EtfCandidate]:
        """Return data-backed candidates without consulting mutable ETF status.

        ``list_status`` and other mutable ETF-basic fields are intentionally not
        part of this query.  The current reference table cannot prove their value
        on a historical strategy date.  Listing date and bars no later than the
        visibility boundary are the only reference evidence used in phase one.
        """
        if min_history_sessions < 1:
            raise InvalidDataQueryError("min_history_sessions 必须大于或等于 1。")

        normalized_exchanges = self._normalize_exchanges(exchanges)
        if normalized_exchanges == ():
            return []

        bar_history_count = (
            select(func.count())
            .select_from(EtfDailyBar)
            .where(
                EtfDailyBar.source == self._source,
                EtfDailyBar.ts_code == EtfCode.ts_code,
                EtfDailyBar.trade_date <= self._visible_through_date,
            )
            .correlate(EtfCode)
            .scalar_subquery()
        )
        filters: list[object] = [
            EtfCode.source == self._source,
            EtfCode.list_date.is_not(None),
            EtfCode.list_date <= self._visible_through_date,
            bar_history_count >= min_history_sessions,
        ]
        if normalized_exchanges is not None:
            filters.append(EtfCode.exchange.in_(normalized_exchanges))
        if require_bar_on_cutoff:
            filters.append(
                exists(
                    select(EtfDailyBar.ts_code).where(
                        EtfDailyBar.source == self._source,
                        EtfDailyBar.ts_code == EtfCode.ts_code,
                        EtfDailyBar.trade_date == self._visible_through_date,
                    )
                )
            )

        rows = self._session.execute(
            select(EtfCode.ts_code, EtfCode.exchange, EtfCode.list_date)
            .where(*filters)
            .order_by(EtfCode.ts_code.asc())
        ).all()
        return [
            EtfCandidate(code=row.ts_code, exchange=row.exchange, list_date=row.list_date)
            for row in rows
        ]

    def _resolve_window(
        self,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
        calendar_exchange: str | None = None,
    ) -> _ResolvedWindow | None:
        """Validate a caller window and bind its end to the visibility boundary."""
        if start_date is not None and lookback_sessions is not None:
            raise InvalidDataQueryError(
                "start_date 与 lookback_sessions 不能同时使用。"
            )
        if start_date is None and lookback_sessions is None:
            raise InvalidDataQueryError(
                "必须指定 start_date 或 lookback_sessions。"
            )
        if lookback_sessions is not None and lookback_sessions < 1:
            raise InvalidDataQueryError("lookback_sessions 必须大于或等于 1。")

        effective_end_date = end_date or self._visible_through_date
        if effective_end_date > self._visible_through_date:
            raise FutureDataAccessError(
                "查询截止日期不能晚于当前可见数据截止日期。"
            )

        if start_date is not None:
            if start_date > effective_end_date:
                raise InvalidDataQueryError("开始日期不能晚于截止日期。")
            return _ResolvedWindow(
                start_date=start_date,
                end_date=effective_end_date,
            )

        sessions = self._latest_open_sessions(
            end_date=effective_end_date,
            count=lookback_sessions,
            exchange=calendar_exchange or self._calendar_exchange,
        )
        if not sessions:
            return None
        return _ResolvedWindow(start_date=sessions[0], end_date=effective_end_date)

    def _latest_open_sessions(
        self, *, end_date: date, count: int, exchange: str
    ) -> list[date]:
        """Load a calendar-aligned trailing window and restore ascending order."""
        rows = list(
            self._session.scalars(
                select(TradingCalendarDay.calendar_date)
                .where(
                    TradingCalendarDay.exchange == exchange,
                    TradingCalendarDay.is_open.is_(True),
                    TradingCalendarDay.calendar_date <= end_date,
                )
                .order_by(TradingCalendarDay.calendar_date.desc())
                .limit(count)
            )
        )
        rows.reverse()
        return rows

    @staticmethod
    def _normalize_codes(codes: Sequence[str]) -> tuple[str, ...]:
        """Reject blank codes while preserving deterministic first-seen ordering."""
        normalized: list[str] = []
        for code in codes:
            value = code.strip()
            if not value:
                raise InvalidDataQueryError("ETF 代码不能为空。")
            if value not in normalized:
                normalized.append(value)
        return tuple(normalized)

    @staticmethod
    def _normalize_bar_fields(fields: Sequence[str]) -> tuple[str, ...]:
        """Accept only persisted raw-bar columns rather than arbitrary SQL fields."""
        normalized: list[str] = []
        for field in fields:
            value = field.strip()
            if value not in DAILY_BAR_FIELDS:
                raise InvalidDataQueryError(f"不支持的日线字段：{value}。")
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise InvalidDataQueryError("至少需要指定一个日线字段。")
        return tuple(normalized)

    @staticmethod
    def _normalize_exchanges(
        exchanges: Iterable[str] | None,
    ) -> tuple[str, ...] | None:
        """Expand accepted exchange aliases without accepting blank filter values."""
        if exchanges is None:
            return None
        normalized: list[str] = []
        for exchange in exchanges:
            value = exchange.strip().upper()
            if not value:
                raise InvalidDataQueryError("交易所代码不能为空。")
            for alias in EXCHANGE_CODE_ALIASES.get(value, (value,)):
                if alias not in normalized:
                    normalized.append(alias)
        return tuple(normalized)

    @staticmethod
    def _decimal_field(row: EtfDailyBar, field: str) -> Decimal:
        """Keep the model-to-public-field conversion explicit and auditable."""
        return getattr(row, field)

    def _assert_no_future_dates(self, values: Iterable[date]) -> None:
        """Defend against an accidental future predicate regression in every query."""
        if any(value > self._visible_through_date for value in values):
            raise FutureDataAccessError("查询结果包含了当前时点之后的数据。")
