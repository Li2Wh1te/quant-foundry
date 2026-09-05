"""Acceptance checks for task 14-06 preflight/hash and 14-07 isolation."""

from __future__ import annotations

import copy
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.backtesting.data.adjustment_policy import (
    ADJUSTMENT_SERIES_POLICY_KEY,
    AdjustmentSeriesPolicy,
)
from app.backtesting.data.adapters.etf import (
    EtfFactsAdapter,
    build_data_preflight_payloads,
)
from app.backtesting.data.errors import ProviderContractViolationError
from app.backtesting.data.facts import Bar, FactEvidence
from app.backtesting.data.requests import (
    BarQuery,
    DateRange,
    PriceBasis,
    QueryBoundary,
    QualityStatus,
)
from app.backtesting.data.views import ChunkEngineDataView


INSTRUMENT_ID = uuid4()
SESSIONS = (date(2026, 8, 19), date(2026, 8, 20))
CUTOFF = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _artifact(*, batch_id: str | None = None) -> dict[str, object]:
    verification: dict[str, object] = {
        "summary": "real source rows matched at declared precision",
        "status": "verified",
        "published": True,
        "input_hash": HASH_A,
        "output_hash": HASH_B,
        "evidence_hash": HASH_C,
    }
    if batch_id is not None:
        # Source batch metadata is intentionally not part of the run-level
        # policy contract; only its reproducible evidence digest is retained.
        verification["batch_id"] = batch_id
    return {
        "policy": {"key": ADJUSTMENT_SERIES_POLICY_KEY, "version": 1},
        "adapter": {"version": "etf_raw_bar_adapter@1"},
        "source": {"name": "tushare", "batch_id": "batch-202608"},
        "field_mapping": {"adj_factor": "adj_factor", "effective_date": "trade_date"},
        "semantics": {
            "cutoff_rule": "effective_date <= data_cutoff",
            "qfq_formula": "tushare_qfq_native_v1",
            "hfq_formula": "tushare_hfq_native_v1",
            "qfq_anchor": "latest-visible-close",
            "hfq_anchor": "first-visible-close",
            "precision": 6,
            "rounding": "source-declared-half-up",
        },
        "verification": verification,
    }


def _adapter(*, policy: AdjustmentSeriesPolicy | None = None) -> EtfFactsAdapter:
    return EtfFactsAdapter(
        code_mappings=lambda *args, **kwargs: (),
        daily_bars=lambda *args, **kwargs: (),
        adjustment_factors=lambda *args, **kwargs: (),
        trading_days=lambda *args, **kwargs: (),
        adjustment_policy=policy,
    )


def _summary(adapter: EtfFactsAdapter, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "instrument_ids": [INSTRUMENT_ID],
        "expected_sessions": SESSIONS,
        "bars_by_instrument": {INSTRUMENT_ID: list(SESSIONS)},
        "data_cutoff": CUTOFF,
    }
    values.update(overrides)
    return adapter.preflight_summary(**values)  # type: ignore[arg-type]


class PreflightContractTestCase(unittest.TestCase):
    def test_raw_is_allowed_while_inactive_and_payload_contains_contract(self) -> None:
        summary = _summary(_adapter(), strategy_price_bases=(PriceBasis.RAW,))
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["policy_status"], "inactive")
        self.assertEqual(summary["pit_status"]["adjustment_factors"], "tushare_adj_factor_native@1:effective_date_cutoff")
        payloads = build_data_preflight_payloads(summary)
        capabilities = payloads["capabilities"]
        self.assertEqual(capabilities["adjustment_series_policy"]["key"], ADJUSTMENT_SERIES_POLICY_KEY)
        self.assertEqual(capabilities["policy_status"], "inactive")
        self.assertIn("factor_coverage", capabilities)
        self.assertEqual(
            capabilities["adjustment_series_policy"]["verification"]["status"],
            None,
        )

    def test_adjusted_basis_requires_active_policy_and_complete_coverages(self) -> None:
        summary = _summary(
            _adapter(),
            strategy_price_bases=(PriceBasis.QFQ,),
            factors_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
            research_prices_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
        )
        self.assertEqual(summary["status"], "blocked")
        self.assertIn("ADJUSTMENT_POLICY_INACTIVE", {item["code"] for item in summary["issues"]})

        active = AdjustmentSeriesPolicy.from_verification_artifact(_artifact())
        ready = _summary(
            _adapter(policy=active),
            strategy_price_bases=(PriceBasis.QFQ, PriceBasis.HFQ),
            factors_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
            research_prices_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["factor_coverage"][str(INSTRUMENT_ID)]["status"], "complete")
        self.assertEqual(ready["research_price_coverage"][str(INSTRUMENT_ID)]["status"], "complete")
        self.assertEqual(ready["cutoff_boundary"]["cutoff_local_date"], "2026-08-20")

    def test_adjusted_basis_blocks_missing_research_coverage_and_cutoff(self) -> None:
        summary = _summary(
            _adapter(policy=AdjustmentSeriesPolicy.from_verification_artifact(_artifact())),
            strategy_price_bases=(PriceBasis.QFQ,),
            factors_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
            data_cutoff=None,
        )
        codes = {item["code"] for item in summary["issues"]}
        self.assertIn("ADJUSTED_PRICE_COVERAGE_MISSING", codes)
        self.assertIn("ADJUSTMENT_CUTOFF_MISSING", codes)
        self.assertEqual(summary["status"], "blocked")

    def test_adjustment_contract_and_coverage_are_hash_relevant_but_credentials_are_not(self) -> None:
        first_policy = AdjustmentSeriesPolicy.from_verification_artifact(_artifact(batch_id="one"))
        second_policy = AdjustmentSeriesPolicy.from_verification_artifact(_artifact(batch_id="two"))
        common = {
            "strategy_price_bases": (PriceBasis.QFQ,),
            "factors_by_instrument": {INSTRUMENT_ID: list(SESSIONS)},
            "research_prices_by_instrument": {INSTRUMENT_ID: list(SESSIONS)},
        }
        first = _summary(_adapter(policy=first_policy), **common)
        second = _summary(_adapter(policy=second_policy), **common)
        self.assertEqual(first["report_hash"], second["report_hash"])
        self.assertNotIn("batch_id", repr(first))
        changed_coverage = copy.deepcopy(common)
        changed_coverage["factors_by_instrument"] = {INSTRUMENT_ID: [SESSIONS[0]]}
        changed = _summary(_adapter(policy=first_policy), **changed_coverage)
        self.assertNotEqual(first["report_hash"], changed["report_hash"])
        payloads = build_data_preflight_payloads(first)
        self.assertNotIn("batch_id", repr(payloads))

    def test_preflight_redacts_credential_shaped_issue_details(self) -> None:
        summary = _summary(
            _adapter(),
            blocking_issues=(
                {
                    "code": "PROVIDER_CONTRACT_VIOLATION",
                    "authorization": "Bearer should-not-be-retained",
                    "reason": "token=should-not-be-retained",
                },
            ),
        )
        rendered = repr(summary)
        self.assertNotIn("should-not-be-retained", rendered)
        self.assertNotIn("authorization", rendered)


class EnginePriceBasisIsolationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        evidence = FactEvidence(
            source="tushare",
            observed_at=CUTOFF,
            quality_status=QualityStatus.COMPLETE,
        )
        self.raw = Bar(
            instrument_id=INSTRUMENT_ID,
            trade_date=SESSIONS[0],
            frequency="1d",
            open=Decimal("10"),
            high=Decimal("12"),
            low=Decimal("8"),
            close=Decimal("11"),
            evidence=evidence,
        )
        self.adjusted = Bar(
            instrument_id=INSTRUMENT_ID,
            trade_date=SESSIONS[0],
            frequency="1d",
            open=Decimal("5"),
            high=Decimal("6"),
            low=Decimal("4"),
            close=Decimal("5.5"),
            price_basis=PriceBasis.QFQ,
            evidence=evidence,
        )
        self.boundary = QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True)

    def _query(self, basis: PriceBasis) -> BarQuery:
        return BarQuery(
            instrument_ids=INSTRUMENT_ID,
            frequency="1d",
            boundary=self.boundary,
            window=DateRange(SESSIONS[0], SESSIONS[0]),
            price_basis=basis,
        )

    def test_engine_accepts_raw_and_rejects_adjusted_queries_or_rows(self) -> None:
        class Chunk:
            def __init__(self, row: Bar) -> None:
                self.row = row
                self.queries: list[BarQuery] = []

            def bars(self, query: BarQuery):
                self.queries.append(query)
                return (self.row,)

        raw_chunk = Chunk(self.raw)
        self.assertEqual(ChunkEngineDataView(raw_chunk).bars(self._query(PriceBasis.RAW)), (self.raw,))
        with self.assertRaises(ProviderContractViolationError):
            ChunkEngineDataView(Chunk(self.raw)).bars(self._query(PriceBasis.QFQ))
        with self.assertRaises(ProviderContractViolationError):
            ChunkEngineDataView(Chunk(self.adjusted)).bars(self._query(PriceBasis.RAW))


if __name__ == "__main__":
    unittest.main()
