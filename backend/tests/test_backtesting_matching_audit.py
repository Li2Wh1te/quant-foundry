"""Acceptance tests for task package 05B: matching and rule-version audit.

Covers the one-shot next-open match (no rollover), suspension and
missing-open gates, collection-order-independent stable sorting, the
documented minimum-commission lot-reduction example, slippage tick
rounding, and the complete audited fill event including the settlement
lot reference.
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.backtesting.accounting import OrderSide
from app.backtesting.bar_matching import (
    BarOpenMatchingModel,
    StatelessFeeQuoteProvider,
)
from app.backtesting.execution import (
    BarMarketExecutionModel,
    MarketState,
    MatchContext,
    Order,
    OrderStatus,
)
from app.backtesting.fees import (
    FeeCalculator,
    FeeRoundingLevel,
    FeeRoundingMode,
    FeeRule,
    FeeSchedule,
)
from app.backtesting.session_matching import MatchLedger
from app.backtesting.slippage import BpsSlippageModel
from app.strategy_protocol.interpretation import (
    InstrumentExecutionFacts,
    SellOddLotPolicy,
)

OPEN_TS = datetime(2026, 8, 5, 9, 30, tzinfo=timezone.utc)


def min_commission_schedule():
    return FeeSchedule(
        key="doc-min-commission",
        version=1,
        fee_rules=(
            FeeRule(
                key="commission",
                category="commission",
                side="both",
                rate="0.0003",
                minimum="5",
                rounding_level=FeeRoundingLevel.FEE_ITEM,
                rounding_scope="commission",
                rounding_mode=FeeRoundingMode.HALF_UP,
                rounding_precision="0.01",
            ),
        ),
    )


class StableOrderingTests(unittest.TestCase):
    """Acceptance 15: batch results never depend on collection order."""

    def setUp(self) -> None:
        self.iid = uuid4()
        self.model = BarMarketExecutionModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_calculator=FeeCalculator(min_commission_schedule()),
        )

    def order(self, sequence, side=OrderSide.BUY, quantity="100"):
        return Order(
            order_id=uuid4(),
            intent_id=uuid4(),
            instrument_id=self.iid,
            side=side,
            quantity=quantity,
            submitted_at=OPEN_TS,
            submission_sequence=sequence,
        )

    def state(self):
        return MarketState(
            instrument_id=self.iid,
            timestamp=OPEN_TS,
            open_price="10",
            price_tick="0.01",
        )

    def context(self, cash="1000000"):
        return MatchContext(currency="CNY", available_cash=cash)

    def test_fill_order_is_identical_under_input_permutations(self) -> None:
        orders = [self.order(3), self.order(1), self.order(2)]

        forward = self.model.match(
            orders, {self.iid: self.state()}, self.context()
        )
        reversed_orders = list(reversed(orders))
        for order in orders:
            # Reset runtime state so both runs start identically.
            order.status = OrderStatus.SUBMITTED
            order.filled_quantity = Decimal("0")
        backward = self.model.match(
            reversed_orders, {self.iid: self.state()}, self.context()
        )

        self.assertEqual(
            [fill.order_id for fill in forward.fills],
            [fill.order_id for fill in backward.fills],
        )
        # The four-key order: sells first, then instrument, submission
        # sequence, then order identity.
        self.assertEqual(
            [fill.order_id for fill in forward.fills],
            [orders[1].order_id, orders[2].order_id, orders[0].order_id],
        )


class OneShotMatchGateTests(unittest.TestCase):
    """Acceptance 14: one attempt at the next open session; suspended
    instruments and missing opens expire without rolling over."""

    def setUp(self) -> None:
        self.iid = uuid4()
        self.model = BarMarketExecutionModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_calculator=FeeCalculator(min_commission_schedule()),
        )
        self.order = Order(
            order_id=uuid4(),
            intent_id=uuid4(),
            instrument_id=self.iid,
            side=OrderSide.BUY,
            quantity="100",
            submitted_at=OPEN_TS,
            submission_sequence=1,
        )

    def match(self, state):
        context = MatchContext(currency="CNY", available_cash="100000")
        result = self.model.match([self.order], {self.iid: state}, context)
        return result, context

    def test_suspended_instrument_expires_with_its_reason(self) -> None:
        state = MarketState(
            instrument_id=self.iid,
            timestamp=OPEN_TS,
            open_price="10",
            price_tick="0.01",
            is_suspended=True,
        )
        result, _ = self.match(state)
        self.assertEqual(result.fills, ())
        self.assertEqual(
            result.skipped_orders[0].reason, "instrument_suspended"
        )
        self.assertEqual(self.order.status, OrderStatus.EXPIRED)

    def test_missing_open_price_expires_without_a_price_guess(self) -> None:
        state = MarketState(
            instrument_id=self.iid,
            timestamp=OPEN_TS,
            open_price=None,
            price_tick="0.01",
            open_available=False,
        )
        result, _ = self.match(state)
        self.assertEqual(result.fills, ())
        self.assertEqual(result.skipped_orders[0].reason, "open_unavailable")

    def test_expired_order_never_reenters_a_later_match(self) -> None:
        # A terminal order fed into a later batch is skipped untouched.
        state = MarketState(
            instrument_id=self.iid,
            timestamp=OPEN_TS,
            open_price="10",
            price_tick="0.01",
            is_suspended=True,
        )
        self.match(state)
        assert self.order.status is OrderStatus.EXPIRED

        open_state = MarketState(
            instrument_id=self.iid,
            timestamp=OPEN_TS,
            open_price="10",
            price_tick="0.01",
        )
        result, context = self.match(open_state)
        self.assertEqual(result.fills, ())
        self.assertEqual(context.available_cash, Decimal("100000"))


class MinimumCommissionLotReductionTests(unittest.TestCase):
    """Acceptance 6 / documented example: with cash 2,004, price 10 and a
    max(notional x 0.03%, 5) commission, only 100 of the requested 200
    units are affordable -- the allocator recomputes fees per reduced lot
    instead of sizing on a minimum-free quote."""

    def test_allocator_drops_one_lot_and_recomputes_the_minimum_fee(self) -> None:
        iid = uuid4()
        model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=min_commission_schedule()
            ),
        )
        order = Order(
            order_id=uuid4(),
            intent_id=uuid4(),
            instrument_id=iid,
            side=OrderSide.BUY,
            quantity="200",
            submitted_at=OPEN_TS,
            submission_sequence=1,
        )
        ledger = MatchLedger(
            currency="CNY",
            cash_balance_snapshot=Decimal("2004"),
            available_cash=Decimal("2004"),
        )
        facts = InstrumentExecutionFacts(
            instrument_id=iid,
            holding_precision=0,
            order_precision=0,
            lot_size="100",
            minimum_order_quantity="100",
            sell_odd_lot_policy=SellOddLotPolicy.STRICT_LOT,
            contract_multiplier="1",
            fee_categories=frozenset({"commission"}),
        )
        state = MarketState(
            instrument_id=iid,
            timestamp=OPEN_TS,
            open_price="10",
            price_tick="0.01",
        )

        result = model.match(
            orders=[order],
            market_states={iid: state},
            ledger=ledger,
            facts={iid: facts},
            match_at=OPEN_TS,
            position_quantities={},
        )

        update = result.order_updates[0]
        self.assertEqual(update.filled_quantity, Decimal("100"))
        self.assertEqual(update.remaining_quantity, Decimal("100"))
        self.assertEqual(update.remaining_status, "terminal_unfilled")
        self.assertEqual(
            update.reason_code.code, "INSUFFICIENT_CASH"
        )
        self.assertEqual(
            update.remaining_reason_code.code, "expired_after_partial_fill"
        )
        fill = result.fills[0]
        # 100 units at 10 cost 1,000 plus the minimum commission of 5.
        self.assertEqual(fill.fees, Decimal("5"))
        self.assertEqual(ledger.available_cash, Decimal("999"))


class SlippageAuditTests(unittest.TestCase):
    """Documented example: 15 bps on a 10.00 reference with a 0.01 tick
    rounds adversely -- buys up to 10.02, sells down to 9.98."""

    def test_bps_then_adverse_tick_rounding(self) -> None:
        model = BpsSlippageModel(slippage_bps="15", price_tick="0.01")

        buy = model.apply("10.00", "buy", price_tick="0.01")
        sell = model.apply("10.00", "sell", price_tick="0.01")

        self.assertEqual(buy.execution_price, Decimal("10.02"))
        self.assertGreater(buy.price_delta, Decimal("0"))
        self.assertEqual(sell.execution_price, Decimal("9.98"))
        self.assertLess(sell.price_delta, Decimal("0"))
        # The snapshot identifies the exact model configuration.
        self.assertEqual(buy.model_key, "bps")
        self.assertEqual(buy.model_version, 1)
        self.assertEqual(buy.slippage_bps, Decimal("15"))
        self.assertEqual(buy.price_tick, Decimal("0.01"))

    def test_none_model_is_a_distinct_configuration_from_bps(self) -> None:
        none_model = BpsSlippageModel.none(price_tick="0.01")
        zero_bps = BpsSlippageModel(slippage_bps="0", price_tick="0.01")

        self.assertEqual(none_model.model_key, "none")
        self.assertEqual(zero_bps.model_key, "bps")


class FillEventAuditTests(unittest.TestCase):
    """Acceptance 17: every fill event carries the reference price, the
    execution price, slippage identity, the fee breakdown, the contract
    multiplier, notional, and its settlement lot id."""

    def test_runtime_fill_created_payload_carries_the_full_audit(self) -> None:
        from tests.backtest_runtime_fixture import (
            CountingStrategyView,
            DictMarketData,
            build_axis,
            build_runner,
        )

        from tests.backtest_runtime_fixture import INSTRUMENT_ID

        d0 = datetime(2026, 8, 3).date()
        d1 = datetime(2026, 8, 4).date()
        d2 = datetime(2026, 8, 5).date()
        axis = build_axis([d0, d1, d2])
        market_data = DictMarketData(
            {
                d0: {INSTRUMENT_ID: ("99.00", "100.00")},
                d1: {INSTRUMENT_ID: ("100.00", "102.00")},
                d2: {INSTRUMENT_ID: ("101.00", "103.00")},
            }
        )

        runner = build_runner(
            run_id="run-fill-audit",
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView({d0: "100.00", d1: "102.00"}),
            strategy=__import__(
                "tests.backtest_runtime_fixture", fromlist=["ScriptedStrategy"]
            ).ScriptedStrategy({0: {str(INSTRUMENT_ID): "0.5"}}),
            execution_model=BarMarketExecutionModel(
                slippage_model=BpsSlippageModel(
                    slippage_bps="15", price_tick="0.01"
                ),
                fee_calculator=FeeCalculator(min_commission_schedule()),
                model_key="bar_market",
                model_version=1,
            ),
            initial_cash="100000",
        )

        result = runner.run()

        created = [
            e for e in result.events if e.event_type == "fill_created"
        ]
        # Fill one is the opening buy; fill two is the end-of-run
        # flatten sell (whose lot id stays empty).
        self.assertEqual(len(created), 2)
        payload = created[0].payload
        # Reference vs execution price: 15 bps adverse slippage applied
        # to the 100.00 reference (100.15 lands on the 0.01 tick grid).
        self.assertEqual(payload["reference_price"], Decimal("100"))
        self.assertEqual(payload["execution_price"], Decimal("100.15"))
        self.assertEqual(payload["quantity"], Decimal("500"))
        self.assertEqual(payload["contract_multiplier"], Decimal("1"))
        self.assertEqual(payload["notional"], Decimal("50075"))
        self.assertGreater(payload["fees"], Decimal("0"))
        breakdown = payload["fee_breakdown"]
        self.assertEqual(breakdown["schedule_key"], "doc-min-commission")
        self.assertEqual(breakdown["schedule_version"], 1)
        self.assertTrue(breakdown["components"])
        self.assertEqual(payload["slippage_model_key"], "bps")
        self.assertEqual(payload["slippage_model_version"], 1)
        self.assertEqual(payload["slippage_bps"], Decimal("15"))
        self.assertIn("price_tick", payload["slippage_model_parameters"])
        # A buy fill always references the settlement lot it produced;
        # the lot exists once the fill is applied, so the reference is
        # audited on the applied event.
        self.assertIsNone(payload["settlement_lot_id"])
        applied = [
            e for e in result.events if e.event_type == "fill_applied"
        ]
        lot_id = applied[0].payload["settlement_lot_id"]
        self.assertIsNotNone(lot_id)
        # The lot id is a stable machine identifier.
        UUID(lot_id)

    def test_component_snapshot_captures_the_complete_fee_rule(self) -> None:
        from tests.backtest_runtime_fixture import (
            CountingStrategyView,
            DictMarketData,
            INSTRUMENT_ID,
            ScriptedStrategy,
            build_axis,
            build_runner,
        )

        from datetime import date as _date

        d0 = _date(2026, 8, 3)
        d1 = _date(2026, 8, 4)
        d2 = _date(2026, 8, 5)
        runner = build_runner(
            run_id="run-fee-snapshot",
            axis=build_axis([d0, d1, d2]),
            market_data=DictMarketData(
                {
                    d0: {INSTRUMENT_ID: ("99.00", "100.00")},
                    d1: {INSTRUMENT_ID: ("100.00", "102.00")},
                    d2: {INSTRUMENT_ID: ("101.00", "103.00")},
                }
            ),
            strategy_view=CountingStrategyView({d0: "100.00"}),
            strategy=ScriptedStrategy({}),
            execution_model=BarMarketExecutionModel(
                slippage_model=BpsSlippageModel.none(price_tick="0.01"),
                fee_calculator=FeeCalculator(min_commission_schedule()),
                model_key="bar_market",
                model_version=1,
            ),
            initial_cash="100000",
        )
        result = runner.run()

        fee_snapshot = dict(result.components)["fee_schedule"]
        self.assertEqual(fee_snapshot["key"], "doc-min-commission")
        rule = fee_snapshot["fee_rules"][0]
        # Every money-moving field of the rule is reproducible.
        self.assertEqual(rule["base_measure"], "gross_notional")
        self.assertEqual(rule["charge_timing"], "on_fill")
        self.assertEqual(rule["rule_type"], "simple_rate")
        self.assertEqual(rule["currency"], None)
        self.assertEqual(rule["applicability"], {})


class FeeRuleValidationTests(unittest.TestCase):
    """Acceptance 5: declared bases, minimums, fixed amounts, rounding,
    and the unsupported-rule rejection list are all enforced."""

    def rule(self, **overrides):
        values = dict(
            key="commission",
            category="commission",
            side="both",
            rate="0",
            minimum="0",
            rounding_level=FeeRoundingLevel.FEE_ITEM,
            rounding_scope="commission",
            rounding_mode=FeeRoundingMode.HALF_UP,
            rounding_precision="0.01",
        )
        values.update(overrides)
        return FeeRule(**values)

    def calculate(self, rules, *, side="buy", notional="1000", quantity=None):
        from app.backtesting.fees import FeeCalculator

        return FeeCalculator(FeeSchedule(key="s", version=1, fee_rules=tuple(rules))).calculate(
            side=side, notional=notional, currency="CNY", quantity=quantity
        )

    def test_gross_notional_base_uses_rate_minimum_and_fixed(self) -> None:
        breakdown = self.calculate(
            [self.rule(rate="0.001", minimum="3", fixed_amount="2")]
        )
        # max(1000 x 0.001, 3) + 2 = 3, rounded half-up to 0.01 grid.
        self.assertEqual(breakdown.total, Decimal("5"))

    def test_quantity_base_requires_the_filled_quantity(self) -> None:
        per_unit = self.rule(
            rate="0.05", minimum="0", base_measure="quantity"
        )
        self.assertEqual(
            self.calculate([per_unit], notional="999", quantity="200").total,
            Decimal("10"),
        )
        from app.backtesting.fees import FeeRuleUnresolvedError

        with self.assertRaises(FeeRuleUnresolvedError):
            self.calculate([per_unit], notional="999")

    def test_fixed_base_charges_only_the_fixed_amount(self) -> None:
        flat = self.rule(fixed_amount="7", base_measure="fixed")
        self.assertEqual(self.calculate([flat]).total, Decimal("7"))

    def test_incomplete_rounding_configuration_is_rejected_for_runs(self) -> None:
        incomplete = self.rule(rounding_mode=None)
        with self.assertRaises(Exception) as context:
            incomplete.validate_for_run()
        self.assertIn("rounding", str(context.exception))

    def test_fixed_rule_with_configured_rate_or_minimum_is_rejected(self) -> None:
        with self.assertRaises(Exception) as context:
            self.rule(base_measure="fixed", minimum="5").validate_for_run()
        self.assertIn("fixed base", str(context.exception))

    def test_rebates_tiered_rates_and_negative_amounts_are_rejected(self) -> None:
        from app.backtesting.fees import FeeError, FeeRuleType

        # A rebate is a negative amount: structurally impossible.
        with self.assertRaises(Exception):
            self.rule(rate="-0.0001")
        # Tiered / capped / waived shapes carry no supported type.
        with self.assertRaises(FeeError) as context:
            self.rule(rule_type="tiered_rate")
        self.assertIn("rebates", str(context.exception))
        # Only the simple shape exists for formal runs.
        self.assertEqual(FeeRuleType.SIMPLE_RATE.value, "simple_rate")

    def test_currency_mismatch_fails_closed(self) -> None:
        from app.backtesting.fees import FeeRuleUnresolvedError

        usd_only = self.rule(currency="USD")
        with self.assertRaises(FeeRuleUnresolvedError):
            self.calculate([usd_only])


if __name__ == "__main__":
    unittest.main()
