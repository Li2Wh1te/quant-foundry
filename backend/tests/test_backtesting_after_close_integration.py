"""Integration tests for ``after_close_to_next_open@1`` end-to-end runs.

The scenarios follow the task-package acceptance examples: the documented
cash/dividend/T+1 walk-through on three official sessions, the minimal
integration case (three sessions, two decisions, one fill), proof that an
open match cannot spend same-day dividends, order expiry without rollover,
and fill-application idempotency under retries.
"""

import unittest
from datetime import date
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.backtesting.dividends import CashDividendEvent

from tests.backtest_runtime_fixture import (
    CountingStrategyView,
    DictMarketData,
    INSTRUMENT_ID,
    RecordingExecutionModel,
    ScriptedStrategy,
    build_axis,
    build_runner,
)

D0 = date(2026, 8, 3)
D1 = date(2026, 8, 4)
D2 = date(2026, 8, 5)
D3 = date(2026, 8, 6)


class StaticCorporateActions:
    """Cash-dividend source emitting full corporate-action events.

    Entries are ``(event_key, instrument_id, amount_per_share)``; the
    event key is the corporate action's stable identity and derives the
    unique ``event_id`` deterministically.  Fixture events register the
    entitlement on their session day (post-match holdings) and land the
    cash in that same session, strictly after its opening match.
    """

    def __init__(
        self,
        dividends_by_day: dict[date, list[tuple[str, UUID, str]]],
    ) -> None:
        self._events: list[CashDividendEvent] = []
        for day, entries in dividends_by_day.items():
            for event_key, instrument_id, amount in entries:
                self._events.append(
                    CashDividendEvent(
                        event_id=_dividend_event_id(event_key),
                        instrument_id=instrument_id,
                        ex_date=day,
                        record_date=day,
                        source_payment_date=day,
                        source_arrival_date=day,
                        cash_effective_session_id=day,
                        amount_per_share=Decimal(amount),
                        # The fixture calendar maps every session day to
                        # itself, so the arrival-day session derives back
                        # to that same day.
                        as_of=day,
                        source_evidence={"source": "fixture", "key": event_key},
                    )
                )

    def cash_dividend_events(self) -> tuple[CashDividendEvent, ...]:
        return tuple(self._events)


def _dividend_event_id(event_key: str) -> UUID:
    """Deterministic unique event id derived from the stable action key."""

    return uuid5(NAMESPACE_URL, f"quant-foundry:cash-dividend:{event_key}")


def document_scenario_runner(*, run_id: str = "run-doc", initial_cash: str = "10000"):
    """The task package's worked example over D0/D1/D2.

    D0 closes at 100 with 10,000 cash and a full-weight buy decision;
    D1 opens at 100 (the fill), pays a 200 cash dividend, and closes at
    102; D2 closes at 103.
    """

    axis = build_axis([D0, D1, D2])
    market_data = DictMarketData(
        {
            D0: {INSTRUMENT_ID: ("99.00", "100.00")},
            D1: {INSTRUMENT_ID: ("100.00", "102.00")},
            D2: {INSTRUMENT_ID: ("101.00", "103.00")},
        }
    )
    view = CountingStrategyView({D0: "100.00", D1: "102.00", D2: "103.00"})
    strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
    runner = build_runner(
        run_id=run_id,
        axis=axis,
        market_data=market_data,
        strategy_view=view,
        strategy=strategy,
        corporate_actions=StaticCorporateActions({D1: [("div-2026-D1-510300", INSTRUMENT_ID, "2")]}),
        initial_cash=initial_cash,
    )
    return runner


class DocumentedDividendScenarioTests(unittest.TestCase):
    def test_dividend_and_t_plus_one_walk_through_matches_the_document(self) -> None:
        runner = document_scenario_runner()
        result = runner.run()

        events = {(e.step_sequence, e.event_type): e for e in result.events}
        # Pre-match cash is the untouched 10,000; the buy consumes all of it.
        self.assertEqual(
            events[(1, "fill_created")].payload["execution_price"],
            Decimal("100"),
        )
        self.assertEqual(
            events[(1, "fill_applied")].payload["cash_delta"], Decimal("-10000")
        )
        # The dividend lands after matching: +200 on the emptied account.
        self.assertEqual(
            events[(1, "cash_dividend_applied")].payload["cash_delta"],
            Decimal("200"),
        )
        # D1 close equity = 100 x 102 + 200 = 10,400 exactly as documented.
        self.assertEqual(
            events[(1, "portfolio_valued")].payload["equity"], Decimal("10400")
        )

        # Within D1's open block, the dividend strictly follows accounting:
        # it can never fund the morning match.
        d1_sequences = {
            e.event_type: e.event_sequence
            for e in result.events
            if e.step_sequence == 1
        }
        self.assertLess(d1_sequences["fill_applied"], d1_sequences["cash_dividend_applied"])
        self.assertLess(
            d1_sequences["cash_dividend_applied"], d1_sequences["portfolio_valued"]
        )

        # The bought units are not sellable on the purchase day (T+1), but
        # the flatten decision still submits the full sell delta: capping
        # by availability is the matching stage's job, never the
        # interpreter's.
        step1_types = [
            e.event_type for e in result.events if e.step_sequence == 1
        ]
        self.assertIn("order_submitted", step1_types)
        # Availability is restored before the D2 open match and the sell
        # fills there at the D2 open price of 101.
        self.assertIn("settlement_restored", [e.event_type for e in result.events if e.step_sequence == 2])

    def test_minimal_case_sample_and_decision_counts(self) -> None:
        runner = document_scenario_runner()
        result = runner.run()

        # Close-equity samples exist for every formal session.
        self.assertEqual(len(result.equity_curve), 3)
        self.assertEqual([sample.session_date for sample in result.equity_curve], [D0, D1, D2])
        # Exactly two decisions (final day decides nothing).
        self.assertEqual(len(result.decisions), 2)
        types_by_step: dict[int, set[str]] = {}
        for event in result.events:
            types_by_step.setdefault(event.step_sequence, set()).add(event.event_type)
        # D0: submission without any fill.
        self.assertIn("order_submitted", types_by_step[0])
        self.assertNotIn("fill_created", types_by_step[0])
        # D1: the fill influences the same day's closing equity.
        self.assertIn("fill_created", types_by_step[1])
        self.assertEqual(result.equity_curve[1].equity, Decimal("10400"))
        # D2: settlement restore, the sell fills at the D2 open, and the
        # final close valuation follows -- no decisions and no new orders.
        self.assertEqual(
            types_by_step[2],
            {
                "settlement_restored",
                "fill_created",
                "fill_applied",
                "portfolio_valued",
            },
        )

    def test_final_equity_carries_position_at_d2_close(self) -> None:
        runner = document_scenario_runner()
        result = runner.run()

        # The flatten sell fills at the D2 open (101): 200 dividend cash
        # plus 100 x 101 sale proceeds, with nothing left in position.
        self.assertEqual(result.equity_curve[2].equity, Decimal("10300"))


class OpenMatchCannotSpendSameDayDividendsTests(unittest.TestCase):
    def insufficient_runner(self, *, run_id: str):
        """Full-weight buy sized at a 100 close, but D1 gaps up to 105.

        The 100-share intent needs 10,500 at the open against 10,000 cash,
        so it expires -- even though a same-day dividend would have added
        200 after the match.
        """

        axis = build_axis([D0, D1, D2])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("105.00", "102.00")},
                D2: {INSTRUMENT_ID: ("101.00", "103.00")},
            }
        )
        view = CountingStrategyView({D0: "100.00", D1: "102.00", D2: "103.00"})
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        return build_runner(
            run_id=run_id,
            axis=axis,
            market_data=market_data,
            strategy_view=view,
            strategy=strategy,
            corporate_actions=StaticCorporateActions({D1: [("div-2026-D1-510300", INSTRUMENT_ID, "2")]}),
            initial_cash="10000",
        )

    def test_insufficient_order_expires_despite_later_dividend(self) -> None:
        """An unaffordable morning order expires even though a dividend on
        the same session would have covered part of it afterwards."""

        result = self.insufficient_runner(run_id="run-expiry").run()

        expired = [e for e in result.events if e.event_type == "order_expired"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].payload["reason"], "insufficient_cash")
        # No fill ever happens, the dividend has no position to attach to,
        # and the account keeps its original cash through every valuation.
        self.assertFalse(
            [e for e in result.events if e.event_type == "fill_created"]
        )
        self.assertEqual(len(result.equity_curve), 3)
        self.assertEqual(result.equity_curve[-1].equity, Decimal("10000"))

    def test_unfilled_orders_never_rollover_to_the_next_session(self) -> None:
        result = self.insufficient_runner(run_id="run-rollover").run()

        expired_events = [
            e for e in result.events if e.event_type == "order_expired"
        ]
        # Exactly one match attempt on D1 expires the order; nothing is
        # resubmitted or re-matched on D2.
        self.assertEqual(len(expired_events), 1)
        self.assertEqual(expired_events[0].step_sequence, 1)
        outcomes = result.order_outcomes
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, "expired")


class SettlementCalendarResolutionTests(unittest.TestCase):
    """T+1 dates must come from the named calendar gateway, never from
    natural-calendar-day guesses."""

    def holiday_skipped_scenario(self, *, run_id: str):
        """Axis skips Wednesday D2 (a holiday): buy fills Tuesday D1's
        open and settles Thursday D3 before its open match."""

        d3 = date(2026, 8, 6)
        axis = build_axis([D0, D1, d3])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "102.00")},
                d3: {INSTRUMENT_ID: ("101.00", "103.00")},
            }
        )
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        return build_runner(
            run_id=run_id,
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView({D0: "100.00", D1: "102.00"}),
            strategy=strategy,
            corporate_actions=StaticCorporateActions({D1: [("div-2026-D1-510300", INSTRUMENT_ID, "2")]}),
        ), d3

    def test_settlement_skips_the_holiday_instead_of_using_the_next_day(
        self,
    ) -> None:
        runner, d3 = self.holiday_skipped_scenario(run_id="run-holiday")
        result = runner.run()

        # The restore happens on Thursday D3 -- never on the natural next
        # day, which was a market holiday with no session at all.
        restored = [
            e for e in result.events if e.event_type == "settlement_restored"
        ]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].step_sequence, 2)
        # The bought position is sellable exactly when the calendar says so.
        self.assertEqual(result.equity_curve[2].session_date, d3)

    def test_buy_on_the_final_session_fails_explicitly_without_a_natural_day_guess(
        self,
    ) -> None:
        from app.backtesting.runtime import PhaseExecutionError

        axis = build_axis([D0, D1])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "102.00")},
            }
        )
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        runner = build_runner(
            run_id="run-last-day-buy",
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView({D0: "100.00", D1: "102.00"}),
            strategy=strategy,
        )
        with self.assertRaises(PhaseExecutionError) as context:
            runner.run()
        # The fill on the final session cannot resolve a T+1 date; the run
        # fails in accounting instead of settling on trade_date + 1 day.
        self.assertEqual(context.exception.error_type, "SettlementScheduleError")
        self.assertEqual(context.exception.phase_key, "account")
        self.assertEqual(context.exception.step_sequence, 1)

    def test_settlement_uses_each_instruments_own_calendar(self) -> None:
        from tests.backtest_runtime_fixture import (
            SessionListSettlementCalendar,
            make_candidate,
        )

        other_instrument = UUID("00000000-0000-4000-8000-000000000002")
        axis = build_axis([D0, D1, D2])
        market_data = DictMarketData(
            {
                D0: {
                    INSTRUMENT_ID: ("99.00", "100.00"),
                    other_instrument: ("49.00", "50.00"),
                },
                D1: {
                    INSTRUMENT_ID: ("100.00", "102.00"),
                    other_instrument: ("50.00", "51.00"),
                },
                D2: {
                    INSTRUMENT_ID: ("101.00", "103.00"),
                    other_instrument: ("51.00", "52.00"),
                },
            },
            calendar_by_instrument={
                other_instrument: "SSE_OTHER",
            },
        )
        gateway = SessionListSettlementCalendar(
            {"XSHG": [D0, D1, D2], "SSE_OTHER": [D0, D1, D2]}
        )
        strategy = ScriptedStrategy(
            {
                # Half weight per instrument so both buys fit the cash.
                0: {
                    str(INSTRUMENT_ID): "0.5",
                    str(other_instrument): "0.5",
                }
            }
        )
        runner = build_runner(
            run_id="run-multi-calendar",
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView(
                {D0: "100.00", D1: "102.00", D2: "103.00"}
            ),
            strategy=strategy,
            scope_instrument_ids=(INSTRUMENT_ID, other_instrument),
            candidates=[
                make_candidate(INSTRUMENT_ID),
                make_candidate(other_instrument),
            ],
            settlement_calendar=gateway,
            initial_cash="20000",
        )
        result = runner.run()

        # Both buys filled on D1 and both positions settled for D2.
        requested_calendars = {
            calendar_id for calendar_id, _ in gateway.resolved_requests
        }
        self.assertIn("XSHG", requested_calendars)
        self.assertIn("SSE_OTHER", requested_calendars)
        restored = [
            e
            for e in result.events
            if e.event_type == "settlement_restored"
        ]
        self.assertEqual(len(restored), 1)
        self.assertEqual(len(restored[0].payload["instrument_ids"]), 2)


class DividendIdempotencyTests(unittest.TestCase):
    def test_failed_account_precondition_does_not_consume_the_event_id(
        self,
    ) -> None:
        """A rejected attempt (missing currency) must leave the id free so
        a corrected retry still applies the dividend."""

        from decimal import Decimal
        from uuid import uuid4

        from app.backtesting.accounting import (
            AccountState,
            AccountingPolicy,
            PortfolioState,
        )
        from tests.backtest_runtime_fixture import session_open

        policy = AccountingPolicy(currency="CNY")
        # The account only carries USD: the CNY dividend cannot apply.
        portfolio = PortfolioState(
            account=AccountState(
                cash_balances={"USD": "0"},
                available_cash="0",
                frozen_cash="0",
                margin_used="0",
                margin_available="0",
                equity="0",
            ),
            as_of=session_open(D1),
        )
        event_id = uuid4()
        with self.assertRaises(Exception):
            policy.apply_cash_dividend(
                portfolio,
                dividend_event_id=event_id,
                instrument_id=INSTRUMENT_ID,
                effective_date=D1,
                amount_per_share=Decimal("2"),
            )

        # Fix the account, then retry with the same event id: the id was
        # never consumed by the failed attempt, and with no position the
        # corrected attempt consumes it without paying out.
        portfolio.account.cash_balances["CNY"] = Decimal("0")
        application = policy.apply_cash_dividend(
            portfolio,
            dividend_event_id=event_id,
            instrument_id=INSTRUMENT_ID,
            effective_date=D1,
            amount_per_share=Decimal("2"),
        )
        self.assertFalse(application.applied)
        # A later replay is still a no-op (no retroactive claim).
        replay = policy.apply_cash_dividend(
            portfolio,
            dividend_event_id=event_id,
            instrument_id=INSTRUMENT_ID,
            effective_date=D1,
            amount_per_share=Decimal("2"),
        )
        self.assertFalse(replay.applied)

    def test_same_day_distinct_dividend_events_stay_separate(self) -> None:
        """Two corporate actions on the same instrument and day must never
        collapse into one idempotency key."""

        axis = build_axis([D0, D1, D2])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "102.00")},
                D2: {INSTRUMENT_ID: ("101.00", "103.00")},
            }
        )
        strategy = ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}})
        runner = build_runner(
            run_id="run-two-dividends",
            axis=axis,
            market_data=market_data,
            strategy_view=CountingStrategyView(
                {D0: "100.00", D1: "102.00", D2: "103.00"}
            ),
            strategy=strategy,
            corporate_actions=StaticCorporateActions(
                {
                    D1: [
                        ("div-2026-D1-first", INSTRUMENT_ID, "1"),
                        ("div-2026-D1-second", INSTRUMENT_ID, "3"),
                    ]
                }
            ),
        )
        result = runner.run()

        dividend_events = [
            e
            for e in result.events
            if e.event_type == "cash_dividend_applied"
        ]
        self.assertEqual(len(dividend_events), 2)
        self.assertNotEqual(
            dividend_events[0].payload["dividend_event_id"],
            dividend_events[1].payload["dividend_event_id"],
        )
        deltas = sorted(
            event.payload["cash_delta"] for event in dividend_events
        )
        self.assertEqual(deltas, [Decimal("100"), Decimal("300")])
        # D1 close equity = 100 x 102 + (100 + 300) = 10,600.
        self.assertEqual(result.equity_curve[1].equity, Decimal("10600"))

    def test_replaying_a_dividend_event_never_pays_twice(self) -> None:
        from decimal import Decimal

        runner = document_scenario_runner(run_id="run-div-idem")
        result = runner.run()

        dividend_events = [
            e
            for e in result.events
            if e.event_type == "cash_dividend_applied"
        ]
        self.assertEqual(len(dividend_events), 1)
        event = dividend_events[0]
        cash_before = runner._portfolio.account.cash_balances["CNY"]

        # Replay the exact same event id through the accounting boundary.
        from app.backtesting.dividends import CashDividendEvent

        replay_event = CashDividendEvent(
            event_id=UUID(event.payload["dividend_event_id"]),
            instrument_id=INSTRUMENT_ID,
            ex_date=D1,
            record_date=D1,
            source_payment_date=D1,
            source_arrival_date=D1,
            cash_effective_session_id=D1,
            amount_per_share=Decimal("2"),
            entitlement_quantity=Decimal("100"),
            source_evidence={"source": "fixture", "key": "replay"},
            as_of=D1,
        )
        application = runner._accounting.apply_cash_dividend_event(
            runner._portfolio,
            replay_event,
            session_date=D1,
        )
        self.assertFalse(application.applied)
        self.assertEqual(
            runner._portfolio.account.cash_balances["CNY"], cash_before
        )


class SellAfterSettlementRestoreTests(unittest.TestCase):
    def test_sell_submitted_after_restore_fills_next_open(self) -> None:
        """The flatten sell is submitted while availability is zero and
        fills at the next session's open after the T+1 restore."""

        axis = build_axis([D0, D1, D2, D3])
        market_data = DictMarketData(
            {
                D0: {INSTRUMENT_ID: ("99.00", "100.00")},
                D1: {INSTRUMENT_ID: ("100.00", "101.00")},
                D2: {INSTRUMENT_ID: ("101.00", "102.00")},
                D3: {INSTRUMENT_ID: ("102.00", "103.00")},
            }
        )
        view = CountingStrategyView(
            {D0: "100.00", D1: "101.00", D2: "102.00", D3: "103.00"}
        )
        # Step0 buys everything; steps 1-2 keep flattening; step 3 is final.
        strategy = ScriptedStrategy(
            {
                0: {str(INSTRUMENT_ID): "1"},
                1: {},
                2: {},
            }
        )
        runner = build_runner(
            run_id="run-sell",
            axis=axis,
            market_data=market_data,
            strategy_view=view,
            strategy=strategy,
        )
        result = runner.run()

        by_step: dict[int, list[str]] = {}
        for event in result.events:
            by_step.setdefault(event.step_sequence, []).append(event.event_type)
        # D1 close: the flatten decision submits the full sell delta even
        # though availability is still zero (T+1 not restored yet).
        self.assertIn("order_submitted", by_step[1])
        # D2: availability is restored before the open match, the sell
        # fills at the D2 open (101), and the close then values the
        # all-cash account.
        self.assertEqual(by_step[2][0], "settlement_restored")
        self.assertIn("fill_created", by_step[2])
        self.assertLess(
            by_step[2].index("fill_created"), by_step[2].index("portfolio_valued")
        )
        # D3: nothing left to trade; only the final valuation remains.
        self.assertEqual(by_step[3], ["portfolio_valued"])
        # Final equity is pure cash: the sell filled at the D2 open (101).
        self.assertEqual(result.equity_curve[3].equity, Decimal("10100"))


class IdempotencyTests(unittest.TestCase):
    def test_replaying_a_fill_produces_no_duplicate_application(self) -> None:
        runner = document_scenario_runner(run_id="run-idem")
        # Reach inside the fixture decorator to recover the produced fills.
        result = runner.run()
        fills = runner._execution_model.recorded_fills
        self.assertEqual(len(fills), 2)

        accounting = runner._accounting
        for fill in fills:
            cash_before = runner._portfolio.account.cash_balances["CNY"]
            application = accounting.apply_fill(runner._portfolio, fill)
            self.assertFalse(application.applied)
            self.assertEqual(
                runner._portfolio.account.cash_balances["CNY"], cash_before
            )
        # Exactly one applied fact exists on the stream for every fill.
        applied_ids = {
            e.payload["fill_id"]
            for e in result.events
            if e.event_type == "fill_applied"
        }
        self.assertEqual(applied_ids, {str(fill.fill_id) for fill in fills})

    def test_repeated_runs_do_not_double_apply_fills(self) -> None:
        first = document_scenario_runner(run_id="same")
        second = document_scenario_runner(run_id="same")

        first_result = first.run()
        second_result = second.run()
        self.assertEqual(first_result.events, second_result.events)
        self.assertEqual(first_result.final_snapshot, second_result.final_snapshot)


if __name__ == "__main__":
    unittest.main()
