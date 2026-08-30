"""Contract tests for task-16A qualification profiles and fixtures."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from app.backtesting.data.protocols import InstrumentCoverageQualification
from app.backtesting.data.requests import (
    CoverageQualificationRequest,
    DataCapability,
    DateRange,
    FORMAL_PROFILE,
    InternalFixture,
    INTERNAL_LINK_ACCEPTANCE_PROFILE,
    MarketScope,
    QueryBoundary,
    get_preflight_profile,
    registered_preflight_profiles,
)
from app.backtesting.data.reports import DataCoverageReport
from app.backtesting.data.requests import QualityStatus


def _memory_spec(instrument_id, calendar_id="XSHG"):
    """Build a complete immutable spec without adapter-side defaults."""

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

    return InstrumentSpec(
        instrument_id=instrument_id,
        display=InstrumentDisplay(instrument_id, "TEST", "测试", "测试 ETF"),
        asset_class="etf",
        exchange="SH",
        currency="CNY",
        calendar_id=calendar_id,
        price_precision=3,
        quantity_precision=0,
        price_tick="0.001",
        lot_size="100",
        minimum_order_quantity="100",
        contract_multiplier="1",
        trading_session_template=VersionedReference("test_session", 1),
        trading_hours={"timezone": "Asia/Shanghai", "sessions": []},
        settlement_rule_class="t1_before_open_match",
        sellable_rule=StrategyRuleDeclaration(("sell_limited_by_position",)),
        fee_categories=frozenset({"commission"}),
        trading_status_policy={
            "suspension": TradingStatusRequirement.NOT_APPLICABLE,
            "opening_availability": TradingStatusRequirement.NOT_APPLICABLE,
            "price_limit_tradability": TradingStatusRequirement.NOT_APPLICABLE,
        },
        order_types=frozenset({"market"}),
        price_limit_rule=VersionedReference("price_limit", 1),
        cash_availability_rule=VersionedReference("cash", 1),
        position_availability_rule=VersionedReference("position", 1),
        capabilities=InstrumentCapabilities(
            position_sides=frozenset({"long"}),
            order_types=frozenset({"market"}),
            margin_supported=False,
            corporate_action_requirement=CorporateActionRequirement.NOT_APPLICABLE,
        ),
        rule_package_reference=VersionedReference("rules", 1),
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_to=None,
    )


def _memory_dataset(*, bars, instrument_id, calendar_id="XSHG", fixtures=()):
    from app.backtesting.calendar_axis import CalendarDefinition, CalendarSessionFact
    from app.backtesting.data.memory import MemoryDataSet

    day = date(2026, 1, 5)
    definition = CalendarDefinition(
        calendar_id=calendar_id,
        definition_version="test@1",
        timezone="Asia/Shanghai",
        default_sessions=((datetime.min.time().replace(hour=9, minute=30), datetime.min.time().replace(hour=15)),),
        source="fixture",
    )
    fact = CalendarSessionFact(
        calendar_id=calendar_id,
        session_date=day,
        is_open=True,
        definition_version="test@1",
        source="fixture",
    )
    return MemoryDataSet(
        provider_key="qualification-memory",
        fixture_revision="fixture-v1",
        calendar_definitions=(definition,),
        calendar_facts=(fact,),
        instruments=(_memory_spec(instrument_id, calendar_id),),
        bars=tuple(bars),
        clock=datetime(2026, 1, 6, tzinfo=UTC),
        fixtures=fixtures,
    )


class ProfileContractTestCase(unittest.TestCase):
    """The server exposes exactly the versioned profile pair."""

    def test_only_exact_profiles_are_registered(self) -> None:
        profiles = registered_preflight_profiles()
        self.assertEqual(
            {(item.key, item.version) for item in profiles},
            {("formal", 1), ("internal_link_acceptance", 1)},
        )
        self.assertEqual(
            get_preflight_profile("internal_link_acceptance@1").run_kind,
            "internal_link_acceptance",
        )
        with self.assertRaises(Exception):
            get_preflight_profile("internal_link_acceptance@latest")

    def test_formal_profile_rejects_fixture_only_fact(self) -> None:
        instrument_id = uuid4()
        fixture = InternalFixture(
            fixture_key="quantity_action_coverage",
            fixture_version=1,
            capability="quantity_action_coverage",
            instrument_ids=(instrument_id,),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            proof_summary="named internal proof",
            content_hash="a" * 64,
        )
        request = self._request(instrument_id, profile=FORMAL_PROFILE, fixtures=(fixture,))
        with self.assertRaises(Exception):
            get_preflight_profile("formal@1").validate_request(request)

    def test_fixture_scope_and_content_are_explicit(self) -> None:
        instrument_id = uuid4()
        fixture = InternalFixture(
            fixture_key="trading_status",
            fixture_version=1,
            capability="trading_status",
            scope={"markets": ["CN"]},
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            proof_summary={"method": "named fixture"},
            content_hash="b" * 64,
        )
        self.assertTrue(fixture.fixture_only)
        self.assertEqual(fixture.source, "internal_fixture")
        self.assertEqual(fixture.scope["start_date"], "2026-01-01")
        request = self._request(
            instrument_id,
            market_scope=MarketScope(markets=("CN",)),
            fixtures=(fixture,),
            required_fixture_capabilities=("trading_status",),
        )
        get_preflight_profile("internal_link_acceptance@1").validate_request(request)

    def test_internal_profile_has_no_implicit_status_fixture_requirement(self) -> None:
        """A bars-only request is valid without the optional status fixture."""

        request = self._request(uuid4())
        profile = get_preflight_profile("internal_link_acceptance@1")

        profile.validate_request(request)

        self.assertEqual(request.required_capabilities, (DataCapability.BARS,))
        self.assertEqual(request.required_fixture_capabilities, ())
        self.assertEqual(request.fixtures, ())

    def _request(self, instrument_id, *, profile=INTERNAL_LINK_ACCEPTANCE_PROFILE, fixtures=(), market_scope=None, required_fixture_capabilities=()):
        window = DateRange(date(2026, 1, 5), date(2026, 1, 7))
        return CoverageQualificationRequest(
            instrument_id=instrument_id,
            effective_date=date(2026, 1, 5),
            requested_window=window,
            formal_envelope=window,
            warmup_envelope=None,
            history_envelope=window,
            required_capabilities=(DataCapability.BARS,),
            query_boundary=QueryBoundary(
                datetime(2026, 1, 8, tzinfo=UTC), include_cutoff_day=True
            ),
            preflight_profile=profile,
            resolved_calendar_ids=("XSHG",),
            market_scope=market_scope,
            required_fixture_capabilities=required_fixture_capabilities,
            fixtures=fixtures,
        )


class QualificationHashTestCase(unittest.TestCase):
    """Qualification hashes contain business evidence, not run metadata."""

    def test_run_metadata_does_not_change_hash(self) -> None:
        instrument_id = uuid4()
        window = DateRange(date(2026, 1, 5), date(2026, 1, 5))
        report = DataCoverageReport(
            requested_window=window,
            capability=DataCapability.BARS,
            instrument_ids=(instrument_id,),
            expected_count=1,
            complete_count=1,
            partial_count=0,
            invalid_count=0,
            unavailable_count=0,
            quality_status=QualityStatus.COMPLETE,
        )
        first = InstrumentCoverageQualification(
            instrument_id=instrument_id,
            eligible=True,
            coverage_reports=(report,),
            reason_codes=(),
            evidence_summary={"adapter": "memory"},
            run_id=uuid4(),
            generated_at=datetime(2026, 1, 5, tzinfo=UTC),
            message="中文展示文案 A",
        )
        second = replace(
            first,
            run_id=uuid4(),
            generated_at=datetime(2030, 1, 1, tzinfo=UTC),
            message="中文展示文案 B",
        )
        self.assertEqual(first.qualification_hash, second.qualification_hash)

    def test_result_rejects_raw_credentials(self) -> None:
        instrument_id = uuid4()
        window = DateRange(date(2026, 1, 5), date(2026, 1, 5))
        report = DataCoverageReport(
            requested_window=window,
            capability=DataCapability.BARS,
            instrument_ids=(instrument_id,),
            expected_count=1,
            complete_count=1,
            partial_count=0,
            invalid_count=0,
            unavailable_count=0,
            quality_status=QualityStatus.COMPLETE,
        )
        with self.assertRaises(Exception):
            InstrumentCoverageQualification(
                instrument_id=instrument_id,
                eligible=True,
                coverage_reports=(report,),
                reason_codes=(),
                evidence_summary={"access_token": "must-not-be-stored"},
            )


class MemoryQualificationProjectionTestCase(unittest.TestCase):
    """The in-memory provider exposes all four Bar quality outcomes."""

    def _request(self, instrument_id):
        day = date(2026, 1, 5)
        window = DateRange(day, day)
        return CoverageQualificationRequest(
            instrument_id=instrument_id,
            effective_date=day,
            requested_window=window,
            formal_envelope=window,
            warmup_envelope=None,
            history_envelope=None,
            required_capabilities=(DataCapability.BARS,),
            query_boundary=QueryBoundary(
                datetime(2026, 1, 6, tzinfo=UTC), include_cutoff_day=True
            ),
            preflight_profile=INTERNAL_LINK_ACCEPTANCE_PROFILE,
            resolved_calendar_ids=("XSHG",),
        )

    def _bar(self, instrument_id, quality):
        from app.backtesting.data.facts import Bar, FactEvidence

        return Bar(
            instrument_id=instrument_id,
            trade_date=date(2026, 1, 5),
            frequency="1d",
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1.5"),
            volume=Decimal("1"),
            amount=Decimal("1"),
            evidence=FactEvidence(
                source="memory-fixture",
                observed_at=datetime(2026, 1, 6, tzinfo=UTC),
                known_at=datetime(2026, 1, 6, tzinfo=UTC),
                quality_status=quality,
            ),
        )

    def test_complete_bar_is_eligible(self):
        from app.backtesting.data.memory import MemoryDataProvider

        instrument_id = uuid4()
        provider = MemoryDataProvider(
            _memory_dataset(
                bars=(self._bar(instrument_id, QualityStatus.COMPLETE),),
                instrument_id=instrument_id,
            )
        )
        result = provider.qualify(self._request(instrument_id))
        self.assertTrue(result.eligible)
        self.assertEqual(result.coverage_reports[0].complete_count, 1)

    def test_missing_bar_is_unavailable_and_ineligible(self):
        from app.backtesting.data.memory import MemoryDataProvider

        instrument_id = uuid4()
        provider = MemoryDataProvider(
            _memory_dataset(bars=(), instrument_id=instrument_id)
        )
        result = provider.qualify(self._request(instrument_id))
        self.assertFalse(result.eligible)
        self.assertEqual(result.coverage_reports[0].quality_status, QualityStatus.UNAVAILABLE)
        self.assertIn("coverage_unavailable", result.reason_codes)

    def test_invalid_bar_is_preserved_as_invalid(self):
        from app.backtesting.data.memory import MemoryDataProvider

        instrument_id = uuid4()
        provider = MemoryDataProvider(
            _memory_dataset(
                bars=(self._bar(instrument_id, QualityStatus.INVALID),),
                instrument_id=instrument_id,
            )
        )
        result = provider.qualify(self._request(instrument_id))
        self.assertFalse(result.eligible)
        self.assertEqual(result.coverage_reports[0].quality_status, QualityStatus.INVALID)
        self.assertIn("coverage_invalid", result.reason_codes)


if __name__ == "__main__":
    unittest.main()
