"""Task package 03-05: EngineDataView / StrategyDataView permission split.

Covers the permission boundary between the engine-only chunk reads and
the strategy-facing view (``data_cutoff`` enforcement, 512/513 lookback,
gap preservation, immutable DTOs, inactive adjustment series), plus the
dynamic candidate-set calendar gate.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from app.backtesting.data.errors import (
    HistoryIncompleteError,
    InvalidDataRequestError,
    UniverseCalendarNotPreflightedError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.memory import MemoryDataProvider
from app.backtesting.data.requests import (
    BarQuery,
    DateRange,
    QueryBoundary,
)
from app.backtesting.data.views import (
    ChunkEngineDataView,
    ChunkStrategyDataView,
    EngineDataView,
    require_preflighted_calendar_ids,
)
from app.strategy_protocol.contract import (
    AdjustmentNotActiveError,
    DataCutoffViolationError,
    LookbackLimitExceededError,
)
from app.strategy_protocol.data_view import (
    AdjustmentBasis,
    AdjustmentPolicyGate,
    BarDTO,
    StrategyDataView,
)
from tests.test_backtesting_memory_provider import (
    IID_A,
    IID_B,
    TZ,
    build_dataset,
    build_fixture_a,
    chunk_query,
    make_bar,
    make_intent,
    open_ready_session,
    weekdays,
)

J5 = date(2026, 1, 5)
J6 = date(2026, 1, 6)
J7 = date(2026, 1, 7)
J8 = date(2026, 1, 8)


def at(day: date, hour: int = 15) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=TZ)


def open_chunk_fixture():
    """A ready three-session run (J5..J7) with two warmup sessions."""

    provider = MemoryDataProvider(
        build_dataset(
            facts_start=date(2025, 12, 26),
            facts_end=date(2026, 1, 8),
            open_days={date(2025, 12, 29), date(2025, 12, 30),
                       date(2025, 12, 31), date(2026, 1, 2), J5, J6, J7},
        )
    )
    session = open_ready_session(provider, make_intent(start=J5, end=J7, warmup=2))
    chunk = session.open_chunk(
        chunk_query(session, 0)
    )
    return provider, session, chunk


# ---------------------------------------------------------------------------
# Permission boundary between the two views
# ---------------------------------------------------------------------------


class TestViewPermissionBoundary(unittest.TestCase):
    def setUp(self) -> None:
        self.provider, self.session, self.chunk = open_chunk_fixture()
        self.chunk.validate_consistency()
        # The strategy froze its knowledge at the J5 close AND the whole
        # J5 daily bar is proven complete, so the cutoff day is readable;
        # later sessions inside the chunk stay engine-only.
        self.strategy_view = ChunkStrategyDataView(
            chunk=self.chunk,
            frequency="1d",
            data_cutoff=at(J5),
            include_cutoff_day=True,
        )
        self.engine_view = ChunkEngineDataView(self.chunk)

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self.strategy_view, StrategyDataView)
        self.assertIsInstance(self.engine_view, EngineDataView)

    def test_strategy_view_hides_chunk_provider_and_session(self) -> None:
        for attribute in (
            "chunk",
            "provider",
            "session",
            "_session",
            "_provider",
            "dataset",
            "consistency_evidence",
        ):
            with self.subTest(attribute=attribute):
                with self.assertRaises(AttributeError):
                    getattr(self.strategy_view, attribute)

    def test_facades_are_read_only(self) -> None:
        for facade in (self.strategy_view, self.engine_view):
            with self.assertRaises(AttributeError):
                facade.data_cutoff = at(J7)
            with self.assertRaises(AttributeError):
                delattr(facade, "bars")

    def test_views_do_not_leak_each_other_surface(self) -> None:
        # The strategy view offers no engine-only fact reads.
        for name in ("trading_rules", "trading_status", "corporate_actions"):
            self.assertFalse(hasattr(self.strategy_view, name))
        # The engine view offers no strategy-facing surface.
        for name in ("adjusted_series", "universe"):
            self.assertFalse(hasattr(self.engine_view, name))

    def test_future_read_fails_without_trimming_or_reads(self) -> None:
        reads_before = self.provider.read_count
        with self.assertRaises(DataCutoffViolationError):
            self.strategy_view.bars(IID_A, start_date=J6, end_date=J6)
        # Failed before any provider access.
        self.assertEqual(self.provider.read_count, reads_before)

    def test_engine_still_reads_the_match_day(self) -> None:
        rows = self.engine_view.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=QueryBoundary(data_cutoff=at(J7), include_cutoff_day=True),
                window=DateRange(start_date=J5, end_date=J7),
            )
        )
        self.assertEqual([bar.trade_date for bar in rows], [J5, J6, J7])
        # The same dates stay invisible to the strategy view.
        with self.assertRaises(DataCutoffViolationError):
            self.strategy_view.bars(IID_A, start_date=J6, end_date=J6)

    def test_returned_dtos_are_immutable_and_carry_decimals(self) -> None:
        result = self.strategy_view.bars(IID_A, start_date=J5, end_date=J5)
        self.assertEqual(len(result), 1)
        bar = result[0]
        self.assertIsInstance(bar, BarDTO)
        self.assertEqual(bar.trade_date, J5)
        self.assertIsInstance(bar.values["close"], Decimal)
        with self.assertRaises(FrozenInstanceError):
            bar.trade_date = J6
        with self.assertRaises(TypeError):
            bar.values["close"] = Decimal("0")


# ---------------------------------------------------------------------------
# Bounded lookback windows through the strategy view
# ---------------------------------------------------------------------------


class TestStrategyLookbackBounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        all_days = weekdays(date(2024, 1, 1), date(2026, 3, 31))
        cls.formal = all_days[-513:]
        cls.provider = MemoryDataProvider(
            build_dataset(
                facts_start=all_days[0],
                facts_end=all_days[-1],
                open_days=set(all_days),
            )
        )
        cls.session = open_ready_session(
            cls.provider,
            make_intent(start=cls.formal[0], end=cls.formal[-1], warmup=2),
        )
        cls.last_chunk_index = (len(cls.formal) - 1) // 20
        cls.chunk = cls.session.open_chunk(
            chunk_query(
                cls.session, cls.last_chunk_index
            )
        )
        cls.chunk.validate_consistency()
        cls.view = ChunkStrategyDataView(
            chunk=cls.chunk,
            frequency="1d",
            data_cutoff=at(cls.formal[-1]),
            # The fixture holds complete daily bars for every open day, so
            # the whole cutoff day is proven visible.
            include_cutoff_day=True,
        )

    def test_lookback_512_succeeds_through_the_view(self) -> None:
        rows = self.view.bars(IID_A, lookback_sessions=512)
        self.assertEqual(len(rows), 512)
        self.assertEqual(rows[0].trade_date, self.formal[-512])
        self.assertEqual(rows[-1].trade_date, self.formal[-1])

    def test_lookback_513_fails_before_any_read(self) -> None:
        reads_before = self.provider.read_count
        with self.assertRaises(LookbackLimitExceededError):
            self.view.bars(IID_A, lookback_sessions=513)
        self.assertEqual(self.provider.read_count, reads_before)


class TestMissingBarsStayMissing(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = build_fixture_a()
        self.session = open_ready_session(
            self.provider, make_intent(start=J5, end=J7)
        )
        self.chunk = self.session.open_chunk(
            chunk_query(self.session, 0)
        )
        self.chunk.validate_consistency()

    def test_gap_is_preserved_not_filled(self) -> None:
        view = ChunkStrategyDataView(
            chunk=self.chunk,
            frequency="1d",
            data_cutoff=at(J7),
            include_cutoff_day=True,
        )
        rows = view.bars(IID_A, start_date=J5, end_date=J7)
        # Bars exist only on J5 and J7 in this fixture; J6 stays absent
        # instead of being forward-filled.
        self.assertEqual([bar.trade_date for bar in rows], [J5, J7])

    def test_lookback_over_an_incomplete_window_blocks(self) -> None:
        view = ChunkStrategyDataView(
            chunk=self.chunk,
            frequency="1d",
            data_cutoff=at(J7),
            include_cutoff_day=True,
        )
        with self.assertRaises(HistoryIncompleteError):
            view.bars(IID_A, lookback_sessions=7)


# ---------------------------------------------------------------------------
# Adjustment gating and construction validation
# ---------------------------------------------------------------------------


class TestAdjustmentAndConstruction(unittest.TestCase):
    def setUp(self) -> None:
        self.provider, self.session, self.chunk = open_chunk_fixture()
        self.chunk.validate_consistency()

    def test_inactive_adjustment_basis_is_blocked(self) -> None:
        view = ChunkStrategyDataView(
            chunk=self.chunk, frequency="1d", data_cutoff=at(J5)
        )
        for basis in (AdjustmentBasis.QFQ, AdjustmentBasis.HFQ):
            with self.subTest(basis=basis):
                with self.assertRaises(AdjustmentNotActiveError):
                    view.adjusted_series(IID_A, basis=basis)

    def test_raw_adjusted_series_is_unsupported_here(self) -> None:
        view = ChunkStrategyDataView(
            chunk=self.chunk,
            frequency="1d",
            data_cutoff=at(J5),
            adjustment_gate=AdjustmentPolicyGate.active_gate(),
        )
        with self.assertRaises(UnsupportedCapabilityError):
            view.adjusted_series(IID_A, basis=AdjustmentBasis.RAW)

    def test_naive_data_cutoff_is_rejected(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            ChunkStrategyDataView(
                chunk=self.chunk,
                frequency="1d",
                data_cutoff=datetime(2026, 1, 5, 15, 0),
            )

    def test_unbounded_bar_read_is_rejected(self) -> None:
        view = ChunkStrategyDataView(
            chunk=self.chunk, frequency="1d", data_cutoff=at(J5)
        )
        with self.assertRaises(InvalidDataRequestError):
            view.bars(IID_A)


# ---------------------------------------------------------------------------
# Dynamic candidate-set calendar gate
# ---------------------------------------------------------------------------


class TestUniverseCalendarGate(unittest.TestCase):
    def test_preflighted_calendars_pass(self) -> None:
        require_preflighted_calendar_ids(
            ["cal-a", "cal-b"], allowed_calendar_ids={"cal-a", "cal-b"}
        )

    def test_unpreflighted_calendar_blocks_with_stable_code(self) -> None:
        with self.assertRaises(UniverseCalendarNotPreflightedError) as ctx:
            require_preflighted_calendar_ids(
                ["cal-a", "cal-x"], allowed_calendar_ids=("cal-a", "cal-b")
            )
        self.assertEqual(
            ctx.exception.code, "universe_calendar_not_preflighted"
        )
        self.assertEqual(
            dict(ctx.exception.details)["unpreflighted_calendar_ids"],
            ("cal-x",),
        )

    def test_non_string_input_is_rejected(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            require_preflighted_calendar_ids(["cal-a", 3], allowed_calendar_ids=["cal-a"])
        with self.assertRaises(InvalidDataRequestError):
            require_preflighted_calendar_ids("cal-a", allowed_calendar_ids=["cal-a"])


# ---------------------------------------------------------------------------
# Cutoff-day visibility: pre-open, intraday, and after-close cutoffs
# ---------------------------------------------------------------------------


class TestCutoffDayVisibility(unittest.TestCase):
    """The cutoff day leaks nothing unless whole-day proof is explicit."""

    def setUp(self) -> None:
        self.provider, self.session, self.chunk = open_chunk_fixture()
        self.chunk.validate_consistency()

    def _view(self, cutoff: datetime, **kwargs) -> ChunkStrategyDataView:
        return ChunkStrategyDataView(
            chunk=self.chunk, frequency="1d", data_cutoff=cutoff, **kwargs
        )

    def test_pre_open_cutoff_cannot_read_that_day(self) -> None:
        view = self._view(at(J6, hour=8))
        with self.assertRaises(DataCutoffViolationError):
            view.bars(IID_A, start_date=J6, end_date=J6)
        # A lookback ends at the last COMPLETED session instead.
        rows = view.bars(IID_A, lookback_sessions=1)
        self.assertEqual([bar.trade_date for bar in rows], [J5])

    def test_intraday_cutoff_cannot_read_that_day(self) -> None:
        view = self._view(at(J6, hour=11))
        with self.assertRaises(DataCutoffViolationError):
            view.bars(IID_A, start_date=J6, end_date=J6)
        rows = view.bars(IID_A, lookback_sessions=1)
        self.assertEqual([bar.trade_date for bar in rows], [J5])

    def test_after_close_cutoff_still_requires_explicit_proof(self) -> None:
        # Even a 15:00 cutoff must opt in: the facade cannot know on its
        # own that the daily bar for the cutoff day is complete.
        view = self._view(at(J6, hour=15))
        with self.assertRaises(DataCutoffViolationError):
            view.bars(IID_A, start_date=J6, end_date=J6)

    def test_proven_complete_cutoff_day_is_readable(self) -> None:
        view = self._view(at(J6, hour=15), include_cutoff_day=True)
        rows = view.bars(IID_A, start_date=J6, end_date=J6)
        self.assertEqual([bar.trade_date for bar in rows], [J6])


# ---------------------------------------------------------------------------
# Provider-row re-validation at the strategy boundary
# ---------------------------------------------------------------------------


class _FakeChunk:
    """A chunk stand-in whose bars() returns attacker-controlled rows."""

    def __init__(self, rows) -> None:
        self._rows = rows

    def bars(self, query: BarQuery):
        return self._rows


class TestProviderRowRevalidation(unittest.TestCase):
    def _view(self, rows) -> ChunkStrategyDataView:
        self.chunk = _FakeChunk(rows)
        return ChunkStrategyDataView(
            chunk=self.chunk,
            frequency="1d",
            data_cutoff=at(J7),
            include_cutoff_day=True,
        )

    def test_wrong_instrument_row_is_rejected(self) -> None:
        from app.strategy_protocol.contract import InvalidProviderResultError

        stranger = make_bar(IID_B, J5)
        view = self._view([stranger])
        with self.assertRaises(InvalidProviderResultError):
            view.bars(IID_A, start_date=J5, end_date=J5)

    def test_non_bar_row_is_rejected(self) -> None:
        from app.strategy_protocol.contract import InvalidProviderResultError

        view = self._view([object()])
        with self.assertRaises(InvalidProviderResultError):
            view.bars(IID_A, start_date=J5, end_date=J5)

    def test_future_dated_row_is_rejected(self) -> None:
        from app.strategy_protocol.contract import InvalidProviderResultError

        view = self._view([make_bar(IID_A, J8)])
        with self.assertRaises(InvalidProviderResultError):
            view.bars(IID_A, start_date=J5, end_date=J7)

    def test_wrong_frequency_row_is_rejected(self) -> None:
        from app.strategy_protocol.contract import InvalidProviderResultError

        row = make_bar(IID_A, J5)
        object.__setattr__(row, "frequency", "1m")
        view = self._view([row])
        with self.assertRaises(InvalidProviderResultError):
            view.bars(IID_A, start_date=J5, end_date=J5)

    def test_valid_rows_still_pass_revalidation(self) -> None:
        view = self._view([make_bar(IID_A, J5), make_bar(IID_A, J6)])
        rows = view.bars(IID_A, start_date=J5, end_date=J6)
        self.assertEqual([bar.trade_date for bar in rows], [J5, J6])

    def test_row_beyond_requested_end_date_is_rejected(self) -> None:
        from app.strategy_protocol.contract import InvalidProviderResultError

        # Requested 2026-01-05..2026-01-06 but the provider also returned
        # 2026-01-07: an explicit end date is a hard upper bound.
        view = self._view(
            [make_bar(IID_A, J5), make_bar(IID_A, J6), make_bar(IID_A, J7)]
        )
        with self.assertRaises(InvalidProviderResultError):
            view.bars(IID_A, start_date=J5, end_date=J6)

    def test_cutoff_day_row_rejected_when_not_proven_complete(self) -> None:
        from app.strategy_protocol.contract import InvalidProviderResultError

        # include_cutoff_day=False: even inside a lookback read, a forged
        # provider row on the cutoff day itself must be rejected.
        chunk = _FakeChunk((make_bar(IID_A, J7),))
        view = ChunkStrategyDataView(
            chunk=chunk,
            frequency="1d",
            data_cutoff=at(J7),
        )
        with self.assertRaises(InvalidProviderResultError):
            view.bars(IID_A, lookback_sessions=1)


if __name__ == "__main__":
    unittest.main()
