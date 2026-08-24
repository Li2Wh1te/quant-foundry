"""Tests for the aborted-analysis finalization boundary: fail-fast runner,
failure snapshot transfer, independent Session/transaction, re-raise of the
original exception, finalization errors, and idempotent terminal state."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.backtesting.analysis_finalization import (
    AnalysisFinalizationError,
    AnalysisFinalizer,
    unwrap_valuation_blocked_error,
)
from app.backtesting.analysis_inputs import InitialEquitySnapshot
from app.backtesting.analyzers import (
    AnalyzerEngine,
    build_fee_summary_spec,
    build_sharpe_simple_spec,
    build_turnover_spec,
)
from app.backtesting.result_models import (
    AnalysisSummaryStatus,
    BacktestMetricRecord,
)
from app.backtesting.result_records import (
    BacktestAnalysisSummaryRecord,
    BacktestMetricRecord as BacktestMetricOrm,
    Base,
    RESULT_TABLE_NAMES,
)

from tests.backtest_runtime_fixture import (
    INSTRUMENT_ID,
    CountingStrategyView,
    DictMarketData,
    ScriptedStrategy,
    build_axis,
    build_runner,
)

SESSION_DATES = [date(2026, 7, d) for d in (6, 7, 8, 9, 10)]
RUN_ID = "77777777-7777-4777-8777-777777777777"
FIXED_CUTOFF = datetime(2026, 7, 5, 20, 0, tzinfo=timezone.utc)


class FixedCutoffGateway:
    def data_cutoff_at(self, *, session_date: date, as_of: datetime) -> datetime:
        return FIXED_CUTOFF

    def risk_free_rate_snapshot(self, query):
        return ()


def shanghai_open(day: date) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=timezone.utc)


CLOSES = {
    SESSION_DATES[0]: "10",
    SESSION_DATES[1]: "10",
    SESSION_DATES[2]: "11",
    SESSION_DATES[3]: "11",
    # The last session lacks a close mark: while holding a position this
    # blocks the close valuation mid-run.
    SESSION_DATES[4]: "",
}

HOLD_TARGETS = {
    step: {str(INSTRUMENT_ID): "0.5"} for step in range(len(SESSION_DATES))
}


def make_engine() -> AnalyzerEngine:
    e0 = InitialEquitySnapshot(
        run_id=RUN_ID,
        session_date=SESSION_DATES[0],
        market_open_at=shanghai_open(SESSION_DATES[0]),
        valuation_as_of=datetime(2026, 7, 3, 7, 0, tzinfo=timezone.utc),
        data_cutoff_at=FIXED_CUTOFF,
        reporting_currency="CNY",
        cash="10000",
    )
    return AnalyzerEngine.create(
        e0,
        [
            build_sharpe_simple_spec(),
            build_turnover_spec(),
            build_fee_summary_spec(),
        ],
    )


def make_runner(engine: AnalyzerEngine):
    axis = build_axis(SESSION_DATES)
    quotes = {
        day: {INSTRUMENT_ID: (close or "10", close)} for day, close in CLOSES.items()
    }
    # A missing close is modeled by an empty string; keep the open present
    # so earlier sessions trade normally.
    quotes[SESSION_DATES[4]] = {INSTRUMENT_ID: ("11", None)}
    market_data = DictMarketData(
        {
            day: {
                instrument: (
                    (open_price, close_price)
                    if close_price is not None
                    else (open_price, "")
                )
                for instrument, (open_price, close_price) in inner.items()
            }
            for day, inner in quotes.items()
        }
    )
    return build_runner(
        run_id=RUN_ID,
        axis=axis,
        market_data=market_data,
        strategy_view=CountingStrategyView(
            {day: c for day, c in CLOSES.items() if c}
        ),
        strategy=ScriptedStrategy(HOLD_TARGETS),
        analysis_engine=engine,
        pit_data_gateway=FixedCutoffGateway(),
        initial_cash="10000",
    )


class SqliteHarness:
    """Shared in-memory SQLite database served through independent Sessions."""

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        # Create only the result tables: the shared metadata also carries
        # unrelated models whose checks use PostgreSQL-only functions.
        result_tables = [Base.metadata.tables[name] for name in RESULT_TABLE_NAMES]
        Base.metadata.create_all(self.engine, tables=result_tables)

    def session_factory(self) -> Session:
        return Session(self.engine)


class TestAnalysisFinalization(unittest.TestCase):
    def setUp(self):
        self.harness = SqliteHarness()
        self.engine = make_engine()
        self.runner = make_runner(self.engine)
        self.steps = list(build_axis(SESSION_DATES))

    def test_runner_stays_fail_fast_on_valuation_block(self):
        with self.assertRaises(Exception) as caught:
            self.runner.run_steps(tuple(self.steps), next_after_last=None)
        self.assertIsNotNone(
            unwrap_valuation_blocked_error(caught.exception),
            "the wrapped error must still be recognizable as a valuation block",
        )
        with self.assertRaises(Exception):
            # The failed runner accepts no further slices.
            self.runner.run_steps(tuple(self.steps[-1:]), next_after_last=None)

    def test_coordinator_persists_aborted_and_reraises_original(self):
        from app.backtesting.analysis_finalization import (
            AnalysisFinalizationCoordinator,
        )

        coordinator = AnalysisFinalizationCoordinator()
        with self.assertRaises(Exception) as caught:
            coordinator.execute_steps(
                self.runner,
                tuple(self.steps),
                next_after_last=None,
                session_factory=self.harness.session_factory,
            )
        original = caught.exception
        self.assertIsNotNone(unwrap_valuation_blocked_error(original))

        # Independent transaction evidence: brand-new session, real rows.
        with self.harness.session_factory() as session:
            summary = session.scalars(
                select(BacktestAnalysisSummaryRecord).where(
                    BacktestAnalysisSummaryRecord.run_id == UUID(RUN_ID)
                )
            ).one()
            self.assertEqual(summary.status, AnalysisSummaryStatus.ABORTED.value)
            self.assertIsNotNone(summary.abort_reason)
            self.assertEqual(summary.failed_step_sequence, len(SESSION_DATES) - 1)
            self.assertIsNotNone(summary.finalized_at)
            self.assertEqual(summary.reporting_currency, "CNY")
            # An A-run carries no rate snapshot evidence.
            self.assertIsNone(summary.rate_snapshot)
            metrics = session.scalars(
                select(BacktestMetricOrm).where(
                    BacktestMetricOrm.run_id == UUID(RUN_ID)
                )
            ).all()
            keys = sorted((m.metric_key, m.formula_version) for m in metrics)
            self.assertIn(("cumulative_fees", "cumulative_applied_fill_fees_v1"), keys)
            for metric in metrics:
                self.assertIsNotNone(metric.analyzer_key)
                self.assertIsNotNone(metric.analyzer_version)

    def test_repeated_aborted_finalization_is_idempotent(self):
        from app.backtesting.analysis_finalization import (
            AnalysisFinalizationCoordinator,
        )

        coordinator = AnalysisFinalizationCoordinator()
        with self.assertRaises(Exception) as first_caught:
            coordinator.execute_steps(
                self.runner,
                tuple(self.steps),
                next_after_last=None,
                session_factory=self.harness.session_factory,
            )
        snapshot = self.runner.build_analysis_failure_snapshot(first_caught.exception)
        finalizer = AnalysisFinalizer()
        second = finalizer.finalize_aborted(
            snapshot, self.harness.session_factory
        )

        with self.harness.session_factory() as session:
            metrics = session.scalars(
                select(BacktestMetricOrm).where(
                    BacktestMetricOrm.run_id == UUID(RUN_ID)
                )
            ).all()
            summaries = session.scalars(select(BacktestAnalysisSummaryRecord)).all()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(second.summary_id, summaries[0].id)
        # No duplicate metric rows were written by the retry.
        expected_keys = {(m.metric_key, m.formula_version) for m in metrics}
        self.assertEqual(len(metrics), len(expected_keys))
        self.assertGreaterEqual(second.persisted_metric_count, 0)

    def test_persistence_failure_raises_finalization_error(self):
        from app.backtesting.analysis_finalization import (
            AnalysisFinalizationCoordinator,
        )

        coordinator = AnalysisFinalizationCoordinator()

        class ExplodingFactory:
            def __call__(self) -> Session:
                raise RuntimeError("database unavailable")

        with self.assertRaises(Exception) as caught:
            coordinator.execute_steps(
                self.runner,
                tuple(self.steps),
                next_after_last=None,
                session_factory=ExplodingFactory(),
            )
        self.assertIsInstance(caught.exception, AnalysisFinalizationError)
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

        # Nothing was persisted; a later retry can still finalize because
        # the engine reuses its frozen aborted results idempotently.
        with self.harness.session_factory() as session:
            summaries = session.scalars(
                select(BacktestAnalysisSummaryRecord)
            ).all()
        self.assertEqual(summaries, [])
        retry = AnalysisFinalizer().finalize_aborted(
            self.runner.build_analysis_failure_snapshot(
                RuntimeError("valuation blocked (replayed)")
            ),
            self.harness.session_factory,
        )
        self.assertEqual(retry.persisted_metric_count, 4)

    def test_unrelated_errors_bypass_finalization(self):
        from app.backtesting.analysis_finalization import (
            AnalysisFinalizationCoordinator,
        )

        coordinator = AnalysisFinalizationCoordinator()

        class Broken:
            def run_steps(self, *args, **kwargs):
                raise ValueError("unrelated configuration problem")

            def build_analysis_failure_snapshot(self, exc):  # pragma: no cover
                raise AssertionError("must not be called")

        with self.assertRaises(ValueError):
            coordinator.execute_steps(Broken(), (object(),))
        self.assertIsNone(self.engine.finalized_status)


if __name__ == "__main__":
    unittest.main()
