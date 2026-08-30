"""Minimal task-10 acceptance tests for stable identity and PIT history."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
import threading
import unittest
from uuid import UUID, uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    MetaData,
    String,
    Table,
    Uuid,
    create_engine,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.backtesting.data.errors import (
    HistoryBarInstrumentMismatchError,
    HistoryBarsDuplicateError,
    HistoryBarsIncompleteError,
    IdentityMappingConflictError,
    IdentityMappingEvidenceMissingError,
    IdentityMappingIncompleteError,
    LookbackSessionsLimitExceededError,
)
from app.backtesting.data.facts import Bar, FactEvidence
from app.backtesting.domain import DomainValidationError
from app.backtesting.data.pit_history import (
    read_segmented_history,
    resolve_pit_mappings,
)
from app.backtesting.data.requests import PriceBasis, QualityStatus
from app.backtesting.result_models import resolve_display_snapshot
from app.data_ingestion.models.etf import EtfEntity
from app.db.base import Base
from app.core.config import get_settings
from app.instruments.identity_repository import (
    IdentityFactConflictError,
    IdentityMergeEvidenceMissingError,
    InstrumentDisplayFactRepository,
    InstrumentIdentityService,
    migrate_existing_etf_identities,
    resolve_instrument_identity,
)
from app.instruments.domain import (
    AuthorityStatus,
    InstrumentCodeMapping,
    InstrumentDisplay,
    InstrumentDisplayFact,
    InstrumentIdentityFact,
)
from app.instruments.models import (
    DisplayResolutionHead,
    Instrument,
    InstrumentCodeMappingRecord,
    InstrumentDisplayFactRecord,
    InstrumentIdentityFactRecord,
    InstrumentIdentityMergeAuditRecord,
    MappingResolutionHead,
)
from app.instruments.repository import InstrumentCodeMappingRepository
from app.strategy_protocol.contract import LookbackLimitExceededError
from app.strategy_protocol.data_view import BarDTO, StrategyDataDTO


INSTRUMENT_ID = uuid4()
SOURCE = "tushare"
CUTOFF = datetime(2026, 8, 22, 15, tzinfo=UTC)


def _mapping(
    source_code: str,
    valid_from: date,
    valid_to: date | None = None,
    *,
    known_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    logical_fact_key: str | None = None,
    fact_id=None,
    fact_version: int = 1,
    supersedes_fact_id=None,
    evidence: str = "announcement://task-10/identity-1",
) -> InstrumentCodeMapping:
    return InstrumentCodeMapping(
        instrument_id=INSTRUMENT_ID,
        source=SOURCE,
        source_code=source_code,
        trading_code=source_code.split(".")[0],
        valid_from=valid_from,
        valid_to=valid_to,
        mapping_source="exchange_announcement",
        evidence=evidence,
        known_at=known_at,
        observed_at=known_at,
        logical_fact_key=logical_fact_key,
        fact_id=fact_id,
        fact_version=fact_version,
        supersedes_fact_id=supersedes_fact_id,
    )


def _bar(day: date, *, instrument_id=INSTRUMENT_ID) -> Bar:
    return Bar(
        instrument_id=instrument_id,
        trade_date=day,
        frequency="1d",
        open=Decimal("1"),
        high=Decimal("1.1"),
        low=Decimal("0.9"),
        close=Decimal("1.05"),
        volume=Decimal("100"),
        amount=Decimal("105"),
        price_basis=PriceBasis.RAW,
        evidence=FactEvidence(
            source=SOURCE,
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            quality_status=QualityStatus.COMPLETE,
        ),
    )


class _BarReader:
    def __init__(self, rows):
        self.rows = {code: list(values) for code, values in rows.items()}
        self.calls = []

    def read_bars(self, source_code, start_date, end_date):
        self.calls.append((source_code, start_date, end_date))
        return tuple(
            row
            for row in self.rows.get(source_code, ())
            if start_date <= row.trade_date <= end_date
        )


class StableIdentityAcceptanceTests(unittest.TestCase):
    def test_display_resolution_rejects_invalid_time_coordinates(self):
        repository = InstrumentDisplayFactRepository(object())
        for effective_at, data_cutoff in (
            (datetime(2026, 1, 5), datetime(2026, 1, 6, tzinfo=UTC)),
            (datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 1, 6)),
            ("2026-01-05T00:00:00Z", datetime(2026, 1, 6, tzinfo=UTC)),
            (datetime(2026, 1, 5, tzinfo=UTC), "2026-01-06T00:00:00Z"),
        ):
            with self.subTest(effective_at=effective_at, data_cutoff=data_cutoff):
                with self.assertRaises(DomainValidationError):
                    repository.resolve_display(
                        INSTRUMENT_ID,
                        effective_at=effective_at,
                        data_cutoff=data_cutoff,
                    )

    def test_fact_validation_and_nullable_display_fields(self):
        with self.assertRaises(DomainValidationError):
            _mapping("A.SH", date(2026, 1, 1), evidence="")

        fact = InstrumentDisplayFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=1,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            source="registry",
            evidence="review://display-1",
            trading_code=" ",
            name=None,
            display_name=" ",
        )
        self.assertIsNone(fact.trading_code)
        self.assertIsNone(fact.display_name)

    def test_unreviewed_display_revision_does_not_hide_authoritative_fact(self):
        logical_key = f"display:{INSTRUMENT_ID}:review-regression"
        first_id = uuid4()
        first = InstrumentDisplayFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=1,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            source="registry",
            evidence="review://display-authoritative",
            trading_code="OLD",
            display_name="旧名称",
            fact_id=first_id,
            logical_fact_key=logical_key,
            authority_rank=1,
            authority_status=AuthorityStatus.AUTHORITATIVE,
        )
        pending = InstrumentDisplayFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=2,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 3, tzinfo=UTC),
            observed_at=datetime(2026, 1, 3, tzinfo=UTC),
            source="registry",
            evidence="review://display-pending",
            trading_code="NEW",
            display_name="待审名称",
            fact_id=uuid4(),
            logical_fact_key=logical_key,
            supersedes_fact_id=first_id,
            authority_rank=1,
            authority_status=AuthorityStatus.PENDING,
        )
        resolution = resolve_instrument_identity(
            INSTRUMENT_ID,
            effective_at=datetime(2026, 1, 5, tzinfo=UTC),
            data_cutoff=datetime(2026, 1, 4, tzinfo=UTC),
            display_facts=(first, pending),
        )
        self.assertIsNotNone(resolution)
        self.assertEqual(resolution.display_name, "旧名称")
        self.assertEqual(resolution.trading_code, "OLD")

    def test_pure_resolution_evidence_summary_retains_fact_provenance(self):
        identity_fact = InstrumentIdentityFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=1,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            observed_at=datetime(2026, 1, 3, tzinfo=UTC),
            asset_class="etf",
            currency="CNY",
            calendar_id="XSHG",
            evidence="review://identity-1",
            fact_id=uuid4(),
        )
        mapping = _mapping(
            "SUMMARY.SH",
            date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            fact_id=uuid4(),
        )
        display = InstrumentDisplayFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=1,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            observed_at=datetime(2026, 1, 3, tzinfo=UTC),
            source="display-registry",
            source_revision="display-1",
            evidence="review://display-1",
            trading_code="SUMMARY",
            display_name="汇总名称",
            fact_id=uuid4(),
        )
        resolution = resolve_instrument_identity(
            INSTRUMENT_ID,
            effective_at=datetime(2026, 1, 5, tzinfo=UTC),
            data_cutoff=datetime(2026, 1, 5, tzinfo=UTC),
            identity_facts=(identity_fact,),
            display_facts=(display,),
            mappings=(mapping,),
            source=SOURCE,
        )
        self.assertIsNotNone(resolution)
        summary = resolution.evidence_summary
        self.assertEqual(summary["identity_fact"]["fact_id"], str(identity_fact.fact_id))
        self.assertNotIn("source", summary["identity_fact"])
        self.assertNotIn("source_revision", summary["identity_fact"])
        self.assertEqual(summary["identity_fact"]["valid_from"], "2026-01-01")
        self.assertEqual(summary["mapping_fact"]["source_code"], "SUMMARY.SH")
        self.assertEqual(summary["mapping_fact"]["mapping_source"], "exchange_announcement")
        self.assertEqual(summary["display_fact"]["source"], "display-registry")
        self.assertEqual(summary["display_fact"]["authority_status"], "authoritative")

    def test_pure_resolution_rejects_missing_identity_fact_evidence(self):
        fact = InstrumentIdentityFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=1,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            asset_class="etf",
            currency="CNY",
            calendar_id="XSHG",
            evidence="review://identity-evidence",
            fact_id=uuid4(),
        )
        # Simulate a corrupted provider materialization after construction.
        object.__setattr__(fact, "evidence", "")

        with self.assertRaises(IdentityMappingEvidenceMissingError) as context:
            resolve_instrument_identity(
                INSTRUMENT_ID,
                effective_at=datetime(2026, 1, 5, tzinfo=UTC),
                data_cutoff=datetime(2026, 1, 6, tzinfo=UTC),
                identity_facts=(fact,),
            )

        details = context.exception.details
        self.assertEqual(details["instrument_id"], str(INSTRUMENT_ID))
        self.assertEqual(details["source"], "identity_fact")
        self.assertIsNone(details["source_code"])
        self.assertEqual(details["session_date"], "2026-01-05")
        self.assertEqual(details["expected"], "non-blank evidence")
        self.assertEqual(details["actual"], "")
        self.assertEqual(
            details["data_cutoff"], datetime(2026, 1, 6, tzinfo=UTC).isoformat()
        )
        self.assertEqual(details["fact_version"], 1)

    def test_pure_resolution_rejects_missing_display_fact_evidence(self):
        fact = InstrumentDisplayFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=1,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            source="display-registry",
            evidence="review://display-evidence",
            trading_code="DISPLAY",
            fact_id=uuid4(),
        )
        # Simulate a corrupted provider materialization after construction.
        object.__setattr__(fact, "evidence", "")

        with self.assertRaises(IdentityMappingEvidenceMissingError) as context:
            resolve_instrument_identity(
                INSTRUMENT_ID,
                effective_at=datetime(2026, 1, 5, tzinfo=UTC),
                data_cutoff=datetime(2026, 1, 6, tzinfo=UTC),
                display_facts=(fact,),
            )

        details = context.exception.details
        self.assertEqual(details["instrument_id"], str(INSTRUMENT_ID))
        self.assertEqual(details["source"], "display-registry")
        self.assertIsNone(details["source_code"])
        self.assertEqual(details["session_date"], "2026-01-05")
        self.assertEqual(details["expected"], "non-blank evidence")
        self.assertEqual(details["actual"], "")
        self.assertEqual(
            details["data_cutoff"], datetime(2026, 1, 6, tzinfo=UTC).isoformat()
        )
        self.assertEqual(details["fact_version"], 1)

    def test_pure_resolution_rejects_duplicate_mapping_fact_version(self):
        logical_key = f"mapping:{INSTRUMENT_ID}:resolve-duplicate-version"
        first = _mapping(
            "DUPLICATE-RESOLVE-A.SH",
            date(2026, 1, 1),
            logical_fact_key=logical_key,
            fact_id=uuid4(),
        )
        duplicate = _mapping(
            "DUPLICATE-RESOLVE-B.SH",
            date(2026, 1, 1),
            logical_fact_key=logical_key,
            fact_id=uuid4(),
        )

        with self.assertRaises(IdentityMappingConflictError) as context:
            resolve_instrument_identity(
                INSTRUMENT_ID,
                effective_at=datetime(2026, 1, 5, tzinfo=UTC),
                data_cutoff=datetime(2026, 1, 6, tzinfo=UTC),
                mappings=(first, duplicate),
                source=SOURCE,
            )

        details = context.exception.details
        self.assertEqual(details["instrument_id"], str(INSTRUMENT_ID))
        self.assertEqual(details["source"], SOURCE)
        self.assertIsNone(details["source_code"])
        self.assertEqual(details["session_date"], "2026-01-05")
        self.assertEqual(
            details["expected"],
            "one immutable fact per logical_fact_key/fact_version",
        )
        self.assertEqual(details["actual"], 2)
        self.assertEqual(
            details["data_cutoff"], datetime(2026, 1, 6, tzinfo=UTC).isoformat()
        )
        self.assertEqual(details["fact_version"], 1)
        self.assertEqual(details["logical_fact_key"], logical_key)
        self.assertEqual(len(details["fact_ids"]), 2)

    def test_pure_identity_resolution_rejects_duplicate_fact_version(self):
        logical_key = f"identity:{INSTRUMENT_ID}:duplicate-version"
        first = InstrumentIdentityFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=1,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            asset_class="etf",
            currency="CNY",
            calendar_id="XSHG",
            evidence="review://identity-duplicate-1",
            fact_id=uuid4(),
            logical_fact_key=logical_key,
        )
        duplicate = InstrumentIdentityFact(
            instrument_id=INSTRUMENT_ID,
            fact_version=1,
            valid_from=date(2026, 1, 1),
            known_at=datetime(2026, 1, 3, tzinfo=UTC),
            observed_at=datetime(2026, 1, 3, tzinfo=UTC),
            asset_class="etf",
            currency="CNY",
            calendar_id="XSHG",
            evidence="review://identity-duplicate-2",
            fact_id=uuid4(),
            logical_fact_key=logical_key,
        )

        with self.assertRaises(IdentityFactConflictError) as ctx:
            resolve_instrument_identity(
                INSTRUMENT_ID,
                effective_at=datetime(2026, 1, 5, tzinfo=UTC),
                data_cutoff=datetime(2026, 1, 4, tzinfo=UTC),
                identity_facts=(first, duplicate),
            )

        self.assertEqual(ctx.exception.details["logical_fact_key"], logical_key)
        self.assertEqual(ctx.exception.details["fact_version"], 1)
        self.assertEqual(ctx.exception.details["actual"], 2)
        self.assertEqual(len(ctx.exception.details["fact_ids"]), 2)

    def test_pure_mapping_resolution_rejects_duplicate_fact_version(self):
        logical_key = f"mapping:{INSTRUMENT_ID}:duplicate-version"
        first = _mapping(
            "DUPLICATE-A.SH",
            date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            logical_fact_key=logical_key,
            fact_id=uuid4(),
        )
        duplicate = _mapping(
            "DUPLICATE-B.SH",
            date(2026, 1, 1),
            known_at=datetime(2026, 1, 3, tzinfo=UTC),
            logical_fact_key=logical_key,
            fact_id=uuid4(),
        )

        with self.assertRaises(IdentityMappingConflictError) as ctx:
            resolve_pit_mappings(
                INSTRUMENT_ID,
                source=SOURCE,
                sessions=[date(2026, 1, 5)],
                mappings=(first, duplicate),
                data_cutoff=datetime(2026, 1, 6, tzinfo=UTC),
            )

        self.assertEqual(ctx.exception.details["logical_fact_key"], logical_key)
        self.assertEqual(ctx.exception.details["fact_version"], 1)
        self.assertEqual(ctx.exception.details["actual"], 2)
        self.assertEqual(len(ctx.exception.details["fact_ids"]), 2)

    def test_pure_identity_resolution_rejects_blank_mapping_evidence(self):
        mapping = _mapping("NO-EVIDENCE.SH", date(2026, 1, 1))
        # Simulate a corrupted provider materialization after construction;
        # the resolver remains the final evidence gate before returning it.
        object.__setattr__(mapping, "evidence", "")

        with self.assertRaises(IdentityMappingEvidenceMissingError) as ctx:
            resolve_instrument_identity(
                INSTRUMENT_ID,
                effective_at=datetime(2026, 1, 5, tzinfo=UTC),
                data_cutoff=datetime(2026, 1, 6, tzinfo=UTC),
                mappings=(mapping,),
                source=SOURCE,
            )

        self.assertEqual(ctx.exception.code, "identity_mapping_evidence_missing")
        self.assertEqual(ctx.exception.details["source_code"], "NO-EVIDENCE.SH")
        self.assertEqual(ctx.exception.details["expected"], "non-blank evidence")
        self.assertEqual(ctx.exception.details["actual"], "")

    def test_mapping_correction_replays_by_data_cutoff(self):
        old_id = uuid4()
        key = "mapping:task10:effective-window"
        old = _mapping(
            "OLD.SH",
            date(2026, 1, 1),
            known_at=datetime(2026, 1, 2, tzinfo=UTC),
            logical_fact_key=key,
            fact_id=old_id,
        )
        new = _mapping(
            "NEW.SH",
            date(2026, 1, 1),
            known_at=datetime(2026, 2, 1, tzinfo=UTC),
            logical_fact_key=key,
            fact_id=uuid4(),
            fact_version=2,
            supersedes_fact_id=old_id,
        )
        sessions = [date(2026, 1, 5)]
        before = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=sessions,
            mappings=[old, new],
            data_cutoff=datetime(2026, 1, 31, tzinfo=UTC),
        )
        after = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=sessions,
            mappings=[old, new],
            data_cutoff=datetime(2026, 2, 2, tzinfo=UTC),
        )
        self.assertEqual(before.session_bindings[date(2026, 1, 5)], "OLD.SH")
        self.assertEqual(after.session_bindings[date(2026, 1, 5)], "NEW.SH")

    def test_cross_code_history_stitches_one_stable_identity(self):
        sessions = [date(2026, 1, 5), date(2026, 1, 6)]
        mappings = [
            _mapping("OLD.SH", date(2026, 1, 1), date(2026, 1, 6)),
            _mapping("NEW.SH", date(2026, 1, 6)),
        ]
        resolution = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=sessions,
            mappings=mappings,
            data_cutoff=CUTOFF,
        )
        reader = _BarReader(
            {"OLD.SH": [_bar(sessions[0])], "NEW.SH": [_bar(sessions[1])]}
        )
        history = read_segmented_history(resolution, reader)
        self.assertEqual([row.trade_date for row in history.bars], sessions)
        self.assertEqual({row.instrument_id for row in history.bars}, {INSTRUMENT_ID})
        self.assertEqual([call[0] for call in reader.calls], ["OLD.SH", "NEW.SH"])

    def test_missing_duplicate_out_of_range_and_wrong_identity_bars_block(self):
        day = date(2026, 1, 5)
        mapping = _mapping("A.SH", date(2026, 1, 1))
        resolution = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=[day],
            mappings=[mapping],
            data_cutoff=CUTOFF,
        )

        with self.assertRaises(HistoryBarsIncompleteError):
            read_segmented_history(resolution, _BarReader({"A.SH": []}))

        class DuplicateReader(_BarReader):
            def read_bars(self, source_code, start_date, end_date):
                rows = super().read_bars(source_code, start_date, end_date)
                return rows + rows

        with self.assertRaises(HistoryBarsDuplicateError):
            read_segmented_history(resolution, DuplicateReader({"A.SH": [_bar(day)]}))

        class OutOfRangeReader(_BarReader):
            def read_bars(self, source_code, start_date, end_date):
                self.calls.append((source_code, start_date, end_date))
                return (_bar(start_date), _bar(end_date + timedelta(days=1)))

        with self.assertRaises(HistoryBarsIncompleteError):
            read_segmented_history(resolution, OutOfRangeReader({}))

        with self.assertRaises(HistoryBarInstrumentMismatchError):
            read_segmented_history(
                resolution,
                _BarReader({"A.SH": [_bar(day, instrument_id=uuid4())]}),
            )

    def test_lookback_limit_fails_before_session_or_pit_provider_reads(self):
        class Resolver:
            calls = 0

            def resolve_sessions(self, **kwargs):
                self.calls += 1
                return ()

        class PitReader:
            calls = 0

            def bars_for_sessions(self, **kwargs):
                self.calls += 1
                sessions = kwargs.get("sessions") or kwargs.get("resolved_sessions")
                return tuple(
                    BarDTO(
                        instrument_id=INSTRUMENT_ID,
                        trade_date=day,
                        values={"close": Decimal("1")},
                    )
                    for day in sessions
                )

        resolver = Resolver()
        pit = PitReader()
        facade = StrategyDataDTO(
            object(),
            data_cutoff=CUTOFF,
            session_resolver=resolver,
            pit_reader=pit,
        )
        with self.assertRaises(LookbackSessionsLimitExceededError):
            facade.bars(INSTRUMENT_ID, lookback_sessions=513)
        self.assertEqual(resolver.calls, 0)
        self.assertEqual(pit.calls, 0)

        explicit_sessions = [
            CUTOFF.date() - timedelta(days=index)
            for index in range(512, -1, -1)
        ]
        explicit_pit = PitReader()
        explicit = StrategyDataDTO(
            object(),
            data_cutoff=CUTOFF,
            resolved_sessions=explicit_sessions,
            pit_reader=explicit_pit,
        )
        with self.assertRaises(LookbackLimitExceededError) as context:
            explicit.bars(
                INSTRUMENT_ID,
                start_date=explicit_sessions[0],
                end_date=explicit_sessions[-1],
            )
        self.assertEqual(context.exception.requested, 513)
        self.assertEqual(context.exception.maximum, 512)
        self.assertEqual(explicit_pit.calls, 0)

        sessions = [CUTOFF.date() - timedelta(days=index) for index in range(511, -1, -1)]
        valid = StrategyDataDTO(
            object(), data_cutoff=CUTOFF, resolved_sessions=sessions, pit_reader=pit
        )
        valid.bars(INSTRUMENT_ID, lookback_sessions=512)
        self.assertEqual(pit.calls, 1)

    def test_missing_display_never_uses_current_snapshot_fallback(self):
        class MissingDisplayProvider:
            def resolve_display(self, instrument_id, *, effective_at, data_cutoff):
                return None

        snapshot = resolve_display_snapshot(
            MissingDisplayProvider(),
            INSTRUMENT_ID,
            effective_at=datetime(2026, 1, 5, tzinfo=UTC),
            data_cutoff=CUTOFF,
        )
        self.assertEqual(snapshot.instrument_id, INSTRUMENT_ID)
        self.assertIsNone(snapshot.event_trading_code)
        self.assertIsNone(snapshot.event_name)
        self.assertIsNone(snapshot.event_display_name)

    def test_result_event_display_snapshot_is_frozen(self):
        from tests.backtest_runtime_fixture import (
            CountingStrategyView,
            DictMarketData,
            INSTRUMENT_ID as RUNTIME_INSTRUMENT_ID,
            ScriptedStrategy,
            build_axis,
            build_runner,
        )

        class DisplayProvider:
            name = "旧名称"

            def resolve_display(self, instrument_id, *, effective_at, data_cutoff):
                return InstrumentDisplay(
                    instrument_id=instrument_id,
                    trading_code="510300",
                    name=self.name,
                    display_name=self.name,
                )

        provider = DisplayProvider()
        axis = build_axis([date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)])
        runner = build_runner(
            run_id="task10-display-snapshot",
            axis=axis,
            market_data=DictMarketData(
                {
                    day: {RUNTIME_INSTRUMENT_ID: ("99", "100")}
                    for day in (date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5))
                }
            ),
            strategy_view=CountingStrategyView(
                {date(2026, 8, 3): "100", date(2026, 8, 4): "101"}
            ),
            strategy=ScriptedStrategy({0: {str(RUNTIME_INSTRUMENT_ID): "1"}}),
        )
        runner._display_provider = provider
        result = runner.run()
        provider.name = "新名称"
        event = next(event for event in result.events if event.event_type == "order_submitted")
        self.assertEqual(event.display_snapshot.event_name, "旧名称")
        self.assertEqual(event.payload["display"]["event_name"], "旧名称")


class IdentityLifecycleAcceptanceTests(unittest.TestCase):
    """Exercise the persistent identity path over a dependency-light SQLite schema."""

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:")
        cls.tables = [
            Instrument.__table__,
            EtfEntity.__table__,
            InstrumentCodeMappingRecord.__table__,
            InstrumentIdentityFactRecord.__table__,
            InstrumentDisplayFactRecord.__table__,
            MappingResolutionHead.__table__,
            DisplayResolutionHead.__table__,
            InstrumentIdentityMergeAuditRecord.__table__,
        ]
        Base.metadata.create_all(cls.engine, tables=cls.tables)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_get_or_create_rejects_missing_trading_code(self):
        for trading_code in (None, "", "   "):
            with self.subTest(trading_code=trading_code), Session(self.engine) as session:
                service = InstrumentIdentityService(session)
                with self.assertRaises(DomainValidationError):
                    service.get_or_create(
                        source=SOURCE,
                        source_code="NO-FALLBACK.SH",
                        trading_code=trading_code,
                        effective_session=date(2026, 1, 1),
                        asset_class="etf",
                        currency="CNY",
                        calendar_id="XSHG",
                        mapping_source="announcement",
                        evidence="announcement://no-fallback",
                        known_at=datetime(2026, 1, 2, tzinfo=UTC),
                        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                    )

    def test_first_and_repeated_import_reuse_the_same_uuid(self):
        with Session(self.engine) as session:
            service = InstrumentIdentityService(session)
            first = service.get_or_create(
                source=SOURCE,
                source_code="REPEAT.SH",
                trading_code="REPEAT",
                effective_session=date(2026, 1, 1),
                asset_class="etf",
                currency="CNY",
                calendar_id="XSHG",
                mapping_source="announcement",
                evidence="announcement://repeat",
                known_at=datetime(2026, 1, 2, tzinfo=UTC),
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            session.commit()
            second = service.get_or_create(
                source=SOURCE,
                source_code=" repeat.sh ",
                trading_code="REPEAT",
                effective_session=date(2026, 1, 1),
                asset_class="etf",
                currency="CNY",
                calendar_id="XSHG",
                mapping_source="announcement",
                evidence="announcement://repeat",
                known_at=datetime(2026, 1, 2, tzinfo=UTC),
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            self.assertEqual(first, second)
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(InstrumentCodeMappingRecord)
                    .where(
                        InstrumentCodeMappingRecord.source_code == "REPEAT.SH"
                    )
                ),
                1,
            )

    def test_service_resolution_evidence_summary_contains_selected_facts(self):
        with Session(self.engine) as session:
            service = InstrumentIdentityService(session)
            identity_id = service.get_or_create(
                source=SOURCE,
                source_code="SUMMARY-SERVICE.SH",
                trading_code="SUMMARY-SERVICE",
                effective_session=date(2026, 1, 1),
                asset_class="etf",
                currency="CNY",
                calendar_id="XSHG",
                mapping_source="announcement",
                evidence="announcement://summary-service",
                known_at=datetime(2026, 1, 2, tzinfo=UTC),
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                source_revision="mapping-1",
            )
            service.display_facts.append_fact(
                InstrumentDisplayFact(
                    instrument_id=identity_id,
                    fact_version=1,
                    valid_from=date(2026, 1, 1),
                    known_at=datetime(2026, 1, 2, tzinfo=UTC),
                    observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                    source="display-registry",
                    source_revision="display-1",
                    evidence="review://summary-service",
                    trading_code="SUMMARY-SERVICE",
                    display_name="服务汇总名称",
                    authority_rank=3,
                )
            )
            session.flush()
            resolution = service.resolve(
                identity_id,
                effective_at=datetime(2026, 1, 5, tzinfo=UTC),
                data_cutoff=datetime(2026, 1, 5, tzinfo=UTC),
                source=SOURCE,
            )
            self.assertIsNotNone(resolution)
            summary = resolution.evidence_summary
            self.assertEqual(
                summary["mapping_fact"]["source_revision"], "mapping-1"
            )
            self.assertEqual(
                summary["display_fact"]["source_revision"], "display-1"
            )
            self.assertEqual(summary["display_fact"]["authority_rank"], 3)
            self.assertEqual(summary["identity_fact"]["currency"], "CNY")

    def test_same_code_in_non_contiguous_windows_does_not_auto_merge(self):
        with Session(self.engine) as session:
            service = InstrumentIdentityService(session)
            first = service.get_or_create(
                source=SOURCE,
                source_code="REUSED.SH",
                trading_code="REUSED",
                effective_session=date(2026, 2, 1),
                valid_to=date(2026, 2, 2),
                asset_class="etf",
                currency="CNY",
                calendar_id="XSHG",
                mapping_source="announcement",
                evidence="announcement://reused-1",
                known_at=datetime(2026, 2, 2, tzinfo=UTC),
                observed_at=datetime(2026, 2, 2, tzinfo=UTC),
            )
            session.commit()
            second = service.get_or_create(
                source=SOURCE,
                source_code="REUSED.SH",
                trading_code="REUSED",
                effective_session=date(2026, 2, 5),
                asset_class="etf",
                currency="CNY",
                calendar_id="XSHG",
                mapping_source="announcement",
                evidence="announcement://reused-2",
                known_at=datetime(2026, 2, 6, tzinfo=UTC),
                observed_at=datetime(2026, 2, 6, tzinfo=UTC),
            )
            self.assertNotEqual(first, second)

    def test_merge_without_evidence_is_rejected_and_audited(self):
        with Session(self.engine) as session:
            service = InstrumentIdentityService(session)
            source = service.identities.create_identity(asset_class="etf")
            target = service.identities.create_identity(asset_class="etf")
            session.flush()
            with self.assertRaises(IdentityMergeEvidenceMissingError) as context:
                service.merge_identities(source, target, evidence=None)
            self.assertEqual(context.exception.code, "identity_mapping_evidence_missing")
            session.commit()
            audit = session.scalar(
                select(InstrumentIdentityMergeAuditRecord).where(
                    InstrumentIdentityMergeAuditRecord.source_instrument_id == source
                )
            )
            self.assertEqual(audit.outcome, "rejected")
            self.assertEqual(session.get(Instrument, source).status, "active")

    def test_transition_status_rejects_merge_bypass_without_audit(self):
        with Session(self.engine) as session:
            repository = InstrumentIdentityService(session).identities
            source = repository.create_identity(asset_class="etf")
            target = repository.create_identity(asset_class="etf")
            session.flush()

            with self.assertRaises(DomainValidationError):
                repository.transition_status(
                    source,
                    "merged",
                    merged_into_id=target,
                )

            self.assertEqual(session.get(Instrument, source).status, "active")
            self.assertIsNone(
                session.scalar(
                    select(InstrumentIdentityMergeAuditRecord).where(
                        InstrumentIdentityMergeAuditRecord.source_instrument_id == source
                    )
                )
            )

    def test_merged_identity_is_terminal_and_cannot_be_redirected(self):
        with Session(self.engine) as session:
            service = InstrumentIdentityService(session)
            repository = service.identities
            source = repository.create_identity(asset_class="etf")
            target = repository.create_identity(asset_class="etf")
            other_target = repository.create_identity(asset_class="etf")
            session.flush()

            with self.assertRaises(DomainValidationError):
                repository.transition_status(
                    source,
                    "merged",
                    merged_into_id=target,
                )
            self.assertEqual(session.get(Instrument, source).status, "active")

            merged_result = service.merge_identities(
                source,
                target,
                evidence="review://merge-terminal",
                mapping_source="identity-review",
            )
            self.assertTrue(merged_result)
            merged = session.get(Instrument, source)
            self.assertIsNotNone(merged)
            self.assertEqual(merged.merged_into_id, target)
            self.assertIs(
                repository.transition_status(
                    source,
                    "merged",
                    merged_into_id=target,
                ),
                merged,
            )
            with self.assertRaises(DomainValidationError):
                repository.transition_status(
                    source,
                    "merged",
                    merged_into_id=other_target,
                )
            self.assertEqual(session.get(Instrument, source).merged_into_id, target)

    def test_identity_cannot_be_created_directly_as_merged(self):
        with Session(self.engine) as session:
            with self.assertRaises(DomainValidationError):
                InstrumentIdentityService(session).identities.create_identity(
                    asset_class="etf",
                    status="merged",
                )

    def test_identity_uuid_is_server_generated_and_caller_ids_are_rejected(self):
        with Session(self.engine) as session:
            repository = InstrumentIdentityService(session).identities
            supplied_id = uuid4()
            with self.assertRaises(DomainValidationError):
                repository.create_identity(
                    asset_class="etf",
                    instrument_id=supplied_id,
                )
            self.assertIsNone(session.get(Instrument, supplied_id))

            generated_id = repository.create_identity(asset_class="etf")
            self.assertIsInstance(generated_id, UUID)
            self.assertNotEqual(generated_id, supplied_id)

    def test_identity_status_and_merge_target_are_database_invariants(self):
        with Session(self.engine) as session:
            invalid_rows = (
                # A terminal identity must point at a reconciliation target.
                Instrument(id=uuid4(), asset_class="etf", status="merged"),
                # Active/deprecated identities cannot retain a redirect.
                Instrument(
                    id=uuid4(),
                    asset_class="etf",
                    status="active",
                    merged_into_id=uuid4(),
                ),
            )
            for row in invalid_rows:
                with self.subTest(status=row.status, redirect=row.merged_into_id):
                    session.add(row)
                    with self.assertRaises(IntegrityError):
                        session.flush()
                    session.rollback()

            target_id = InstrumentIdentityService(session).identities.create_identity(
                asset_class="etf"
            )
            source_id = uuid4()
            session.add(
                Instrument(
                    id=source_id,
                    asset_class="etf",
                    status="merged",
                    merged_into_id=target_id,
                )
            )
            session.flush()

            # Even a valid target cannot be the identity's own redirect.
            session.add(
                Instrument(
                    id=uuid4(),
                    asset_class="etf",
                    status="merged",
                    merged_into_id=source_id,
                )
            )
            # The preceding row is valid; this explicit self-target check is
            # covered by constructing a separate identity whose ID is used as
            # its own target.
            self_target_id = uuid4()
            session.add(
                Instrument(
                    id=self_target_id,
                    asset_class="etf",
                    status="merged",
                    merged_into_id=self_target_id,
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

    def test_falsey_fact_ids_are_rejected_instead_of_regenerated(self):
        for invalid_fact_id in (False, "", 0):
            with self.subTest(fact_id=invalid_fact_id):
                with self.assertRaises(DomainValidationError):
                    _mapping(
                        "INVALID-ID.SH",
                        date(2026, 1, 1),
                        fact_id=invalid_fact_id,
                    )
                with self.assertRaises(DomainValidationError):
                    InstrumentIdentityFact(
                        instrument_id=INSTRUMENT_ID,
                        fact_version=1,
                        asset_class="etf",
                        currency="CNY",
                        calendar_id="XSHG",
                        valid_from=date(2026, 1, 1),
                        known_at=datetime(2026, 1, 1, tzinfo=UTC),
                        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                        evidence="review://identity-id",
                        fact_id=invalid_fact_id,
                    )
                with self.assertRaises(DomainValidationError):
                    InstrumentDisplayFact(
                        instrument_id=INSTRUMENT_ID,
                        fact_version=1,
                        valid_from=date(2026, 1, 1),
                        known_at=datetime(2026, 1, 1, tzinfo=UTC),
                        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                        source="registry",
                        evidence="review://display-id",
                        fact_id=invalid_fact_id,
                    )

    def test_existing_etf_identity_migration_preserves_id_and_no_history_facts(self):
        legacy_id = uuid4()
        with Session(self.engine) as session:
            session.add(EtfEntity(id=legacy_id))
            session.commit()
            self.assertEqual(migrate_existing_etf_identities(session), 1)
            session.commit()
            identity = session.get(Instrument, legacy_id)
            self.assertIsNotNone(identity)
            self.assertEqual(identity.id, legacy_id)
            self.assertEqual(identity.asset_class, "etf")
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(InstrumentCodeMappingRecord).where(
                        InstrumentCodeMappingRecord.instrument_id == legacy_id
                    )
                ),
                0,
            )

    def test_ordinary_mapping_append_rejects_reverse_knowledge_time(self):
        with Session(self.engine) as session:
            identity_id = InstrumentIdentityService(session).identities.create_identity(
                asset_class="etf"
            )
            session.flush()
            repository = InstrumentCodeMappingRepository(session)
            key = f"mapping:monotonic:{uuid4()}"
            first = InstrumentCodeMapping(
                instrument_id=identity_id,
                source=SOURCE,
                source_code="MONO.SH",
                trading_code="MONO",
                valid_from=date(2026, 1, 1),
                mapping_source="announcement",
                evidence="announcement://monotonic/1",
                known_at=datetime(2026, 1, 2, tzinfo=UTC),
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                fact_id=uuid4(),
                logical_fact_key=key,
            )
            repository.add_mapping(first)
            session.flush()
            correction = InstrumentCodeMapping(
                instrument_id=identity_id,
                source=SOURCE,
                source_code="MONO-CORRECTED.SH",
                trading_code="MONO-CORRECTED",
                valid_from=date(2026, 1, 1),
                mapping_source="announcement",
                evidence="announcement://monotonic/2",
                known_at=datetime(2026, 1, 1, tzinfo=UTC),
                observed_at=datetime(2026, 1, 3, tzinfo=UTC),
                fact_id=uuid4(),
                fact_version=2,
                logical_fact_key=key,
                supersedes_fact_id=first.fact_id,
            )
            with self.assertRaises(DomainValidationError):
                repository.add_mapping(correction)
            repository.append_historical_mapping(correction)
            session.flush()
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(InstrumentCodeMappingRecord)
                    .where(
                        InstrumentCodeMappingRecord.logical_fact_key == key
                    )
                ),
                2,
            )

    def test_ordinary_identity_and_display_append_reject_reverse_knowledge_time(self):
        with Session(self.engine) as session:
            service = InstrumentIdentityService(session)
            identity_id = service.identities.create_identity(asset_class="etf")
            session.flush()
            identity_key = f"identity:monotonic:{uuid4()}"
            identity_first = InstrumentIdentityFact(
                instrument_id=identity_id,
                fact_version=1,
                asset_class="etf",
                currency="CNY",
                calendar_id="XSHG",
                valid_from=date(2026, 1, 1),
                known_at=datetime(2026, 1, 2, tzinfo=UTC),
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                evidence="review://identity/1",
                fact_id=uuid4(),
                logical_fact_key=identity_key,
            )
            service.identity_facts.append_fact(identity_first)
            session.flush()
            identity_correction = InstrumentIdentityFact(
                instrument_id=identity_id,
                fact_version=2,
                asset_class="etf",
                currency="CNY",
                calendar_id="XSHG",
                valid_from=date(2026, 1, 1),
                known_at=datetime(2026, 1, 1, tzinfo=UTC),
                observed_at=datetime(2026, 1, 3, tzinfo=UTC),
                evidence="review://identity/2",
                fact_id=uuid4(),
                logical_fact_key=identity_key,
                supersedes_fact_id=identity_first.fact_id,
            )
            with self.assertRaises(DomainValidationError):
                service.identity_facts.append_fact(identity_correction)
            service.identity_facts.append_reconstructed_fact(identity_correction)

            display_key = f"display:monotonic:{uuid4()}"
            display_first = InstrumentDisplayFact(
                instrument_id=identity_id,
                fact_version=1,
                valid_from=date(2026, 1, 1),
                known_at=datetime(2026, 1, 2, tzinfo=UTC),
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                source="registry",
                evidence="review://display/1",
                trading_code="MONO",
                fact_id=uuid4(),
                logical_fact_key=display_key,
            )
            service.display_facts.append_fact(display_first)
            session.flush()
            display_correction = InstrumentDisplayFact(
                instrument_id=identity_id,
                fact_version=2,
                valid_from=date(2026, 1, 1),
                known_at=datetime(2026, 1, 1, tzinfo=UTC),
                observed_at=datetime(2026, 1, 3, tzinfo=UTC),
                source="registry",
                evidence="review://display/2",
                trading_code="MONO-CORRECTED",
                fact_id=uuid4(),
                logical_fact_key=display_key,
                supersedes_fact_id=display_first.fact_id,
            )
            with self.assertRaises(DomainValidationError):
                service.display_facts.append_fact(display_correction)
            service.display_facts.append_historical_fact(display_correction)
            session.flush()


class PostgreSQLIdentityAcceptanceTests(unittest.TestCase):
    """The production-only race is opt-in and never touches ordinary local runs."""

    @classmethod
    def setUpClass(cls):
        cls.engine = None
        if os.getenv("POSTGRES_TEST_ENABLED") == "1":
            cls.engine = create_engine(get_settings().database_url)

    @classmethod
    def tearDownClass(cls):
        if cls.engine is not None:
            cls.engine.dispose()

    def _insert_mapping_fact(
        self,
        session,
        *,
        instrument_id,
        source_code,
        valid_from,
        valid_to,
        known_at,
        logical_fact_key,
        fact_version=1,
        supersedes_fact_id=None,
    ):
        """Insert one fully materialized mapping row for PostgreSQL checks."""

        fact_id = uuid4()
        session.add(
            InstrumentCodeMappingRecord(
                id=fact_id,
                instrument_id=instrument_id,
                source=SOURCE,
                source_code=source_code,
                trading_code=source_code.split(".")[0],
                valid_from=valid_from,
                valid_to=valid_to,
                fact_version=fact_version,
                logical_fact_key=logical_fact_key,
                supersedes_fact_id=supersedes_fact_id,
                effective_range=Range(valid_from, valid_to, bounds="[)"),
                knowledge_range=Range(
                    known_at,
                    None,
                    bounds="[)",
                ),
                mapping_source="exchange_announcement",
                evidence="announcement://task-10/postgresql",
                known_at=known_at,
                observed_at=known_at,
            )
        )
        return fact_id

    def _insert_identity(self, session):
        """Insert one identity row and return its stable UUID."""

        identity_id = uuid4()
        session.add(Instrument(id=identity_id, asset_class="etf"))
        session.flush()
        return identity_id

    @unittest.skipUnless(
        os.getenv("POSTGRES_TEST_ENABLED") == "1",
        "requires the disposable PostgreSQL CI service",
    )
    def test_concurrent_identity_import_is_reserved_for_postgresql_ci(self):
        source_code = f"TASK10_{uuid4().hex[:12]}.SH"
        barrier = threading.Barrier(2)
        outcomes = []

        def import_identity():
            with Session(self.engine) as session:
                try:
                    barrier.wait(timeout=10)
                    identity_id = InstrumentIdentityService(session).get_or_create(
                        source=SOURCE,
                        source_code=source_code,
                        trading_code=source_code.split(".")[0],
                        effective_session=date(2026, 3, 1),
                        asset_class="etf",
                        currency="CNY",
                        calendar_id="XSHG",
                        mapping_source="announcement",
                        evidence="announcement://task-10/concurrent",
                        known_at=datetime(2026, 3, 2, tzinfo=UTC),
                        observed_at=datetime(2026, 3, 2, tzinfo=UTC),
                    )
                    session.commit()
                    outcomes.append(("ok", identity_id))
                except Exception as exc:  # pragma: no cover - CI race path
                    session.rollback()
                    outcomes.append(("error", type(exc).__name__, str(exc)))

        threads = [threading.Thread(target=import_identity) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(item[0] == "ok" for item in outcomes), outcomes)
        self.assertEqual(outcomes[0][1], outcomes[1][1])
        with Session(self.engine) as session:
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(InstrumentCodeMappingRecord)
                    .where(
                        InstrumentCodeMappingRecord.source_code == source_code
                    )
                ),
                1,
            )

    @unittest.skipUnless(
        os.getenv("POSTGRES_TEST_ENABLED") == "1",
        "requires the disposable PostgreSQL CI service",
    )
    def test_concurrent_mapping_correction_does_not_create_two_heads(self):
        """Concurrent writes for one correction snapshot leave one head."""

        identity_id = uuid4()
        logical_key = f"mapping:task10:concurrent:{uuid4()}"
        known_at = datetime(2026, 4, 2, tzinfo=UTC)
        previous_known_at = datetime(2026, 4, 1, tzinfo=UTC)
        with Session(self.engine) as session:
            session.add(Instrument(id=identity_id, asset_class="etf"))
            session.flush()
            previous_id = self._insert_mapping_fact(
                session,
                instrument_id=identity_id,
                source_code="CORRECTION.SH",
                valid_from=date(2026, 1, 1),
                valid_to=None,
                known_at=previous_known_at,
                logical_fact_key=logical_key,
            )
            corrected_id = self._insert_mapping_fact(
                session,
                instrument_id=identity_id,
                source_code="CORRECTION.SH",
                valid_from=date(2026, 1, 1),
                valid_to=None,
                known_at=known_at,
                logical_fact_key=logical_key,
                fact_version=2,
                supersedes_fact_id=previous_id,
            )
            session.commit()

        barrier = threading.Barrier(2)
        outcomes = []

        def write_head():
            with Session(self.engine) as session:
                try:
                    barrier.wait(timeout=10)
                    session.add(
                        MappingResolutionHead(
                            id=uuid4(),
                            logical_fact_key=logical_key,
                            knowledge_from=known_at,
                            fact_id=corrected_id,
                            instrument_id=identity_id,
                            source=SOURCE,
                            source_code="CORRECTION.SH",
                            effective_range=Range(
                                date(2026, 1, 1), None, bounds="[)"
                            ),
                            knowledge_range=Range(
                                known_at,
                                None,
                                bounds="[)",
                            ),
                        )
                    )
                    session.commit()
                    outcomes.append("written")
                except IntegrityError:
                    session.rollback()
                    outcomes.append("conflict")
                except Exception as exc:  # pragma: no cover - CI race diagnostics
                    session.rollback()
                    outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=write_head) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(sorted(outcomes), ["conflict", "written"], outcomes)
        with Session(self.engine) as session:
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(MappingResolutionHead)
                    .where(
                        MappingResolutionHead.logical_fact_key == logical_key,
                        MappingResolutionHead.knowledge_from == known_at,
                    )
                ),
                1,
            )

    @unittest.skipUnless(
        os.getenv("POSTGRES_TEST_ENABLED") == "1",
        "requires the disposable PostgreSQL CI service",
    )
    def test_same_knowledge_snapshot_overlapping_mappings_are_rejected(self):
        """One source code cannot identify two instruments in one snapshot."""

        known_at = datetime(2026, 5, 2, tzinfo=UTC)
        with Session(self.engine) as session:
            first_identity = self._insert_identity(session)
            second_identity = self._insert_identity(session)
            self._insert_mapping_fact(
                session,
                instrument_id=first_identity,
                source_code="OVERLAP.SH",
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 3, 1),
                known_at=known_at,
                logical_fact_key=f"mapping:task10:overlap:a:{uuid4()}",
            )
            self._insert_mapping_fact(
                session,
                instrument_id=second_identity,
                source_code="OVERLAP.SH",
                valid_from=date(2026, 2, 1),
                valid_to=date(2026, 4, 1),
                known_at=known_at,
                logical_fact_key=f"mapping:task10:overlap:b:{uuid4()}",
            )
            with self.assertRaises(IntegrityError):
                session.commit()

    @unittest.skipUnless(
        os.getenv("POSTGRES_TEST_ENABLED") == "1",
        "requires the disposable PostgreSQL CI service",
    )
    def test_same_source_code_non_overlapping_effective_windows_are_reusable(self):
        """One source code may be reassigned after its effective interval ends."""

        known_at = datetime(2026, 6, 2, tzinfo=UTC)
        source_code = f"REUSE_{uuid4().hex[:12]}.SH"
        with Session(self.engine) as session:
            first_identity = self._insert_identity(session)
            second_identity = self._insert_identity(session)
            self._insert_mapping_fact(
                session,
                instrument_id=first_identity,
                source_code=source_code,
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 3, 1),
                known_at=known_at,
                logical_fact_key=f"mapping:task10:reuse:first:{uuid4()}",
            )
            self._insert_mapping_fact(
                session,
                instrument_id=second_identity,
                source_code=source_code,
                valid_from=date(2026, 3, 1),
                valid_to=None,
                known_at=known_at,
                logical_fact_key=f"mapping:task10:reuse:second:{uuid4()}",
            )
            session.commit()
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(InstrumentCodeMappingRecord)
                    .where(
                        InstrumentCodeMappingRecord.source_code == source_code
                    )
                ),
                2,
            )


class MigrationScopeAcceptanceTests(unittest.TestCase):
    """Static checks keep schema scope and downgrade behavior reviewable."""

    def test_task10_migration_has_native_ranges_and_no_etf_snapshot_backfill(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "app/db/migrations/versions/20260828_01_add_stable_instrument_identity.py"
        )
        source = path.read_text(encoding="utf-8")
        self.assertIn("CREATE EXTENSION IF NOT EXISTS btree_gist", source)
        self.assertIn("postgresql.DATERANGE", source)
        self.assertIn("postgresql.TSTZRANGE", source)
        self.assertIn("ex_mapping_effective_knowledge_overlap", source)
        self.assertIn("ex_mapping_source_code_identity_overlap", source)
        self.assertIn("instrument_display_facts", source)
        self.assertNotIn("INSERT INTO instrument_code_mappings", source)
        self.assertNotIn("INSERT INTO instrument_display_facts", source)
        self.assertIn("etf_codes is not a PIT source", source)

    def test_postgresql_exclusions_cover_fact_and_resolution_head_conflicts(self):
        """Keep every production overlap invariant visible in review tests."""

        path = (
            Path(__file__).resolve().parents[1]
            / "app/db/migrations/versions/20260828_01_add_stable_instrument_identity.py"
        )
        source = path.read_text(encoding="utf-8")
        for constraint in (
            "ex_mapping_effective_knowledge_overlap",
            "ex_mapping_source_code_identity_overlap",
            "ex_display_effective_knowledge_overlap",
            "ex_display_heads_effective_knowledge_overlap",
            "ex_mapping_heads_effective_knowledge_overlap",
            "ex_mapping_heads_source_code_identity_overlap",
        ):
            with self.subTest(constraint=constraint):
                self.assertIn(constraint, source)
        self.assertIn('(\"authority_rank\", \"=\")', source)
        self.assertIn('(\"source_code\", \"=\")', source)
        self.assertIn('(\"instrument_id\", \"<>\")', source)
        self.assertGreaterEqual(source.count('(\"effective_range\", \"&&\")'), 6)
        self.assertGreaterEqual(source.count('(\"knowledge_range\", \"&&\")'), 6)

    def test_public_scope_does_not_add_deferred_protocols(self):
        source = Path(
            __file__).resolve().parents[1] / "app/backtesting/data/pit_history.py"
        tree = source.read_text(encoding="utf-8")
        self.assertNotIn("class DataSession", tree)
        self.assertNotIn("class TimeAxis", tree)
        self.assertNotIn("warmup_sessions", tree)
        self.assertNotIn("consistency_token", tree)
        self.assertNotIn("qfq", tree.lower())
        self.assertNotIn("hfq", tree.lower())

    def test_task10_migration_round_trips_on_sqlite(self):
        """SQLite smoke tests must exercise migration DDL, not only ORM metadata."""

        metadata = MetaData()
        instruments = Table(
            "instruments",
            metadata,
            Column("id", Uuid, primary_key=True),
            Column("asset_class", String(32), nullable=False),
            Column("created_at", DateTime),
        )
        mappings = Table(
            "instrument_code_mappings",
            metadata,
            Column("id", Uuid, primary_key=True),
            Column("instrument_id", Uuid, nullable=False),
            Column("source", String(32), nullable=False),
            Column("source_code", String(32), nullable=False),
            Column("trading_code", String(16), nullable=False),
            Column("valid_from", Date, nullable=False),
            Column("valid_to", Date),
            Column("source_revision", String(64)),
            Column("mapping_source", String(64), nullable=False),
            Column("evidence", String(2048), nullable=False),
            Column("known_at", DateTime, nullable=False),
            Column("observed_at", DateTime, nullable=False),
            Column("created_at", DateTime),
        )
        engine = create_engine("sqlite:///:memory:")
        identity_id = uuid4()
        mapping_id = uuid4()
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(
                instruments.insert().values(
                    id=identity_id,
                    asset_class="etf",
                    created_at=datetime(2026, 1, 1),
                )
            )
            connection.execute(
                mappings.insert().values(
                    id=mapping_id,
                    instrument_id=identity_id,
                    source=SOURCE,
                    source_code="MIGRATE.SH",
                    trading_code="MIGRATE",
                    valid_from=date(2026, 1, 1),
                    valid_to=None,
                    source_revision=None,
                    mapping_source="announcement",
                    evidence="announcement://migration",
                    known_at=datetime(2026, 1, 2),
                    observed_at=datetime(2026, 1, 2),
                    created_at=datetime(2026, 1, 2),
                )
            )
        migration = __import__(
            "app.db.migrations.versions.20260828_01_add_stable_instrument_identity",
            fromlist=["upgrade", "downgrade"],
        )
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
            upgraded_metadata = MetaData()
            upgraded_instruments = Table(
                "instruments", upgraded_metadata, autoload_with=connection
            )
            upgraded_mappings = Table(
                "instrument_code_mappings",
                upgraded_metadata,
                autoload_with=connection,
            )
            upgraded_identity_facts = Table(
                "instrument_identity_facts",
                upgraded_metadata,
                autoload_with=connection,
            )
            upgraded_display_facts = Table(
                "instrument_display_facts",
                upgraded_metadata,
                autoload_with=connection,
            )
            self.assertIn("effective_range", upgraded_identity_facts.c)
            self.assertIn("knowledge_range", upgraded_identity_facts.c)
            self.assertNotIn("source", upgraded_identity_facts.c)
            self.assertNotIn("source_revision", upgraded_identity_facts.c)
            self.assertNotIn("source_code", upgraded_identity_facts.c)
            self.assertEqual(
                inspect(connection)
                .get_pk_constraint("instrument_identity_facts")["constrained_columns"],
                ["id"],
            )
            self.assertEqual(
                inspect(connection)
                .get_pk_constraint("instrument_display_facts")["constrained_columns"],
                ["id"],
            )
            self.assertIn("id", upgraded_display_facts.c)
            identity_fact_indexes = {
                index["name"]
                for index in inspect(connection).get_indexes("instrument_identity_facts")
            }
            self.assertNotIn(
                "ix_instrument_identity_facts_source_code", identity_fact_indexes
            )
            upgraded = connection.execute(
                select(
                    upgraded_instruments.c.id,
                    upgraded_instruments.c.status,
                    upgraded_mappings.c.fact_version,
                    upgraded_mappings.c.logical_fact_key,
                ).select_from(
                    upgraded_instruments.join(
                        upgraded_mappings,
                        upgraded_instruments.c.id == upgraded_mappings.c.instrument_id,
                    )
                )
            ).one()
            self.assertEqual(UUID(str(upgraded.id)), identity_id)
            self.assertEqual(upgraded.status, "active")
            self.assertEqual(upgraded.fact_version, 1)
            self.assertTrue(upgraded.logical_fact_key.startswith("legacy:"))
            with Operations.context(context):
                migration.downgrade()
            self.assertEqual(
                inspect(connection).get_table_names(),
                ["instrument_code_mappings", "instruments"],
            )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
