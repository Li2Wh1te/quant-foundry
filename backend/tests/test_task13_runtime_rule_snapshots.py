"""Acceptance checks for T13-10/T13-12 snapshot persistence and runtime use."""

import importlib
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import VersionedReference
from app.instruments.rule_snapshots import (
    FactProvenance,
    InstrumentRuleSnapshotSegment,
    RunRuleSnapshotBundle,
)
from app.instruments.rule_snapshots_models import (
    BacktestRunInstrumentRuleSnapshotRecord,
    BacktestRunRuleSnapshotRecord,
)
from app.instruments.rule_snapshots_repository import RunRuleSnapshotRepository
from tests.backtest_runtime_fixture import (
    CountingStrategyView,
    DictMarketData,
    INSTRUMENT_ID,
    ScriptedStrategy,
    build_axis,
    build_runner,
)


PACKAGE = VersionedReference("china_listed_etf_rules", 1)
RUN_ID = uuid4()


def _values(
    *,
    lot_size: str = "200",
    price_tick: str = "0.05",
    contract_multiplier: str = "1",
) -> dict:
    """Return the complete execution projection used by runtime admission."""

    return {
        "lot_size": Decimal(lot_size),
        "quantity_precision": 0,
        "price_precision": 2,
        "price_tick": Decimal(price_tick),
        "contract_multiplier": Decimal(contract_multiplier),
        "trading_session_template": VersionedReference("cn_etf_session", 1),
        "settlement_rule_class": "t_plus_1_before_open_match",
        "sellable_rule": VersionedReference("sell_rule", 1),
        "fee_categories": ("none",),
        "trading_status_applicability": {
            "suspension": "not_applicable",
            "opening_availability": "not_applicable",
            "price_limit_tradability": "not_applicable",
        },
        "currency": "CNY",
        "order_types": ("market",),
        "minimum_order_quantity": Decimal(lot_size),
        "price_limit_rule": VersionedReference("price_limit_rule", 1),
        "cash_availability_rule": VersionedReference("cash_rule", 1),
        "position_availability_rule": VersionedReference("position_rule", 1),
    }


def _segment(
    instrument_id: UUID = INSTRUMENT_ID,
    *,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
    lot_size: str = "200",
    contract_multiplier: str = "1",
) -> InstrumentRuleSnapshotSegment:
    observed_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    provenance = {
        "normal_fact": FactProvenance(
            fact_reference=VersionedReference("etf_rule_fact", 3),
            source="exchange_rule_book",
            source_revision="2026-edition",
            valid_from=date(2024, 1, 1),
            valid_to=None,
            known_at=observed_at,
            observed_at=observed_at,
            quality_status="complete",
            fixture_only=False,
            content_hash="f" * 64,
        ).to_payload()
    }
    return InstrumentRuleSnapshotSegment(
        instrument_id=instrument_id,
        effective_from=effective_from,
        effective_to=effective_to,
        normal_fact_reference=VersionedReference("etf_rule_fact", 3),
        exception_fact_reference=None,
        normalized_values=_values(
            lot_size=lot_size,
            contract_multiplier=contract_multiplier,
        ),
        capability_declarations={
            "suspension": "not_applicable",
            "opening_availability": "not_applicable",
            "price_limit_tradability": "not_applicable",
        },
        provenance=provenance,
        resolution_hash="r" * 64,
    )


def _bundle(*, segments=None, run_id: UUID | None = RUN_ID) -> RunRuleSnapshotBundle:
    return RunRuleSnapshotBundle(
        run_id=run_id,
        rule_package_reference=PACKAGE,
        rule_package_semantic_hash="p" * 64,
        parser_revision="rule-package-resolver@2",
        exception_set_reference=None,
        exception_set_hash=None,
        data_cutoff=datetime(2026, 8, 1, tzinfo=UTC),
        instrument_segments=tuple(segments or (_segment(),)),
    )


class SnapshotDomainAcceptanceTests(unittest.TestCase):
    def test_provenance_retains_observed_at_and_content_hash(self) -> None:
        payload = _segment().provenance["normal_fact"]
        self.assertEqual(payload["observed_at"], "2026-08-01T12:00:00+00:00")
        self.assertEqual(payload["content_hash"], "f" * 64)

    def test_provenance_rejects_malformed_content_hash(self) -> None:
        with self.assertRaises(DomainValidationError):
            FactProvenance(
                fact_reference=VersionedReference("etf_rule_fact", 3),
                source="exchange_rule_book",
                source_revision="2026-edition",
                valid_from=date(2024, 1, 1),
                valid_to=None,
                known_at=datetime(2026, 8, 1, tzinfo=UTC),
                observed_at=datetime(2026, 8, 1, tzinfo=UTC),
                quality_status="complete",
                fixture_only=False,
                content_hash="not-a-hash",
            )

    def test_segment_lookup_is_date_deterministic(self) -> None:
        later = _segment(effective_from=date(2026, 6, 1), lot_size="300")
        bundle = _bundle(segments=(_segment(effective_to=date(2026, 6, 1)), later))
        selected = bundle.segment_for(INSTRUMENT_ID, date(2026, 6, 1))
        self.assertEqual(selected.normalized_values["lot_size"], Decimal("300"))
        with self.assertRaises(DomainValidationError):
            bundle.segment_for(uuid4(), date(2026, 6, 1))
        with self.assertRaises(DomainValidationError):
            bundle.segment_for(INSTRUMENT_ID, datetime(2026, 6, 1))

    def test_tampered_hash_is_rejected_before_runtime_use(self) -> None:
        bundle = _bundle()
        object.__setattr__(bundle, "snapshot_hash", "0" * 64)
        with self.assertRaises(DomainValidationError):
            bundle.verify_hash()

    def test_snapshot_timestamps_are_canonicalized_to_utc(self) -> None:
        local = datetime(2026, 8, 1, 20, tzinfo=timezone(timedelta(hours=8)))
        bundle = RunRuleSnapshotBundle(
            rule_package_reference=PACKAGE,
            rule_package_semantic_hash="p" * 64,
            parser_revision="rule-package-resolver@2",
            exception_set_reference=None,
            exception_set_hash=None,
            data_cutoff=local,
            instrument_segments=(_segment(),),
        )
        self.assertEqual(bundle.data_cutoff, datetime(2026, 8, 1, 12, tzinfo=UTC))


class SnapshotSQLiteAcceptanceTests(unittest.TestCase):
    def test_write_load_round_trip_preserves_hash_and_timezone(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        BaseTables = [
            BacktestRunRuleSnapshotRecord.__table__,
            BacktestRunInstrumentRuleSnapshotRecord.__table__,
        ]
        from app.db.base import Base

        Base.metadata.create_all(engine, tables=BaseTables)
        bundle = _bundle()
        with Session(engine) as session:
            repository = RunRuleSnapshotRepository(session)
            self.assertEqual(repository.write_bundle(bundle), bundle.snapshot_hash)
            session.commit()
            loaded = repository.load_bundle(bundle.run_id)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.snapshot_hash, bundle.snapshot_hash)
            self.assertEqual(loaded.data_cutoff.tzinfo, UTC)
            self.assertEqual(
                repository.snapshot_hash_for(bundle.run_id), bundle.snapshot_hash
            )

    def test_rule_snapshot_migration_upgrades_and_downgrades_on_sqlite(self) -> None:
        migration = importlib.import_module(
            "app.db.migrations.versions.20260822_04_add_instrument_rule_facts_and_snapshots"
        )
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
            names = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).scalars()
            )
            self.assertIn("backtest_run_rule_snapshots", names)
            self.assertIn("backtest_run_instrument_rule_snapshots", names)
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            remaining = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).scalars()
            )
            self.assertNotIn("backtest_run_rule_snapshots", remaining)


class RuntimeSnapshotAcceptanceTests(unittest.TestCase):
    def test_runtime_rejects_tampered_snapshot_at_admission(self) -> None:
        bundle = _bundle(run_id=None)
        object.__setattr__(bundle, "snapshot_hash", "0" * 64)
        day = date(2026, 8, 3)
        with self.assertRaises(DomainValidationError):
            build_runner(
                run_id="runtime-tampered",
                axis=build_axis([day]),
                market_data=DictMarketData({day: {INSTRUMENT_ID: ("100", "100")}}),
                strategy_view=CountingStrategyView({day: "100"}),
                strategy=ScriptedStrategy({}),
                rule_snapshot_bundle=bundle,
            )

    def test_runtime_uses_frozen_lot_and_returns_snapshot_identity(self) -> None:
        days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]
        runner = build_runner(
            run_id="runtime-snapshot",
            axis=build_axis(days),
            market_data=DictMarketData(
                {day: {INSTRUMENT_ID: ("100.02", "100.02")} for day in days}
            ),
            strategy_view=CountingStrategyView(
                {day: "100.00" for day in days}
            ),
            strategy=ScriptedStrategy(
                {
                    0: {str(INSTRUMENT_ID): "1"},
                    1: {str(INSTRUMENT_ID): "1"},
                }
            ),
            initial_cash="50000",
            rule_snapshot_bundle=_bundle(run_id=None),
        )
        policy = runner.execution_policy_for(INSTRUMENT_ID, days[0])
        self.assertEqual(policy.lot_size, Decimal("200"))
        result = runner.run()
        self.assertEqual(result.rule_snapshot_hash, runner.rule_snapshot_hash)
        used_segments = result.components["rule_snapshot"]["used_segments"]
        self.assertTrue(used_segments)
        self.assertEqual(used_segments[0]["resolution_hash"], "r" * 64)
        submitted = [
            event for event in result.events if event.event_type == "order_submitted"
        ]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].payload["quantity"], Decimal("400"))
        fills = [event for event in result.events if event.event_type == "fill_created"]
        self.assertEqual(fills[0].payload["execution_price"], Decimal("100.05"))

    def test_formal_runtime_executes_the_selected_long_only_interpreter(self) -> None:
        days = [date(2026, 8, 3), date(2026, 8, 4)]
        runner = build_runner(
            run_id="runtime-selected-interpreter",
            axis=build_axis(days),
            market_data=DictMarketData(
                {day: {INSTRUMENT_ID: ("100.00", "100.00")} for day in days}
            ),
            strategy_view=CountingStrategyView(
                {day: "100.00" for day in days}
            ),
            # The legacy interpreter would size this leveraged target. The
            # selected v1 formal interpreter must reject it as a whole.
            strategy=ScriptedStrategy({0: {str(INSTRUMENT_ID): "2"}}),
            initial_cash="50000",
            rule_snapshot_bundle=_bundle(run_id=None),
        )

        result = runner.run()

        self.assertEqual(
            result.components["decision_interpreter"]["key"],
            "long_only_target_weights",
        )
        self.assertEqual(result.order_outcomes, ())
        self.assertFalse(
            any(event.event_type == "order_submitted" for event in result.events)
        )

    def test_formal_runtime_uses_multiplier_for_sizing_matching_and_valuation(self) -> None:
        days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]
        runner = build_runner(
            run_id="runtime-multiplier",
            axis=build_axis(days),
            market_data=DictMarketData(
                {day: {INSTRUMENT_ID: ("10.00", "10.00")} for day in days}
            ),
            strategy_view=CountingStrategyView({day: "10.00" for day in days}),
            strategy=ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}}),
            initial_cash="20000",
            rule_snapshot_bundle=_bundle(
                run_id=None,
                segments=(_segment(contract_multiplier="10", lot_size="100"),)
            ),
        )

        result = runner.run()
        submitted = [
            event for event in result.events
            if event.event_type == "order_submitted"
        ]
        fills = [
            event for event in result.events
            if event.event_type == "fill_created"
        ]

        # 20,000 / (10 x 10) = 200 units; a legacy price-only sizing path
        # would incorrectly submit 2,000 units.
        self.assertEqual(submitted[0].payload["quantity"], Decimal("200"))
        self.assertEqual(fills[0].payload["contract_multiplier"], Decimal("10"))
        self.assertEqual(fills[0].payload["gross_notional"], Decimal("20000"))
        self.assertEqual(result.equity_curve[1].equity, Decimal("20000"))

    def test_formal_runtime_uses_rule_aware_partial_cash_matching(self) -> None:
        days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]
        runner = build_runner(
            run_id="runtime-cash-allocation",
            axis=build_axis(days),
            market_data=DictMarketData(
                {
                    days[0]: {INSTRUMENT_ID: ("1.00", "1.00")},
                    days[1]: {INSTRUMENT_ID: ("10.00", "10.00")},
                    days[2]: {INSTRUMENT_ID: ("10.00", "10.00")},
                }
            ),
            strategy_view=CountingStrategyView({day: "10.00" for day in days}),
            strategy=ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}}),
            initial_cash="1500",
            rule_snapshot_bundle=_bundle(
                run_id=None,
                segments=(_segment(contract_multiplier="1", lot_size="100"),),
            ),
        )

        result = runner.run()
        order = result.order_outcomes[0]

        # Sizing uses the D0 close (1.00), while matching uses the D1 open
        # (10.00). The formal matcher keeps the affordable lot instead of
        # expiring the full 1,500-share request.
        self.assertEqual(order.status, "partially_filled")
        self.assertEqual(order.filled_quantity, Decimal("100"))
        # The partial buy spends 1,000 and the following scripted flatten
        # sells it back for 1,000; no rule-induced cash drift remains.
        self.assertEqual(runner._portfolio.account.available_cash, Decimal("1500"))

    def test_formal_runtime_uses_multiplier_for_valuation(self) -> None:
        from app.backtesting.domain import PositionSide, PositionState

        days = [date(2026, 8, 3), date(2026, 8, 4)]
        runner = build_runner(
            run_id="runtime-valuation-multiplier",
            axis=build_axis(days),
            market_data=DictMarketData(
                {day: {INSTRUMENT_ID: ("10.00", "10.00")} for day in days}
            ),
            strategy_view=CountingStrategyView({day: "10.00" for day in days}),
            strategy=ScriptedStrategy({0: {str(INSTRUMENT_ID): "1"}}),
            initial_cash="0",
            rule_snapshot_bundle=_bundle(
                run_id=None,
                segments=(_segment(contract_multiplier="10", lot_size="1"),),
            ),
        )
        runner._portfolio.positions[INSTRUMENT_ID] = PositionState(
            instrument_id=INSTRUMENT_ID,
            side=PositionSide.LONG,
            quantity="100",
            available_quantity="100",
            average_price="9",
        )

        result = runner.run()

        self.assertEqual(result.equity_curve[0].equity, Decimal("10000"))
        self.assertEqual(
            runner._portfolio.positions[INSTRUMENT_ID].unrealized_pnl,
            Decimal("1000"),
        )

    def test_runtime_fails_closed_when_snapshot_has_no_segment(self) -> None:
        bundle = _bundle(segments=(_segment(uuid4()),), run_id=None)
        runner = build_runner(
            run_id="runtime-no-segment",
            axis=build_axis([date(2026, 8, 3)]),
            market_data=DictMarketData(
                {date(2026, 8, 3): {INSTRUMENT_ID: ("100.00", "100.00")}}
            ),
            strategy_view=CountingStrategyView({date(2026, 8, 3): "100.00"}),
            strategy=ScriptedStrategy({}),
            rule_snapshot_bundle=bundle,
        )
        with self.assertRaises(DomainValidationError):
            runner.execution_policy_for(INSTRUMENT_ID, date(2026, 8, 3))


if __name__ == "__main__":
    unittest.main()
