"""Unit tests for analyzer protocols, registry resolution, formulas,
Decimal policy, sample counts, and fixed unavailability reason codes."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, time, timezone
from decimal import Context, Decimal, Overflow, ROUND_HALF_EVEN, Subnormal, localcontext
from uuid import UUID, uuid4

import app.backtesting.analyzers as analyzers_module

from app.backtesting.analysis_inputs import (
    AppliedFillFact,
    EquityObservation,
    FillObservation,
    FormalSessionTimeline,
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
    MetricResult,
    MetricStatus,
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
    RegistryError,
    UnknownComponentError,
    build_default_component_registry,
)

RUN_ID = "22222222-2222-4222-8222-222222222222"
OPEN_AT = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
CLOSE_PREV = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)
INSTRUMENT = UUID("33333333-3333-4333-8333-333333333333")

SESSIONS = [date(2026, 1, d) for d in (5, 6, 7, 8, 9)]


def e0(
    cash: str = "100000",
    *,
    formal_sessions=SESSIONS,
) -> InitialEquitySnapshot:
    return InitialEquitySnapshot(
        run_id=RUN_ID,
        session_date=SESSIONS[0],
        market_open_at=OPEN_AT,
        valuation_as_of=CLOSE_PREV,
        data_cutoff_at=CUTOFF,
        reporting_currency="CNY",
        cash=cash,
        formal_sessions=formal_sessions,
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


def fill_observation(
    fees: str = "5",
    notional: str = "1000",
    *,
    price: str = "10",
    quantity: str = "100",
    multiplier: str = "1",
) -> FillObservation:
    fact = AppliedFillFact(
        fill_id=uuid4(),
        run_id=RUN_ID,
        session_date=SESSIONS[0],
        timestamp=datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc),
        instrument_id=INSTRUMENT,
        side="buy",
        fill_price=price,
        fill_quantity=quantity,
        contract_multiplier=multiplier,
        currency="CNY",
        reporting_currency="CNY",
        fees=fees,
        gross_traded_notional=notional,
    )
    return FillObservation(fact=fact)


def observe_days(engine: AnalyzerEngine, equities: list[str]) -> None:
    for index, (day, equity) in enumerate(zip(SESSIONS, equities)):
        engine.observe_equity(observation(day, equity, step=index))


def make_test_engine(specs, *, timeline=True, rate_snapshot=None):
    """Create an admission-shaped test engine with the fixture timeline."""

    if not isinstance(specs, (list, tuple)):
        specs = [specs]
    engine = AnalyzerEngine.create(
        e0(), list(specs), frozen_rate_snapshot=rate_snapshot
    )
    if timeline:
        engine.attach_formal_timeline(SESSIONS)
    return engine



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

    def test_version_identity_rejects_python_equality_aliases(self):
        registry = build_default_component_registry()
        for invalid in (True, 1.0, 0, -1, "1"):
            with self.assertRaises(RegistryError):
                registry.resolve("sharpe_simple", invalid)

    def test_construct_rejects_non_mapping_parameters(self):
        entry = build_default_component_registry().resolve("sharpe_simple", 1)
        with self.assertRaises(RegistryError):
            entry.construct(1)

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
        engine = make_test_engine([build_sharpe_simple_spec()])
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
        engine = make_test_engine([build_sharpe_simple_spec()])
        engine.observe_equity(observation(SESSIONS[0], "100000", step=0))
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        self.assertEqual(result.status.value, "unavailable")
        self.assertEqual(result.sample_count, 1)
        self.assertEqual(
            result.analyzer_metadata["reason_code"],
            ReasonCode.INSUFFICIENT_RETURNS.value,
        )
        self.assertEqual(result.unavailable_reason, "有效收益点少于 2 个")
        self.assertIsNone(result.value)

    def test_zero_stddev_is_unavailable(self):
        engine = make_test_engine([build_sharpe_simple_spec()])
        observe_days(engine, ["101000", "102010", "103030.1"])
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        self.assertEqual(result.status.value, "unavailable")
        self.assertEqual(
            result.analyzer_metadata["reason_code"],
            ReasonCode.ZERO_RETURN_STDDEV.value,
        )

    def test_negative_equity_invalidates_sharpe_but_not_fees(self):
        engine = make_test_engine(
            [build_sharpe_simple_spec(), build_fee_summary_spec()]
        )
        observe_days(engine, ["101000", "-1", "102000"])
        engine.observe_fill(fill_observation())
        results = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
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
        engine = make_test_engine(
            [build_sharpe_pit_rf_spec()], rate_snapshot=snapshot
        )
        observe_days(engine, ["101000", "101500", "102200", "102800", "103300"])
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
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

    def test_sharpe_reason_priority_and_candidate_sample_count_are_fixed(self):
        snapshot = self._rate_snapshot({SESSIONS[0]: "0"})
        engine = make_test_engine(
            [build_sharpe_pit_rf_spec()], rate_snapshot=snapshot
        )
        observe_days(engine, ["101000", "-1", "102000"])
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        # Invalid equity dominates missing rates and insufficient samples.
        self.assertEqual(
            result.analyzer_metadata["reason_code"],
            ReasonCode.INVALID_EQUITY.value,
        )
        self.assertEqual(result.sample_count, 1)
        self.assertEqual(result.analyzer_metadata["valid_equity_day_count"], 2)
        self.assertEqual(result.analyzer_metadata["candidate_return_count"], 1)

    def test_sharpe_b_available_when_series_complete(self):
        snapshot = self._rate_snapshot({day: "0" for day in SESSIONS})
        simple_engine = make_test_engine([build_sharpe_simple_spec()])
        pit_engine = make_test_engine(
            [build_sharpe_pit_rf_spec()], rate_snapshot=snapshot
        )
        equities = ["101000", "101500", "102200", "102800", "103300"]
        observe_days(simple_engine, equities)
        observe_days(pit_engine, equities)
        (simple_result,) = simple_engine.finalize("final")
        (pit_result,) = pit_engine.finalize("final")
        self.assertEqual(pit_result.status.value, "available")
        # rf = 0 makes B numerically identical to A.
        self.assertEqual(pit_result.value, simple_result.value)
        self.assertEqual(pit_result.analyzer_metadata["missing_ranges"], ())

    def test_sharpe_c_negative_rate_allowed_and_divides_by_252(self):
        engine = make_test_engine([build_sharpe_config_rf_spec(
            {"rf_annual": "-0.01", "rf_source_note": "unit config"}
        )])
        observe_days(engine, ["101000", "102000", "101000"])
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        self.assertEqual(result.status.value, "available")
        metadata = dict(result.analyzer_metadata)
        from decimal import localcontext, Context, ROUND_HALF_EVEN

        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            self.assertEqual(
                Decimal(metadata["rf_daily"]),
                Decimal("-0.01") / ANNUALIZATION_FACTOR,
            )
        self.assertEqual(metadata["annual_rate_converter"], "annual_rate_div_252@1")
        self.assertEqual(metadata["risk_free_rate_note"], "unit config")

    def test_invalid_config_rf_blocks_run_creation(self):
        for rf_annual in ("-1", "-1.5", "abc"):
            with self.assertRaises(AnalyzerConfigurationError) as caught:
                AnalyzerEngine.create(
                    e0(),
                    [build_sharpe_config_rf_spec({
                        "rf_annual": rf_annual,
                        "rf_source_note": "unit config",
                    })],
                )
            self.assertEqual(
                caught.exception.reason_code, ReasonCode.INVALID_ANALYZER_CONFIG.value
            )

    def test_missing_config_rf_parameter_blocks_run_creation(self):
        with self.assertRaises(AnalyzerConfigurationError):
            make_test_engine([build_sharpe_config_rf_spec({})])

    def test_config_rf_requires_bounded_explicit_source_note(self):
        for parameters in (
            {"rf_annual": "0.02"},
            {"rf_annual": "0.02", "rf_source_note": " "},
            {"rf_annual": "0.02", "rf_source_note": "x" * 201},
        ):
            with self.subTest(parameters=parameters):
                with self.assertRaises(AnalyzerConfigurationError):
                    make_test_engine([build_sharpe_config_rf_spec(parameters)])


class TestTurnoverAndFees(unittest.TestCase):
    def test_turnover_formula_uses_gross_notional_over_average_equity(self):
        engine = make_test_engine([build_turnover_spec()])
        observe_days(engine, ["100000", "110000", "120000"])
        engine.observe_fill(fill_observation(notional="1000"))
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
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
        engine = make_test_engine([build_turnover_spec()])
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        self.assertEqual(
            result.analyzer_metadata["reason_code"],
            ReasonCode.NO_VALID_END_OF_DAY_EQUITY.value,
        )

    def test_positive_valid_equities_never_emit_unreachable_average_reason(self):
        # Every individually valid end-of-day equity is strictly positive,
        # so a non-positive average is unreachable and is not a v1 reason.
        from app.backtesting.analyzers import REASON_CODE_MESSAGES

        self.assertNotIn("NON_POSITIVE_AVERAGE_EQUITY", REASON_CODE_MESSAGES)
        engine = make_test_engine([build_turnover_spec()])
        observe_days(engine, ["50", "40", "30"])
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        # Positive equities keep the turnover computable.
        self.assertEqual(result.status.value, "available")

    def test_zero_gross_traded_notional_keeps_zero_turnover(self):
        engine = make_test_engine([build_turnover_spec()])
        observe_days(engine, ["100000", "101000"])
        (result,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
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
            gross_traded_notional="4000",
        )
        engine = make_test_engine([build_turnover_spec(), build_fee_summary_spec()])
        observe_days(engine, ["100000", "110000"])
        engine.observe_fill(FillObservation(fact=sell))
        results = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        by_key = {result.metric_key: result for result in results}
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            expected_turnover = (Decimal("4000") / Decimal("105000")).quantize(
                Decimal(1).scaleb(-18), rounding=ROUND_HALF_EVEN
            )
        self.assertEqual(by_key["turnover"].value, expected_turnover)
        self.assertEqual(by_key["cumulative_fees"].value, Decimal("3"))

    def test_fee_summary_produces_two_outputs(self):
        engine = make_test_engine([build_fee_summary_spec()])
        engine.observe_fill(fill_observation(fees="7"))
        results = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
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

    def test_fee_aggregates_share_the_numeric_38_18_boundary(self):
        engine = make_test_engine([build_fee_summary_spec()])
        engine.observe_fill(
            fill_observation(
                fees="6.1424372340224469887",
                notional="21.0137899097747100929",
                price="21.0137899097747100929",
                quantity="1",
            )
        )
        results = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        cumulative, ratio = results
        raw_fees = Decimal("6.1424372340224469887")
        raw_notional = Decimal("21.0137899097747100929")
        expected_fees = Decimal("6.142437234022446989")
        self.assertEqual(cumulative.value, expected_fees)
        self.assertEqual(
            cumulative.analyzer_metadata["cumulative_fees"],
            format(raw_fees, "f"),
        )
        self.assertEqual(
            cumulative.analyzer_metadata["gross_traded_notional"],
            format(raw_notional, "f"),
        )
        self.assertEqual(
            engine.snapshot().summary_counts()["cumulative_fees"], raw_fees
        )
        expected_ratio = Decimal("0.292305065406847420")
        prematurely_quantized_ratio = Decimal("0.292305065406847421")
        self.assertEqual(
            ratio.value,
            expected_ratio,
        )
        self.assertNotEqual(ratio.value, prematurely_quantized_ratio)

    def test_turnover_divides_raw_aggregates_before_final_quantization(self):
        engine = make_test_engine([build_turnover_spec()])
        engine.observe_equity(
            observation(SESSIONS[0], "21.0137899097747100929")
        )
        engine.observe_fill(
            fill_observation(
                fees="0",
                notional="6.1424372340224469887",
                price="6.1424372340224469887",
                quantity="1",
            )
        )
        (turnover,) = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        self.assertEqual(turnover.value, Decimal("0.292305065406847420"))
        self.assertNotEqual(turnover.value, Decimal("0.292305065406847421"))
        self.assertEqual(
            turnover.analyzer_metadata["gross_traded_notional"],
            "6.1424372340224469887",
        )
        self.assertEqual(
            turnover.analyzer_metadata["average_end_of_day_equity"],
            "21.0137899097747100929",
        )

    def test_ratio_unavailable_without_gross_traded_notional(self):
        engine = make_test_engine([build_fee_summary_spec()])
        results = engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        )
        cumulative, ratio = results
        self.assertEqual(cumulative.value, Decimal("0"))
        self.assertEqual(cumulative.sample_count, 0)
        self.assertEqual(ratio.status.value, "unavailable")
        self.assertEqual(
            ratio.analyzer_metadata["reason_code"],
            ReasonCode.ZERO_GROSS_TRADED_NOTIONAL.value,
        )


class TestAnalyzerEngineContracts(unittest.TestCase):
    def test_final_requires_complete_formal_timeline(self):
        sessions = SESSIONS[:2]
        engine = AnalyzerEngine.create(
            e0(formal_sessions=sessions), [build_sharpe_simple_spec()]
        )
        engine.observe_equity(observation(sessions[0], "100000", step=0))
        with self.assertRaises(AnalysisStateConflictError):
            engine.finalize("final")
        # A rejected early-final attempt must not lock the engine.
        engine.observe_equity(observation(sessions[1], "101000", step=1))
        self.assertEqual(engine.finalize("final")[0].status, MetricStatus.AVAILABLE)

    def test_evidence_is_validated_deeply_at_dto_construction(self):
        for invalid in ({"bad": object()}, {"bad": {1, 2}}, {"bad": 1.0}):
            with self.assertRaises(DomainValidationError):
                replace(e0(), source_versions=invalid, evidence_hash=None)
        with self.assertRaises(DomainValidationError):
            replace(
                e0(),
                source_versions={1: "numeric", "1": "text"},
                evidence_hash=None,
            )

    def test_admission_evidence_is_deeply_frozen(self):
        engine = make_test_engine([build_sharpe_simple_spec()])
        timeline = {"sessions": ["2026-01-05"]}
        evidence = {"formal_timeline": timeline}
        engine.mark_admitted(evidence)
        timeline["sessions"].append("2026-01-06")
        self.assertEqual(
            engine.admission_evidence["formal_timeline"]["sessions"],
            ("2026-01-05",),
        )
        with self.assertRaises(TypeError):
            engine.admission_evidence["formal_timeline"]["x"] = "mutable"

    def test_metric_result_normalizes_identity_and_validates_state(self):
        result = MetricResult.available(
            run_id=f"  {RUN_ID}  ",
            spec=build_sharpe_simple_spec(),
            metric_key="  sharpe  ",
            formula_version="  sharpe_simple_ddof1_252_v1  ",
            value=Decimal("1"),
            unit="  ratio  ",
            sample_count=0,
        )
        self.assertEqual(result.run_id, RUN_ID)
        self.assertEqual(result.metric_key, "sharpe")
        self.assertEqual(result.formula_version, "sharpe_simple_ddof1_252_v1")
        self.assertEqual(result.unit, "ratio")
        for invalid_count in (True, -1, 1.0, "1"):
            with self.assertRaises(DomainValidationError):
                replace(result, sample_count=invalid_count)
        with self.assertRaises(DomainValidationError):
            replace(result, analyzer_metadata={"reason_code": "INSUFFICIENT_RETURNS"})

    def test_public_invalid_inputs_raise_domain_errors(self):
        with self.assertRaises(DomainValidationError):
            observation(SESSIONS[0], "100", step=0).__class__(
                run_id=RUN_ID,
                step_sequence=0,
                session_date=SESSIONS[0],
                as_of="not-a-datetime",
                valuation_status="valid",
                data_cutoff_at=CUTOFF,
                reporting_currency="CNY",
                cash="100",
                equity="100",
                cumulative_fees="0",
            )
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()], decimal_policy=1)
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()], accounting_currency=1)
        engine = make_test_engine([build_fee_summary_spec()])
        with self.assertRaises(DomainValidationError):
            engine.finalize("aborted", failure=1)
        for abort_reason in (None, " ", 1):
            with self.assertRaises(DomainValidationError):
                engine.finalize("aborted", failure={"abort_reason": abort_reason})
        with self.assertRaises(RegistryError):
            build_default_component_registry().resolve([], 1)
        with self.assertRaises(DomainValidationError):
            MetricResult.unavailable(
                run_id=RUN_ID,
                spec=build_sharpe_simple_spec(),
                metric_key="sharpe",
                formula_version="sharpe_simple_ddof1_252_v1",
                unit="ratio",
                sample_count=0,
                reason_code=[],
            )

    def test_formal_timeline_rejects_unordered_containers(self):
        for unordered in ({SESSIONS[0]}, {"day": SESSIONS[0]}):
            with self.assertRaises(DomainValidationError):
                FormalSessionTimeline(unordered)
        engine = make_test_engine([build_sharpe_simple_spec()])
        with self.assertRaises(DomainValidationError):
            engine.attach_formal_timeline({SESSIONS[0]})

    def test_producer_outputs_must_exactly_match_spec(self):
        engine = make_test_engine([build_fee_summary_spec()])
        original = analyzers_module._BUILTIN_PRODUCERS[("fee_summary", 1)]
        valid = original(engine._state(), engine.specs[0])
        invalid_outputs = (
            ({},),
            (replace(valid[0], run_id="different-run"), valid[1]),
            (replace(valid[0], analyzer_key="turnover"), valid[1]),
            (replace(valid[0], sample_count=999), valid[1]),
            tuple(reversed(valid)),
        )
        for produced in invalid_outputs:
            with self.subTest(produced=produced):
                analyzers_module._BUILTIN_PRODUCERS[("fee_summary", 1)] = (
                    lambda state, spec, produced=produced: produced
                )
                try:
                    with self.assertRaises(DomainValidationError):
                        engine.snapshot().compute_provisional_results()
                finally:
                    analyzers_module._BUILTIN_PRODUCERS[("fee_summary", 1)] = original

    def test_metric_result_requires_complete_persistence_shape(self):
        fields = dict(
            run_id=RUN_ID,
            metric_key="sharpe",
            formula_version="sharpe_simple_ddof1_252_v1",
            analyzer_key="sharpe_simple",
            analyzer_version=1,
            status=MetricStatus.AVAILABLE,
            value="1",
        )
        with self.assertRaises(DomainValidationError):
            MetricResult(**fields, unit=None, sample_count=1)
        with self.assertRaises(DomainValidationError):
            MetricResult(**fields, unit="ratio", sample_count=None)

    def test_metric_result_enforces_numeric_38_18_range(self):
        fields = dict(
            run_id=RUN_ID,
            metric_key="sharpe",
            formula_version="sharpe_simple_ddof1_252_v1",
            analyzer_key="sharpe_simple",
            analyzer_version=1,
            status=MetricStatus.AVAILABLE,
            unit="ratio",
            sample_count=1,
        )
        with self.assertRaises(DomainValidationError):
            MetricResult(**fields, value=Decimal("1e20"))
        with self.assertRaises(DomainValidationError):
            MetricResult(**fields, value=Decimal("1e1000"))

    def test_metric_factories_wrap_invalid_public_inputs(self):
        for factory, is_available in (
            (MetricResult.available, True),
            (MetricResult.unavailable, False),
        ):
            common = dict(
                run_id=RUN_ID,
                spec=object(),
                metric_key="sharpe",
                formula_version="sharpe_simple_ddof1_252_v1",
                unit="ratio",
                sample_count=0,
            )
            if is_available:
                common["value"] = Decimal("1")
            else:
                common["reason_code"] = ReasonCode.INSUFFICIENT_RETURNS
            with self.assertRaises(DomainValidationError):
                factory(**common)
        with self.assertRaises(DomainValidationError):
            MetricResult.available(
                run_id=RUN_ID,
                spec=build_sharpe_simple_spec(),
                metric_key="sharpe",
                formula_version="sharpe_simple_ddof1_252_v1",
                value=Decimal("1"),
                unit="ratio",
                sample_count=1,
                extra_metadata=1,
            )

    def test_output_contract_identity_and_order_are_frozen(self):
        descriptor = MetricOutputDescriptor(
            metric_key=" sharpe ",
            formula_version=" v1 ",
            unit="ratio",
            sample_count_semantics=(
                "candidate_return_count_including_zero_return_days"
            ),
            unavailable_reason_codes=[],
        )
        self.assertEqual(descriptor.metric_key, "sharpe")
        self.assertEqual(descriptor.formula_version, "v1")
        for unordered_reasons in ({"INSUFFICIENT_RETURNS"}, iter(())):
            with self.assertRaises(DomainValidationError):
                replace(descriptor, unavailable_reason_codes=unordered_reasons)
        for unordered_outputs in ({descriptor}, iter((descriptor,))):
            with self.assertRaises(DomainValidationError):
                AnalyzerSpec(
                    analyzer_key="demo",
                    analyzer_version=1,
                    name_zh="演示",
                    name_en="Demo",
                    output_contract=unordered_outputs,
                )

    def test_exact_input_context_does_not_inherit_global_decimal_limits(self):
        from app.backtesting.analysis_inputs import _exact_context

        with localcontext() as global_context:
            global_context.Emax = 9
            global_context.traps[Overflow] = True
            with _exact_context():
                self.assertEqual(Decimal("1e20") * Decimal("10"), Decimal("1e21"))

    def test_metric_quantization_does_not_inherit_global_decimal_traps(self):
        with localcontext() as global_context:
            global_context.Emin = -9
            global_context.traps[Subnormal] = True
            result = MetricResult.available(
                run_id=RUN_ID,
                spec=build_sharpe_simple_spec(),
                metric_key="sharpe",
                formula_version="sharpe_simple_ddof1_252_v1",
                value=Decimal("0.000000000000000001"),
                unit="ratio",
                sample_count=1,
            )
        self.assertEqual(result.value, Decimal("0.000000000000000001"))

    def test_formula_signature_excludes_formal_timeline(self):
        first = AnalyzerEngine.create(
            e0(formal_sessions=SESSIONS[:2]),
            [build_sharpe_simple_spec()],
        )
        second = AnalyzerEngine.create(
            e0(formal_sessions=(SESSIONS[0], SESSIONS[2], SESSIONS[4])),
            [build_sharpe_simple_spec()],
        )
        self.assertNotEqual(
            first.snapshot().formal_timeline,
            second.snapshot().formal_timeline,
        )
        self.assertEqual(first.formula_signature(), second.formula_signature())

    def test_duplicate_analyzer_identity_rejected_before_execution(self):
        spec = build_fee_summary_spec()
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), [spec, spec])

    def test_non_pit_rate_snapshot_rejected(self):
        snapshot = PitRateSnapshot(
            rates={},
            source_key="test-rates",
            source_version=1,
            coverage_start=SESSIONS[0],
            coverage_end=SESSIONS[-1],
            expected_sessions=SESSIONS,
        )
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(
                e0(), [build_sharpe_simple_spec()], frozen_rate_snapshot=snapshot
            )

    def test_fill_outside_formal_timeline_rejected(self):
        engine = make_test_engine([build_fee_summary_spec()])
        outside = AppliedFillFact(
            fill_id=uuid4(),
            run_id=RUN_ID,
            session_date=date(2026, 1, 12),
            timestamp=datetime(2026, 1, 12, 1, 0, tzinfo=timezone.utc),
            instrument_id=INSTRUMENT,
            side="buy",
            fill_price="10",
            fill_quantity="100",
            contract_multiplier="1",
            currency="CNY",
            reporting_currency="CNY",
            fees="5",
            gross_traded_notional="1000",
        )
        with self.assertRaises(DomainValidationError):
            engine.observe_fill(FillObservation(fact=outside))

    def test_missing_gross_traded_notional_is_rejected(self):
        with self.assertRaises(DomainValidationError):
            AppliedFillFact(
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
                fees="5",
                gross_traded_notional=None,
            )

    def test_declared_gross_traded_notional_must_match_accounting_identity(self):
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
            fees="5",
            gross_traded_notional="1000",
        )
        self.assertEqual(fact.gross_traded_notional, Decimal("1000"))
        with self.assertRaises(DomainValidationError):
            replace(fact, gross_traded_notional="999")

    def test_fill_notional_identity_uses_multiplier_and_prec_50_product(self):
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            high_precision_product = (
                Decimal("1.234567890123456789")
                * Decimal("3.000000000000000001")
                * Decimal("2.5")
            )
        fact = AppliedFillFact(
            fill_id=uuid4(),
            run_id=RUN_ID,
            session_date=SESSIONS[0],
            timestamp=datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc),
            instrument_id=INSTRUMENT,
            side="buy",
            fill_price="1.234567890123456789",
            fill_quantity="3.000000000000000001",
            contract_multiplier="2.5",
            currency="CNY",
            reporting_currency="CNY",
            fees="0",
            gross_traded_notional=format(high_precision_product, "f"),
        )
        self.assertEqual(fact.gross_traded_notional, high_precision_product)
        with self.assertRaises(DomainValidationError):
            replace(
                fact,
                gross_traded_notional=format(
                    high_precision_product.quantize(Decimal("1e-18")), "f"
                ),
            )

    def test_unavailable_reason_code_cannot_be_overridden(self):
        with self.assertRaises(DomainValidationError):
            MetricResult.unavailable(
                run_id=RUN_ID,
                spec=build_sharpe_simple_spec(),
                metric_key="sharpe",
                formula_version="sharpe_simple_ddof1_252_v1",
                unit="ratio",
                sample_count=0,
                reason_code=ReasonCode.INSUFFICIENT_RETURNS,
                extra_metadata={"reason_code": "OTHER"},
            )
        with self.assertRaises(DomainValidationError):
            MetricResult(
                run_id=RUN_ID,
                metric_key="sharpe",
                formula_version="sharpe_simple_ddof1_252_v1",
                analyzer_key="sharpe_simple",
                analyzer_version=1,
                status=MetricStatus.UNAVAILABLE,
                unavailable_reason="bad reason",
                analyzer_metadata={"reason_code": "OTHER"},
            )

    def test_incomplete_protocol_inputs_raise_domain_errors(self):
        with self.assertRaises(DomainValidationError):
            AnalyzerSpec(
                analyzer_key="fee_summary",
                analyzer_version=1,
                name_zh="费用摘要",
                name_en="Fee Summary",
                output_contract=[{}],
            )
        with self.assertRaises(DomainValidationError):
            AnalyzerSpec(
                analyzer_key="fee_summary",
                analyzer_version=1,
                name_zh="费用摘要",
                name_en="Fee Summary",
                output_contract=None,
            )
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), None)

    def test_sharpe_a_and_c_specs_can_share_the_metric_key(self):
        # A and C produce metric sharpe under different formula versions,
        # which admission accepts; the one-producer-per-metric-key rule is
        # enforced at the persistence layer (see the repository tests).
        engine = AnalyzerEngine.create(
            e0(),
            [
                build_sharpe_simple_spec(),
                build_sharpe_config_rf_spec({
                    "rf_annual": "0.02",
                    "rf_source_note": "unit config",
                }),
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
                    sample_count_semantics="candidate_return_count",
                    unavailable_reason_codes=(),
                )
            ],
        )
        with self.assertRaises(AnalyzerConfigurationError):
            make_test_engine([spec])

    def test_forged_output_contract_rejected_at_admission(self):
        spec = AnalyzerSpec(
            analyzer_key="sharpe_simple",
            analyzer_version=1,
            name_zh="简单夏普比率",
            name_en="Simple Sharpe Ratio",
            output_contract=[
                MetricOutputDescriptor(
                    metric_key="forged_metric",
                    formula_version="forged_formula",
                    unit="ratio",
                    sample_count_semantics="candidate_return_count",
                    unavailable_reason_codes=(),
                )
            ],
        )
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), [spec])

    def test_unknown_parameters_rejected_at_admission(self):
        spec = AnalyzerSpec(
            analyzer_key="sharpe_simple",
            analyzer_version=1,
            name_zh="简单夏普比率",
            name_en="Simple Sharpe Ratio",
            parameters={"secret_knob": "1"},
            output_contract=[
                MetricOutputDescriptor(
                    metric_key="sharpe",
                    formula_version="sharpe_simple_ddof1_252_v1",
                    unit="ratio",
                    sample_count_semantics="candidate_return_count",
                    unavailable_reason_codes=(),
                )
            ],
        )
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(e0(), [spec])

    def test_admission_stamp_is_single_use_and_required_shape(self):
        from app.backtesting.analyzers import AnalysisStateConflictError

        engine = make_test_engine([build_sharpe_simple_spec()])
        self.assertIsNone(engine.admission_evidence)
        engine.mark_admitted({"initial_equity_hash": "sha256:x"})
        self.assertIsNotNone(engine.admission_evidence)
        with self.assertRaises(AnalysisStateConflictError):
            engine.mark_admitted({"initial_equity_hash": "sha256:y"})

    def test_accounting_currency_mismatch_rejected(self):
        with self.assertRaises(AnalyzerConfigurationError):
            AnalyzerEngine.create(
                e0(), [build_turnover_spec()], accounting_currency="USD"
            )

    def test_sharpe_pit_requires_rate_snapshot(self):
        with self.assertRaises(AnalyzerConfigurationError):
            make_test_engine([build_sharpe_pit_rf_spec()])

    def test_duplicate_session_observation_rejected(self):
        engine = make_test_engine([build_sharpe_simple_spec()])
        engine.observe_equity(observation(SESSIONS[0], "100000"))
        with self.assertRaises(DomainValidationError):
            engine.observe_equity(observation(SESSIONS[0], "101000"))

    def test_timeline_gaps_and_inverted_steps_rejected(self):
        engine = make_test_engine([build_sharpe_simple_spec()])
        engine.observe_equity(observation(SESSIONS[0], "100000", step=0))
        # Skipping a formal session (e.g. a zero-return day) is rejected.
        with self.assertRaises(DomainValidationError):
            engine.observe_equity(
                observation(SESSIONS[2], "101000", step=1)
            )
        # An inverted/duplicated step sequence is rejected even when the
        # session date happens to line up.
        with self.assertRaises(DomainValidationError):
            engine.observe_equity(
                observation(SESSIONS[1], "100000", step=5)
            )

    def test_create_binds_formal_timeline(self):
        engine = AnalyzerEngine.create(e0(), [build_sharpe_simple_spec()])
        engine.observe_equity(observation(SESSIONS[0], "100000", step=0))

    def test_cross_run_and_cross_currency_facts_rejected(self):
        engine = make_test_engine([build_sharpe_simple_spec()])
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
        engine = make_test_engine([build_fee_summary_spec()])
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
                gross_traded_notional="1100",
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
        engine = make_test_engine([build_turnover_spec()])
        with self.assertRaises(DomainValidationError):
            engine.finalize("partial")
        observe_days(engine, ["100000"] * len(SESSIONS))
        first = engine.finalize("final")
        # Same-state retries return the frozen terminal result.  An
        # opposite terminal state remains a hard conflict.
        self.assertEqual(engine.finalize("final"), first)
        with self.assertRaises(AnalysisStateConflictError):
            engine.finalize("aborted", failure={"abort_reason": "late"})

    def test_blocked_equity_timeline_can_only_finalize_as_aborted(self):
        engine = make_test_engine([build_sharpe_simple_spec()])
        for index, day in enumerate(SESSIONS):
            engine.observe_equity(
                observation(
                    day,
                    None if index == 2 else "100000",
                    step=index,
                    status="blocked" if index == 2 else "valid",
                )
            )
        with self.assertRaises(AnalysisStateConflictError):
            engine.finalize("final")
        results = engine.finalize(
            "aborted", failure={"abort_reason": "valuation blocked"}
        )
        self.assertEqual(results[0].status, MetricStatus.UNAVAILABLE)

    def test_aborted_finalization_carries_reason_and_results(self):
        engine = make_test_engine([build_fee_summary_spec()])
        engine.observe_fill(fill_observation())
        results = engine.finalize(
            "aborted", failure={"abort_reason": "valuation blocked"}
        )
        self.assertEqual(len(results), 2)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot.failure["abort_reason"], "valuation blocked")

    def test_decimal_results_quantized_to_eighteen_places(self):
        engine = make_test_engine([build_fee_summary_spec()])
        engine.observe_fill(fill_observation(fees="1"))
        for result in engine.finalize(
            "aborted", failure={"abort_reason": "partial test timeline"}
        ):
            if result.value is not None:
                self.assertGreaterEqual(result.value.as_tuple().exponent, -18)

    def test_provisional_results_match_final_on_identical_inputs(self):
        provisional_engine = make_test_engine(
            [build_sharpe_simple_spec(), build_turnover_spec()]
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
