"""Tests for the formal ``long_only_target_weights@1`` decision interpreter.

The cases follow task-package 05's acceptance list: weight validation,
whole-decision rejection, hold no-ops, omitted positions, sell odd-lot
policies, corporate-action cash states, snapshot-consistency priority,
and the structured interpretation result.
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.backtesting.accounting import OrderSide
from app.backtesting.reason_codes import DecisionReasonCode
from app.strategy_protocol.decisions import StrategyDecision
from app.strategy_protocol.interpretation import (
    CorporateActionCashStatus,
    CorporateActionSnapshot,
    DecisionStatus,
    InstrumentExecutionFacts,
    LongOnlyTargetWeightsInterpreter,
    PortfolioDecisionSnapshot,
    SellOddLotPolicy,
    SnapshotConsistencyStatus,
    WeightBoundaryStatus,
)

DECISION_TIME = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


def make_facts(
    instrument_id,
    *,
    lot_size="100",
    minimum_order_quantity="100",
    policy=SellOddLotPolicy.STRICT_LOT,
    odd_bypass=False,
    full_lot_bypass=False,
    full_precision_bypass=False,
):
    return InstrumentExecutionFacts(
        instrument_id=instrument_id,
        holding_precision=0,
        order_precision=0,
        lot_size=lot_size,
        minimum_order_quantity=minimum_order_quantity,
        sell_odd_lot_policy=policy,
        contract_multiplier="1",
        odd_lot_bypasses_lot_size=odd_bypass,
        full_liquidation_bypasses_lot_size=full_lot_bypass,
        full_liquidation_bypasses_order_precision=full_precision_bypass,
    )


def make_snapshot(
    *,
    equity="10000",
    cash="10000",
    positions=None,
    problems=(),
    cash_status=CorporateActionCashStatus.CREDITED,
):
    return PortfolioDecisionSnapshot(
        decision_snapshot_at=DECISION_TIME,
        cash=cash,
        equity=equity,
        valuation_status="complete",
        corporate_action_snapshot=CorporateActionSnapshot(cash_status),
        positions=positions or {},
        consistency_problems=problems,
    )


def decide(targets):
    return StrategyDecision(
        step_sequence=1,
        decision_time=DECISION_TIME,
        mode="target_weights",
        targets=dict(targets),
    )


class InterpreterFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.interpreter = LongOnlyTargetWeightsInterpreter()
        self.a = uuid4()
        self.b = uuid4()

    def interpret(
        self,
        targets,
        *,
        snapshot=None,
        facts=None,
        closes=None,
        equity="10000",
        positions=None,
        tol=None,
    ):
        interpreter = (
            LongOnlyTargetWeightsInterpreter(weight_sum_tolerance=tol)
            if tol is not None
            else self.interpreter
        )
        return interpreter.interpret(
            decide(targets),
            snapshot=snapshot
            or make_snapshot(equity=equity, positions=positions),
            facts=facts
            if facts is not None
            else {
                self.a: make_facts(self.a),
                self.b: make_facts(self.b),
            },
            unadjusted_market_closes=closes
            if closes is not None
            else {self.a: "10", self.b: "10"},
        )


class WeightValidationTests(InterpreterFixture):
    def test_invalid_weight_rejects_the_whole_decision(self) -> None:
        result = self.interpret({str(self.a): "1.5"})

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(result.order_intents, ())
        self.assertEqual(
            result.issues[0].code,
            DecisionReasonCode.INVALID_WEIGHT.value,
        )
        self.assertEqual(result.issues[0].stage.value, "decision")

    def test_negative_weight_rejects_the_whole_decision(self) -> None:
        result = self.interpret({str(self.a): "-0.1"})
        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(
            result.issues[0].code, DecisionReasonCode.INVALID_WEIGHT.value
        )

    def test_weight_sum_within_tolerance_is_recorded_structurally(self) -> None:
        result = self.interpret(
            {str(self.a): "0.5", str(self.b): "0.5005"}, tol="0.001"
        )

        self.assertIs(result.decision_status, DecisionStatus.ACCEPTED)
        self.assertEqual(result.weight_sum, Decimal("1.0005"))
        self.assertEqual(result.weight_sum_tolerance, Decimal("0.001"))
        self.assertIs(
            result.weight_boundary_status,
            WeightBoundaryStatus.OVER_WITHIN_TOLERANCE,
        )
        self.assertEqual(result.cash_weight, Decimal("0"))

    def test_weight_sum_exceeding_tolerance_rejects_the_whole_decision(
        self,
    ) -> None:
        result = self.interpret(
            {str(self.a): "0.5", str(self.b): "0.502"}, tol="0.001"
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(result.order_intents, ())
        self.assertEqual(
            result.issues[-1].code,
            DecisionReasonCode.WEIGHT_SUM_EXCEEDED.value,
        )
        self.assertIs(
            result.weight_boundary_status, WeightBoundaryStatus.EXCEEDED
        )

    def test_weights_are_never_normalized_and_cash_weight_is_reported(
        self,
    ) -> None:
        result = self.interpret({str(self.a): "0.6"})

        self.assertIs(result.decision_status, DecisionStatus.ACCEPTED)
        # The intent uses the raw 0.6 weight, not a normalized 1.0.
        self.assertEqual(
            result.instrument_results[0].target_value, Decimal("6000")
        )
        self.assertEqual(result.cash_weight, Decimal("0.4"))


class HoldAndNoopTests(InterpreterFixture):
    def test_hold_generates_no_orders(self) -> None:
        decision = StrategyDecision(
            step_sequence=1,
            decision_time=DECISION_TIME,
            mode="hold",
            targets={},
        )
        result = self.interpreter.interpret(
            decision,
            snapshot=make_snapshot(positions={self.a: Decimal("100")}),
            facts={self.a: make_facts(self.a)},
            unadjusted_market_closes={self.a: "10"},
        )

        self.assertIs(result.decision_status, DecisionStatus.ACCEPTED_NOOP)
        self.assertEqual(result.order_intents, ())

    def test_hold_still_rejects_on_an_inconsistent_snapshot(self) -> None:
        decision = StrategyDecision(
            step_sequence=1,
            decision_time=DECISION_TIME,
            mode="hold",
            targets={},
        )
        result = self.interpreter.interpret(
            decision,
            snapshot=make_snapshot(problems=("stale",)),
            facts={self.a: make_facts(self.a)},
            unadjusted_market_closes={self.a: "10"},
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(
            result.issues[0].code,
            DecisionReasonCode.DECISION_SNAPSHOT_STALE.value,
        )
        self.assertEqual(result.order_intents, ())

    def test_all_zero_deltas_are_a_noop(self) -> None:
        # 10,000 equity * 0.1 / 10 = 100 -> delta 0 against 100 held.
        result = self.interpret(
            {str(self.a): "0.1"}, positions={self.a: Decimal("100")}
        )

        self.assertIs(result.decision_status, DecisionStatus.ACCEPTED_NOOP)
        self.assertEqual(result.order_intents, ())


class TargetSizingTests(InterpreterFixture):
    def test_omitted_positions_become_zero_targets_with_warnings(self) -> None:
        facts = {
            entry.instrument_id: entry
            for entry in (
                make_facts(
                    self.a,
                    policy=SellOddLotPolicy.ALLOW_FULL_LIQUIDATION_ODD_LOT,
                    full_lot_bypass=True,
                    full_precision_bypass=True,
                ),
            )
        }
        result = self.interpret(
            {}, facts=facts, positions={self.a: Decimal("250")}
        )

        self.assertIs(result.decision_status, DecisionStatus.ACCEPTED)
        self.assertEqual(len(result.order_intents), 1)
        intent = result.order_intents[0]
        self.assertEqual(intent.instrument_id, self.a)
        self.assertIs(intent.side, OrderSide.SELL)
        self.assertEqual(intent.quantity, Decimal("250"))
        warning_codes = [warning.code for warning in result.warnings]
        self.assertIn("OMITTED_POSITION_ZERO_TARGET", warning_codes)
        self.assertIn(str(self.a), result.warnings[0].details.values())

    def test_target_quantities_use_only_the_unadjusted_market_close(self) -> None:
        result = self.interpret({str(self.a): "0.6"}, closes={self.a: "10"})

        sizing = result.instrument_results[0]
        self.assertEqual(sizing.unadjusted_market_close, Decimal("10"))
        self.assertEqual(sizing.target_value, Decimal("6000"))
        self.assertEqual(sizing.raw_quantity, Decimal("600"))
        self.assertEqual(sizing.target_quantity, Decimal("600"))
        self.assertEqual(sizing.delta, Decimal("600"))
        self.assertEqual(result.order_intents[0].quantity, Decimal("600"))

    def test_missing_close_price_cannot_be_sized_and_rejects(self) -> None:
        result = self.interpret({str(self.a): "0.6"}, closes={})

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(result.order_intents, ())
        self.assertEqual(
            result.issues[-1].code,
            DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE.value,
        )

    def test_contract_multiplier_participates_in_notional_sizing(self) -> None:
        facts = {self.a: make_facts(self.a)}
        facts[self.a] = InstrumentExecutionFacts(
            instrument_id=self.a,
            holding_precision=0,
            order_precision=0,
            lot_size="1",
            minimum_order_quantity="1",
            sell_odd_lot_policy=SellOddLotPolicy.STRICT_LOT,
            contract_multiplier="10",
        )
        result = self.interpret(
            {str(self.a): "0.6"}, facts=facts, closes={self.a: "10"}
        )

        # 6,000 / (10 * 10) = 60 units of the multiplier.
        self.assertEqual(
            result.instrument_results[0].target_quantity, Decimal("60")
        )


class SellOrderabilityTests(InterpreterFixture):
    def test_non_lot_holding_partial_sell_rejects_under_strict_lot(self) -> None:
        # Target 200 vs 250 held -> a 50-share odd-lot sale.
        result = self.interpret(
            {str(self.a): "0.2"}, positions={self.a: Decimal("250")}
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(result.order_intents, ())
        issue = result.issues[-1]
        self.assertEqual(
            issue.code, DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE.value
        )
        self.assertEqual(issue.details["instrument_id"], str(self.a))
        # A valid target elsewhere must NOT leak an order.
        self.assertFalse(
            [
                item
                for item in result.instrument_results
                if item.orderable
                and item.order_side is OrderSide.BUY
            ]
        )

    def test_allow_odd_lot_with_declared_bypass_produces_a_legal_sell(self) -> None:
        facts = {
            self.a: make_facts(
                self.a,
                minimum_order_quantity="50",
                policy=SellOddLotPolicy.ALLOW_ODD_LOT,
                odd_bypass=True,
            )
        }
        result = self.interpret(
            {str(self.a): "0.2"},
            facts=facts,
            positions={self.a: Decimal("250")},
        )

        self.assertIs(result.decision_status, DecisionStatus.ACCEPTED)
        self.assertEqual(result.order_intents[0].quantity, Decimal("50"))
        self.assertIs(result.order_intents[0].side, OrderSide.SELL)

    def test_allow_odd_lot_without_lot_size_exemption_does_not_pass_odd_lots(
        self,
    ) -> None:
        facts = {
            self.a: make_facts(
                self.a,
                policy=SellOddLotPolicy.ALLOW_ODD_LOT,
                odd_bypass=False,
            )
        }
        result = self.interpret(
            {str(self.a): "0.2"},
            facts=facts,
            positions={self.a: Decimal("250")},
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        detail = result.issues[-1].details["reason"]
        self.assertEqual(detail, "odd_lot_lot_size_exemption_missing")

    def test_full_liquidation_odd_lot_policy_applies_only_to_full_sales(
        self,
    ) -> None:
        facts = {
            self.a: make_facts(
                self.a,
                minimum_order_quantity="50",
                policy=SellOddLotPolicy.ALLOW_FULL_LIQUIDATION_ODD_LOT,
                full_lot_bypass=True,
                full_precision_bypass=True,
            )
        }
        liquidation = self.interpret(
            {},
            facts=facts,
            positions={self.a: Decimal("250")},
        )
        partial = self.interpret(
            {str(self.a): "0.2"},
            facts=facts,
            positions={self.a: Decimal("250")},
        )

        self.assertIs(liquidation.decision_status, DecisionStatus.ACCEPTED)
        self.assertEqual(liquidation.order_intents[0].quantity, Decimal("250"))
        self.assertIs(partial.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(partial.order_intents, ())

    def test_buy_delta_violating_lot_rules_rejects_like_the_documented_example(
        self,
    ) -> None:
        # Held 325 (odd), target 400 -> buy delta 75, not orderable in
        # strict-lot lots of 100.
        result = self.interpret(
            {str(self.a): "0.4"}, positions={self.a: Decimal("325")}
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(result.order_intents, ())
        self.assertEqual(
            result.issues[-1].code,
            DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE.value,
        )

    def test_long_only_interpreter_never_produces_short_targets(self) -> None:
        result = self.interpret(
            {str(self.a): "1.0"}, positions={self.a: Decimal("100")}
        )
        for intent in result.order_intents:
            self.assertIs(intent.side, OrderSide.BUY)


class ScopeAndRuleTests(InterpreterFixture):
    def test_targets_outside_the_allowed_scope_are_rejected(self) -> None:
        outside = uuid4()
        result = self.interpreter.interpret(
            decide({str(outside): "0.5"}),
            snapshot=make_snapshot(),
            facts={
                self.a: make_facts(self.a),
                outside: make_facts(outside),
            },
            unadjusted_market_closes={outside: "10"},
            allowed_instrument_ids={self.a},
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(
            result.issues[-1].code,
            DecisionReasonCode.INSTRUMENT_RULE_MISSING.value,
        )

    def test_missing_execution_rules_reject_the_whole_decision(self) -> None:
        result = self.interpret(
            {str(self.a): "0.5"}, facts={}
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(
            result.issues[-1].code,
            DecisionReasonCode.INSTRUMENT_RULE_MISSING.value,
        )

    def test_negative_current_position_rejects_as_snapshot_corruption(
        self,
    ) -> None:
        # Build the corruption directly through the public constructor's
        # validation-free path is impossible; use a rejected negative via
        # the interpreter guard by monkeypatching the frozen mapping.
        snapshot = make_snapshot()
        object.__setattr__(snapshot, "positions", {})
        # A negative current position cannot pass snapshot validation, so
        # the guard is exercised through a forged mapping type.
        class NegativePositions(dict):
            def __getitem__(self, key):
                return Decimal("-50")

            def get(self, key, default=None):
                return Decimal("-50")

        object.__setattr__(
            snapshot, "positions", NegativePositions({self.a: "-50"})
        )
        result = self.interpreter.interpret(
            decide({str(self.a): "0.5"}),
            snapshot=snapshot,
            facts={self.a: make_facts(self.a)},
            unadjusted_market_closes={self.a: "10"},
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(
            result.issues[-1].code,
            DecisionReasonCode.DECISION_SNAPSHOT_INVALID.value,
        )


class CorporateActionCashTests(InterpreterFixture):
    def test_not_credited_dividend_cash_is_not_counted_into_equity(self) -> None:
        # Equity passed in excludes the pending dividend; sizing works on
        # that value alone.
        result = self.interpret(
            {str(self.a): "0.5"},
            equity="9000",
            snapshot=make_snapshot(
                equity="9000",
                cash="9000",
                cash_status=CorporateActionCashStatus.NOT_CREDITED,
            ),
        )

        self.assertIs(result.decision_status, DecisionStatus.ACCEPTED)
        self.assertEqual(
            result.instrument_results[0].target_value, Decimal("4500")
        )
        # 4,500 / 10 = 450 raw, floored onto the 100-share lot grid.
        self.assertEqual(result.order_intents[0].quantity, Decimal("400"))

    def test_unknown_corporate_action_cash_rejects_the_whole_decision(
        self,
    ) -> None:
        result = self.interpret(
            {str(self.a): "0.5"},
            snapshot=make_snapshot(
                cash_status=CorporateActionCashStatus.UNKNOWN
            ),
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(result.order_intents, ())
        self.assertEqual(
            result.issues[0].code,
            DecisionReasonCode.DECISION_SNAPSHOT_INVALID.value,
        )
        self.assertEqual(
            result.snapshot_consistency_status,
            SnapshotConsistencyStatus.INVALID,
        )

    def test_not_credited_cash_leaves_an_auditable_warning(self) -> None:
        result = self.interpret(
            {str(self.a): "0.5"},
            equity="9000",
            snapshot=make_snapshot(
                equity="9000",
                cash="9000",
                cash_status=CorporateActionCashStatus.NOT_CREDITED,
            ),
        )

        warning_codes = [warning.code for warning in result.warnings]
        self.assertIn("CORPORATE_ACTION_CASH_NOT_CREDITED", warning_codes)
        warning = next(
            w
            for w in result.warnings
            if w.code == "CORPORATE_ACTION_CASH_NOT_CREDITED"
        )
        self.assertEqual(
            warning.details["corporate_action_cash_status"], "not_credited"
        )

    def test_unknown_decision_mode_rejects_through_the_protocol_field(
        self,
    ) -> None:
        decision = StrategyDecision(
            step_sequence=1,
            decision_time=DECISION_TIME,
            mode="foo",
            targets={},
        )

        result = self.interpreter.interpret(
            decision,
            snapshot=make_snapshot(),
            facts={self.a: make_facts(self.a)},
            unadjusted_market_closes={self.a: "10"},
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(result.order_intents, ())
        # A mode error is a decision-protocol violation, NOT a snapshot
        # problem: it must not borrow DECISION_SNAPSHOT_INVALID and must
        # leave the consistency status untouched.
        self.assertIsNotNone(result.protocol_reason)
        self.assertEqual(
            result.protocol_reason.code, "UNKNOWN_DECISION_MODE"
        )
        self.assertEqual(result.protocol_reason.details["mode"], "foo")
        self.assertEqual(
            result.snapshot_consistency_status,
            SnapshotConsistencyStatus.CONSISTENT,
        )
        self.assertEqual(
            [issue.code for issue in result.issues if issue.code.startswith("DECISION_SNAPSHOT")],
            [],
        )

    def test_not_credited_warning_is_present_on_every_result_path(self) -> None:
        def warning_codes(result):
            return [w.code for w in result.warnings]

        # Accepted path.
        accepted = self.interpret(
            {str(self.a): "0.5"},
            equity="9000",
            snapshot=make_snapshot(
                equity="9000", cash_status=CorporateActionCashStatus.NOT_CREDITED
            ),
        )
        self.assertIn(
            "CORPORATE_ACTION_CASH_NOT_CREDITED", warning_codes(accepted)
        )
        # Hold path.
        hold = self.interpreter.interpret(
            StrategyDecision(
                step_sequence=1,
                decision_time=DECISION_TIME,
                mode="hold",
                targets={},
            ),
            snapshot=make_snapshot(
                cash_status=CorporateActionCashStatus.NOT_CREDITED
            ),
            facts={self.a: make_facts(self.a)},
            unadjusted_market_closes={self.a: "10"},
        )
        self.assertIn(
            "CORPORATE_ACTION_CASH_NOT_CREDITED", warning_codes(hold)
        )
        # Weight-sum rejection path.
        rejected = self.interpret(
            {str(self.a): "1.5"},
            snapshot=make_snapshot(
                cash_status=CorporateActionCashStatus.NOT_CREDITED
            ),
        )
        self.assertIn(
            "CORPORATE_ACTION_CASH_NOT_CREDITED", warning_codes(rejected)
        )

    def test_corrupt_close_price_produces_structured_rejection(self) -> None:
        result = self.interpret({str(self.a): "0.5"}, closes={self.a: "abc"})

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(result.order_intents, ())
        issue = result.issues[-1]
        self.assertEqual(
            issue.code, DecisionReasonCode.TARGET_QUANTITY_NOT_ORDERABLE.value
        )
        self.assertEqual(issue.details["reason"], "invalid_unadjusted_market_close")

    def test_non_mapping_targets_payload_rejects_through_protocol_reason(
        self,
    ) -> None:
        # Falsy payloads must not be coerced to an empty target set: every
        # non-mapping payload is a protocol violation.
        for bad_payload in (None, [], "", 0, False, ["bad"]):
            with self.subTest(payload=bad_payload):
                decision = StrategyDecision(
                    step_sequence=1,
                    decision_time=DECISION_TIME,
                    mode="target_weights",
                    targets={},
                )
                # Bypass the payload validator to inject the raw payload.
                object.__setattr__(decision, "targets", bad_payload)

                result = self.interpreter.interpret(
                    decision,
                    snapshot=make_snapshot(),
                    facts={self.a: make_facts(self.a)},
                    unadjusted_market_closes={self.a: "10"},
                )

                self.assertIs(
                    result.decision_status, DecisionStatus.REJECTED
                )
                self.assertEqual(result.order_intents, ())
                self.assertIsNotNone(result.protocol_reason)
                self.assertEqual(
                    result.protocol_reason.code, "INVALID_TARGETS_PAYLOAD"
                )
                self.assertEqual(
                    result.protocol_reason.details["mode"], "target_weights"
                )
                self.assertEqual(result.protocol_reason.details["targets_type"],
                                 type(bad_payload).__name__)
                self.assertEqual(result.issues, ())

    def test_malformed_target_key_is_distinguished_in_details(self) -> None:
        decision = StrategyDecision(
            step_sequence=1,
            decision_time=DECISION_TIME,
            mode="target_weights",
            targets={},
        )
        # Bypass the payload validator to inject a malformed key directly.
        object.__setattr__(decision, "targets", {"not-a-uuid": "0.5"})
        result = self.interpreter.interpret(
            decision,
            snapshot=make_snapshot(),
            facts={self.a: make_facts(self.a)},
            unadjusted_market_closes={self.a: "10"},
        )

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        issue = next(
            i
            for i in result.issues
            if i.details.get("reason") == "invalid_instrument_id_format"
        )
        self.assertEqual(issue.details["targets_key"], "not-a-uuid")


class SnapshotConsistencyTests(InterpreterFixture):
    def _interpret_with_problems(self, problems):
        return self.interpret(
            {str(self.a): "0.5"}, snapshot=make_snapshot(problems=problems)
        )

    def test_consistent_snapshot_accepts(self) -> None:
        result = self._interpret_with_problems(())
        self.assertEqual(
            result.snapshot_consistency_status,
            SnapshotConsistencyStatus.CONSISTENT,
        )

    def test_stale_snapshot_maps_one_to_one_onto_its_reason_code(self) -> None:
        result = self._interpret_with_problems(("stale",))
        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(
            result.issues[0].code,
            DecisionReasonCode.DECISION_SNAPSHOT_STALE.value,
        )

    def test_multiple_problems_pick_the_main_code_by_fixed_priority(self) -> None:
        result = self._interpret_with_problems(("incomplete", "invalid"))

        self.assertIs(result.decision_status, DecisionStatus.REJECTED)
        self.assertEqual(
            result.issues[0].code,
            DecisionReasonCode.DECISION_SNAPSHOT_INVALID.value,
        )
        codes = {issue.code for issue in result.issues}
        self.assertIn(DecisionReasonCode.DECISION_SNAPSHOT_INCOMPLETE.value, codes)

        conflicted_first = self._interpret_with_problems(
            ("incomplete", "stale", "conflicted")
        )
        self.assertEqual(
            conflicted_first.issues[0].code,
            DecisionReasonCode.DECISION_SNAPSHOT_CONFLICTED.value,
        )


class RegistryIdentityTests(unittest.TestCase):
    def test_star_import_exposes_exactly_the_documented_names(self) -> None:
        import app.strategy_protocol.interpretation as module

        # ``__all__`` must only name attributes that exist; a phantom
        # entry breaks ``from ... import *`` with an AttributeError.
        for name in module.__all__:
            self.assertTrue(hasattr(module, name), name)


    def test_formal_interpreter_identity_is_registered(self) -> None:
        from app.backtesting.registry import (
            DECISION_INTERPRETER_KEY_LONG_ONLY_TARGET_WEIGHTS,
            build_default_component_registry,
        )

        registry = build_default_component_registry()
        entry = registry.resolve(
            DECISION_INTERPRETER_KEY_LONG_ONLY_TARGET_WEIGHTS, 1
        )

        self.assertEqual(entry.name_zh, "只多目标权重")
        self.assertEqual(entry.capabilities["long_only"], True)
        instance = entry.construct({"weight_sum_tolerance": "0.001"})
        self.assertIsInstance(instance, LongOnlyTargetWeightsInterpreter)
        self.assertEqual(instance.interpreter_key, "long_only_target_weights")
        self.assertEqual(instance.interpreter_version, 1)


if __name__ == "__main__":
    unittest.main()
