"""Acceptance tests for task package 05B: settlement lots and accounting.

Covers per-fill pending-settlement lots (release phase, release session,
calendar version, deterministic lot ids), accumulated availability
across multiple releases, T+1 sale restrictions, sell-side quantity and
availability handling, idempotent releases, and batch atomicity.
"""

import unittest
from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.backtesting.accounting import (
    AccountState,
    AccountingPolicy,
    DeferredSettlementPlan,
    Fill,
    OrderSide,
    PortfolioState,
    SettlementBoundaryMissedError,
    SettlementPolicy,
    SettlementReleasePhase,
)
from tests.backtest_runtime_fixture import session_open

D1 = date(2026, 8, 4)
D2 = date(2026, 8, 5)
D3 = date(2026, 8, 6)

INSTRUMENT = uuid4()


def portfolio(cash="100000"):
    return PortfolioState(
        account=AccountState(
            cash_balances={"CNY": Decimal(cash)},
            available_cash=Decimal(cash),
            frozen_cash="0",
            margin_used="0",
            margin_available="0",
            equity=cash,
        ),
        as_of=session_open(D1),
    )


def policy():
    return AccountingPolicy(
        currency="CNY",
        settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH,
    )


def buy_fill(quantity="100", *, price="10", session=D1):
    return Fill(
        fill_id=uuid4(),
        order_id=uuid4(),
        instrument_id=INSTRUMENT,
        timestamp=session_open(session),
        side=OrderSide.BUY,
        price=price,
        quantity=quantity,
        currency="CNY",
    )


def plan_for(session):
    return DeferredSettlementPlan(
        calendar_id="XSHG",
        trade_session=session,
        settlement_session=date.fromordinal(session.toordinal() + 1),
        calendar_version="v2026a",
    )


def position(portfolio_state):
    return portfolio_state.positions[INSTRUMENT]


class PendingSettlementLotTests(unittest.TestCase):
    def test_buy_creates_a_lot_with_frozen_release_metadata(self) -> None:
        accounting = policy()
        state = portfolio()
        fill = buy_fill("100")

        accounting.apply_fill(state, fill, settlement_plan=plan_for(D1))

        batches = accounting.pending_batches()
        self.assertEqual(len(batches), 1)
        lot = batches[0]
        # The frozen lot carries its release session, the fixed formal
        # release phase, and the pinned calendar version.
        self.assertEqual(lot.release_session_id, D2)
        self.assertEqual(lot.release_phase, SettlementReleasePhase.BEFORE_OPEN_MATCH)
        self.assertEqual(lot.calendar_version, "v2026a")
        self.assertEqual(lot.source_fill_id, fill.fill_id)
        # The result layer can trace the fill back to its exact lot.
        self.assertEqual(accounting.settlement_lot_for_fill(fill.fill_id), lot.lot_id)

    def test_lot_ids_are_deterministic_per_source_fill(self) -> None:
        first = policy().pending_batches()
        # Two policies processing the same fill produce the same lot id.
        p = portfolio()
        a = policy()
        b = policy()
        fill = buy_fill("100")
        plan = plan_for(D1)
        a.apply_fill(p, fill, settlement_plan=plan)
        b.apply_fill(portfolio(), fill, settlement_plan=plan)
        self.assertEqual(a.pending_batches()[0].lot_id, b.pending_batches()[0].lot_id)
        self.assertNotEqual(first, a.pending_batches())


class TPlusOneAvailabilityTests(unittest.TestCase):
    """Acceptance 8: bought units are unsellable today and unlock right
    before the next open session's match."""

    def test_buy_day_units_are_not_available_and_release_next_open(self) -> None:
        accounting = policy()
        state = portfolio()
        accounting.apply_fill(state, buy_fill("100"), settlement_plan=plan_for(D1))

        # D1 close: held, but not yet sellable.
        self.assertEqual(position(state).quantity, Decimal("100"))
        self.assertEqual(position(state).available_quantity, Decimal("0"))

        # D2 pre-open-match boundary releases exactly the due lot.
        released = accounting.settle_pending_before_open_match(
            state, calendar_id="XSHG", session_date=D2
        )
        self.assertEqual(released, (INSTRUMENT,))
        self.assertEqual(position(state).quantity, Decimal("100"))
        self.assertEqual(position(state).available_quantity, Decimal("100"))


class MultiLotAccumulationTests(unittest.TestCase):
    """Acceptance 7: several lots release per batch and availability
    accumulates without ever being overwritten."""

    def test_lots_from_two_days_accumulate(self) -> None:
        accounting = policy()
        state = portfolio()
        accounting.apply_fill(state, buy_fill("100"), settlement_plan=plan_for(D1))
        accounting.apply_fill(
            state, buy_fill("100", session=D2), settlement_plan=plan_for(D2)
        )
        self.assertEqual(len(accounting.pending_batches()), 2)

        # D2 boundary releases only the D1 lot.
        accounting.settle_pending_before_open_match(
            state, calendar_id="XSHG", session_date=D2
        )
        self.assertEqual(position(state).available_quantity, Decimal("100"))
        self.assertEqual(position(state).quantity, Decimal("200"))

        # D3 boundary releases the D2 lot ON TOP of the existing value.
        accounting.settle_pending_before_open_match(
            state, calendar_id="XSHG", session_date=D3
        )
        self.assertEqual(position(state).available_quantity, Decimal("200"))
        self.assertEqual(position(state).quantity, Decimal("200"))
        self.assertEqual(len(accounting.pending_batches()), 0)
        self.assertEqual(len(accounting.settled_batches()), 2)

    def test_same_session_lots_release_together_onto_one_write(self) -> None:
        accounting = policy()
        state = portfolio()
        accounting.apply_fill(state, buy_fill("60"), settlement_plan=plan_for(D1))
        accounting.apply_fill(state, buy_fill("40"), settlement_plan=plan_for(D1))

        accounting.settle_pending_before_open_match(
            state, calendar_id="XSHG", session_date=D2
        )
        self.assertEqual(position(state).available_quantity, Decimal("100"))

    def test_release_is_idempotent_per_boundary(self) -> None:
        accounting = policy()
        state = portfolio()
        accounting.apply_fill(state, buy_fill("100"), settlement_plan=plan_for(D1))
        accounting.settle_pending_before_open_match(
            state, calendar_id="XSHG", session_date=D2
        )

        # A retried boundary releases nothing new but stays auditable.
        released_again = accounting.settle_pending_before_open_match(
            state, calendar_id="XSHG", session_date=D2
        )
        self.assertEqual(released_again, ())
        self.assertEqual(len(accounting.releases()), 2)
        self.assertEqual(
            position(state).available_quantity, Decimal("100")
        )


class SellAccountingTests(unittest.TestCase):
    """Acceptance 9: selling reduces quantity AND available_quantity."""

    def test_sell_reduces_quantity_and_available_quantity(self) -> None:
        accounting = policy()
        state = portfolio()
        fill = buy_fill("100")
        accounting.apply_fill(state, fill, settlement_plan=plan_for(D1))
        accounting.settle_pending_before_open_match(
            state, calendar_id="XSHG", session_date=D2
        )

        sell = Fill(
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=INSTRUMENT,
            timestamp=session_open(D2),
            side=OrderSide.SELL,
            price="11",
            quantity="100",
            currency="CNY",
        )
        application = accounting.apply_fill(state, sell)

        self.assertTrue(application.applied)
        self.assertNotIn(INSTRUMENT, state.positions)
        # Net proceeds land in cash: 100 x 11 minus zero fees.
        self.assertEqual(
            state.account.cash_balances["CNY"], Decimal("100000") - Decimal("1000") + Decimal("1100")
        )

    def test_sell_cannot_touch_pending_settlement_units(self) -> None:
        accounting = policy()
        state = portfolio()
        accounting.apply_fill(state, buy_fill("100"), settlement_plan=plan_for(D1))

        sell = Fill(
            fill_id=uuid4(),
            order_id=uuid4(),
            instrument_id=INSTRUMENT,
            timestamp=session_open(D1),
            side=OrderSide.SELL,
            price="11",
            quantity="100",
            currency="CNY",
        )
        with self.assertRaises(Exception) as context:
            accounting.apply_fill(state, sell)
        self.assertIn("available_quantity", str(context.exception))
        # Nothing changed: the position keeps its locked units.
        self.assertEqual(position(state).quantity, Decimal("100"))
        self.assertEqual(position(state).available_quantity, Decimal("0"))


class AtomicityTests(unittest.TestCase):
    """Acceptance 10: failures never leave partial account state."""

    def test_skipped_boundary_blocks_the_release_atomically(self) -> None:
        accounting = policy()
        state = portfolio()
        accounting.apply_fill(state, buy_fill("100"), settlement_plan=plan_for(D1))

        # The runner first calls settle on D3: the D2 boundary was
        # skipped, so nothing may release and nothing may change.
        with self.assertRaises(SettlementBoundaryMissedError):
            accounting.settle_pending_before_open_match(
                state, calendar_id="XSHG", session_date=D3
            )
        self.assertEqual(len(accounting.pending_batches()), 1)
        self.assertEqual(position(state).available_quantity, Decimal("0"))

    def test_insufficient_cash_buy_commits_nothing(self) -> None:
        accounting = policy()
        state = portfolio(cash="500")
        # 100 units at 10 costs 1,000 -- more than the available cash.
        with self.assertRaises(Exception):
            accounting.apply_fill(state, buy_fill("100"), settlement_plan=plan_for(D1))
        self.assertEqual(state.account.cash_balances["CNY"], Decimal("500"))
        self.assertNotIn(INSTRUMENT, state.positions)
        self.assertEqual(accounting.pending_batches(), ())
        # The failed fill id was never consumed: a corrected retry works.
        state.account.cash_balances["CNY"] = Decimal("50000")
        state.account.available_cash = Decimal("50000")
        application = accounting.apply_fill(
            state, buy_fill("100"), settlement_plan=plan_for(D1)
        )
        self.assertTrue(application.applied)


class CalendarVersionThreadingTests(unittest.TestCase):
    """Formal lots pin the calendar definition version they were
    resolved against when the gateway is version-aware."""

    def test_runtime_lots_carry_the_gateway_calendar_version(self) -> None:
        from tests.backtest_runtime_fixture import (
            CountingStrategyView,
            DictMarketData,
            INSTRUMENT_ID as FIXTURE_INSTRUMENT,
            ScriptedStrategy,
            build_axis,
            build_runner,
        )

        d0 = date(2026, 8, 3)
        d1 = date(2026, 8, 4)
        d2 = date(2026, 8, 5)
        runner = build_runner(
            run_id="run-lot-calendar-version",
            axis=build_axis([d0, d1, d2]),
            market_data=DictMarketData(
                {
                    d0: {FIXTURE_INSTRUMENT: ("99.00", "100.00")},
                    d1: {FIXTURE_INSTRUMENT: ("100.00", "102.00")},
                    d2: {FIXTURE_INSTRUMENT: ("101.00", "103.00")},
                }
            ),
            strategy_view=CountingStrategyView(
                {d0: "100.00", d1: "102.00", d2: "103.00"}
            ),
            strategy=ScriptedStrategy({0: {str(FIXTURE_INSTRUMENT): "1"}}),
            initial_cash="20000",
        )
        result = runner.run()

        fills = [e for e in result.events if e.event_type == "fill_created"]
        self.assertTrue(fills)
        lot_id = next(
            e.payload["settlement_lot_id"]
            for e in result.events
            if e.event_type == "fill_applied"
            and e.payload["settlement_lot_id"] is not None
        )
        self.assertIsNotNone(lot_id)
        batches = (
            runner._accounting.pending_batches()
            + runner._accounting.settled_batches()
        )
        lot = next(b for b in batches if str(b.lot_id) == lot_id)
        # The versioned fixture gateway reported its definition version
        # and the lot froze it.
        self.assertEqual(lot.calendar_version, "fixture-v1")
        self.assertEqual(lot.release_phase, SettlementReleasePhase.BEFORE_OPEN_MATCH)


if __name__ == "__main__":
    unittest.main()
