"""Focused task-11 acceptance checks for the canonical calendar boundary.

These tests intentionally exercise the production-facing contracts rather than
only the legacy convenience resolver: the strict request must carry a single
PIT boundary, SQL and memory snapshots must be immutable batch reads, and the
ETF adapter must fail closed when identity evidence is absent.
"""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.analysis_admission import resource_limited_preflight_failure
from app.backtesting.calendar_axis import (
    CalendarAxisDifferenceField,
    CalendarAxisStatus,
    CalendarCapabilityDeclaration,
    CalendarDefinition,
    CalendarRegistry,
    CalendarSessionFact,
    CalendarSessionWindowLimitExceededError,
    CalendarSourcePriority,
    CalendarSnapshotRequest,
    InMemoryCalendarAxisDataProvider,
    normalize_window_payloads,
    resolve_calendar_axis,
    select_capability_declaration,
    select_pit_candidate,
)
from app.backtesting.calendar_models import (
    CalendarCapabilityDeclarationRecord,
    CalendarDefinitionRecord,
    CalendarExchangeBindingRecord,
    CalendarReconciliationRangeRecord,
    CalendarRegistryRecord,
    CalendarResolutionHeadRecord,
    CalendarSessionFactRecord,
    CalendarSourcePriorityRecord,
)
from app.backtesting.data.adapters.etf import EtfFactsAdapter
from app.backtesting.data.calendar_repository import CalendarFactRepository
from app.backtesting.data.calendar_sql import SqlCalendarAxisDataProvider
from app.backtesting.data.errors import (
    CalendarCrossMidnightUnsupportedError,
    CalendarJsonInvalidError,
    LookbackSessionsLimitExceededError,
    DataCutoffExceededError,
    DataCutoffRequiredError,
    InstrumentCalendarUnresolvedError,
)
from app.backtesting.data.requests import QueryBoundary


UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)
SEED_HASH = "a" * 64


class Task11BoundaryAcceptanceTestCase(unittest.TestCase):
    """A-26/A-27/A-46: cutoff is explicit and uses the calendar timezone."""

    def test_registry_strict_validate_rejects_non_json_evidence_first(self) -> None:
        registry = CalendarRegistry(
            "SSE",
            "Shanghai Stock Exchange",
            evidence=object(),
        )
        with self.assertRaises(CalendarJsonInvalidError):
            registry.strict_validate()

    def test_snapshot_request_rejects_missing_cutoff_before_provider_read(self) -> None:
        with self.assertRaises(DataCutoffRequiredError):
            CalendarSnapshotRequest(
                calendar_ids=("SSE",),
                formal_start=date(2026, 1, 1),
                formal_end=date(2026, 1, 1),
                warmup_sessions=0,
                query_boundary=None,
            )

    def test_cutoff_local_date_is_not_the_utc_surface_date(self) -> None:
        boundary = QueryBoundary(
            data_cutoff=datetime(2026, 1, 1, 16, 30, tzinfo=UTC),
            include_cutoff_day=False,
        )
        with self.assertRaises(DataCutoffExceededError):
            boundary.require_not_past_cutoff(date(2026, 1, 2), "Asia/Shanghai", "formal_end")
        self.assertEqual(boundary.derive_cutoff_local_date("Asia/Shanghai"), date(2026, 1, 2))


class Task11SqlSnapshotAcceptanceTestCase(unittest.TestCase):
    """A-10/A-17: SQL opens one pinned, batch-backed immutable snapshot."""

    @staticmethod
    def _calendar_tables():
        return [
            CalendarSourcePriorityRecord,
            CalendarRegistryRecord,
            CalendarDefinitionRecord,
            CalendarSessionFactRecord,
            CalendarExchangeBindingRecord,
            CalendarCapabilityDeclarationRecord,
            CalendarResolutionHeadRecord,
            CalendarReconciliationRangeRecord,
        ]

    def test_sql_snapshot_uses_one_prepare_and_one_batch_read(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        for record_class in self._calendar_tables():
            record_class.__table__.create(engine)
        source_priority = CalendarSourcePriority(
            source="official",
            source_priority_version="v1",
            source_priority=1,
            source_revision_order=1,
            source_revision="r1",
            valid_from=date(2020, 1, 1),
            known_at=NOW,
            knowledge_from=NOW,
            knowledge_as_of=NOW,
            observed_at=NOW,
            evidence={},
            bootstrap_seed_hash=SEED_HASH,
        )
        common = {
            "source": "official",
            "source_revision": "r1",
            "known_at": NOW,
            "knowledge_from": NOW,
            "knowledge_as_of": NOW,
            "observed_at": NOW,
            "source_priority_fact_id": source_priority.fact_id,
            "source_priority_version": "v1",
            "source_priority": 1,
            "source_revision_order": 1,
            "bootstrap_seed_id": "calendar-source-priority-bootstrap",
            "bootstrap_seed_version": 1,
            "bootstrap_seed_hash": SEED_HASH,
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
            ((time(9, 30), time(10, 0)),),
            valid_from=date(2020, 1, 1),
            registry_fact_id=registry.fact_id,
            registry_version=1,
            **common,
        )
        # A definition outside the frozen envelope must not leak into the
        # in-memory snapshot audit projection; SQL's bounded batch query omits
        # the same row.
        future_definition = CalendarDefinition(
            "SSE",
            "sse-future-v1",
            "Asia/Shanghai",
            ((time(9, 30), time(10, 0)),),
            valid_from=date(2030, 1, 1),
            logical_fact_key="calendar_definition:SSE:future",
            registry_fact_id=registry.fact_id,
            registry_version=1,
            **common,
        )
        after_knowledge_definition = CalendarDefinition(
            "SSE",
            "sse-after-knowledge-v1",
            "Asia/Shanghai",
            ((time(9, 30), time(10, 0)),),
            valid_from=date(2026, 1, 2),
            logical_fact_key="calendar_definition:SSE:after-knowledge",
            known_at=datetime(2026, 1, 6, tzinfo=UTC),
            knowledge_from=datetime(2026, 1, 6, tzinfo=UTC),
            knowledge_as_of=datetime(2026, 1, 6, tzinfo=UTC),
            registry_fact_id=registry.fact_id,
            registry_version=1,
            **{
                key: value
                for key, value in common.items()
                if key not in {"known_at", "knowledge_from", "knowledge_as_of"}
            },
        )
        quarantined_definition = CalendarDefinition(
            "SSE",
            "sse-quarantined-v1",
            "Asia/Shanghai",
            ((time(9, 30), time(10, 0)),),
            valid_from=date(2026, 1, 2),
            logical_fact_key="calendar_definition:SSE:quarantined",
            quality_status="quarantined",
            registry_fact_id=registry.fact_id,
            registry_version=1,
            **common,
        )
        facts = [
            CalendarSessionFact(
                "SSE",
                date(2026, 1, 2) + timedelta(days=offset),
                True,
                definition_version="sse-v1",
                registry_fact_id=registry.fact_id,
                registry_version=1,
                definition_fact_id=definition.fact_id,
                **common,
            )
            for offset in range(3)
        ]
        boundary = QueryBoundary(
            data_cutoff=datetime(2026, 1, 10, tzinfo=UTC),
            include_cutoff_day=True,
            knowledge_as_of=datetime(2026, 1, 5, tzinfo=UTC),
        )
        with Session(engine) as session:
            repository = CalendarFactRepository(session)
            repository.append_source_priority(source_priority)
            repository.append_registry(registry)
            repository.append_definition(definition)
            repository.append_definition(future_definition)
            repository.append_definition(after_knowledge_definition)
            repository.append_definition(quarantined_definition)
            for fact in facts:
                repository.append_session_fact(fact)
            # The SQL provider's prepare phase intentionally reads only the
            # rebuildable resolution-head index.  Populate it through the
            # same repository method used by calendar ingestion.
            repository.rebuild_resolution_heads(
                calendar_id="SSE",
                start_date=date(2026, 1, 2),
                end_date=date(2026, 1, 4),
            )
            self.assertEqual(
                repository.list_open_dates(
                    calendar_id="SSE",
                    start_date=date(2026, 1, 2),
                    end_date=date(2026, 1, 4),
                    data_cutoff=boundary.data_cutoff,
                ),
                [date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)],
            )
            session.commit()
            provider = SqlCalendarAxisDataProvider(session)
            snapshot = provider.open_calendar_snapshot(
                CalendarSnapshotRequest(
                    calendar_ids=("SSE",),
                    formal_start=date(2026, 1, 2),
                    formal_end=date(2026, 1, 4),
                    warmup_sessions=0,
                    query_boundary=boundary,
                )
            )
        memory_snapshot = InMemoryCalendarAxisDataProvider(
            definitions=(
                definition,
                future_definition,
                after_knowledge_definition,
                quarantined_definition,
            ),
            facts=tuple(facts),
            registries=(registry,),
            source_priorities=(source_priority,),
        ).open_calendar_snapshot(
            CalendarSnapshotRequest(
                calendar_ids=("SSE",),
                formal_start=date(2026, 1, 2),
                formal_end=date(2026, 1, 4),
                warmup_sessions=0,
                query_boundary=boundary,
            )
        )
        # A row learned after the historical cognition cutoff is not visible
        # to this snapshot and therefore must not perturb its revision/hash
        # evidence.  SQL and memory both load the bounded data-cutoff batch,
        # then apply the same knowledge_as_of predicate while hashing it.
        memory_without_after_knowledge = InMemoryCalendarAxisDataProvider(
            definitions=(definition, future_definition, quarantined_definition),
            facts=tuple(facts),
            registries=(registry,),
            source_priorities=(source_priority,),
        ).open_calendar_snapshot(
            CalendarSnapshotRequest(
                calendar_ids=("SSE",),
                formal_start=date(2026, 1, 2),
                formal_end=date(2026, 1, 4),
                warmup_sessions=0,
                query_boundary=boundary,
            )
        )
        self.assertEqual(len(snapshot.resolution.resolved_sessions), 3)
        self.assertEqual(provider.prepare_calls, 1)
        self.assertEqual(provider.batch_read_calls, 1)
        self.assertEqual(provider.fact_calls, 0)
        self.assertEqual(snapshot.calendar_semantic_signature, memory_snapshot.calendar_semantic_signature)
        self.assertEqual(snapshot.calendar_revision_digest, memory_snapshot.calendar_revision_digest)
        self.assertEqual(
            memory_without_after_knowledge.calendar_revision_digest,
            memory_snapshot.calendar_revision_digest,
        )
        self.assertEqual(
            memory_without_after_knowledge.snapshot_fingerprint,
            memory_snapshot.snapshot_fingerprint,
        )
        self.assertEqual(snapshot.calendar_session_signature, memory_snapshot.calendar_session_signature)
        self.assertEqual(snapshot.snapshot_fingerprint, memory_snapshot.snapshot_fingerprint)
        self.assertEqual(
            [item.definition_version for item in snapshot.resolved_calendar_definitions],
            ["sse-v1"],
        )
        self.assertEqual(
            [
                (item.calendar_id, item.definition_version, item.fact_id)
                for item in snapshot.resolved_calendar_definitions
            ],
            [
                (item.calendar_id, item.definition_version, item.fact_id)
                for item in memory_snapshot.resolved_calendar_definitions
            ],
        )

    def test_resolution_head_uses_source_priority_before_newer_fact(self) -> None:
        """A-17/A-20/A-29/A-40: SQL heads and memory use one PIT winner."""

        engine = create_engine("sqlite:///:memory:")
        for record_class in self._calendar_tables():
            record_class.__table__.create(engine)
        official = CalendarSourcePriority(
            source="official", source_priority_version="v1", source_priority=1,
            source_revision_order=1, source_revision="official-priority",
            valid_from=date(2020, 1, 1), known_at=NOW, observed_at=NOW,
            evidence={}, bootstrap_seed_hash=SEED_HASH,
        )
        backup_time = datetime(2026, 1, 2, 12, tzinfo=UTC)
        backup = CalendarSourcePriority(
            source="backup", source_priority_version="v1", source_priority=2,
            source_revision_order=1, source_revision="backup-priority",
            valid_from=date(2020, 1, 1), known_at=backup_time,
            observed_at=backup_time, evidence={}, bootstrap_seed_hash=SEED_HASH,
        )

        def provenance(source: str, priority: CalendarSourcePriority, when: datetime) -> dict[str, object]:
            return {
                "source": source,
                "source_revision": source + "-fact",
                "known_at": when,
                "knowledge_from": when,
                "knowledge_as_of": when,
                "observed_at": when,
                "source_priority_fact_id": priority.fact_id,
                "source_priority_version": priority.source_priority_version,
                "source_priority": priority.source_priority,
                "source_revision_order": priority.source_revision_order,
                "bootstrap_seed_id": priority.bootstrap_seed_id,
                "bootstrap_seed_version": priority.bootstrap_seed_version,
                "bootstrap_seed_hash": SEED_HASH,
                "evidence": {},
            }

        registry = CalendarRegistry(
            "SSE", "Shanghai Stock Exchange", registry_version=1,
            valid_from=date(2020, 1, 1), **provenance("official", official, NOW),
        )
        definition = CalendarDefinition(
            "SSE", "sse-v1", "Asia/Shanghai", ((time(9, 30), time(10, 0)),),
            valid_from=date(2020, 1, 1), registry_fact_id=registry.fact_id,
            registry_version=1, **provenance("official", official, NOW),
        )
        day = date(2026, 1, 2)
        first = CalendarSessionFact(
            "SSE", day, True, "sse-v1", source="official",
            source_revision="official-fact", known_at=NOW, observed_at=NOW,
            registry_fact_id=registry.fact_id, registry_version=1,
            definition_fact_id=definition.fact_id,
            **{
                key: value for key, value in provenance("official", official, NOW).items()
                if key not in {"source", "source_revision", "known_at", "observed_at"}
            },
        )
        second = CalendarSessionFact(
            "SSE", day, False, "sse-v1", source="backup",
            source_revision="backup-fact", known_at=backup_time,
            observed_at=backup_time, fact_version=2,
            logical_fact_key=first.logical_fact_key,
            supersedes_fact_id=first.fact_id, registry_fact_id=registry.fact_id,
            registry_version=1, definition_fact_id=definition.fact_id,
            **{
                key: value for key, value in provenance("backup", backup, backup_time).items()
                if key not in {"source", "source_revision", "known_at", "observed_at"}
            },
        )
        boundary = QueryBoundary(
            data_cutoff=datetime(2026, 1, 3, tzinfo=UTC), include_cutoff_day=True,
        )
        with Session(engine) as session:
            repository = CalendarFactRepository(session)
            for priority in (official, backup):
                repository.append_source_priority(priority)
            repository.append_registry(registry)
            repository.append_definition(definition)
            repository.append_session_fact(first)
            repository.append_session_fact(second)
            repository.rebuild_resolution_heads(
                calendar_id="SSE", start_date=day, end_date=day,
            )
            session.commit()
            provider = SqlCalendarAxisDataProvider(session)
            sql_snapshot = provider.open_calendar_snapshot(
                CalendarSnapshotRequest(
                    calendar_ids=("SSE",), formal_start=day, formal_end=day,
                    warmup_sessions=0, query_boundary=boundary,
                )
            )
            head = session.query(CalendarResolutionHeadRecord).one()
            self.assertEqual(head.selected_fact_id, first.fact_id)
            self.assertTrue(head.is_open)

        memory_snapshot = InMemoryCalendarAxisDataProvider(
            definitions=(definition,), facts=(first, second), registries=(registry,),
            source_priorities=(official, backup),
        ).open_calendar_snapshot(
            CalendarSnapshotRequest(
                calendar_ids=("SSE",), formal_start=day, formal_end=day,
                warmup_sessions=0, query_boundary=boundary,
            )
        )
        self.assertEqual(sql_snapshot.calendar_revision_digest, memory_snapshot.calendar_revision_digest)
        self.assertEqual(sql_snapshot.snapshot_fingerprint, memory_snapshot.snapshot_fingerprint)
        self.assertEqual(sql_snapshot.resolution.selected_facts, memory_snapshot.resolution.selected_facts)


class Task11ResourceAcceptanceTestCase(unittest.TestCase):
    """A-44/A-49: blocked creation responses are explicit and non-queryable."""

    def test_resource_limited_response_is_not_a_truncated_report(self) -> None:
        failure = resource_limited_preflight_failure(
            observed={"issue_groups": 4097},
            requested_window=SimpleNamespace(
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 10)
            ),
            warmup_sessions=20,
            pit_context={"pit_profile": "strict_calendar_cutoff"},
            calendar_ids=("SSE",),
        )
        payload = failure.as_dict()
        self.assertEqual(payload["reason_code"], "calendar_preflight_resource_limit_exceeded")
        self.assertFalse(payload["details"]["issues_complete"])
        self.assertIsNone(payload["details"]["cursor"])
        self.assertFalse(payload["details"]["truncated"])
        self.assertFalse(payload["details"]["retention"]["queryable"])

    def test_resource_limited_response_normalizes_ids_and_forces_non_pageable_fields(self) -> None:
        failure = resource_limited_preflight_failure(
            observed={"issue_groups": 4097},
            calendar_ids=("sse", "SSE", "szse"),
        )
        payload = failure.as_dict()
        self.assertEqual(payload["details"]["calendar_ids"], ["SSE", "SZSE"])
        self.assertIsNone(payload["details"]["cursor"])
        self.assertFalse(payload["details"]["truncated"])

        from app.backtesting.analysis_admission import AnalysisAdmissionFailure

        generic = AnalysisAdmissionFailure(
            reason_code="data_preflight_blocked",
            message="blocked",
            details={"cursor": "forged", "truncated": True},
        )
        generic_payload = generic.as_dict()
        self.assertIsNone(generic_payload["details"]["cursor"])
        self.assertFalse(generic_payload["details"]["truncated"])

    def test_resource_limited_calendar_overrun_does_not_echo_unbounded_ids(self) -> None:
        calendar_ids = tuple(f"C{index}" for index in range(33))
        failure = resource_limited_preflight_failure(
            observed={"calendar_ids": len(calendar_ids)},
            calendar_ids=calendar_ids,
        )
        details = failure.as_dict()["details"]
        self.assertEqual(details["resource_limit"]["observed"]["calendar_ids"], 33)
        self.assertEqual(details["calendar_ids"], [])
        self.assertFalse(details["issues_complete"])
        self.assertIsNone(details["cursor"])
        self.assertFalse(details["truncated"])


class Task11EtfIdentityAcceptanceTestCase(unittest.TestCase):
    """A-21/A-22: ETF production projection cannot infer SSE."""

    def test_missing_identity_calendar_is_stably_blocked(self) -> None:
        adapter = EtfFactsAdapter(
            code_mappings=lambda *args, **kwargs: (),
            daily_bars=lambda *args, **kwargs: (),
            adjustment_factors=lambda *args, **kwargs: (),
            trading_days=lambda *args, **kwargs: (),
        )
        row = SimpleNamespace(
            etf_id=__import__("uuid").uuid4(),
            ts_code="510300.SH",
            exchange="SH",
            list_date=date(2020, 1, 1),
            cname="ETF",
            csname="ETF",
        )
        with self.assertRaises(InstrumentCalendarUnresolvedError):
            adapter.project_instrument_spec(row, data_cutoff=NOW)


class Task11DomainAcceptanceMatrixTestCase(unittest.TestCase):
    """Executable domain checks for the stable calendar acceptance matrix."""

    @staticmethod
    def _provider(
        *,
        sessions: tuple[tuple[date, bool], ...],
        definition_timezone: str = "Asia/Shanghai",
        definition_sessions: tuple[tuple[time, time], ...] = ((time(9, 30), time(15, 0)),),
        second_timezone: str | None = None,
        second_sessions: tuple[tuple[time, time], ...] | None = None,
    ) -> InMemoryCalendarAxisDataProvider:
        definitions = [
            CalendarDefinition("SSE", "v1", definition_timezone, definition_sessions),
            CalendarDefinition(
                "SZSE",
                "v1",
                second_timezone or definition_timezone,
                second_sessions or definition_sessions,
            ),
        ]
        facts = []
        for day, is_open in sessions:
            facts.append(CalendarSessionFact("SSE", day, is_open, "v1"))
            facts.append(CalendarSessionFact("SZSE", day, is_open, "v1"))
        return InMemoryCalendarAxisDataProvider(definitions, facts)

    @staticmethod
    def _resolve(provider: InMemoryCalendarAxisDataProvider, start: date, end: date):
        return resolve_calendar_axis(
            provider,
            policy_key="strict_compatible",
            policy_version="1",
            start_date=start,
            end_date=end,
            calendar_ids=("SZSE", "SSE"),
        )

    def test_a01_a02_compatible_axis_is_order_independent(self) -> None:
        days = tuple((date(2026, 1, 2) + timedelta(days=i), True) for i in range(3))
        first = self._resolve(self._provider(sessions=days), days[0][0], days[-1][0])
        second = self._resolve(self._provider(sessions=days), days[0][0], days[-1][0])
        self.assertEqual(first.status, CalendarAxisStatus.COMPATIBLE)
        self.assertEqual(first.calendar_ids, ("SSE", "SZSE"))
        self.assertEqual(first.session_signature, second.session_signature)

    def test_a03_is_open_difference_is_blocking_and_complete(self) -> None:
        start = date(2026, 1, 2)
        provider = InMemoryCalendarAxisDataProvider(
            (CalendarDefinition("SSE", "v1", "Asia/Shanghai", ((time(9), time(10)),)),
             CalendarDefinition("SZSE", "v1", "Asia/Shanghai", ((time(9), time(10)),))),
            (CalendarSessionFact("SSE", start, True, "v1"),
             CalendarSessionFact("SZSE", start, False, "v1")),
        )
        result = self._resolve(provider, start, start)
        self.assertEqual(result.status, CalendarAxisStatus.INCOMPATIBLE)
        self.assertEqual(result.differences[0].field, CalendarAxisDifferenceField.IS_OPEN)
        self.assertEqual(set(result.differences[0].values_by_calendar), {"SSE", "SZSE"})

    def test_a04_window_difference_is_blocking(self) -> None:
        day = date(2026, 1, 2)
        provider = InMemoryCalendarAxisDataProvider(
            (CalendarDefinition("SSE", "v1", "Asia/Shanghai", ((time(9), time(10)),)),
             CalendarDefinition("SZSE", "v1", "Asia/Shanghai", ((time(9), time(11)),))),
            (CalendarSessionFact("SSE", day, True, "v1"), CalendarSessionFact("SZSE", day, True, "v1")),
        )
        result = self._resolve(provider, day, day)
        self.assertEqual(result.status, CalendarAxisStatus.INCOMPATIBLE)
        self.assertEqual(result.differences[0].field, CalendarAxisDifferenceField.SESSIONS)

    def test_a05_a06_timezone_mismatch_is_not_compatible(self) -> None:
        day = date(2026, 1, 2)
        result = self._resolve(
            self._provider(sessions=((day, True),), second_timezone="UTC"), day, day
        )
        self.assertEqual(result.status, CalendarAxisStatus.INCOMPATIBLE)
        self.assertTrue(result.differences)

    def test_a07_missing_fact_is_not_closed(self) -> None:
        day = date(2026, 1, 2)
        provider = InMemoryCalendarAxisDataProvider(
            (CalendarDefinition("SSE", "v1", "Asia/Shanghai", ((time(9), time(10)),)),
             CalendarDefinition("SZSE", "v1", "Asia/Shanghai", ((time(9), time(10)),))),
            (CalendarSessionFact("SSE", day, True, "v1"),),
        )
        result = self._resolve(provider, day, day)
        self.assertEqual(result.status, CalendarAxisStatus.INCOMPATIBLE)
        self.assertEqual(result.differences[0].field, CalendarAxisDifferenceField.MISSING_FACT)

    def test_a08_open_without_template_is_unresolved(self) -> None:
        day = date(2026, 1, 2)
        provider = InMemoryCalendarAxisDataProvider(
            (CalendarDefinition("SSE", "v1", "Asia/Shanghai", ()),
             CalendarDefinition("SZSE", "v1", "Asia/Shanghai", ())),
            (CalendarSessionFact("SSE", day, True, "v1"), CalendarSessionFact("SZSE", day, True, "v1")),
        )
        result = self._resolve(provider, day, day)
        self.assertEqual(result.status, CalendarAxisStatus.INCOMPATIBLE)
        self.assertEqual(result.differences[0].field, CalendarAxisDifferenceField.UNRESOLVED_SESSION)

    def test_a09_json_error_precedence_and_window_limit(self) -> None:
        with self.assertRaises(CalendarCrossMidnightUnsupportedError):
            normalize_window_payloads([{"start": "09:00", "end": "10:00", "day_offset": 1, "end_day_offset": 0}])
        with self.assertRaises(CalendarSessionWindowLimitExceededError):
            normalize_window_payloads([
                {"start": "09:00", "end": "10:00", "day_offset": 0, "end_day_offset": 0}
            ] * 17)

    def test_a11_warmup_is_separate_from_formal(self) -> None:
        from app.backtesting.data.warmup import CoverageBoundedWarmupSessionResolver, resolve_warmup_sessions
        days = tuple((date(2026, 1, 1) + timedelta(days=i), True) for i in range(5))
        provider = self._provider(sessions=days)
        warmup = resolve_warmup_sessions(
            provider,
            calendar_ids=("SSE", "SZSE"),
            first_formal_session=date(2026, 1, 5),
            requested_sessions=2,
            resolver=CoverageBoundedWarmupSessionResolver({"SSE": date(2026, 1, 1), "SZSE": date(2026, 1, 1)}),
        )
        self.assertEqual(warmup.status.value, "ready")
        self.assertTrue(all(item.session_date < date(2026, 1, 5) for item in warmup.resolved_sessions))

    def test_a12_a13_neighbor_states_are_distinct(self) -> None:
        from app.backtesting.calendar_axis import NeighborResult, NeighborState
        target = date(2026, 1, 2)
        none = NeighborResult(NeighborState.NONE_WITHIN_COVERAGE, target)
        unknown = NeighborResult(NeighborState.UNKNOWN_COVERAGE, target)
        self.assertNotEqual(none.state, unknown.state)

    def test_a26_a27_a46_cutoff_profile_is_frozen(self) -> None:
        boundary = QueryBoundary(data_cutoff=datetime(2026, 1, 1, 16, 30, tzinfo=UTC), include_cutoff_day=True)
        from app.backtesting.calendar_axis import CalendarPITContext
        context = CalendarPITContext.from_query_boundary(boundary)
        self.assertEqual(context.cutoff_local_date, date(2026, 1, 2))
        self.assertEqual(context.pit_profile, "strict_calendar_cutoff")
        self.assertEqual(context.profile_version, "calendar_pit_profile@1:H")

    def test_a33_warmup_513_is_rejected_before_resolution(self) -> None:
        from app.backtesting.data.warmup import resolve_warmup_sessions
        with self.assertRaises(LookbackSessionsLimitExceededError) as caught:
            resolve_warmup_sessions(
                self._provider(sessions=((date(2026, 1, 2), True),)),
                calendar_ids=("SSE", "SZSE"),
                first_formal_session=date(2026, 1, 3),
                requested_sessions=513,
            )
        self.assertIn("512", str(caught.exception))

    def test_a14_calendar_set_limit_is_checked_before_provider_reads(self) -> None:
        ids = tuple(f"C{index:02d}" for index in range(33))
        with self.assertRaisesRegex(Exception, "32"):
            CalendarSnapshotRequest(
                calendar_ids=ids,
                formal_start=date(2026, 1, 2),
                formal_end=date(2026, 1, 2),
                warmup_sessions=0,
                query_boundary=QueryBoundary(
                    data_cutoff=datetime(2026, 1, 3, tzinfo=UTC),
                    include_cutoff_day=True,
                ),
            )

    def test_a15_a16_missing_capability_is_unknown_not_supported(self) -> None:
        result = select_capability_declaration(
            (),
            capability="suspension",
            effective_day=date(2026, 1, 2),
        )
        self.assertTrue(result.missing)
        self.assertEqual(result.value.value, "unknown")

    def test_capability_pit_filter_precedes_specificity_fallback(self) -> None:
        from app.backtesting.calendar_axis import CalendarPITContext

        priority = CalendarSourcePriority(
            source="official",
            source_priority_version="v1",
            source_priority=1,
            source_revision_order=1,
            source_revision="r1",
            known_at=NOW,
            observed_at=NOW,
            bootstrap_seed_hash=SEED_HASH,
        )
        common = {
            "capability": "suspension",
            "value": "unsupported",
            "applicability": "required",
            "source": "official",
            "source_revision": "r1",
            "source_priority_fact_id": priority.fact_id,
            "source_priority_version": "v1",
            "source_priority": 1,
            "source_revision_order": 1,
            "bootstrap_seed_id": priority.bootstrap_seed_id,
            "bootstrap_seed_version": 1,
            "bootstrap_seed_hash": SEED_HASH,
            "observed_at": NOW,
        }
        provider = CalendarCapabilityDeclaration(
            scope_kind="provider",
            scope_key="provider:memory",
            provider_key="memory",
            known_at=NOW,
            **common,
        )
        calendar = CalendarCapabilityDeclaration(
            scope_kind="calendar",
            scope_key="calendar:SSE",
            calendar_id="SSE",
            registry_fact_id=uuid4(),
            registry_version=1,
            value="supported",
            known_at=datetime(2026, 1, 4, tzinfo=UTC),
            **{key: value for key, value in common.items() if key != "value"},
        )

        result = select_capability_declaration(
            (calendar, provider),
            capability="suspension",
            effective_day=date(2026, 1, 2),
            pit_context=CalendarPITContext.from_query_boundary(
                QueryBoundary(
                    data_cutoff=datetime(2026, 1, 3, tzinfo=UTC),
                    include_cutoff_day=True,
                )
            ),
            provider_key="memory",
            calendar_id="SSE",
            source_priorities=(priority,),
        )

        self.assertIs(result.declaration, provider)
        self.assertEqual(result.specificity, 1)
        self.assertEqual(result.value.value, "unsupported")

    def test_a36_a48_capability_scope_and_key_are_canonical(self) -> None:
        with self.assertRaises(CalendarJsonInvalidError):
            CalendarCapabilityDeclaration(
                scope_kind="provider",
                scope_key="calendar:SSE",
                capability="price_limit_execution",
                provider_key="sql-calendar",
            )
        with self.assertRaises(CalendarJsonInvalidError):
            CalendarCapabilityDeclaration(
                scope_kind="provider",
                scope_key="provider:sql-calendar",
                capability="suspension",
                provider_key="sql-calendar",
                applicability="optional",
            )

    def test_a20_a29_cutoff_selects_the_historical_fact_version(self) -> None:
        from app.backtesting.calendar_axis import CalendarPITContext
        priority = CalendarSourcePriority(
            source="official",
            source_priority_version="v1",
            source_priority=1,
            source_revision_order=1,
            source_revision="r1",
            known_at=NOW,
            observed_at=NOW,
            bootstrap_seed_hash=SEED_HASH,
        )
        first = CalendarSessionFact(
            "SSE", date(2026, 1, 2), True, "v1",
            source="official", source_revision="old", known_at=NOW,
            observed_at=NOW, source_priority_fact_id=priority.fact_id,
            source_priority_version="v1", source_priority=1,
            source_revision_order=1, bootstrap_seed_id=priority.bootstrap_seed_id,
            bootstrap_seed_version=1, bootstrap_seed_hash=SEED_HASH,
        )
        later_time = datetime(2026, 1, 2, tzinfo=UTC)
        second = CalendarSessionFact(
            "SSE", date(2026, 1, 2), False, "v1",
            source="official", source_revision="new", fact_version=2,
            logical_fact_key=first.logical_fact_key,
            supersedes_fact_id=first.fact_id,
            known_at=later_time, observed_at=later_time,
            source_priority_fact_id=priority.fact_id,
            source_priority_version="v1", source_priority=1,
            source_revision_order=1, bootstrap_seed_id=priority.bootstrap_seed_id,
            bootstrap_seed_version=1, bootstrap_seed_hash=SEED_HASH,
        )
        early = select_pit_candidate(
            (first, second), effective_day=first.session_date,
            pit_context=CalendarPITContext.from_query_boundary(
                QueryBoundary(data_cutoff=datetime(2026, 1, 1, 12, tzinfo=UTC), include_cutoff_day=True)
            ), source_priorities=(priority,),
        )
        late = select_pit_candidate(
            (first, second), effective_day=first.session_date,
            pit_context=CalendarPITContext.from_query_boundary(
                QueryBoundary(data_cutoff=datetime(2026, 1, 3, tzinfo=UTC), include_cutoff_day=True)
            ), source_priorities=(priority,),
        )
        self.assertIs(early, first)
        self.assertIs(late, second)

    def test_a30_same_source_revision_rank_conflict_does_not_use_uuid(self) -> None:
        from app.backtesting.calendar_axis import CalendarPITContext
        priority = CalendarSourcePriority(
            source="official", source_priority_version="v1", source_priority=1,
            source_revision_order=1, source_revision="r1", known_at=NOW,
            observed_at=NOW, bootstrap_seed_hash=SEED_HASH,
        )
        common = dict(
            calendar_id="SSE", session_date=date(2026, 1, 2), is_open=True,
            definition_version="v1", source="official", known_at=NOW,
            observed_at=NOW, source_priority_fact_id=priority.fact_id,
            source_priority_version="v1", source_priority=1,
            source_revision_order=1, bootstrap_seed_id=priority.bootstrap_seed_id,
            bootstrap_seed_version=1, bootstrap_seed_hash=SEED_HASH,
        )
        left = CalendarSessionFact(**common, source_revision="r-left")
        right = CalendarSessionFact(**common, source_revision="r-right", fact_id=__import__("uuid").uuid4())
        with self.assertRaises(Exception):
            select_pit_candidate(
                (left, right), effective_day=date(2026, 1, 2),
                pit_context=CalendarPITContext.from_query_boundary(
                    QueryBoundary(data_cutoff=datetime(2026, 1, 3, tzinfo=UTC), include_cutoff_day=True)
                ), source_priorities=(priority,),
            )

    def test_a39_canonical_provider_without_cutoff_cannot_use_legacy_axis(self) -> None:
        day = date(2026, 1, 2)
        provider = InMemoryCalendarAxisDataProvider(
            (CalendarDefinition("SSE", "v1", "Asia/Shanghai", ((time(9), time(10)),)),),
            (CalendarSessionFact("SSE", day, True, "v1", known_at=NOW),),
        )
        with self.assertRaises(DataCutoffRequiredError):
            resolve_calendar_axis(
                provider, policy_key="strict_compatible", policy_version="1",
                start_date=day, end_date=day, calendar_ids=("SSE",),
            )

    def test_a43_priority_root_has_immutable_bootstrap_and_no_self_reference(self) -> None:
        priority = CalendarSourcePriority(
            source="official", source_priority_version="v1", source_priority=1,
            source_revision_order=1, source_revision="r1", known_at=NOW,
            observed_at=NOW, bootstrap_seed_hash=SEED_HASH,
        )
        self.assertIsNone(priority.supersedes_fact_id)
        priority.strict_validate()

    def test_explicit_calendar_content_hash_is_validated_at_construction(self) -> None:
        with self.assertRaises(CalendarJsonInvalidError):
            CalendarDefinition(
                "SSE",
                "v1",
                "Asia/Shanghai",
                ((time(9), time(10)),),
                content_hash="z" * 64,
            )

        with self.assertRaises(CalendarJsonInvalidError):
            CalendarDefinition(
                "SSE",
                "v1",
                "Asia/Shanghai",
                ((time(9), time(10)),),
                content_hash="a" * 64,
            )

    def test_a32_all_closed_has_no_formal_sessions(self) -> None:
        day = date(2026, 1, 2)
        result = self._resolve(self._provider(sessions=((day, False),)), day, day)
        self.assertEqual(result.status, CalendarAxisStatus.COMPATIBLE)
        self.assertEqual(result.resolved_sessions, ())


if __name__ == "__main__":
    unittest.main()
