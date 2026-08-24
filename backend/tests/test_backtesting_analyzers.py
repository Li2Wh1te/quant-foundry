"""Unit tests for analyzer protocols, registry resolution, formulas,
Decimal policy, sample counts, and fixed unavailability reason codes."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.backtesting.analysis_inputs import (
    AppliedFillFact,
    EquityObservation,
    FillObservation,
    InitialEquitySnapshot,
    InitialHolding,
    PitRateSnapshot,
)
from app.backtesting.analyzers import (
    ANNUALIZATION_FACTOR,
    AnalysisStateConflictError,
    AnalyzerConfigurationError,
    AnalyzerEngine,
    AnalyzerSpec,
    MetricOutputDescriptor,
    ReasonCode,
    build_fee_summary_spec,
    build_sharpe_config_rf_spec,
    build_sharpe_pit_rf_spec,
    build_sharpe_simple_spec,
    build_turnover_spec,
)
from app.backtesting.domain import DomainValidationError
from app.backtesting.registry import (
    ANALYZER_COMPONENT_KIND,
    ANNUAL_RATE_CONVERTER_COMPONENT_KIND,
    UnknownComponentError,
    build_default_component_registry,
)

RUN_ID = "22222222-2222-4222-8222-222222222222"
OPEN_AT = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
CLOSE_PREV = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)
INSTRUMENT = UUID("33333333-3333-4333-8333-333333333333")

SESSIONS = [date(2026, 1, d) for d in (5, 6, 7, 8, 9)]


def e0(cash: str = "100000") -> InitialEquitySnapshot:
    return InitialEquitySnapshot(
        run_id=RUN_ID,
        session_date=SESSIONS[0],
        market_open_at=OPEN_AT,
        valuation_as_of=CLOSE_PREV,
        data_cutoff_at=CUTOFF,
        reporting_currency="CNY",
        cash=cash,
    )


def observation(
    session_date: date,
    equity: str | None,
    *,
    step: int = 0,
    status: str = "valid",
    cash: str = "50000",
) -> EquityObservation:
    return EquityObservation(
        run_id=RUN_ID,
        step_sequence=step,
        session_date=session_date,
        as_of=datetime.combine(
            session_date, time(7, 0), tzinfo=timezone.utc
        ),
        valuation_status=status,
        data_cutoff_at=CUTOFF,
        reporting_currency="CNY",
        cash=cash,
        equity=equity,
        cumulative_fees="0",
        valuation_reason=None if status == "valid" else "missing marks",
    )


def fill_observation(fees: str = "5", notional: str | None = None) -> FillObservation:
    fact = AppliedFillFact(
        fill_id=uuid4(),
        run_id=RUN_ID,
        session_date=SESSIONS[0],
        timestamp=datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc),
        instrument_id=INSTRUMENT,
        side="buy",
        fill_price="10",
        fill_quantity="100",
        contract_multiplier="1",
        currency="CNY",
        reporting_currency="CNY",
        fees=fees,
        gross_traded_notional=notional,
    )
    return FillObservation(fact=fact)


def observe_days(engine: AnalyzerEngine, equities: list[str]) -> None:
    for index, (day, equity) in enumerate(zip(SESSIONS, equities)):
        engine.observe_equity(observation(day, equity, step=index))


class TestRegistryResolution(unittest.TestCase):
    def test_six_v1_identities_are_registered(self):
        registry = build_default_component_registry()
        expected = {
            ("sharpe_simple", 1): ANALYZER_COMPONENT_KIND,
            ("sharpe_pit_rf", 1): ANALYZER_COMPONENT_KIND,
            ("sharpe_config_rf", 1): ANALYZER_COMPONENT_KIND,
            ("turnover", 1): ANALYZER_COMPONENT_KIND,
            ("fee_summary", 1): ANALYZER_COMPONENT_KIND,
            ("annual_rate_div_252", 1): ANNUAL_RATE_CONVERTER_COMPONENT_KIND,
        }
        for (key, version), kind in expected.items():
            entry = registry.resolve(key, version)
            self.assertEqual(entry.component_kind, kind)

    def test_unknown_version_never_falls_back(self):
        registry = build_default_component_registry()
        with self.assertRaises(UnknownComponentError):
            registry.resolve("sharpe_simple", 2)
        # The @1 display notation is never itself a resolvable key.
        with self.assertRaises(UnknownComponentError):
            registry.resolve("sharpe_simple@1", 1)

    def test_spec_factories_produce_frozen_contracts(self):
        registry = build_default_component_registry()
        spec = registry.resolve("fee_summary", 1).construct({})
        keys = spec.output_metric_keys
        self.assertEqual(keys, ("cumulative_fees", "fee_to_gross_traded_notional"))
        converter = registry.resolve("annual_rate_div_252", 1).construct({})
        from decimal import localcontext, Context, ROUND_HALF_EVEN

        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            expected = Decimal("0.03") / ANNUALIZATION_FACTOR
        self.assertEqual(converter.compute("0.03"), expected)


class TestInitialEquitySnapshot(unittest.TestCase):
    def test_valuation_must_strictly_precede_open(self):
        with self.assertRaises(DomainValidationError):
            InitialEquitySnapshot(
                run_id=RUN_ID,
                session_date=SESSIONS[0],
                market_open_at=OPEN_AT,
                valuation_as_of=OPEN_AT,
                data_cutoff_at=CUTOFF,
                reporting_currency="CNY",
                cash="1000",
            )

    def test_non_positive_e0_is_rejected(self):
        with self.assertRaises(DomainValidationError):
            e0(cash="0")
        with self.assertRaises(DomainValidationError):
            e0(cash="-5")

    def test_holding_currency_must_match_reporting_currency(self):
        with self.assertRaises(DomainValidationError):
            InitialEquitySnapshot(
                run_id=RUN_ID,
                session_date=SESSIONS[0],
                market_open_at=OPEN_AT,
                valuation_as_of=CLOSE_PREV,
                data_cutoff_at=CUTOFF,
                reporting_currency="CNY",
                cash="1000",
                holdings=[
                    InitialHolding(
                        instrument_id=INSTRUMENT,
                        quantity="100",
                        currency="USD",
                        close_price="10",
                    )
                ],
            )


class TestSharpeFormulas(unittest.TestCase):
    def test_sharpe_a_includes_zero_return_day_and_reports_sample_count(self):
        engine = AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()])
        observe_days(engine, ["101000", "101000", "102010", "99989.9", "103989.207"])
        results = engine.finalize("final")
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.metric_key, "sharpe")
        self.assertEqual(result.formula_version, "sharpe_simple_ddof1_252_v1")
        self.assertEqual(result.analyzer_key, "sharpe_simple")
        self.assertEqual(result.analyzer_version, 1)
        self.assertEqual(result.status.value, "available")
        self.assertEqual(result.sample_count, 5)
        self.assertEqual(result.unit, "ratio")
        # Deterministic value recomputed under the frozen Decimal policy.
        from decimal import localcontext, Context, ROUND_HALF_EVEN

        equities = [Decimal(x) for x in
                    ["100000", "101000", "101000", "102010", "99989.9", "103989.207"]]
        returns = [b / a - 1 for a, b in zip(equities, equities[1:])]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            expected = mean / variance.sqrt() * ANNUALIZATION_FACTOR.sqrt()
            expected = expected.quantize(
                Decimal(1).scaleb(-18), rounding=ROUND_HALF_EVEN
            )
        self.assertEqual(result.value, expected)

    def test_insufficient_returns_when_single_day(self):
        engine = AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()])
        engine.observe_equity(observation(SESSIONS[0], "100000", step=0))
        (result,) = engine.finalize("final")
        self.assertEqual(result.status.value, "unavailable")
        self.assertEqual(result.sample_count, 1)
        self.assertEqual(
            result.analyzer_metadata["reason_code"],
            ReasonCode.INSUFFICIENT_RETURNS.value,
        )
        self.assertEqual(result.unavailable_reason, "有效收益点少于 2 个")
        self.assertIsNone(result.value)

    def test_zero_stddev_is_unavailable(self):
        engine = AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()])
        observe_days(engine, ["101000", "102010", "103030.1"])
        (result,) = engine.finalize("final")
        self.assertEqual(result.status.value, "unavailable")
        self.assertEqual(
            result.analyzer_metadata["reason_code"],
            ReasonCode.ZERO_RETURN_STDDEV.value,
        )

    def test_negative_equity_invalidates_sharpe_but_not_fees(self):
        engine = AnalyzerEngine.create(
            e0(), [build_sharpe_simple_spec(), build_fee_summary_spec()]
        )
        observe_days(engine, ["101000", "-1", "102000"])
        engine.observe_fill(fill_observation())
        results = engine.finalize("final")
        by_key = {result.metric_key: result for result in results}
        sharpe = by_key["sharpe"]
        self.assertEqual(sharpe.status.value, "unavailable")
        self.assertEqual(
            sharpe.analyzer_metadata["reason_code"],
            ReasonCode.INVALID_EQUITY.value,
        )
        self.assertIn(
            SESSIONS[1].isoformat(), sharpe.analyzer_metadata["invalid_session_dates"]
        )
        # Fee amounts stay computable from applied fills.
        self.assertEqual(by_key["cumulative_fees"].value, Decimal("5"))

    def _rate_snapshot(self, rates) -> PitRateSnapshot:
        return PitRateSnapshot(
            rates=rates,
            source_key="test_rf_source",
            source_version=1,
            coverage_start=SESSIONS[0],
            coverage_end=SESSIONS[-1],
            expected_sessions=tuple(SESSIONS),
        )

    def test_sharpe_b_reports_missing_rates_with_frozen_evidence(self):
        rates = {day: "0.02" for day in SESSIONS}
        del rates[SESSIONS[2]]
        snapshot = self._rate_snapshot(rates)
        engine = AnalyzerEngine.create(
            e0(), [build_sharpe_pit_rf_spec()], frozen_rate_snapshot=snapshot
        )
        observe_days(engine, ["101000", "101500", "102200", "102800", "103300"])
        (result,) = engine.finalize("final")
        self.assertEqual(result.status.value, "unavailable")
        self.assertEqual(
            result.analyzer_metadata["reason_code"],
            ReasonCode.MISSING_PIT_RF.value,
        )
        self.assertIn(
            SESSIONS[2].isoformat(),
            result.analyzer_metadata["missing_rate_session_dates"],
        )
        self.assertEqual(
            result.analyzer_metadata["rate_snapshot_hash"], snapshot.snapshot_hash
        )
        self.assertEqual(result.analyzer_metadata["rate_source_key"], "test_rf_source")

    def test_sharpe_b_available_when_series_complete(self):
        snapshot = self._rate_snapshot({day: "0" for day in SESSIONS})
        simple_engine = AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()])
        pit_engine = AnalyzerEngine.create(
            e0(), [build_sharpe_pit_rf_spec()], frozen_rate_snapshot=snapshot
        )
        equities = ["101000", "101500", "102200", "102800", "103300"]
        observe_days(simple_engine, equities)
        observe_days(pit_engine, equities)
        (simple_result,) = simple_engine.finalize("final")
        (pit_result,) = pit_engine.finalize("final")
        self.assertEqual(pit_result.status.value, "available")
        # rf = 0 makes B numerically identical to A.
        self.assertEqual(pit_result.value, simple_result.value)
        self.assertEqual(pit_result.analyzer_metadata["missing_ranges"], [])

    def test_sharpe_c_negative_rate_allowed_and_divides_by_252(self):
        engine = AnalyzerEngine.create(
            e0(), [build_sharpe_config_rf_spec({"rf_annual": "-0.01"})]
        )
        observe_days(engine, ["101000", "102000", "101000"])
        (result,) = engine.finalize("final")
        self.assertEqual(result.status.value, "available")
        metadata = dict(result.analyzer_metadata)
        from decimal import localcontext, Context, ROUND_HALF_EVEN

        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            self.assertEqual(
                Decimal(metadata["rf_daily"]),
                Decimal("-0.01") / ANNUALIZATION_FACTOR,
            )
        self.assertEqual(metadata["annual_rate_converter"], "annual_rate_div_252@1")

    def test_invalid_config_rf_blocks_run_creation(self):
        for rf_annual in ("-1", "-1.5", "abc"):
            with self.assertRaises(AnalyzerConfigurationError) as caught:
                AnalyzerEngine.create(
                    e0(), [build_sharpe_config_rf_spec({"rf_annual": rf_annual})]
                )
            self.assertEqual(
                caught.exception.reason_code, ReasonCode.INVALID_ANALYZER_CONFIG.value
            )

    def test_missing_config_rf_parameter_blocks_run_creation(self):
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), [build_sharpe_config_rf_spec({})])


class TestTurnoverAndFees(unittest.TestCase):
    def test_turnover_formula_uses_gross_notional_over_average_equity(self):
        engine = AnalyzerEngine.create(e0(), [build_turnover_spec()])
        observe_days(engine, ["100000", "110000", "120000"])
        engine.observe_fill(fill_observation(notional=None))
        (result,) = engine.finalize("final")
        expected_gross = Decimal("1000")
        expected_average = (
            Decimal("100000") + Decimal("110000") + Decimal("120000")
        ) / 3
        from decimal import localcontext, Context, ROUND_HALF_EVEN

        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            expected = expected_gross / expected_average
        self.assertEqual(result.value, expected.quantize(Decimal(1).scaleb(-18)))
        self.assertEqual(
            result.formula_version, "turnover_gross_notional_avg_eod_equity_v1"
        )

    def test_no_valid_equity_blocks_turnover(self):
        engine = AnalyzerEngine.create(e0(), [build_turnover_spec()])
        (result,) = engine.finalize("final")
        self.assertEqual(
            result.analyzer_metadata["reason_code"],
            ReasonCode.NO_VALID_END_OF_DAY_EQUITY.value,
        )

    def test_non_positive_average_equity_reason_is_fixed_v1_vocabulary(self):
        # Every individually valid end-of-day equity is strictly positive,
        # so a non-positive average is defensively unreachable through the
        # public path; the reason code itself is still part of the frozen
        # v1 vocabulary with its user-facing text.
        from app.backtesting.analyzers import REASON_CODE_MESSAGES

        self.assertIn(
            ReasonCode.NON_POSITIVE_AVERAGE_EQUITY.value, REASON_CODE_MESSAGES
        )
        engine = AnalyzerEngine.create(e0(), [build_turnover_spec()])
        observe_days(engine, ["50", "40", "30"])
        (result,) = engine.finalize("final")
        # Positive equities keep the turnover computable.
        self.assertEqual(result.status.value, "available")

    def test_zero_gross_traded_notional_keeps_zero_turnover(self):
        engine = AnalyzerEngine.create(e0(), [build_turnover_spec()])
        observe_days(engine, ["100000", "101000"])
        (result,) = engine.finalize("final")
        self.assertEqual(result.value, Decimal("0"))

    def test_sell_side_fills_count_into_gross_notional(self):
        from decimal import localcontext, Context, ROUND_HALF_EVEN

        sell = AppliedFillFact(
            fill_id=uuid4(),
            run_id=RUN_ID,
            session_date=SESSIONS[0],
            timestamp=datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc),
            instrument_id=INSTRUMENT,
            side="sell",
            fill_price="20",
            fill_quantity="100",
            contract_multiplier="2",
            currency="CNY",
            reporting_currency="CNY",
            fees="3",
        )
        engine = AnalyzerEngine.create(e0(), [build_turnover_spec(), build_fee_summary_spec()])
        observe_days(engine, ["100000", "110000"])
        engine.observe_fill(FillObservation(fact=sell))
        results = engine.finalize("final")
        by_key = {result.metric_key: result for result in results}
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            expected_turnover = (Decimal("4000") / Decimal("105000")).quantize(
                Decimal(1).scaleb(-18), rounding=ROUND_HALF_EVEN
            )
        self.assertEqual(by_key["turnover"].value, expected_turnover)
        self.assertEqual(by_key["cumulative_fees"].value, Decimal("3"))

    def test_fee_summary_produces_two_outputs(self):
        engine = AnalyzerEngine.create(e0(), [build_fee_summary_spec()])
        engine.observe_fill(fill_observation(fees="7"))
        results = engine.finalize("final")
        self.assertEqual(len(results), 2)
        cumulative, ratio = results
        self.assertEqual(cumulative.metric_key, "cumulative_fees")
        self.assertEqual(cumulative.formula_version, "cumulative_applied_fill_fees_v1")
        self.assertEqual(cumulative.value, Decimal("7"))
        self.assertEqual(ratio.metric_key, "fee_to_gross_traded_notional")
        self.assertEqual(
            ratio.formula_version, "fee_to_gross_traded_notional_v1"
        )
        self.assertEqual(ratio.value, Decimal("7") / Decimal("1000"))

    def test_ratio_unavailable_without_gross_traded_notional(self):
        engine = AnalyzerEngine.create(e0(), [build_fee_summary_spec()])
        results = engine.finalize("final")
        cumulative, ratio = results
        self.assertEqual(cumulative.value, Decimal("0"))
        self.assertEqual(cumulative.sample_count, 0)
        self.assertEqual(ratio.status.value, "unavailable")
        self.assertEqual(
            ratio.analyzer_metadata["reason_code"],
            ReasonCode.ZERO_GROSS_TRADED_NOTIONAL.value,
        )


class TestAnalyzerEngineContracts(unittest.TestCase):
    def test_sharpe_a_and_c_specs_can_share_the_metric_key(self):
        # A and C produce metric sharpe under different formula versions,
        # which admission accepts; the one-producer-per-metric-key rule is
        # enforced at the persistence layer (see the repository tests).
        engine = AnalyzerEngine.create(
            e0(),
            [
                build_sharpe_simple_spec(),
                build_sharpe_config_rf_spec({"rf_annual": "0.02"}),
            ],
        )
        self.assertEqual(len(engine.specs), 2)

    def test_unknown_analyzer_identity_rejected(self):
        spec = AnalyzerSpec(
            analyzer_key="sharpe_simple",
            analyzer_version=2,
            name_zh="简单夏普比率",
            name_en="Simple Sharpe Ratio",
            output_contract=[
                MetricOutputDescriptor(
                    metric_key="sharpe",
                    formula_version="sharpe_simple_ddof1_252_v2",
                    unit="ratio",
                )
            ],
        )
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), [spec])

    def test_accounting_currency_mismatch_rejected(self):
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(
                e0(), [build_turnover_spec()], accounting_currency="USD"
            )

    def test_sharpe_pit_requires_rate_snapshot(self):
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), [build_sharpe_pit_rf_spec()])

    def test_duplicate_session_observation_rejected(self):
        engine = AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()])
        engine.observe_equity(observation(SESSIONS[0], "100000"))
        with self.assertRaises(DomainValidationError):
            engine.observe_equity(observation(SESSIONS[0], "101000"))

    def test_backwards_session_observation_rejected(self):
        engine = AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()])
        engine.observe_equity(observation(SESSIONS[1], "100000", step=1))
        with self.assertRaises(DomainValidationError):
            engine.observe_equity(observation(SESSIONS[0], "100000", step=0))

    def test_cross_run_and_cross_currency_facts_rejected(self):
        engine = AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()])
        foreign = observation(SESSIONS[0], "100000")
        object.__setattr__(
            foreign, "run_id", "44444444-4444-4444-8444-444444444444"
        )
        with self.assertRaises(DomainValidationError):
            engine.observe_equity(foreign)
        usd = observation(SESSIONS[0], "100000")
        object.__setattr__(usd, "reporting_currency", "USD")
        with self.assertRaises(DomainValidationError):
            engine.observe_equity(usd)

    def test_same_fill_content_deduplicated_different_content_conflict(self):
        engine = AnalyzerEngine.create(e0(), [build_fee_summary_spec()])
        same = fill_observation()
        engine.observe_fill(same)
        engine.observe_fill(same)
        conflicting = FillObservation(
            fact=AppliedFillFact(
                fill_id=same.fact.fill_id,
                run_id=RUN_ID,
                session_date=SESSIONS[0],
                timestamp=same.fact.timestamp,
                instrument_id=INSTRUMENT,
                side="buy",
                fill_price="11",
                fill_quantity="100",
                contract_multiplier="1",
                currency="CNY",
                reporting_currency="CNY",
                fees="5",
            )
        )
        with self.assertRaises(DomainValidationError):
            engine.observe_fill(conflicting)

    def test_blocked_observation_contracts(self):
        # Blocked observations must state a reason...
        with self.assertRaises(DomainValidationError):
            EquityObservation(
                run_id=RUN_ID,
                step_sequence=0,
                session_date=SESSIONS[0],
                as_of=datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc),
                valuation_status="blocked",
                data_cutoff_at=CUTOFF,
                reporting_currency="CNY",
                cash="50000",
                equity=None,
            )
        # ...and may never carry an equity value.
        with self.assertRaises(DomainValidationError):
            EquityObservation(
                run_id=RUN_ID,
                step_sequence=0,
                session_date=SESSIONS[0],
                as_of=datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc),
                valuation_status="blocked",
                data_cutoff_at=CUTOFF,
                reporting_currency="CNY",
                cash="50000",
                equity="100000",
                valuation_reason="missing marks",
            )

    def test_finalize_runs_once_and_partial_is_not_finalizable(self):
        engine = AnalyzerEngine.create(e0(), [build_turnover_spec()])
        with self.assertRaises(DomainValidationError):
            engine.finalize("partial")
        engine.finalize("final")
        with self.assertRaises(AnalysisStateConflictError):
            engine.finalize("final")

    def test_aborted_finalization_carries_reason_and_results(self):
        engine = AnalyzerEngine.create(e0(), [build_fee_summary_spec()])
        engine.observe_fill(fill_observation())
        results = engine.finalize(
            "aborted", failure={"abort_reason": "valuation blocked"}
        )
        self.assertEqual(len(results), 2)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot.failure["abort_reason"], "valuation blocked")

    def test_decimal_results_quantized_to_eighteen_places(self):
        engine = AnalyzerEngine.create(e0(), [build_fee_summary_spec()])
        engine.observe_fill(fill_observation(fees="1"))
        for result in engine.finalize("final"):
            if result.value is not None:
                self.assertGreaterEqual(result.value.as_tuple().exponent, -18)

    def test_provisional_results_match_final_on_identical_inputs(self):
        provisional_engine = AnalyzerEngine.create(
            e0(), [build_sharpe_simple_spec(), build_turnover_spec()]
        )
        equities = ["101000", "101500", "102200", "102800", "103300"]
        observe_days(provisional_engine, equities)
        provisional = provisional_engine.snapshot().compute_provisional_results()
        final_results = provisional_engine.finalize("final")
        self.assertEqual(
            [r.metric_key for r in provisional],
            [r.metric_key for r in final_results],
        )
        for before, after in zip(provisional, final_results):
            self.assertEqual(before.value, after.value)


if __name__ == "__main__":
    unittest.main()
