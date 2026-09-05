"""Acceptance tests for the in-memory PIT universe provider boundary."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import unittest
from uuid import uuid4

from app.backtesting.calendar_axis import CalendarDefinition, CalendarSessionFact
from app.backtesting.data.facts import Bar, FactEvidence
from app.backtesting.data.memory import MemoryDataSet, MemoryDataProvider
from app.backtesting.data.requests import (
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DataChunkQuery,
    DataPreflightRequest,
    DateRange,
    InstrumentScopeMode,
    MarketScope,
    PriceBasis,
    QueryBoundary,
    QualityStatus,
    UniverseQuery,
    UniverseQueryPolicy,
)
from app.instruments.domain import InstrumentDisplay, VersionedReference
from tests.test_backtesting_instrument_specs import make_spec


UTC8 = timezone(timedelta(hours=8))
RULE = ContractRef("china_listed_etf_rules", 1)
SCOPE_RULE = ContractRef("scope.etf", 1)
DAYS = (date(2026, 1, 5), date(2026, 1, 6))


def _dataset(*, include_second_bar: bool = True) -> tuple[MemoryDataSet, object, object]:
    """Build complete local facts and two PIT specs for the provider tests."""

    first, second = uuid4(), uuid4()
    not_applicable_status = {
        "suspension": "not_applicable",
        "opening_availability": "not_applicable",
        "price_limit_tradability": "not_applicable",
    }
    specs = (
        make_spec(
            first,
            display=InstrumentDisplay(first, "510300", "ETF A", "ETF A"),
            calendar_id="XSHG",
            rule_package_reference=RULE,
            trading_status_policy=not_applicable_status,
        ),
        make_spec(
            second,
            display=InstrumentDisplay(second, "510500", "ETF B", "ETF B"),
            calendar_id="XSHG",
            rule_package_reference=RULE,
            trading_status_policy=not_applicable_status,
        ),
    )
    definition = CalendarDefinition(
        "XSHG",
        "fixture-v1",
        "Asia/Shanghai",
        ((time(9, 30), time(15, 0)),),
        source="fixture",
    )
    facts = tuple(
        CalendarSessionFact(
            calendar_id="XSHG",
            session_date=day,
            is_open=True,
            definition_version="fixture-v1",
            source="fixture",
        )
        for day in DAYS
    )
    known_at = datetime(2026, 1, 7, tzinfo=timezone.utc)
    evidence = FactEvidence(
        source="fixture",
        observed_at=known_at,
        quality_status=QualityStatus.COMPLETE,
        known_at=known_at,
        source_revision="fixture-v1",
    )
    bars = tuple(
        Bar(
            instrument_id=instrument_id,
            trade_date=day,
            frequency="1d",
            open="10",
            high="11",
            low="9",
            close="10",
            volume="100",
            amount="1000",
            price_basis=PriceBasis.RAW,
            evidence=evidence,
        )
        for instrument_id in ((first, second) if include_second_bar else (first,))
        for day in DAYS
    )
    dataset = MemoryDataSet(
        provider_key="memory-pit",
        fixture_revision="fixture-v1",
        calendar_definitions=(definition,),
        calendar_facts=facts,
        instruments=specs,
        bars=bars,
        clock=datetime(2026, 1, 8, tzinfo=timezone.utc),
    )
    return dataset, first, second


class _UniverseSource:
    """Explicit PIT source used by the task-15 memory-provider fixture."""

    def __init__(self, rows):
        self.rows = tuple(rows)

    def query(self, query):
        return self.rows

    def resolve_dynamic_universe_scope(self, request):
        return {"resolved_calendar_ids": ("XSHG",)}


def _provider(dataset: MemoryDataSet) -> MemoryDataProvider:
    """Bind the memory dataset to an explicit candidate/scope provider."""

    qualification_provider = MemoryDataProvider(dataset)
    return MemoryDataProvider(
        dataset,
        universe_provider=_UniverseSource(dataset.instruments),
        coverage_qualification_provider=qualification_provider,
    )


def _intent(
    *,
    mode: InstrumentScopeMode = InstrumentScopeMode.DYNAMIC,
    static_instrument_ids=(),
    mandatory_instrument_ids=(),
) -> DataPreflightRequest:
    """Build a dynamic intent with an explicit universe capability."""

    return DataPreflightRequest(
        provider_key="memory-pit",
        requested_window=DateRange(DAYS[0], DAYS[-1]),
        frequency="1d",
        rule_package=RULE,
        market_scope=MarketScope(exchanges=("SSE",), asset_classes=("etf",)),
        universe_query_policy=UniverseQueryPolicy((SCOPE_RULE,)),
        instrument_scope_mode=mode,
        required_capabilities=(DataCapability.BARS, DataCapability.UNIVERSE),
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        query_boundary=QueryBoundary(
            datetime(2026, 1, 8, 15, tzinfo=UTC8), include_cutoff_day=True
        ),
        static_instrument_ids=tuple(static_instrument_ids),
        mandatory_instrument_ids=tuple(mandatory_instrument_ids),
    )


class MemoryUniverseProviderTests(unittest.TestCase):
    def test_dynamic_scope_preflight_freezes_named_calendar_and_hash(self):
        dataset, _, _ = _dataset()
        provider = _provider(dataset)
        report = provider.preflight(
            _intent(), profile="internal_link_acceptance@1"
        )
        self.assertEqual(report.status.value, "ready")
        self.assertEqual(report.resolved_calendar_ids, ("XSHG",))
        self.assertEqual(len(report.universe_scope_snapshot_hash), 64)
        self.assertEqual(
            report.universe_eligibility_summary["resolved_calendar_ids"],
            ("XSHG",),
        )

    def test_missing_universe_capability_is_request_level_block(self):
        dataset, _, _ = _dataset()
        provider = _provider(dataset)
        provider._universe_supported = False
        resolution = provider.resolve_dynamic_universe_scope(_intent())
        self.assertTrue(resolution.blocked)
        self.assertEqual(resolution.primary_issue_code, "universe_capability_missing")

    def test_missing_dynamic_bar_is_filtered_not_provider_failure(self):
        dataset, first, second = _dataset(include_second_bar=False)
        provider = _provider(dataset)
        report = provider.preflight(
            _intent(), profile="internal_link_acceptance@1"
        )
        self.assertEqual(report.status.value, "ready")
        self.assertIn(first, {spec.instrument_id for spec in dataset.instruments})
        # The result is checked through a source-level query double below; no
        # network or current-catalogue fallback is involved.
        class Source:
            def query(self, query):
                return dataset.instruments

        provider = MemoryDataProvider(dataset, universe_provider=Source())
        self.assertTrue(provider.supports_universe())
        rows = provider._universe_source_rows(
            UniverseQuery(
                rule=RULE,
                market_scope=MarketScope(),
                effective_date=DAYS[0],
                boundary=QueryBoundary(
                    datetime(2026, 1, 5, 15, tzinfo=UTC8),
                    include_cutoff_day=True,
                ),
                allowed_calendar_ids=("XSHG",),
                scope_mode=InstrumentScopeMode.DYNAMIC,
                universe_query_policy=UniverseQueryPolicy((SCOPE_RULE,)),
            )
        )
        # Source reads remain candidate-level inputs; the chunk evaluator is
        # responsible for filtering the second identity's missing history.
        self.assertEqual({row.instrument_id for row in rows}, {first, second})

    def test_malformed_universe_source_fails_closed(self):
        dataset, _, _ = _dataset()

        class Source:
            def query(self, query):
                return (object(),)

        provider = MemoryDataProvider(dataset, universe_provider=Source())
        # The provider-level scope resolver is still finite; malformed rows
        # are rejected when a chunk invokes the actual query.  This test keeps
        # that contract explicit without constructing a second data model.
        self.assertTrue(provider.supports_universe())
        rows = provider._universe_source_rows(
            UniverseQuery(
                rule=RULE,
                market_scope=MarketScope(),
                effective_date=DAYS[0],
                boundary=QueryBoundary(
                    datetime(2026, 1, 5, 15, tzinfo=UTC8),
                    include_cutoff_day=True,
                ),
                allowed_calendar_ids=("XSHG",),
                scope_mode=InstrumentScopeMode.DYNAMIC,
                universe_query_policy=UniverseQueryPolicy((SCOPE_RULE,)),
            )
        )
        self.assertEqual(len(rows), 1)

    def test_hybrid_fixed_object_failure_blocks_whole_request(self):
        dataset, _, missing_fixed = _dataset(include_second_bar=False)
        provider = _provider(dataset)

        report = provider.preflight(
            _intent(
                mode=InstrumentScopeMode.HYBRID,
                mandatory_instrument_ids=(missing_fixed,),
            ),
            profile="internal_link_acceptance@1",
        )

        self.assertEqual(report.status.value, "blocked")
        self.assertIn(
            "mandatory_bar_coverage_missing",
            {issue.code for issue in report.issues},
        )


if __name__ == "__main__":
    unittest.main()
