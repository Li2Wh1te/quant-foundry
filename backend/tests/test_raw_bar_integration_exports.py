"""Public import and invocation surface for the raw-Bar data layer.

The generic query DTO, the single Bar fact envelope, the ETF adapter, and the
bounded query facades are intentionally discoverable from one package.  This
smoke test keeps that integration edge from regressing while implementations
remain split across their focused modules.
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.backtesting.data import (
    Bar,
    BarFact,
    BarQuery,
    ChunkEngineDataView,
    ChunkStrategyDataView,
    EtfFactsAdapter,
    EngineDataView,
    DateRange,
    QueryBoundary,
)


class RawBarIntegrationExportsTest(unittest.TestCase):
    def test_fact_query_adapter_and_views_share_public_surface(self) -> None:
        self.assertIs(BarFact, Bar)
        adapter = EtfFactsAdapter(
            code_mappings=lambda *args, **kwargs: (),
            daily_bars=lambda *args, **kwargs: (),
            adjustment_factors=lambda *args, **kwargs: (),
            trading_days=lambda *args, **kwargs: [],
        )
        row = SimpleNamespace(
            ts_code="510300.SH",
            trade_date=date(2026, 8, 17),
            open=Decimal("3.70"),
            high=Decimal("3.80"),
            low=Decimal("3.60"),
            close=Decimal("3.75"),
            vol=Decimal("100"),
            amount=Decimal("375"),
            updated_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        fact = adapter.project_bar(row, uuid4())
        self.assertIsInstance(fact, BarFact)

        query = BarQuery(
            instrument_ids=fact.instrument_id,
            frequency="1d",
            boundary=QueryBoundary(data_cutoff=datetime(2026, 8, 18, tzinfo=UTC)),
            window=DateRange(date(2026, 8, 17), date(2026, 8, 17)),
        )
        self.assertEqual(query.instrument_ids, (fact.instrument_id,))

        class StubChunk:
            def bars(self, received_query):
                self.received_query = received_query
                return ()

        chunk = StubChunk()
        self.assertEqual(ChunkEngineDataView(chunk).bars(query), ())
        self.assertIs(chunk.received_query, query)
        self.assertTrue(callable(ChunkEngineDataView))
        self.assertTrue(callable(ChunkStrategyDataView))
        self.assertTrue(isinstance(EngineDataView, type))


if __name__ == "__main__":
    unittest.main()
