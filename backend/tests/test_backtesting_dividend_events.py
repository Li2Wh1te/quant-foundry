"""Acceptance tests for task package 05B: cash-dividend events.

Covers record-date entitlement (selling after the record date keeps the
dividend, buying after it gains nothing), crediting strictly after the
cash-effective session's opening match, unique-event-id idempotency,
revisions as new events, and calendar-derived cash-effective sessions.
"""

import unittest
from datetime import date
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.backtesting.accounting import (
    AccountState,
    AccountingPolicy,
    PortfolioState,
    SettlementPolicy,
)
from app.backtesting.dividends import (
    CashDividendEvent,
    DividendDerivationError,
    DividendEntryKind,
    DividendError,
    derive_cash_effective_session,
    entitlement_from_portfolio,
)
from app.backtesting.domain import PortfolioSnapshot
from tests.backtest_runtime_fixture import (
    CountingStrategyView,
    DictMarketData,
    INSTRUMENT_ID,
    ScriptedStrategy,
    build_axis,
    build_runner,
    session_open,
)

D0 = date(2026, 8, 3)
D1 = date(2026, 8, 4)
D2 = date(2026, 8, 5)
D3 = date(2026, 8, 6)
D4 = date(2026, 8, 7)


def dividend_event(
    *,
    event_id=None,
    instrument_id=INSTRUMENT_ID,
    record_date=D1,
    effective_session=D3,
    amount_per_share="0.10",
    entitlement_quantity=None,
    withholding_tax="0",
    entry_kind="dividend",
    revision_of=None,
):
    return CashDividendEvent(
        event_id=event_id if event_id is not None else uuid4(),
        instrument_id=instrument_id,
        ex_date=record_date,
        record_date=record_date,
        source_payment_date=effective_session,
        source_arrival_date=effective_session,
        cash_effective_session_id=effective_session,
        amount_per_share=Decimal(amount_per_share),
        entitlement_quantity=(
            Decimal(entitlement_quantity)
            if entitlement_quantity is not None
            else None
        ),
        withholding_tax=Decimal(withholding_tax),
        entry_kind=entry_kind,
        revision_of_event_id=revision_of,
        source_evidence={"source": "unit-test", "announcement": "EQ0001"},
    )


class StaticDividends:
    """Corporate-action source over one fixed event tuple."""

    def __init__(self, events) -> None:
        self._events = tuple(events)

    def cash_dividend_events(self):
        return self._events


class SellAfterRecordDateKeepsDividendTests(unittest.TestCase):
    """Acceptance 11a: units sold after the record date still receive the
    dividend paid in the derived cash-effective session."""

    def test_sold_position_still_receives_the_record_date_dividend(self) -> None:
        # D0 decision buys full weight; the fill lands at D1's open.
        # Record date is D1 (100 registered units); the D1-close decision
        # flattens, so the D2 open sells everything -- yet the dividend
        # still pays 100 x 0.10 when it lands on D3.
        axis = build_axis([D0, D1, D2, D3])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "102.00")},
                D2: {INSTRUMENT_ID: ("101.00", "103.00")},
                D3: {INSTRUMENT_ID: ("102.00", "104.00")},
            }
        )
        view = CountingStrategyView(
            {D0: "100.00", D1: "102.00", D2: "103.00", D3: "104.00"}
        )
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        runner = build_runner(
            run_id="run-record-date-sell",
            axis=axis,
            market_data=market_data,
            strategy_view=view,
            strategy=strategy,
            corporate_actions=StaticDividends(
                [dividend_event(record_date=D1, effective_session=D3)]
            ),
            initial_cash="10000",
        )

        result = runner.run()

        dividends = [
            e for e in result.events if e.event_type == "cash_dividend_applied"
        ]
        self.assertEqual(len(dividends), 1)
        payload = dividends[0]
        # The entitlement was frozen on the record date, before the sale.
        self.assertEqual(payload.step_sequence, 3)
        self.assertEqual(payload.payload["record_date"], D1.isoformat())
        self.assertEqual(
            payload.payload["entitlement_quantity"], Decimal("100")
        )
        self.assertEqual(payload.payload["quantity"], Decimal("100"))
        self.assertEqual(payload.payload["cash_delta"], Decimal("10"))

        # Final equity: the flat account holds the sale proceeds (100 x
        # 101 = 10,100) plus the dividend (10) -- proof the sold units
        # kept their entitlement.
        self.assertEqual(result.equity_curve[3].equity, Decimal("10110"))


class BuyAfterRecordDateGainsNothingTests(unittest.TestCase):
    """Acceptance 11b: units bought after the record date gain no
    entitlement for that dividend."""

    def test_later_purchase_does_not_increase_the_entitlement(self) -> None:
        # 100 units are registered on the record date D1; the top-up buy
        # of another 100 units happens at D2's open.  The dividend still
        # pays for exactly the registered 100.
        axis = build_axis([D0, D1, D2, D3])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("9.90", "10.00")},
                D1: {INSTRUMENT_ID: ("10.00", "10.20")},
                D2: {INSTRUMENT_ID: ("10.10", "10.30")},
                D3: {INSTRUMENT_ID: ("10.20", "10.40")},
            }
        )
        view = CountingStrategyView(
            {D0: "10.00", D1: "10.20", D2: "10.30", D3: "10.40"}
        )
        strategy = ScriptedStrategy(
            {0: {str(INSTRUMENT_ID): "0.5"}, 1: {str(INSTRUMENT_ID): "1"}}
        )
        runner = build_runner(
            run_id="run-record-date-buy",
            axis=axis,
            market_data=market_data,
            strategy_view=view,
            strategy=strategy,
            corporate_actions=StaticDividends(
                [dividend_event(record_date=D1, effective_session=D3)]
            ),
            initial_cash="3000",
        )

        result = runner.run()

        dividends = [
            e for e in result.events if e.event_type == "cash_dividend_applied"
        ]
        self.assertEqual(len(dividends), 1)
        self.assertEqual(
            dividends[0].payload["entitlement_quantity"], Decimal("100")
        )
        self.assertEqual(dividends[0].payload["cash_delta"], Decimal("10"))
        # Two top-up buys happened: 100 units at D1 and 100 more at D2;
        # the third fill is the end-of-run flatten sell at D3.
        fills = [e for e in result.events if e.event_type == "fill_created"]
        self.assertEqual(len(fills), 3)


class EffectiveSessionOrderingTests(unittest.TestCase):
    """Acceptance 12: dividend cash lands only inside its derived
    cash-effective session and never funds that morning's match."""

    def test_credit_happens_in_the_derived_session_after_the_match(self) -> None:
        axis = build_axis([D0, D1, D2, D3])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "102.00")},
                D2: {INSTRUMENT_ID: ("101.00", "103.00")},
                D3: {INSTRUMENT_ID: ("102.00", "104.00")},
            }
        )
        view = CountingStrategyView(
            {D0: "100.00", D1: "102.00", D2: "103.00", D3: "104.00"}
        )
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        runner = build_runner(
            run_id="run-effective-ordering",
            axis=axis,
            market_data=market_data,
            strategy_view=view,
            strategy=strategy,
            corporate_actions=StaticDividends(
                [dividend_event(record_date=D1, effective_session=D3)]
            ),
            initial_cash="10000",
        )

        result = runner.run()

        d3 = [
            (e.event_type, e.event_sequence) for e in result.events if e.step_sequence == 3
        ]
        types = dict()
        for event_type, sequence in d3:
            types.setdefault(event_type, []).append(sequence)
        # Nothing trades on D3 (the position was sold on D2): the
        # dividend credit is the only cash movement of that session and
        # it lands before the close valuation records it.
        self.assertNotIn("fill_created", types)
        self.assertIn("cash_dividend_applied", types)
        self.assertLess(
            types["cash_dividend_applied"][0], types["portfolio_valued"][0]
        )

    def test_accounting_refuses_to_credit_outside_the_effective_session(self) -> None:
        policy = AccountingPolicy(currency="CNY")
        portfolio = _empty_portfolio()
        event = dividend_event(effective_session=D3, entitlement_quantity="100")

        with self.assertRaises(DividendError) as context:
            policy.apply_cash_dividend_event(portfolio, event, session_date=D2)
        self.assertIn("becomes effective", str(context.exception))

        # The correct session accepts the event.
        application = policy.apply_cash_dividend_event(
            portfolio, event, session_date=D3
        )
        self.assertTrue(application.applied)
        self.assertEqual(application.cash_delta, Decimal("10"))


class DuplicateEventIdempotencyTests(unittest.TestCase):
    """Acceptance 13: one unique event id can never pay twice."""

    def test_replaying_the_same_event_id_is_a_no_op(self) -> None:
        policy = AccountingPolicy(currency="CNY")
        portfolio = _empty_portfolio(cash="1000")
        event_id = uuid4()
        event = dividend_event(
            event_id=event_id,
            effective_session=D2,
            entitlement_quantity="400",
            amount_per_share="0.10",
        )

        first = policy.apply_cash_dividend_event(portfolio, event, session_date=D2)
        second = policy.apply_cash_dividend_event(portfolio, event, session_date=D2)
        third = policy.apply_cash_dividend_event(portfolio, event, session_date=D2)

        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertFalse(third.applied)
        self.assertEqual(policy.apply_cash_dividend_event.__self__, policy)
        # Exactly one credit of 40 hit the account.
        self.assertEqual(
            portfolio.account.cash_balances["CNY"], Decimal("1040")
        )

    def test_duplicate_declaration_ids_are_rejected_at_admission(self) -> None:
        from app.backtesting.runtime import DeterministicBacktestRunner

        shared_id = uuid4()
        source = StaticDividends(
            [
                dividend_event(event_id=shared_id),
                dividend_event(event_id=shared_id, effective_session=D2),
            ]
        )
        with self.assertRaises(Exception) as context:
            build_runner(
                run_id="run-duplicate-decl",
                axis=build_axis([D0, D1]),
                market_data=DictMarketData({}),
                strategy_view=CountingStrategyView({}),
                strategy=ScriptedStrategy({}),
                corporate_actions=source,
            ).run()
        self.assertIn("twice", str(context.exception))


class RevisionAsNewEventTests(unittest.TestCase):
    """Revisions and reversals enter as new events; history is untouched."""

    def test_reversal_debits_through_a_new_referencing_event(self) -> None:
        policy = AccountingPolicy(currency="CNY")
        portfolio = _empty_portfolio(cash="1000")
        original_id = uuid4()
        original = dividend_event(
            event_id=original_id,
            effective_session=D2,
            entitlement_quantity="400",
            amount_per_share="0.10",
        )
        policy.apply_cash_dividend_event(portfolio, original, session_date=D2)
        self.assertEqual(
            portfolio.account.cash_balances["CNY"], Decimal("1040")
        )
        # Replaying the original id in its own session stays a no-op...
        replay = policy.apply_cash_dividend_event(
            portfolio, original, session_date=D2
        )
        self.assertFalse(replay.applied)
        self.assertEqual(
            portfolio.account.cash_balances["CNY"], Decimal("1040")
        )
        # ...and the correcting reversal is a brand-new event referencing
        # the original, debiting the net amount in its own session.
        reversal = dividend_event(
            effective_session=D3,
            entitlement_quantity="400",
            amount_per_share="0.10",
            entry_kind=DividendEntryKind.REVERSAL.value,
            revision_of=original_id,
        )
        application = policy.apply_cash_dividend_event(
            portfolio, reversal, session_date=D3
        )
        self.assertTrue(application.applied)
        self.assertEqual(application.cash_delta, Decimal("-40"))
        self.assertEqual(
            portfolio.account.cash_balances["CNY"], Decimal("1000")
        )

    def test_reversal_requires_a_reference_and_cannot_overdraw_cash(self) -> None:
        policy = AccountingPolicy(currency="CNY")
        portfolio = _empty_portfolio(cash="5")
        orphan = dividend_event(
            effective_session=D2,
            entitlement_quantity="400",
            amount_per_share="0.10",
            entry_kind=DividendEntryKind.REVERSAL.value,
        )
        with self.assertRaises(DividendError):
            policy.apply_cash_dividend_event(portfolio, orphan, session_date=D2)

        referencing = dividend_event(
            effective_session=D2,
            entitlement_quantity="400",
            amount_per_share="0.10",
            entry_kind=DividendEntryKind.REVERSAL.value,
            revision_of=uuid4(),
        )
        with self.assertRaises(Exception) as context:
            policy.apply_cash_dividend_event(
                portfolio, referencing, session_date=D2
            )
        self.assertIn("negative", str(context.exception))
        # Nothing changed.
        self.assertEqual(
            portfolio.account.cash_balances["CNY"], Decimal("5")
        )


class WithholdingAndEntitlementRuleTests(unittest.TestCase):
    def test_withholding_tax_reduces_the_net_credit(self) -> None:
        policy = AccountingPolicy(currency="CNY")
        portfolio = _empty_portfolio(cash="0")
        event = dividend_event(
            effective_session=D2,
            entitlement_quantity="1000",
            amount_per_share="0.10",
            withholding_tax="20",
        )
        application = policy.apply_cash_dividend_event(
            portfolio, event, session_date=D2
        )
        self.assertEqual(application.cash_delta, Decimal("80"))

    def test_unsettled_lot_counting_follows_the_declared_rule(self) -> None:
        from app.backtesting.accounting import DeferredSettlementPlan, Fill, OrderSide

        accounting = AccountingPolicy(
            currency="CNY",
            settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH,
        )
        portfolio = _empty_portfolio(cash="100000")

        def buy(fill_id, session, quantity):
            accounting.apply_fill(
                portfolio,
                Fill(
                    fill_id=fill_id,
                    order_id=uuid4(),
                    instrument_id=INSTRUMENT_ID,
                    timestamp=session_open(session),
                    side=OrderSide.BUY,
                    price="10",
                    quantity=quantity,
                    currency="CNY",
                ),
                settlement_plan=DeferredSettlementPlan(
                    calendar_id="XSHG",
                    trade_session=session,
                    settlement_session=session.replace(day=session.day + 1),
                ),
            )

        # D0's lot was already released before D1; D1's own lot is still
        # pending when the record date (D1) closes.
        buy(uuid4(), D0, "500")
        accounting.settle_pending_before_open_match(
            portfolio, calendar_id="XSHG", session_date=D1
        )
        buy(uuid4(), D1, "500")

        # Position quantity is 1,000; exactly the D1 lot (500 units) is
        # still unsettled at the record-date freeze.
        including = entitlement_from_portfolio(
            portfolio, accounting, instrument_id=INSTRUMENT_ID,
            include_pending_settlement=True,
        )
        settled_only = entitlement_from_portfolio(
            portfolio, accounting, instrument_id=INSTRUMENT_ID,
            include_pending_settlement=False,
        )
        self.assertEqual(including, Decimal("1000"))
        self.assertEqual(settled_only, Decimal("500"))


class CashEffectiveSessionDerivationTests(unittest.TestCase):
    """The cash-effective session comes from the trading calendar, never
    from natural-day guesses."""

    def setUp(self) -> None:
        from tests.backtest_runtime_fixture import (
            SessionListSettlementCalendar,
        )

        # D2 is deliberately missing: a mid-week holiday.
        self.gateway = SessionListSettlementCalendar(
            {"XSHG": [D0, D1, D3, D4]}
        )

    def test_arrival_on_an_open_session_is_used_directly(self) -> None:
        effective = derive_cash_effective_session(
            self.gateway, calendar_id="XSHG", source_arrival_date=D3
        )
        self.assertEqual(effective, D3)

    def test_arrival_on_a_closed_day_rolls_to_the_next_open_session(self) -> None:
        effective = derive_cash_effective_session(
            self.gateway, calendar_id="XSHG", source_arrival_date=D2
        )
        self.assertEqual(effective, D3)

    def test_calendar_without_any_open_session_fails_explicitly(self) -> None:
        with self.assertRaises(DividendDerivationError):
            derive_cash_effective_session(
                self.gateway,
                calendar_id="XSHG",
                source_arrival_date=date(2027, 1, 1),
            )


def _empty_portfolio(*, cash="0") -> PortfolioState:
    return PortfolioState(
        account=AccountState(
            cash_balances={"CNY": cash},
            available_cash=cash,
            frozen_cash="0",
            margin_used="0",
            margin_available="0",
            equity=cash,
        ),
        as_of=session_open(D0),
    )


if __name__ == "__main__":
    unittest.main()
