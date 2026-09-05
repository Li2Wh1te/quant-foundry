"""Task package 03-03/03-04: bounded fixed-session chunks and consistency tokens.

Covers the ``fixed_trading_sessions@1`` boundary sizes (0..43 formal
sessions, tail chunks below 20), warmup exclusion from chunk numbering,
cross-chunk run-state continuity, the bounded logical-token lifecycle
(valid / expired / coverage-incomplete / unsupported fact types), the
transitional repeatable-read mode (no token digest, pinned revision
vector, marked resource risk), and digest-only persistence of token
evidence.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from app.backtesting.calendar_axis import (
    CalendarDefinition,
    CalendarSessionFact,
)
from app.backtesting.data.errors import (
    ConsistencyCoverageIncompleteError,
    ConsistencyTokenExpiredError,
    DataSessionClosedError,
    InvalidDataRequestError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.memory import (
    ISSUE_UNSUPPORTED_TOKEN_CONTRACT,
    MemoryDataSet,
    MemoryDataProvider,
)
from app.backtesting.data.protocols import CoverageEnvelope
from app.backtesting.data.requests import (
    BarQuery,
    ConsistencyMode,
    ConsistencyValidation,
    ContractRef,
    DataCapability,
    DataChunkQuery,
    DataPreflightRequest,
    DateRange,
    InstrumentScopeMode,
    MarketScope,
    PreflightStatus,
    PriceBasis,
    QueryBoundary,
    QualityStatus,
    CoverageQuery,
    UniverseQueryPolicy,
)
from app.backtesting.data.facts import Bar, FactEvidence
from app.backtesting.time_axis import (
    FixedTradingSessionsV1,
    SESSIONS_PER_CHUNK_V1,
    TimeStep,
)
from app.instruments.domain import (
    CorporateActionRequirement,
    InstrumentCapabilities,
    InstrumentDisplay,
    InstrumentSpec,
    VersionedReference,
)
from zoneinfo import ZoneInfo

# Reuse the established fixture builders instead of duplicating them.
from tests.test_backtesting_memory_provider import (
    CLOCK,
    CAL_ID,
    DEF_VERSION,
    IID_A,
    PROVIDER_KEY,
    RULES,
    SESSION_WINDOWS,
    TOKEN_CONTRACT,
    TZ,
    admit,
    build_dataset,
    chunk_query,
    every_day,
    make_bar,
    make_definition,
    make_evidence,
    make_intent,
    make_spec,
    open_ready_session,
    weekdays,
)

CUTOFF = datetime(2026, 3, 16, 15, 0, tzinfo=TZ)


def transitional_intent(
    *,
    start: date,
    end: date,
    warmup: int = 0,
    static: tuple[UUID, ...] = (IID_A,),
    token_contract: ContractRef | None = None,
) -> DataPreflightRequest:
    """A fixed-scope intent frozen to ``transitional_repeatable_read``."""

    return DataPreflightRequest(
        provider_key=PROVIDER_KEY,
        requested_window=DateRange(start_date=start, end_date=end),
        frequency="1d",
        rule_package=RULES,
        market_scope=MarketScope(),
        universe_query_policy=UniverseQueryPolicy(),
        instrument_scope_mode=InstrumentScopeMode.FIXED,
        required_capabilities=(DataCapability.BARS,),
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        consistency_token_contract=token_contract,
        query_boundary=QueryBoundary(data_cutoff=CUTOFF),
        static_instrument_ids=static,
        warmup_sessions=warmup,
    )


# ---------------------------------------------------------------------------
# Point 3: fixed_trading_sessions@1 boundary sizes
# ---------------------------------------------------------------------------


class TestFixedChunkBoundarySizes(unittest.TestCase):
    """Partition sizes for every boundary count named by the task package."""

    def setUp(self) -> None:
        self.policy = FixedTradingSessionsV1()
        self.zone = ZoneInfo("Asia/Shanghai")

    def _steps(self, count: int):
        return tuple(
            TimeStep(
                sequence=index,
                start_time=datetime(2026, 1, 1, 9, 30, tzinfo=self.zone),
                end_time=datetime(2026, 1, 1, 15, 0, tzinfo=self.zone),
                session_id=f"s{index:03d}",
                timezone="Asia/Shanghai",
                metadata={},
            )
            for index in range(count)
        )

    def _sizes(self, count: int) -> tuple[int, ...]:
        return tuple(len(chunk.steps) for chunk in self.policy.partition(self._steps(count)))

    def test_boundary_counts(self) -> None:
        expected = {
            0: (),
            1: (1,),
            19: (19,),
            20: (20,),
            21: (20, 1),
            40: (20, 20),
            41: (20, 20, 1),
            42: (20, 20, 2),
            43: (20, 20, 3),
        }
        for count, sizes in expected.items():
            with self.subTest(formal_sessions=count):
                self.assertEqual(self._sizes(count), sizes)

    def test_43_sessions_split_20_20_3(self) -> None:
        chunks = self.policy.partition(self._steps(43))
        self.assertEqual([len(chunk.steps) for chunk in chunks], [20, 20, 3])
        # Chunk sequences are contiguous from 0 and each chunk keeps its
        # steps' original timeline sequence numbers.
        for chunk_sequence, chunk in enumerate(chunks):
            self.assertEqual(chunk.chunk_sequence, chunk_sequence)
        flat = [step.sequence for chunk in chunks for step in chunk.steps]
        self.assertEqual(flat, list(range(43)))

    def test_tail_chunk_may_hold_fewer_than_twenty(self) -> None:
        sizes = self._sizes(45)
        self.assertEqual(sizes, (20, 20, 5))
        self.assertTrue(all(size <= SESSIONS_PER_CHUNK_V1 for size in sizes))

    def test_non_contiguous_input_is_blocked_before_partitioning(self) -> None:
        steps = list(self._steps(21))
        del steps[5]
        # A gap breaks the sequence==index precondition and must fail
        # instead of silently splitting one run into two.
        with self.assertRaises(Exception):
            self.policy.partition(steps)


class TestProviderLevelFortyThreeSessions(unittest.TestCase):
    """The 43-formal-session example sliced into 20 + 20 + 3 chunks."""

    @classmethod
    def setUpClass(cls) -> None:
        all_days = weekdays(date(2025, 11, 3), date(2026, 3, 31))
        cls.formal = all_days[10:53]
        assert len(cls.formal) == 43
        cls.provider = MemoryDataProvider(
            build_dataset(
                facts_start=all_days[0],
                facts_end=all_days[-1],
                open_days=set(all_days),
            )
        )
        cls.session = open_ready_session(
            cls.provider,
            make_intent(start=cls.formal[0], end=cls.formal[-1], warmup=5),
        )

    def test_formal_and_warmup_are_frozen_correctly(self) -> None:
        self.assertEqual(len(self.session.resolved_sessions), 43)
        self.assertEqual(len(self.session.warmup_sessions), 5)
        # Warmup sessions precede the first formal session and never enter
        # the formal numbering.
        self.assertTrue(
            self.session.warmup_sessions[-1].session_date
            < self.session.resolved_sessions[0].session_date
        )

    def test_chunks_match_the_fixed_boundaries(self) -> None:
        sessions = self.session.resolved_sessions
        expected_bounds = [(0, 20), (20, 40), (40, 43)]
        for index, (start, end) in enumerate(expected_bounds):
            chunk = self.session.open_chunk(chunk_query(self.session, index))
            with chunk:
                chunk.validate_consistency()
                self.assertEqual(
                    chunk._sessions,
                    sessions[start:end],
                    f"chunk {index} must hold official sessions {start}..{end - 1}",
                )

    def test_chunk_sequences_start_at_zero_and_are_contiguous(self) -> None:
        for index in range(3):
            chunk = self.session.open_chunk(chunk_query(self.session, index))
            self.assertEqual(chunk.consistency_evidence.chunk_index, index)

    def test_out_of_range_chunk_index_cannot_open(self) -> None:
        sessions = self.session.resolved_sessions
        # Chunk-2 boundaries with an out-of-range index: the frozen
        # sessions decide, so the forged pair cannot open anything.
        query = DataChunkQuery(
            chunk_index=3,
            first_session_id=sessions[40].session_id,
            last_session_id=sessions[42].session_id,
            fact_types=(DataCapability.BARS,),
        )
        with self.assertRaises(InvalidDataRequestError):
            self.session.open_chunk(query)

    def test_forged_boundaries_cannot_open(self) -> None:
        sessions = self.session.resolved_sessions
        query = DataChunkQuery(
            chunk_index=0,
            first_session_id=sessions[1].session_id,
            last_session_id=sessions[19].session_id,
            fact_types=(DataCapability.BARS,),
        )
        with self.assertRaises(InvalidDataRequestError):
            self.session.open_chunk(query)


# ---------------------------------------------------------------------------
# Point 3: cross-chunk run-state continuity fixture
# ---------------------------------------------------------------------------


class RunState:
    """Stand-in for engine-owned state created outside the chunk loop."""

    def __init__(self) -> None:
        self.strategy_instance = object()
        self.account_state = {"equity": "0"}
        self.positions: list[str] = []
        self.active_orders: list[str] = []
        self.analyzers: list[str] = []
        self.global_event_sequence: list[int] = []


class TestCrossChunkStateContinuity(unittest.TestCase):
    """Chunk switches never reset run-level domain state."""

    @classmethod
    def setUpClass(cls) -> None:
        all_days = weekdays(date(2025, 11, 3), date(2026, 3, 31))
        cls.formal = all_days[10:53]
        cls.provider = MemoryDataProvider(
            build_dataset(
                facts_start=all_days[0],
                facts_end=all_days[-1],
                open_days=set(all_days),
            )
        )
        cls.session = open_ready_session(
            cls.provider,
            make_intent(start=cls.formal[0], end=cls.formal[-1], warmup=5),
        )

    def test_state_survives_every_chunk_boundary(self) -> None:
        state = RunState()
        snapshot_before = (
            state.strategy_instance,
            dict(state.account_state),
            list(state.positions),
        )
        for index in range(3):
            before = len(state.global_event_sequence)
            chunk = self.session.open_chunk(chunk_query(self.session, index))
            with chunk:
                chunk.validate_consistency()
                rows = chunk.bars(
                    BarQuery(
                        instrument_ids=IID_A,
                        frequency="1d",
                        boundary=QueryBoundary(data_cutoff=CUTOFF),
                        window=DateRange(
                            start_date=self.formal[index * 20],
                            end_date=self.formal[min(index * 20 + 19, 42)],
                        ),
                    )
                )
                self.assertTrue(rows)
                state.global_event_sequence.append(index)
            # The chunk is closed now: its reads fail, but nothing owned by
            # the run loop was rebuilt or reset.
            with self.assertRaises(DataSessionClosedError):
                chunk.bars(
                    BarQuery(
                        instrument_ids=IID_A,
                        frequency="1d",
                        boundary=QueryBoundary(data_cutoff=CUTOFF),
                        window=DateRange(
                            start_date=self.formal[0], end_date=self.formal[19]
                        ),
                    )
                )
            self.assertIs(state.strategy_instance, snapshot_before[0])
            self.assertEqual(state.account_state, snapshot_before[1])
            self.assertEqual(state.positions, snapshot_before[2])
            self.assertEqual(
                len(state.global_event_sequence), before + 1
            )


# ---------------------------------------------------------------------------
# Point 4: bounded consistency tokens and transitional mode
# ---------------------------------------------------------------------------


class TestTokenFailureSemantics(unittest.TestCase):
    """Failures block reads without consuming any provider read budget."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.all_days = weekdays(date(2025, 11, 3), date(2026, 1, 30))
        cls.formal = cls.all_days[10:35]

    def _provider(self, *, with_bars: bool = True) -> MemoryDataProvider:
        return MemoryDataProvider(
            build_dataset(
                facts_start=self.all_days[0],
                facts_end=self.all_days[-1],
                open_days=set(self.all_days),
                bar_days=set(self.all_days) if with_bars else set(),
            )
        )

    def test_expired_token_blocks_reads_without_new_reads(self) -> None:
        provider = self._provider()
        session = open_ready_session(
            provider,
            make_intent(start=self.formal[0], end=self.formal[-1]),
        )
        chunk = session.open_chunk(chunk_query(session, 0))
        chunk.validate_consistency()
        reads_before = provider.read_count
        provider.invalidate_revision()
        status = chunk.validate_consistency()
        self.assertIs(status.status, ConsistencyValidation.EXPIRED)
        # Persistable evidence attributes the block to the data
        # consistency phase.
        self.assertEqual(
            chunk.consistency_evidence.coverage_summary["failure_phase"],
            "data_consistency",
        )
        with self.assertRaises(ConsistencyTokenExpiredError):
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=QueryBoundary(data_cutoff=CUTOFF),
                    window=DateRange(
                        start_date=self.formal[0], end_date=self.formal[19]
                    ),
                )
            )
        # The failed attempt never reached the provider funnel.
        self.assertEqual(provider.read_count, reads_before)

    def test_uncovered_fact_type_blocks_as_coverage_incomplete(self) -> None:
        provider = self._provider(with_bars=False)
        session = open_ready_session(
            provider,
            make_intent(start=self.formal[0], end=self.formal[-1]),
        )
        chunk = session.open_chunk(chunk_query(session, 0))
        status = chunk.validate_consistency()
        self.assertIs(status.status, ConsistencyValidation.COVERAGE_INCOMPLETE)

    def test_undeclared_bars_query_is_rejected(self) -> None:
        provider = self._provider()
        session = open_ready_session(
            provider,
            make_intent(start=self.formal[0], end=self.formal[-1]),
        )
        sessions = session.resolved_sessions
        query = DataChunkQuery(
            chunk_index=0,
            first_session_id=sessions[0].session_id,
            last_session_id=sessions[19].session_id,
            fact_types=(DataCapability.COVERAGE,),
        )
        chunk = session.open_chunk(query)
        with chunk:
            self.assertIs(
                chunk.validate_consistency().status, ConsistencyValidation.VALID
            )
            # The token declared coverage only: a bar read would not be
            # covered by the persisted consistency evidence.
            with self.assertRaises(InvalidDataRequestError):
                chunk.bars(
                    BarQuery(
                        instrument_ids=IID_A,
                        frequency="1d",
                        boundary=QueryBoundary(data_cutoff=CUTOFF),
                        window=DateRange(
                            start_date=self.formal[0],
                            end_date=self.formal[19],
                        ),
                    )
                )

    def test_coverage_query_beyond_declared_types_is_rejected(self) -> None:
        provider = self._provider()
        session = open_ready_session(
            provider,
            make_intent(start=self.formal[0], end=self.formal[-1]),
        )
        sessions = session.resolved_sessions
        query = DataChunkQuery(
            chunk_index=0,
            first_session_id=sessions[0].session_id,
            last_session_id=sessions[19].session_id,
            fact_types=(DataCapability.BARS, DataCapability.COVERAGE),
        )
        chunk = session.open_chunk(query)
        with chunk:
            chunk.validate_consistency()
            # CALENDARS was never declared and can never be served by a
            # chunk, so auditing it here must fail instead of fabricating
            # evidence about unservable facts.
            with self.assertRaises(InvalidDataRequestError):
                chunk.coverage(
                    CoverageQuery(
                        capability=DataCapability.CALENDARS,
                        instrument_ids=(IID_A,),
                        window=DateRange(
                            start_date=self.formal[0],
                            end_date=self.formal[19],
                        ),
                        boundary=QueryBoundary(data_cutoff=CUTOFF),
                    )
                )

    def test_declared_but_unservable_fact_type_fails_closed_at_open(self) -> None:
        provider = self._provider()
        session = open_ready_session(
            provider,
            make_intent(start=self.formal[0], end=self.formal[-1]),
        )
        sessions = session.resolved_sessions
        query = DataChunkQuery(
            chunk_index=0,
            first_session_id=sessions[0].session_id,
            last_session_id=sessions[19].session_id,
            fact_types=(DataCapability.BARS, DataCapability.CALENDARS),
        )
        # Calendar facts are resolved before chunks on this provider, so opening
        # fails closed before any strategy stage could begin.
        with self.assertRaises(UnsupportedCapabilityError):
            session.open_chunk(query)

    def test_manifest_served_coverage_capability_is_chunk_servable(self) -> None:
        provider = self._provider()
        session = open_ready_session(
            provider,
            make_intent(start=self.formal[0], end=self.formal[-1]),
        )
        sessions = session.resolved_sessions
        query = DataChunkQuery(
            chunk_index=0,
            first_session_id=sessions[0].session_id,
            last_session_id=sessions[19].session_id,
            fact_types=(DataCapability.BARS, DataCapability.COVERAGE),
        )
        chunk = session.open_chunk(query)
        with chunk:
            self.assertIs(
                chunk.validate_consistency().status, ConsistencyValidation.VALID
            )
            report = chunk.coverage(
                CoverageQuery(
                    capability=DataCapability.BARS,
                    instrument_ids=(IID_A,),
                    window=DateRange(
                        start_date=self.formal[0], end_date=self.formal[19]
                    ),
                    boundary=QueryBoundary(data_cutoff=CUTOFF),
                )
            )
            self.assertEqual(report.capability, DataCapability.BARS)


class TestTransitionalRepeatableRead(unittest.TestCase):
    """The explicitly frozen transitional mode issues no logical tokens."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.all_days = weekdays(date(2025, 11, 3), date(2026, 1, 30))
        cls.formal = cls.all_days[10:35]

    def _session(self, *, token_contract=None):
        provider = MemoryDataProvider(
            build_dataset(
                facts_start=self.all_days[0],
                facts_end=self.all_days[-1],
                open_days=set(self.all_days),
            )
        )
        request = admit(
            provider,
            transitional_intent(
                start=self.formal[0],
                end=self.formal[-1],
                token_contract=token_contract,
            ),
        )
        session = provider.open_session(request)
        report = session.preflight()
        assert report.status is PreflightStatus.READY, report.issues
        return provider, session

    def test_mode_freezes_before_the_run_and_issues_no_token(self) -> None:
        provider, session = self._session()
        self.assertEqual(
            session.consistency_context.mode,
            ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        )
        chunk = session.open_chunk(chunk_query(session, 0))
        evidence = chunk.consistency_evidence
        self.assertIsNone(evidence.token_digest)
        self.assertEqual(evidence.mode, ConsistencyMode.TRANSITIONAL_REPEATABLE_READ)
        self.assertEqual(
            evidence.coverage_summary["consistency_mode"],
            "transitional_repeatable_read",
        )
        self.assertIn("transitional_resource_risk", evidence.coverage_summary)
        self.assertIs(chunk.validate_consistency().status, ConsistencyValidation.VALID)

    def test_revision_advance_expires_the_pinned_snapshot(self) -> None:
        provider, session = self._session()
        chunk = session.open_chunk(chunk_query(session, 0))
        provider.invalidate_revision()
        status = chunk.validate_consistency()
        self.assertIs(status.status, ConsistencyValidation.EXPIRED)
        with self.assertRaises(ConsistencyTokenExpiredError):
            chunk.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=QueryBoundary(data_cutoff=CUTOFF),
                    window=DateRange(
                        start_date=self.formal[0], end_date=self.formal[19]
                    ),
                )
            )

    def test_one_snapshot_covers_every_chunk_of_the_session(self) -> None:
        provider, session = self._session()
        first = session.open_chunk(chunk_query(session, 0))
        self.assertIs(first.validate_consistency().status, ConsistencyValidation.VALID)
        # The base revision advances AFTER chunk 0 was validated: chunk 1
        # must fail against the SAME session-pinned snapshot before any
        # strategy call of that chunk, not silently start a new window.
        provider.invalidate_revision()
        second = session.open_chunk(chunk_query(session, 1))
        status = second.validate_consistency()
        self.assertIs(status.status, ConsistencyValidation.EXPIRED)
        reads_before = provider.read_count
        with self.assertRaises(ConsistencyTokenExpiredError):
            second.bars(
                BarQuery(
                    instrument_ids=IID_A,
                    frequency="1d",
                    boundary=QueryBoundary(data_cutoff=CUTOFF),
                    window=DateRange(
                        start_date=self.formal[20], end_date=self.formal[24]
                    ),
                )
            )
        self.assertEqual(provider.read_count, reads_before)

    def test_configured_token_contract_is_blocked_at_preflight(self) -> None:
        provider = MemoryDataProvider(
            build_dataset(
                facts_start=self.all_days[0],
                facts_end=self.all_days[-1],
                open_days=set(self.all_days),
            )
        )
        report = provider.preflight(
            transitional_intent(
                start=self.formal[0],
                end=self.formal[-1],
                token_contract=TOKEN_CONTRACT,
            )
        )
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertIn(
            ISSUE_UNSUPPORTED_TOKEN_CONTRACT,
            {issue.code for issue in report.issues},
        )

    def test_mode_never_switches_during_a_token_run(self) -> None:
        provider = MemoryDataProvider(
            build_dataset(
                facts_start=self.all_days[0],
                facts_end=self.all_days[-1],
                open_days=set(self.all_days),
            )
        )
        session = open_ready_session(
            provider,
            make_intent(start=self.formal[0], end=self.formal[-1]),
        )
        modes = set()
        for index in range(2):
            chunk = session.open_chunk(chunk_query(session, index))
            with chunk:
                chunk.validate_consistency()
                evidence = chunk.consistency_evidence
                modes.add(evidence.mode)
                self.assertEqual(
                    evidence.coverage_summary["consistency_mode"],
                    session.consistency_context.mode.value,
                )
        self.assertEqual(
            modes, {ConsistencyMode.CHUNKED_LOGICAL_TOKEN}
        )


class TestTokenEvidenceIsDigestOnly(unittest.TestCase):
    """Only the irreversible digest leaves the provider boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        all_days = weekdays(date(2025, 11, 3), date(2026, 1, 30))
        cls.formal = all_days[10:35]
        cls.provider = MemoryDataProvider(
            build_dataset(
                facts_start=all_days[0],
                facts_end=all_days[-1],
                open_days=set(all_days),
            )
        )
        cls.session = open_ready_session(
            cls.provider, make_intent(start=cls.formal[0], end=cls.formal[-1])
        )
        cls.chunk = cls.session.open_chunk(chunk_query(cls.session, 0))
        cls.chunk.validate_consistency()

    def test_evidence_carries_only_a_short_hex_digest(self) -> None:
        evidence = self.chunk.consistency_evidence
        self.assertIsNotNone(evidence.token_digest)
        self.assertLessEqual(len(evidence.token_digest), 64)
        self.assertTrue(
            all(c in "0123456789abcdef" for c in evidence.token_digest)
        )

    def test_summary_holds_no_raw_token_material(self) -> None:
        evidence = self.chunk.consistency_evidence
        forbidden_markers = ("revision", "fixture", "digest_spec", "secret")
        blob = repr(dict(evidence.coverage_summary))
        for marker in forbidden_markers:
            self.assertNotIn(marker, blob)

    def test_envelope_summary_is_frozen_json(self) -> None:
        envelope = CoverageEnvelope(
            chunk_first_session_date=self.formal[0],
            chunk_last_session_date=self.formal[19],
            fact_types=(DataCapability.BARS,),
        )
        summary = envelope.to_summary()
        with self.assertRaises((TypeError, ValueError)):
            summary["chunk_session_count"] = 5


class TestCoverageEnvelopeInvariants(unittest.TestCase):
    """Warmup fields must describe a strictly preceding, coherent range."""

    def setUp(self) -> None:
        self.kwargs = dict(
            chunk_first_session_date=date(2026, 1, 5),
            chunk_last_session_date=date(2026, 1, 30),
        )

    def test_warmup_may_not_start_on_the_chunk_first_day(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            CoverageEnvelope(
                **self.kwargs,
                warmup_first_session_date=date(2026, 1, 5),
                warmup_session_count=3,
            )

    def test_warmup_may_not_start_after_the_chunk_first_day(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            CoverageEnvelope(
                **self.kwargs,
                warmup_first_session_date=date(2026, 1, 6),
                warmup_session_count=3,
            )

    def test_positive_count_without_first_date_is_rejected(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            CoverageEnvelope(**self.kwargs, warmup_session_count=3)

    def test_first_date_without_positive_count_is_rejected(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            CoverageEnvelope(
                **self.kwargs,
                warmup_first_session_date=date(2026, 1, 2),
                warmup_session_count=0,
            )

    def test_coherent_strictly_preceding_warmup_passes(self) -> None:
        envelope = CoverageEnvelope(
            **self.kwargs,
            warmup_first_session_date=date(2026, 1, 2),
            warmup_session_count=3,
        )
        self.assertEqual(envelope.warmup_session_count, 3)
        # Zero warmup stays representable without a first date.
        empty = CoverageEnvelope(**self.kwargs)
        self.assertIsNone(empty.warmup_first_session_date)


class TestLookbackEndBound(unittest.TestCase):
    """A lookback window never reads sessions beyond its own end_at."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.all_days = weekdays(date(2025, 11, 3), date(2026, 1, 30))
        # Formal window J5..J7 with warmup D31, Jan-2 (the two open days
        # before J5); bars are complete on every open day.
        cls.provider = MemoryDataProvider(
            build_dataset(
                facts_start=cls.all_days[0],
                facts_end=cls.all_days[-1],
                open_days=set(cls.all_days),
            )
        )
        j5 = date(2026, 1, 5)
        j7 = date(2026, 1, 7)
        cls.session = open_ready_session(
            cls.provider,
            make_intent(start=j5, end=j7, warmup=2),
        )
        cls.chunk = cls.session.open_chunk(chunk_query(cls.session, 0))
        cls.chunk.validate_consistency()

    def _lookback_rows(self, *, end_at: datetime, cutoff: datetime):
        from app.backtesting.data.requests import LookbackWindow

        return self.chunk.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=QueryBoundary(
                    data_cutoff=cutoff, include_cutoff_day=True
                ),
                window=LookbackWindow(sessions=2, end_at=end_at),
            )
        )

    def test_lookback_never_reads_past_its_end_at(self) -> None:
        end_at = datetime(2026, 1, 5, 15, 0, tzinfo=TZ)
        cutoff = datetime(2026, 1, 7, 15, 0, tzinfo=TZ)
        rows = self._lookback_rows(end_at=end_at, cutoff=cutoff)
        days = [bar.trade_date for bar in rows]
        # Exactly the two warmup sessions before the end day: the proven
        # cutoff day (Jan-7) must NOT leak past the lookback's own bound.
        self.assertEqual(len(days), 2)
        self.assertEqual(max(days), date(2026, 1, 2))

    def test_end_day_is_admitted_only_when_it_is_the_proven_cutoff(self) -> None:
        end_at = datetime(2026, 1, 7, 15, 0, tzinfo=TZ)
        cutoff = end_at
        rows = self._lookback_rows(end_at=end_at, cutoff=cutoff)
        days = [bar.trade_date for bar in rows]
        self.assertEqual(len(days), 2)
        self.assertEqual(max(days), date(2026, 1, 7))

    def test_end_day_stays_excluded_without_completeness_proof(self) -> None:
        from app.backtesting.data.requests import LookbackWindow

        end_at = datetime(2026, 1, 7, 15, 0, tzinfo=TZ)
        chunk = self.chunk
        rows = chunk.bars(
            BarQuery(
                instrument_ids=IID_A,
                frequency="1d",
                boundary=QueryBoundary(data_cutoff=end_at),
                window=LookbackWindow(sessions=2, end_at=end_at),
            )
        )
        self.assertEqual(max(bar.trade_date for bar in rows), date(2026, 1, 6))


class TestOpenChunkParameterValidation(unittest.TestCase):
    """Foreign open_chunk arguments fail with the stable contract error."""

    def setUp(self) -> None:
        all_days = weekdays(date(2025, 11, 3), date(2026, 1, 30))
        self.provider = MemoryDataProvider(
            build_dataset(
                facts_start=all_days[0],
                facts_end=all_days[-1],
                open_days=set(all_days),
            )
        )
        self.session = open_ready_session(
            self.provider,
            make_intent(start=date(2026, 1, 5), end=date(2026, 1, 7)),
        )

    def test_none_query_raises_invalid_request(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            self.session.open_chunk(None)

    def test_non_chunk_query_object_raises_invalid_request(self) -> None:
        class Forged:
            chunk_index = -1  # would bypass DTO validation if accepted

            first_session_id = "s"
            last_session_id = "s"
            fact_types = ()

        with self.assertRaises(InvalidDataRequestError):
            self.session.open_chunk(Forged())  # type: ignore[arg-type]


class TestBusinessQueryParameterValidation(unittest.TestCase):
    """Every implemented business query rejects foreign query objects."""

    def setUp(self) -> None:
        all_days = weekdays(date(2025, 11, 3), date(2026, 1, 30))
        self.provider = MemoryDataProvider(
            build_dataset(
                facts_start=all_days[0],
                facts_end=all_days[-1],
                open_days=set(all_days),
            )
        )
        self.session = open_ready_session(
            self.provider,
            make_intent(start=date(2026, 1, 5), end=date(2026, 1, 7)),
        )
        self.chunk = self.session.open_chunk(chunk_query(self.session, 0))
        self.chunk.validate_consistency()

    def test_coverage_rejects_none_query(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            self.chunk.coverage(None)  # type: ignore[arg-type]

    def test_coverage_rejects_foreign_object(self) -> None:
        class Forged:
            capability = "bars"  # not a DataCapability; would bypass DTO checks

        with self.assertRaises(InvalidDataRequestError):
            self.chunk.coverage(Forged())  # type: ignore[arg-type]

    def test_instruments_rejects_none_query(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            self.chunk.instruments(None)  # type: ignore[arg-type]

    def test_bars_still_rejects_none_query(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            self.chunk.bars(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
