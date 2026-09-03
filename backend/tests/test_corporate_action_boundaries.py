"""Focused regression tests for company-action boundary failures."""

import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.backtesting.data.adapters.etf import EtfFactsAdapter
from app.backtesting.data.corporate_actions import RunCorporateActionEventSnapshot
from app.backtesting.data.errors import ProviderContractViolationError
from app.backtesting.data.facts import CorporateAction, FactEvidence
from app.backtesting.data.requests import (
    CorporateActionQuery,
    DateRange,
    QueryBoundary,
    QualityStatus,
    ConsistencyValidation,
)
from app.backtesting.dividends import CashDividendEvent
from app.backtesting.production_runtime import _corporate_action_snapshot
from app.data_ingestion.services.corporate_action import sync_fund_div
from tests.backtest_runtime_fixture import (
    CountingStrategyView,
    DictMarketData,
    INSTRUMENT_ID as RUNTIME_INSTRUMENT_ID,
    ScriptedStrategy,
    build_axis,
    build_runner,
)


INSTRUMENT_ID = uuid4()
START = date(2026, 8, 3)
END = date(2026, 8, 7)
CUTOFF = datetime(2026, 8, 8, tzinfo=UTC)


class _SourceClient:
    def __init__(self, rows):
        self.rows = rows

    def fund_div(self, **_kwargs):
        return list(self.rows)


class _Session:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)


class _CorporateActionRepository:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def list_facts(self, *_args, **_kwargs):
        return self.rows


def _query():
    return CorporateActionQuery(
        instrument_ids=(INSTRUMENT_ID,),
        window=DateRange(START, END),
        boundary=QueryBoundary(CUTOFF, include_cutoff_day=True),
    )


def _adapter(rows):
    return EtfFactsAdapter(
        code_mappings=lambda *_args, **_kwargs: (),
        daily_bars=lambda *_args, **_kwargs: (),
        adjustment_factors=lambda *_args, **_kwargs: (),
        trading_days=lambda *_args, **_kwargs: (),
        corporate_action_repository=_CorporateActionRepository(rows),
    )


class CorporateActionIngestionTests(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "ts_code": "510300.SH",
            "ann_date": "2026-08-01",
            "record_date": "2026-08-03",
            "ex_date": "2026-08-03",
            "pay_date": "2026-08-04",
            "earpay_date": "2026-08-05",
            "div_proc": "实施",
            "div_cash": "0.10",
            "currency": "CNY",
        }
        row.update(overrides)
        return row

    def _resolver(self, _item, _instrument_id):
        definition = SimpleNamespace(definition_version="calendar@1")
        return {
            "calendar_id": "XSHG",
            "timezone": "Asia/Shanghai",
            "calendar_definition": definition,
            "next_open_session": lambda _calendar_id, after_session: date(2026, 8, 5),
        }

    def test_persists_calendar_derived_effective_date_and_evidence(self):
        session = _Session()
        result = sync_fund_div(
            _SourceClient([self._row()]),
            session=session,
            instrument_map={"510300.SH": INSTRUMENT_ID},
            calendar_resolver=self._resolver,
        )

        facts = [item for item in session.added if item.__class__.__name__ == "CorporateActionFact"]
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cash_effective_date, date(2026, 8, 5))
        self.assertEqual(facts[0].cash_date_rule, "tushare_fund_div_cash_date@1")
        self.assertEqual(facts[0].timing_rule, "after_open_match@1")
        self.assertEqual(facts[0].entitlement_rule, "record_date_entitlement")
        self.assertEqual(facts[0].evidence["calendar_id"], "XSHG")
        self.assertEqual(facts[0].evidence["calendar_definition"], "calendar@1")

    def test_missing_source_dates_is_a_failure_and_does_not_advance_checkpoint(self):
        session = _Session()
        checkpoint = SimpleNamespace(cursor={"synced_through_date": "2026-08-02"})
        checkpoint_repo = SimpleNamespace(
            advance=lambda **_kwargs: self.fail("invalid company action advanced checkpoint")
        )
        result = sync_fund_div(
            _SourceClient([self._row(pay_date=None, earpay_date=None)]),
            session=session,
            instrument_map={"510300.SH": INSTRUMENT_ID},
            calendar_resolver=self._resolver,
            checkpoint_repo=checkpoint_repo,
            sync_key="fund-div",
            checkpoint_version=1,
        )

        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["checkpoint_advanced"])
        self.assertEqual(
            [item.__class__.__name__ for item in session.added],
            ["CorporateActionSourceFact"],
        )
        self.assertEqual(checkpoint.cursor["synced_through_date"], "2026-08-02")


class _RuntimeCorporateActions:
    def __init__(self, event):
        self.event = event

    def cash_dividend_events(self):
        return (self.event,)


class _OrderUpdateStrategy(ScriptedStrategy):
    def __init__(self):
        super().__init__({0: {str(RUNTIME_INSTRUMENT_ID): "1"}})
        self.order_updates = []

    def on_order_update(self, update):
        self.order_updates.append(update)


class CorporateActionProviderTests(unittest.TestCase):
    def test_production_snapshot_loads_fixed_scope_actions(self):
        event_id = uuid4()
        row = CorporateAction(
            instrument_id=INSTRUMENT_ID,
            action_type="cash_dividend",
            ex_date=START,
            evidence=FactEvidence(
                source="tushare",
                observed_at=CUTOFF,
                quality_status=QualityStatus.COMPLETE,
                source_revision="1",
            ),
            attributes={
                "event_id": str(event_id),
                "record_date": START.isoformat(),
                "source_payment_date": "2026-08-04",
                "source_arrival_date": "2026-08-05",
                "cash_effective_date": END.isoformat(),
                "cash_amount_per_unit": "0.10",
                "currency": "CNY",
                "cash_effective_phase": "after_open_match",
                "entitlement_rule": "record_date_entitlement",
                "calendar_id": "XSHG",
                "timezone": "Asia/Shanghai",
                "cash_date_rule": "tushare_fund_div_cash_date@1",
                "timing_rule": "after_open_match@1",
            },
        )
        request = SimpleNamespace(
            fixed_instrument_ids=(INSTRUMENT_ID,),
            requested_window=DateRange(START, END),
            query_boundary=QueryBoundary(CUTOFF, include_cutoff_day=True),
            data_chunk_size_sessions=20,
        )

        class _Chunk:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def validate_consistency(self):
                return SimpleNamespace(status=ConsistencyValidation.VALID)

            def corporate_actions(self, _query):
                return (row,)

        data_session = SimpleNamespace(
            resolved_sessions=(SimpleNamespace(session_id="s1", session_date=START),),
            open_chunk=lambda _query: _Chunk(),
        )

        snapshot = _corporate_action_snapshot(data_session, request)

        self.assertEqual(snapshot.cash_dividend_events[0].event_id, event_id)
        self.assertEqual(snapshot.coverage_summary["event_count"], 1)

    def test_dividend_credit_does_not_call_order_hook_from_cash_phase(self):
        day = date(2026, 8, 4)
        strategy = _OrderUpdateStrategy()
        event = CashDividendEvent(
            event_id=uuid4(),
            instrument_id=RUNTIME_INSTRUMENT_ID,
            ex_date=day,
            record_date=day,
            source_payment_date=day,
            source_arrival_date=day,
            cash_effective_session_id=day,
            amount_per_share="0.10",
            source_evidence={"source": "test"},
            as_of=day,
        )
        final_day = date(2026, 8, 5)
        runner = build_runner(
            run_id="corporate-action-hook-regression",
            axis=build_axis([date(2026, 8, 3), day, final_day]),
            market_data=DictMarketData(
                {
                    date(2026, 8, 3): {RUNTIME_INSTRUMENT_ID: ("99", "100")},
                    day: {RUNTIME_INSTRUMENT_ID: ("100", "100")},
                    final_day: {RUNTIME_INSTRUMENT_ID: ("100", "100")},
                }
            ),
            strategy_view=CountingStrategyView({
                date(2026, 8, 3): "100",
                day: "100",
                final_day: "100",
            }),
            strategy=strategy,
            corporate_actions=_RuntimeCorporateActions(event),
        )

        result = runner.run()

        self.assertTrue(any(item.event_type == "cash_dividend_applied" for item in result.events))
        self.assertGreaterEqual(len(strategy.order_updates), 1)

    def test_missing_one_source_date_never_uses_cash_effective_date(self):
        row = CorporateAction(
            instrument_id=INSTRUMENT_ID,
            action_type="cash_dividend",
            ex_date=START,
            evidence=FactEvidence(
                source="tushare",
                observed_at=CUTOFF,
                quality_status=QualityStatus.COMPLETE,
                source_revision="1",
            ),
            attributes={
                "event_id": str(uuid4()),
                "record_date": START.isoformat(),
                "source_payment_date": None,
                "source_arrival_date": "2026-08-05",
                "cash_effective_date": END.isoformat(),
                "cash_amount_per_unit": "0.10",
                "currency": "CNY",
                "cash_effective_phase": "after_open_match",
                "entitlement_rule": "record_date_entitlement",
                "calendar_id": "XSHG",
                "timezone": "Asia/Shanghai",
                "cash_date_rule": "tushare_fund_div_cash_date@1",
                "timing_rule": "after_open_match@1",
            },
        )

        event = RunCorporateActionEventSnapshot._event_from_provider_fact(row)

        self.assertEqual(event.source_payment_date, date(2026, 8, 5))
        self.assertEqual(event.source_arrival_date, date(2026, 8, 5))
        self.assertNotEqual(event.source_payment_date, event.cash_effective_session_id)

    def test_provider_projection_preserves_event_id(self):
        event_id = uuid4()
        row = CorporateAction(
            instrument_id=INSTRUMENT_ID,
            action_type="cash_dividend",
            ex_date=START,
            evidence=FactEvidence(
                source="tushare",
                observed_at=CUTOFF,
                quality_status=QualityStatus.COMPLETE,
                source_revision="1",
            ),
            attributes={
                "event_id": str(event_id),
                "record_date": START.isoformat(),
                "source_payment_date": "2026-08-04",
                "source_arrival_date": "2026-08-05",
                "cash_effective_date": END.isoformat(),
                "cash_amount_per_unit": "0.10",
                "currency": "CNY",
                "cash_effective_phase": "after_open_match",
                "entitlement_rule": "record_date_entitlement",
                "calendar_id": "XSHG",
                "timezone": "Asia/Shanghai",
                "cash_date_rule": "tushare_fund_div_cash_date@1",
                "timing_rule": "after_open_match@1",
            },
        )

        event = RunCorporateActionEventSnapshot._event_from_provider_fact(row)

        self.assertEqual(event.event_id, event_id)
        self.assertEqual(event.source_payment_date, date(2026, 8, 4))
        self.assertEqual(event.source_arrival_date, date(2026, 8, 5))

    def test_quantity_action_is_not_silently_dropped(self):
        row = SimpleNamespace(action_type="split", event_id=uuid4())
        with self.assertRaises(ProviderContractViolationError) as context:
            _adapter([row]).corporate_actions(_query())
        self.assertEqual(
            context.exception.details["reason_code"],
            "quantity_corporate_action_unsupported",
        )

    def test_adapter_blocks_cash_action_without_both_source_dates(self):
        row = SimpleNamespace(
            action_type="cash_dividend",
            event_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            ex_date=START,
            record_date=START,
            source_payment_date=None,
            source_arrival_date=None,
            cash_effective_date=END,
            cash_amount_per_unit="0.10",
            currency="CNY",
            cash_effective_phase="after_open_match",
            entitlement_rule="record_date_entitlement",
            cash_date_rule="tushare_fund_div_cash_date@1",
            timing_rule="after_open_match@1",
            source="tushare",
            fact_version=1,
            quality="complete",
            created_at=CUTOFF,
            evidence={
                "calendar_id": "XSHG",
                "timezone": "Asia/Shanghai",
                "cash_date_rule": "tushare_fund_div_cash_date@1",
                "timing_rule": "after_open_match@1",
            },
        )
        with self.assertRaises(ProviderContractViolationError) as context:
            _adapter([row]).corporate_actions(_query())
        self.assertEqual(
            context.exception.details["reason_code"],
            "corporate_action_cash_date_unresolved",
        )

    def test_cash_action_without_both_source_dates_is_not_silently_dropped(self):
        row = CorporateAction(
            instrument_id=INSTRUMENT_ID,
            action_type="cash_dividend",
            ex_date=START,
            evidence=FactEvidence(
                source="tushare",
                observed_at=CUTOFF,
                quality_status=QualityStatus.COMPLETE,
                source_revision="1",
            ),
            attributes={
                "event_id": str(uuid4()),
                "record_date": START.isoformat(),
                "source_payment_date": None,
                "source_arrival_date": None,
                "cash_effective_date": END.isoformat(),
                "cash_amount_per_unit": "0.10",
                "currency": "CNY",
                "cash_effective_phase": "after_open_match",
                "entitlement_rule": "record_date_entitlement",
                "cash_date_rule": "tushare_fund_div_cash_date@1",
                "timing_rule": "after_open_match@1",
                "calendar_id": "XSHG",
                "timezone": "Asia/Shanghai",
            },
        )
        with self.assertRaises(ProviderContractViolationError) as context:
            RunCorporateActionEventSnapshot._event_from_provider_fact(row)
        self.assertEqual(
            context.exception.details["reason_code"],
            "corporate_action_cash_date_unresolved",
        )


if __name__ == "__main__":
    unittest.main()
