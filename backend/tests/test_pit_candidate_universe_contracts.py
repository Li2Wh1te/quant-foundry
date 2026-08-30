"""Contract tests for the task-15 PIT candidate-universe boundary."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import unittest
from uuid import uuid4

from app.backtesting.calendar_axis import (
    CalendarAxisResolution,
    CalendarAxisStatus,
    SessionPoint,
)
from app.backtesting.data.requests import (
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DataPreflightRequest,
    DateRange,
    InstrumentScopeMode,
    MarketScope,
    PriceBasis,
    QueryBoundary,
    UniverseQueryPolicy,
)
from app.backtesting.data.universe import (
    CANDIDATE_CALENDAR_NOT_PREFLIGHTED,
    CANDIDATE_MARKET_DATA_INCOMPLETE,
    CandidateEligibilityContext,
    CandidateInput,
    UniverseScopeStatus,
    evaluate_candidate,
    filter_candidates,
)
from app.backtesting.preflight import resolve_dynamic_universe_scope


UTC = timezone.utc


def _request(mode: InstrumentScopeMode = InstrumentScopeMode.DYNAMIC) -> DataPreflightRequest:
    """Build one fully explicit PIT request for these pure contract tests."""

    return DataPreflightRequest(
        provider_key="memory",
        requested_window=DateRange(date(2026, 1, 2), date(2026, 1, 9)),
        frequency="1d",
        rule_package=ContractRef("rules.etf", 1),
        market_scope=MarketScope(exchanges=("SSE",), asset_classes=("etf",)),
        universe_query_policy=UniverseQueryPolicy((ContractRef("scope.etf", 1),))
        if mode is not InstrumentScopeMode.FIXED
        else UniverseQueryPolicy(),
        instrument_scope_mode=mode,
        required_capabilities=(DataCapability.BARS,),
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        query_boundary=QueryBoundary(
            datetime(2026, 1, 10, 0, 0, tzinfo=UTC),
            include_cutoff_day=True,
        ),
        static_instrument_ids=(uuid4(),) if mode is InstrumentScopeMode.FIXED else (),
        qualification_policy_version=ContractRef("candidate.qualification", 1),
    )


class CandidateContractTests(unittest.TestCase):
    """Verify immutable, deterministic candidate qualification semantics."""

    def test_effective_date_and_cutoff_are_distinct(self) -> None:
        context = CandidateEligibilityContext(
            effective_date=date(2026, 1, 5),
            data_cutoff=datetime(2026, 1, 6, tzinfo=UTC),
            resolved_calendar_ids=("sse",),
            market_scope=MarketScope(exchanges=("SSE",), asset_classes=("etf",)),
        )
        self.assertEqual(context.effective_date, date(2026, 1, 5))
        self.assertEqual(context.data_cutoff.date(), date(2026, 1, 6))

    def test_calendar_permission_and_explicit_quality_failure_filter_only_candidate(self) -> None:
        context = CandidateEligibilityContext(
            effective_date=date(2026, 1, 5),
            data_cutoff=datetime(2026, 1, 6, tzinfo=UTC),
            resolved_calendar_ids=("SSE",),
            market_scope=MarketScope(exchanges=("SSE",), asset_classes=("etf",)),
        )
        candidate = CandidateInput(
            instrument_id=uuid4(),
            calendar_id="SZSE",
            trading_code="510300",
            name="ETF",
            display_name="ETF",
            asset_class="etf",
            exchange="SSE",
            market_data_evidence={"complete": False},
        )
        result = evaluate_candidate(candidate, context)
        self.assertFalse(result.eligible)
        self.assertIn(CANDIDATE_CALENDAR_NOT_PREFLIGHTED, result.reason_codes)
        self.assertIn(CANDIDATE_MARKET_DATA_INCOMPLETE, result.reason_codes)

    def test_filter_is_order_independent_and_does_not_treat_empty_actions_as_failure(self) -> None:
        context = CandidateEligibilityContext(
            effective_date=date(2026, 1, 5),
            data_cutoff=datetime(2026, 1, 6, tzinfo=UTC),
            resolved_calendar_ids=("SSE",),
            market_scope=MarketScope(exchanges=("SSE",), asset_classes=("etf",)),
        )
        first = CandidateInput(
            instrument_id=uuid4(),
            calendar_id="SSE",
            trading_code="510300",
            name="ETF",
            display_name="ETF",
            asset_class="etf",
            exchange="SSE",
            corporate_action_evidence={},
        )
        second = CandidateInput(
            instrument_id=uuid4(),
            calendar_id="SSE",
            trading_code="510500",
            name="ETF2",
            display_name="ETF2",
            asset_class="etf",
            exchange="SSE",
        )
        left = filter_candidates((first, second), context)
        right = filter_candidates((second, first), context)
        self.assertEqual(left.result_hash, right.result_hash)
        self.assertEqual(
            tuple(item.instrument_id for item in left.eligible_candidates),
            tuple(item.instrument_id for item in right.eligible_candidates),
        )

    def test_scope_provider_must_return_named_calendar_and_session_signature(self) -> None:
        request = _request()
        axis = CalendarAxisResolution(
            policy_key="strict_compatible",
            policy_version="1",
            start_date=request.requested_window.start_date,
            end_date=request.requested_window.end_date,
            calendar_ids=("SSE",),
            session_signature="a" * 64,
            timezone="Asia/Shanghai",
            resolved_sessions=(
                SessionPoint(
                    date(2026, 1, 5),
                    "2026-01-05",
                    "Asia/Shanghai",
                    ((time(9, 30), time(15, 0)),),
                ),
            ),
            status=CalendarAxisStatus.COMPATIBLE,
            differences=(),
        )

        class Provider:
            def resolve_dynamic_universe_scope(self, value):
                return {
                    "resolved_calendar_ids": ("SSE",),
                    "calendar_session_signature": axis.session_signature,
                    "calendar_axis_resolution": axis,
                    "capability_summary": {
                        "universe": "available",
                        "identity": "available",
                        "mapping": "available",
                        "rules": "available",
                        "market_data": "available",
                        "corporate_actions": "available",
                        "status": "available",
                    },
                }

        resolution = resolve_dynamic_universe_scope(
            request,
            Provider(),
            profile="internal_link_acceptance@1",
        )
        self.assertIs(resolution.status, UniverseScopeStatus.READY)
        self.assertEqual(resolution.resolved_calendar_ids, ("SSE",))
        self.assertEqual(len(resolution.snapshot_hash), 64)

    def test_missing_scope_provider_is_request_level_capability_block(self) -> None:
        resolution = resolve_dynamic_universe_scope(_request())
        self.assertIs(resolution.status, UniverseScopeStatus.BLOCKED)
        self.assertEqual(resolution.primary_issue_code, "universe_capability_missing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
