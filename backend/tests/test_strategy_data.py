"""Tests for the strategy-facing, future-safe ETF data control plane."""

from datetime import UTC, date, datetime
from decimal import Decimal
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.models.etf import EtfCode, EtfEntity
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_ingestion.models.trading_calendar import TradingCalendarDay
from app.strategy_data import (
    DecisionPhase,
    FutureDataAccessError,
    InvalidDataQueryError,
    StrategyDataContext,
)


class StrategyDataContextTestCase(unittest.TestCase):
    """Exercise query results against a real temporary relational database."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        # Production targets PostgreSQL, whose ``btrim`` function is used by the
        # database constraints.  Register the equivalent SQLite test function so
        # these integration-style tests can exercise real query results locally.
        event.listen(
            self.engine,
            "connect",
            lambda connection, _: connection.create_function(
                "btrim", 1, lambda value: value.strip() if value is not None else None
            ),
        )
        self._create_strategy_data_tables()
        self.session = Session(self.engine)
        self._seed_calendar()
        self._seed_etfs()
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()
        self._drop_strategy_data_tables()
        self.engine.dispose()

    def test_after_close_daily_bars_exclude_a_seeded_future_bar(self) -> None:
        context = self._context(date(2026, 8, 17), DecisionPhase.AFTER_CLOSE)

        bars = context.market.daily_bars(
            ["510300.SH"],
            ["close"],
            start_date=date(2026, 8, 13),
        )

        self.assertEqual(
            [bar.trade_date for bar in bars],
            [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)],
        )
        self.assertEqual([dict(bar.values) for bar in bars], [
            {"close": Decimal("3.13")},
            {"close": Decimal("3.14")},
            {"close": Decimal("3.17")},
        ])

    def test_future_end_date_is_rejected_instead_of_silently_clipped(self) -> None:
        context = self._context(date(2026, 8, 17), DecisionPhase.AFTER_CLOSE)

        with self.assertRaises(FutureDataAccessError):
            context.market.daily_bars(
                ["510300.SH"],
                ["close"],
                start_date=date(2026, 8, 13),
                end_date=date(2026, 8, 18),
            )

        with self.assertRaises(FutureDataAccessError):
            context.market.adjustment_factors(
                ["510300.SH"],
                start_date=date(2026, 8, 13),
                end_date=date(2026, 8, 18),
            )

        with self.assertRaises(FutureDataAccessError):
            context.calendar.sessions(
                start_date=date(2026, 8, 13),
                end_date=date(2026, 8, 18),
            )

    def test_before_open_uses_previous_open_session_not_calendar_arithmetic(self) -> None:
        context = self._context(date(2026, 8, 17), DecisionPhase.BEFORE_OPEN)

        bars = context.market.daily_bars(
            ["510300.SH"],
            ["close"],
            start_date=date(2026, 8, 13),
        )

        self.assertEqual(context.session_date, date(2026, 8, 17))
        self.assertEqual(
            [bar.trade_date for bar in bars],
            [date(2026, 8, 13), date(2026, 8, 14)],
        )

    def test_lookback_sessions_uses_the_exchange_calendar_window(self) -> None:
        context = self._context(date(2026, 8, 17), DecisionPhase.AFTER_CLOSE)

        bars = context.market.daily_bars(
            ["510300.SH"],
            ["close"],
            lookback_sessions=2,
        )
        sessions = context.calendar.sessions(lookback_sessions=2)

        self.assertEqual(
            [bar.trade_date for bar in bars],
            [date(2026, 8, 14), date(2026, 8, 17)],
        )
        self.assertEqual(sessions, [date(2026, 8, 14), date(2026, 8, 17)])

    def test_adjustment_factors_follow_the_same_cutoff_and_ordering(self) -> None:
        context = self._context(date(2026, 8, 17), DecisionPhase.AFTER_CLOSE)

        factors = context.market.adjustment_factors(
            ["510300.SH"],
            start_date=date(2026, 8, 14),
        )

        self.assertEqual(
            [(factor.trade_date, factor.adj_factor) for factor in factors],
            [
                (date(2026, 8, 14), Decimal("1.14")),
                (date(2026, 8, 17), Decimal("1.17")),
            ],
        )

    def test_eligible_etfs_uses_listing_and_bar_evidence_not_current_status(self) -> None:
        context = self._context(date(2026, 8, 17), DecisionPhase.AFTER_CLOSE)

        candidates = context.universe.eligible_etfs(
            exchanges=["SSE"],
            min_history_sessions=3,
        )

        self.assertEqual(
            [candidate.code for candidate in candidates],
            ["510300.SH", "512222.SH"],
        )

    def test_ambiguous_and_unsupported_queries_are_rejected(self) -> None:
        context = self._context(date(2026, 8, 17), DecisionPhase.AFTER_CLOSE)

        with self.assertRaises(InvalidDataQueryError):
            context.market.daily_bars(
                ["510300.SH"],
                ["close"],
                start_date=date(2026, 8, 13),
                lookback_sessions=2,
            )
        with self.assertRaises(InvalidDataQueryError):
            context.market.daily_bars(
                ["510300.SH"],
                ["adjusted_close"],
                lookback_sessions=2,
            )
        with self.assertRaises(InvalidDataQueryError):
            context.universe.eligible_etfs(min_history_sessions=0)

    def _context(
        self, session_date: date, phase: DecisionPhase
    ) -> StrategyDataContext:
        return StrategyDataContext.for_session(
            self.session,
            session_date=session_date,
            phase=phase,
        )

    def _create_strategy_data_tables(self) -> None:
        """Create only the PostgreSQL-independent tables used by these tests."""
        for model in (
            EtfEntity,
            EtfCode,
            EtfDailyBar,
            EtfAdjustmentFactor,
            TradingCalendarDay,
        ):
            model.__table__.create(self.engine)

    def _drop_strategy_data_tables(self) -> None:
        """Drop in reverse dependency order after each isolated test database."""
        for model in (
            TradingCalendarDay,
            EtfAdjustmentFactor,
            EtfDailyBar,
            EtfCode,
            EtfEntity,
        ):
            model.__table__.drop(self.engine)

    def _seed_calendar(self) -> None:
        self.session.add_all(
            [
                TradingCalendarDay(
                    exchange="SSE",
                    calendar_date=date(2026, 8, 13),
                    is_open=True,
                    previous_trading_date=date(2026, 8, 12),
                ),
                TradingCalendarDay(
                    exchange="SSE",
                    calendar_date=date(2026, 8, 14),
                    is_open=True,
                    previous_trading_date=date(2026, 8, 13),
                ),
                TradingCalendarDay(
                    exchange="SSE",
                    calendar_date=date(2026, 8, 17),
                    is_open=True,
                    previous_trading_date=date(2026, 8, 14),
                ),
                TradingCalendarDay(
                    exchange="SSE",
                    calendar_date=date(2026, 8, 18),
                    is_open=True,
                    previous_trading_date=date(2026, 8, 17),
                ),
            ]
        )

    def _seed_etfs(self) -> None:
        self._add_etf("510300.SH", list_status="L", bar_dates=(13, 14, 17, 18))
        self._add_etf("512222.SH", list_status="D", bar_dates=(13, 14, 17))
        self._add_etf("511111.SH", list_status="L", bar_dates=(13, 17))
        self._add_etf(
            "599999.SH",
            list_status="L",
            list_date=date(2026, 8, 18),
            bar_dates=(18,),
        )

    def _add_etf(
        self,
        code: str,
        *,
        list_status: str,
        bar_dates: tuple[int, ...],
        list_date: date = date(2026, 8, 13),
    ) -> None:
        entity_id = uuid4()
        observed_at = datetime(2026, 8, 18, tzinfo=UTC)
        self.session.add(EtfEntity(id=entity_id))
        self.session.add(
            EtfCode(
                source=TUSHARE_SOURCE,
                ts_code=code,
                etf_id=entity_id,
                list_date=list_date,
                list_status=list_status,
                exchange="SSE",
                first_seen_at=observed_at,
                last_seen_at=observed_at,
            )
        )
        for day in bar_dates:
            trade_date = date(2026, 8, day)
            self.session.add(
                EtfDailyBar(
                    source=TUSHARE_SOURCE,
                    ts_code=code,
                    trade_date=trade_date,
                    open=Decimal(f"3.{day}"),
                    high=Decimal(f"3.{day}"),
                    low=Decimal(f"3.{day}"),
                    close=Decimal(f"3.{day}"),
                    vol=Decimal("100"),
                    amount=Decimal("1000"),
                )
            )
            self.session.add(
                EtfAdjustmentFactor(
                    source=TUSHARE_SOURCE,
                    ts_code=code,
                    trade_date=trade_date,
                    adj_factor=Decimal(f"1.{day}"),
                )
            )


if __name__ == "__main__":
    unittest.main()
