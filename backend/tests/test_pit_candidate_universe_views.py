"""Acceptance tests for PIT candidate DTOs and step-scoped authorization."""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest
from uuid import UUID

from app.backtesting.data.requests import (
    ContractRef,
    InstrumentScopeMode,
    MarketScope,
    QueryBoundary,
    UniverseQuery,
    UniverseQueryPolicy,
)
from app.backtesting.data.views import ChunkStrategyDataView
from app.strategy_protocol.data_view import (
    InstrumentCandidateDTO,
    StrategyDataDTO,
    UniverseQueryDTO,
)
from app.backtesting.data.errors import InvalidDataRequestError


UTC = timezone.utc
RULE = ContractRef("scope.etf", 1)
FIRST = UUID("00000000-0000-4000-8000-000000000001")
SECOND = UUID("00000000-0000-4000-8000-000000000002")


def _candidate(instrument_id: UUID, exchange: str = "SSE") -> InstrumentCandidateDTO:
    """Build one already-resolved strategy candidate projection."""

    return InstrumentCandidateDTO(
        instrument_id=instrument_id,
        trading_code=f"{instrument_id.int % 1000000:06d}",
        name=f"ETF-{instrument_id.int % 1000}",
        display_name=f"ETF-{instrument_id.int % 1000}",
        asset_class="etf",
        exchange=exchange,
    )


class _Chunk:
    """Small chunk double exposing only the candidate permission hooks."""

    def __init__(self, rows):
        self.rows = tuple(rows)
        self.calls = 0
        self.authorized = frozenset()

    def bars(self, query):
        return ()

    def begin_decision_step(self, step_key=None):
        self.authorized = frozenset()

    def clear_step_candidate_authorization(self):
        self.authorized = frozenset()

    def universe(self, query):
        self.calls += 1
        return self.rows

    def authorize_step_candidates(self, instrument_ids, *, query=None):
        self.authorized = frozenset(instrument_ids)


def _query(*, effective_date: date = date(2026, 1, 5)) -> UniverseQuery:
    """Build a complete bound PIT query for the fake chunk."""

    return UniverseQuery(
        rule=RULE,
        market_scope=MarketScope(exchanges=("SSE",), asset_classes=("etf",)),
        effective_date=effective_date,
        boundary=QueryBoundary(
            datetime(2026, 1, 5, 15, tzinfo=UTC), include_cutoff_day=True
        ),
        allowed_calendar_ids=("SSE",),
        scope_mode=InstrumentScopeMode.DYNAMIC,
        universe_query_policy=UniverseQueryPolicy((RULE,)),
    )


class CandidateViewTests(unittest.TestCase):
    def test_repeated_query_is_cached_and_authorizes_only_narrowed_rows(self):
        chunk = _Chunk((_candidate(FIRST), _candidate(SECOND, exchange="SZSE")))
        view = ChunkStrategyDataView(
            chunk=chunk,
            frequency="1d",
            data_cutoff=datetime(2026, 1, 5, 15, tzinfo=UTC),
            include_cutoff_day=True,
            effective_date=date(2026, 1, 5),
        )
        bound = view.universe(_query())
        rows = bound.query(exchanges=("SSE",))
        self.assertEqual(tuple(row.instrument_id for row in rows), (FIRST,))
        self.assertEqual(chunk.authorized, frozenset({FIRST}))
        self.assertEqual(bound.query(exchanges=("SSE",)), rows)
        self.assertEqual(chunk.calls, 1)

    def test_strategy_filter_cannot_widen_frozen_scope(self):
        chunk = _Chunk((_candidate(FIRST),))
        view = ChunkStrategyDataView(
            chunk=chunk,
            frequency="1d",
            data_cutoff=datetime(2026, 1, 5, 15, tzinfo=UTC),
            include_cutoff_day=True,
            effective_date=date(2026, 1, 5),
        )
        with self.assertRaises(InvalidDataRequestError):
            view.universe(_query()).query(exchanges=("SZSE",))
        self.assertEqual(chunk.authorized, frozenset())

    def test_new_step_derives_new_pit_coordinates(self):
        chunk = _Chunk((_candidate(FIRST),))
        view = ChunkStrategyDataView(
            chunk=chunk,
            frequency="1d",
            data_cutoff=datetime(2026, 1, 5, 15, tzinfo=UTC),
            include_cutoff_day=True,
            effective_date=date(2026, 1, 5),
        )
        next_bound = view.universe(_query()).for_step(
            effective_date=date(2026, 1, 6),
            data_cutoff=datetime(2026, 1, 6, 15, tzinfo=UTC),
        )
        self.assertEqual(next_bound.effective_date, date(2026, 1, 6))
        self.assertEqual(next_bound.boundary.data_cutoff.date(), date(2026, 1, 6))
        self.assertEqual(chunk.authorized, frozenset())

    def test_strategy_data_facade_can_expose_bound_universe(self):
        chunk = _Chunk((_candidate(FIRST),))
        view = ChunkStrategyDataView(
            chunk=chunk,
            frequency="1d",
            data_cutoff=datetime(2026, 1, 5, 15, tzinfo=UTC),
            include_cutoff_day=True,
            effective_date=date(2026, 1, 5),
        )
        data = StrategyDataDTO(
            view,
            data_cutoff=datetime(2026, 1, 5, 15, tzinfo=UTC),
            universe=view.universe(_query()),
        )
        self.assertIsInstance(data.universe, UniverseQueryDTO)
        self.assertEqual(data.universe.query()[0].instrument_id, FIRST)


if __name__ == "__main__":
    unittest.main()
