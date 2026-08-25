"""Persistence tests for analyzer results: old/new schema compatibility,
metric uniqueness and producer conflicts, summary terminal states,
rate-snapshot JSONB payloads, and idempotent retries.

SQLite carries the repository behavior tests; the Alembic migration chain
is validated structurally (revision linkage and ORM/migration column
parity) because PostgreSQL is not available in this suite."""

from __future__ import annotations

import importlib
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.backtesting.result_models import (
    AnalysisSummaryStatus,
    AnalyzerState,
    BacktestAnalysisSummaryRecord,
    BacktestMetricRecord as BacktestMetricDto,
)
from app.backtesting.result_records import (
    BacktestAnalysisSummaryRecord as SummaryOrm,
    BacktestMetricRecord as MetricOrm,
    RESULT_TABLE_NAMES,
    Base,
)
from app.backtesting.result_repository import (
    BacktestResultRepository,
    ResultRecordConflictError,
)

UTC = timezone.utc
SIGNING_KEY = "unit-test-signing-key"


def make_metric(
    run_id,
    *,
    metric_key="sharpe",
    formula_version="sharpe_simple_ddof1_252_v1",
    value="1.5",
    unavailable_reason=None,
    sample_count=5,
    analyzer_key="sharpe_simple",
    analyzer_version=1,
    analyzer_metadata=None,
) -> BacktestMetricDto:
    return BacktestMetricDto(
        run_id=run_id,
        metric_key=metric_key,
        formula_version=formula_version,
        value=value if unavailable_reason is None else None,
        unit="ratio",
        sample_count=sample_count,
        unavailable_reason=unavailable_reason,
        analyzer_key=analyzer_key,
        analyzer_version=analyzer_version,
        analyzer_metadata=analyzer_metadata or {"reason_code": "x"}
        if unavailable_reason is not None
        else analyzer_metadata,
    )


class PersistenceTestCase(unittest.TestCase):
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

    # -- schema / legacy -------------------------------------------------

    def test_legacy_row_reads_as_legacy_state(self):
        legacy = BacktestMetricDto(
            run_id=self.run_id,
            metric_key="sharpe",
            formula_version="pre_analyzer_v0",
            value="2",
        )
        self.assertEqual(legacy.analyzer_state, AnalyzerState.LEGACY)
        self.repo.append("metrics", legacy)
        page = self.repo.read_page("metrics", run_id=self.run_id)
        row = page.items[0]
        self.assertIsNone(row.analyzer_key)
        self.assertIsNone(row.analyzer_version)
        self.assertEqual(row.analyzer_state, AnalyzerState.LEGACY.value)

    def test_unresolvable_identity_reads_as_unknown(self):
        orphan = make_metric(self.run_id, analyzer_version=9)
        self.assertEqual(orphan.analyzer_state, AnalyzerState.UNKNOWN)

    def test_identity_pair_is_enforced_on_the_dto(self):
        with self.assertRaises(Exception):
            BacktestMetricDto(
                run_id=self.run_id,
                metric_key="sharpe",
                formula_version="v",
                value="1",
                analyzer_key="sharpe_simple",
            )

    def test_unique_key_unchanged_for_metrics(self):
        first = make_metric(self.run_id)
        duplicate = make_metric(self.run_id)
        # The generic append path still enforces the run-scoped unique key.
        self.repo.append("metrics", first)
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append("metrics", duplicate)

    # -- producer consistency ---------------------------------------------

    def test_same_identity_second_producer_rejected(self):
        self.repo.append_metrics(make_metric(self.run_id))
        impostor = make_metric(
            self.run_id,
            analyzer_key="sharpe_config_rf",
        )
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append_metrics(impostor)

    def test_sharpe_abc_can_coexist_under_different_formula_versions(self):
        written = self.repo.append_metrics(
            make_metric(self.run_id),
            make_metric(
                self.run_id,
                formula_version="sharpe_pit_rf_ddof1_252_v1",
                analyzer_key="sharpe_pit_rf",
            ),
            make_metric(
                self.run_id,
                formula_version="sharpe_config_rf_ddof1_252_v1",
                analyzer_key="sharpe_config_rf",
                value="0.9",
            ),
        )
        self.assertEqual(written, 3)
        page = self.repo.read_page("metrics", run_id=self.run_id, limit=10)
        producers = {
            (row.metric_key, row.formula_version): (
                row.analyzer_key,
                row.analyzer_version,
            )
            for row in page.items
        }
        self.assertEqual(len(producers), 3)
        self.assertEqual(
            producers[("sharpe", "sharpe_config_rf_ddof1_252_v1")][0],
            "sharpe_config_rf",
        )

    def test_append_metrics_requires_analyzer_identity(self):
        from app.backtesting.result_repository import ResultRepositoryError

        legacy_shaped = BacktestMetricDto(
            run_id=self.run_id,
            metric_key="sharpe",
            formula_version="v",
            value="1",
        )
        with self.assertRaises(ResultRepositoryError):
            self.repo.append_metrics(legacy_shaped)

    def test_conflicting_value_rewrite_rejected_but_identical_retry_idempotent(self):
        written = self.repo.append_metrics(make_metric(self.run_id))
        self.assertEqual(written, 1)
        identical = self.repo.append_metrics(make_metric(self.run_id))
        self.assertEqual(identical, 0)  # pure idempotent retry
        conflicting = make_metric(self.run_id, value="9.9")
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append_metrics(conflicting)

    def test_batch_internal_duplicates_collapsed_or_conflicted(self):
        written = self.repo.append_metrics(
            make_metric(self.run_id), make_metric(self.run_id)
        )
        self.assertEqual(written, 1)
        other = make_metric(
            self.run_id, metric_key="turnover",
            formula_version="turnover_gross_notional_avg_eod_equity_v1",
        )
        conflicting_pair = (
            other,
            make_metric(
                self.run_id,
                metric_key="turnover",
                formula_version="turnover_gross_notional_avg_eod_equity_v1",
                value="7",
            ),
        )
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append_metrics(*conflicting_pair)

    # -- analysis summaries ----------------------------------------------

    def _summary(self, status="partial", **overrides) -> BacktestAnalysisSummaryRecord:
        now = datetime.now(UTC)
        fields = dict(
            run_id=self.run_id,
            status=status,
            analyzer_snapshot={"specs": []},
            formula_signature="sha256:formula",
            input_evidence_signature="sha256:evidence",
            reporting_currency="cny",
            initial_equity=Decimal("10000"),
            valid_day_count=3,
            fill_count=1,
            gross_traded_notional=Decimal("500"),
            cumulative_fees=Decimal("1.25"),
            last_chunk_sequence=2,
            created_at=now,
            updated_at=now,
        )
        fields.update(overrides)
        return BacktestAnalysisSummaryRecord(**fields)

    def test_summary_partial_upsert_and_progress_update(self):
        stored = self.repo.upsert_analysis_summary(self._summary())
        self.assertIsNotNone(stored.id)
        advanced = self.repo.upsert_analysis_summary(
            self._summary(valid_day_count=4, last_chunk_sequence=3)
        )
        self.assertEqual(advanced.valid_day_count, 4)
        self.assertEqual(advanced.last_chunk_sequence, 3)

    def test_terminal_summary_is_protected(self):
        finalized_at = datetime.now(UTC)
        final = self.repo.upsert_analysis_summary(
            self._summary(status="final", finalized_at=finalized_at)
        )
        # Identical retry returns the persisted row.
        retry = self.repo.upsert_analysis_summary(
            self._summary(status="final", finalized_at=finalized_at)
        )
        self.assertEqual(retry.id, final.id)
        # Any conflicting write (even a partial) is rejected.
        with self.assertRaises(ResultRecordConflictError):
            self.repo.upsert_analysis_summary(self._summary(status="partial"))
        with self.assertRaises(ResultRecordConflictError):
            self.repo.upsert_analysis_summary(
                self._summary(
                    status="aborted",
                    abort_reason="other",
                    finalized_at=datetime.now(UTC),
                )
            )

    def test_aborted_summary_requires_reason(self):
        with self.assertRaises(Exception):
            self._summary(status="aborted")

    def test_rate_snapshot_jsonb_round_trip(self):
        rates = {f"2026-07-{day:02d}": "0.02" for day in range(6, 11)}
        payload = {
            "rates": rates,
            "coverage_start": "2026-07-06",
            "coverage_end": "2026-07-10",
            "query_parameters": {"tenor": "1Y"},
        }
        stored = self.repo.upsert_analysis_summary(
            self._summary(
                status="final",
                rate_snapshot=payload,
                rate_snapshot_hash="sha256:rates",
                rate_source_versions={
                    "source_key": "rf_source",
                    "source_version": 1,
                },
                missing_ranges=[["2026-07-08", "2026-07-08"]],
                finalized_at=datetime.now(UTC),
            )
        )
        read_back = self.repo.get_analysis_summary(self.run_id)
        assert read_back is not None
        self.assertEqual(stored.rate_snapshot_hash, "sha256:rates")
        self.assertEqual(read_back.rate_snapshot["rates"], rates)
        self.assertEqual(
            read_back.rate_source_versions["source_key"], "rf_source"
        )
        # JSONB round-trips tuples as lists.
        self.assertEqual(
            list(read_back.missing_ranges), [["2026-07-08", "2026-07-08"]]
        )

    def test_a_c_runs_allow_empty_rate_snapshot(self):
        stored = self.repo.get_analysis_summary(self.run_id)
        self.assertIsNone(stored)
        self.repo.upsert_analysis_summary(self._summary())
        stored = self.repo.get_analysis_summary(self.run_id)
        self.assertIsNone(stored.rate_snapshot)
        self.assertIsNone(stored.rate_snapshot_hash)


class MigrationChainTestCase(unittest.TestCase):
    """Structural validation of the additive migration."""

    def test_migration_revision_chain_and_columns(self):
        migration = importlib.import_module(
            "app.db.migrations.versions.20260824_01_add_backtest_analysis"
        )
        previous = importlib.import_module(
            "app.db.migrations.versions.20260823_01_add_backtest_fill_audit_columns"
        )
        self.assertEqual(migration.down_revision, previous.revision)

        table = Base.metadata.tables["backtest_analysis_summaries"]
        expected = {
            "id", "run_id", "status", "analyzer_snapshot",
            "formula_signature", "input_evidence_signature",
            "initial_equity", "valid_day_count", "fill_count",
            "gross_traded_notional", "cumulative_fees",
            "rate_snapshot", "rate_snapshot_hash",
            "rate_source_versions", "missing_ranges",
            "reporting_currency", "last_chunk_sequence",
            "completed_through_session", "abort_reason",
            "failed_step_sequence", "created_at", "updated_at",
            "finalized_at",
        }
        self.assertEqual(set(table.columns.keys()), expected)
        metrics = Base.metadata.tables["backtest_metrics"]
        for column in ("analyzer_key", "analyzer_version", "analyzer_metadata"):
            self.assertIn(column, metrics.columns)


if __name__ == "__main__":
    unittest.main()
