"""Tests for run-admission gates: strict-PIT E0 construction, portfolio
consistency, frozen rate prefetch, and external cash-flow blocking."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from app.backtesting.analysis_admission import (
    AdmissionBlockedError,
    admit_analysis_run,
    build_initial_equity_snapshot,
    compute_portfolio_snapshot_binding,
    ensure_modeled_cash_movements,
    freeze_rate_snapshot,
    verify_initial_portfolio_consistency,
)
from app.backtesting.analyzers import (
    build_sharpe_config_rf_spec,
    build_sharpe_pit_rf_spec,
    build_sharpe_simple_spec,
    build_turnover_spec,
)
from app.backtesting.analysis_inputs import PitRateSnapshot
from app.backtesting.data.facts import ClosePriceFact, FactEvidence, PitRateFact
from app.backtesting.data.requests import QualityStatus

UTC = timezone.utc
OPEN_AT = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)  # 09:30 Shanghai
FIRST_SESSION = date(2026, 8, 3)
SESSIONS = [date(2026, 8, d) for d in (3, 4, 5, 6, 7)]
INSTRUMENT = UUID("88888888-8888-4888-9888-888888888888")
RUN_ID = "99999999-9999-4999-8999-999999999999"


def evidence(known_at: datetime | None, observed_at: datetime | None = None):
    return FactEvidence(
        source="unit-source",
        observed_at=observed_at or known_at or datetime(2020, 1, 1, tzinfo=UTC),
        quality_status=QualityStatus.COMPLETE,
        known_at=known_at,
    )


def close_fact(
    *,
    session_date=date(2026, 7, 31),
    price="10",
    known_at=datetime(2026, 7, 31, 8, 0, tzinfo=UTC),
    currency="CNY",
) -> ClosePriceFact:
    return ClosePriceFact(
        instrument_id=INSTRUMENT,
        session_date=session_date,
        close_price=price,
        evidence=evidence(known_at),
        currency=currency,
    )


def build_snapshot(close_facts, quantities=None, cash="10000", **overrides):
    fields = dict(
        run_id=RUN_ID,
        first_formal_session_date=FIRST_SESSION,
        market_open_at=OPEN_AT,
        valuation_as_of=datetime(2026, 7, 31, 15, 0, tzinfo=UTC),
        data_cutoff_at=datetime(2026, 7, 31, 16, 0, tzinfo=UTC),
        initial_cash=cash,
        initial_quantities=quantities or {},
        close_facts=close_facts,
        reporting_currency="CNY",
    )
    fields.update(overrides)
    return build_initial_equity_snapshot(**fields)


class TestInitialEquityAdmission(unittest.TestCase):
    def test_invalid_analyzer_config_precedes_cash_and_market_fact_access(self):
        class Account:
            cash_balances = {"CNY": Decimal("10000")}
            equity = Decimal("10000")

        class Portfolio:
            account = Account()
            positions = {}

        class ExplodingMovements:
            def __iter__(self):
                raise AssertionError("cash movements must not be queried")

        with self.assertRaises(AdmissionBlockedError) as caught:
            admit_analysis_run(
                run_id=RUN_ID,
                formal_sessions=SESSIONS,
                market_open_at=OPEN_AT,
                valuation_as_of=datetime(2026, 7, 31, 15, 0, tzinfo=UTC),
                data_cutoff_at=datetime(2026, 7, 31, 16, 0, tzinfo=UTC),
                reporting_currency="CNY",
                initial_cash="10000",
                initial_portfolio_state=Portfolio(),
                analyzer_specs=[
                    build_sharpe_config_rf_spec(
                        {"rf_annual": "-1", "rf_source_note": "unit config"}
                    )
                ],
                cash_movements=ExplodingMovements(),
            )
        self.assertEqual(caught.exception.reason_code, "INVALID_ANALYZER_CONFIG")

    def test_currency_and_e0_precede_cash_and_rate_gateway(self):
        class Account:
            cash_balances = {"CNY": Decimal("10000")}
            equity = Decimal("10000")

        class Portfolio:
            account = Account()
            positions = {}

        class ExplodingGateway:
            def risk_free_rate_snapshot(self, query):  # pragma: no cover
                raise AssertionError("rate I/O must remain the final gate")

        common = dict(
            run_id=RUN_ID,
            formal_sessions=SESSIONS,
            market_open_at=OPEN_AT,
            valuation_as_of=datetime(2026, 7, 31, 15, 0, tzinfo=UTC),
            data_cutoff_at=datetime(2026, 7, 31, 16, 0, tzinfo=UTC),
            reporting_currency="CNY",
            initial_cash="10000",
            initial_portfolio_state=Portfolio(),
            analyzer_specs=[build_sharpe_simple_spec()],
            cash_movements=[("owner_deposit", "1")],
        )
        with self.assertRaises(AdmissionBlockedError) as currency_error:
            admit_analysis_run(**common, accounting_currency="USD")
        self.assertEqual(currency_error.exception.reason_code, "INVALID_ANALYZER_CONFIG")

        with self.assertRaises(AdmissionBlockedError) as e0_error:
            admit_analysis_run(
                **{**common, "initial_quantities": {INSTRUMENT: Decimal("1")}}
            )
        self.assertEqual(e0_error.exception.reason_code, "MISSING_INITIAL_MARK")

        with self.assertRaises(AdmissionBlockedError) as cash_error:
            admit_analysis_run(
                **{
                    **common,
                    "analyzer_specs": [build_sharpe_pit_rf_spec()],
                    "pit_gateway": ExplodingGateway(),
                    "rate_source_key": "rf",
                    "rate_source_version": 1,
                    "rate_session_open_at": {day: OPEN_AT for day in SESSIONS},
                }
            )
        self.assertEqual(
            cash_error.exception.reason_code, "UNMODELED_EXTERNAL_CASH_FLOW"
        )

    def test_ambiguous_initial_mark_uses_frozen_missing_reason(self):
        first = close_fact()
        second = replace(first, close_price=Decimal("11"))
        with self.assertRaises(AdmissionBlockedError) as caught:
            build_snapshot([first, second], quantities={INSTRUMENT: "1"})
        self.assertEqual(caught.exception.reason_code, "MISSING_INITIAL_MARK")

    def test_portfolio_binding_covers_both_accounting_pnl_fields(self):
        class Position:
            instrument_id = INSTRUMENT
            side = "long"
            quantity = Decimal("1")
            available_quantity = Decimal("1")
            average_price = Decimal("10")
            mark_price = Decimal("11")
            realized_pnl = Decimal("2")
            unrealized_pnl = Decimal("1")

        class Account:
            cash_balances = {"CNY": Decimal("100")}
            equity = Decimal("111")

        class Portfolio:
            account = Account()
            positions = {INSTRUMENT: Position()}

        portfolio = Portfolio()
        _, original = compute_portfolio_snapshot_binding(
            portfolio, reporting_currency="CNY"
        )
        portfolio.positions[INSTRUMENT].realized_pnl = Decimal("3")
        _, realized_changed = compute_portfolio_snapshot_binding(
            portfolio, reporting_currency="CNY"
        )
        portfolio.positions[INSTRUMENT].realized_pnl = Decimal("2")
        portfolio.positions[INSTRUMENT].unrealized_pnl = Decimal("2")
        _, unrealized_changed = compute_portfolio_snapshot_binding(
            portfolio, reporting_currency="CNY"
        )
        self.assertNotEqual(original, realized_changed)
        self.assertNotEqual(original, unrealized_changed)
    def test_run_must_select_exactly_one_sharpe_convention(self):
        common = dict(
            run_id=RUN_ID,
            formal_sessions=SESSIONS,
            market_open_at=OPEN_AT,
            valuation_as_of=datetime(2026, 7, 31, 15, 0, tzinfo=UTC),
            data_cutoff_at=datetime(2026, 7, 31, 16, 0, tzinfo=UTC),
            reporting_currency="CNY",
            initial_cash="10000",
            initial_portfolio_state=None,
        )
        invalid_selections = (
            [build_turnover_spec()],
            [
                build_sharpe_simple_spec(),
                build_sharpe_config_rf_spec(
                    {"rf_annual": "0.02", "rf_source_note": "unit config"}
                ),
            ],
        )
        for specs in invalid_selections:
            with self.subTest(specs=specs):
                with self.assertRaises(AdmissionBlockedError) as caught:
                    admit_analysis_run(**common, analyzer_specs=specs)
                self.assertEqual(
                    caught.exception.reason_code,
                    "INVALID_ANALYZER_CONFIG",
                )

    def test_creation_failure_has_structured_non_persisted_response(self):
        with self.assertRaises(AdmissionBlockedError) as caught:
            build_snapshot([], quantities={INSTRUMENT: "1"})
        response = caught.exception.with_run_id(RUN_ID).as_response()
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["run_id"], RUN_ID)
        self.assertEqual(response["reason_code"], "MISSING_INITIAL_MARK")
        self.assertFalse(response["persisted"])

    def test_pre_open_mark_builds_frozen_e0(self):
        snapshot = build_snapshot(
            [close_fact()], quantities={INSTRUMENT: "100"}
        )
        self.assertEqual(snapshot.equity_e0, Decimal("11000"))
        self.assertEqual(snapshot.holdings[0].close_price, Decimal("10"))

    def test_mark_known_only_after_open_blocks_with_missing_initial_mark(self):
        late_fact = close_fact(
            known_at=datetime(2026, 8, 3, 2, 0, tzinfo=UTC)
        )
        with self.assertRaises(AdmissionBlockedError) as caught:
            build_snapshot([late_fact], quantities={INSTRUMENT: "100"})
        self.assertEqual(
            caught.exception.reason_code, "MISSING_INITIAL_MARK"
        )

    def test_held_quantity_without_any_mark_blocks(self):
        with self.assertRaises(AdmissionBlockedError) as caught:
            build_snapshot([], quantities={INSTRUMENT: "100"})
        self.assertEqual(
            caught.exception.reason_code, "MISSING_INITIAL_MARK"
        )

    def test_non_positive_e0_blocks_run_creation(self):
        for cash in ("0", "-1"):
            with self.assertRaises(AdmissionBlockedError) as caught:
                build_snapshot([close_fact()], quantities={}, cash=cash)
            self.assertEqual(
                caught.exception.reason_code,
                "NON_POSITIVE_INITIAL_EQUITY",
            )

    def test_accounting_currency_mismatch_blocks(self):
        with self.assertRaises(AdmissionBlockedError) as caught:
            build_snapshot(
                [close_fact()],
                quantities={},
                accounting_currency="USD",
            )
        self.assertEqual(
            caught.exception.reason_code, "INVALID_ANALYZER_CONFIG"
        )

    def test_foreign_currency_mark_is_not_a_usable_pit_mark(self):
        usd_fact = close_fact(currency="USD")
        with self.assertRaises(AdmissionBlockedError) as caught:
            build_snapshot([usd_fact], quantities={INSTRUMENT: "100"})
        self.assertEqual(
            caught.exception.reason_code, "MISSING_INITIAL_MARK"
        )

    def test_portfolio_consistency_gate(self):
        snapshot = build_snapshot([close_fact()], quantities={INSTRUMENT: "100"})

        class Account:
            equity = Decimal("11000")

        class Portfolio:
            account = Account()

        verify_initial_portfolio_consistency(snapshot, Portfolio())

        class WrongAccount:
            equity = Decimal("12000")

        class WrongPortfolio:
            account = WrongAccount()

        with self.assertRaises(AdmissionBlockedError):
            verify_initial_portfolio_consistency(snapshot, WrongPortfolio())


class _RateGateway:
    """PIT analysis gateway stub serving a fixed rate series."""

    def __init__(self, facts, missing_from: date | None = None) -> None:
        self._facts = [
            fact
            for fact in facts
            if missing_from is None or fact.session_date < missing_from
        ]
        self.queries = []

    def risk_free_rate_snapshot(self, query):
        self.queries.append(query)
        return tuple(
            fact
            for fact in self._facts
            if query.start_session <= fact.session_date <= query.end_session
        )


class TestRatePrefetch(unittest.TestCase):
    def test_snapshot_hash_binds_the_complete_expected_session_axis(self):
        sparse_sessions = (SESSIONS[0], SESSIONS[2], SESSIONS[-1])
        dense_sessions = tuple(SESSIONS)
        sparse = PitRateSnapshot(
            rates={},
            source_key="unit_rf",
            source_version=1,
            coverage_start=SESSIONS[0],
            coverage_end=SESSIONS[-1],
            expected_sessions=sparse_sessions,
        )
        dense = PitRateSnapshot(
            rates={},
            source_key="unit_rf",
            source_version=1,
            coverage_start=SESSIONS[0],
            coverage_end=SESSIONS[-1],
            expected_sessions=dense_sessions,
        )
        # These are all fields the legacy hash could observe. Their equality
        # demonstrates the old collision; only the ordered formal axis differs.
        self.assertEqual(sparse.coverage_start, dense.coverage_start)
        self.assertEqual(sparse.coverage_end, dense.coverage_end)
        self.assertEqual(sparse.rates, dense.rates)
        self.assertEqual(sparse.missing_ranges, dense.missing_ranges)
        self.assertNotEqual(sparse.expected_sessions, dense.expected_sessions)
        self.assertNotEqual(sparse.snapshot_hash, dense.snapshot_hash)

    def test_ambiguous_rate_revision_becomes_missing_coverage(self):
        first = PitRateFact(
            session_date=SESSIONS[0],
            rate="0.02",
            evidence=evidence(datetime(2026, 8, 1, tzinfo=UTC)),
            data_cutoff_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        snapshot = freeze_rate_snapshot(
            _RateGateway([first, replace(first, rate=Decimal("0.03"))]),
            expected_sessions=SESSIONS,
            source_key="unit_rf",
            source_version=1,
            session_open_at={
                day: datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                for day in SESSIONS
            },
        )
        assert snapshot is not None
        self.assertNotIn(SESSIONS[0], snapshot.rates)
        self.assertEqual(snapshot.missing_ranges, ((SESSIONS[0], SESSIONS[-1]),))
        self.assertEqual(
            snapshot.fact_evidence[SESSIONS[0].isoformat()]["status"],
            "ambiguous",
        )

        reversed_snapshot = freeze_rate_snapshot(
            _RateGateway([replace(first, rate=Decimal("0.03")), first]),
            expected_sessions=SESSIONS,
            source_key="unit_rf",
            source_version=1,
            session_open_at={
                day: datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                for day in SESSIONS
            },
        )
        changed_snapshot = freeze_rate_snapshot(
            _RateGateway([first, replace(first, rate=Decimal("0.04"))]),
            expected_sessions=SESSIONS,
            source_key="unit_rf",
            source_version=1,
            session_open_at={
                day: datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                for day in SESSIONS
            },
        )
        assert reversed_snapshot is not None and changed_snapshot is not None
        self.assertEqual(snapshot.snapshot_hash, reversed_snapshot.snapshot_hash)
        self.assertNotEqual(snapshot.snapshot_hash, changed_snapshot.snapshot_hash)

    def test_full_window_prefetched_once_with_missing_ranges(self):
        gateway = _RateGateway(
            [
                PitRateFact(
                    session_date=day,
                    rate="0.02",
                    evidence=evidence(datetime(2026, 8, 1, tzinfo=UTC)),
                    data_cutoff_at=datetime(2026, 8, 1, tzinfo=UTC),
                )
                for day in SESSIONS[:3]  # last two sessions intentionally absent
            ]
        )
        snapshot = freeze_rate_snapshot(
            gateway,
            expected_sessions=SESSIONS,
            source_key="unit_rf",
            source_version=1,
            session_open_at={day: datetime.combine(day, datetime.min.time(), tzinfo=UTC) for day in SESSIONS},
        )
        assert snapshot is not None
        self.assertIsInstance(snapshot, PitRateSnapshot)
        self.assertEqual(len(gateway.queries), 1)
        self.assertEqual(gateway.queries[0].expected_sessions, tuple(SESSIONS))
        self.assertEqual(
            snapshot.missing_ranges,
            ((date(2026, 8, 6), date(2026, 8, 7)),),
        )
        # The hash is recomputed by the DTO itself, not caller-supplied.
        self.assertTrue(snapshot.snapshot_hash.startswith("sha256:"))
        self.assertEqual(snapshot.rate_unit, "decimal_fraction")
        self.assertEqual(snapshot.rate_convention, "simple_daily_rate")
        self.assertEqual(snapshot.effective_at, "session_date")
        self.assertEqual(snapshot.session_mapping, "exact_formal_session_date")

    def test_empty_session_window_returns_none(self):
        self.assertIsNone(
            freeze_rate_snapshot(
                _RateGateway([]),
                expected_sessions=[],
                source_key="s",
                source_version=1,
                session_open_at={},
            )
        )


class TestCashFlowGate(unittest.TestCase):
    def test_modeled_kinds_pass(self):
        movements = [
            ("initial_capital", "10000"),
            ("applied_fill", "-500"),
            ("corporate_action", "12"),
        ]
        ensure_modeled_cash_movements(movements)  # must not raise

    def test_unknown_kind_blocks_with_frozen_reason(self):
        with self.assertRaises(AdmissionBlockedError) as caught:
            ensure_modeled_cash_movements(
                [
                    ("initial_capital", "10000"),
                    ("owner_deposit", "50000"),
                ]
            )
        self.assertEqual(
            caught.exception.reason_code, "UNMODELED_EXTERNAL_CASH_FLOW"
        )
        self.assertEqual(
            caught.exception.details["unmodeled"][0]["kind"], "owner_deposit"
        )


if __name__ == "__main__":
    unittest.main()
