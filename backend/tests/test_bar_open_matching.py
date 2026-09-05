"""Tests for the deterministic bar-open matching model.

The cases follow task-package 05's acceptance list: sequential multi-sell
updates, precise availability reasons, negative net proceeds, the one-pass
pro-rata plus per-lot buy allocator, stateless fee quoting,
``submission_sequence`` continuity, slippage-based funding checks,
remaining-quantity bookkeeping, and the absence of ``submitted`` in final
results.
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.backtesting.accounting import OrderSide
from app.backtesting.execution import (
    MarketState,
    Order,
    OrderStatus,
    SubmissionSequenceAllocator,
)
from app.backtesting.bar_matching import (
    ALLOCATION_PHASE_PRO_RATA,
    ALLOCATION_STATUS_NOT_SUBMITTED,
    BarOpenMatchingModel,
    StatelessFeeQuoteProvider,
)
from app.backtesting.fees import (
    FeeBaseMeasure,
    FeeRule,
    FeeRoundingLevel,
    FeeRoundingMode,
    FeeSchedule,
)
from app.backtesting.reason_codes import MatchReasonCode
from app.backtesting.session_matching import MatchLedger
from app.backtesting.slippage import BpsSlippageModel
from app.strategy_protocol.interpretation import (
    InstrumentExecutionFacts,
    SellOddLotPolicy,
)

OPEN_TS = datetime(2026, 8, 5, 9, 30, tzinfo=timezone.utc)


def zero_fee_schedule():
    return FeeSchedule(
        key="test-zero-cost",
        version=1,
        fee_rules=(
            FeeRule(
                key="commission",
                category="commission",
                side="both",
                rate="0",
                minimum="0",
                rounding_level=FeeRoundingLevel.FEE_ITEM,
                rounding_scope="commission",
                rounding_mode=FeeRoundingMode.HALF_UP,
                rounding_precision="0.01",
            ),
        ),
    )


def min_fee_schedule(minimum="5"):
    return FeeSchedule(
        key="test-min-commission",
        version=1,
        fee_rules=(
            FeeRule(
                key="commission",
                category="commission",
                side="both",
                rate="0.0003",
                minimum=minimum,
                rounding_level=FeeRoundingLevel.FEE_ITEM,
                rounding_scope="commission",
                rounding_mode=FeeRoundingMode.HALF_UP,
                rounding_precision="0.01",
            ),
        ),
    )


def huge_fixed_fee_schedule():
    return FeeSchedule(
        key="test-huge-fixed",
        version=1,
        fee_rules=(
            FeeRule(
                key="commission",
                category="commission",
                side="both",
                rate="0",
                minimum="0",
                fixed_amount="10000",
                rounding_level=FeeRoundingLevel.FEE_ITEM,
                rounding_scope="commission",
                rounding_mode=FeeRoundingMode.HALF_UP,
                rounding_precision="0.01",
            ),
        ),
    )


class MatchingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.iid = uuid4()
        self.model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=zero_fee_schedule()
            ),
        )

    def make_facts(self, **overrides):
        values = dict(
            instrument_id=self.iid,
            holding_precision=0,
            order_precision=0,
            lot_size="100",
            minimum_order_quantity="100",
            sell_odd_lot_policy=SellOddLotPolicy.STRICT_LOT,
            contract_multiplier="1",
            fee_categories=frozenset({"commission"}),
        )
        values.update(overrides)
        return InstrumentExecutionFacts(**values)

    def make_state(self, *, open_price="10", **overrides):
        values = dict(
            instrument_id=self.iid,
            timestamp=OPEN_TS,
            open_price=open_price,
            price_tick="0.01",
        )
        values.update(overrides)
        return MarketState(**values)

    def make_ledger(self, *, cash="0", available=None, total=None):
        # ``total`` is the whole held quantity; it defaults to the
        # available part so simple fixtures behave like fully-unlocked
        # positions.
        self.position_total = Decimal(total if total is not None else (available or 0))
        return MatchLedger(
            currency="CNY",
            cash_balance_snapshot=Decimal(cash),
            available_cash=Decimal(cash),
            available_quantities={self.iid: Decimal(available or 0)},
        )

    def make_order(self, side, quantity, sequence, **overrides):
        values = dict(
            order_id=uuid4(),
            intent_id=uuid4(),
            instrument_id=self.iid,
            side=side,
            quantity=quantity,
            submitted_at=OPEN_TS,
            submission_sequence=sequence,
        )
        values.update(overrides)
        return Order(**values)

    def run_match(
        self, orders, *, ledger, states=None, facts=None, position_quantities=None
    ):
        return self.model.match(
            orders=orders,
            market_states=states or {self.iid: self.make_state()},
            ledger=ledger,
            facts=facts or {self.iid: self.make_facts()},
            match_at=OPEN_TS,
            position_quantities=(
                position_quantities
                if position_quantities is not None
                else {self.iid: self.position_total}
            ),
        )

    def update_for(self, result, order):
        return next(
            record
            for record in result.order_updates
            if record.order_id == order.order_id
        )


class SequentialSellTests(MatchingFixture):
    def test_later_sell_observes_earlier_sell_deductions(self) -> None:
        ledger = self.make_ledger(cash="0", available="150")
        first = self.make_order(OrderSide.SELL, "100", 1)
        second = self.make_order(OrderSide.SELL, "100", 2)

        result = self.run_match([second, first], ledger=ledger)

        first_update = self.update_for(result, first)
        second_update = self.update_for(result, second)
        # First sell fills in full and immediately frees its proceeds.
        self.assertIs(first_update.order_status, OrderStatus.FILLED)
        self.assertEqual(first_update.remaining_status, "none")
        self.assertIsNone(first_update.reason_code)
        self.assertEqual(ledger.available_cash, Decimal("1000"))
        # Second sell sees only 50 left: an illegal remainder expires with
        # the precise reason instead of trading.
        self.assertIs(second_update.order_status, OrderStatus.EXPIRED)
        self.assertEqual(second_update.filled_quantity, Decimal("0"))
        # The 50-share remainder is both below the minimum order size and
        # not a lot multiple; the minimum check is the first precise
        # reason reported.
        self.assertEqual(
            second_update.remaining_reason_code.code,
            MatchReasonCode.AVAILABLE_QUANTITY_BELOW_MINIMUM.value,
        )
        self.assertEqual(ledger.available_quantities[self.iid], Decimal("50"))
        # Deterministic regardless of caller collection order.
        self.assertEqual(
            [u.order_id for u in result.order_updates],
            [first.order_id, second.order_id],
        )


class SellAvailabilityReasonTests(MatchingFixture):
    def test_self_illegal_below_minimum_request_is_rejected(self) -> None:
        # A 200-share request is a legal lot multiple but below the 300
        # minimum: the ORDER itself is illegal and is rejected.
        facts = {
            self.iid: self.make_facts(minimum_order_quantity="300")
        }
        ledger = self.make_ledger(cash="0", available="200")
        order = self.make_order(OrderSide.SELL, "200", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(update.remaining_status, "not_executed")
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ORDER_QUANTITY_BELOW_MINIMUM.value,
        )
        self.assertEqual(ledger.available_quantities[self.iid], Decimal("200"))

    def test_capped_availability_below_minimum_expires_with_available_code(
        self,
    ) -> None:
        # A legal 300-share request against only 150 sellable shares: the
        # capped candidate (150) sits below the 200 minimum, so nothing
        # trades and the AVAILABLE_* code expires the legal order.
        facts = {
            self.iid: self.make_facts(minimum_order_quantity="200")
        }
        ledger = self.make_ledger(cash="0", available="150", total="300")
        order = self.make_order(OrderSide.SELL, "300", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.EXPIRED)
        self.assertEqual(update.remaining_status, "terminal_unorderable")
        self.assertEqual(
            update.remaining_reason_code.code,
            MatchReasonCode.AVAILABLE_QUANTITY_BELOW_MINIMUM.value,
        )
        self.assertEqual(ledger.available_quantities[self.iid], Decimal("150"))

    def test_self_illegal_odd_request_is_rejected_with_order_quantity_code(
        self,
    ) -> None:
        # The request does not exceed availability, so its own quantity
        # governs: a 250-share request under a 100 lot is an illegal order
        # and is REJECTED with the ORDER_* code -- never expired with an
        # AVAILABLE_* code and never traded as a floored 200.
        ledger = self.make_ledger(cash="0", available="250")
        order = self.make_order(OrderSide.SELL, "250", 1)

        result = self.run_match([order], ledger=ledger)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(ledger.planned_fills, [])
        self.assertEqual(update.remaining_status, "not_executed")
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT.value,
        )
        self.assertEqual(
            update.remaining_reason_code.code,
            MatchReasonCode.ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT.value,
        )

    def test_zero_candidate_is_reported_without_consulting_slippage(self) -> None:
        # A slippage model that always fails must never be consulted when
        # nothing is sellable: the availability reason wins.
        from app.backtesting.reason_codes import ResultStage

        class ExplodingSlippageModel:
            def apply(self, *args, **kwargs):
                raise AssertionError("slippage must not be reached")

        model = BarOpenMatchingModel(
            slippage_model=ExplodingSlippageModel(),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=zero_fee_schedule()
            ),
        )
        ledger = self.make_ledger(cash="0", available="0")
        order = self.make_order(OrderSide.SELL, "100", 1)

        result = model.match(
            orders=[order],
            market_states={self.iid: self.make_state()},
            ledger=ledger,
            facts={self.iid: self.make_facts()},
            match_at=OPEN_TS,
            position_quantities={self.iid: Decimal("100")},
        )

        update = self.update_for(result, order)
        self.assertIs(update.order_status, OrderStatus.EXPIRED)
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.INSUFFICIENT_AVAILABLE_QUANTITY.value,
        )

    def test_illegal_quantity_outranks_a_slippage_failure(self) -> None:
        # Quantity legality is evaluated before any pricing work: an
        # illegal buy is rejected with its own rule code even though the
        # slippage model would also have failed.
        class ExplodingSlippageModel:
            def apply(self, *args, **kwargs):
                raise AssertionError("slippage must not be reached")

        model = BarOpenMatchingModel(
            slippage_model=ExplodingSlippageModel(),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=zero_fee_schedule()
            ),
        )
        ledger = self.make_ledger(cash="100000")
        order = self.make_order(OrderSide.BUY, "250", 1)

        result = model.match(
            orders=[order],
            market_states={self.iid: self.make_state()},
            ledger=ledger,
            facts={self.iid: self.make_facts()},
            match_at=OPEN_TS,
        )

        update = self.update_for(result, order)
        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT.value,
        )

    def test_shortfall_path_derives_candidate_instead_of_rejecting_odd_request(
        self,
    ) -> None:
        # 250 requested against 150 sellable of a 300 holding: the
        # shortfall path derives the legal 100-share candidate and fills
        # it partially -- the odd request is NOT rejected outright.
        ledger = self.make_ledger(cash="0", available="150", total="300")
        order = self.make_order(OrderSide.SELL, "250", 1)

        result = self.run_match([order], ledger=ledger)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(update.filled_quantity, Decimal("100"))
        self.assertEqual(update.remaining_status, "terminal_unfilled")
        self.assertEqual(
            update.remaining_reason_code.code,
            MatchReasonCode.INSUFFICIENT_AVAILABLE_QUANTITY.value,
        )

    def test_shortfall_path_reports_available_reason_for_below_minimum_request(
        self,
    ) -> None:
        # A below-minimum request on a short availability never reaches
        # the ORDER_QUANTITY_BELOW_MINIMUM rejection: the capped
        # candidate (100 < 200 minimum) expires with the AVAILABLE code.
        facts = {
            self.iid: self.make_facts(minimum_order_quantity="200")
        }
        ledger = self.make_ledger(cash="0", available="100", total="300")
        order = self.make_order(OrderSide.SELL, "150", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.EXPIRED)
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.AVAILABLE_QUANTITY_BELOW_MINIMUM.value,
        )

    def test_zero_availability_outranks_a_missing_market_state(self) -> None:
        # The candidate derivation reads availability before any market
        # state: a zero-candidate sell expires with its availability
        # reason even when no market state exists at all.
        ledger = self.make_ledger(cash="0", available="0")
        order = self.make_order(OrderSide.SELL, "100", 1)

        result = self.run_match([order], ledger=ledger, states={})
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.EXPIRED)
        self.assertEqual(update.remaining_status, "terminal_unfilled")
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.INSUFFICIENT_AVAILABLE_QUANTITY.value,
        )

    def test_availability_shortfall_may_derive_a_smaller_legal_candidate(
        self,
    ) -> None:
        # 300 requested against 150 available: the shortfall path derives
        # the legal 100-share candidate and reports the blocked remainder.
        ledger = self.make_ledger(cash="0", available="150")
        order = self.make_order(OrderSide.SELL, "300", 1)

        result = self.run_match([order], ledger=ledger)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(update.filled_quantity, Decimal("100"))
        self.assertEqual(
            update.remaining_reason_code.code,
            MatchReasonCode.INSUFFICIENT_AVAILABLE_QUANTITY.value,
        )
        self.assertEqual(update.remaining_status, "terminal_unfilled")

    def test_allow_full_liquidation_policy_does_not_bless_partial_odd_sales(
        self,
    ) -> None:
        facts = {
            self.iid: self.make_facts(
                sell_odd_lot_policy=SellOddLotPolicy.ALLOW_FULL_LIQUIDATION_ODD_LOT
            )
        }
        ledger = self.make_ledger(cash="0", available="300")
        order = self.make_order(OrderSide.SELL, "150", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        # 150 of a 300-share holding is not a full liquidation, so the
        # policy grants nothing: the odd-lot request is rejected.
        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(ledger.planned_fills, [])
        self.assertEqual(update.remaining_status, "not_executed")
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ODD_LOT_NOT_ALLOWED.value,
        )

    def test_allow_odd_lot_without_lot_size_exemption_is_rejected(self) -> None:
        facts = {
            self.iid: self.make_facts(
                sell_odd_lot_policy=SellOddLotPolicy.ALLOW_ODD_LOT,
                odd_lot_bypasses_lot_size=False,
            )
        }
        ledger = self.make_ledger(cash="0", available="150")
        order = self.make_order(OrderSide.SELL, "150", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        # Selling the entire 150-share holding needs the explicit lot-size
        # exemption; the policy alone does not waive it.
        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(ledger.planned_fills, [])
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ODD_LOT_LOT_SIZE_EXEMPTION_MISSING.value,
        )

    def test_allow_odd_lot_with_declared_bypass_trades_the_odd_lot(self) -> None:
        facts = {
            self.iid: self.make_facts(
                sell_odd_lot_policy=SellOddLotPolicy.ALLOW_ODD_LOT,
                odd_lot_bypasses_lot_size=True,
            )
        }
        ledger = self.make_ledger(cash="0", available="150")
        order = self.make_order(OrderSide.SELL, "150", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.FILLED)
        self.assertEqual(update.filled_quantity, Decimal("150"))
        self.assertIsNone(update.reason_code)
        self.assertIsNone(update.remaining_reason_code)


class NegativeNetProceedsTests(MatchingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=huge_fixed_fee_schedule()
            ),
        )

    def test_negative_net_proceeds_never_trade_or_move_state(self) -> None:
        ledger = self.make_ledger(cash="500", available="100")
        order = self.make_order(OrderSide.SELL, "100", 1)

        result = self.run_match([order], ledger=ledger)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(
            update.reason_code.code, MatchReasonCode.NEGATIVE_NET_PROCEEDS.value
        )
        # No deduction, no fee, no cash movement.
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(ledger.available_quantities[self.iid], Decimal("100"))
        self.assertEqual(ledger.available_cash, Decimal("500"))
        self.assertEqual(ledger.planned_fills, [])
        self.assertEqual(len(result.skipped_or_rejected_orders), 1)

    def test_no_extra_partial_fill_attempt_hunts_for_a_viable_quantity(
        self,
    ) -> None:
        ledger = self.make_ledger(cash="0", available="100")
        order = self.make_order(OrderSide.SELL, "100", 1)

        result = self.run_match([order], ledger=ledger)

        self.assertEqual(len(result.order_updates), 1)
        self.assertEqual(result.fills, ())
        update = result.order_updates[0]
        self.assertEqual(update.remaining_status, "not_executed")
        self.assertEqual(
            update.remaining_reason_code.code,
            MatchReasonCode.NEGATIVE_NET_PROCEEDS.value,
        )

    def test_shortfall_fact_is_kept_when_net_proceeds_dominate(self) -> None:
        ledger = self.make_ledger(cash="0", available="150")
        order = self.make_order(OrderSide.SELL, "300", 1)

        result = self.run_match([order], ledger=ledger)
        update = self.update_for(result, order)

        # Candidate 150 floors to 100; its net proceeds are still negative,
        # so NEGATIVE_NET_PROCEEDS stays the main reason while the
        # availability shortfall is preserved as a detail fact.
        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(
            update.reason_code.code, MatchReasonCode.NEGATIVE_NET_PROCEEDS.value
        )
        self.assertEqual(ledger.planned_fills, [])


class BuyAllocationTests(MatchingFixture):
    def test_single_pro_rata_pass_scales_and_lands_on_the_lot_grid(self) -> None:
        ledger = self.make_ledger(cash="6000")
        first = self.make_order(OrderSide.BUY, "800", 1)
        second = self.make_order(OrderSide.BUY, "400", 2)

        result = self.run_match([first, second], ledger=ledger)

        by_intent = {r.intent_id: r for r in result.buy_allocation_results}
        first_alloc = by_intent[first.intent_id]
        second_alloc = by_intent[second.intent_id]
        self.assertEqual(first_alloc.allocated_quantity, Decimal("400"))
        self.assertEqual(second_alloc.allocated_quantity, Decimal("200"))
        self.assertEqual(first_alloc.allocation_phase, ALLOCATION_PHASE_PRO_RATA)
        # Exactly one scaling pass: demand landed within cash.
        self.assertEqual(ledger.available_cash, Decimal("0"))
        self.assertIsNone(second_alloc.allocation_reason_code)

    def test_pro_rata_zero_buy_creates_no_order_and_gets_no_redistribution(
        self,
    ) -> None:
        ledger = self.make_ledger(cash="4600")
        big = self.make_order(OrderSide.BUY, "900", 1)
        tiny = self.make_order(OrderSide.BUY, "100", 2)

        result = self.run_match([big, tiny], ledger=ledger)

        by_intent = {r.intent_id: r for r in result.buy_allocation_results}
        big_alloc = by_intent[big.intent_id]
        tiny_alloc = by_intent[tiny.intent_id]
        self.assertEqual(big_alloc.allocated_quantity, Decimal("400"))
        self.assertIs(tiny_alloc.allocation_status, ALLOCATION_STATUS_NOT_SUBMITTED)
        self.assertEqual(tiny_alloc.allocated_quantity, Decimal("0"))
        self.assertEqual(tiny_alloc.unsubmitted_quantity, Decimal("100"))
        self.assertFalse(tiny_alloc.reallocated)
        self.assertEqual(
            tiny_alloc.allocation_reason_code.code,
            MatchReasonCode.CASH_ALLOCATION_PRO_RATA_ROUNDED_TO_ZERO.value,
        )
        # No executable order exists for the tiny buy: no order update and
        # no skipped record — only the not_submitted allocation result.
        with self.assertRaises(StopIteration):
            self.update_for(result, tiny)
        self.assertIs(big.status, OrderStatus.PARTIALLY_FILLED)
        self.assertFalse(
            [
                record
                for record in result.skipped_or_rejected_orders
                if record.order_id == tiny.order_id
            ]
        )
        # The match explicitly hands back the candidate's id so the caller
        # atomically removes it from the run's order set: a later batch can
        # never re-match a submitted leftover.
        self.assertEqual(result.unsubmitted_order_ids, (tiny.order_id,))
        self.assertEqual(ledger.available_cash, Decimal("600"))

    def test_lot_reduction_never_leaves_a_below_minimum_buy(self) -> None:
        model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=min_fee_schedule()
            ),
        )
        facts = {
            self.iid: self.make_facts(
                lot_size="100", minimum_order_quantity="150"
            )
        }
        first = self.make_order(OrderSide.BUY, "300", 1)
        second = self.make_order(OrderSide.BUY, "300", 2)

        result = model.match(
            orders=[first, second],
            market_states={self.iid: self.make_state(open_price="1")},
            ledger=self.make_ledger(cash="407"),
            facts=facts,
            match_at=OPEN_TS,
        )

        allocations = {
            record.intent_id: record for record in result.buy_allocation_results
        }
        self.assertEqual(allocations[first.intent_id].allocated_quantity, Decimal("0"))
        self.assertEqual(allocations[second.intent_id].allocated_quantity, Decimal("200"))
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].quantity, Decimal("200"))
        self.assertGreaterEqual(
            result.fills[0].quantity, facts[self.iid].minimum_order_quantity
        )

    def test_lot_reduction_recomputes_the_fee_quote_at_each_step(self) -> None:
        model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=min_fee_schedule()
            ),
        )
        facts = {
            self.iid: self.make_facts(lot_size="50", minimum_order_quantity="50")
        }
        ledger = self.make_ledger(cash="1009")
        first = self.make_order(OrderSide.BUY, "200", 1)
        second = self.make_order(OrderSide.BUY, "200", 2)

        result = model.match(
            orders=[first, second],
            market_states={self.iid: self.make_state()},
            ledger=ledger,
            facts=facts,
            match_at=OPEN_TS,
        )

        by_intent = {r.intent_id: r for r in result.buy_allocation_results}
        first_alloc = by_intent[first.intent_id]
        second_alloc = by_intent[second.intent_id]
        # Both land on 50 shares after pro-rata; together they cost
        # 2 x (500 + 5) = 1,010 > 1,009, so stable-order lot reduction
        # zeroes the first buy before the second can afford its share.
        self.assertEqual(first_alloc.allocated_quantity, Decimal("0"))
        self.assertEqual(
            first_alloc.allocation_reason_code.code,
            MatchReasonCode.CASH_ALLOCATION_LOT_REDUCTION_TO_ZERO.value,
        )
        self.assertEqual(second_alloc.allocated_quantity, Decimal("50"))
        fill = result.fills[0]
        # The fee was quoted for the executed 50-share quantity
        # (minimum 5 dominates the 0.3% rate), not for any earlier size.
        self.assertEqual(fill.quantity, Decimal("50"))
        self.assertEqual(fill.fees, Decimal("5"))

    def test_two_zero_paths_report_different_reason_codes(self) -> None:
        pro_result = self.run_match(
            [
                self.make_order(OrderSide.BUY, "900", 1),
                self.make_order(OrderSide.BUY, "100", 2),
            ],
            ledger=self.make_ledger(cash="4600"),
        )
        lot_model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=min_fee_schedule()
            ),
        )
        lot_facts = {
            self.iid: self.make_facts(lot_size="50", minimum_order_quantity="50")
        }
        # Three buys of 200: after pro-rata each holds 50 shares costing
        # 505; the total overshoots cash by one unit, so stable-order lot
        # reduction zeroes the first buy.
        lot_result = lot_model.match(
            orders=[
                self.make_order(OrderSide.BUY, "200", 1),
                self.make_order(OrderSide.BUY, "200", 2),
                self.make_order(OrderSide.BUY, "200", 3),
            ],
            market_states={self.iid: self.make_state()},
            ledger=self.make_ledger(cash="1514"),
            facts=lot_facts,
            match_at=OPEN_TS,
        )

        pro_codes = {
            r.allocation_reason_code.code
            for r in pro_result.buy_allocation_results
            if r.allocation_reason_code is not None
        }
        lot_codes = {
            r.allocation_reason_code.code
            for r in lot_result.buy_allocation_results
            if r.allocation_reason_code is not None
        }
        self.assertIn(
            MatchReasonCode.CASH_ALLOCATION_PRO_RATA_ROUNDED_TO_ZERO.value,
            pro_codes,
        )
        self.assertIn(
            MatchReasonCode.CASH_ALLOCATION_LOT_REDUCTION_TO_ZERO.value,
            lot_codes,
        )
        self.assertNotEqual(pro_codes, lot_codes)


class FundingPriceTests(MatchingFixture):
    def _slipped_model(self):
        return BarOpenMatchingModel(
            slippage_model=BpsSlippageModel(slippage_bps="500", price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=zero_fee_schedule()
            ),
        )

    def test_buy_funding_check_uses_the_slipped_execution_price(self) -> None:
        ledger = self.make_ledger(cash="10400")
        order = self.make_order(OrderSide.BUY, "100", 1)

        # Reference 100 would cost 10,000 and fit; the slipped execution
        # price 105 costs 10,500 and does not, so nothing may trade.
        result = self._slipped_model().match(
            orders=[order],
            market_states={self.iid: self.make_state(open_price="100")},
            ledger=ledger,
            facts={self.iid: self.make_facts()},
            match_at=OPEN_TS,
        )

        alloc = result.buy_allocation_results[0]
        self.assertEqual(alloc.allocated_quantity, Decimal("0"))
        self.assertEqual(ledger.available_cash, Decimal("10400"))
        self.assertEqual(result.fills, ())

    def test_fill_records_the_execution_price_not_the_reference(self) -> None:
        ledger = self.make_ledger(cash="10700")
        order = self.make_order(OrderSide.BUY, "100", 1)

        result = self._slipped_model().match(
            orders=[order],
            market_states={self.iid: self.make_state(open_price="100")},
            ledger=ledger,
            facts={self.iid: self.make_facts()},
            match_at=OPEN_TS,
        )

        fill = result.fills[0]
        self.assertEqual(fill.price, Decimal("105"))
        self.assertEqual(fill.reference_price, Decimal("100"))
        self.assertEqual(ledger.available_cash, Decimal("200"))


class PartialFillRecordTests(MatchingFixture):
    def test_partial_results_record_remaining_status_and_reason(self) -> None:
        ledger = self.make_ledger(cash="6000")
        first = self.make_order(OrderSide.BUY, "800", 1)
        second = self.make_order(OrderSide.BUY, "400", 2)

        result = self.run_match([first, second], ledger=ledger)

        first_update = self.update_for(result, first)
        second_update = self.update_for(result, second)
        for update in (first_update, second_update):
            # The order-level reason explains the cash shortfall that
            # truncated the fill.
            self.assertIsNotNone(update.reason_code)
            self.assertEqual(
                update.reason_code.code,
                MatchReasonCode.INSUFFICIENT_CASH.value,
            )
            self.assertIsNotNone(update.remaining_reason_code.code)
            self.assertEqual(update.remaining_status, "terminal_unfilled")
            # The unfilled remainder expires after the one-shot match
            # under its own code instead of rolling to a later session.
            self.assertEqual(
                update.remaining_reason_code.code,
                MatchReasonCode.EXPIRED_AFTER_PARTIAL_FILL.value,
            )
            self.assertGreater(update.filled_quantity, Decimal("0"))
            self.assertLess(update.filled_quantity, update.requested_quantity)

    def test_filled_results_carry_empty_reason_codes(self) -> None:
        ledger = self.make_ledger(cash="10000")
        order = self.make_order(OrderSide.BUY, "100", 1)

        result = self.run_match([order], ledger=ledger)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.FILLED)
        self.assertIsNone(update.reason_code)
        self.assertIsNone(update.remaining_reason_code)
        self.assertEqual(update.remaining_status, "none")


class BuyQuantityRejectionTests(MatchingFixture):
    def test_illegal_buy_quantities_are_rejected_with_precise_codes(self) -> None:
        cases = [
            ("100.5", MatchReasonCode.ORDER_QUANTITY_PRECISION_INVALID.value),
            ("250", MatchReasonCode.ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT.value),
        ]
        for quantity, expected_code in cases:
            with self.subTest(quantity=quantity):
                ledger = self.make_ledger(cash="100000")
                order = self.make_order(OrderSide.BUY, quantity, 1)
                result = self.run_match([order], ledger=ledger)
                update = self.update_for(result, order)
                self.assertIs(update.order_status, OrderStatus.REJECTED)
                self.assertEqual(update.reason_code.code, expected_code)
                self.assertEqual(update.remaining_status, "not_executed")

    def test_buy_below_minimum_is_rejected(self) -> None:
        facts = {self.iid: self.make_facts(minimum_order_quantity="200")}
        ledger = self.make_ledger(cash="100000")
        order = self.make_order(OrderSide.BUY, "100", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        self.assertEqual(
            update.reason_code.code, MatchReasonCode.ORDER_QUANTITY_BELOW_MINIMUM.value
        )

    def test_market_gates_reject_before_pricing(self) -> None:
        cases = [
            (
                {"is_suspended": True},
                MatchReasonCode.INSTRUMENT_SUSPENDED.value,
            ),
            (
                {"open_available": False, "open_price": None},
                MatchReasonCode.OPEN_UNAVAILABLE.value,
            ),
            (
                {"buy_allowed": False},
                MatchReasonCode.BUY_UNAVAILABLE_AT_PRICE_LIMIT.value,
            ),
        ]
        for overrides, expected_code in cases:
            with self.subTest(code=expected_code):
                ledger = self.make_ledger(cash="10000")
                order = self.make_order(OrderSide.BUY, "100", 1)
                result = self.run_match(
                    [order],
                    ledger=ledger,
                    states={self.iid: self.make_state(**overrides)},
                )
                update = self.update_for(result, order)
                self.assertIs(update.order_status, OrderStatus.REJECTED)
                self.assertEqual(update.reason_code.code, expected_code)

    def test_expired_validity_window_expires_legally(self) -> None:
        ledger = self.make_ledger(cash="0", available="100")
        order = self.make_order(
            OrderSide.SELL,
            "100",
            1,
            valid_until=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )

        result = self.run_match([order], ledger=ledger)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.EXPIRED)
        self.assertEqual(update.reason_code.code, MatchReasonCode.ORDER_EXPIRED.value)


class AllocationResultScopeTests(MatchingFixture):
    def test_preflight_rejected_buy_produces_no_allocation_result(self) -> None:
        # An illegally-sized buy never entered the cash-allocation stage:
        # it appears only as a rejected order update, and no
        # "not_submitted" allocation record dilutes the frozen meaning of
        # a budget scaled to zero.
        ledger = self.make_ledger(cash="100000")
        illegal = self.make_order(OrderSide.BUY, "250", 1)
        legal = self.make_order(OrderSide.BUY, "100", 2)

        result = self.run_match([illegal, legal], ledger=ledger)

        alloc_intents = {
            r.intent_id for r in result.buy_allocation_results
        }
        self.assertNotIn(illegal.intent_id, alloc_intents)
        self.assertIn(legal.intent_id, alloc_intents)
        update = self.update_for(result, illegal)
        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT.value,
        )


class FinalStateContractTests(MatchingFixture):
    def test_submitted_never_survives_a_completed_match(self) -> None:
        orders = [
            self.make_order(OrderSide.SELL, "100", 1),
            self.make_order(OrderSide.BUY, "900", 2),
            self.make_order(OrderSide.BUY, "100", 3),  # pro-rata to zero
        ]
        result = self.run_match(
            orders, ledger=self.make_ledger(cash="4600", available="100")
        )

        allowed = {
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }
        updated_ids = {u.order_id for u in result.order_updates}
        for update in result.order_updates:
            self.assertIn(update.order_status, allowed)
        for record in result.skipped_or_rejected_orders:
            self.assertIn(record.order_status, allowed)
        for order in orders:
            if order.order_id in updated_ids:
                # Every reported order ends in a terminal state.
                self.assertIn(order.status, allowed)
            else:
                # A zero-allocation buy never became an order: it is not
                # reported and its runtime object stays untouched.
                self.assertIs(order.status, OrderStatus.SUBMITTED)
        zero_allocs = [
            r
            for r in result.buy_allocation_results
            if r.allocation_status == ALLOCATION_STATUS_NOT_SUBMITTED
        ]
        self.assertEqual(len(zero_allocs), 1)


class StructuredReasonContractTests(MatchingFixture):
    def test_negative_net_proceeds_keeps_stage_and_audit_details(self) -> None:
        from app.backtesting.reason_codes import ResultStage

        model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=huge_fixed_fee_schedule()
            ),
        )
        ledger = self.make_ledger(cash="500", available="100")
        order = self.make_order(OrderSide.SELL, "100", 1)

        result = model.match(
            orders=[order],
            market_states={self.iid: self.make_state()},
            ledger=ledger,
            facts={self.iid: self.make_facts()},
            match_at=OPEN_TS,
        )

        reason = result.order_updates[0].reason_code
        self.assertEqual(reason.stage, ResultStage.MATCHING)
        self.assertEqual(
            reason.code, MatchReasonCode.NEGATIVE_NET_PROCEEDS.value
        )
        # The audit facts survive into the structured details.
        self.assertEqual(reason.details["candidate_quantity"], "100")
        self.assertEqual(reason.details["available_shortfall"], False)
        self.assertEqual(reason.details["net_proceeds"], "-9000")

    def test_slippage_failure_becomes_a_structured_rejection(self) -> None:
        from app.backtesting.reason_codes import ResultStage, SlippageReasonCode

        model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel(
                slippage_bps="20000", price_tick="0.01"
            ),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=zero_fee_schedule()
            ),
        )
        ledger = self.make_ledger(cash="0", available="100")
        order = self.make_order(OrderSide.SELL, "100", 1)

        result = model.match(
            orders=[order],
            market_states={self.iid: self.make_state(open_price="10")},
            ledger=ledger,
            facts={self.iid: self.make_facts()},
            match_at=OPEN_TS,
        )

        update = self.update_for(result, order)
        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(update.reason_code.stage, ResultStage.SLIPPAGE)
        self.assertEqual(
            update.reason_code.code, SlippageReasonCode.NON_POSITIVE_EXECUTION_PRICE.value
        )
        # No fill and no state movement happened.
        self.assertEqual(result.fills, ())
        self.assertEqual(ledger.available_quantities[self.iid], Decimal("100"))

    def test_self_illegal_precision_is_rejected_with_the_order_code(self) -> None:
        facts = {
            self.iid: self.make_facts(
                lot_size="1",
                minimum_order_quantity="1",
                holding_precision=1,
                order_precision=0,
            )
        }
        ledger = self.make_ledger(cash="0", available="1.5")
        order = self.make_order(OrderSide.SELL, "1.5", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        # The request itself violates the order precision; it is a
        # rejected illegal order, not an availability problem.
        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(update.remaining_status, "not_executed")
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ORDER_QUANTITY_PRECISION_INVALID.value,
        )

    def test_lot_exemption_does_not_skip_the_minimum_check(self) -> None:
        # A declared odd-lot lot-size exemption waives only the lot rule:
        # a 50-share full-liquidation sell below the 100 minimum is still
        # rejected for the minimum, never filled.
        facts = {
            self.iid: self.make_facts(
                minimum_order_quantity="100",
                sell_odd_lot_policy=SellOddLotPolicy.ALLOW_ODD_LOT,
                odd_lot_bypasses_lot_size=True,
            )
        }
        ledger = self.make_ledger(cash="0", available="50")
        order = self.make_order(OrderSide.SELL, "50", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(ledger.planned_fills, [])
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ORDER_QUANTITY_BELOW_MINIMUM.value,
        )

    def test_liquidation_precision_exemption_does_not_skip_the_lot_rule(
        self,
    ) -> None:
        # The full-liquidation order-precision bypass waives only
        # precision: a fractional 100.5-share liquidation is still
        # rejected because it is not a multiple of the 100 lot.
        facts = {
            self.iid: self.make_facts(
                holding_precision=1,
                order_precision=0,
                sell_odd_lot_policy=SellOddLotPolicy.ALLOW_FULL_LIQUIDATION_ODD_LOT,
                full_liquidation_bypasses_order_precision=True,
            )
        }
        ledger = self.make_ledger(cash="0", available="100.5")
        order = self.make_order(OrderSide.SELL, "100.5", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.REJECTED)
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(ledger.planned_fills, [])
        # The policy permits full-liquidation odd sales in principle but
        # the explicit lot-size exemption flag was not declared, so the
        # precise lot rejection is the exemption-missing code.
        self.assertEqual(
            update.reason_code.code,
            MatchReasonCode.ODD_LOT_LOT_SIZE_EXEMPTION_MISSING.value,
        )

    def test_negative_total_position_is_rejected_as_invalid_input(self) -> None:
        from app.backtesting.domain import DomainValidationError

        ledger = self.make_ledger(cash="0", available="100")
        order = self.make_order(OrderSide.SELL, "100", 1)

        with self.assertRaises(DomainValidationError):
            self.run_match(
                [order],
                ledger=ledger,
                position_quantities={self.iid: Decimal("-5")},
            )

    def test_capped_availability_precision_is_reported_before_lot_flooring(
        self,
    ) -> None:
        facts = {
            self.iid: self.make_facts(
                lot_size="1",
                minimum_order_quantity="1",
                holding_precision=1,
                order_precision=0,
            )
        }
        # Legal request of the whole 3-share holding, but only a
        # precision-corrupt 1.5 is sellable today.
        ledger = self.make_ledger(cash="0", available="1.5", total="3")
        order = self.make_order(OrderSide.SELL, "3", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        # 1.5 is floorable onto the lot-1 grid, but the precision-invalid
        # availability must be identified first, not silently rounded.
        self.assertIs(update.order_status, OrderStatus.EXPIRED)
        self.assertEqual(update.filled_quantity, Decimal("0"))
        self.assertEqual(update.remaining_status, "terminal_unorderable")
        self.assertEqual(
            update.remaining_reason_code.code,
            MatchReasonCode.AVAILABLE_QUANTITY_PRECISION_INVALID.value,
        )

    def test_full_liquidation_exemption_is_judged_against_total_holdings(
        self,
    ) -> None:
        facts = {
            self.iid: self.make_facts(
                sell_odd_lot_policy=SellOddLotPolicy.ALLOW_FULL_LIQUIDATION_ODD_LOT,
                full_liquidation_bypasses_lot_size=True,
            )
        }
        # Selling all 250 sellable shares out of a 300-share holding is
        # NOT a full liquidation: the odd 250 must not trade whole via the
        # exemption; the capped path derives the legal 100-lot portion.
        ledger = self.make_ledger(cash="0", available="250", total="300")
        order = self.make_order(OrderSide.SELL, "300", 1)

        result = self.run_match([order], ledger=ledger, facts=facts)
        update = self.update_for(result, order)

        self.assertIs(update.order_status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(update.filled_quantity, Decimal("200"))
        self.assertEqual(update.remaining_quantity, Decimal("100"))
        self.assertEqual(update.remaining_status, "terminal_unfilled")
        self.assertEqual(
            update.remaining_reason_code.code,
            MatchReasonCode.INSUFFICIENT_AVAILABLE_QUANTITY.value,
        )


class FeeQuoteTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.strategy_protocol.interpretation import InstrumentExecutionFacts

        self.iid = uuid4()
        self.provider = StatelessFeeQuoteProvider(
            fee_schedule=min_fee_schedule()
        )
        self.context = InstrumentExecutionFacts(
            instrument_id=self.iid,
            holding_precision=0,
            order_precision=0,
            lot_size="100",
            minimum_order_quantity="100",
            sell_odd_lot_policy=SellOddLotPolicy.STRICT_LOT,
            contract_multiplier="1",
            fee_categories=frozenset({"commission"}),
        )

    def test_identical_inputs_return_identical_quotes(self) -> None:
        kwargs = dict(
            side=OrderSide.BUY,
            quantity=Decimal("500"),
            execution_price=Decimal("10"),
            contract_multiplier=Decimal("1"),
            currency="CNY",
            instrument_context=self.context,
            order_context={},
        )
        self.assertEqual(self.provider.quote(**kwargs), self.provider.quote(**kwargs))

    def test_quotes_support_full_scaled_and_executed_quantities(self) -> None:
        common = dict(
            side=OrderSide.BUY,
            execution_price=Decimal("10"),
            contract_multiplier=Decimal("1"),
            currency="CNY",
            instrument_context=self.context,
            order_context={},
        )
        small = self.provider.quote(quantity=Decimal("100"), **common)
        large = self.provider.quote(quantity=Decimal("10000"), **common)

        # Non-linear minimum commission: 10,000 notional pays the 5
        # minimum while 100,000 notional pays 30 by rate.
        self.assertEqual(small.total, Decimal("5"))
        self.assertEqual(large.total, Decimal("30"))

    def test_unresolvable_category_fails_closed(self) -> None:
        from app.backtesting.fees import FeeRuleUnresolvedError

        broken = InstrumentExecutionFacts(
            instrument_id=self.iid,
            holding_precision=0,
            order_precision=0,
            lot_size="100",
            minimum_order_quantity="100",
            sell_odd_lot_policy=SellOddLotPolicy.STRICT_LOT,
            contract_multiplier="1",
            fee_categories=frozenset({"stamp_tax"}),
        )
        with self.assertRaises(FeeRuleUnresolvedError):
            self.provider.quote(
                side=OrderSide.BUY,
                quantity=Decimal("100"),
                execution_price=Decimal("10"),
                contract_multiplier=Decimal("1"),
                currency="CNY",
                instrument_context=broken,
                order_context={},
            )


class SubmissionSequenceTests(unittest.TestCase):
    def test_sequences_start_at_one_and_increase_monotonically(self) -> None:
        allocator = SubmissionSequenceAllocator(run_id="run-1")

        self.assertEqual(
            [allocator.next_sequence() for _ in range(3)], [1, 2, 3]
        )
        self.assertEqual(allocator.last_sequence, 3)

    def test_shard_resume_continues_across_chunks_without_reset(self) -> None:
        first = SubmissionSequenceAllocator(run_id="run-1")
        first.next_sequence()
        first.next_sequence()

        second = SubmissionSequenceAllocator(
            run_id="run-1", resume_after=first.last_sequence
        )

        self.assertEqual(second.next_sequence(), 3)

    def test_retry_keys_reuse_the_original_sequence(self) -> None:
        allocator = SubmissionSequenceAllocator(run_id="run-1")

        original = allocator.sequence_for("intent-a")
        self.assertEqual(allocator.sequence_for("intent-a"), original)
        self.assertNotEqual(allocator.sequence_for("intent-b"), original)
        self.assertEqual(allocator.last_sequence, 2)

    def test_retry_mapping_survives_a_process_restart(self) -> None:
        first = SubmissionSequenceAllocator(run_id="run-1")
        first.sequence_for("intent-a")
        first.sequence_for("intent-b")

        # A restarted process restores the durable retry-key mapping; the
        # same key then keeps its original ordinal instead of receiving a
        # second one.
        second = SubmissionSequenceAllocator(
            run_id="run-1",
            resume_after=first.last_sequence,
            restored_sequences=first.sequences_snapshot(),
        )

        self.assertEqual(second.sequence_for("intent-a"), 1)
        self.assertEqual(second.sequence_for("intent-b"), 2)
        self.assertEqual(second.last_sequence, 2)
        self.assertEqual(second.next_sequence(), 3)

    def test_restored_mappings_are_validated(self) -> None:
        from app.backtesting.execution import ExecutionError

        with self.assertRaises(ExecutionError):
            SubmissionSequenceAllocator(
                run_id="run-1",
                resume_after=1,
                restored_sequences={"a": 5},
            )
        with self.assertRaises(ExecutionError):
            SubmissionSequenceAllocator(
                run_id="run-1",
                resume_after=2,
                restored_sequences={"a": 1, "b": 1},
            )
        with self.assertRaises(ExecutionError):
            SubmissionSequenceAllocator(
                run_id="run-1", restored_sequences={"": 1}
            )

    def test_invalid_construction_is_rejected(self) -> None:
        from app.backtesting.execution import ExecutionError

        with self.assertRaises(ExecutionError):
            SubmissionSequenceAllocator(run_id=" ")
        with self.assertRaises(ExecutionError):
            SubmissionSequenceAllocator(run_id="run-1", resume_after=-1)

    def test_duplicate_order_ids_fail_the_batch(self) -> None:
        from app.backtesting.bar_matching import BarOpenMatchingError

        iid = uuid4()
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
        model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=zero_fee_schedule()
            ),
        )
        ledger = MatchLedger(
            currency="CNY",
            cash_balance_snapshot=Decimal("100000"),
            available_cash=Decimal("100000"),
        )
        shared_id = uuid4()
        orders = [
            Order(
                order_id=shared_id,
                intent_id=uuid4(),
                instrument_id=iid,
                side=OrderSide.BUY,
                quantity="100",
                submitted_at=OPEN_TS,
                submission_sequence=sequence,
            )
            for sequence in (1, 2)
        ]
        # The same order id twice would fill twice while the result keeps
        # one record; the batch must fail instead.
        with self.assertRaises(BarOpenMatchingError):
            model.match(
                orders=orders,
                market_states={iid: MarketState(
                    instrument_id=iid,
                    timestamp=OPEN_TS,
                    open_price="10",
                    price_tick="0.01",
                )},
                ledger=ledger,
                facts={iid: facts},
                match_at=OPEN_TS,
            )
        self.assertEqual(ledger.planned_fills, [])
        self.assertEqual(ledger.available_cash, Decimal("100000"))

    def test_duplicate_sequences_fail_the_batch(self) -> None:
        from app.backtesting.bar_matching import BarOpenMatchingError

        iid = uuid4()
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
        model = BarOpenMatchingModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_quote_provider=StatelessFeeQuoteProvider(
                fee_schedule=zero_fee_schedule()
            ),
        )
        ledger = MatchLedger(
            currency="CNY",
            cash_balance_snapshot=Decimal("0"),
            available_cash=Decimal("0"),
            available_quantities={iid: Decimal("100")},
        )
        orders = [
            Order(
                order_id=uuid4(),
                intent_id=uuid4(),
                instrument_id=iid,
                side=OrderSide.SELL,
                quantity="100",
                submitted_at=OPEN_TS,
                submission_sequence=1,
            ),
            Order(
                order_id=uuid4(),
                intent_id=uuid4(),
                instrument_id=iid,
                side=OrderSide.SELL,
                quantity="100",
                submitted_at=OPEN_TS,
                submission_sequence=1,
            ),
        ]
        with self.assertRaises(BarOpenMatchingError):
            model.match(
                orders=orders,
                market_states={},
                ledger=ledger,
                facts={iid: facts},
                match_at=OPEN_TS,
            )


if __name__ == "__main__":
    unittest.main()
