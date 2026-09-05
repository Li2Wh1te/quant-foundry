"""Persistence tests for analyzer results: old/new schema compatibility,
metric uniqueness and producer conflicts, summary terminal states,
rate-snapshot JSONB payloads, and idempotent retries.

SQLite carries the repository behavior tests; the Alembic migration chain
is validated structurally (revision linkage and ORM/migration column
parity) because PostgreSQL is not available in this suite."""

from __future__ import annotations

import importlib
import unittest
from dataclasses import replace
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
from app.backtesting.analyzers import (
    build_fee_summary_spec,
    build_sharpe_config_rf_spec,
    build_sharpe_pit_rf_spec,
    build_sharpe_simple_spec,
    build_turnover_spec,
)

UTC = timezone.utc
SIGNING_KEY = "unit-test-signing-key"
CHECKPOINT_TOKEN = "sha256:" + "a" * 64
RATE_SNAPSHOT_HASH = "sha256:" + "3" * 64


def formal_spec_payloads():
    """Return the real frozen Registry payloads used by persistence fixtures."""

    return [
        builder().describe()
        for builder in (
            build_sharpe_simple_spec,
            build_sharpe_pit_rf_spec,
            build_sharpe_config_rf_spec,
            build_turnover_spec,
            build_fee_summary_spec,
        )
    ]


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
    if analyzer_metadata is None:
        semantics = (
            "applied_fill_count"
            if analyzer_key == "fee_summary"
            else "valid_end_of_day_equity_count"
            if analyzer_key == "turnover"
            else "candidate_return_count_including_zero_return_days"
        )
        analyzer_metadata = {
            "formula_signature": "sha256:" + "1" * 64,
            "input_evidence_signature": "sha256:" + "2" * 64,
            "contract_unit": "ratio",
            "sample_count_semantics": semantics,
            **(
                {
                    "valid_equity_day_count": sample_count,
                    "candidate_return_count": sample_count,
                    "annualization_factor": "252",
                    "std_ddof": 1,
                }
                if analyzer_key.startswith("sharpe_")
                else {}
            ),
            **(
                {
                    "rate_unit": "decimal_fraction",
                    "rate_convention": "simple_daily_rate",
                    "rate_effective_at": "session_date",
                    "rate_session_mapping": "exact_formal_session_date",
                    "rate_cutoff_boundary": (
                        "data_cutoff_at_not_after_session_open"
                    ),
                    "rate_data_cutoff_semantics": (
                        "data_cutoff_at_not_after_session_open"
                    ),
                    "rate_source_key": "unit_rf",
                    "rate_source_version": 1,
                    "rate_snapshot_hash": "sha256:" + "3" * 64,
                    "missing_ranges": [],
                }
                if analyzer_key == "sharpe_pit_rf"
                else {}
            ),
            **(
                {
                    "rf_annual": "0.02",
                    "rf_daily": (
                        "0.000079365079365079365079365079365079365079365079365079"
                    ),
                    "annual_rate_converter": "annual_rate_div_252@1",
                    "risk_free_rate_note": "unit config",
                }
                if analyzer_key == "sharpe_config_rf"
                else {}
            ),
            **(
                {"gross_traded_notional": "500", "fill_count": sample_count}
                if analyzer_key == "turnover"
                else {}
            ),
            **(
                {"reason_code": "INSUFFICIENT_RETURNS"}
                if unavailable_reason is not None
                else {}
            ),
            **(
                {
                    "gross_traded_notional": "500",
                    "cumulative_fees": "1.25",
                }
                if analyzer_key == "fee_summary"
                else {}
            ),
        }
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
        annualization_factor=(
            Decimal("252") if analyzer_key.startswith("sharpe_") else None
        ),
        risk_free_rate_note=(
            "unit config" if analyzer_key == "sharpe_config_rf" else None
        ),
        analyzer_metadata=analyzer_metadata,
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

    def _seed_terminal_summary(self) -> None:
        if self.repo.get_analysis_summary(self.run_id) is not None:
            return
        self.repo.upsert_analysis_summary(
            self._summary(
                status="final",
                valid_day_count=5,
                candidate_return_count=5,
                fill_count=5,
                gross_traded_notional=Decimal("500"),
                cumulative_fees=Decimal("1.25"),
                rate_snapshot={
                    "rate_unit": "decimal_fraction",
                    "rate_convention": "simple_daily_rate",
                    "effective_at": "session_date",
                    "session_mapping": "exact_formal_session_date",
                    "cutoff_boundary": "data_cutoff_at_not_after_session_open",
                    "data_cutoff_semantics": (
                        "data_cutoff_at_not_after_session_open"
                    ),
                },
                rate_snapshot_hash="sha256:" + "3" * 64,
                rate_source_versions={
                    "source_key": "unit_rf",
                    "source_version": 1,
                },
                missing_ranges=[],
                finalized_at=datetime.now(UTC),
            )
        )

    def _append_metrics(self, *dtos):
        self._seed_terminal_summary()
        return self.repo.append_metrics(*dtos)

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

    def test_generic_append_rejects_analyzer_bearing_metrics(self):
        from app.backtesting.result_repository import ResultRepositoryError

        first = make_metric(self.run_id)
        with self.assertRaises(ResultRepositoryError):
            self.repo.append("metrics", first)

    # -- producer consistency ---------------------------------------------

    def test_same_identity_second_producer_rejected(self):
        self._append_metrics(make_metric(self.run_id))
        impostor = make_metric(
            self.run_id,
            analyzer_key="sharpe_config_rf",
        )
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(impostor)

    def test_sharpe_abc_can_coexist_under_different_formula_versions(self):
        written = self._append_metrics(
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
            self._append_metrics(legacy_shaped)

    def test_append_metrics_requires_complete_frozen_output_metadata(self):
        incomplete = make_metric(
            self.run_id,
            unavailable_reason="样本不足",
            analyzer_metadata={"reason_code": "INSUFFICIENT_RETURNS"},
        )
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(incomplete)

    def test_formal_metrics_require_terminal_summary_and_matching_signatures(self):
        other_run = uuid4()
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append_metrics(make_metric(other_run))

        self._seed_terminal_summary()
        metric = make_metric(self.run_id)
        conflicting_metadata = dict(metric.analyzer_metadata)
        conflicting_metadata["formula_signature"] = "sha256:" + "9" * 64
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append_metrics(
                replace(metric, analyzer_metadata=conflicting_metadata)
            )

    def test_formal_metric_requires_membership_in_summary_specs(self):
        run_id = uuid4()
        self.repo.upsert_analysis_summary(
            replace(
                self._summary(status="final", finalized_at=datetime.now(UTC)),
                run_id=run_id,
                analyzer_snapshot={"specs": []},
            )
        )
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append_metrics(make_metric(run_id))

    def test_unavailable_reason_evidence_is_semantically_enforced(self):
        metric = make_metric(self.run_id, unavailable_reason="样本不足", sample_count=2)
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(metric)

        fee = make_metric(
            self.run_id,
            metric_key="fee_to_gross_traded_notional",
            formula_version="fee_to_gross_traded_notional_v1",
            unavailable_reason="毛成交额为 0",
            sample_count=5,
            analyzer_key="fee_summary",
            analyzer_metadata={
                "formula_signature": "sha256:" + "1" * 64,
                "input_evidence_signature": "sha256:" + "2" * 64,
                "contract_unit": "ratio",
                "sample_count_semantics": "applied_fill_count",
                "gross_traded_notional": "500",
                "cumulative_fees": "1.25",
                "reason_code": "ZERO_GROSS_TRADED_NOTIONAL",
            },
        )
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(fee)

    def test_fee_persistence_compares_raw_evidence_at_numeric_boundary(self):
        raw_fees = "0.9999999999999999996"
        raw_notional = "1.0000000000000000002"
        self.repo.upsert_analysis_summary(
            self._summary(
                status="final",
                fill_count=1,
                gross_traded_notional=raw_notional,
                cumulative_fees=raw_fees,
                finalized_at=datetime.now(UTC),
            )
        )
        common_metadata = {
            "formula_signature": "sha256:" + "1" * 64,
            "input_evidence_signature": "sha256:" + "2" * 64,
            "sample_count_semantics": "applied_fill_count",
            "gross_traded_notional": raw_notional,
            "cumulative_fees": raw_fees,
        }
        cumulative = BacktestMetricDto(
            run_id=self.run_id,
            metric_key="cumulative_fees",
            formula_version="cumulative_applied_fill_fees_v1",
            value="1.000000000000000000",
            unit="currency",
            sample_count=1,
            analyzer_key="fee_summary",
            analyzer_version=1,
            analyzer_metadata={**common_metadata, "contract_unit": "currency"},
        )
        ratio = BacktestMetricDto(
            run_id=self.run_id,
            metric_key="fee_to_gross_traded_notional",
            formula_version="fee_to_gross_traded_notional_v1",
            value="0.999999999999999999",
            unit="ratio",
            sample_count=1,
            analyzer_key="fee_summary",
            analyzer_version=1,
            analyzer_metadata={**common_metadata, "contract_unit": "ratio"},
        )
        self.assertEqual(self.repo.append_metrics(cumulative, ratio), 2)

    def test_summary_initial_equity_uses_numeric_persistence_boundary(self):
        summary = self._summary(initial_equity="10000.1234567890123456789")
        self.assertEqual(
            summary.initial_equity, Decimal("10000.123456789012345679")
        )
        with self.assertRaises(Exception):
            self._summary(initial_equity="100000000000000000000")

    def test_summary_dto_rejects_forged_or_negative_evidence(self):
        for overrides in (
            {"formula_signature": "sha256:forged"},
            {"initial_equity": Decimal("0")},
            {"valid_day_count": -1},
            {"candidate_return_count": 4, "valid_day_count": 3},
            {"gross_traded_notional": Decimal("-1")},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(Exception):
                self._summary(**overrides)

    def test_summary_nested_rate_evidence_is_deeply_frozen(self):
        versions = {"source_key": "rf", "source_version": 1, "nested": {"x": 1}}
        ranges = [
            {"start_session": "2026-07-08", "end_session": "2026-07-08"}
        ]
        summary = self._summary(
            rate_source_versions=versions,
            missing_ranges=ranges,
        )
        versions["nested"]["x"] = 9
        ranges[0]["start_session"] = "2020-01-01"
        self.assertEqual(summary.rate_source_versions["nested"]["x"], 1)
        self.assertEqual(
            summary.missing_ranges[0]["start_session"], "2026-07-08"
        )
        with self.assertRaises(Exception):
            self._summary(rate_source_versions={"weight": 0.1})
        with self.assertRaises(Exception):
            self._summary(missing_ranges=[["2026-07-08", "2026-07-08"]])

    def test_sharpe_static_formula_and_count_contract_is_enforced(self):
        metric = make_metric(self.run_id)
        invalid_metadata = dict(metric.analyzer_metadata)
        invalid_metadata["candidate_return_count"] = 4
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(
                replace(metric, analyzer_metadata=invalid_metadata)
            )
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(
                replace(metric, annualization_factor=Decimal("999"))
            )

    def test_rate_analyzer_frozen_contracts_are_enforced(self):
        configured = make_metric(
            self.run_id,
            formula_version="sharpe_config_rf_ddof1_252_v1",
            analyzer_key="sharpe_config_rf",
        )
        invalid_config = dict(configured.analyzer_metadata)
        invalid_config["annual_rate_converter"] = "wrong@99"
        invalid_config["rf_daily"] = "42"
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(
                replace(configured, analyzer_metadata=invalid_config)
            )
        wrong_note = dict(configured.analyzer_metadata)
        wrong_note["risk_free_rate_note"] = "different source"
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(
                replace(configured, analyzer_metadata=wrong_note)
            )
        with self.assertRaises(Exception):
            replace(configured, risk_free_rate_note="x" * 201)

        pit = make_metric(
            self.run_id,
            formula_version="sharpe_pit_rf_ddof1_252_v1",
            analyzer_key="sharpe_pit_rf",
        )
        invalid_pit = dict(pit.analyzer_metadata)
        invalid_pit["rate_convention"] = "continuous"
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(replace(pit, analyzer_metadata=invalid_pit))

    def test_conflicting_value_rewrite_rejected_but_identical_retry_idempotent(self):
        written = self._append_metrics(make_metric(self.run_id))
        self.assertEqual(written, 1)
        identical = self._append_metrics(make_metric(self.run_id))
        self.assertEqual(identical, 0)  # pure idempotent retry
        conflicting = make_metric(self.run_id, value="9.9")
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(conflicting)

    def test_batch_internal_duplicates_collapsed_or_conflicted(self):
        written = self._append_metrics(
            make_metric(self.run_id), make_metric(self.run_id)
        )
        self.assertEqual(written, 1)
        other = make_metric(
            self.run_id, metric_key="turnover",
            formula_version="turnover_gross_notional_avg_eod_equity_v1",
            analyzer_key="turnover",
        )
        conflicting_pair = (
            other,
            make_metric(
                self.run_id,
                metric_key="turnover",
                formula_version="turnover_gross_notional_avg_eod_equity_v1",
                analyzer_key="turnover",
                value="7",
            ),
        )
        with self.assertRaises(ResultRecordConflictError):
            self._append_metrics(*conflicting_pair)

    # -- analysis summaries ----------------------------------------------

    def _summary(self, status="partial", **overrides) -> BacktestAnalysisSummaryRecord:
        now = datetime.now(UTC)
        fields = dict(
            run_id=self.run_id,
            status=status,
            analyzer_snapshot={"specs": formal_spec_payloads()},
            formula_signature="sha256:" + "1" * 64,
            input_evidence_signature="sha256:" + "2" * 64,
            reporting_currency="cny",
            initial_equity=Decimal("10000"),
            valid_day_count=3,
            fill_count=1,
            gross_traded_notional=Decimal("500"),
            cumulative_fees=Decimal("1.25"),
            last_chunk_sequence=0,
            last_chunk_token=CHECKPOINT_TOKEN,
            completed_through_session=date(2026, 7, 8),
            created_at=now,
            updated_at=now,
        )
        fields.update(overrides)
        if status in ("final", "aborted"):
            fields.setdefault("terminal_fingerprint", "sha256:" + "0" * 64)
        return BacktestAnalysisSummaryRecord(**fields)

    def test_summary_partial_upsert_and_progress_update(self):
        stored = self.repo.upsert_analysis_summary(self._summary())
        self.assertIsNotNone(stored.id)
        advanced = self.repo.upsert_analysis_summary(
            self._summary(
                valid_day_count=4,
                last_chunk_sequence=1,
                last_chunk_token="sha256:" + "c" * 64,
                completed_through_session=date(2026, 7, 9),
            )
        )
        self.assertEqual(advanced.valid_day_count, 4)
        self.assertEqual(advanced.last_chunk_sequence, 1)

    def test_success_summary_rejects_incomplete_or_non_hash_checkpoint(self):
        with self.assertRaises(Exception):
            self._summary(last_chunk_token=None)
        with self.assertRaises(Exception):
            self._summary(last_chunk_token="not-a-hash")
        with self.assertRaises(ResultRecordConflictError):
            self.repo.upsert_analysis_summary(
                self._summary(last_chunk_sequence=1)
            )

    def test_partial_to_aborted_preserves_last_successful_checkpoint(self):
        partial = self._summary(
            last_chunk_sequence=0,
            last_chunk_token=CHECKPOINT_TOKEN,
            completed_through_session=date(2026, 7, 8),
        )
        self.repo.upsert_analysis_summary(partial)
        aborted = self.repo.upsert_analysis_summary(
            self._summary(
                status="aborted",
                last_chunk_sequence=0,
                last_chunk_token=CHECKPOINT_TOKEN,
                completed_through_session=date(2026, 7, 8),
                abort_reason="valuation blocked",
                failed_step_sequence=3,
                finalized_at=datetime.now(UTC),
            )
        )
        self.assertEqual(aborted.last_chunk_sequence, 0)
        self.assertEqual(aborted.last_chunk_token, CHECKPOINT_TOKEN)

    def test_aborted_summary_rejects_a_later_completed_session(self):
        self.repo.upsert_analysis_summary(self._summary())
        with self.assertRaises(ResultRecordConflictError):
            self.repo.upsert_analysis_summary(
                self._summary(
                    status="aborted",
                    completed_through_session=date(2026, 7, 9),
                    abort_reason="valuation blocked",
                    failed_step_sequence=3,
                    finalized_at=datetime.now(UTC),
                )
            )

    def test_same_sequence_conflicting_terminal_token_is_rejected(self):
        self.repo.upsert_analysis_summary(
            self._summary(
                last_chunk_sequence=0,
                last_chunk_token=CHECKPOINT_TOKEN,
                completed_through_session=date(2026, 7, 8),
            )
        )
        with self.assertRaises(ResultRecordConflictError):
            self.repo.upsert_analysis_summary(
                self._summary(
                    status="aborted",
                    last_chunk_sequence=0,
                    last_chunk_token="sha256:" + "b" * 64,
                    completed_through_session=date(2026, 7, 8),
                    abort_reason="valuation blocked",
                    failed_step_sequence=3,
                    finalized_at=datetime.now(UTC),
                )
            )

    def test_final_cannot_relabel_an_existing_partial_sequence(self):
        self.repo.upsert_analysis_summary(
            self._summary(
                last_chunk_sequence=0,
                last_chunk_token=CHECKPOINT_TOKEN,
                completed_through_session=date(2026, 7, 8),
            )
        )
        with self.assertRaises(ResultRecordConflictError):
            self.repo.upsert_analysis_summary(
                self._summary(
                    status="final",
                    last_chunk_sequence=0,
                    last_chunk_token=CHECKPOINT_TOKEN,
                    completed_through_session=date(2026, 7, 8),
                    finalized_at=datetime.now(UTC),
                )
            )

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
                rate_snapshot_hash=RATE_SNAPSHOT_HASH,
                rate_source_versions={
                    "source_key": "rf_source",
                    "source_version": 1,
                },
                missing_ranges=[
                    {
                        "start_session": "2026-07-08",
                        "end_session": "2026-07-08",
                    }
                ],
                finalized_at=datetime.now(UTC),
            )
        )
        read_back = self.repo.get_analysis_summary(self.run_id)
        assert read_back is not None
        self.assertEqual(stored.rate_snapshot_hash, RATE_SNAPSHOT_HASH)
        self.assertEqual(read_back.rate_snapshot["rates"], rates)
        self.assertEqual(
            read_back.rate_source_versions["source_key"], "rf_source"
        )
        # JSONB round-trips tuples as lists.
        self.assertEqual(
            [dict(item) for item in read_back.missing_ranges],
            [{"start_session": "2026-07-08", "end_session": "2026-07-08"}],
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
        retry_migration = importlib.import_module(
            "app.db.migrations.versions.20260825_02_add_analysis_terminal_and_chunk_fingerprints"
        )
        widening_migration = importlib.import_module(
            "app.db.migrations.versions.20260825_01_widen_metric_formula_version"
        )
        self.assertEqual(retry_migration.down_revision, widening_migration.revision)
        timeline_migration = importlib.import_module(
            "app.db.migrations.versions.20260825_04_add_formal_session_timeline"
        )
        self.assertEqual(timeline_migration.down_revision, "20260825_03")

        table = Base.metadata.tables["backtest_analysis_summaries"]
        expected = {
            "id", "run_id", "status", "analyzer_snapshot",
            "formula_signature", "input_evidence_signature",
            "initial_equity", "valid_day_count", "candidate_return_count", "fill_count",
            "formal_timeline",
            "gross_traded_notional", "cumulative_fees",
            "rate_snapshot", "rate_snapshot_hash",
            "rate_source_versions", "missing_ranges",
            "reporting_currency", "last_chunk_sequence",
            "last_chunk_token", "terminal_fingerprint",
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
