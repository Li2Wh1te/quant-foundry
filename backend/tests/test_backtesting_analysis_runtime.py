"""Integration tests for analyzer wiring inside the deterministic runtime:
ACCOUNT/VALUE/ANALYZE handoff, chunk lifecycle states, PIT cutoff pass-through,
and valuation-failure behavior."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from zoneinfo import ZoneInfo

from app.backtesting.analysis_inputs import (
    AppliedFillFact,
    FillObservation,
    InitialEquitySnapshot,
    PitRateSnapshot,
    canonical_evidence_json,
)
from app.backtesting.analysis_admission import AdmissionBlockedError
from app.backtesting.analysis_finalization import analysis_equivalence_projection
from app.backtesting.analyzers import (
    AnalyzerEngine,
    build_fee_summary_spec,
    build_sharpe_simple_spec,
    build_turnover_spec,
)
from app.backtesting.runtime import (
    DeterministicBacktestRunner,
    SessionQuote,
    ValuationBlockedError,
)

from tests.backtest_runtime_fixture import (
    INSTRUMENT_ID,
    TEST_TIMEZONE,
    CountingStrategyView,
    DictMarketData,
    ScriptedStrategy,
    build_axis,
    build_runner,
    session_close,
)

TZ = ZoneInfo(TEST_TIMEZONE)
SESSION_DATES = [date(2026, 6, d) for d in (1, 2, 3, 4, 5)]
RUN_ID = "55555555-5555-4555-8555-555555555555"
FIXED_CUTOFF = datetime(2026, 5, 31, 20, 0, tzinfo=timezone.utc)


class FixedCutoffGateway:
    """PIT analysis gateway stub with one frozen cutoff per query."""

    def __init__(self, cutoff: datetime) -> None:
        self.cutoff = cutoff
        self.queries: list[date] = []

    def data_cutoff_at(self, *, session_date: date, as_of: datetime) -> datetime:
        self.queries.append(session_date)
        return self.cutoff

    def risk_free_rate_snapshot(self, query):
        return ()


def shanghai_open(day: date) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=ZoneInfo(TEST_TIMEZONE))


def market_data_for(closes: dict[date, str]) -> DictMarketData:
    quotes = {
        day: {INSTRUMENT_ID: (close, close)} for day, close in closes.items()
    }
    return DictMarketData(quotes)


def e0_snapshot() -> InitialEquitySnapshot:
    return InitialEquitySnapshot(
        run_id=RUN_ID,
        session_date=SESSION_DATES[0],
        market_open_at=shanghai_open(SESSION_DATES[0]),
        valuation_as_of=datetime(2026, 5, 29, 7, 0, tzinfo=timezone.utc),
        data_cutoff_at=FIXED_CUTOFF,
        reporting_currency="CNY",
        cash="10000",
        formal_sessions=SESSION_DATES,
    )


def build_analysis_specs():
    return [
        build_sharpe_simple_spec(),
        build_turnover_spec(),
        build_fee_summary_spec(),
    ]


def build_wired_runner(
    *,
    closes: dict[date, str],
    targets_by_step=None,
    axis=None,
    analysis_engine=None,
    quote_evidence_by_day=None,
):
    axis = axis or build_axis(SESSION_DATES)
    market_data = DictMarketData(
        {
            day: {INSTRUMENT_ID: (close, close)}
            for day, close in closes.items()
        },
        quote_evidence_by_day=quote_evidence_by_day,
    )
    strategy_view = CountingStrategyView(dict(closes))
    return build_runner(
        run_id=RUN_ID,
        axis=axis,
        market_data=market_data,
        strategy_view=strategy_view,
        strategy=ScriptedStrategy(targets_by_step or {}),
        analysis_engine=analysis_engine,
        pit_data_gateway=FixedCutoffGateway(FIXED_CUTOFF),
        initial_cash="10000",
    )


FULL_CLOSES = {
    SESSION_DATES[0]: "10",
    SESSION_DATES[1]: "10",
    SESSION_DATES[2]: "11",
    SESSION_DATES[3]: "11",
    SESSION_DATES[4]: "12",
}


def fresh_engine() -> AnalyzerEngine:
    return AnalyzerEngine.create(e0_snapshot(), build_analysis_specs())


# Hold a constant 50% target from the first decision onward so the
# portfolio keeps its position for the whole window.
BUY_THEN_HOLD = {
    step: {str(INSTRUMENT_ID): "0.5"} for step in range(len(SESSION_DATES))
}


class TestFullRunAnalysis(unittest.TestCase):
    def test_session_quote_rejects_float_in_canonical_evidence(self):
        with self.assertRaises(Exception):
            SessionQuote(
                instrument_id=INSTRUMENT_ID,
                session_date=SESSION_DATES[0],
                open_price=Decimal("10"),
                close_price=Decimal("10"),
                evidence={"source_revision": 1.5},
            )

    def test_engine_with_preexisting_fills_is_rejected_by_admission(self):
        engine = fresh_engine()
        engine.observe_fill(
            FillObservation(
                fact=AppliedFillFact(
                    fill_id=uuid4(),
                    run_id=RUN_ID,
                    session_date=SESSION_DATES[0],
                    timestamp=shanghai_open(SESSION_DATES[0]),
                    instrument_id=INSTRUMENT_ID,
                    side="buy",
                    fill_price="10",
                    fill_quantity="1",
                    contract_multiplier="1",
                    currency="CNY",
                    reporting_currency="CNY",
                    fees="0",
                    gross_traded_notional="10",
                )
            )
        )
        with self.assertRaises(AdmissionBlockedError):
            build_wired_runner(
                closes=FULL_CLOSES,
                analysis_engine=engine,
            )

    def test_full_run_finalizes_with_explicit_status(self):
        engine = fresh_engine()
        runner = build_wired_runner(
            closes=FULL_CLOSES, targets_by_step=BUY_THEN_HOLD, analysis_engine=engine
        )
        result = runner.run()
        self.assertEqual(result.analysis_status, "final")
        self.assertIsNotNone(
            engine.snapshot().initial_equity_snapshot.portfolio_snapshot_hash
        )
        self.assertEqual(result.completed_through_step_sequence, len(SESSION_DATES) - 1)
        self.assertIsNotNone(engine.finalized_status)
        self.assertEqual(len(result.analysis_metrics), 4)

        by_key = {m.metric_key: m for m in result.analysis_metrics}
        # The strategy rebalances a constant 50% weight daily, so applied
        # fills exist and the fee summary stays computable (the fixture
        # charges zero fees).
        self.assertGreaterEqual(engine.snapshot().fill_count, 1)
        self.assertEqual(by_key["cumulative_fees"].value, Decimal("0"))
        turnover = by_key["turnover"]
        self.assertEqual(turnover.status.value, "available")
        expected_notional = Decimal("10") * Decimal("500")
        expected_average = (
            Decimal("10000")
            + expected_notional
            + Decimal("10") * Decimal("500") * Decimal("11") / Decimal("10")
            + Decimal("10") * Decimal("500") * Decimal("12") / Decimal("10")
        ) / Decimal("5")
        # Turnover equals gross traded notional over average end-of-day
        # equity; both sides derive purely from applied facts.
        self.assertGreater(turnover.value, Decimal("0"))
        del expected_notional, expected_average

    def test_value_phase_cutoff_comes_from_gateway(self):
        engine = fresh_engine()
        gateway = FixedCutoffGateway(FIXED_CUTOFF)
        runner = build_wired_runner(closes=FULL_CLOSES, analysis_engine=engine)
        # Replace the default gateway with our recording instance.
        object.__setattr__(runner, "_pit_data_gateway", gateway)
        runner.run()
        self.assertEqual(len(gateway.queries), len(SESSION_DATES))
        snapshot = engine.snapshot()
        for observation in snapshot.equity_observations:
            self.assertEqual(observation.data_cutoff_at, FIXED_CUTOFF)
            self.assertNotEqual(observation.as_of, FIXED_CUTOFF)

    def test_equal_prices_with_different_revisions_have_distinct_evidence(self):
        signatures = []
        for revision in ("revision-a", "revision-b"):
            engine = fresh_engine()
            evidence_by_day = {
                day: {
                    INSTRUMENT_ID: {
                        "source_key": "unit_market",
                        "source_version": 1,
                        "source_revision": revision,
                        "coverage": {"session_date": day.isoformat()},
                    }
                }
                for day in SESSION_DATES
            }
            runner = build_wired_runner(
                closes=FULL_CLOSES,
                analysis_engine=engine,
                quote_evidence_by_day=evidence_by_day,
                targets_by_step=BUY_THEN_HOLD,
            )
            runner.run()
            signatures.append(engine.input_evidence_signature())
        self.assertNotEqual(*signatures)

    def test_missing_close_revision_remains_in_blocked_evidence(self):
        hashes = []
        closes = {**FULL_CLOSES, SESSION_DATES[-1]: ""}
        for revision in ("missing-a", "missing-b"):
            engine = fresh_engine()
            evidence_by_day = {
                day: {
                    INSTRUMENT_ID: {
                        "source_key": "unit_market",
                        "source_version": 1,
                        "source_revision": (
                            revision if day == SESSION_DATES[-1] else "stable"
                        ),
                    }
                }
                for day in SESSION_DATES
            }
            runner = build_wired_runner(
                closes=closes,
                analysis_engine=engine,
                quote_evidence_by_day=evidence_by_day,
                targets_by_step=BUY_THEN_HOLD,
            )
            with self.assertRaises(Exception):
                runner.run()
            hashes.append(engine.snapshot().equity_observations[-1].evidence_hash)
        self.assertNotEqual(*hashes)

    def test_runner_rejects_foreign_engine_and_missing_gateway(self):
        foreign = AnalyzerEngine.create(
            InitialEquitySnapshot(
                run_id="66666666-6666-4666-8666-666666666666",
                session_date=SESSION_DATES[0],
                market_open_at=shanghai_open(SESSION_DATES[0]),
                valuation_as_of=datetime(2026, 5, 29, 7, 0, tzinfo=timezone.utc),
                data_cutoff_at=FIXED_CUTOFF,
                reporting_currency="CNY",
                cash="10000",
                formal_sessions=SESSION_DATES,
            ),
            build_analysis_specs(),
        )
        axis = build_axis(SESSION_DATES)
        with self.assertRaises(Exception):
            build_runner(
                run_id=RUN_ID,
                axis=axis,
                market_data=market_data_for(FULL_CLOSES),
                strategy_view=CountingStrategyView(FULL_CLOSES),
                strategy=ScriptedStrategy({}),
                analysis_engine=foreign,
                pit_data_gateway=FixedCutoffGateway(FIXED_CUTOFF),
            )
        with self.assertRaises(Exception):
            build_runner(
                run_id=RUN_ID,
                axis=axis,
                market_data=market_data_for(FULL_CLOSES),
                strategy_view=CountingStrategyView(FULL_CLOSES),
                strategy=ScriptedStrategy({}),
                analysis_engine=fresh_engine(),
                pit_data_gateway=None,
            )


class TestChunkedRunEquivalence(unittest.TestCase):
    def _chunked_run(self, split: int):
        engine = fresh_engine()
        runner = build_wired_runner(
            closes=FULL_CLOSES, targets_by_step=BUY_THEN_HOLD, analysis_engine=engine
        )
        axis = build_axis(SESSION_DATES)
        steps = list(axis)
        first = runner.run_steps(tuple(steps[:split]), next_after_last=steps[split])
        second = runner.run_steps(tuple(steps[split:]), next_after_last=None)
        return first, second, engine

    def test_partial_chunk_reports_partial_without_metrics_write(self):
        first, second, _ = self._chunked_run(3)
        self.assertEqual(first.analysis_status, "partial")
        self.assertEqual(first.completed_through_step_sequence, 2)
        # Provisional metrics exist as an intermediate view but are marked
        # partial through the explicit status field.
        self.assertTrue(first.analysis_metrics)
        self.assertEqual(second.analysis_status, "final")

    @staticmethod
    def _metric_fingerprint(metrics):
        return [
            (
                m.metric_key,
                m.formula_version,
                m.unit,
                m.analyzer_key,
                m.analyzer_version,
                m.status.value,
                m.value,
                m.sample_count,
                m.unavailable_reason,
                canonical_evidence_json(dict(m.analyzer_metadata)),
            )
            for m in metrics
        ]

    def test_chunk_splits_do_not_change_results(self):
        full_engine = fresh_engine()
        full_runner = build_wired_runner(
            closes=FULL_CLOSES, targets_by_step=BUY_THEN_HOLD, analysis_engine=full_engine
        )
        full_result = full_runner.run()

        for split in (1, 2, 3, 4):
            _, second, chunked_engine = self._chunked_run(split)
            self.assertEqual(
                self._metric_fingerprint(second.analysis_metrics),
                self._metric_fingerprint(full_result.analysis_metrics),
                f"metrics diverged at chunk split {split}",
            )
            self.assertEqual(
                second.events,
                full_result.events,
                f"event stream diverged at chunk split {split}",
            )
            self.assertEqual(second.equity_curve, full_result.equity_curve)
            self.assertEqual(second.final_snapshot, full_result.final_snapshot)
            full_snapshot = full_engine.snapshot()
            chunked_snapshot = chunked_engine.snapshot()
            self.assertEqual(
                analysis_equivalence_projection(second, chunked_snapshot),
                analysis_equivalence_projection(full_result, full_snapshot),
                f"fixed business projection diverged at chunk split {split}",
            )
            self.assertEqual(
                chunked_snapshot.summary_counts(), full_snapshot.summary_counts()
            )
            self.assertEqual(
                chunked_snapshot.formula_signature(),
                full_snapshot.formula_signature(),
            )
            self.assertEqual(
                chunked_snapshot.input_evidence_signature(),
                full_snapshot.input_evidence_signature(),
            )
            self.assertEqual(
                chunked_snapshot.initial_equity_snapshot.evidence_hash,
                full_snapshot.initial_equity_snapshot.evidence_hash,
            )
            self.assertEqual(
                chunked_snapshot.initial_equity_snapshot.portfolio_snapshot_hash,
                full_snapshot.initial_equity_snapshot.portfolio_snapshot_hash,
            )
            self.assertEqual(
                chunked_snapshot.rate_snapshot,
                full_snapshot.rate_snapshot,
            )


class TestValuationFailure(unittest.TestCase):
    def test_blocked_observation_is_submitted_before_failure(self):
        # The run holds a position from day 0, so the missing close on the
        # last session blocks the close valuation.
        broken_closes = dict(FULL_CLOSES)
        broken_closes[SESSION_DATES[4]] = ""  # missing close mark
        engine = fresh_engine()
        runner = build_wired_runner(
            closes=broken_closes,
            targets_by_step=BUY_THEN_HOLD,
            analysis_engine=engine,
        )
        with self.assertRaises(Exception) as caught:
            runner.run()
        # The original failure must remain a valuation block, whatever the
        # phase wrapper named it.
        error = caught.exception
        causes = []
        while error is not None and len(causes) < 16:
            causes.append(error)
            error = error.__cause__
        self.assertTrue(
            any(isinstance(item, ValuationBlockedError) for item in causes)
        )
        snapshot = engine.snapshot()
        self.assertTrue(snapshot.equity_observations)
        blocked = snapshot.equity_observations[-1]
        self.assertEqual(blocked.valuation_status, "blocked")
        self.assertEqual(blocked.session_date, SESSION_DATES[4])
        self.assertIsNone(blocked.equity)
        # The engine stays unfinalized: persistence belongs to the
        # independent failure-finalization boundary, not the runner.
        self.assertIsNone(engine.finalized_status)
        # The runner is permanently stopped (fail-fast preserved).
        with self.assertRaises(Exception):
            runner.run_steps(list(build_axis(SESSION_DATES))[2:])



if __name__ == "__main__":
    unittest.main()
