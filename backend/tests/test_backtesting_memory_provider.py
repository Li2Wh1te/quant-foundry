"""Tests for the deterministic in-memory DataProvider (task 03-02).

Covers the provider/session/chunk protocol conformance, the frozen
first-version constraints (512-session lookback limit, ``data_cutoff``
visibility, no gap filling), warmup isolation, the fixed 20-session
chunk policy, and consistency-validation-before-business-query.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from unittest.mock import patch

from app.backtesting.calendar_axis import (
    CalendarQualityStatus,
    CalendarDefinition,
    CalendarRegistry,
    CalendarSessionFact,
    CalendarSourcePriority,
)
from app.backtesting.data.errors import (
    ConsistencyCoverageIncompleteError,
    ConsistencyNotValidatedError,
    ConsistencyTokenExpiredError,
    CalendarDefinitionMissingError,
    DataCutoffExceededError,
    DataPreflightBlockedError,
    DataSessionClosedError,
    HistoryIncompleteError,
    InvalidDataRequestError,
    LookbackSessionsLimitExceededError,
    ProviderContractViolationError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.facts import Bar, FactEvidence
from app.backtesting.data.memory import (
    ISSUE_INSTRUMENT_NOT_FOUND,
    ISSUE_MANDATORY_BAR_COVERAGE_MISSING,
    ISSUE_PROVIDER_KEY_MISMATCH,
    ISSUE_UNSUPPORTED_CAPABILITY,
    ISSUE_UNSUPPORTED_FREQUENCY,
    ISSUE_UNSUPPORTED_PRICE_BASIS,
    ISSUE_UNSUPPORTED_TOKEN_CONTRACT,
    MemoryDataSet,
    MemoryDataProvider,
)
from app.backtesting.data.protocols import (
    DataChunkSession,
    DataProvider,
    DataSession,
)
from app.backtesting.data.requests import (
    BarQuery,
    ConsistencyMode,
    ConsistencyValidation,
    ContractRef,
    CoverageQuery,
    DataCapability,
    DataChunkQuery,
    DataPreflightRequest,
    DataRequest,
    DateRange,
    InstrumentQuery,
    InstrumentScopeMode,
    LookbackWindow,
    MarketScope,
    PreflightStatus,
    PriceBasis,
    QualityStatus,
    QueryBoundary,
    TickQuery,
    UniverseQuery,
    UniverseQueryPolicy,
)
from app.backtesting.data.warmup import WARMUP_COVERAGE_INSUFFICIENT
from app.instruments.domain import (
    CorporateActionRequirement,
    InstrumentCapabilities,
    InstrumentDisplay,
    InstrumentSpec,
    VersionedReference,
)
from app.instruments.rules.contracts import (
    StrategyRuleDeclaration,
    TradingStatusRequirement,
)

RULES = ContractRef(key="rules.fixture", version=1)
TOKEN_CONTRACT = ContractRef(key="memory_revision_vector", version=1)

IID_A = UUID("00000000-0000-0000-0000-000000000001")
IID_B = UUID("00000000-0000-0000-0000-000000000002")

PROVIDER_KEY = "memory-fixture"
CAL_ID = "fixture-calendar"
DEF_VERSION = "fixture@1"
SESSION_WINDOWS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
CLOCK = datetime(2026, 3, 15, 16, 0, tzinfo=timezone.utc)

D26 = date(2025, 12, 26)
D29 = date(2025, 12, 29)
D30 = date(2025, 12, 30)
D31 = date(2025, 12, 31)
J2 = date(2026, 1, 2)
J5 = date(2026, 1, 5)
J6 = date(2026, 1, 6)
J7 = date(2026, 1, 7)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def weekdays(start: date, end: date) -> list[date]:
    """Every weekday between ``start`` and ``end`` inclusive."""

    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    ]


def every_day(start: date, end: date) -> list[date]:
    """Every calendar day between ``start`` and ``end`` inclusive."""

    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def make_definition() -> CalendarDefinition:
    return CalendarDefinition(
        calendar_id=CAL_ID,
        definition_version=DEF_VERSION,
        timezone="Asia/Shanghai",
        default_sessions=SESSION_WINDOWS,
        source="fixture",
    )


def make_evidence(quality: QualityStatus = QualityStatus.COMPLETE) -> FactEvidence:
    return FactEvidence(
        source="memory-fixture",
        observed_at=CLOCK,
        quality_status=quality,
        known_at=CLOCK,
        source_revision="fixture-v1",
    )


def make_bar(
    instrument_id: UUID,
    day: date,
    quality: QualityStatus = QualityStatus.COMPLETE,
) -> Bar:
    return Bar(
        instrument_id=instrument_id,
        trade_date=day,
        frequency="1d",
        open="10",
        high="11",
        low="9",
        close="10.5",
        volume="100",
        amount="1050",
        price_basis=PriceBasis.RAW,
        evidence=make_evidence(quality),
    )


def make_spec(instrument_id: UUID) -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id=instrument_id,
        display=InstrumentDisplay(
            instrument_id=instrument_id,
            trading_code="FIXTURE01",
            name="虚构标的A",
        ),
        asset_class="equity",
        exchange="XFIX",
        currency="CNY",
        calendar_id=CAL_ID,
        price_precision=2,
        quantity_precision=0,
        price_tick="0.01",
        lot_size="100",
        minimum_order_quantity="100",
        contract_multiplier="1",
        trading_session_template=VersionedReference(key="fixture_template", version=1),
        trading_hours={
            "timezone": "Asia/Shanghai",
            "sessions": (
                (time(9, 30), time(11, 30)),
                (time(13, 0), time(15, 0)),
            ),
        },
        settlement_rule_class="t1_before_open_match",
        sellable_rule=StrategyRuleDeclaration(
            ("sell_limited_by_available_position",)
        ),
        fee_categories=frozenset({"commission"}),
        trading_status_policy={
            "suspension": TradingStatusRequirement.REQUIRED,
            "opening_availability": TradingStatusRequirement.REQUIRED,
            "price_limit_tradability": TradingStatusRequirement.REQUIRED,
        },
        order_types=frozenset({"limit"}),
        price_limit_rule=VersionedReference(
            key="fixture_price_limit_rule", version=1
        ),
        cash_availability_rule=VersionedReference(
            key="fixture_cash_availability_rule", version=1
        ),
        position_availability_rule=VersionedReference(
            key="fixture_position_availability_rule", version=1
        ),
        rule_package_reference=VersionedReference(key="fixture_rules", version=1),
        valid_from=datetime(2000, 1, 1, tzinfo=timezone.utc),
        valid_to=None,
        capabilities=InstrumentCapabilities(
            position_sides=frozenset({"long"}),
            order_types=frozenset({"limit"}),
            margin_supported=False,
            corporate_action_requirement=CorporateActionRequirement.NOT_APPLICABLE,
        ),
    )


def build_dataset(
    *,
    facts_start: date,
    facts_end: date,
    open_days: set[date],
    bar_days: set[date] | None = None,
    instruments: tuple[UUID, ...] = (IID_A,),
    bar_quality: QualityStatus = QualityStatus.COMPLETE,
) -> MemoryDataSet:
    """Assemble a dataset whose facts cover every day of the span."""

    facts = [
        CalendarSessionFact(
            calendar_id=CAL_ID,
            session_date=day,
            is_open=day in open_days,
            definition_version=DEF_VERSION,
            source="fixture",
        )
        for day in every_day(facts_start, facts_end)
    ]
    if bar_days is None:
        bar_days = open_days
    bars = [
        make_bar(instrument_id, day, bar_quality)
        for instrument_id in instruments
        for day in sorted(bar_days)
    ]
    return MemoryDataSet(
        provider_key=PROVIDER_KEY,
        fixture_revision="fixture-v1",
        calendar_definitions=(make_definition(),),
        calendar_facts=tuple(facts),
        instruments=tuple(make_spec(iid) for iid in instruments),
        bars=tuple(bars),
        clock=CLOCK,
    )


def make_intent(
    *,
    start: date,
    end: date,
    warmup: int = 0,
    capabilities: tuple[DataCapability, ...] = (DataCapability.BARS,),
    static: tuple[UUID, ...] = (IID_A,),
    mandatory: tuple[UUID, ...] = (),
    provider_key: str = PROVIDER_KEY,
) -> DataPreflightRequest:
    return DataPreflightRequest(
        provider_key=provider_key,
        requested_window=DateRange(start_date=start, end_date=end),
        frequency="1d",
        rule_package=RULES,
        market_scope=MarketScope(),
        universe_query_policy=UniverseQueryPolicy(),
        instrument_scope_mode=InstrumentScopeMode.FIXED,
        required_capabilities=capabilities,
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
        consistency_token_contract=TOKEN_CONTRACT,
        query_boundary=boundary(CUTOFF_A),
        static_instrument_ids=static,
        mandatory_instrument_ids=mandatory,
        warmup_sessions=warmup,
    )


def _rule_report_for(intent: DataPreflightRequest):
    """A minimal READY fixed-instrument rule preflight bound to ``intent``."""

    from app.instruments.domain import VersionedReference as _VR
    from app.instruments.rule_preflight import (
        InstrumentRulePreflightResult,
        RuleCheckStatus,
        RulePreflightReport,
    )
    from app.instruments.rule_snapshots import (
        InstrumentRuleSnapshotSegment,
        RunRuleSnapshotBundle,
    )
    from app.instruments.rules import ResolutionStatus

    ids = sorted(
        set(intent.static_instrument_ids) | set(intent.mandatory_instrument_ids)
    )
    results = tuple(
        InstrumentRulePreflightResult(
            instrument_id=iid,
            status=ResolutionStatus.READY,
            rules_check_status=RuleCheckStatus.OK,
            capability_check_status=RuleCheckStatus.OK,
            resolved_segments=(),
            selected_fact_references=(),
            issues=(),
        )
        for iid in ids
    )
    cutoff = (
        intent.query_boundary.knowledge_as_of
        or intent.query_boundary.data_cutoff
    )
    request_start = intent.requested_window.start_date
    segments = tuple(
        InstrumentRuleSnapshotSegment(
            instrument_id=iid,
            effective_from=request_start,
            effective_to=None,
            normal_fact_reference=_VR(
                key="etf_rule_fact", version=1
            ),
            exception_fact_reference=None,
            normalized_values={},
            capability_declarations={},
            provenance={"normal_fact": {"fact_key": "etf_rule_fact"}},
            resolution_hash="c" * 64,
        )
        for iid in ids
    )
    bundle = RunRuleSnapshotBundle(
        rule_package_reference=intent.rule_package,
        rule_package_semantic_hash="a" * 64,
        parser_revision="rule-package-resolver@2",
        exception_set_reference=intent.rule_exception_set,
        exception_set_hash="b" * 64 if intent.rule_exception_set else None,
        data_cutoff=cutoff,
        instrument_segments=segments,
    )
    return RulePreflightReport(
        status=ResolutionStatus.READY,
        rule_package_reference=intent.rule_package,
        rule_package_semantic_hash="a" * 64,
        exception_set_reference=intent.rule_exception_set,
        exception_set_hash="b" * 64 if intent.rule_exception_set else None,
        data_cutoff=cutoff,
        start_date=intent.requested_window.start_date,
        end_date=intent.requested_window.end_date,
        checked_instruments=results,
        issues=(),
        snapshot_bundle=bundle,
        snapshot_hash=bundle.snapshot_hash,
    )


def admit(provider: MemoryDataProvider, intent: DataPreflightRequest) -> DataRequest:
    report = provider.preflight(intent)
    return DataRequest.from_admission(
        intent, report, rule_preflight_report=_rule_report_for(intent)
    )


def open_ready_session(provider: MemoryDataProvider, intent: DataPreflightRequest):
    request = admit(provider, intent)
    session = provider.open_session(request)
    report = session.preflight()
    assert report.status is PreflightStatus.READY, report.issues
    return session


def boundary(cutoff: datetime, include_cutoff_day: bool = False) -> QueryBoundary:
    return QueryBoundary(
        data_cutoff=cutoff, include_cutoff_day=include_cutoff_day
    )


CUTOFF_A = datetime(2026, 1, 8, 15, 0, tzinfo=TZ)


def build_fixture_a() -> MemoryDataProvider:
    """Warmup/gap fixture: opens Dec29-31, Jan2, Jan5-7; bars only Jan5+Jan7."""

    return MemoryDataProvider(
        build_dataset(
            facts_start=date(2025, 12, 26),
            facts_end=date(2026, 1, 8),
            open_days={D29, D30, D31, J2, J5, J6, J7},
            bar_days={J5, J7},
        )
    )


def build_fixture_b() -> MemoryDataProvider:
    """Warmup fixture with complete bars on every open day."""

    return MemoryDataProvider(
        build_dataset(
            facts_start=date(2025, 12, 26),
            facts_end=date(2026, 1, 8),
            open_days={D29, D30, D31, J2, J5, J6, J7},
        )
    )


def chunk_window(chunk) -> DateRange:
    """The inclusive date span covered by one opened chunk."""

    return DateRange(
        start_date=chunk._sessions[0].session_date,
        end_date=chunk._sessions[-1].session_date,
    )


def chunk_query(session, index: int) -> DataChunkQuery:
    sessions = session.resolved_sessions
    size = session._request.data_chunk_size_sessions
    start = index * size
    end = min(start + size, len(sessions))
    return DataChunkQuery(
        chunk_index=index,
        first_session_id=sessions[start].session_id,
        last_session_id=sessions[end - 1].session_id,
        # The fixture exercises both servable fact types: bar reads and
        # coverage accounting over those bars.
        fact_types=(DataCapability.BARS, DataCapability.COVERAGE),
    )


# ---------------------------------------------------------------------------
# Provider basic contract
# ---------------------------------------------------------------------------


class TestProviderBasics(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = build_fixture_b()
        self.session = open_ready_session(
            self.provider, make_intent(start=J5, end=J7)
        )
        self.chunk = self.session.open_chunk(chunk_query(self.session, 0))
        self.chunk.validate_consistency()

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self.provider, DataProvider)
        self.assertIsInstance(self.session, DataSession)
        self.assertIsInstance(self.chunk, DataChunkSession)

    def test_manifest_capabilities(self) -> None:
        manifest = self.provider.capability_manifest()
        self.assertEqual(
            set(manifest.capabilities),
            {DataCapability.CALENDARS, DataCapability.BARS, DataCapability.COVERAGE},
        )
        self.assertEqual(manifest.supported_frequencies, ("1d",))
        self.assertEqual(manifest.supported_price_bases, (PriceBasis.RAW,))
        self.assertEqual(
            manifest.consistency_modes,
            (
                ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
                ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
            ),
        )

    def test_external_mutation_does_not_affect_dataset(self) -> None:
        days = {J5, J6, J7}
        raw_bars = [make_bar(IID_A, J5)]
        raw_facts = [
            CalendarSessionFact(
                calendar_id=CAL_ID,
                session_date=day,
                is_open=True,
                definition_version=DEF_VERSION,
                source="fixture",
            )
            for day in every_day(J5, J7)
        ]
        dataset = MemoryDataSet(
            provider_key=PROVIDER_KEY,
            fixture_revision="fixture-v1",
            calendar_definitions=[make_definition()],
            calendar_facts=raw_facts,
            instruments=[make_spec(IID_A)],
            bars=raw_bars,
            clock=CLOCK,
        )
        raw_bars.append(make_bar(IID_A, J6))
        raw_facts.append(
            CalendarSessionFact(
                calendar_id=CAL_ID,
                session_date=D29,
                is_open=True,
                definition_version=DEF_VERSION,
                source="fixture",
            )
        )
        provider = MemoryDataProvider(dataset)
        session = open_ready_session(provider, make_intent(start=J5, end=J7))
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        rows = chunk.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(CUTOFF_A),
                window=DateRange(start_date=J5, end_date=J7),
            )
        )
        self.assertEqual([bar.trade_date for bar in rows], [J5])

    def test_results_are_immutable_tuples_sorted_by_date(self) -> None:
        rows = self.chunk.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(CUTOFF_A),
                window=DateRange(start_date=date(2025, 12, 26), end_date=J7),
            )
        )
        self.assertIsInstance(rows, tuple)
        dates = [bar.trade_date for bar in rows]
        self.assertEqual(dates, sorted(dates))

    def test_invalid_numbers_rejected(self) -> None:
        base = dict(
            instrument_id=IID_A,
            trade_date=J5,
            frequency="1d",
            volume="100",
            amount="1050",
            price_basis=PriceBasis.RAW,
            evidence=make_evidence(),
        )
        for bad in (
            float("nan"),
            float("inf"),
            float("-inf"),
            10.5,
            True,
        ):
            with self.assertRaises(ProviderContractViolationError):
                Bar(open=bad, high=bad, low=bad, close=bad, **base)

    def test_duplicate_bars_rejected(self) -> None:
        duplicate_bars = (
            make_bar(IID_A, J5),
            make_bar(IID_A, J5),
        )
        facts = tuple(
            CalendarSessionFact(
                calendar_id=CAL_ID,
                session_date=day,
                is_open=True,
                definition_version=DEF_VERSION,
                source="fixture",
            )
            for day in every_day(J5, J6)
        )
        with self.assertRaises(InvalidDataRequestError):
            MemoryDataSet(
                provider_key=PROVIDER_KEY,
                fixture_revision="fixture-v1",
                calendar_definitions=(make_definition(),),
                calendar_facts=facts,
                instruments=(make_spec(IID_A),),
                bars=duplicate_bars,
                clock=CLOCK,
            )

    def test_unsupported_capability_blocks_preflight(self) -> None:
        intent = make_intent(
            start=J5, end=J7, capabilities=(DataCapability.BARS, DataCapability.TICKS)
        )
        report = self.provider.preflight(intent)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(ISSUE_UNSUPPORTED_CAPABILITY, codes)
        with self.assertRaises(Exception):
            DataRequest.from_admission(
                intent, report, rule_preflight_report=_rule_report_for(intent)
            )

    def test_unsupported_capability_chunk_query(self) -> None:
        query = TickQuery(
            instrument_ids=IID_A,
            start_at=CUTOFF_A - timedelta(hours=1),
            end_at=CUTOFF_A,
            boundary=boundary(CUTOFF_A),
        )
        with self.assertRaises(UnsupportedCapabilityError) as caught:
            self.chunk.ticks(query)
        self.assertEqual(caught.exception.code, "unsupported_capability")

    def test_provider_key_mismatch_blocked(self) -> None:
        intent = make_intent(start=J5, end=J7, provider_key="someone-else")
        report = self.provider.preflight(intent)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(ISSUE_PROVIDER_KEY_MISMATCH, codes)

    def test_canonical_preflight_runs_common_admission_gates_before_snapshot(self) -> None:
        """Canonical dispatch must not let invalid requests reach the snapshot API."""

        provider = build_fixture_a()
        base = make_intent(start=J5, end=J7)
        cases = (
            (
                "provider key",
                replace(base, provider_key="someone-else"),
                ISSUE_PROVIDER_KEY_MISMATCH,
            ),
            (
                "instrument",
                replace(base, static_instrument_ids=(IID_B,)),
                ISSUE_INSTRUMENT_NOT_FOUND,
            ),
            (
                "frequency",
                replace(base, frequency="1m"),
                ISSUE_UNSUPPORTED_FREQUENCY,
            ),
            (
                "price basis",
                replace(base, strategy_price_bases=(PriceBasis.QFQ,)),
                ISSUE_UNSUPPORTED_PRICE_BASIS,
            ),
            (
                "consistency mode",
                replace(
                    base,
                    consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
                ),
                ISSUE_UNSUPPORTED_TOKEN_CONTRACT,
            ),
            (
                "capability",
                replace(base, required_capabilities=(DataCapability.TICKS,)),
                ISSUE_UNSUPPORTED_CAPABILITY,
            ),
            (
                "scope",
                replace(
                    base,
                    instrument_scope_mode=InstrumentScopeMode.DYNAMIC,
                    static_instrument_ids=(),
                    universe_query_policy=UniverseQueryPolicy(
                        candidate_set_rules=(RULES,)
                    ),
                ),
                ISSUE_UNSUPPORTED_CAPABILITY,
            ),
        )
        calendar_provider = provider.dataset.calendar_axis_provider
        with patch.object(
            provider, "_has_canonical_calendar_metadata", return_value=True
        ), patch.object(
            calendar_provider,
            "open_calendar_snapshot",
            side_effect=AssertionError(
                "invalid canonical requests must stop before snapshot"
            ),
        ):
            for label, intent, expected_code in cases:
                with self.subTest(label=label):
                    report = provider.preflight(intent)
                    self.assertIs(report.status, PreflightStatus.BLOCKED)
                    self.assertIn(
                        expected_code, {issue.code for issue in report.issues}
                    )

    def test_unknown_instrument_blocked(self) -> None:
        intent = make_intent(start=J5, end=J7, static=(IID_B,))
        report = self.provider.preflight(intent)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(ISSUE_INSTRUMENT_NOT_FOUND, codes)

    def test_strict_report_definitions_are_bounded_to_snapshot(self) -> None:
        """The report must expose only definitions proven by the strict snapshot."""

        known_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cutoff = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
        priority = CalendarSourcePriority(
            source="official",
            source_priority_version="v1",
            source_priority=1,
            source_revision_order=1,
            source_revision="r1",
            valid_from=date(2020, 1, 1),
            known_at=known_at,
            observed_at=known_at,
            evidence={},
            bootstrap_seed_hash="a" * 64,
        )
        common = {
            "source": "official",
            "source_revision": "r1",
            "known_at": known_at,
            "observed_at": known_at,
            "source_priority_fact_id": priority.fact_id,
            "source_priority_version": "v1",
            "source_priority": 1,
            "source_revision_order": 1,
            "bootstrap_seed_id": "calendar-source-priority-bootstrap",
            "bootstrap_seed_version": 1,
            "bootstrap_seed_hash": "a" * 64,
            "evidence": {},
        }
        registry = CalendarRegistry(
            "SSE",
            "Shanghai Stock Exchange",
            registry_version=1,
            valid_from=date(2020, 1, 1),
            **common,
        )
        definition = CalendarDefinition(
            "SSE",
            "sse-v1",
            "Asia/Shanghai",
            ((time(9, 30), time(15, 0)),),
            valid_from=date(2020, 1, 1),
            registry_fact_id=registry.fact_id,
            registry_version=1,
            **common,
        )
        future = replace(
            definition,
            definition_version="sse-future",
            valid_from=date(2030, 1, 1),
            logical_fact_key="calendar_definition:SSE:future",
            fact_id=None,
            content_hash=None,
        )
        after_cutoff = replace(
            definition,
            definition_version="sse-after-cutoff",
            known_at=datetime(2026, 1, 11, tzinfo=timezone.utc),
            knowledge_from=datetime(2026, 1, 11, tzinfo=timezone.utc),
            logical_fact_key="calendar_definition:SSE:after-cutoff",
            fact_id=None,
            content_hash=None,
        )
        quarantined = replace(
            definition,
            definition_version="sse-quarantined",
            quality_status=CalendarQualityStatus.QUARANTINED,
            logical_fact_key="calendar_definition:SSE:quarantined",
            fact_id=None,
            content_hash=None,
        )
        open_days = {D29, D30, D31, J2, J5, J6, J7}
        facts = tuple(
            CalendarSessionFact(
                "SSE",
                day,
                day in open_days,
                definition_version="sse-v1",
                registry_fact_id=registry.fact_id,
                registry_version=1,
                definition_fact_id=definition.fact_id,
                **common,
            )
            for day in every_day(D26, date(2026, 1, 8))
        )
        dataset = MemoryDataSet(
            provider_key=PROVIDER_KEY,
            fixture_revision="strict-fixture-v1",
            calendar_definitions=(definition, future, after_cutoff, quarantined),
            calendar_facts=facts,
            instruments=(replace(make_spec(IID_A), calendar_id="SSE"),),
            bars=(make_bar(IID_A, J2),),
            clock=CLOCK,
            calendar_registries=(registry,),
            calendar_source_priorities=(priority,),
        )
        intent = replace(
            make_intent(
                start=J5,
                end=J7,
                warmup=2,
                static=(IID_A,),
                provider_key=PROVIDER_KEY,
            ),
            query_boundary=QueryBoundary(
                data_cutoff=cutoff,
                include_cutoff_day=True,
            ),
        )
        report = MemoryDataProvider(dataset).preflight(intent)
        self.assertIs(report.status, PreflightStatus.READY)
        self.assertEqual(
            [item.definition_version for item in report.resolved_calendar_definitions],
            ["sse-v1"],
        )
        self.assertIsNotNone(report.warmup_resolution)
        assert report.warmup_resolution is not None
        # The history window covers every natural day in the snapshot envelope,
        # including the closed Jan 1 holiday between warmup sessions.
        self.assertEqual(
            report.warmup_resolution.history_window,
            DateRange(start_date=D31, end_date=J5 - timedelta(days=1)),
        )


# ---------------------------------------------------------------------------
# 512-session lookback and data-cutoff visibility
# ---------------------------------------------------------------------------

LONG_START = date(2023, 1, 2)
LONG_END = date(2026, 3, 31)
LONG_CUTOFF = datetime(2025, 6, 2, 15, 0, tzinfo=TZ)


class TestLookbackAndCutoff(unittest.TestCase):
    def setUp(self) -> None:
        open_days = set(weekdays(date(2022, 12, 26), LONG_END))
        self.provider = MemoryDataProvider(
            build_dataset(
                facts_start=date(2022, 12, 26),
                facts_end=LONG_END,
                open_days=open_days,
            )
        )

    def test_lookback_512_succeeds_on_complete_history(self) -> None:
        session = open_ready_session(
            self.provider, make_intent(start=LONG_START, end=LONG_END)
        )
        # Open the chunk that owns the last eligible session: reads are
        # bounded by the current chunk, so the lookback must draw on warmup,
        # earlier formal sessions, and this chunk only.
        sessions = session.resolved_sessions
        eligible = [
            point for point in sessions if point.session_date < LONG_CUTOFF.date()
        ]
        anchor_index = sessions.index(eligible[-1])
        chunk = session.open_chunk(chunk_query(session, anchor_index // 20))
        chunk.validate_consistency()
        before = self.provider.read_count
        rows = chunk.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(LONG_CUTOFF),
                window=LookbackWindow(sessions=512, end_at=LONG_CUTOFF),
            )
        )
        self.assertEqual(len(rows), 512)
        dates = [bar.trade_date for bar in rows]
        self.assertEqual(dates, sorted(dates))
        self.assertGreater(self.provider.read_count, before)

    def test_lookback_513_fails_before_any_index_read(self) -> None:
        before = self.provider.read_count
        with self.assertRaises(LookbackSessionsLimitExceededError):
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(LONG_CUTOFF),
                window=LookbackWindow(sessions=513, end_at=LONG_CUTOFF),
            )
        self.assertEqual(self.provider.read_count, before)

    def test_data_cutoff_exceeded_fails_instead_of_trimming(self) -> None:
        provider = build_fixture_b()
        session = open_ready_session(provider, make_intent(start=J5, end=J7))
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        with self.assertRaises(DataCutoffExceededError):
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(CUTOFF_A),
                window=DateRange(start_date=J5, end_date=date(2026, 8, 22)),
            )
        # Touching the incomplete cutoff day without explicit confirmation
        # fails too; nothing is silently trimmed to Aug/Jan boundaries.
        with self.assertRaises(DataCutoffExceededError):
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(CUTOFF_A),
                window=DateRange(start_date=J5, end_date=CUTOFF_A.date()),
            )
        self.assertEqual(provider.read_count, 0)

    def test_future_bar_injection_is_intercepted(self) -> None:
        class LeakyProvider(MemoryDataProvider):
            def _raw_bars(self, instrument_ids, frequency, start_day, end_day):
                rows = super()._raw_bars(instrument_ids, frequency, start_day, end_day)
                leak = make_bar(IID_A, end_day + timedelta(days=10))
                return tuple(rows) + (leak,)

        leaky = LeakyProvider(build_fixture_b().dataset)
        session = open_ready_session(leaky, make_intent(start=J5, end=J7))
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        before = leaky.read_count
        with self.assertRaises(ProviderContractViolationError) as caught:
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(CUTOFF_A),
                    window=DateRange(start_date=J5, end_date=J7),
                )
            )
        self.assertEqual(caught.exception.code, "provider_contract_violation")
        # The index was accessed (the leak happened at the source) but no
        # future fact reached the caller.
        self.assertGreater(leaky.read_count, before)

    def test_window_shapes_cannot_be_mixed(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(CUTOFF_A),
                window=None,
            )


# ---------------------------------------------------------------------------
# Gaps and warmup isolation
# ---------------------------------------------------------------------------


class TestGapsAndWarmup(unittest.TestCase):
    def test_explicit_range_gap_is_preserved(self) -> None:
        provider = build_fixture_a()
        session = open_ready_session(provider, make_intent(start=J5, end=J7))
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        rows = chunk.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(CUTOFF_A),
                window=DateRange(start_date=J5, end_date=J7),
            )
        )
        self.assertEqual([bar.trade_date for bar in rows], [J5, J7])

    def test_full_lookback_with_gap_blocks_as_history_incomplete(self) -> None:
        provider = build_fixture_a()
        session = open_ready_session(provider, make_intent(start=J5, end=J7))
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        before = provider.read_count
        with self.assertRaises(HistoryIncompleteError) as caught:
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(datetime(2026, 1, 7, 23, 59, tzinfo=TZ)),
                    window=LookbackWindow(
                        sessions=3,
                        end_at=datetime(2026, 1, 7, 23, 59, tzinfo=TZ),
                    ),
                )
            )
        self.assertEqual(caught.exception.code, "history_incomplete")
        # The window failed before any bar-index access: nothing was read
        # and nothing was silently shortened.
        self.assertEqual(provider.read_count, before)

    def test_warmup_sessions_are_reachable_by_history_queries(self) -> None:
        provider = build_fixture_b()
        session = open_ready_session(
            provider, make_intent(start=J5, end=J7, warmup=2)
        )
        self.assertEqual(
            [point.session_date for point in session.warmup_sessions], [D31, J2]
        )
        first = session.resolved_sessions[0]
        self.assertEqual(first.session_date, J5)
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        rows = chunk.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(CUTOFF_A),
                window=LookbackWindow(
                    sessions=2, end_at=datetime(2026, 1, 5, 23, 59, tzinfo=TZ)
                ),
            )
        )
        self.assertEqual([bar.trade_date for bar in rows], [D31, J2])

    def test_warmup_never_enters_formal_chunks_or_numbering(self) -> None:
        provider = build_fixture_b()
        session = open_ready_session(
            provider, make_intent(start=J5, end=J7, warmup=2)
        )
        warmup_ids = {point.session_id for point in session.warmup_sessions}
        formal_ids = [point.session_id for point in session.resolved_sessions]
        self.assertFalse(warmup_ids & set(formal_ids))
        # A chunk anchored on a warmup session can never be opened.
        with self.assertRaises(InvalidDataRequestError):
            session.open_chunk(
                DataChunkQuery(
                    chunk_index=0,
                    first_session_id=session.warmup_sessions[0].session_id,
                    last_session_id=formal_ids[-1],
                    fact_types=(DataCapability.BARS,),
                )
            )
        # The only legal chunk covers exactly the three formal sessions.
        chunk = session.open_chunk(chunk_query(session, 0))
        self.assertEqual(len(chunk._sessions), 3)

    def test_insufficient_warmup_blocks_preflight(self) -> None:
        provider = build_fixture_a()
        intent = make_intent(start=J5, end=J7, warmup=10)
        report = provider.preflight(intent)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(WARMUP_COVERAGE_INSUFFICIENT, codes)

    def test_mandatory_coverage_gap_blocks_strict_preflight(self) -> None:
        provider = build_fixture_a()
        intent = make_intent(start=J5, end=J7, mandatory=(IID_A,))
        report = provider.preflight(intent)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(ISSUE_MANDATORY_BAR_COVERAGE_MISSING, codes)

    def test_coverage_report_counts_gaps(self) -> None:
        provider = build_fixture_a()
        session = open_ready_session(provider, make_intent(start=J5, end=J7))
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        report = chunk.coverage(
            CoverageQuery(
                capability=DataCapability.BARS,
                instrument_ids=IID_A,
                window=DateRange(start_date=J5, end_date=J7),
                boundary=boundary(CUTOFF_A),
            )
        )
        self.assertEqual(report.expected_count, 3)
        self.assertEqual(report.complete_count, 2)
        self.assertEqual(report.unavailable_count, 1)
        self.assertEqual(
            [(item.start_date, item.end_date) for item in report.missing_ranges],
            [(J6, J6)],
        )
        self.assertIs(report.quality_status, QualityStatus.PARTIAL)


# ---------------------------------------------------------------------------
# Chunks and consistency
# ---------------------------------------------------------------------------


class TestChunksAndConsistency(unittest.TestCase):
    def build_45(self):
        open_days = set(weekdays(date(2025, 10, 6), date(2025, 12, 5)))
        self.assertEqual(len(open_days), 45)
        provider = MemoryDataProvider(
            build_dataset(
                facts_start=date(2025, 10, 4),
                facts_end=date(2025, 12, 7),
                open_days=open_days,
            )
        )
        session = open_ready_session(
            provider,
            make_intent(start=date(2025, 10, 6), end=date(2025, 12, 5)),
        )
        return provider, session

    def test_20_formal_sessions_yield_one_chunk(self) -> None:
        open_days = set(weekdays(date(2025, 10, 6), date(2025, 10, 31)))
        self.assertEqual(len(open_days), 20)
        provider = MemoryDataProvider(
            build_dataset(
                facts_start=date(2025, 10, 4),
                facts_end=date(2025, 11, 2),
                open_days=open_days,
            )
        )
        session = open_ready_session(
            provider,
            make_intent(start=date(2025, 10, 6), end=date(2025, 10, 31)),
        )
        chunk = session.open_chunk(chunk_query(session, 0))
        self.assertEqual(len(chunk._sessions), 20)
        with self.assertRaises(InvalidDataRequestError):
            session.open_chunk(
                DataChunkQuery(
                    chunk_index=1,
                    first_session_id="2025-10-06",
                    last_session_id="2025-10-31",
                    fact_types=(DataCapability.BARS,),
                )
            )

    def test_45_formal_sessions_yield_20_20_5(self) -> None:
        _, session = self.build_45()
        sizes = []
        for index in range(3):
            chunk = session.open_chunk(chunk_query(session, index))
            sizes.append(len(chunk._sessions))
            status = chunk.validate_consistency()
            self.assertIs(status.status, ConsistencyValidation.VALID)
        self.assertEqual(sizes, [20, 20, 5])
        with self.assertRaises(InvalidDataRequestError):
            session.open_chunk(
                DataChunkQuery(
                    chunk_index=3,
                    first_session_id="2025-10-06",
                    last_session_id="2025-12-05",
                    fact_types=(DataCapability.BARS,),
                )
            )

    def test_illegal_boundaries_cannot_open(self) -> None:
        _, session = self.build_45()
        sessions = session.resolved_sessions
        cases = [
            DataChunkQuery(
                chunk_index=99,
                first_session_id=sessions[0].session_id,
                last_session_id=sessions[-1].session_id,
                fact_types=(DataCapability.BARS,),
            ),
            DataChunkQuery(
                chunk_index=0,
                first_session_id=sessions[1].session_id,
                last_session_id=sessions[19].session_id,
                fact_types=(DataCapability.BARS,),
            ),
            DataChunkQuery(
                chunk_index=1,
                first_session_id=sessions[0].session_id,
                last_session_id=sessions[39].session_id,
                fact_types=(DataCapability.BARS,),
            ),
        ]
        for query in cases:
            with self.assertRaises(InvalidDataRequestError):
                session.open_chunk(query)

    def test_business_query_requires_prior_validation(self) -> None:
        provider, session = self.build_45()
        chunk = session.open_chunk(chunk_query(session, 0))
        before = provider.read_count
        with self.assertRaises(ConsistencyNotValidatedError) as caught:
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(datetime(2025, 12, 7, 15, 0, tzinfo=TZ)),
                    window=DateRange(
                        start_date=date(2025, 10, 6), end_date=date(2025, 12, 5)
                    ),
                )
            )
        self.assertEqual(caught.exception.code, "consistency_not_validated")
        self.assertEqual(provider.read_count, before)

    def test_expired_token_blocks_all_reads(self) -> None:
        provider, session = self.build_45()
        chunk = session.open_chunk(chunk_query(session, 0))
        self.assertIs(
            chunk.validate_consistency().status, ConsistencyValidation.VALID
        )
        provider.invalidate_revision()
        status = chunk.validate_consistency()
        self.assertIn(
            status.status,
            (ConsistencyValidation.EXPIRED, ConsistencyValidation.INVALID),
        )
        before = provider.read_count
        with self.assertRaises(Exception) as caught:
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(datetime(2025, 12, 7, 15, 0, tzinfo=TZ)),
                    window=chunk_window(chunk),
                )
            )
        self.assertIsInstance(caught.exception, ConsistencyTokenExpiredError)
        self.assertEqual(provider.read_count, before)
        evidence = chunk.consistency_evidence
        self.assertIsNotNone(evidence.token_digest)
        self.assertNotEqual(evidence.token_digest, "")

    def test_coverage_incomplete_token_blocks_reads(self) -> None:
        provider = MemoryDataProvider(
            build_dataset(
                facts_start=date(2025, 12, 26),
                facts_end=date(2026, 1, 8),
                open_days={D29, D30, D31, J2, J5, J6, J7},
                bar_days=set(),
            )
        )
        session = open_ready_session(provider, make_intent(start=J5, end=J7))
        chunk = session.open_chunk(chunk_query(session, 0))
        status = chunk.validate_consistency()
        self.assertIs(status.status, ConsistencyValidation.COVERAGE_INCOMPLETE)
        with self.assertRaises(ConsistencyCoverageIncompleteError):
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(CUTOFF_A),
                    window=DateRange(start_date=J5, end_date=J7),
                )
            )

    def test_closed_session_and_chunk_block_everything(self) -> None:
        provider, session = self.build_45()
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.close()
        with self.assertRaises(DataSessionClosedError):
            chunk.validate_consistency()

        session2 = open_ready_session(
            provider,
            make_intent(start=date(2025, 10, 6), end=date(2025, 12, 5)),
        )
        session2.close()
        with self.assertRaises(DataSessionClosedError):
            session2.open_chunk(chunk_query(session2, 0))

        session3 = open_ready_session(
            provider,
            make_intent(start=date(2025, 10, 6), end=date(2025, 12, 5)),
        )
        chunk3 = session3.open_chunk(chunk_query(session3, 0))
        chunk3.validate_consistency()
        session3.close()
        with self.assertRaises(DataSessionClosedError):
            chunk3.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(datetime(2025, 12, 7, 15, 0, tzinfo=TZ)),
                    window=DateRange(
                        start_date=date(2025, 10, 6), end_date=date(2025, 12, 5)
                    ),
                )
            )

    def test_chunk_access_before_preflight_fails(self) -> None:
        provider, _ = self.build_45()
        request = admit(
            provider,
            make_intent(start=date(2025, 10, 6), end=date(2025, 12, 5)),
        )
        fresh = provider.open_session(request)
        with self.assertRaises(InvalidDataRequestError):
            fresh.open_chunk(chunk_query(fresh, 0))
        with self.assertRaises(InvalidDataRequestError):
            fresh.resolved_sessions

    def test_chunk_cannot_read_beyond_its_own_end(self) -> None:
        provider, session = self.build_45()
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        last_day = chunk._sessions[-1].session_date
        before = provider.read_count

        # An explicit range may not extend past this chunk's last session.
        with self.assertRaises(InvalidDataRequestError):
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(datetime(2025, 12, 7, 15, 0, tzinfo=TZ)),
                    window=DateRange(
                        start_date=last_day, end_date=last_day + timedelta(days=1)
                    ),
                )
            )
        # A lookback cannot reach into later chunks either.
        with self.assertRaises(HistoryIncompleteError):
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(datetime(2025, 12, 7, 15, 0, tzinfo=TZ)),
                    window=LookbackWindow(
                        sessions=21,
                        end_at=datetime(2025, 12, 7, 15, 0, tzinfo=TZ),
                    ),
                )
            )
        self.assertEqual(provider.read_count, before)
        # Exactly the current chunk's sessions remain reachable.
        rows = chunk.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=boundary(datetime(2025, 12, 7, 15, 0, tzinfo=TZ)),
                window=LookbackWindow(
                    sessions=20, end_at=datetime(2025, 12, 7, 15, 0, tzinfo=TZ)
                ),
            )
        )
        self.assertEqual(len(rows), 20)

    def test_coverage_window_is_chunk_bounded(self) -> None:
        provider, session = self.build_45()
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        with self.assertRaises(InvalidDataRequestError):
            chunk.coverage(
                CoverageQuery(
                    capability=DataCapability.BARS,
                    instrument_ids=IID_A,
                    window=DateRange(
                        start_date=date(2025, 10, 6), end_date=date(2025, 12, 5)
                    ),
                    boundary=boundary(datetime(2025, 12, 7, 15, 0, tzinfo=TZ)),
                )
            )

    def test_lookback_completeness_judges_returned_rows(self) -> None:
        class DroppingProvider(MemoryDataProvider):
            def _raw_bars(self, instrument_ids, frequency, start_day, end_day):
                rows = list(
                    super()._raw_bars(instrument_ids, frequency, start_day, end_day)
                )
                return [bar for bar in rows if bar.trade_date != J5]

        dropping = DroppingProvider(build_fixture_b().dataset)
        session = open_ready_session(
            dropping, make_intent(start=J5, end=J7, warmup=2)
        )
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        before = dropping.read_count
        with self.assertRaises(HistoryIncompleteError) as caught:
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(CUTOFF_A),
                    window=LookbackWindow(
                        sessions=3, end_at=datetime(2026, 1, 6, 23, 59, tzinfo=TZ)
                    ),
                )
            )
        self.assertEqual(caught.exception.code, "history_incomplete")
        self.assertGreater(dropping.read_count, before)

    def test_wrong_instrument_injection_is_rejected(self) -> None:
        class LeakyProvider(MemoryDataProvider):
            def _raw_bars(self, instrument_ids, frequency, start_day, end_day):
                rows = super()._raw_bars(instrument_ids, frequency, start_day, end_day)
                stranger = make_bar(IID_B, J6)
                return tuple(rows) + (stranger,)

        leaky = LeakyProvider(build_fixture_b().dataset)
        session = open_ready_session(leaky, make_intent(start=J5, end=J7))
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        with self.assertRaises(ProviderContractViolationError) as caught:
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(CUTOFF_A),
                    window=chunk_window(chunk),
                )
            )
        self.assertEqual(caught.exception.code, "provider_contract_violation")

    def test_session_preflight_accepts_equal_raw_intent_and_frozen_calendar(self) -> None:
        provider = build_fixture_b()
        intent = make_intent(start=J5, end=J7, warmup=2)
        request = admit(provider, intent)
        session = provider.open_session(request)
        # A different intent is rejected before preflight even runs...
        with self.assertRaises(InvalidDataRequestError):
            session.preflight(make_intent(start=D30, end=J7, warmup=2))
        # ...while the original unresolved intent (same business fields,
        # no admission fields) is accepted for the authoritative re-check.
        report = session.preflight(intent)
        self.assertIs(report.status, PreflightStatus.READY)
        # The frozen calendar set from admission stays authoritative.
        self.assertEqual(report.resolved_calendar_ids, request.resolved_calendar_ids)

    def test_canonical_session_preflight_passes_frozen_calendar_ids(self) -> None:
        provider = build_fixture_b()
        intent = make_intent(start=J5, end=J7)
        request = admit(provider, intent)
        session = provider.open_session(request)
        observed_requests: list[object] = []

        def fail_open(*args: object) -> None:
            observed_requests.append(args[-1])
            raise CalendarDefinitionMissingError("calendar lookup is intentionally blocked")

        # Force this legacy fixture through the canonical dispatch branch.  If
        # the builder re-derives IDs from instrument specs, this guard fails.
        with patch.object(provider, "_has_canonical_calendar_metadata", return_value=True), patch.object(
            MemoryDataSet,
            "instrument",
            side_effect=AssertionError("canonical preflight must use frozen IDs"),
        ), patch.object(
            type(provider.dataset.calendar_axis_provider),
            "open_calendar_snapshot",
            side_effect=fail_open,
        ):
            report = session.preflight()

        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertEqual(
            observed_requests[0].calendar_ids,
            tuple(calendar_id.upper() for calendar_id in request.resolved_calendar_ids),
        )

    def test_blocked_report_with_ready_warmup_resolution_stays_valid(self) -> None:
        # Warmup resolves fine, but mandatory coverage fails afterwards:
        # the report must come out blocked instead of raising an internal
        # invalid_data_request while mounting a ready warmup resolution.
        provider = MemoryDataProvider(
            build_dataset(
                facts_start=date(2025, 12, 26),
                facts_end=date(2026, 1, 8),
                open_days={D29, D30, D31, J2, J5, J6, J7},
                bar_days={J5, J7},
            )
        )
        intent = make_intent(start=J5, end=J7, warmup=2, mandatory=(IID_A,))
        report = provider.preflight(intent)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(ISSUE_MANDATORY_BAR_COVERAGE_MISSING, codes)
        self.assertEqual(report.warmup_sessions, ())
        with self.assertRaises(DataPreflightBlockedError):
            DataRequest.from_admission(
                intent, report, rule_preflight_report=_rule_report_for(intent)
            )

    def test_manifest_stays_daily_even_with_foreign_frequency_bars(self) -> None:
        # A dataset that smuggles in a 1m bar is a fixture bug: the provider
        # rejects it instead of advertising ("1d", "1m") support.
        ds = build_dataset(
            facts_start=date(2025, 12, 26),
            facts_end=date(2026, 1, 8),
            open_days={J5, J6, J7},
        )
        one_m = Bar(
            instrument_id=IID_A,
            trade_date=J5,
            frequency="1m",
            open="10",
            high="11",
            low="9",
            close="10.5",
            volume="1",
            amount="10",
            price_basis=PriceBasis.RAW,
            evidence=make_evidence(),
        )
        mixed = MemoryDataSet(
            provider_key=PROVIDER_KEY,
            fixture_revision="fixture-v1",
            calendar_definitions=ds.calendar_definitions,
            calendar_facts=ds.calendar_facts,
            instruments=ds.instruments,
            bars=tuple(ds.bars) + (one_m,),
            clock=CLOCK,
        )
        with self.assertRaises(InvalidDataRequestError):
            MemoryDataProvider(mixed)

    def test_dynamic_scope_yields_blocked_report_not_internal_error(self) -> None:
        provider = build_fixture_b()
        intent = DataPreflightRequest(
            provider_key=PROVIDER_KEY,
            requested_window=DateRange(start_date=J5, end_date=J7),
            frequency="1d",
            rule_package=RULES,
            market_scope=MarketScope(),
            universe_query_policy=UniverseQueryPolicy(
                candidate_set_rules=(ContractRef(key="u.rule", version=1),)
            ),
            instrument_scope_mode=InstrumentScopeMode.DYNAMIC,
            required_capabilities=(DataCapability.BARS,),
            strategy_price_bases=(PriceBasis.RAW,),
            consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
            consistency_token_contract=TOKEN_CONTRACT,
            query_boundary=boundary(CUTOFF_A),
        )
        report = provider.preflight(intent)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(ISSUE_UNSUPPORTED_CAPABILITY, codes)
        scopes = {
            issue.scope for issue in report.issues if issue.code == ISSUE_UNSUPPORTED_CAPABILITY
        }
        self.assertIn("instrument_scope", scopes)

    def test_lookback_future_bar_injection_is_intercepted(self) -> None:
        class LeakyProvider(MemoryDataProvider):
            def _raw_bars(self, instrument_ids, frequency, start_day, end_day):
                rows = super()._raw_bars(instrument_ids, frequency, start_day, end_day)
                return tuple(rows) + (make_bar(IID_A, date(2026, 12, 31)),)

        leaky = LeakyProvider(build_fixture_b().dataset)
        session = open_ready_session(
            leaky, make_intent(start=J5, end=J7, warmup=2)
        )
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        with self.assertRaises(ProviderContractViolationError):
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=boundary(CUTOFF_A),
                    window=LookbackWindow(
                        sessions=3,
                        end_at=datetime(2026, 1, 7, 23, 59, tzinfo=CUTOFF_A.tzinfo),
                    ),
                )
            )

    def test_queries_outside_frozen_scope_are_rejected(self) -> None:
        provider = MemoryDataProvider(
            build_dataset(
                facts_start=date(2025, 12, 26),
                facts_end=date(2026, 1, 8),
                open_days={J5, J6, J7},
                instruments=(IID_A, IID_B),
            )
        )
        session = open_ready_session(provider, make_intent(start=J5, end=J7))
        self.assertEqual(session._request.static_instrument_ids, (IID_A,))
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        for operation in (
            lambda: chunk.bars(
                BarQuery(
                    instrument_ids=IID_B,
                    frequency="1d",
                    boundary=boundary(CUTOFF_A),
                    window=DateRange(start_date=J5, end_date=J7),
                )
            ),
            lambda: chunk.instruments(
                InstrumentQuery(
                    instrument_ids=IID_B,
                    effective_at=CUTOFF_A,
                    boundary=boundary(CUTOFF_A),
                )
            ),
            lambda: chunk.coverage(
                CoverageQuery(
                    capability=DataCapability.BARS,
                    instrument_ids=(IID_A, IID_B),
                    window=DateRange(start_date=J5, end_date=J7),
                    boundary=boundary(CUTOFF_A),
                )
            ),
        ):
            with self.assertRaises(InvalidDataRequestError):
                operation()

    def test_closed_before_preflight_properties_fail_stably(self) -> None:
        provider = build_fixture_b()
        request = admit(provider, make_intent(start=J5, end=J7))
        session = provider.open_session(request)
        session.close()
        for prop in ("resolved_sessions", "warmup_sessions"):
            with self.assertRaises(DataSessionClosedError):
                getattr(session, prop)

    def test_preflight_rejects_non_request_object(self) -> None:
        provider = build_fixture_b()
        request = admit(provider, make_intent(start=J5, end=J7))
        session = provider.open_session(request)
        with self.assertRaises(InvalidDataRequestError):
            session.preflight(object())

    def test_blocked_report_cannot_be_admitted(self) -> None:
        provider = build_fixture_a()
        intent = make_intent(start=J5, end=J7, warmup=10)
        report = provider.preflight(intent)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        with self.assertRaises(DataPreflightBlockedError):
            DataRequest.from_admission(
                intent, report, rule_preflight_report=_rule_report_for(intent)
            )


# ---------------------------------------------------------------------------
# Acceptance flow from the task package
# ---------------------------------------------------------------------------


class TestAcceptanceFlow(unittest.TestCase):
    def test_end_to_end_example_flow(self) -> None:
        provider = build_fixture_b()
        intent = make_intent(start=J5, end=J7, warmup=2)
        admission = provider.preflight(intent)
        self.assertIs(admission.status, PreflightStatus.READY)
        request = DataRequest.from_admission(
            intent,
            admission,
            rule_preflight_report=_rule_report_for(intent),
        )
        with provider.open_session(request) as session:
            report = session.preflight()
            self.assertIs(report.status, PreflightStatus.READY)
            self.assertEqual(len(session.warmup_sessions), 2)
            self.assertEqual(session.resolved_sessions[0].session_date, J5)

            first = session.resolved_sessions[:20]
            query = DataChunkQuery(
                chunk_index=0,
                first_session_id=first[0].session_id,
                last_session_id=first[-1].session_id,
                fact_types=(DataCapability.BARS,),
            )
            with session.open_chunk(query) as chunk:
                status = chunk.validate_consistency()
                self.assertIs(status.status, ConsistencyValidation.VALID)
                bars = chunk.bars(
                    BarQuery(
                        instrument_ids=IID_A,
                        frequency="1d",
                        boundary=boundary(CUTOFF_A),
                        window=DateRange(start_date=J5, end_date=J7),
                    )
                )
                self.assertEqual(
                    [bar.trade_date for bar in bars], [J5, J6, J7]
                )
                self.assertEqual(status.covered_chunk, 0)
        # Context-manager exit closed the session; further chunks fail.
        with self.assertRaises(DataSessionClosedError):
            session.open_chunk(query)


if __name__ == "__main__":
    unittest.main()
