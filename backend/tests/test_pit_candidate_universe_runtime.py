"""Acceptance tests for the PIT candidate final-check runtime boundary."""

from __future__ import annotations

import unittest
from datetime import date
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.backtesting.result_models import (
    BacktestDecisionRecord,
    DecisionValidationStatus,
)
from app.backtesting.data.universe import (
    CandidateEligibilityContext,
    CandidateInput,
    evaluate_candidate,
)
from app.backtesting.runtime import PhaseExecutionError
from app.strategy_protocol.data_view import InstrumentCandidateDTO
from app.backtesting.data.requests import DataCapability, InstrumentScopeMode
from app.instruments.domain import VersionedReference
from app.instruments.rule_snapshots import (
    FactProvenance,
    InstrumentRuleSnapshotSegment,
    RunRuleSnapshotBundle,
)
from tests.backtest_runtime_fixture import (
    CountingStrategyView,
    DictMarketData,
    INSTRUMENT_ID,
    ScriptedStrategy,
    build_axis,
    build_runner,
)


D0 = date(2026, 8, 3)
D1 = date(2026, 8, 4)
D2 = date(2026, 8, 5)

_DEFAULT_RULE_SNAPSHOT = object()


def _rule_snapshot_segment(
    instrument_id: UUID = INSTRUMENT_ID,
) -> InstrumentRuleSnapshotSegment:
    """Build one complete all-N/A rule segment for formal runtime tests."""

    observed_at = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    return InstrumentRuleSnapshotSegment(
        instrument_id=instrument_id,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        normal_fact_reference=VersionedReference("etf_rule_fact", 3),
        exception_fact_reference=None,
        normalized_values={
            "lot_size": Decimal("200"),
            "quantity_precision": 0,
            "price_precision": 2,
            "price_tick": Decimal("0.05"),
            "contract_multiplier": Decimal("1"),
            "trading_session_template": VersionedReference("cn_etf_session", 1),
            "settlement_rule_class": "t_plus_1_before_open_match",
            "sellable_rule": VersionedReference("sell_rule", 1),
            "fee_categories": ("none",),
            "currency": "CNY",
            "order_types": ("market",),
            "minimum_order_quantity": Decimal("200"),
            "price_limit_rule": VersionedReference("price_limit_rule", 1),
            "cash_availability_rule": VersionedReference("cash_rule", 1),
            "position_availability_rule": VersionedReference("position_rule", 1),
        },
        capability_declarations={
            "suspension": "not_applicable",
            "opening_availability": "not_applicable",
            "price_limit_tradability": "not_applicable",
        },
        provenance={
            "normal_fact": FactProvenance(
                fact_reference=VersionedReference("etf_rule_fact", 3),
                source="exchange_rule_book",
                source_revision="2026-edition",
                valid_from=date(2024, 1, 1),
                valid_to=None,
                known_at=observed_at,
                observed_at=observed_at,
                quality_status="complete",
                fixture_only=False,
                content_hash="f" * 64,
            ).to_payload()
        },
        resolution_hash="r" * 64,
    )


def _rule_snapshot_bundle(
    *,
    segments=None,
) -> RunRuleSnapshotBundle:
    """Build an unbound run snapshot with explicitly supplied segments."""

    return RunRuleSnapshotBundle(
        rule_package_reference=VersionedReference("china_listed_etf_rules", 1),
        rule_package_semantic_hash="p" * 64,
        parser_revision="rule-package-resolver@2",
        exception_set_reference=None,
        exception_set_hash=None,
        data_cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
        instrument_segments=(
            tuple(segments)
            if segments is not None
            else (_rule_snapshot_segment(),)
        ),
    )


class _CompleteCandidate:
    """Engine-side candidate carrying all final-qualification evidence."""

    def __init__(self, instrument_id: UUID, *, calendar_id: str = "XSHG") -> None:
        self.instrument_id = instrument_id
        self.trading_code = "510300"
        self.name = "沪深300ETF"
        self.display_name = "沪深300ETF"
        self.asset_class = "etf"
        self.exchange = "SSE"
        self.calendar_id = calendar_id
        self.settlement_rule_class = "t1_before_open_match"
        self.rule_package_reference = "rules@1"
        self.identity_evidence = {"complete": True, "calendar_id": calendar_id}
        self.mapping_evidence = {"complete": True}
        self.rule_evidence = {
            "valid": True,
            "rule_package_reference": "rules@1",
            "settlement_rule_class": "t1_before_open_match",
        }
        self.market_data_evidence = {"complete": True}
        self.corporate_action_evidence = {"complete": True}
        self.quantity_action_coverage_evidence = {"complete": True}
        self.status_evidence = {"complete": True}


class _DynamicUniverseQuery:
    """Minimal dynamic query used to exercise the Runtime permission boundary."""

    scope_mode = InstrumentScopeMode.DYNAMIC
    allowed_calendar_ids = ("XSHG",)

    def __init__(self, candidates, *, filter_summary=None, by_date=None) -> None:
        self._candidates = tuple(candidates)
        self.filter_summary = filter_summary or {}
        self._by_date = dict(by_date or {})

    def for_step(self, *, effective_date=None, **_kwargs):
        if effective_date in self._by_date:
            return self._by_date[effective_date]
        return self

    def query(self, *, exchanges=None, asset_classes=None):
        del exchanges, asset_classes
        return self._candidates


class _NoTradingStatusMarketData(DictMarketData):
    """Expose a failing status port to prove formal N/A runs never use it."""

    def __init__(self, quotes_by_day) -> None:
        super().__init__(
            quotes_by_day,
            suspended_instruments=(INSTRUMENT_ID,),
        )

    def trading_status(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("formal N/A runtime must not query trading_status")


class _QueryingStrategy(ScriptedStrategy):
    """Scripted strategy that explicitly earns dynamic query permission."""

    def on_step(self, context):
        context.universe.query()
        return super().on_step(context)


class _DynamicScope:
    """Frozen formal scope used by the Runtime-only acceptance tests."""

    status = "ready"
    scope_mode = InstrumentScopeMode.DYNAMIC
    resolved_calendar_ids = ("XSHG",)
    snapshot_hash = "a" * 64
    current_snapshot_hash = "a" * 64
    qualification_policy_version = "candidate-qualification@1"


def _complete_evaluator(candidate, context):
    """Return one complete result while recording current PIT coordinates."""

    _complete_evaluator.calls.append((candidate, context))
    return {
        "instrument_id": candidate.instrument_id,
        "eligible": True,
        "reason_codes": (),
        "calendar_id": candidate.calendar_id,
        "identity_evidence": candidate.identity_evidence,
        "mapping_evidence": candidate.mapping_evidence,
        "rule_evidence": candidate.rule_evidence,
        "market_data_evidence": candidate.market_data_evidence,
        "corporate_action_evidence": candidate.corporate_action_evidence,
        "quantity_action_coverage_evidence": candidate.quantity_action_coverage_evidence,
        "status_evidence": candidate.status_evidence,
        "evidence_summary": {
            "effective_date": context.effective_date,
            "data_cutoff": context.data_cutoff,
            "market_scope": context.market_scope,
        },
    }


_complete_evaluator.calls = []


def _runner(*, target_id: UUID = INSTRUMENT_ID):
    """Build a two-session runner with one strategy-visible candidate."""

    axis = build_axis([D0, D1])
    return build_runner(
        run_id=f"pit-runtime-{uuid4()}",
        axis=axis,
        market_data=DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "101.00")},
            }
        ),
        strategy_view=CountingStrategyView({D0: "100.00", D1: "101.00"}),
        strategy=ScriptedStrategy({0: {str(target_id): "1"}}),
    )


class FinalQualificationRuntimeTests(unittest.TestCase):
    def test_dto_only_pure_evaluator_adapter_fails_closed_without_metadata(self) -> None:
        runner = _runner()
        candidate = InstrumentCandidateDTO(
            instrument_id=INSTRUMENT_ID,
            trading_code="510300",
            name="沪深300ETF",
            display_name="沪深300ETF",
            asset_class="etf",
            exchange="SSE",
        )

        adapted = runner._prepare_evaluator_candidate(
            candidate,
            evaluate_candidate,
            calendar_id="XSHG",
        )

        # A strategy DTO may contribute only its six public fields.  It cannot
        # smuggle qualification evidence through a removed metadata field.
        self.assertIsInstance(adapted, CandidateInput)
        self.assertEqual(adapted.instrument_id, INSTRUMENT_ID)
        self.assertEqual(adapted.calendar_id, "XSHG")
        self.assertEqual(adapted.identity_evidence, {})
        self.assertEqual(adapted.mapping_evidence, {})
        self.assertEqual(adapted.rule_evidence, {})
        self.assertEqual(adapted.market_data_evidence, {})
        self.assertEqual(adapted.metadata, {})
        self.assertFalse(hasattr(candidate, "metadata"))

        result = evaluate_candidate(
            adapted,
            CandidateEligibilityContext(
                effective_date=D0,
                data_cutoff=datetime(2026, 8, 3, 15, tzinfo=timezone.utc),
                resolved_calendar_ids=("XSHG",),
                scope_mode=InstrumentScopeMode.DYNAMIC,
            ),
        )
        self.assertFalse(result.eligible)

    def test_ineligible_selected_candidate_fails_before_order_creation(self) -> None:
        runner = _runner()

        # The evaluator is the only qualification implementation.  The
        # runtime merely applies its result at the order boundary.
        runner._candidate_eligibility_evaluator = lambda candidate, context: {
            "instrument_id": candidate.instrument_id,
            "eligible": False,
            "reason_codes": ["market_data_incomplete"],
            "calendar_id": "XSHG",
        }

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_selected_ineligible")
        self.assertEqual(raised.exception.details["instrument_id"], str(INSTRUMENT_ID))
        self.assertEqual(raised.exception.details["failed_check"], "candidate_qualification")
        self.assertEqual(raised.exception.details["calendar_id"], "XSHG")
        self.assertIn("expected", raised.exception.details)
        self.assertIn("actual", raised.exception.details)
        self.assertIn("evidence_summary", raised.exception.details)
        self.assertEqual(runner._orders, [])

    def test_structured_final_failure_evidence_fits_existing_decision_json(self) -> None:
        decision = BacktestDecisionRecord(
            run_id=uuid4(),
            decision_id=uuid4(),
            step_sequence=0,
            decision_time=datetime(2026, 8, 3, 15, tzinfo=timezone.utc),
            mode="target_weights",
            validation_status=DecisionValidationStatus.REJECTED,
            validation_issues=[
                {
                    "instrument_id": str(INSTRUMENT_ID),
                    "session_date": "2026-08-03",
                    "decision_time": "2026-08-03T15:00:00+00:00",
                    "data_cutoff": "2026-08-03T15:00:00+00:00",
                    "failed_check": "candidate_qualification",
                    "reason_codes": ["market_data_incomplete"],
                    "expected": True,
                    "actual": {"eligible": False},
                    "evidence_summary": {"scope_snapshot_hash": "sha256:example"},
                }
            ],
        )

        self.assertEqual(
            decision.validation_issues[0]["failed_check"],
            "candidate_qualification",
        )

    def test_all_targets_are_checked_before_any_order_is_staged(self) -> None:
        stranger = UUID("00000000-0000-4000-8000-000000000099")
        runner = _runner()
        runner._strategy.targets_by_step[0] = {
            str(INSTRUMENT_ID): "1",
            str(stranger): "1",
        }

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_target_outside_scope")
        self.assertEqual(runner._orders, [])

    def test_successful_target_is_projected_to_run_audit(self) -> None:
        runner = _runner()
        # A zero target still exercises the final qualification path while
        # avoiding an opening buy whose T+1 settlement would require a third
        # official fixture session.
        runner._strategy.targets_by_step[0] = {str(INSTRUMENT_ID): "0"}
        result = runner.run()

        self.assertEqual(result.universe_target_ids, (str(INSTRUMENT_ID),))
        self.assertEqual(result.final_qualification_results[0]["eligible"], True)
        self.assertEqual(result.final_qualification_results[0]["instrument_id"], str(INSTRUMENT_ID))

    def test_scope_hash_change_fails_before_strategy_execution(self) -> None:
        class Scope:
            status = "ready"
            resolved_calendar_ids = ("XSHG",)
            snapshot_hash = "a" * 64
            current_snapshot_hash = "b" * 64

        runner = _runner()
        runner._universe_scope_resolution = Scope()
        runner._frozen_universe_calendar_ids = ("XSHG",)
        runner._universe_scope_snapshot_hash = "a" * 64

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_preflight_hash_mismatch")
        self.assertEqual(runner._strategy.observed_contexts, [])

    def test_target_outside_current_candidate_scope_is_not_dropped(self) -> None:
        stranger = UUID("00000000-0000-4000-8000-000000000099")
        runner = _runner(target_id=stranger)

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_target_outside_scope")
        self.assertEqual(raised.exception.details["instrument_id"], str(stranger))
        # A failed decision cannot leave a partial order for the valid
        # candidate, nor can it silently continue to the next session.
        self.assertEqual(runner._orders, [])

    def _formal_dynamic_runner(
        self,
        *,
        candidate=None,
        strategy=None,
        universe_query_override=None,
        session_dates=(D0, D1),
        candidate_eligibility_evaluator=_complete_evaluator,
        market_data=None,
        rule_snapshot_bundle=_DEFAULT_RULE_SNAPSHOT,
        initial_cash="10000",
    ):
        candidate = candidate or _CompleteCandidate(INSTRUMENT_ID)
        quote_values = {
            day: {
                INSTRUMENT_ID: (
                    str(99 + index) + ".00",
                    str(100 + index) + ".00",
                )
            }
            for index, day in enumerate(session_dates)
        }
        if market_data is None:
            market_data = DictMarketData(quote_values)
        if rule_snapshot_bundle is _DEFAULT_RULE_SNAPSHOT:
            rule_snapshot_bundle = _rule_snapshot_bundle()
        return build_runner(
            run_id=f"pit-formal-runtime-{uuid4()}",
            axis=build_axis(session_dates),
            market_data=market_data,
            strategy_view=CountingStrategyView(
                {day: str(100 + index) + ".00" for index, day in enumerate(session_dates)}
            ),
            strategy=strategy or _QueryingStrategy({0: {str(INSTRUMENT_ID): "0"}}),
            scope_instrument_ids=(),
            universe_query_override=(
                universe_query_override
                or _DynamicUniverseQuery((candidate,))
            ),
            candidate_eligibility_evaluator=candidate_eligibility_evaluator,
            universe_scope_resolution=_DynamicScope(),
            rule_snapshot_bundle=rule_snapshot_bundle,
            initial_cash=initial_cash,
        )

    def test_dynamic_target_without_rule_snapshot_bundle_fails_before_order_creation(
        self,
    ) -> None:
        runner = self._formal_dynamic_runner(
            session_dates=(D0, D1, D2),
            strategy=_QueryingStrategy({0: {str(INSTRUMENT_ID): "2"}}),
            rule_snapshot_bundle=None,
        )

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_capability_missing")
        self.assertEqual(
            raised.exception.details["failed_check"],
            "rule_snapshot_segment_handoff",
        )
        self.assertIn(
            "rule_snapshot_bundle_missing",
            raised.exception.details["reason_codes"],
        )
        self.assertEqual(runner._orders, [])

    def test_dynamic_target_without_current_rule_segment_fails_before_order_creation(
        self,
    ) -> None:
        runner = self._formal_dynamic_runner(
            session_dates=(D0, D1, D2),
            strategy=_QueryingStrategy({0: {str(INSTRUMENT_ID): "2"}}),
            rule_snapshot_bundle=_rule_snapshot_bundle(
                segments=(_rule_snapshot_segment(uuid4()),)
            ),
        )

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_capability_missing")
        self.assertEqual(
            raised.exception.details["instrument_id"], str(INSTRUMENT_ID)
        )
        self.assertIn(
            "rule_snapshot_segment_missing",
            raised.exception.details["reason_codes"],
        )
        self.assertEqual(runner._orders, [])

    def test_all_na_dynamic_target_uses_handoff_without_trading_status_query(
        self,
    ) -> None:
        candidate = _CompleteCandidate(INSTRUMENT_ID)
        candidate.status_evidence = {
            "complete": False,
            "quality_status": "invalid",
        }
        market_data = _NoTradingStatusMarketData(
            {
                day: {INSTRUMENT_ID: ("100.00", "100.00")}
                for day in (D0, D1, D2)
            }
        )
        runner = self._formal_dynamic_runner(
            candidate=candidate,
            candidate_eligibility_evaluator=evaluate_candidate,
            market_data=market_data,
            session_dates=(D0, D1, D2),
            strategy=_QueryingStrategy({0: {str(INSTRUMENT_ID): "1"}}),
            initial_cash="50000",
        )

        result = runner.run()

        self.assertTrue(
            any(event.event_type == "fill_created" for event in result.events)
        )
        self.assertEqual(
            runner._execution_facts[(INSTRUMENT_ID, D1)].suspension_state.value,
            "not_applicable",
        )
        self.assertIn(
            (INSTRUMENT_ID, D0), runner._rule_policy_cache
        )

    def test_final_recheck_uses_complete_engine_candidate_and_current_cutoff(self) -> None:
        _complete_evaluator.calls = []
        runner = self._formal_dynamic_runner()

        runner.run()

        # The strategy sees only a projected DTO; the final port must receive
        # the complete engine-side row and the same step PIT coordinates.
        final_candidate, context = _complete_evaluator.calls[-1]
        self.assertIsInstance(final_candidate, _CompleteCandidate)
        self.assertEqual(context.effective_date, D0)
        self.assertEqual(context.data_cutoff, context.effective_at)

    def test_final_recheck_ignores_unrequested_invalid_status_evidence(self) -> None:
        """A full N/A dynamic target does not need a status qualification port."""

        candidate = _CompleteCandidate(INSTRUMENT_ID)
        candidate.status_evidence = {
            "complete": False,
            "quality_status": "invalid",
        }
        runner = self._formal_dynamic_runner(
            candidate=candidate,
            candidate_eligibility_evaluator=evaluate_candidate,
        )

        result = runner.run()

        self.assertEqual(result.universe_target_ids, (str(INSTRUMENT_ID),))
        self.assertTrue(result.final_qualification_results[0]["eligible"])

    def test_final_recheck_required_status_without_fact_fails_closed(self) -> None:
        """A required status dimension still blocks before order creation."""

        class RequiredStatusScope(_DynamicScope):
            required_capabilities = (DataCapability.STATUS,)

        candidate = _CompleteCandidate(INSTRUMENT_ID)
        candidate.status_evidence = {}
        runner = self._formal_dynamic_runner(
            candidate=candidate,
            candidate_eligibility_evaluator=evaluate_candidate,
        )
        runner._universe_scope_resolution = RequiredStatusScope()

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_selected_ineligible")
        self.assertIn(
            "candidate_status_incomplete",
            raised.exception.details["reason_codes"],
        )
        self.assertEqual(runner._orders, [])

    def test_dynamic_target_requires_strategy_query_result(self) -> None:
        runner = self._formal_dynamic_runner(
            strategy=ScriptedStrategy({0: {str(INSTRUMENT_ID): "0"}})
        )

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_target_outside_scope")
        self.assertEqual(
            raised.exception.details["failed_check"], "strategy_universe_query"
        )
        self.assertEqual(runner._orders, [])

    def test_formal_dynamic_without_qualification_port_fails_closed(self) -> None:
        runner = self._formal_dynamic_runner(
            candidate_eligibility_evaluator=None
        )

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(
            raised.exception.error_code, "universe_capability_missing"
        )
        self.assertEqual(runner._strategy.observed_contexts, [])

    def test_formal_final_recheck_rejects_missing_qualification_evidence(self) -> None:
        def incomplete_evaluator(candidate, _context):
            return {
                "instrument_id": candidate.instrument_id,
                "eligible": True,
            }

        candidate = _CompleteCandidate(INSTRUMENT_ID)
        for name in (
            "identity_evidence",
            "mapping_evidence",
            "rule_evidence",
            "market_data_evidence",
            "corporate_action_evidence",
            "quantity_action_coverage_evidence",
            "status_evidence",
        ):
            delattr(candidate, name)
        runner = self._formal_dynamic_runner(
            candidate=candidate,
            candidate_eligibility_evaluator=incomplete_evaluator,
        )

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(raised.exception.error_code, "universe_selected_ineligible")
        self.assertEqual(raised.exception.details["failed_check"], "qualification_evidence")
        self.assertEqual(runner._orders, [])

    def test_unpreflighted_calendar_target_is_stable_and_axis_is_unchanged(self) -> None:
        runner = self._formal_dynamic_runner(
            candidate=_CompleteCandidate(INSTRUMENT_ID, calendar_id="NEW_CAL")
        )
        official_axis = tuple(runner._axis)

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(
            raised.exception.error_code, "universe_calendar_not_preflighted"
        )
        self.assertEqual(raised.exception.details["calendar_id"], "NEW_CAL")
        self.assertEqual(tuple(runner._axis), official_axis)
        self.assertEqual(runner._orders, [])

    def test_running_candidate_new_calendar_fails_without_rebuilding_axis(self) -> None:
        first = _DynamicUniverseQuery((_CompleteCandidate(INSTRUMENT_ID),))
        second = _DynamicUniverseQuery(
            (_CompleteCandidate(INSTRUMENT_ID, calendar_id="NEW_CAL"),)
        )
        source = _DynamicUniverseQuery(
            first._candidates, by_date={D0: first, D1: second}
        )
        runner = self._formal_dynamic_runner(
            universe_query_override=source,
            session_dates=(D0, D1, date(2026, 8, 5)),
            strategy=_QueryingStrategy(
                {
                    0: {str(INSTRUMENT_ID): "0"},
                    1: {str(INSTRUMENT_ID): "0"},
                }
            ),
        )
        official_axis = tuple(runner._axis)

        with self.assertRaises(PhaseExecutionError) as raised:
            runner.run()

        self.assertEqual(
            raised.exception.error_code, "universe_calendar_not_preflighted"
        )
        self.assertEqual(tuple(runner._axis), official_axis)
        self.assertEqual(runner._orders, [])

    def test_filter_summary_reason_counts_are_projected_to_result(self) -> None:
        source = _DynamicUniverseQuery(
            (_CompleteCandidate(INSTRUMENT_ID),),
            filter_summary={
                "reason_counts": {"candidate_market_data_incomplete": 2},
                "records": (
                    {
                        "instrument_id": str(INSTRUMENT_ID),
                        "reason_codes": ("candidate_market_data_incomplete",),
                    },
                ),
            },
        )
        result = self._formal_dynamic_runner(
            universe_query_override=source
        ).run()

        self.assertEqual(
            result.universe_filtered_reason_counts[
                "candidate_market_data_incomplete"
            ],
            2,
        )
        self.assertEqual(
            result.universe_eligibility_summary["filter_records"][0]["instrument_id"],
            str(INSTRUMENT_ID),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
