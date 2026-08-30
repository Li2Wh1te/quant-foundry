"""Round-two integration coverage for the real memory chunk/provider path.

These tests deliberately open a ``MemoryDataSession`` and
``MemoryDataChunkSession``.  They do not assert only on source rows or on a
strategy-side fake DTO, so filtering, authorization, PIT coordinates, and the
existing coverage qualification port are exercised together.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
import socket
import unittest
from uuid import UUID

from app.backtesting.data.memory import MemoryDataProvider
from app.backtesting.data.protocols import InstrumentCoverageQualification
from app.backtesting.data.reports import DataCoverageReport
from app.backtesting.data.requests import (
    DataCapability,
    DataChunkQuery,
    DataRequest,
    InstrumentScopeMode,
    QualityStatus,
    QueryBoundary,
    UniverseQuery,
    UniverseQueryPolicy,
)
from app.backtesting.data.errors import InvalidDataRequestError
from app.strategy_protocol.data_view import InstrumentCandidateDTO
from tests.test_backtesting_memory_provider import _rule_report_for
from tests.test_pit_candidate_universe_provider import (
    DAYS,
    RULE,
    SCOPE_RULE,
    _dataset,
    _intent,
)


UTC8 = timezone(timedelta(hours=8))


class _QualificationSource:
    """Explicit PIT source and the existing typed qualification port."""

    def __init__(self, dataset, *, bad: dict[UUID, set[DataCapability]] | None = None):
        self.dataset = dataset
        self.bad = bad or {}
        self.queries: list[UniverseQuery] = []

    def resolve_dynamic_universe_scope(self, request):
        del request
        return {"resolved_calendar_ids": ("XSHG",)}

    def query(self, query: UniverseQuery):
        self.queries.append(query)
        return self.dataset.instruments

    def qualify_instrument(self, request):
        reports = []
        failed = self.bad.get(request.instrument_id, set())
        for capability in request.required_capabilities:
            is_bad = capability in failed
            reports.append(
                DataCoverageReport(
                    requested_window=request.history_envelope,
                    capability=capability,
                    instrument_ids=(request.instrument_id,),
                    expected_count=1,
                    complete_count=0 if is_bad else 1,
                    partial_count=0,
                    invalid_count=0,
                    unavailable_count=1 if is_bad else 0,
                    quality_status=(
                        QualityStatus.UNAVAILABLE if is_bad else QualityStatus.COMPLETE
                    ),
                )
            )
        return InstrumentCoverageQualification(
            instrument_id=request.instrument_id,
            eligible=True,
            coverage_reports=tuple(reports),
            reason_codes=(),
            evidence_summary={"source": "round2-fixture"},
        )


def _admitted(provider: MemoryDataProvider, intent):
    report = provider.preflight(intent, profile="internal_link_acceptance@1")
    if report.blocked:
        raise AssertionError(report.issues)
    request = DataRequest.from_admission(
        intent,
        report,
        rule_preflight_report=_rule_report_for(intent),
    )
    session = provider.open_session(request)
    session_report = session.preflight()
    if session_report.blocked:
        raise AssertionError(session_report.issues)
    chunk = session.open_chunk(
        DataChunkQuery(
            chunk_index=0,
            first_session_id=session.resolved_sessions[0].session_id,
            last_session_id=session.resolved_sessions[-1].session_id,
            fact_types=(DataCapability.BARS, DataCapability.UNIVERSE),
        )
    )
    chunk.validate_consistency()
    return report, request, session, chunk


def _query(intent, report, *, day=DAYS[0], mode=None, cutoff=None):
    return UniverseQuery(
        rule=intent.rule_package,
        market_scope=intent.market_scope,
        effective_date=day,
        boundary=QueryBoundary(
            cutoff or intent.query_boundary.data_cutoff,
            include_cutoff_day=intent.query_boundary.include_cutoff_day,
        ),
        allowed_calendar_ids=report.resolved_calendar_ids,
        scope_mode=mode or intent.instrument_scope_mode,
        universe_query_policy=intent.universe_query_policy,
        universe_scope_snapshot_hash=report.universe_scope_snapshot_hash,
    )


class RealChunkQualificationTests(unittest.TestCase):
    """Candidate qualification is proven through the real chunk boundary."""

    def test_rules_bar_actions_and_status_failures_filter_only_the_bad_rows(self):
        dataset, first, second = _dataset()
        # The first spec carries the wrong rule package; the second has all
        # required dimensions available through the explicit port.
        bad_rule = replace(
            dataset.instrument(first),
            rule_package_reference=type(RULE)("other.rules", 1),
        )
        dataset = replace(dataset, instruments=(bad_rule, dataset.instrument(second)))
        source = _QualificationSource(
            dataset,
            bad={
                second: {
                    DataCapability.BARS,
                    DataCapability.ACTIONS,
                    DataCapability.STATUS,
                }
            },
        )
        provider = MemoryDataProvider(dataset, universe_provider=source)
        intent = _intent()
        report, _, _, chunk = _admitted(provider, intent)
        rows = chunk.universe(_query(intent, report))
        self.assertEqual(rows, ())
        reasons = chunk.universe_filter_reason_counts
        self.assertIn("rule_package_mismatch", reasons)
        self.assertIn("candidate_market_data_incomplete", reasons)
        self.assertIn("candidate_corporate_action_incomplete", reasons)
        self.assertIn("candidate_status_incomplete", reasons)

    def test_empty_actions_evidence_is_not_a_negative_proof(self):
        dataset, first, second = _dataset()

        class EmptyActions(_QualificationSource):
            def qualify_instrument(self, request):
                result = super().qualify_instrument(request)
                if request.instrument_id != first:
                    return result
                # No action report at all: because the spec explicitly
                # requires actions this must be filtered, rather than treated
                # as a complete-zero assertion.
                return replace(
                    result,
                    qualification_hash="",
                    coverage_reports=tuple(
                        report
                        for report in result.coverage_reports
                        if report.capability
                        not in (DataCapability.ACTIONS, DataCapability.STATUS)
                    ),
                )

        source = EmptyActions(dataset)
        provider = MemoryDataProvider(dataset, universe_provider=source)
        intent = _intent()
        report, _, _, chunk = _admitted(provider, intent)
        rows = chunk.universe(_query(intent, report))
        self.assertEqual(tuple(row.instrument_id for row in rows), (second,))
        self.assertIn(
            "candidate_corporate_action_incomplete",
            chunk.universe_filter_reason_counts,
        )

    def test_future_identity_is_hidden_and_cross_day_pit_rows_change(self):
        dataset, first, second = _dataset()
        original = dataset.instrument(first)
        old = replace(
            original,
            display=replace(
                original.display,
                trading_code="OLD510300",
                name="Old ETF",
                display_name="Old ETF",
            ),
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2026, 1, 6, tzinfo=timezone.utc),
        )
        new = replace(
            original,
            valid_from=datetime(2026, 1, 6, tzinfo=timezone.utc),
        )
        dataset = replace(dataset, instruments=(old, new, dataset.instrument(second)))

        class HistoricalSource(_QualificationSource):
            def query(self, query):
                self.queries.append(query)
                # Return both versions and let the chunk select the version
                # whose validity interval covers the requested effective date.
                return (old, new, self.dataset.instrument(second))

        source = HistoricalSource(dataset)
        provider = MemoryDataProvider(dataset, universe_provider=source)
        intent = _intent()
        report, _, _, chunk = _admitted(provider, intent)
        first_rows = chunk.universe(_query(intent, report, day=DAYS[0]))
        second_rows = chunk.universe(_query(intent, report, day=DAYS[1]))
        first_code = next(
            row.display.trading_code for row in first_rows if row.instrument_id == first
        )
        second_code = next(
            row.display.trading_code for row in second_rows if row.instrument_id == first
        )
        self.assertEqual(first_code, "OLD510300")
        self.assertEqual(second_code, "510300")
        self.assertEqual({query.effective_date for query in source.queries}, set(DAYS))

        future = replace(
            new,
            valid_from=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        # A source that returns only a future-valid identity yields a valid
        # empty candidate result, never a current-row fallback.
        class FutureSource(HistoricalSource):
            def query(self, query):
                self.queries.append(query)
                return (future,)

        future_provider = MemoryDataProvider(
            dataset,
            universe_provider=FutureSource(dataset),
        )
        future_report, _, _, future_chunk = _admitted(future_provider, intent)
        self.assertEqual(
            future_chunk.universe(_query(intent, future_report)), ()
        )

    def test_hybrid_explicitly_unions_fixed_and_dynamic_when_source_omits_fixed(self):
        dataset, first, second = _dataset()

        class DynamicOnly(_QualificationSource):
            def query(self, query):
                self.queries.append(query)
                return (self.dataset.instrument(second),)

        source = DynamicOnly(dataset)
        provider = MemoryDataProvider(dataset, universe_provider=source)
        intent = _intent(mode=InstrumentScopeMode.HYBRID, static_instrument_ids=(first,))
        report, _, _, chunk = _admitted(provider, intent)
        rows = chunk.universe(_query(intent, report, mode=InstrumentScopeMode.HYBRID))
        self.assertEqual(
            {row.instrument_id for row in rows},
            {first, second},
        )
        self.assertEqual(
            tuple(row.instrument_id for row in rows),
            tuple(sorted((first, second), key=str)),
        )

    def test_all_candidates_filtered_is_a_valid_empty_result_and_no_network_is_used(self):
        dataset, first, second = _dataset()
        source = _QualificationSource(
            dataset,
            bad={
                first: {DataCapability.BARS, DataCapability.ACTIONS, DataCapability.STATUS},
                second: {DataCapability.BARS, DataCapability.ACTIONS, DataCapability.STATUS},
            },
        )
        provider = MemoryDataProvider(dataset, universe_provider=source)
        intent = _intent()
        report, _, _, chunk = _admitted(provider, intent)
        original_socket = socket.socket

        def forbidden_socket(*args, **kwargs):
            raise AssertionError("network access is forbidden in memory universe")

        socket.socket = forbidden_socket
        try:
            self.assertEqual(chunk.universe(_query(intent, report)), ())
        finally:
            socket.socket = original_socket
        self.assertEqual(provider.universe_read_count, 1)

    def test_three_modes_are_stably_ordered_and_duplicate_identity_is_order_independent(self):
        dataset, first, second = _dataset()
        duplicate = dataset.instrument(first)

        class DuplicateSource(_QualificationSource):
            def query(self, query):
                self.queries.append(query)
                return (self.dataset.instrument(second), duplicate, duplicate)

        source = DuplicateSource(dataset)
        provider = MemoryDataProvider(dataset, universe_provider=source)
        intent = _intent()
        report, _, _, chunk = _admitted(provider, intent)
        rows = chunk.universe(_query(intent, report))
        self.assertEqual(
            tuple(row.instrument_id for row in rows),
            tuple(sorted({first, second}, key=str)),
        )
        # Reversing the provider order must leave both values and the query
        # hash unchanged because the chunk sorts rows before de-duplication.
        source.query = lambda query: (
            duplicate,
            dataset.instrument(second),
            duplicate,
        )
        second_rows = chunk.universe(
            _query(intent, report, day=DAYS[1])
        )
        self.assertEqual(
            tuple(row.instrument_id for row in second_rows),
            tuple(sorted({first, second}, key=str)),
        )


class CandidateProjectionContractTests(unittest.TestCase):
    """The strategy candidate projection has exactly six fields."""

    def test_strategy_dto_has_only_the_frozen_six_fields(self):
        self.assertEqual(
            tuple(item.name for item in fields(InstrumentCandidateDTO)),
            (
                "instrument_id",
                "trading_code",
                "name",
                "display_name",
                "asset_class",
                "exchange",
            ),
        )
        candidate = InstrumentCandidateDTO(
            instrument_id=UUID("00000000-0000-4000-8000-000000000001"),
            trading_code="510300",
            name="ETF",
            display_name="ETF",
            asset_class="etf",
            exchange="SSE",
        )
        self.assertFalse(hasattr(candidate, "metadata"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
