"""Task-11 capability-gate boundary checks.

The tests exercise the small gate independently of calendar storage so each
case isolates the value/applicability contract from snapshot construction.
"""

from dataclasses import replace
from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from uuid import uuid4
import unittest

from app.backtesting.calendar_axis import (
    CAPABILITY_OPENING_AVAILABILITY,
    CAPABILITY_PRICE_LIMIT_TRADABILITY,
    CAPABILITY_SUSPENSION,
    CalendarCapabilityDeclaration,
    CalendarDefinition,
    CalendarRegistry,
    CalendarSessionFact,
    CalendarSourcePriority,
    CapabilityApplicability,
    CapabilityValue,
    InMemoryCalendarAxisDataProvider,
)
from app.backtesting.data.errors import CalendarContractError, CalendarJsonInvalidError
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
from app.backtesting.data.sessions import evaluate_calendar_capability_gate
from app.backtesting.data.sessions import AuthoritativeDataSession

from tests.test_backtesting_data_session import make_request as make_frozen_request


DAY = date(2026, 1, 2)


def make_request(*, status_required: bool) -> DataPreflightRequest:
    capabilities = ((DataCapability.BARS, DataCapability.STATUS)
                    if status_required else (DataCapability.BARS,))
    return DataPreflightRequest(
        provider_key="memory",
        requested_window=DateRange(start_date=DAY, end_date=DAY),
        frequency="1d",
        rule_package=ContractRef(key="rules.cn.etf", version=1),
        market_scope=MarketScope(),
        universe_query_policy=UniverseQueryPolicy(),
        instrument_scope_mode=InstrumentScopeMode.FIXED,
        required_capabilities=capabilities,
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        query_boundary=QueryBoundary(
            data_cutoff=datetime(2026, 1, 3, tzinfo=timezone.utc)
        ),
        static_instrument_ids=(uuid4(),),
    )


def make_snapshot():
    # The gate only consumes these two immutable snapshot fields.
    return SimpleNamespace(calendar_ids=("SSE",), pit_context=None)


def declaration(capability: str, *, value: str, applicability: str | None):
    return CalendarCapabilityDeclaration(
        scope_kind="provider",
        scope_key="provider:memory",
        provider_key="memory",
        capability=capability,
        value=value,
        applicability=applicability,
    )


def strict_provider(*, capabilities=(), source_priorities=()):
    """Build one minimal canonical provider for the atomic snapshot path."""

    source_priority_rows = tuple(source_priorities)
    if source_priority_rows:
        source_priority = source_priority_rows[0]
        known_at = source_priority.known_at
    else:
        known_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        source_priority = CalendarSourcePriority(
            source="test",
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
        source_priority_rows = (source_priority,)
    common = {
        "source": "test",
        "source_revision": "r1",
        "known_at": known_at,
        "observed_at": known_at,
        "source_priority_fact_id": source_priority.fact_id,
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
        valid_from=date(2020, 1, 1),
        **common,
    )
    definition = CalendarDefinition(
        "SSE",
        "v1",
        "Asia/Shanghai",
        ((time(9, 30), time(15, 0)),),
        valid_from=date(2020, 1, 1),
        registry_fact_id=registry.fact_id,
        registry_version=registry.registry_version,
        **common,
    )
    fact = CalendarSessionFact(
        "SSE",
        DAY,
        True,
        "v1",
        registry_fact_id=registry.fact_id,
        registry_version=registry.registry_version,
        definition_fact_id=definition.fact_id,
        **common,
    )
    return InMemoryCalendarAxisDataProvider(
        definitions=(definition,),
        facts=(fact,),
        registries=(registry,),
        source_priorities=source_priority_rows,
        capabilities=capabilities,
    )


class CalendarCapabilityGateTest(unittest.TestCase):
    def test_scope_columns_are_single_scope_only(self):
        cases = (
            {
                "scope_kind": "provider",
                "scope_key": "provider:memory",
                "provider_key": "memory",
                "package_version": 1,
            },
            {
                "scope_kind": "rule_package",
                "scope_key": "rule_package:rules.cn.etf@1",
                "package_key": "rules.cn.etf",
                "package_version": 1,
                "provider_key": "memory",
            },
            {
                "scope_kind": "calendar",
                "scope_key": "calendar:SSE",
                "calendar_id": "SSE",
                "registry_fact_id": uuid4(),
                "registry_version": 1,
                "package_version": 1,
            },
            {
                "scope_kind": "instrument",
                "scope_key": f"instrument:{(instrument_id := uuid4())}",
                "instrument_id": instrument_id,
                "package_version": 1,
            },
        )
        for fields in cases:
            with self.subTest(scope_kind=fields["scope_kind"]):
                with self.assertRaises(CalendarJsonInvalidError):
                    CalendarCapabilityDeclaration(
                        capability=CAPABILITY_SUSPENSION,
                        **fields,
                    )

    def test_static_and_mandatory_ids_are_each_resolved_and_audited(self):
        static_id, mandatory_id = uuid4(), uuid4()
        request = make_request(status_required=True)
        request = request.__class__(
            **{
                **{
                    field.name: getattr(request, field.name)
                    for field in request.__dataclass_fields__.values()
                },
                "static_instrument_ids": (static_id,),
                "mandatory_instrument_ids": (mandatory_id,),
            }
        )
        declarations = tuple(
            CalendarCapabilityDeclaration(
                scope_kind="instrument",
                scope_key=f"instrument:{instrument_id}",
                capability=capability,
                value=CapabilityValue.SUPPORTED,
                applicability=CapabilityApplicability.REQUIRED,
                instrument_id=instrument_id,
            )
            for instrument_id in (static_id, mandatory_id)
            for capability in (
                CAPABILITY_SUSPENSION,
                CAPABILITY_OPENING_AVAILABILITY,
                CAPABILITY_PRICE_LIMIT_TRADABILITY,
            )
        )
        issues, evidence = evaluate_calendar_capability_gate(
            InMemoryCalendarAxisDataProvider(capabilities=declarations),
            request,
            make_snapshot(),
        )

        self.assertEqual(issues, ())
        self.assertEqual(len(evidence), 6)
        self.assertEqual(
            {item["instrument_id"] for item in evidence},
            {str(static_id), str(mandatory_id)},
        )
        self.assertTrue(all(item["scope_kind"] == "instrument" for item in evidence))

    def test_missing_declarations_are_unknown_and_block_when_status_required(self):
        provider = InMemoryCalendarAxisDataProvider()
        issues, evidence = evaluate_calendar_capability_gate(
            provider, make_request(status_required=True), make_snapshot()
        )

        self.assertEqual(len(issues), 3)
        self.assertTrue(all(issue.code == "CAPABILITY_DECLARATION_INVALID" for issue in issues))
        self.assertEqual({item["value"] for item in evidence}, {CapabilityValue.UNKNOWN})
        self.assertTrue(all(item["missing"] for item in evidence))

    def test_not_applicable_is_independent_from_provider_value(self):
        declarations = tuple(
            declaration(
                capability,
                value=CapabilityValue.UNKNOWN,
                applicability=CapabilityApplicability.NOT_APPLICABLE,
            )
            for capability in (
                CAPABILITY_SUSPENSION,
                CAPABILITY_OPENING_AVAILABILITY,
                CAPABILITY_PRICE_LIMIT_TRADABILITY,
            )
        )
        provider = InMemoryCalendarAxisDataProvider(capabilities=declarations)
        issues, evidence = evaluate_calendar_capability_gate(
            provider, make_request(status_required=True), make_snapshot()
        )

        self.assertEqual(issues, ())
        self.assertEqual(
            {item["applicability"] for item in evidence},
            {CapabilityApplicability.NOT_APPLICABLE},
        )

    def test_required_unknown_or_unsupported_is_blocked(self):
        declarations = tuple(
            declaration(
                capability,
                value=(
                    CapabilityValue.SUPPORTED
                    if capability == CAPABILITY_SUSPENSION
                    else CapabilityValue.UNKNOWN
                ),
                applicability=CapabilityApplicability.REQUIRED,
            )
            for capability in (
                CAPABILITY_SUSPENSION,
                CAPABILITY_OPENING_AVAILABILITY,
                CAPABILITY_PRICE_LIMIT_TRADABILITY,
            )
        )
        provider = InMemoryCalendarAxisDataProvider(capabilities=declarations)
        issues, _ = evaluate_calendar_capability_gate(
            provider, make_request(status_required=True), make_snapshot()
        )

        self.assertEqual(len(issues), 2)
        self.assertTrue(all(issue.code == "UNSUPPORTED_CAPABILITY" for issue in issues))
        self.assertEqual(
            {issue.details["cause_code"] for issue in issues},
            {"rule_capability_unsupported"},
        )

    def test_missing_applicability_blocks_only_when_status_is_required(self):
        declarations = tuple(
            declaration(
                capability,
                value=CapabilityValue.SUPPORTED,
                applicability=None,
            )
            for capability in (
                CAPABILITY_SUSPENSION,
                CAPABILITY_OPENING_AVAILABILITY,
                CAPABILITY_PRICE_LIMIT_TRADABILITY,
            )
        )
        provider = InMemoryCalendarAxisDataProvider(capabilities=declarations)

        optional_issues, _ = evaluate_calendar_capability_gate(
            provider, make_request(status_required=False), make_snapshot()
        )
        required_issues, _ = evaluate_calendar_capability_gate(
            provider, make_request(status_required=True), make_snapshot()
        )

        self.assertEqual(optional_issues, ())
        self.assertEqual(len(required_issues), 3)
        self.assertTrue(
            all(issue.code == "CAPABILITY_DECLARATION_INVALID" for issue in required_issues)
        )
        self.assertTrue(all(issue.title == "能力声明无效" for issue in required_issues))

    def test_manifest_without_status_fails_closed_even_with_declarations(self):
        declarations = tuple(
            declaration(
                capability,
                value=CapabilityValue.SUPPORTED,
                applicability=CapabilityApplicability.REQUIRED,
            )
            for capability in (
                CAPABILITY_SUSPENSION,
                CAPABILITY_OPENING_AVAILABILITY,
                CAPABILITY_PRICE_LIMIT_TRADABILITY,
            )
        )

        class Provider(InMemoryCalendarAxisDataProvider):
            def capability_manifest(self):
                return SimpleNamespace(capabilities=(), manifest_version=1)

        provider = Provider(capabilities=declarations)
        issues, _ = evaluate_calendar_capability_gate(
            provider, make_request(status_required=True), make_snapshot()
        )

        self.assertIn("UNSUPPORTED_CAPABILITY", {issue.code for issue in issues})
        self.assertEqual(sum(issue.code == "UNSUPPORTED_CAPABILITY" for issue in issues), 1)

    def test_more_specific_calendar_declaration_wins_over_provider_fallback(self):
        registry_fact_id = uuid4()
        declarations = (
            declaration(
                CAPABILITY_SUSPENSION,
                value=CapabilityValue.UNSUPPORTED,
                applicability=CapabilityApplicability.REQUIRED,
            ),
            CalendarCapabilityDeclaration(
                scope_kind="calendar",
                scope_key="calendar:SSE",
                calendar_id="sse",
                registry_fact_id=registry_fact_id,
                registry_version=1,
                capability=CAPABILITY_SUSPENSION,
                value=CapabilityValue.SUPPORTED,
                applicability=CapabilityApplicability.REQUIRED,
            ),
        ) + tuple(
            declaration(
                capability,
                value=CapabilityValue.SUPPORTED,
                applicability=CapabilityApplicability.NOT_APPLICABLE,
            )
            for capability in (
                CAPABILITY_OPENING_AVAILABILITY,
                CAPABILITY_PRICE_LIMIT_TRADABILITY,
            )
        )
        provider = InMemoryCalendarAxisDataProvider(capabilities=declarations)
        issues, evidence = evaluate_calendar_capability_gate(
            provider, make_request(status_required=True), make_snapshot()
        )

        self.assertEqual(issues, ())
        suspension = next(item for item in evidence if item["capability"] == CAPABILITY_SUSPENSION)
        self.assertEqual(suspension["value"], CapabilityValue.SUPPORTED)
        self.assertEqual(suspension["specificity"], 3)

    def test_resolver_failure_is_a_blocked_contract_issue(self):
        class BrokenProvider:
            def capability_manifest(self):
                return SimpleNamespace(
                    capabilities=(DataCapability.STATUS,), manifest_version=1
                )

            def resolve_capability(self, *_args, **_kwargs):
                raise RuntimeError("resolver unavailable")

        issues, evidence = evaluate_calendar_capability_gate(
            BrokenProvider(), make_request(status_required=True), make_snapshot()
        )

        self.assertEqual(len(issues), 3)
        self.assertTrue(
            all(issue.code == "PROVIDER_CONTRACT_VIOLATION" for issue in issues)
        )
        self.assertTrue(all(item["missing"] for item in evidence))

    def test_manifest_failure_is_a_blocked_contract_issue(self):
        class BrokenManifestProvider:
            def capability_manifest(self):
                raise RuntimeError("manifest unavailable")

            def resolve_capability(self, *_args, **_kwargs):
                return None

        issues, _ = evaluate_calendar_capability_gate(
            BrokenManifestProvider(), make_request(status_required=True), make_snapshot()
        )

        self.assertEqual(issues[0].code, "PROVIDER_CONTRACT_VIOLATION")
        self.assertEqual(issues[0].details["error_type"], "RuntimeError")

    def test_authoritative_session_snapshot_includes_mandatory_ids(self):
        static_id, mandatory_id = uuid4(), uuid4()
        request = make_frozen_request(DAY, DAY, calendar_ids=("SSE",), instrument_id=static_id)
        request = request.__class__(
            **{
                **{
                    field.name: getattr(request, field.name)
                    for field in request.__dataclass_fields__.values()
                },
                "mandatory_instrument_ids": (mandatory_id,),
            }
        )

        class CaptureProvider:
            def __init__(self):
                self.snapshot_request = None

            def open_calendar_snapshot(self, snapshot_request):
                self.snapshot_request = snapshot_request
                raise CalendarContractError("snapshot unavailable")

        provider = CaptureProvider()
        report = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
        ).preflight()

        self.assertEqual(report.status.value, "blocked")
        self.assertEqual(
            provider.snapshot_request.instrument_ids,
            tuple(sorted((static_id, mandatory_id), key=str)),
        )

    def test_strict_snapshot_runs_capability_gate_before_ready(self):
        provider = strict_provider()
        priority = provider.source_priorities()[0]
        capabilities = tuple(
            CalendarCapabilityDeclaration(
                scope_kind="provider",
                scope_key="provider:memory",
                provider_key="memory",
                capability=capability,
                value=CapabilityValue.SUPPORTED,
                applicability=CapabilityApplicability.REQUIRED,
                source="test",
                source_revision="r1",
                known_at=priority.known_at,
                observed_at=priority.observed_at,
                source_priority_fact_id=priority.fact_id,
                source_priority_version=priority.source_priority_version,
                source_priority=priority.source_priority,
                source_revision_order=priority.source_revision_order,
                bootstrap_seed_id=priority.bootstrap_seed_id,
                bootstrap_seed_version=priority.bootstrap_seed_version,
                bootstrap_seed_hash=priority.bootstrap_seed_hash,
            )
            for capability in (
                CAPABILITY_SUSPENSION,
                CAPABILITY_OPENING_AVAILABILITY,
                CAPABILITY_PRICE_LIMIT_TRADABILITY,
            )
        )
        provider = strict_provider(
            capabilities=capabilities,
            source_priorities=(priority,),
        )
        request = make_frozen_request(DAY, DAY, calendar_ids=("SSE",), instrument_id=uuid4())
        request = replace(
            request,
            required_capabilities=(DataCapability.BARS, DataCapability.STATUS),
        )

        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
        )
        report = session.preflight()

        self.assertEqual(report.status.value, "ready")
        self.assertIsNotNone(session.snapshot)
        self.assertEqual(
            {item["value"] for item in report.calendar_summary["capabilities"]},
            {"supported"},
        )
        self.assertEqual(
            {item["applicability"] for item in report.calendar_summary["capabilities"]},
            {"required"},
        )

    def test_strict_snapshot_missing_capability_blocks_after_single_read(self):
        request = make_frozen_request(DAY, DAY, calendar_ids=("SSE",), instrument_id=uuid4())
        request = replace(
            request,
            required_capabilities=(DataCapability.BARS, DataCapability.STATUS),
        )
        provider = strict_provider()
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
        )

        report = session.preflight()

        self.assertEqual(report.status.value, "blocked")
        self.assertEqual(
            {issue.code for issue in report.issues},
            {"CAPABILITY_DECLARATION_INVALID"},
        )
        self.assertIsNotNone(session.snapshot)
        self.assertEqual(session.snapshot.prepare_calls, 1)
        self.assertEqual(session.snapshot.batch_read_calls, 1)

    def test_authoritative_session_blocks_legacy_provider_without_snapshot(self):
        request = make_frozen_request(DAY, DAY, calendar_ids=("SSE",), instrument_id=uuid4())

        class LegacyProvider:
            def definitions(self, calendar_id):
                raise AssertionError("legacy definitions() must not be read")

            def fact(self, calendar_id, day):
                raise AssertionError("legacy fact() must not be read")

        report = AuthoritativeDataSession(
            request=request,
            calendar_provider=LegacyProvider(),
        ).preflight()

        self.assertEqual(report.status.value, "blocked")
        issue = next(issue for issue in report.issues if issue.code == "unsupported_capability")
        self.assertEqual(issue.details["cause_code"], "calendar_provider_legacy")

    def test_authoritative_session_keeps_legacy_in_memory_fixture_compatibility(self):
        provider = InMemoryCalendarAxisDataProvider(
            definitions=(
                CalendarDefinition("SSE", "v1", "Asia/Shanghai", ((time(9), time(10)),)),
            ),
            facts=(
                CalendarSessionFact("SSE", DAY, True, "v1"),
            ),
        )
        report = AuthoritativeDataSession(
            request=make_frozen_request(DAY, DAY, calendar_ids=("SSE",), instrument_id=uuid4()),
            calendar_provider=provider,
        ).preflight()

        self.assertEqual(report.status.value, "ready")


if __name__ == "__main__":
    unittest.main()
