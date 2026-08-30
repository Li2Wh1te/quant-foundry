"""Executable baseline checks for task 14-01.

This test deliberately checks the contracts that already exist before the
adjustment implementation work starts.  It does not introduce a second data
provider or mutate production behavior; later task-14 slices can use the same
surfaces while extending the missing evidence and price-generation gates.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.backtesting.data.adapters.etf import EtfFactsAdapter
from app.backtesting.data.facts import AdjustedSeriesPoint, Bar
from app.backtesting.data.protocols import DataChunkSession, DataProvider
from app.backtesting.data.reports import DataPreflightReport
from app.backtesting.data.requests import (
    AdjustedSeriesQuery,
    BarQuery,
    PriceBasis,
)
from app.backtesting.data.views import ChunkEngineDataView, ChunkStrategyDataView
from app.backtesting.result_models import BacktestDataPreflightRecord
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.instruments.domain import InstrumentCodeMapping
from app.strategy_protocol.data_view import AdjustmentBasis, StrategyDataDTO


INSTRUMENT_ID = uuid4()
SOURCE_CODE = "510300.SH"
DAY = date(2026, 8, 21)
CUTOFF = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)


class Task14BaselineContractTestCase(unittest.TestCase):
    """Pin the 14-01 inventory without implementing later task slices."""

    def test_price_basis_and_adjusted_query_facts_are_existing_contracts(self) -> None:
        self.assertEqual(
            tuple(item.value for item in PriceBasis),
            ("raw", "qfq", "hfq"),
        )
        self.assertIn("price_basis", AdjustedSeriesQuery.__dataclass_fields__)
        self.assertIn("price_basis", BarQuery.__dataclass_fields__)
        self.assertIn("price_basis", AdjustedSeriesPoint.__dataclass_fields__)
        self.assertIn("adj_factor", AdjustedSeriesPoint.__dataclass_fields__)

    def test_adjusted_series_protocol_is_exposed_by_the_chunk_session(self) -> None:
        # DataProvider owns admission/session lifecycle; the business method is
        # intentionally on DataChunkSession, which is the equivalent existing
        # capability described by task 14-01.
        self.assertTrue(hasattr(DataProvider, "open_session"))
        self.assertTrue(hasattr(DataProvider, "preflight"))
        self.assertTrue(hasattr(DataChunkSession, "adjusted_series"))

    def test_etf_storage_and_adapter_surfaces_are_available(self) -> None:
        columns = set(EtfAdjustmentFactor.__table__.columns.keys())
        self.assertTrue(
            {"source", "ts_code", "trade_date", "adj_factor"}.issubset(columns)
        )
        adapter_fields = EtfFactsAdapter.__dataclass_fields__
        self.assertTrue(
            {"daily_bars", "adjustment_factors", "code_mappings"}.issubset(
                adapter_fields
            )
        )
        self.assertFalse(adapter_fields["adjustment_active"].default)
        for method_name in ("resolve", "bars", "adjusted_series", "preflight_summary"):
            self.assertTrue(callable(getattr(EtfFactsAdapter, method_name, None)))

    def test_raw_bar_read_does_not_touch_adjustment_factors(self) -> None:
        """The raw path remains usable when the factor port is unavailable."""

        bar_row = SimpleNamespace(
            ts_code=SOURCE_CODE,
            trade_date=DAY,
            open=Decimal("3.70"),
            high=Decimal("3.75"),
            low=Decimal("3.65"),
            close=Decimal("3.72"),
            vol=Decimal("100"),
            amount=Decimal("372"),
            updated_at=CUTOFF,
        )
        mapping = InstrumentCodeMapping(
            instrument_id=INSTRUMENT_ID,
            source="tushare",
            source_code=SOURCE_CODE,
            trading_code="510300",
            valid_from=date(2020, 1, 1),
            valid_to=None,
            mapping_source="baseline",
            evidence="baseline",
            known_at=CUTOFF,
            observed_at=CUTOFF,
        )

        def code_mappings(instrument_id, **kwargs):
            return (mapping,)

        def daily_bars(source_code, start_date, end_date):
            self.assertEqual(source_code, SOURCE_CODE)
            return (bar_row,)

        def adjustment_factors(*args, **kwargs):
            raise AssertionError("raw bar reads must not access adjustment factors")

        adapter = EtfFactsAdapter(
            code_mappings=code_mappings,
            daily_bars=daily_bars,
            adjustment_factors=adjustment_factors,
            trading_days=lambda exchange, start, end: [DAY],
        )
        resolution = adapter.resolve(
            INSTRUMENT_ID,
            sessions=(DAY,),
            data_cutoff=CUTOFF,
        )
        history = adapter.bars(INSTRUMENT_ID, resolution=resolution)
        self.assertEqual(len(history.bars), 1)
        self.assertIs(history.bars[0].price_basis, PriceBasis.RAW)

    def test_preflight_and_strategy_view_keep_explicit_basis_surfaces(self) -> None:
        report_fields = DataPreflightReport.__dataclass_fields__
        self.assertIn("strategy_price_bases", report_fields)
        self.assertIn("engine_price_basis", report_fields)
        self.assertIn("adjustment_series_policy", report_fields)
        record_fields = BacktestDataPreflightRecord.__dataclass_fields__
        self.assertTrue(
            {"capabilities", "coverage", "pit_status", "source_revisions"}.issubset(
                record_fields
            )
        )
        self.assertTrue(hasattr(ChunkStrategyDataView, "adjusted_series"))
        self.assertFalse(hasattr(ChunkEngineDataView, "adjusted_series"))
        self.assertEqual(
            inspect.signature(StrategyDataDTO.adjusted_series).parameters["basis"].default,
            AdjustmentBasis.RAW,
        )
        self.assertEqual(AdjustmentBasis.RAW.value, PriceBasis.RAW.value)
        self.assertEqual(AdjustmentBasis.QFQ.value, PriceBasis.QFQ.value)
        self.assertEqual(AdjustmentBasis.HFQ.value, PriceBasis.HFQ.value)

    def test_etf_adapter_has_no_network_import(self) -> None:
        """Backtest reads must stay on injected storage ports."""

        tree = ast.parse(inspect.getsource(__import__(
            "app.backtesting.data.adapters.etf", fromlist=["EtfFactsAdapter"]
        )))
        imported_modules = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn("tushare", imported_modules)


if __name__ == "__main__":
    unittest.main()
