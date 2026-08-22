"""Tests for slippage, fee calculation, and opening-bar execution."""

from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.accounting import (
    AccountingPolicy,
    AccountState,
    DeferredSettlementPlan,
    OrderSide,
    PortfolioState,
    SettlementPolicy,
)
from app.backtesting.execution import (
    BarMarketExecutionModel,
    MarketState,
    MatchContext,
    Order,
    OrderStatus,
    PriceLimitStatus,
)
from app.backtesting.fees import (
    FeeCalculator,
    FeeRule,
    FeeRoundingLevel,
    FeeRoundingMode,
    FeeSchedule,
)
from app.backtesting.slippage import BpsSlippageModel


OPEN = datetime(2026, 8, 22, 1, 30, tzinfo=timezone.utc)
INSTRUMENT_ID = uuid4()


def order(*, side: OrderSide, quantity: str = "100") -> Order:
    """Create a one-shot market order for a deterministic fixture."""

    return Order(
        order_id=uuid4(),
        intent_id=uuid4(),
        instrument_id=INSTRUMENT_ID,
        side=side,
        quantity=quantity,
        submitted_at=OPEN,
    )


class SlippageAndFeeTestCase(unittest.TestCase):
    def test_bps_slippage_rounds_in_the_adverse_direction(self) -> None:
        model = BpsSlippageModel(slippage_bps="15", price_tick="0.01")

        buy = model.apply("10.00", OrderSide.BUY)
        sell = model.apply("10.00", OrderSide.SELL)

        self.assertEqual(buy.execution_price, Decimal("10.02"))
        self.assertEqual(sell.execution_price, Decimal("9.98"))
        self.assertEqual(buy.price_delta, Decimal("0.02"))
        self.assertEqual(sell.price_delta, Decimal("-0.02"))

    def test_none_slippage_is_explicit_and_still_rounds_to_tick(self) -> None:
        model = BpsSlippageModel.none(price_tick="0.01")

        result = model.apply("10.001", OrderSide.BUY)

        self.assertEqual(result.model_key, "none")
        self.assertEqual(result.model_version, 1)
        self.assertEqual(result.execution_price, Decimal("10.01"))

    def test_fee_schedule_applies_minimum_and_preserves_rounding_contract(self) -> None:
        schedule = FeeSchedule(
            key="demo",
            version=3,
            fee_rules=(
                FeeRule(
                    key="commission",
                    category="commission",
                    side="buy",
                    rate="0.0003",
                    minimum="5",
                    rounding_level=FeeRoundingLevel.FEE_ITEM,
                    rounding_scope="commission",
                    rounding_mode=FeeRoundingMode.HALF_UP,
                    rounding_precision="0.01",
                ),
            ),
        )

        breakdown = FeeCalculator(schedule).calculate(
            side=OrderSide.BUY,
            notional="1000",
        )

        self.assertEqual(breakdown.total, Decimal("5.00"))
        self.assertEqual(breakdown.schedule_key, "demo")
        self.assertEqual(breakdown.schedule_version, 3)

    def test_order_level_rounding_is_preserved_for_one_fill_order(self) -> None:
        schedule = FeeSchedule(
            key="order_rounding",
            version=1,
            fee_rules=(
                FeeRule(
                    key="commission",
                    category="commission",
                    rate="0.0003",
                    rounding_level=FeeRoundingLevel.ORDER,
                    rounding_scope="order_commission",
                    rounding_mode=FeeRoundingMode.UP,
                    rounding_precision="0.01",
                ),
            ),
        )

        breakdown = FeeCalculator(schedule).calculate(
            side=OrderSide.BUY,
            notional="1000.001",
        )

        self.assertEqual(breakdown.total, Decimal("0.31"))
        self.assertEqual(
            breakdown.components[0].rounding_level,
            FeeRoundingLevel.ORDER,
        )


class BarMarketExecutionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        schedule = FeeSchedule(
            key="execution_fixture",
            version=1,
            fee_rules=(
                FeeRule(
                    key="commission",
                    category="commission",
                    minimum="5",
                    rounding_level=FeeRoundingLevel.FEE_ITEM,
                    rounding_scope="commission",
                    rounding_mode=FeeRoundingMode.HALF_UP,
                    rounding_precision="0.01",
                ),
            ),
        )
        self.model = BarMarketExecutionModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_calculator=FeeCalculator(schedule),
        )

    def test_insufficient_cash_skips_the_whole_buy(self) -> None:
        buy = order(side=OrderSide.BUY, quantity="200")
        context = MatchContext(currency="CNY", available_cash="2004")
        state = MarketState(
            instrument_id=INSTRUMENT_ID,
            timestamp=OPEN,
            open_price="10",
            price_tick="0.01",
        )

        result = self.model.match([buy], {INSTRUMENT_ID: state}, context)

        self.assertEqual(result.fills, ())
        self.assertEqual(result.skipped_orders[0].reason, "insufficient_cash")
        self.assertEqual(buy.status, OrderStatus.EXPIRED)
        self.assertEqual(context.available_cash, Decimal("2004"))

    def test_suspension_and_side_price_limit_skip_without_fill(self) -> None:
        suspended = order(side=OrderSide.BUY)
        limit_blocked = order(side=OrderSide.SELL)
        states = {
            INSTRUMENT_ID: MarketState(
                instrument_id=INSTRUMENT_ID,
                timestamp=OPEN,
                open_price="10",
                price_tick="0.01",
                is_suspended=True,
                price_limit_status=PriceLimitStatus.UP,
            )
        }
        context = MatchContext(
            currency="CNY",
            available_cash="10000",
            available_quantities={INSTRUMENT_ID: Decimal("100")},
        )

        result = self.model.match([suspended, limit_blocked], states, context)

        self.assertEqual(result.fills, ())
        self.assertEqual(
            {item.reason for item in result.skipped_orders},
            {"instrument_suspended"},
        )

    def test_explicit_side_availability_controls_price_limit_matching(self) -> None:
        buy = order(side=OrderSide.BUY)
        state = MarketState(
            instrument_id=INSTRUMENT_ID,
            timestamp=OPEN,
            open_price="10",
            price_tick="0.01",
            price_limit_status=PriceLimitStatus.UP,
            buy_allowed=False,
        )

        result = self.model.match(
            [buy],
            {INSTRUMENT_ID: state},
            MatchContext(currency="CNY", available_cash="10000"),
        )

        self.assertEqual(result.fills, ())
        self.assertEqual(result.skipped_orders[0].reason, "buy_unavailable_at_price_limit")

    def test_sell_orders_are_processed_before_buys(self) -> None:
        sell = order(side=OrderSide.SELL, quantity="100")
        buy = order(side=OrderSide.BUY, quantity="100")
        states = {
            INSTRUMENT_ID: MarketState(
                instrument_id=INSTRUMENT_ID,
                timestamp=OPEN,
                open_price="10",
                price_tick="0.01",
            )
        }
        context = MatchContext(
            currency="CNY",
            available_cash="0",
            available_quantities={INSTRUMENT_ID: Decimal("100")},
        )

        result = self.model.match([buy, sell], states, context)

        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].side, OrderSide.SELL)
        self.assertEqual(buy.status, OrderStatus.EXPIRED)

    def test_t_plus_one_release_happens_before_next_open_match(self) -> None:
        portfolio = PortfolioState(
            account=AccountState(
                cash_balances={"CNY": "2000"},
                available_cash="2000",
                frozen_cash="0",
                margin_used="0",
                margin_available="0",
                equity="2000",
            ),
            as_of=datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
        )
        policy = AccountingPolicy(settlement_policy=SettlementPolicy.T_PLUS_ONE)
        instrument_state = MarketState(
            instrument_id=INSTRUMENT_ID,
            timestamp=OPEN,
            open_price="10",
            price_tick="0.01",
        )
        buy = order(side=OrderSide.BUY, quantity="100")
        buy_result = self.model.match(
            [buy],
            {INSTRUMENT_ID: instrument_state},
            MatchContext.from_portfolio(portfolio),
        )
        policy.apply_fill(
            portfolio,
            buy_result.fills[0],
            settlement_plan=DeferredSettlementPlan(
                calendar_id="SSE",
                trade_session=OPEN.date(),
                settlement_session=date(2026, 8, 24),
            ),
        )
        self.assertEqual(portfolio.positions[INSTRUMENT_ID].available_quantity, Decimal("0"))

        same_day_sell = order(side=OrderSide.SELL, quantity="100")
        same_day_result = self.model.match(
            [same_day_sell],
            {INSTRUMENT_ID: instrument_state},
            MatchContext.from_portfolio(portfolio),
        )
        self.assertEqual(same_day_result.fills, ())
        self.assertEqual(
            same_day_result.skipped_orders[0].reason,
            "insufficient_available_quantity",
        )

        # The runner performs this release at the next session's pre-match
        # boundary, before it builds the next match context.
        policy.settle_pending_before_open_match(
            portfolio, calendar_id="SSE", session_date=date(2026, 8, 24)
        )
        next_open = MarketState(
            instrument_id=INSTRUMENT_ID,
            timestamp=datetime(2026, 8, 23, 1, 30, tzinfo=timezone.utc),
            open_price="10",
            price_tick="0.01",
        )
        next_sell = order(side=OrderSide.SELL, quantity="100")
        next_result = self.model.match(
            [next_sell],
            {INSTRUMENT_ID: next_open},
            MatchContext.from_portfolio(portfolio),
        )
        self.assertEqual(len(next_result.fills), 1)
        policy.apply_fill(portfolio, next_result.fills[0])
        self.assertNotIn(INSTRUMENT_ID, portfolio.positions)


if __name__ == "__main__":
    unittest.main()
