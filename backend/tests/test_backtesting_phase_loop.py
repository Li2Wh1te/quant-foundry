"""Unit tests for the deterministic phase loop and its view isolation."""

import unittest
from datetime import date
from decimal import Decimal

from app.backtesting.runtime import (
    EngineDataView,
    EventEnvelope,
    PhaseExecutionError,
)
from app.backtesting.timing import DataViewKind, TimingInstruction, TimingPhase
from app.strategy_protocol.data_view import StrategyDataDTO
from tests.backtest_runtime_fixture import (
    CountingStrategyView,
    DictMarketData,
    INSTRUMENT_ID,
    ScriptedStrategy,
    build_axis,
    build_runner,
    session_close,
)

D0 = date(2026, 8, 3)
D1 = date(2026, 8, 4)
D2 = date(2026, 8, 5)

BUY_ALL = {0: {str(INSTRUMENT_ID): "1"}}

# Expected business event stream of the standard three-day scenario:
# D0 close 100 -> decide buy all; D1 open 100 fill, close 102; D2 close 103.
EXPECTED_EVENT_TYPES = [
    # step0
    "portfolio_valued",
    "strategy_decision_created",
    "order_submitted",
    # step1
    "fill_created",
    "fill_applied",
    "portfolio_valued",
    "strategy_decision_created",
    # step2: final valuation only, plus T+1 sale-availability restore.
    "settlement_restored",
    "portfolio_valued",
]


def scenario_runner(
    *,
    run_id: str = "run-unit",
    targets_by_step: dict | None = None,
    interpreter=None,
    component_parameters=None,
    accounting_currency: str = "CNY",
    execution_model=None,
    settlement_policy=None,
):
    """Three-day runner: D0 close 100, D1 open 100/close 102, D2 close 103."""

    axis = build_axis([D0, D1, D2])
    market_data = DictMarketData(
        {
            D0: {INSTRUMENT_ID: ("99.00", "100.00")},
            D1: {INSTRUMENT_ID: ("100.00", "102.00")},
            D2: {INSTRUMENT_ID: ("101.00", "103.00")},
        }
    )
    view = CountingStrategyView({D0: "100.00", D1: "102.00", D2: "103.00"})
    strategy = ScriptedStrategy(targets_by_step if targets_by_step is not None else BUY_ALL)
    runner = build_runner(
        run_id=run_id,
        axis=axis,
        market_data=market_data,
        strategy_view=view,
        strategy=strategy,
        interpreter=interpreter,
        component_parameters=component_parameters,
        accounting_currency=accounting_currency,
        execution_model=execution_model,
        settlement_policy=settlement_policy,
    )
    return runner, strategy


class PhaseOrderAndEventStreamTests(unittest.TestCase):
    def test_event_stream_matches_the_documented_phase_order(self) -> None:
        runner, _ = scenario_runner()
        result = runner.run()

        self.assertEqual(
            [event.event_type for event in result.events],
            EXPECTED_EVENT_TYPES,
        )
        # Every envelope carries the phase coordinates of its origin.
        for event in result.events:
            self.assertTrue(event.phase_key)
            self.assertGreaterEqual(event.event_sequence, 1)

    def test_sequences_are_monotonic_across_the_whole_run(self) -> None:
        runner, _ = scenario_runner()
        result = runner.run()

        previous_event = 0
        previous_step = -1
        for event in result.events:
            self.assertEqual(event.event_sequence, previous_event + 1)
            previous_event = event.event_sequence
            self.assertGreaterEqual(event.step_sequence, previous_step)
            previous_step = event.step_sequence
        # Phase sequences stay inside one step's phase range and never go
        # backwards; events only exist for phases that emit business facts.
        by_step: dict[int, list[int]] = {}
        for event in result.events:
            by_step.setdefault(event.step_sequence, []).append(event.phase_sequence)
        for phase_sequences in by_step.values():
            self.assertTrue(all(1 <= p <= 9 for p in phase_sequences))
            self.assertEqual(phase_sequences, sorted(phase_sequences))

    def test_decide_and_submit_are_absent_on_the_final_step(self) -> None:
        runner, _ = scenario_runner()
        result = runner.run()

        final_types = [
            event.event_type for event in result.events if event.step_sequence == 2
        ]
        self.assertNotIn("strategy_decision_created", final_types)
        self.assertNotIn("order_submitted", final_types)
        self.assertIn("portfolio_valued", final_types)
        self.assertEqual(len(result.equity_curve), 3)

    def test_identical_inputs_reproduce_identical_results(self) -> None:
        first, _ = scenario_runner(run_id="same-run")
        second, _ = scenario_runner(run_id="same-run")

        first_result = first.run()
        second_result = second.run()
        self.assertEqual(first_result.events, second_result.events)
        self.assertEqual(first_result.equity_curve, second_result.equity_curve)

    def test_different_run_ids_keep_business_payloads_equal(self) -> None:
        first, _ = scenario_runner(run_id="run-a")
        second, _ = scenario_runner(run_id="run-b")

        first_result = first.run()
        second_result = second.run()

        def business(result):
            return [
                (
                    event.step_sequence,
                    event.phase_key,
                    event.event_type,
                    event.event_time,
                )
                for event in result.events
            ]

        self.assertEqual(business(first_result), business(second_result))
        self.assertEqual(first_result.equity_curve, second_result.equity_curve)
        # Derived identifiers follow the run namespace but stay stable within
        # one run.
        first_orders = [
            e.payload["order_id"]
            for e in first_result.events
            if e.event_type == "order_submitted"
        ]
        second_orders = [
            e.payload["order_id"]
            for e in second_result.events
            if e.event_type == "order_submitted"
        ]
        self.assertNotEqual(first_orders, second_orders)

    def test_phase_failure_carries_step_and_phase_coordinates(self) -> None:
        class ExplodingStrategy(ScriptedStrategy):
            def on_step(self, context):
                raise ValueError("boom")

        runner = build_runner(
            run_id="run-fail",
            # Two sessions: the first step is non-final and runs decide.
            axis=build_axis([D0, D1]),
            market_data=DictMarketData(
                {
                    D0: {INSTRUMENT_ID: ("1.00", "1.00")},
                    D1: {INSTRUMENT_ID: ("1.00", "1.00")},
                }
            ),
            strategy_view=CountingStrategyView({D0: "1.00", D1: "1.00"}),
            strategy=ExplodingStrategy({}),
        )
        with self.assertRaises(PhaseExecutionError) as context:
            runner.run()
        self.assertEqual(context.exception.step_sequence, 0)
        self.assertEqual(context.exception.phase_key, "decide")
        self.assertEqual(context.exception.error_type, "ValueError")

    def test_run_result_records_component_versions(self) -> None:
        runner, _ = scenario_runner(component_parameters={"board_lot": 100})
        result = runner.run()

        self.assertEqual(
            result.components["timing_policy"],
            {"key": "after_close_to_next_open", "version": 1},
        )
        self.assertEqual(
            result.components["decision_interpreter"],
            {
                "key": "target_weights",
                "version": 1,
                "parameters": {"board_lot": Decimal("100")},
            },
        )
        # Nested execution sub-components and the accounting configuration
        # are part of the auditable snapshot.
        # The slippage entry captures the model's live parameter snapshot.
        slippage_record = result.components["slippage_model"]
        self.assertEqual(slippage_record["key"], "none")
        self.assertEqual(slippage_record["version"], 1)
        self.assertEqual(
            dict(slippage_record["parameters"]),
            {"slippage_bps": Decimal("0"), "price_tick": Decimal("0.01")},
        )
        self.assertEqual(
            result.components["fee_schedule"],
            {"key": "runtime_fixture", "version": 1, "fee_rules": ()},
        )
        self.assertEqual(
            result.components["accounting_policy"]["settlement_policy"],
            "t_plus_1_before_open_match",
        )
        self.assertEqual(
            result.components["parameters"], {"board_lot": 100}
        )

    def test_runner_rejects_anonymous_replaceable_components(self) -> None:
        from app.backtesting.domain import DomainValidationError

        class AnonymousInterpreter:
            def interpret(self, *args, **kwargs):
                return ()

        with self.assertRaises(DomainValidationError) as context:
            scenario_runner(interpreter=AnonymousInterpreter())
        self.assertIn("decision_interpreter", str(context.exception))

    def test_view_factory_failure_carries_phase_coordinates(self) -> None:
        from app.backtesting.runtime import BacktestViewFactory
        from tests.backtest_runtime_fixture import make_candidate, universe_query

        class ExplodingFactory(BacktestViewFactory):
            def for_phase(self, instruction, step, *, next_step):
                raise ConnectionError("provider offline")

        axis = build_axis([D0, D1])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "102.00")},
            }
        )
        runner = build_runner(
            run_id="run-factory-fail",
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView({D0: "100.00", D1: "102.00"}),
            strategy=ScriptedStrategy({}),
        )
        runner._view_factory = ExplodingFactory(
            strategy_view=CountingStrategyView({D0: "100.00", D1: "102.00"}),
            universe_query=universe_query([make_candidate()]),
            engine_market_data=market_data,
            scope_instrument_ids=(INSTRUMENT_ID,),
        )
        with self.assertRaises(PhaseExecutionError) as context:
            runner.run()
        # The very first phase (pre_open_settle of step 0) already builds a
        # view through the factory.
        self.assertEqual(context.exception.step_sequence, 0)
        self.assertEqual(context.exception.error_type, "ConnectionError")

    def test_event_payloads_are_fully_deep_frozen(self) -> None:
        from app.backtesting.domain import DomainValidationError

        payload = {"nested": ({"items": [1, 2]},)}
        envelope = EventEnvelope(
            run_id="r",
            event_sequence=1,
            step_sequence=0,
            phase_sequence=6,
            phase_key="value",
            event_type="portfolio_valued",
            event_time=session_close(D0),
            payload=payload,
        )
        # Mutating the original input dict cannot change the envelope...
        payload["nested"][0]["items"].append(3)
        self.assertEqual(envelope.payload["nested"][0]["items"], (1, 2))
        # ...and the frozen structure rejects mutation attempts everywhere.
        with self.assertRaises((AttributeError, TypeError)):
            envelope.payload["nested"][0]["items"].append(4)
        with self.assertRaises(DomainValidationError):
            EventEnvelope(
                run_id="r",
                event_sequence=2,
                step_sequence=0,
                phase_sequence=6,
                phase_key="value",
                event_type="portfolio_valued",
                event_time=session_close(D0),
                payload={"bad": {1, 2}},
            )

    def test_runner_lifecycle_rejects_replays_and_gaps(self) -> None:
        from app.backtesting.domain import DomainValidationError

        steps = tuple(build_axis([D0, D1, D2]))
        runner, _ = scenario_runner()

        # Starting mid-timeline is rejected.
        with self.assertRaises(DomainValidationError):
            runner.run_steps(steps[1:])
        # A completed runner can never execute again.
        runner.run()
        with self.assertRaises(DomainValidationError):
            runner.run()
        with self.assertRaises(DomainValidationError):
            runner.run_steps(steps[:1])

    def test_blocked_valuation_terminates_before_deciding_on_stale_equity(
        self,
    ) -> None:
        from app.backtesting.runtime import ValuationBlockedError

        axis = build_axis([D0, D1, D2])
        # D1 has an open (the fill works) but no close mark at all.
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "")},
                D2: {INSTRUMENT_ID: ("101.00", "103.00")},
            }
        )
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        runner = build_runner(
            run_id="run-blocked",
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView({D0: "100.00", D1: "102.00"}),
            strategy=strategy,
        )
        with self.assertRaises(PhaseExecutionError) as context:
            runner.run()
        self.assertEqual(context.exception.error_type, "ValuationBlockedError")
        self.assertEqual(context.exception.phase_key, "value")
        self.assertEqual(context.exception.step_sequence, 1)
        # The strategy was never asked to decide on the stale equity again:
        # only the D0 decision happened.
        self.assertEqual(len(strategy.observed_contexts), 1)

    def test_submit_sizes_against_explicit_board_lots(self) -> None:
        axis = build_axis([D0, D1, D2])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("299.00", "300.00")},
                D1: {INSTRUMENT_ID: ("300.00", "301.00")},
                D2: {INSTRUMENT_ID: ("301.00", "302.00")},
            },
            board_lot=10,
        )
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        runner = build_runner(
            run_id="run-lot10",
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView({D0: "300.00", D1: "301.00"}),
            strategy=strategy,
        )
        result = runner.run()

        submitted = [
            e for e in result.events if e.event_type == "order_submitted"
        ]
        # 10,000 equity / 300 reference = 33.33 raw shares -> floored to the
        # explicit lot-10 grid, not to a default of 100.
        self.assertEqual(submitted[0].payload["quantity"], Decimal("30"))

    def test_missing_trading_status_cannot_default_to_tradable(self) -> None:
        from app.backtesting.runtime import InstrumentFacts

        with self.assertRaises(TypeError):
            # No defaults: suspension and price-limit availability must be
            # stated explicitly by the data source.
            InstrumentFacts(
                instrument_id=INSTRUMENT_ID,
                price_tick=Decimal("0.01"),
                calendar_id="XSHG",
            )

    def test_suspended_instrument_expires_instead_of_matching(self) -> None:
        axis = build_axis([D0, D1])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "102.00")},
            },
            suspended_instruments={INSTRUMENT_ID},
        )
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        runner = build_runner(
            run_id="run-suspended",
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView({D0: "100.00", D1: "102.00"}),
            strategy=strategy,
        )
        result = runner.run()

        expired = [e for e in result.events if e.event_type == "order_expired"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].payload["reason"], "instrument_suspended")

    def test_fee_snapshot_captures_the_actual_rule_configuration(self) -> None:
        from app.backtesting.fees import (
            FeeCalculator,
            FeeSchedule,
        )
        from app.backtesting.execution import BarMarketExecutionModel
        from app.backtesting.registry import (
            build_default_component_registry,
        )
        from app.backtesting.slippage import BpsSlippageModel

        def model_with(rate: str) -> BarMarketExecutionModel:
            # Build through the registry exactly as a formal run would.
            return build_default_component_registry().resolve(
                "bar_market", 1
            ).construct(
                {
                    "slippage_bps": 0,
                    "price_tick": "0.01",
                    "commission_rate": rate,
                    "commission_minimum": "5",
                }
            )

        def snapshot_rates(rate: str):
            axis = build_axis([D0, D1, D2])
            runner, _ = scenario_runner(
                run_id=f"run-fees-{rate}",
                execution_model=model_with(rate),
            )
            return runner.run().components["fee_schedule"]

        low = snapshot_rates("0.001")
        high = snapshot_rates("0.003")
        for snapshot in (low, high):
            self.assertEqual(snapshot["key"], "bar_market_flat_commission")
            rule = snapshot["fee_rules"][0]
            self.assertEqual(rule["category"], "commission")
            self.assertEqual(rule["minimum"], Decimal("5"))
            self.assertIsNotNone(rule["rounding_scope"])
        # Different rates must produce different audits: the schedule key
        # alone cannot reconstruct a historical fee configuration.
        self.assertEqual(low["fee_rules"][0]["rate"], Decimal("0.001"))
        self.assertEqual(high["fee_rules"][0]["rate"], Decimal("0.003"))
        self.assertNotEqual(
            low["fee_rules"][0]["rate"], high["fee_rules"][0]["rate"]
        )

    def test_fee_snapshot_distinguishes_schedules_by_rounding_scope(self) -> None:
        """Two schedules identical except rounding_scope realize different
        fees on multi-order fills and must produce different snapshots."""

        from app.backtesting.fees import (
            FeeCalculator,
            FeeRule,
            FeeRoundingLevel,
            FeeRoundingMode,
            FeeSchedule,
        )
        from app.backtesting.execution import BarMarketExecutionModel
        from app.backtesting.slippage import BpsSlippageModel

        def model_with(scope: str | None) -> BarMarketExecutionModel:
            schedule = FeeSchedule(
                key="scope_probe",
                version=1,
                fee_rules=(
                    FeeRule(
                        key="commission",
                        category="commission",
                        rate="0.001",
                        rounding_level=FeeRoundingLevel.FILL,
                        rounding_scope=scope,
                        rounding_mode=FeeRoundingMode.HALF_UP,
                        rounding_precision="1",
                    ),
                ),
            )
            return BarMarketExecutionModel(
                slippage_model=BpsSlippageModel.none(price_tick="0.01"),
                fee_calculator=FeeCalculator(schedule),
                model_key="bar_market",
                model_version=1,
            )

        def fee_snapshot(scope: str):
            runner, _ = scenario_runner(
                run_id=f"run-scope-{scope}",
                execution_model=model_with(scope),
            )
            return runner.run().components["fee_schedule"]

        per_order_scope = fee_snapshot("commission")
        per_day_scope = fee_snapshot("commission_daily")
        self.assertEqual(
            per_order_scope["fee_rules"][0]["rounding_scope"], "commission"
        )
        self.assertEqual(
            per_day_scope["fee_rules"][0]["rounding_scope"], "commission_daily"
        )
        self.assertNotEqual(
            per_order_scope["fee_rules"], per_day_scope["fee_rules"]
        )

    def test_currency_mismatch_between_runner_and_accounting_is_rejected(
        self,
    ) -> None:
        from app.backtesting.domain import DomainValidationError

        with self.assertRaises(DomainValidationError) as context:
            scenario_runner(accounting_currency="USD")
        self.assertIn("currency", str(context.exception))

    def test_runner_enforces_formal_t_plus_one_settlement_policy(self) -> None:
        from app.backtesting.accounting import SettlementPolicy
        from app.backtesting.domain import DomainValidationError

        for rejected in (
            SettlementPolicy.SAME_DAY,
            SettlementPolicy.T_PLUS_ONE,
        ):
            with self.assertRaises(DomainValidationError) as context:
                scenario_runner(
                    run_id=f"run-settlement-{rejected.value}",
                    settlement_policy=rejected,
                )
            self.assertIn("settlement", str(context.exception).lower())

    def test_malformed_run_steps_input_yields_stable_domain_errors(self) -> None:
        from app.backtesting.domain import DomainValidationError

        runner, _ = scenario_runner()
        steps = tuple(build_axis([D0, D1]))

        # Non-iterable and None inputs fail as domain errors, never as the
        # bare TypeError that tuple() would raise.
        with self.assertRaises(DomainValidationError):
            runner.run_steps(None)
        with self.assertRaises(DomainValidationError):
            runner.run_steps(object())
        with self.assertRaises(DomainValidationError):
            runner.run_steps([object()])
        with self.assertRaises(DomainValidationError):
            runner.run_steps(steps[:1], next_after_last=object())
        with self.assertRaises(DomainValidationError):
            runner.run_steps([])
        with self.assertRaises(DomainValidationError):
            runner.run_steps("D0")

        class BrokenIterator:
            def __iter__(self):
                raise ValueError("broken")

        with self.assertRaises(DomainValidationError):
            runner.run_steps(BrokenIterator())

        class RuntimeBrokenIterator:
            def __iter__(self):
                raise RuntimeError("worse")

        with self.assertRaises(DomainValidationError):
            runner.run_steps(RuntimeBrokenIterator())

    def test_chunked_execution_never_resets_sequences_or_state(self) -> None:
        axis = build_axis([D0, D1, D2])
        runner, _ = scenario_runner()
        steps = tuple(axis)

        # Drive the same timeline as two chunks; the first chunk declares
        # its true successor so the boundary keeps decide/submit alive.
        runner.run_steps(steps[:2], next_after_last=steps[2])
        runner.run_steps(steps[2:])
        chunked_events = runner._events

        single, _ = scenario_runner(run_id="run-unit")
        single.run_steps(steps)
        self.assertEqual(chunked_events, single._events)

    def test_forged_step_with_valid_sequence_is_rejected(self) -> None:
        from app.backtesting.domain import DomainValidationError
        from dataclasses import replace as dc_replace

        steps = tuple(build_axis([D0, D1, D2]))
        forged = dc_replace(steps[1], session_id="forged-session")
        runner, _ = scenario_runner()

        with self.assertRaises(DomainValidationError) as context:
            runner.run_steps([steps[0], forged])
        self.assertIn("official timeline", str(context.exception))

    def test_chunk_tail_requires_the_official_successor(self) -> None:
        from app.backtesting.domain import DomainValidationError
        from dataclasses import replace as dc_replace

        steps = tuple(build_axis([D0, D1, D2]))
        runner, _ = scenario_runner()

        # Omitting the successor would silently treat D0 as the final day
        # and skip decide/submit.
        with self.assertRaises(DomainValidationError):
            runner.run_steps(steps[:1])
        # A forged successor cannot fabricate a non-final day either.
        forged_successor = dc_replace(steps[1], session_id="forged-session")
        with self.assertRaises(DomainValidationError):
            runner.run_steps(steps[:1], next_after_last=forged_successor)
        # The true successor is accepted and the run stays correct.
        runner.run_steps(steps[:1], next_after_last=steps[1])
        runner.run_steps(steps[1:])
        submitted = [
            e for e in runner._events if e.event_type == "order_submitted"
        ]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].step_sequence, 0)

    def test_failed_runner_refuses_reexecution_without_duplicate_events(
        self,
    ) -> None:
        from app.backtesting.domain import DomainValidationError

        class FailAfterFirstDecide(ScriptedStrategy):
            def on_step(self, context):
                if context.step_sequence >= 1:
                    raise RuntimeError("strategy crashed")
                return super().on_step(context)

        runner = build_runner(
            run_id="run-crash",
            axis=build_axis([D0, D1, D2]),
            market_data=DictMarketData(
                {
                    D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                    D1: {INSTRUMENT_ID: ("100.00", "102.00")},
                    D2: {INSTRUMENT_ID: ("101.00", "103.00")},
                }
            ),
            strategy_view=CountingStrategyView(
                {D0: "100.00", D1: "102.00", D2: "103.00"}
            ),
            strategy=FailAfterFirstDecide({0: {str(INSTRUMENT_ID): "1"}}),
        )
        with self.assertRaises(PhaseExecutionError):
            runner.run()
        events_after_failure = len(runner._events)
        self.assertGreater(events_after_failure, 0)

        # The failed instance is permanently stopped: a retry can neither
        # re-execute the failed step nor extend the partial state.
        with self.assertRaises(DomainValidationError) as context:
            runner.run()
        self.assertIn("failed", str(context.exception))
        self.assertEqual(len(runner._events), events_after_failure)

    def test_broken_timing_policy_fails_with_step_coordinates(self) -> None:
        class BrokenPolicy:
            policy_key = "after_close_to_next_open"
            policy_version = 1

            def phases(self, step, *, next_step):
                raise KeyError("boom")

        runner, _ = scenario_runner()
        runner._timing_policy = BrokenPolicy()
        with self.assertRaises(PhaseExecutionError) as context:
            runner.run()
        self.assertEqual(context.exception.phase_key, "timing_policy")
        self.assertEqual(context.exception.error_type, "KeyError")
        self.assertEqual(context.exception.step_sequence, 0)

    def test_component_snapshot_is_deeply_immutable(self) -> None:
        runner, _ = scenario_runner(
            component_parameters={"nested": {"items": [1, 2]}}
        )
        result = runner.run()

        with self.assertRaises((TypeError, AttributeError)):
            result.components["timing_policy"]["key"] = "changed"
        with self.assertRaises((TypeError, AttributeError)):
            result.components["parameters"]["nested"]["items"].append(3)
        self.assertEqual(
            result.components["parameters"]["nested"]["items"], (1, 2)
        )


class ViewIsolationTests(unittest.TestCase):
    def test_view_factory_follows_the_documented_view_rules(self) -> None:
        from app.backtesting.runtime import BacktestViewFactory
        from tests.backtest_runtime_fixture import make_candidate, universe_query

        axis = build_axis([D0])
        step = axis.at(0)
        factory = BacktestViewFactory(
            strategy_view=CountingStrategyView({D0: "1.00"}),
            universe_query=universe_query([make_candidate()]),
            engine_market_data=DictMarketData({D0: {INSTRUMENT_ID: ("1.00", "1.00")}}),
            scope_instrument_ids=(INSTRUMENT_ID,),
        )
        expectations = [
            (TimingPhase.PRE_OPEN_SETTLE, type(None)),
            (TimingPhase.OBSERVE, "engine"),
            (TimingPhase.MATCH, "engine"),
            (TimingPhase.ACCOUNT, type(None)),
            (TimingPhase.CASH_ACTIONS, "engine"),
            (TimingPhase.VALUE, "engine"),
            (TimingPhase.ANALYZE, type(None)),
            (TimingPhase.DECIDE, "strategy"),
            (TimingPhase.SUBMIT, type(None)),
        ]
        from app.backtesting.runtime import EngineDataView

        for phase, expected_kind in expectations:
            instruction = TimingInstruction(
                phase=phase,
                timestamp=step.end_time,
                data_view=(
                    None
                    if expected_kind is type(None)
                    else DataViewKind(expected_kind)
                ),
            )
            view = factory.for_phase(instruction, step, next_step=None)
            if expected_kind is type(None):
                self.assertIsNone(view, msg=phase.value)
            elif expected_kind == "engine":
                self.assertIsInstance(view, EngineDataView, msg=phase.value)
            else:
                self.assertIsInstance(view, StrategyDataDTO, msg=phase.value)

    def test_future_bar_query_is_rejected_without_touching_the_provider(
        self,
    ) -> None:
        from app.strategy_protocol.contract import DataCutoffViolationError

        view = CountingStrategyView({D0: "100.00"})
        observed: dict[str, bool] = {}

        class FuturePeekingStrategy(ScriptedStrategy):
            def on_step(self, context):
                try:
                    context.data.bars(INSTRUMENT_ID, end_date=D1)
                except DataCutoffViolationError:
                    observed["rejected"] = True
                else:
                    observed["rejected"] = False
                raise DataCutoffViolationError(D1, D0)

        runner = build_runner(
            run_id="run-future",
            axis=build_axis([D0, D1]),
            market_data=DictMarketData(
                {
                    D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                    D1: {INSTRUMENT_ID: ("100.00", "102.00")},
                }
            ),
            strategy_view=view,
            strategy=FuturePeekingStrategy({}),
        )
        with self.assertRaises(PhaseExecutionError) as context:
            runner.run()
        self.assertEqual(context.exception.error_type, "DataCutoffViolationError")
        self.assertEqual(context.exception.phase_key, "decide")
        # The facade rejected the future window and never reached the view.
        self.assertTrue(observed["rejected"])
        self.assertEqual(view.read_count, 0)

    def test_decision_context_exposes_only_immutable_dto_surfaces(self) -> None:
        from app.strategy_protocol.context import (
            DecisionContext,
            DeterministicClockDTO,
            PortfolioDTO,
            PreviousStepDTO,
        )

        runner, strategy = scenario_runner()
        runner.run()

        self.assertTrue(strategy.observed_contexts)
        for context in strategy.observed_contexts:
            self.assertIsInstance(context, DecisionContext)
            self.assertIsInstance(context.data, StrategyDataDTO)
            self.assertIsInstance(context.clock, DeterministicClockDTO)
            self.assertIsInstance(context.portfolio, PortfolioDTO)
            self.assertIsInstance(context.previous_step, PreviousStepDTO)
            # No field exposes an engine view, provider, ORM session, or any
            # mutable runtime object.
            for value in (
                context.clock,
                context.portfolio,
                context.previous_step,
                context.data,
                context.universe,
            ):
                self.assertNotIsInstance(value, EngineDataView)

    def test_decide_cutoff_equals_the_session_close(self) -> None:
        runner, strategy = scenario_runner()
        runner.run()

        self.assertEqual(len(strategy.observed_contexts), 2)
        for context in strategy.observed_contexts:
            self.assertEqual(context.decision_time, session_close(context.session_date))
            self.assertEqual(context.data_cutoff, session_close(context.session_date))
            self.assertEqual(context.clock.now(), session_close(context.session_date))


if __name__ == "__main__":
    unittest.main()
