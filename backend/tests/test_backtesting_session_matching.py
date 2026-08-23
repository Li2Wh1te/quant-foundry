"""Task package 04-04: session/order/fee/cash/availability rule tests.

Covers the acceptance criteria of the execution-policy projection,
order validity windows, quantity validation, settlement-boundary
releases, same-batch sell-then-buy cash ordering, contract multipliers,
category-resolved fees, batch atomicity, and replay determinism.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4, UUID

from app.backtesting.domain import PositionSide
from app.backtesting.accounting import (
    AccountingPolicy,
    AccountState,
    DeferredSettlementPlan,
    OrderSide,
    PortfolioState,
    PositionState,
    SettlementPolicy,
)
from app.backtesting.execution import MarketState, Order, OrderStatus
from app.backtesting.execution_policy import (
    ExecutionPolicyError,
    InstrumentExecutionPolicy,
    SessionContext,
    SettlementBoundary,
)
from app.backtesting.fees import (
    FeeBaseMeasure,
    FeeCalculator,
    FeeRoundingLevel,
    FeeRoundingMode,
    FeeRule,
    FeeRuleUnresolvedError,
    FeeSchedule,
    FeeScheduleSnapshot,
    resolve_instrument_fee_rules,
)
from app.backtesting.session_matching import (
    ACCOUNTING_BATCH_ABORTED,
    AccountingBatchAbortedError,
    OpeningMatchService,
)
from app.backtesting.slippage import BpsSlippageModel
from app.instruments.references import VersionedReference
from app.instruments.rule_snapshots import InstrumentRuleSnapshotSegment
from app.instruments.rules.contracts import StrategyRuleDeclaration

OPEN = datetime(2026, 8, 24, 1, 30, tzinfo=timezone.utc)
INSTRUMENT_ID = uuid4()
CALENDAR_ID = "XSHG"


def normalized_values(**overrides):
    """Resolved ``china_listed_etf_rules@1`` values for the fixture."""

    values = {
        "lot_size": Decimal("100"),
        "quantity_precision": 0,
        "price_precision": 2,
        "price_tick": Decimal("0.01"),
        "contract_multiplier": Decimal("1"),
        "trading_session_template": VersionedReference(
            key="xshg_etf_session", version=1
        ),
        "settlement_rule_class": "t1_before_open_match",
        "sellable_rule": StrategyRuleDeclaration(
            statements=("available_quantity_only",)
        ),
        "fee_categories": ("commission",),
        "trading_status_applicability": {
            "suspension": "required",
            "opening_availability": "required",
            "price_limit_tradability": "required",
        },
        "currency": "CNY",
        "order_types": ("market",),
        "minimum_order_quantity": Decimal("100"),
        "price_limit_rule": StrategyRuleDeclaration(statements=("explicit_facts",)),
        "cash_availability_rule": StrategyRuleDeclaration(
            statements=("staged_cash_including_fees",)
        ),
        "position_availability_rule": StrategyRuleDeclaration(
            statements=("deferred_t1_release",)
        ),
    }
    values.update(overrides)
    return values


def snapshot_segment(values=None):
    return InstrumentRuleSnapshotSegment(
        instrument_id=INSTRUMENT_ID,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        normal_fact_reference=VersionedReference(key="etf_rule_fact", version=3),
        exception_fact_reference=None,
        normalized_values=values if values is not None else normalized_values(),
        capability_declarations={},
        provenance={"source": "fixture"},
        resolution_hash="hash-04-04",
    )


def policy(**overrides) -> InstrumentExecutionPolicy:
    return InstrumentExecutionPolicy.from_rule_snapshot(
        snapshot_segment(normalized_values(**overrides)),
        package_reference=VersionedReference(
            key="china_listed_etf_rules", version=1
        ),
    )


def commission_schedule() -> FeeSchedule:
    """``max(gross_notional * 0.0003, 5)`` per the contract example."""

    rule = FeeRule(
        key="commission",
        category="commission",
        rate="0.0003",
        minimum="5",
        rounding_level=FeeRoundingLevel.FEE_ITEM,
        rounding_scope="commission",
        rounding_mode=FeeRoundingMode.HALF_UP,
        rounding_precision="0.01",
    )
    return FeeSchedule(key="acct_fee_v1", version=4, fee_rules=(rule,))


def session(states=None, *, opening=OPEN) -> SessionContext:
    return SessionContext(
        session_id=uuid4(),
        calendar_id=CALENDAR_ID,
        session_date=opening.date(),
        opening_match_at=opening,
        close_at=opening + timedelta(hours=6),
        exchange_open=True,
        market_states=states or {},
    )


def market_state(timestamp=OPEN, **overrides) -> MarketState:
    defaults = {
        "instrument_id": INSTRUMENT_ID,
        "timestamp": timestamp,
        "open_price": "10.00",
        "price_tick": "0.01",
    }
    defaults.update(overrides)
    return MarketState(**defaults)


def portfolio(cash="2000", *, quantities=None) -> PortfolioState:
    positions = {
        instrument_id: PositionState(
            instrument_id=instrument_id,
            side=PositionSide.LONG,
            quantity=quantity,
            available_quantity=available,
            average_price="10",
        )
        for instrument_id, (quantity, available) in (quantities or {}).items()
    }
    return PortfolioState(
        account=AccountState(
            cash_balances={"CNY": cash},
            available_cash=cash,
            frozen_cash="0",
            margin_used="0",
            margin_available="0",
            equity=cash,
        ),
        as_of=datetime(2026, 8, 21, tzinfo=timezone.utc),
        positions=positions,
    )


def deferred_plan_factory(calendar_id: str = CALENDAR_ID):
    def factory(fill) -> DeferredSettlementPlan:
        trade_date = fill.timestamp.date()
        return DeferredSettlementPlan(
            calendar_id=calendar_id,
            trade_session=trade_date,
            settlement_session=trade_date + timedelta(days=1),
        )

    return factory


class MatchingHarness:
    """Shared service/session wiring for matching tests."""

    def __init__(self, fee_schedule=None, **policy_overrides) -> None:
        self.service = OpeningMatchService(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_schedule=fee_schedule or commission_schedule(),
        )
        self.states = {INSTRUMENT_ID: market_state()}
        self.policies = {INSTRUMENT_ID: policy(**policy_overrides)}

    def run(self, orders, *, portfolio_state=None, boundary=None, states=None,
            opening=OPEN):
        context = session(
            states if states is not None else self.states, opening=opening
        )
        accounting = AccountingPolicy(
            settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH
        )
        state = portfolio_state or portfolio("10000")
        result = self.service.run_opening_match(
            session=context,
            orders=orders,
            policies=self.policies,
            portfolio=state,
            accounting=accounting,
            settlement_boundary=boundary,
            settlement_plan_factory=deferred_plan_factory(),
        )
        return result, state, accounting


def sequenced_order(side: OrderSide, sequence: int, quantity="100", **overrides) -> Order:
    defaults = {
        "order_id": uuid4(),
        "intent_id": uuid4(),
        "instrument_id": INSTRUMENT_ID,
        "side": side,
        "quantity": quantity,
        "submitted_at": OPEN,
        "submission_sequence": sequence,
    }
    defaults.update(overrides)
    return Order(**defaults)


class ExecutionPolicyProjectionTestCase(unittest.TestCase):
    def test_policy_projects_all_trading_fields(self) -> None:
        resolved = policy()

        self.assertEqual(resolved.currency, "CNY")
        self.assertEqual(resolved.price_precision, 2)
        self.assertEqual(resolved.quantity_precision, 0)
        self.assertEqual(resolved.price_tick, Decimal("0.01"))
        self.assertEqual(resolved.lot_size, Decimal("100"))
        self.assertEqual(resolved.minimum_order_quantity, Decimal("100"))
        self.assertEqual(resolved.contract_multiplier, Decimal("1"))
        self.assertEqual(resolved.allowed_order_types, frozenset({"market"}))
        self.assertEqual(resolved.fee_categories, frozenset({"commission"}))
        self.assertEqual(resolved.settlement_rule_class, "t1_before_open_match")

    def test_missing_required_field_is_rejected_without_defaults(self) -> None:
        values = normalized_values()
        del values["lot_size"]

        with self.assertRaises(ExecutionPolicyError):
            InstrumentExecutionPolicy.from_rule_snapshot(
                snapshot_segment(values),
                package_reference=VersionedReference(
                    key="china_listed_etf_rules", version=1
                ),
            )

    def test_declared_multiplier_is_preserved(self) -> None:
        self.assertEqual(policy(contract_multiplier=10).contract_multiplier, Decimal("10"))

    def test_session_rejects_state_timestamp_mismatch(self) -> None:
        context = session({INSTRUMENT_ID: market_state()})
        drifted = {
            INSTRUMENT_ID: market_state(
                timestamp=OPEN + timedelta(minutes=1)
            )
        }
        with self.assertRaises(ExecutionPolicyError):
            SessionContext(
                session_id=context.session_id,
                calendar_id=context.calendar_id,
                session_date=context.session_date,
                opening_match_at=context.opening_match_at,
                close_at=context.close_at,
                exchange_open=True,
                market_states=drifted,
            )

    def test_boundary_phase_is_fixed_to_before_open_match(self) -> None:
        boundary = SettlementBoundary(
            boundary_id=uuid4(),
            session_id=uuid4(),
            calendar_id=CALENDAR_ID,
            session_date=date(2026, 8, 24),
        )
        self.assertEqual(boundary.phase.value, "before_open_match")


class OrderValidityAndValidationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = MatchingHarness()

    def _reasons(self, result):
        return {order_id: reason for order_id, reason in result.skipped_orders}

    def test_not_yet_valid_order_stays_submitted(self) -> None:
        order = sequenced_order(
            OrderSide.BUY,
            0,
            valid_from=OPEN + timedelta(minutes=1),
        )
        result, state, _ = self.harness.run([order])

        self.assertEqual(result.fills, ())
        self.assertEqual(order.status, OrderStatus.SUBMITTED)
        self.assertEqual(
            self._reasons(result)[order.order_id], "ORDER_NOT_YET_VALID"
        )

    def test_valid_until_is_half_open(self) -> None:
        at_edge = sequenced_order(
            OrderSide.BUY, 0, valid_until=OPEN
        )
        result, _, _ = self.harness.run([at_edge])
        self.assertEqual(at_edge.status, OrderStatus.EXPIRED)
        self.assertEqual(self._reasons(result)[at_edge.order_id], "ORDER_EXPIRED")

        inside = sequenced_order(
            OrderSide.BUY, 1, valid_from=OPEN, valid_until=datetime(
                2026, 8, 24, 2, 30, tzinfo=timezone.utc
            )
        )
        result2, _, _ = self.harness.run([inside])
        self.assertEqual(len(result2.fills), 1)

    def test_quantity_constraints_reject_with_stable_codes(self) -> None:
        bad_precision = sequenced_order(OrderSide.BUY, 0, quantity="100.5")
        below_min = sequenced_order(OrderSide.BUY, 1, quantity="50")
        not_lot_multiple = sequenced_order(OrderSide.BUY, 2, quantity="150")

        result, _, _ = self.harness.run(
            [bad_precision, below_min, not_lot_multiple]
        )
        reasons = self._reasons(result)

        self.assertEqual(
            reasons[bad_precision.order_id],
            "ORDER_QUANTITY_PRECISION_INVALID",
        )
        self.assertEqual(
            reasons[below_min.order_id],
            "ORDER_QUANTITY_BELOW_MINIMUM",
        )
        self.assertEqual(
            reasons[not_lot_multiple.order_id],
            "ORDER_QUANTITY_NOT_MULTIPLE_OF_LOT",
        )
        for order in (bad_precision, below_min, not_lot_multiple):
            self.assertEqual(order.status, OrderStatus.REJECTED)

    def test_order_type_declaration_check_uses_policy(self) -> None:
        self.assertEqual(
            policy().validate_order_type("market"), None
        )
        self.assertEqual(
            policy().validate_order_type("limit"),
            "ORDER_TYPE_NOT_SUPPORTED",
        )

    def test_market_facts_expire_orders_without_fees(self) -> None:
        # Suspension expires the buy without any fee or cash movement.
        suspended_buy = sequenced_order(OrderSide.BUY, 0)
        result1, state1, _ = self.harness.run(
            [suspended_buy],
            portfolio_state=portfolio("10000"),
            states={INSTRUMENT_ID: market_state(is_suspended=True)},
        )
        self.assertEqual(result1.fills, ())
        self.assertEqual(suspended_buy.status_reason, "INSTRUMENT_SUSPENDED")
        self.assertEqual(state1.account.available_cash, Decimal("10000"))

        # Missing open price expires the order.
        no_open_buy = sequenced_order(OrderSide.BUY, 0)
        result2, _, _ = self.harness.run(
            [no_open_buy],
            states={
                INSTRUMENT_ID: market_state(open_price=None, open_available=False)
            },
        )
        self.assertEqual(result2.fills, ())
        self.assertEqual(no_open_buy.status_reason, "OPEN_UNAVAILABLE")

        # Directional price-limit availability blocks only that side.
        blocked_sell = sequenced_order(OrderSide.SELL, 0)
        result3, _, _ = self.harness.run(
            [blocked_sell],
            portfolio_state=portfolio("10000", quantities={INSTRUMENT_ID: ("200", "200")}),
            states={
                INSTRUMENT_ID: market_state(
                    price_limit_status="down", sell_allowed=False
                )
            },
        )
        self.assertEqual(result3.fills, ())
        self.assertEqual(
            blocked_sell.status_reason, "SELL_UNAVAILABLE_AT_PRICE_LIMIT"
        )


class SettlementAndAvailabilityTestCase(unittest.TestCase):
    """T+1-before-open-match availability semantics (doc sections 9/14)."""

    def test_fresh_buy_cannot_be_sold_in_the_same_session(self) -> None:
        harness = MatchingHarness()
        buy = sequenced_order(OrderSide.BUY, 0)
        same_session_sell = sequenced_order(OrderSide.SELL, 1)
        result, state, accounting = harness.run(
            [buy, same_session_sell], portfolio_state=portfolio("2000")
        )

        self.assertEqual(len(result.fills), 1)
        position = state.positions[INSTRUMENT_ID]
        # cash: 2000 - 1005 (gross + commission floor 5) + 0
        self.assertEqual(state.account.available_cash, Decimal("995"))
        self.assertEqual(position.quantity, Decimal("100"))
        self.assertEqual(position.available_quantity, Decimal("0"))
        self.assertEqual(
            same_session_sell.status_reason,
            "INSUFFICIENT_AVAILABLE_QUANTITY",
        )
        pending = accounting.pending_batches()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].settlement_session, OPEN.date() + timedelta(days=1))

    def test_boundary_release_before_match_enables_next_session_sell(self) -> None:
        harness = MatchingHarness()
        buy = sequenced_order(OrderSide.BUY, 0)
        state = portfolio("2000")
        first_result, state, accounting = harness.run([buy], portfolio_state=state)

        # Next session with a matching boundary supplied.
        next_open = OPEN + timedelta(days=1)
        next_states = {INSTRUMENT_ID: market_state(timestamp=next_open)}
        context = session(next_states, opening=next_open)
        boundary = SettlementBoundary(
            boundary_id=uuid4(),
            session_id=context.session_id,
            calendar_id=context.calendar_id,
            session_date=context.session_date,
        )
        sell = sequenced_order(OrderSide.SELL, 0)
        sell_result = harness.service.run_opening_match(
            session=context,
            orders=[sell],
            policies=harness.policies,
            portfolio=state,
            accounting=accounting,
            settlement_boundary=boundary,
            settlement_plan_factory=deferred_plan_factory(),
        )

        self.assertEqual(len(sell_result.fills), 1)
        self.assertNotIn(INSTRUMENT_ID, state.positions)
        release = accounting.releases()[-1]
        self.assertEqual(release.released_quantities[INSTRUMENT_ID], Decimal("100"))

    def test_reapplying_same_boundary_does_not_double_release(self) -> None:
        harness = MatchingHarness()
        buy = sequenced_order(OrderSide.BUY, 0)
        state = portfolio("2000")
        _, state, accounting = harness.run([buy], portfolio_state=state)

        next_open_date = (OPEN + timedelta(days=1)).date()
        for _ in range(2):
            accounting.settle_pending_before_open_match(
                state,
                calendar_id=CALENDAR_ID,
                session_date=next_open_date,
                boundary_id="boundary-1",
            )
        position = state.positions[INSTRUMENT_ID]
        self.assertEqual(position.available_quantity, Decimal("100"))
        releases = [r for r in accounting.releases() if r.boundary_id == "boundary-1"]
        released_totals = [
            r.released_quantities.get(INSTRUMENT_ID, Decimal("0")) for r in releases
        ]
        self.assertEqual(sum(released_totals), Decimal("100"))

    def test_mismatched_boundary_is_refused_and_releases_nothing(self) -> None:
        from app.backtesting.session_matching import SettlementBoundaryMismatchError

        harness = MatchingHarness()
        buy = sequenced_order(OrderSide.BUY, 0)
        state = portfolio("2000")
        harness.run([buy], portfolio_state=state)
        before_available = state.positions[INSTRUMENT_ID].available_quantity

        wrong_session = SettlementBoundary(
            boundary_id=uuid4(),
            session_id=uuid4(),  # does not match the matched session
            calendar_id=CALENDAR_ID,
            session_date=(OPEN + timedelta(days=1)).date(),
        )
        next_open = OPEN + timedelta(days=1)
        context = session({INSTRUMENT_ID: market_state(timestamp=next_open)}, opening=next_open)
        sell = sequenced_order(OrderSide.SELL, 0)
        with self.assertRaises(SettlementBoundaryMismatchError):
            harness.service.run_opening_match(
                session=context,
                orders=[sell],
                policies=harness.policies,
                portfolio=state,
                accounting=AccountingPolicy(
                    settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH
                ),
                settlement_boundary=wrong_session,
            )
        # The mismatched boundary must not release anything: the batch
        # aborted before commit, so the bought units stay unavailable.
        position = state.positions[INSTRUMENT_ID]
        self.assertEqual(position.available_quantity, Decimal("0"))


class SameBatchCashOrderingTestCase(unittest.TestCase):
    def test_sell_proceeds_fund_same_batch_buy(self) -> None:
        """Doc section 12.2: initial 2000 cash plus 100 sellable units."""

        harness = MatchingHarness()
        sell = sequenced_order(OrderSide.SELL, 0)
        buy = sequenced_order(OrderSide.BUY, 1)
        state = portfolio("2000", quantities={INSTRUMENT_ID: ("100", "100")})

        result, state, _ = harness.run([buy, sell], portfolio_state=state)

        # Input order is deliberately [buy, sell]; the frozen priority is
        # sell-then-buy by submission_sequence.
        self.assertEqual(len(result.fills), 2)
        self.assertEqual(result.fills[0].side, OrderSide.SELL)
        self.assertEqual(state.account.available_cash, Decimal("1990"))
        position = state.positions[INSTRUMENT_ID]
        self.assertEqual(position.quantity, Decimal("100"))
        # Freshly bought units are not available until the next boundary.
        self.assertEqual(position.available_quantity, Decimal("0"))

    def test_processing_order_follows_submission_sequence_not_input_order(self) -> None:
        harness = MatchingHarness()
        first_sell = sequenced_order(OrderSide.SELL, 10)
        second_sell = sequenced_order(OrderSide.SELL, 20)
        state = portfolio("0", quantities={INSTRUMENT_ID: ("200", "200")})

        result, state, _ = harness.run(
            [second_sell, first_sell], portfolio_state=state
        )

        self.assertEqual(len(result.fills), 2)
        self.assertEqual(result.fills[0].order_id, first_sell.order_id)
        self.assertEqual(result.fills[1].order_id, second_sell.order_id)


class ContractMultiplierTestCase(unittest.TestCase):
    def test_buy_with_multiplier_10_needs_full_gross_notional(self) -> None:
        """Doc section 12.4: 100 x 10.00 x 10 needs 10,000, not 1,000."""

        harness = MatchingHarness(contract_multiplier=10)
        buy = sequenced_order(OrderSide.BUY, 0)
        result, state, _ = harness.run([buy], portfolio_state=portfolio("2000"))

        self.assertEqual(result.fills, ())
        self.assertEqual(buy.status_reason, "INSUFFICIENT_CASH")
        self.assertEqual(state.account.available_cash, Decimal("2000"))

    def test_multiplier_participates_in_cash_and_valuation_consistently(self) -> None:
        harness = MatchingHarness(contract_multiplier=10)
        buy = sequenced_order(OrderSide.BUY, 0)
        state = portfolio("20000")
        result, state, _ = harness.run([buy], portfolio_state=state)

        fill = result.fills[0]
        self.assertEqual(fill.gross_notional, Decimal("10000"))
        self.assertEqual(fill.contract_multiplier, Decimal("10"))
        # cash 20000 - 10005
        self.assertEqual(state.account.available_cash, Decimal("9995"))

        valuation = AccountingPolicy().value(
            state,
            {INSTRUMENT_ID: "12.00"},
            as_of=OPEN + timedelta(hours=1),
            contract_multipliers={INSTRUMENT_ID: 10},
        )
        # unrealized = (12 - avg) * 100 * 10 where avg includes fees/units.
        expected_avg = Decimal("10005") / Decimal("100")
        self.assertAlmostEqual(
            float(valuation.market_value), float(Decimal("12000")), places=6
        )
        self.assertAlmostEqual(
            float(state.positions[INSTRUMENT_ID].unrealized_pnl),
            float((Decimal("12") - expected_avg) * 100 * 10),
            places=6,
        )


class FeeCategoryResolutionTestCase(unittest.TestCase):
    def test_required_category_missing_from_snapshot_fails_closed(self) -> None:
        schedule = FeeSchedule(key="s", fee_rules=(
            FeeRule(
                key="stamp_duty", category="stamp_duty", side="sell",
                rate="0.001",
                rounding_level=FeeRoundingLevel.FEE_ITEM,
                rounding_scope="stamp", rounding_mode=FeeRoundingMode.HALF_UP,
                rounding_precision="0.01",
            ),
        ))
        with self.assertRaises(FeeRuleUnresolvedError):
            resolve_instrument_fee_rules(
                schedule,
                fee_categories={"commission"},
                side="buy",
            )

    def test_undeclared_categories_are_never_charged(self) -> None:
        schedule = FeeScheduleSnapshot(
            key="acct_fee_v1",
            fee_rules=(
                FeeRule(
                    key="commission", category="commission", rate="0.0003",
                    minimum="5",
                    rounding_level=FeeRoundingLevel.FEE_ITEM,
                    rounding_scope="commission", rounding_mode=FeeRoundingMode.HALF_UP,
                    rounding_precision="0.01",
                ),
                FeeRule(
                    key="stamp_duty", category="stamp_duty", side="sell",
                    rate="0.001",
                    rounding_level=FeeRoundingLevel.FEE_ITEM,
                    rounding_scope="stamp", rounding_mode=FeeRoundingMode.HALF_UP,
                    rounding_precision="0.01",
                ),
            ),
        )
        rules = resolve_instrument_fee_rules(
            schedule, fee_categories={"commission"}, side="sell"
        )
        breakdown = FeeCalculator(
            FeeScheduleSnapshot(key=schedule.key, fee_rules=rules)
        ).calculate(side="sell", notional="1000")
        self.assertEqual(breakdown.total, Decimal("5.00"))
        categories = {component.category for component in breakdown.components}
        self.assertNotIn("stamp_duty", categories)

    def test_quantity_based_measure_uses_fill_quantity(self) -> None:
        rule = FeeRule(
            key="transfer_fee", category="transfer_fee",
            base_measure=FeeBaseMeasure.QUANTITY, rate="0.001",
            rounding_level=FeeRoundingLevel.FEE_ITEM,
            rounding_scope="transfer", rounding_mode=FeeRoundingMode.HALF_UP,
            rounding_precision="0.01",
        )
        breakdown = FeeCalculator(
            FeeSchedule(key="s", fee_rules=(rule,))
        ).calculate(side="buy", notional="1000", quantity="100")
        self.assertEqual(breakdown.total, Decimal("0.10"))

    def test_side_both_rule_applies_to_both_directions(self) -> None:
        rule = FeeRule(
            key="commission", category="commission", side="both", rate="0.0003",
            rounding_level=FeeRoundingLevel.FEE_ITEM,
            rounding_scope="commission", rounding_mode=FeeRoundingMode.HALF_UP,
            rounding_precision="0.01",
        )
        calculator = FeeCalculator(FeeSchedule(key="s", fee_rules=(rule,)))
        self.assertEqual(
            calculator.calculate(side="buy", notional="10000").total,
            Decimal("3.00"),
        )
        self.assertEqual(
            calculator.calculate(side="sell", notional="10000").total,
            Decimal("3.00"),
        )

    def test_unmatched_fee_category_rejects_order_as_fee_rule_unresolved(self) -> None:
        empty_schedule = FeeSchedule(key="only_stamp", fee_rules=(
            FeeRule(
                key="stamp_duty", category="stamp_duty", side="sell",
                rate="0.001",
                rounding_level=FeeRoundingLevel.FEE_ITEM,
                rounding_scope="stamp", rounding_mode=FeeRoundingMode.HALF_UP,
                rounding_precision="0.01",
            ),
        ))
        harness = MatchingHarness(fee_schedule=empty_schedule)
        buy = sequenced_order(OrderSide.BUY, 0)
        result, state, _ = harness.run([buy])

        self.assertEqual(result.fills, ())
        self.assertEqual(buy.status, OrderStatus.REJECTED)
        self.assertEqual(buy.status_reason, "FEE_RULE_UNRESOLVED")
        self.assertEqual(state.account.available_cash, Decimal("10000"))


class BatchAtomicityTestCase(unittest.TestCase):
    def test_failed_shadow_application_aborts_whole_batch(self) -> None:
        harness = MatchingHarness()
        buy = sequenced_order(OrderSide.BUY, 0)
        state = portfolio("2000")

        context = session(harness.states)
        accounting = AccountingPolicy(
            settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH
        )

        def failing_factory(fill):
            raise ValueError("simulated ledger failure")

        with self.assertRaises(AccountingBatchAbortedError) as caught:
            harness.service.run_opening_match(
                session=context,
                orders=[buy],
                policies=harness.policies,
                portfolio=state,
                accounting=accounting,
                settlement_plan_factory=failing_factory,
            )
        self.assertEqual(caught.exception.code, ACCOUNTING_BATCH_ABORTED)

        # Nothing leaked: formal account, positions, and order untouched.
        self.assertEqual(state.account.available_cash, Decimal("2000"))
        self.assertNotIn(INSTRUMENT_ID, state.positions)
        self.assertEqual(buy.status, OrderStatus.SUBMITTED)
        self.assertEqual(accounting.pending_batches(), ())

    def test_batch_without_settlement_boundary_and_due_batches_is_blocked(self) -> None:
        from app.backtesting.session_matching import SessionMatchError

        harness = MatchingHarness()
        buy = sequenced_order(OrderSide.BUY, 0)
        state = portfolio("2000")
        _, state, accounting = harness.run([buy], portfolio_state=state)

        # The bought batch settles at the next session; matching that
        # session without supplying any boundary must be blocked.
        next_open = OPEN + timedelta(days=1)
        context = session({INSTRUMENT_ID: market_state(timestamp=next_open)}, opening=next_open)
        sell = sequenced_order(OrderSide.SELL, 0)
        with self.assertRaises(SessionMatchError):
            harness.service.run_opening_match(
                session=context,
                orders=[sell],
                policies=harness.policies,
                portfolio=state,
                accounting=accounting,
                settlement_boundary=None,
            )


class ReplayDeterminismTestCase(unittest.TestCase):
    def test_identical_inputs_replay_to_identical_results(self) -> None:
        def run_once(order_ids):
            harness = MatchingHarness()
            orders = [
                sequenced_order(OrderSide.BUY, 0, order_id=order_ids[0]),
                sequenced_order(OrderSide.SELL, 1, order_id=order_ids[1]),
            ]
            state = portfolio("2000", quantities={INSTRUMENT_ID: ("100", "100")})
            context = session(harness.states)
            # Fix the otherwise-random session id for replay identity.
            fixed_session = SessionContext(
                session_id=UUID(int=42),
                calendar_id=context.calendar_id,
                session_date=context.session_date,
                opening_match_at=context.opening_match_at,
                close_at=context.close_at,
                exchange_open=True,
                market_states=harness.states,
            )
            accounting = AccountingPolicy(
                settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH
            )
            result = harness.service.run_opening_match(
                session=fixed_session,
                orders=orders,
                policies=harness.policies,
                portfolio=state,
                accounting=accounting,
                settlement_plan_factory=deferred_plan_factory(),
            )
            return result, state

        ids = (uuid4(), uuid4())
        result_a, state_a = run_once(ids)
        result_b, state_b = run_once(ids)

        self.assertEqual(result_a.batch_id, result_b.batch_id)
        self.assertEqual(
            [(f.fill_id, f.side, f.price, f.quantity, f.fees) for f in result_a.fills],
            [(f.fill_id, f.side, f.price, f.quantity, f.fees) for f in result_b.fills],
        )
        self.assertEqual(
            state_a.account.available_cash, state_b.account.available_cash
        )
        self.assertEqual(
            state_a.positions[INSTRUMENT_ID].quantity,
            state_b.positions[INSTRUMENT_ID].quantity,
        )

    def test_input_permutation_does_not_change_fill_order(self) -> None:
        harness = MatchingHarness()
        forward, _, _ = harness.run(
            [
                sequenced_order(OrderSide.SELL, 0),
                sequenced_order(OrderSide.BUY, 1),
            ],
            portfolio_state=portfolio("0", quantities={INSTRUMENT_ID: ("200", "200")}),
        )
        backward, _, _ = harness.run(
            [
                sequenced_order(OrderSide.BUY, 1),
                sequenced_order(OrderSide.SELL, 0),
            ],
            portfolio_state=portfolio("0", quantities={INSTRUMENT_ID: ("200", "200")}),
        )
        self.assertEqual(
            [f.side for f in forward.fills],
            [f.side for f in backward.fills],
        )
        self.assertEqual(len(forward.fills), 1)
        self.assertEqual(forward.fills[0].side, OrderSide.SELL)


class ResultAuditRecordTestCase(unittest.TestCase):
    def test_fill_record_carries_multiplier_notional_and_settlement(self) -> None:
        from app.backtesting.result_models import (
            BacktestFillRecord,
            InstrumentDisplaySnapshot,
        )
        from app.instruments.domain import InstrumentDisplay

        display = InstrumentDisplaySnapshot.from_display(
            InstrumentDisplay(
                instrument_id=INSTRUMENT_ID,
                trading_code="510300",
                name="沪深300ETF",
            )
        )
        record = BacktestFillRecord(
            run_id=uuid4(),
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            display=display,
            side="buy",
            timestamp=OPEN,
            price="10.00",
            quantity="100",
            fees="5",
            contract_multiplier="10",
            fee_breakdown={
                "schedule_key": "acct_fee_v1",
                "components": [{"category": "commission", "amount": "5"}],
            },
            settlement_calendar_id=CALENDAR_ID,
            settlement_due_session=OPEN.date() + timedelta(days=1),
            settlement_boundary_id="boundary-1",
        )

        self.assertEqual(record.currency, "CNY")
        self.assertEqual(record.contract_multiplier, Decimal("10"))
        # Derived gross notional must include the multiplier.
        self.assertEqual(record.gross_notional, Decimal("10000"))

    def test_fill_record_mapping_includes_audit_columns(self) -> None:
        from app.backtesting.result_repository import _fill_record
        from app.backtesting.result_models import (
            BacktestFillRecord,
            InstrumentDisplaySnapshot,
        )
        from app.instruments.domain import InstrumentDisplay

        display = InstrumentDisplaySnapshot.from_display(
            InstrumentDisplay(
                instrument_id=INSTRUMENT_ID,
                trading_code="510300",
                name="沪深300ETF",
            )
        )
        dto = BacktestFillRecord(
            run_id=uuid4(),
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            display=display,
            side="sell",
            timestamp=OPEN,
            price="10.00",
            quantity="100",
            fees="5",
            currency="CNY",
            gross_notional="1000",
            settlement_boundary_id="boundary-1",
        )
        mapped = _fill_record(dto)

        for column in (
            "currency",
            "contract_multiplier",
            "gross_notional",
            "fee_breakdown",
            "settlement_calendar_id",
            "settlement_due_session",
            "settlement_boundary_id",
        ):
            self.assertIn(column, mapped)


if __name__ == "__main__":
    unittest.main()
