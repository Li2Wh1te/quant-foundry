"""Regression tests for the non-pageable Task-11 resource gate."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from app.backtesting.calendar_axis import CalendarAxisStatus
from app.backtesting.data.errors import CalendarPreflightResourceLimitExceededError
from app.backtesting.data.reports import DataPreflightReport, PreflightIssue
from app.backtesting.data.requests import (
    CALENDAR_AXIS_POLICY,
    CHUNK_POLICY,
    MAX_LOOKBACK_SESSIONS,
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DateRange,
    InstrumentScopeMode,
    IssueSeverity,
    MarketScope,
    PriceBasis,
    PreflightStatus,
    QueryBoundary,
    QualityMode,
    UniverseQueryPolicy,
)
from app.backtesting.data.memory import MemoryDataProvider
from app.backtesting.data.sessions import (
    AuthoritativeDataSession,
    DataSessionState,
    _provider_has_canonical_calendar_metadata,
)

from tests.test_backtesting_data_session import make_request
from tests.test_backtesting_memory_provider import (
    admit,
    build_fixture_a,
    make_intent,
)


class _CanonicalCalendarProvider:
    """Minimal strict provider whose snapshot attempt exceeds its budget."""

    def registries(self, calendar_id: str) -> tuple[object, ...]:
        return (object(),)

    def open_calendar_snapshot(self, request: object) -> object:
        raise CalendarPreflightResourceLimitExceededError(
            "calendar preflight resource limit exceeded",
            details={"observed": 33, "limit": 32},
        )


class Task11ResourceLimitTestCase(unittest.TestCase):
    def test_utf8_budget_includes_issue_display_fields(self) -> None:
        """The response budget must include Chinese issue title/message bytes."""

        max_bytes = 4 * 1024 * 1024
        boundary = QueryBoundary(
            data_cutoff=datetime(2026, 1, 3, tzinfo=timezone.utc),
            include_cutoff_day=True,
        )
        context = {
            "data_cutoff": "2026-01-03T00:00:00Z",
            "cutoff_local_date": "2026-01-03",
            "include_cutoff_day": True,
            "knowledge_as_of": None,
            "pit_profile": "strict_calendar_cutoff",
            "profile_version": "calendar_pit_profile@1:H",
        }
        issue = PreflightIssue(
            code="TEST_ERROR",
            severity=IssueSeverity.ERROR,
            scope="formal",
            message="超" * (max_bytes // 3 + 1),
        )
        with self.assertRaises(CalendarPreflightResourceLimitExceededError) as caught:
            DataPreflightReport(
                status=PreflightStatus.BLOCKED,
                generated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                provider_key="memory",
                capability_manifest_version=1,
                requested_window=DateRange(date(2026, 1, 2), date(2026, 1, 2)),
                scope_mode=InstrumentScopeMode.FIXED,
                resolved_calendar_ids=("SSE",),
                resolved_calendar_definitions=(),
                resolved_timezone="Asia/Shanghai",
                calendar_axis_policy=CALENDAR_AXIS_POLICY,
                calendar_compatibility_status=CalendarAxisStatus.COMPATIBLE,
                calendar_session_signature="a" * 64,
                resolved_sessions=(),
                warmup_sessions=(),
                max_lookback_sessions=MAX_LOOKBACK_SESSIONS,
                knowledge_as_of=None,
                non_strict_pit_capabilities=(),
                consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
                consistency_token_capability=False,
                consistency_token_contract=None,
                data_chunk_policy=CHUNK_POLICY,
                data_chunk_size_sessions=20,
                required_capabilities=(DataCapability.BARS,),
                rule_package=ContractRef("rules.test", 1),
                rule_exception_set=None,
                static_instrument_ids=(),
                mandatory_instrument_ids=(),
                strategy_price_bases=(PriceBasis.RAW,),
                engine_price_basis=PriceBasis.RAW,
                frequency="1d",
                warmup_sessions_count=0,
                market_scope=MarketScope(),
                universe_query_policy=UniverseQueryPolicy(),
                quality_mode=QualityMode.STRICT,
                source_revisions={},
                issues=(issue,),
                query_boundary=boundary,
                hash_schema_version=2,
                pit_context=context,
                calendar_revision_digest="b" * 64,
                snapshot_fingerprint="c" * 64,
                non_strict_pit=False,
                calendar_semantic_signature="d" * 64,
                warmup_session_signature="e" * 64,
                definition_usage_by_date=(
                    {
                        "scope": "formal",
                        "date": "2026-01-02",
                        "values_by_calendar": {"SSE": {"is_open": True}},
                    },
                ),
                calendar_summary={
                    "envelope": {
                        "start_date": "2026-01-02",
                        "end_date_exclusive": "2026-01-03",
                    }
                },
                session_summary={},
            )
        self.assertGreater(caught.exception.details["utf8_bytes"], max_bytes)

    def test_strict_snapshot_gate_does_not_probe_provider_rows(self) -> None:
        class ReadGuardProvider:
            def open_calendar_snapshot(self, request: object) -> object:
                return object()

            def registries(self, calendar_id: str) -> tuple[object, ...]:
                raise AssertionError("strict gate must not read registries")

            def definitions(self, calendar_id: str) -> tuple[object, ...]:
                raise AssertionError("strict gate must not read definitions")

        provider = ReadGuardProvider()
        self.assertTrue(_provider_has_canonical_calendar_metadata(provider, ("SSE",)))

    def test_authoritative_session_does_not_wrap_resource_failure_as_report(self) -> None:
        session = AuthoritativeDataSession(
            request=make_request(
                start=date(2026, 1, 5),
                end=date(2026, 1, 7),
            ),
            calendar_provider=_CanonicalCalendarProvider(),
        )

        with self.assertRaises(CalendarPreflightResourceLimitExceededError) as caught:
            session.preflight()

        self.assertEqual(caught.exception.code, "calendar_preflight_resource_limit_exceeded")
        self.assertIs(session.state, DataSessionState.BLOCKED)
        self.assertIsNone(session.report)
        self.assertEqual(session.resolved_sessions, ())
        self.assertEqual(session.warmup_sessions, ())

    def test_memory_provider_does_not_wrap_resource_failure_as_report(self) -> None:
        provider: MemoryDataProvider = build_fixture_a()
        intent = make_intent(start=date(2026, 1, 5), end=date(2026, 1, 7))
        calendar_provider = provider._dataset._calendar_axis_provider
        failure = CalendarPreflightResourceLimitExceededError(
            "calendar preflight resource limit exceeded",
            details={"issue_groups": 4097, "maximum": 4096},
        )

        with patch.object(provider, "_has_canonical_calendar_metadata", return_value=True), patch.object(
            calendar_provider, "open_calendar_snapshot", side_effect=failure
        ):
            with self.assertRaises(CalendarPreflightResourceLimitExceededError) as caught:
                provider.preflight(intent)

        self.assertIs(caught.exception, failure)
        self.assertEqual(caught.exception.code, "calendar_preflight_resource_limit_exceeded")

        # The authoritative MemoryDataSession also remains terminal when its
        # re-check receives the same non-pageable creation-gate error.
        request = admit(provider, intent)
        session = provider.open_session(request)
        with patch.object(provider, "_build_preflight_report", side_effect=failure):
            with self.assertRaises(CalendarPreflightResourceLimitExceededError):
                session.preflight()
        self.assertEqual(session.state, DataSessionState.BLOCKED)
        self.assertIsNone(session.report)


if __name__ == "__main__":
    unittest.main()
