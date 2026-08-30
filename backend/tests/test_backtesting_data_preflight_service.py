"""Acceptance tests for the Phase 2a preflight composition boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.data.preflight_service import (
    DataPreflightService,
    PreflightContext,
    RUN_KIND_INTERNAL_LINK_ACCEPTANCE,
    _scope_issue,
)
from app.backtesting.data.reports import PreflightIssue
from app.backtesting.data.requests import (
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DataPreflightRequest,
    DataRequest,
    DateRange,
    InstrumentScopeMode,
    IssueSeverity,
    MarketScope,
    PreflightStatus,
    PriceBasis,
    QueryBoundary,
    UniverseQueryPolicy,
    InternalFixture,
)
from app.backtesting.result_models import (
    BacktestAnalysisSummaryRecord,
    BacktestDataPreflightRecord,
    BacktestEquityCurveRecord,
    BacktestMetricRecord,
    ValuationStatus,
)
from app.backtesting.result_records import Base, RESULT_TABLE_NAMES
from app.backtesting.result_repository import (
    BacktestResultRepository,
    InternalResultNotVisibleError,
)
from app.backtesting.result_schemas import BacktestDataPreflightItem
from app.backtesting.result_router import (
    get_analysis_summary,
    list_data_preflight,
    list_equity_curve,
    list_metrics,
)
from fastapi import HTTPException


RULES = ContractRef("rules.fixture", 1)
IID = uuid4()
BOUNDARY = QueryBoundary(
    data_cutoff=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    include_cutoff_day=True,
)


def intent(*, dynamic: bool = False) -> DataPreflightRequest:
    """Build a minimal typed intent for pre-read gate tests."""

    return DataPreflightRequest(
        provider_key="fixture",
        requested_window=DateRange(date(2026, 8, 1), date(2026, 8, 31)),
        frequency="1d",
        rule_package=RULES,
        market_scope=MarketScope(exchanges=("XFIX",)),
        universe_query_policy=UniverseQueryPolicy(
            candidate_set_rules=(ContractRef("candidate.fixture", 1),)
            if dynamic
            else ()
        ),
        instrument_scope_mode=(
            InstrumentScopeMode.DYNAMIC if dynamic else InstrumentScopeMode.FIXED
        ),
        required_capabilities=(DataCapability.BARS,),
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        query_boundary=BOUNDARY,
        static_instrument_ids=() if dynamic else (IID,),
    )


def fixture(*, content_hash: str = "a" * 64) -> InternalFixture:
    """Create the named v1 fixture used by hash/persistence assertions."""

    return InternalFixture(
        fixture_key="quantity_action_coverage",
        fixture_version=1,
        capability="quantity_action_coverage",
        instrument_ids=(IID,),
        start_date=date(2025, 1, 1),
        end_date=date(2027, 1, 1),
        proof_summary="bounded fixture proof",
        content_hash=content_hash,
    )


class FakeProvider:
    """Provider stub whose read count proves pre-read gates short-circuit."""

    def __init__(self, report):
        self.report = report
        self.preflight_calls = 0

    def preflight(self, request):
        self.preflight_calls += 1
        return self.report


class PreflightServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from app.backtesting.data.preflight_service import _minimal_blocked_report, _issue

        self.base_report = _minimal_blocked_report(
            intent(),
            (_issue("provider_contract_violation", "测试底座报告已阻断。"),),
        )

    def test_internal_profile_binds_fixed_union_and_fixture_hash(self) -> None:
        provider = FakeProvider(self.base_report)
        service = DataPreflightService(provider)
        outcome = service.preflight(
            PreflightContext(request=intent(), provider=provider, fixtures=(fixture(),))
        )
        self.assertEqual(outcome.profile.run_kind, RUN_KIND_INTERNAL_LINK_ACCEPTANCE)
        self.assertEqual(outcome.fixed_instrument_ids, (IID,))
        self.assertEqual(outcome.status, PreflightStatus.BLOCKED)
        self.assertEqual(provider.preflight_calls, 1)
        changed = service.preflight(
            PreflightContext(
                request=intent(),
                provider=provider,
                fixtures=(fixture(content_hash="b" * 64),),
            )
        )
        self.assertNotEqual(outcome.report_hash, changed.report_hash)
        self.assertNotEqual(outcome.report.report_hash, changed.report.report_hash)

    def test_named_quantity_fixture_allows_ready_and_is_frozen_on_report(self) -> None:
        from app.backtesting.data.sessions import AuthoritativeDataSession
        from tests.test_backtesting_data_session import (
            COMMON_OPEN,
            J5,
            J7,
            build_provider,
            make_request,
        )

        request = replace(
            make_request(
                J5,
                J7,
                calendar_ids=("SSE", "SZSE"),
                instrument_id=IID,
            ),
            required_capabilities=(DataCapability.BARS, DataCapability.ACTIONS),
        )
        ready_report = AuthoritativeDataSession(
            request=request,
            calendar_provider=build_provider(COMMON_OPEN, COMMON_OPEN),
        ).preflight()
        provider = FakeProvider(ready_report)

        outcome = DataPreflightService(provider).preflight(
            PreflightContext(
                request=request,
                provider=provider,
                fixtures=(fixture(),),
            )
        )

        self.assertEqual(outcome.status, PreflightStatus.READY)
        self.assertEqual(provider.preflight_calls, 1)
        self.assertEqual(outcome.report.run_kind, "internal_link_acceptance")
        self.assertEqual(
            outcome.report.preflight_profile_key, "internal_link_acceptance"
        )
        self.assertEqual(outcome.report.preflight_profile_version, 1)
        self.assertEqual(outcome.report.resolved_instruments, (IID,))
        self.assertEqual(len(outcome.report.fixture_sources), 1)
        self.assertEqual(
            outcome.report.fixture_sources[0]["capability"],
            "quantity_action_coverage",
        )
        serialized = outcome.report.as_dict()
        self.assertEqual(serialized["run_kind"], "internal_link_acceptance")
        self.assertTrue(
            {
                "run_kind",
                "preflight_profile_key",
                "preflight_profile_version",
                "capability_manifest_version",
                "status",
                "scope_mode",
                "resolved_instruments",
                "coverage_reports",
                "instrument_mapping_coverage",
                "instrument_rule_fact_summary",
                "lookback_session_bar_coverage",
                "bar_validity_summary",
                "adjustment_series_policy",
                "universe_eligibility_policy_version",
                "universe_eligibility_summary",
                "missing_bars",
                "missing_fields",
                "invalid_bars",
                "incomplete_rules",
                "non_pit_sources",
                "source_revisions",
                "issues",
                "report_hash",
                "fixture_sources",
            }.issubset(serialized["details"])
        )
        self.assertEqual(
            serialized["details"]["fixture_sources"][0]["fixture_key"],
            "quantity_action_coverage",
        )

    def test_unnamed_status_mapping_is_blocked_before_provider_read(self) -> None:
        provider = FakeProvider(self.base_report)

        outcome = DataPreflightService(provider).preflight(
            PreflightContext(
                request=intent(),
                provider=provider,
                fixtures=(
                    {
                        "suspension": False,
                        "opening_availability": True,
                        "price_limit_tradability": True,
                    },
                ),
            )
        )

        self.assertEqual(outcome.status, PreflightStatus.BLOCKED)
        self.assertEqual(provider.preflight_calls, 0)
        self.assertEqual(
            outcome.report.primary_issue_code,
            "internal_preflight_fixture_missing",
        )
        self.assertEqual(outcome.report.fixture_sources, ())

    def test_formal_profile_is_blocked_before_provider_read(self) -> None:
        provider = FakeProvider(self.base_report)
        outcome = DataPreflightService(provider, profile="formal@1").preflight(intent())
        self.assertEqual(outcome.status, PreflightStatus.BLOCKED)
        self.assertEqual(provider.preflight_calls, 0)
        self.assertEqual(outcome.report.primary_issue_code, "internal_preflight_profile_mismatch")

    def test_dynamic_scope_without_task15_resolution_is_blocked_before_provider_read(self) -> None:
        provider = FakeProvider(self.base_report)
        outcome = DataPreflightService(provider).preflight(intent(dynamic=True))
        self.assertEqual(outcome.status, PreflightStatus.BLOCKED)
        self.assertEqual(provider.preflight_calls, 0)
        self.assertIn(
            "universe_scope_unresolved",
            {issue.code for issue in outcome.report.issues},
        )

    def test_scope_issue_uses_chinese_summary_and_retains_upstream_message(self) -> None:
        issue = _scope_issue(
            {
                "code": "universe_provider_contract_violation",
                "message": "Provider rejected candidate universe payload",
                "field": "universe_query_policy",
                "details": {"provider_key": "fixture", "error_type": "ValueError"},
            }
        )
        self.assertEqual(issue.message, "动态候选范围预检未通过，已阻断回测。")
        self.assertEqual(
            issue.details,
            {
                "provider_key": "fixture",
                "error_type": "ValueError",
                "upstream_message": "Provider rejected candidate universe payload",
            },
        )

    def test_degraded_internal_report_is_converted_to_blocked(self) -> None:
        warning = PreflightIssue(
            code="fixture_warning",
            severity=IssueSeverity.WARNING,
            scope="fixture",
            message="内部 fixture 仅用于链路验收。",
        )
        # A degraded report must retain a resolved compatible axis; the
        # evidence-only blocked fixture used by other tests intentionally has
        # no calendar and therefore cannot be relabeled degraded.
        from app.backtesting.data.sessions import AuthoritativeDataSession
        from tests.test_backtesting_data_session import (
            COMMON_OPEN,
            J5,
            J7,
            build_provider,
            make_request,
        )

        ready_base = AuthoritativeDataSession(
            request=make_request(J5, J7, calendar_ids=("SSE", "SZSE")),
            calendar_provider=build_provider(COMMON_OPEN, COMMON_OPEN),
        ).preflight()
        degraded = replace(
            ready_base,
            status=PreflightStatus.DEGRADED,
            issues=(warning,),
            report_hash="",
        )
        outcome = DataPreflightService(FakeProvider(degraded)).preflight(intent())
        self.assertEqual(outcome.status, PreflightStatus.BLOCKED)
        self.assertIn(
            "internal_preflight_degraded_forbidden",
            {issue.code for issue in outcome.report.issues},
        )

    def test_session_hash_change_blocks_before_strategy_loader(self) -> None:
        page_provider = FakeProvider(self.base_report)
        service = DataPreflightService(page_provider)
        page = service.admission(intent())
        # A distinct but valid blocked base report emulates facts changing
        # between page admission and the worker's authoritative re-check.
        from app.backtesting.data.preflight_service import _minimal_blocked_report, _issue

        changed_report = _minimal_blocked_report(
            intent(),
            (_issue("coverage_incomplete", "会话覆盖已变化。"),),
        )
        session = FakeProvider(changed_report)
        decision = service.validate_session(
            PreflightContext(request=intent(), provider=session),
            admission=page,
        )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.hash_match)
        self.assertEqual(decision.failure_phase, "data_preflight")
        loaded = []
        self.assertEqual(
            service.before_strategy(
                PreflightContext(request=intent(), provider=session),
                lambda: loaded.append("called"),
                admission=page,
            )[1],
            None,
        )
        self.assertEqual(loaded, [])


class PreflightPersistenceTestCase(unittest.TestCase):
    def test_existing_result_table_persists_and_hides_internal_artifact(self) -> None:
        from app.backtesting.data.preflight_service import _minimal_blocked_report, _issue

        request = intent()
        report = _minimal_blocked_report(
            request,
            (_issue("coverage_incomplete", "内部报告测试已阻断。"),),
        )
        from app.backtesting.data.preflight_service import PreflightOutcome, INTERNAL_LINK_ACCEPTANCE_PROFILE

        outcome = PreflightOutcome(
            report=report,
            profile=__import__(
                "app.backtesting.data.requests",
                fromlist=["PreflightProfileRegistry"],
            ).PreflightProfileRegistry().resolve(INTERNAL_LINK_ACCEPTANCE_PROFILE),
            fixed_instrument_ids=(IID,),
            fixtures=(),
        )
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(
            engine,
            tables=[Base.metadata.tables[name] for name in RESULT_TABLE_NAMES],
        )
        session = Session(engine)
        try:
            repository = BacktestResultRepository(session, cursor_signing_key="test-key")
            run_id = uuid4()
            dto = outcome.to_result_record(run_id, "admission")
            self.assertEqual(dto.admission_report_hash, outcome.report_hash)
            repository.append("data_preflight", dto)
            with self.assertRaises(InternalResultNotVisibleError):
                repository.read_page("data_preflight", run_id=run_id)
            for query_context in (
                {"visibility": "internal"},
                {"run_kind": "internal_link_acceptance"},
                {"include_internal": True},
            ):
                with self.subTest(query_context=query_context):
                    with self.assertRaises(InternalResultNotVisibleError):
                        repository.read_page(
                            "data_preflight",
                            run_id=run_id,
                            query_context=query_context,
                        )
            page = repository.read_page(
                "data_preflight", run_id=run_id, include_internal=True
            )
            self.assertEqual(len(page.items), 1)
            item = BacktestDataPreflightItem.model_validate(page.items[0])
            self.assertEqual(item.run_kind, "internal_link_acceptance")
            self.assertEqual(item.preflight_profile, "internal_link_acceptance@1")
            self.assertIn("内部链路验收", item.title)
        finally:
            session.close()
            engine.dispose()

    def test_formal_result_views_reject_internal_run_id(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(
            engine,
            tables=[Base.metadata.tables[name] for name in RESULT_TABLE_NAMES],
        )
        session = Session(engine)
        run_id = uuid4()
        now = datetime(2026, 8, 30, 9, tzinfo=timezone.utc)
        repository = BacktestResultRepository(session, cursor_signing_key="test-key")
        try:
            repository.append(
                "data_preflight",
                BacktestDataPreflightRecord(
                    run_id=run_id,
                    phase="admission",
                    status="blocked",
                    report_hash="internal-report",
                    run_kind="internal_link_acceptance",
                    preflight_profile_key="internal_link_acceptance",
                    preflight_profile_version=1,
                ),
            )
            repository.append(
                "equity_curve",
                BacktestEquityCurveRecord(
                    run_id=run_id,
                    sequence=0,
                    as_of=now,
                    valuation_status=ValuationStatus.COMPLETE,
                    cash=Decimal("100"),
                    market_value=Decimal("0"),
                    equity=Decimal("100"),
                    period_return=Decimal("0"),
                    total_pnl=Decimal("0"),
                    cumulative_return=Decimal("0"),
                    drawdown=Decimal("0"),
                ),
            )
            repository.append(
                "metrics",
                BacktestMetricRecord(
                    run_id=run_id,
                    metric_key="internal_metric",
                    formula_version="v1",
                    value=Decimal("1"),
                ),
            )
            repository.upsert_analysis_summary(
                BacktestAnalysisSummaryRecord(
                    run_id=run_id,
                    status="partial",
                    formula_signature="sha256:" + "1" * 64,
                    input_evidence_signature="sha256:" + "2" * 64,
                    last_chunk_sequence=0,
                    last_chunk_token="sha256:" + "3" * 64,
                    completed_through_session=date(2026, 8, 30),
                )
            )

            list_calls = (
                (list_data_preflight, {}),
                (list_equity_curve, {"start_time": None, "end_time": None}),
                (list_metrics, {}),
            )
            for endpoint, extra in list_calls:
                with self.subTest(endpoint=endpoint.__name__):
                    with self.assertRaises(HTTPException) as caught:
                        endpoint(
                            run_id=run_id,
                            limit=10,
                            cursor=None,
                            session=session,
                            signing_key="test-key",
                            **extra,
                        )
                    self.assertEqual(caught.exception.status_code, 422)

            with self.assertRaises(HTTPException) as caught:
                get_analysis_summary(run_id=run_id, session=session)
            self.assertEqual(caught.exception.status_code, 404)

            # No comparison endpoint exists in the current result router;
            # therefore there is no additional comparison read boundary to
            # exercise for this repository contract.
            from app.backtesting import result_router

            route_paths = {route.path for route in result_router.router.routes}
            self.assertFalse(
                any("compare" in path or "comparison" in path for path in route_paths)
            )
        finally:
            session.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
