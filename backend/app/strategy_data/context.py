"""Strategy-facing facades that bind every query to one decision-time boundary."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from enum import Enum

from sqlalchemy.orm import Session

from app.data_ingestion.constants import TUSHARE_SOURCE
from app.strategy_data.portal import StrategyDataPortal
from app.strategy_data.schemas import (
    AdjustmentFactor,
    DailyBar,
    EtfCandidate,
    NoVisibleSessionError,
)


class DecisionPhase(str, Enum):
    """The two daily decision phases supported by the initial data contract."""

    BEFORE_OPEN = "before_open"
    AFTER_CLOSE = "after_close"


class MarketDataQueries:
    """Expose only safe ETF market-data reads to strategy code."""

    def __init__(self, portal: StrategyDataPortal) -> None:
        self._portal = portal

    def daily_bars(
        self,
        codes: Sequence[str],
        fields: Sequence[str],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
    ) -> list[DailyBar]:
        """Read raw ETF bars ending no later than the context visibility boundary."""
        return self._portal.daily_bars(
            codes,
            fields,
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )

    def adjustment_factors(
        self,
        codes: Sequence[str],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
    ) -> list[AdjustmentFactor]:
        """Read adjustment factors ending no later than the context boundary."""
        return self._portal.adjustment_factors(
            codes,
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )


class TradingCalendarQueries:
    """Expose completed trading sessions through the same visibility boundary."""

    def __init__(self, portal: StrategyDataPortal) -> None:
        self._portal = portal

    def sessions(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
        exchange: str | None = None,
    ) -> list[date]:
        """Read open sessions in ascending order without future dates."""
        return self._portal.calendar_sessions(
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
            exchange=exchange,
        )


class EtfUniverseQueries:
    """Expose conservative, data-backed ETF candidates to strategy code."""

    def __init__(self, portal: StrategyDataPortal) -> None:
        self._portal = portal

    def eligible_etfs(
        self,
        *,
        exchanges: Iterable[str] | None = None,
        min_history_sessions: int = 1,
        require_bar_on_cutoff: bool = True,
    ) -> list[EtfCandidate]:
        """Read candidates whose listing and bars are known by the context date."""
        return self._portal.eligible_etfs(
            exchanges=exchanges,
            min_history_sessions=min_history_sessions,
            require_bar_on_cutoff=require_bar_on_cutoff,
        )


class StrategyDataContext:
    """Provide the only data-query capabilities available to a strategy callback.

    An engine creates this object for one decision date and phase.  The strategy
    may choose a historical start date or trailing window, but all facades retain
    the private visibility cutoff computed here and reject a later query end.
    """

    def __init__(self, *, session_date: date, portal: StrategyDataPortal) -> None:
        self._session_date = session_date
        self._market = MarketDataQueries(portal)
        self._calendar = TradingCalendarQueries(portal)
        self._universe = EtfUniverseQueries(portal)

    @classmethod
    def for_session(
        cls,
        session: Session,
        *,
        session_date: date,
        phase: DecisionPhase,
        source: str = TUSHARE_SOURCE,
        calendar_exchange: str = "SSE",
    ) -> "StrategyDataContext":
        """Build a context with the correct daily visibility boundary.

        At the close, same-day daily data may be read.  Before the open, only the
        latest earlier open session may be read, even when the calendar contains
        an entry for the current date.
        """
        if phase is DecisionPhase.AFTER_CLOSE:
            visible_through_date = session_date
        else:
            visible_through_date = StrategyDataPortal.previous_open_session(
                session,
                session_date=session_date,
                exchange=calendar_exchange,
            )
            if visible_through_date is None:
                raise NoVisibleSessionError("当前时点之前没有可见的已完成交易日。")
        return cls(
            session_date=session_date,
            portal=StrategyDataPortal(
                session,
                visible_through_date=visible_through_date,
                source=source,
                calendar_exchange=calendar_exchange,
            ),
        )

    @property
    def session_date(self) -> date:
        """Return the decision date without revealing a mutable query boundary."""
        return self._session_date

    @property
    def market(self) -> MarketDataQueries:
        """Return the bounded ETF daily-bar and factor query facade."""
        return self._market

    @property
    def calendar(self) -> TradingCalendarQueries:
        """Return the bounded completed-session query facade."""
        return self._calendar

    @property
    def universe(self) -> EtfUniverseQueries:
        """Return the bounded conservative ETF-candidate query facade."""
        return self._universe
