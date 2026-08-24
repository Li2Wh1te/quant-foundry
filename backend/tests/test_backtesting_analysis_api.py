"""API tests for analyzer-aware result endpoints: extended metric fields,
analyzer_state semantics, reason_code exposure, the analysis-summary
endpoint (including rate_snapshot), and unchanged cursor pagination."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.result_models import (
    BacktestAnalysisSummaryRecord,
    BacktestMetricRecord,
)
from app.backtesting.result_records import RESULT_TABLE_NAMES, Base
from app.backtesting.result_repository import BacktestResultRepository
from app.backtesting.result_router import get_analysis_summary, list_metrics
from app.backtesting.result_schemas import (
    BacktestAnalysisSummaryItem,
    BacktestMetricItem,
    ResultCursorPage,
)
from fastapi import HTTPException

UTC = timezone.utc
SIGNING_KEY = "unit-test-signing-key"


class AnalysisApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        result_tables = [Base.metadata.tables[name] for name in RESULT_TABLE_NAMES]
        Base.metadata.create_all(self.engine, tables=result_tables)
        self.session = Session(self.engine)
        self.repo = BacktestResultRepository(
            self.session, cursor_signing_key=SIGNING_KEY
        )
        self.run_id = uuid4()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def seed_metrics(self) -> None:
        rows = [
            BacktestMetricRecord(
                run_id=self.run_id,
                metric_key="cumulative_fees",
                formula_version="cumulative_applied_fill_fees_v1",
                value=Decimal("12.5"),
                unit="currency",
                sample_count=3,
                analyzer_key="fee_summary",
                analyzer_version=1,
                analyzer_metadata={"gross_traded_notional": "5000"},
            ),
            BacktestMetricRecord(
                run_id=self.run_id,
                metric_key="sharpe",
                formula_version="sharpe_simple_ddof1_252_v1",
                sample_count=2,
                unavailable_reason="收益率标准差为 0",
                analyzer_key="sharpe_simple",
                analyzer_version=1,
                analyzer_metadata={
                    "reason_code": "ZERO_RETURN_STDDEV",
                    "annualization_factor": "252",
                },
            ),
            # A legacy row written before analyzer identity existed.
            BacktestMetricRecord(
                run_id=self.run_id,
                metric_key="sharpe",
                formula_version="pre_analyzer_v0",
                value=Decimal("1"),
            ),
        ]
        self.repo.append_metrics(*rows)

    def serialize_metrics_page(self) -> dict:
        page = list_metrics(
            run_id=self.run_id,
            limit=10,
            cursor=None,
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        model = ResultCursorPage[BacktestMetricItem].model_validate(page)
        return model.model_dump(mode="json")

    def test_metrics_expose_analyzer_identity_and_state(self):
        self.seed_metrics()
        payload = self.serialize_metrics_page()
        by_key = {
            (item["metric_key"], item["formula_version"]): item
            for item in payload["items"]
        }
        fees = by_key[("cumulative_fees", "cumulative_applied_fill_fees_v1")]
        self.assertEqual(fees["analyzer_key"], "fee_summary")
        self.assertEqual(fees["analyzer_version"], 1)
        self.assertEqual(fees["analyzer_state"], "registered")
        self.assertEqual(fees["value"], "12.5")
        self.assertIsInstance(fees["value"], str)

        sharpe = by_key[("sharpe", "sharpe_simple_ddof1_252_v1")]
        self.assertIsNone(sharpe["value"])
        self.assertEqual(sharpe["unavailable_reason"], "收益率标准差为 0")
        self.assertEqual(
            sharpe["analyzer_metadata"]["reason_code"], "ZERO_RETURN_STDDEV"
        )
        self.assertEqual(sharpe["analyzer_state"], "registered")

        legacy = by_key[("sharpe", "pre_analyzer_v0")]
        self.assertEqual(legacy["analyzer_state"], "legacy")
        self.assertIsNone(legacy["analyzer_key"])

    def test_metrics_cursor_pagination_unchanged(self):
        many = [
            BacktestMetricRecord(
                run_id=self.run_id,
                metric_key=f"metric_{index:03d}",
                formula_version="v1",
                value=Decimal(index),
                analyzer_key="fee_summary",
                analyzer_version=1,
            )
            for index in range(7)
        ]
        self.repo.append_metrics(*many)
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            page = list_metrics(
                run_id=self.run_id,
                limit=3,
                cursor=cursor,
                session=self.session,
                signing_key=SIGNING_KEY,
            )
            model = ResultCursorPage[BacktestMetricItem].model_validate(page).model_dump(mode="json")
            seen.extend(item["metric_key"] for item in model["items"])
            pages += 1
            if not model["has_more"]:
                break
            cursor = model["next_cursor"]
        self.assertEqual(len(seen), len(set(seen)), "no duplicates across pages")
        self.assertEqual(len(seen), 7, "no rows missed across pages")
        self.assertGreaterEqual(pages, 3)

    def test_summary_endpoint_returns_frozen_content(self):
        now = datetime.now(UTC)
        summary = BacktestAnalysisSummaryRecord(
            run_id=self.run_id,
            status="final",
            analyzer_snapshot={"specs": [{"analyzer_key": "sharpe_simple"}]},
            formula_signature="sha256:formula",
            input_evidence_signature="sha256:evidence",
            reporting_currency="CNY",
            initial_equity=Decimal("10000"),
            valid_day_count=5,
            fill_count=2,
            gross_traded_notional=Decimal("5000"),
            cumulative_fees=Decimal("12.5"),
            rate_snapshot={"rates": {"2026-07-06": "0.02"}},
            rate_snapshot_hash="sha256:rates",
            rate_source_versions={"source_key": "rf", "source_version": 1},
            missing_ranges=[["2026-07-07", "2026-07-08"]],
            last_chunk_sequence=4,
            finalized_at=now,
            created_at=now,
            updated_at=now,
        )
        self.repo.upsert_analysis_summary(summary)
        response = get_analysis_summary(run_id=self.run_id, session=self.session)
        item = BacktestAnalysisSummaryItem.model_validate(response).model_dump(
            mode="json"
        )
        self.assertEqual(item["status"], "final")
        self.assertEqual(item["reporting_currency"], "CNY")
        self.assertEqual(item["initial_equity"], "10000")
        self.assertEqual(item["valid_day_count"], 5)
        self.assertEqual(item["rate_snapshot"]["rates"], {"2026-07-06": "0.02"})
        self.assertEqual(item["rate_snapshot_hash"], "sha256:rates")
        self.assertEqual(item["missing_ranges"], [["2026-07-07", "2026-07-08"]])
        self.assertIsNone(item["abort_reason"])

    def test_summary_endpoint_404_without_rows(self):
        with self.assertRaises(HTTPException) as caught:
            get_analysis_summary(run_id=self.run_id, session=self.session)
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
