"""Tests for the read-only ETF data adapter (task 03-08).

Covers the acceptance matrix of section 5.9 of the task package: raw-row
projection onto generic ``Bar`` facts, PIT identity projection, cross-code
reads, partial coverage blocking under strict mode, unrepaired invalid
prices, non-strict PIT declarations without reliable ``known_at``,
adjustment-policy activation gating, ``effective_date <= data_cutoff``
factor selection, revision-summary sensitivity, report-hash stability,
result-record/API payload round-trips, and the absence of any network
(Tushare) dependency in the backtest path.
"""

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.data.adapters import (
    ADJUSTMENT_SERIES_POLICY,
    EtfFactsAdapter,
    build_data_preflight_payloads,
)
from app.backtesting.data.adjustment_policy import AdjustmentSeriesPolicy
from app.backtesting.data.errors import (
    DataContractError,
    DataCutoffExceededError,
    HistoryBarsIncompleteError,
    IdentityMappingIncompleteError,
    InstrumentCalendarUnresolvedError,
    InvalidDataRequestError,
    ProviderContractViolationError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.facts import AdjustedSeriesPoint, Bar
from app.backtesting.data.requests import (
    CoverageQualificationRequest,
    DataCapability,
    DateRange,
    FORMAL_PROFILE,
    INTERNAL_LINK_ACCEPTANCE_PROFILE,
    InternalFixture,
    PriceBasis,
    QueryBoundary,
    QualityStatus,
)
from app.backtesting.result_models import BacktestDataPreflightRecord, DataPhase
from app.backtesting.result_records import RESULT_TABLE_NAMES, Base
from app.backtesting.result_repository import BacktestResultRepository
from app.backtesting.result_schemas import BacktestDataPreflightItem
from app.instruments.domain import (
    CorporateActionRequirement,
    InstrumentCapabilities,
    InstrumentCodeMapping,
    InstrumentDisplay,
    InstrumentIdentityFact,
    InstrumentSpec,
    MappingConflictError,
    MappingCoverageGapError,
    VersionedReference,
)
from app.instruments.rules.contracts import StrategyRuleDeclaration, TradingStatusRequirement

INSTRUMENT_ID = uuid4()
SOURCE = "tushare"
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)


def days_from(start: date, count: int) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


def make_mapping(source_code, valid_from, valid_to=None, *, known_at=CUTOFF):
    return InstrumentCodeMapping(
        instrument_id=INSTRUMENT_ID,
        source=SOURCE,
        source_code=source_code,
        trading_code="510300",
        valid_from=valid_from,
        valid_to=valid_to,
        mapping_source="exchange_announcement",
        evidence="exchange announcement 2026-001",
        known_at=known_at,
        observed_at=known_at,
    )


class FakeEtfRow(dict):
    """Attribute-access view standing in for one ORM row."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc


def make_bar_row(trade_date, source_code="510300.SH", **overrides):
    values = dict(
        ts_code=source_code,
        trade_date=trade_date,
        open=Decimal("3.710"),
        high=Decimal("3.750"),
        low=Decimal("3.700"),
        close=Decimal("3.740"),
        vol=Decimal("12345"),
        amount=Decimal("46000.50"),
        updated_at=datetime(2026, 8, 21, 2, 0, tzinfo=UTC),
    )
    values.update(overrides)
    return FakeEtfRow(values)


def make_factor_row(point_date, source_code="510300.SH", factor="1.050"):
    return FakeEtfRow(
        ts_code=source_code,
        trade_date=point_date,
        adj_factor=Decimal(factor),
        updated_at=datetime(2026, 8, 20, 2, 0, tzinfo=UTC),
    )


def make_status_fixture() -> InternalFixture:
    """Build a valid named status fixture for the explicit STATUS path."""

    return InternalFixture(
        fixture_key="trading_status",
        fixture_version=1,
        capability="trading_status",
        instrument_ids=(INSTRUMENT_ID,),
        start_date=SESSIONS[0],
        end_date=SESSIONS[-1],
        proof_summary="explicit trading-status fixture",
        content_hash="d" * 64,
    )


def make_verified_adjustment_policy() -> AdjustmentSeriesPolicy:
    """Return the minimal evidence-bound policy used by adjusted-read tests."""

    digest = "a" * 64
    return AdjustmentSeriesPolicy.from_verification_artifact(
        {
            "policy": {"key": "tushare_adj_factor_native", "version": 1},
            "adapter": {"version": "etf_raw_bar_adapter@1"},
            "source": {"name": "tushare"},
            "field_mapping": {
                "adj_factor": "adj_factor",
                "effective_date": "trade_date",
            },
            "semantics": {
                "cutoff_rule": "effective_date <= data_cutoff",
                "qfq_formula": "tushare_qfq_native_v1",
                "hfq_formula": "tushare_hfq_native_v1",
                "qfq_anchor": "latest-visible-close",
                "hfq_anchor": "first-visible-close",
                "precision": 6,
                "rounding": "source-declared-half-up",
            },
            "verification": {
                "summary": "test evidence",
                "status": "verified",
                "published": True,
                "input_hash": digest,
                "output_hash": "b" * 64,
                "evidence_hash": "c" * 64,
            },
        }
    )


class FakeStores:
    """In-memory stand-in for the ingestion tables and mapping store."""

    def __init__(self, *, mappings=(), bar_rows=(), factor_rows=(), open_days=None):
        self.mappings = list(mappings)
        self.bar_rows = list(bar_rows)
        self.factor_rows = list(factor_rows)
        self.open_days = open_days or sorted(
            {row.trade_date for row in self.bar_rows}
        )
        self.read_calls: list[tuple[str, str]] = []

    def code_mappings(self, instrument_id, *, source, start_date, end_date, data_cutoff):
        visible = [
            mapping
            for mapping in self.mappings
            if mapping.instrument_id == instrument_id
            and mapping.source == source
            and mapping.valid_from <= end_date
            and (mapping.valid_to is None or mapping.valid_to > start_date)
            and mapping.known_at <= data_cutoff
        ]
        visible.sort(key=lambda item: item.valid_from)
        if not visible:
            raise MappingCoverageGapError(
                "no mapping rows cover the requested window"
            )
        # Overlap check mirroring the repository's domain validation.
        for earlier, later in zip(visible, visible[1:]):
            if later.valid_from < (earlier.valid_to or date.max):
                raise MappingConflictError("mappings overlap")
        return tuple(visible)

    def daily_bars(self, ts_code, start_date, end_date):
        self.read_calls.append(("bars", ts_code))
        return [
            row
            for row in self.bar_rows
            if row.ts_code == ts_code
            and start_date <= row.trade_date <= end_date
        ]

    def adjustment_factors(self, ts_code, start_date, end_date):
        self.read_calls.append(("factors", ts_code))
        return [
            row
            for row in self.factor_rows
            if row.ts_code == ts_code
            and start_date <= row.trade_date <= end_date
        ]

    def trading_days(self, exchange, start_date, end_date):
        return [day for day in self.open_days if start_date <= day <= end_date]


def make_adapter(stores: FakeStores, **overrides) -> EtfFactsAdapter:
    options = dict(
        code_mappings=stores.code_mappings,
        daily_bars=stores.daily_bars,
        adjustment_factors=stores.adjustment_factors,
        trading_days=stores.trading_days,
    )
    options.update(overrides)
    return EtfFactsAdapter(**options)


BOUNDARY = QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True)
SESSIONS = days_from(date(2026, 8, 17), 5)


class ProjectionTestCase(unittest.TestCase):
    """Matrix items 1, 2, 5, 6."""

    def test_etf_row_projects_to_generic_bar(self) -> None:
        stores = FakeStores(bar_rows=[make_bar_row(date(2026, 8, 17))])
        adapter = make_adapter(stores)
        bar = adapter.project_bar(
            stores.bar_rows[0], INSTRUMENT_ID
        )
        self.assertIsInstance(bar, Bar)
        self.assertEqual(bar.instrument_id, INSTRUMENT_ID)
        self.assertEqual(bar.frequency, "1d")
        self.assertIs(bar.price_basis, PriceBasis.RAW)
        self.assertEqual(bar.open, Decimal("3.710"))
        self.assertEqual(bar.close, Decimal("3.740"))
        self.assertEqual(bar.evidence.source, "tushare")
        self.assertEqual(
            bar.evidence.observed_at, datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
        )
        self.assertIsNone(bar.evidence.known_at)

    def test_etf_code_projects_to_stable_instrument_id(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
        )
        adapter = make_adapter(stores)
        resolution = adapter.resolve(INSTRUMENT_ID, sessions=SESSIONS, data_cutoff=CUTOFF)
        history = adapter.bars(INSTRUMENT_ID, resolution=resolution)
        self.assertEqual(len(history.bars), len(SESSIONS))
        self.assertTrue(all(bar.trade_date == day for bar, day in zip(history.bars, SESSIONS)))

    def test_invalid_ohlc_is_preserved_not_repaired(self) -> None:
        broken_rows = [
            make_bar_row(date(2026, 8, 17), open=Decimal("0")),
            make_bar_row(
                date(2026, 8, 18), open=Decimal("-1.0"), high=Decimal("3.7")
            ),
            make_bar_row(date(2026, 8, 19), low=Decimal("4.0"), high=Decimal("3.7")),
        ]
        for row in broken_rows:
            bar = make_adapter(FakeStores()).project_bar(row, INSTRUMENT_ID)
            self.assertIs(bar.evidence.quality_status, QualityStatus.INVALID)
        zero_open_bar = make_adapter(FakeStores()).project_bar(broken_rows[0], INSTRUMENT_ID)
        self.assertEqual(zero_open_bar.open, Decimal("0"))
        negative_open_bar = make_adapter(FakeStores()).project_bar(broken_rows[1], INSTRUMENT_ID)
        self.assertEqual(negative_open_bar.open, Decimal("-1.0"))

    def test_no_false_strict_pit_without_known_at(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
        )
        adapter = make_adapter(stores)
        pit_status = adapter.pit_status()
        self.assertEqual(pit_status["daily_bars"], "non_strict")
        resolution = adapter.resolve(INSTRUMENT_ID, sessions=SESSIONS[:1], data_cutoff=CUTOFF)
        history = adapter.bars(INSTRUMENT_ID, resolution=resolution)
        self.assertIsNone(history.bars[0].evidence.known_at)
        self.assertEqual(history.bars[0].evidence.quality_status, QualityStatus.COMPLETE)


class CrossCodeAdapterTestCase(unittest.TestCase):
    """Matrix items 2-3: stable identity across a code change."""

    def setUp(self) -> None:
        self.old_days = [date(2026, 8, 17), date(2026, 8, 18)]
        self.new_days = [date(2026, 8, 19), date(2026, 8, 20)]
        self.sessions = self.old_days + self.new_days
        self.stores = FakeStores(
            mappings=[
                make_mapping("OLD300.SH", date(2020, 1, 1), date(2026, 8, 19)),
                make_mapping("NEW300.SH", date(2026, 8, 19)),
            ],
            bar_rows=[make_bar_row(day, "OLD300.SH") for day in self.old_days]
            + [make_bar_row(day, "NEW300.SH") for day in self.new_days],
        )

    def test_segments_read_old_and_new_source_codes(self) -> None:
        adapter = make_adapter(self.stores)
        resolution = adapter.resolve(
            INSTRUMENT_ID, sessions=self.sessions, data_cutoff=CUTOFF
        )
        history = adapter.bars(INSTRUMENT_ID, resolution=resolution)
        bindings = history.resolution.session_bindings
        self.assertEqual(
            [bindings[day] for day in self.sessions],
            ["OLD300.SH", "OLD300.SH", "NEW300.SH", "NEW300.SH"],
        )
        self.assertTrue(
            all(bar.instrument_id == INSTRUMENT_ID for bar in history.bars)
        )
        read_codes = [code for kind, code in self.stores.read_calls]
        self.assertIn("OLD300.SH", read_codes)
        self.assertIn("NEW300.SH", read_codes)

    def test_mapping_gap_blocks_instead_of_using_current_code(self) -> None:
        # The new-code mapping was learned only after the query cutoff.
        late_stores = FakeStores(
            mappings=[
                make_mapping("OLD300.SH", date(2020, 1, 1), date(2026, 8, 19)),
                make_mapping(
                    "NEW300.SH",
                    date(2026, 8, 19),
                    known_at=datetime(2026, 8, 25, tzinfo=UTC),
                ),
            ],
            bar_rows=self.stores.bar_rows,
        )
        adapter = make_adapter(late_stores)
        early_cutoff = datetime(2026, 8, 21, tzinfo=UTC)
        boundary = QueryBoundary(data_cutoff=early_cutoff, include_cutoff_day=True)
        with self.assertRaises((IdentityMappingIncompleteError, DataContractError)):
            adapter.resolve(
                INSTRUMENT_ID, sessions=self.sessions, data_cutoff=early_cutoff
            )


class CoverageBlockingTestCase(unittest.TestCase):
    """Matrix item 4: partial coverage blocks under strict mode."""

    def setUp(self) -> None:
        self.stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[
                make_bar_row(day)
                for day in SESSIONS
                if day != date(2026, 8, 19)
            ],
        )

    def test_missing_bar_blocks_the_read(self) -> None:
        adapter = make_adapter(self.stores)
        resolution = adapter.resolve(INSTRUMENT_ID, sessions=SESSIONS, data_cutoff=CUTOFF)
        with self.assertRaises(HistoryBarsIncompleteError):
            adapter.bars(INSTRUMENT_ID, resolution=resolution)

    def test_partial_coverage_summary_reports_missing_session(self) -> None:
        adapter = make_adapter(self.stores)
        returned = [row.trade_date for row in self.stores.bar_rows]
        coverage = adapter.coverage_summary(SESSIONS, returned)
        self.assertEqual(coverage["expected_sessions"], 5)
        self.assertEqual(coverage["returned_sessions"], 4)
        self.assertEqual(coverage["missing_sessions"], ["2026-08-19"])
        self.assertEqual(coverage["status"], "partial")


class AdjustmentPolicyTestCase(unittest.TestCase):
    """Matrix items 7-8: activation gate and effective-date cutoff."""

    def setUp(self) -> None:
        self.stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
            factor_rows=[make_factor_row(day) for day in SESSIONS],
        )
        self.resolution = make_adapter(self.stores).resolve(
            INSTRUMENT_ID, sessions=SESSIONS, data_cutoff=CUTOFF
        )

    def test_inactive_policy_blocks_adjusted_series(self) -> None:
        adapter = make_adapter(self.stores, adjustment_active=False)
        with self.assertRaises(UnsupportedCapabilityError):
            adapter.adjusted_series(
                INSTRUMENT_ID,
                resolution=self.resolution,
                price_basis=PriceBasis.QFQ,
            )

    def test_active_policy_requires_verification_evidence(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            make_adapter(self.stores, adjustment_active=True)
        adapter = make_adapter(
            self.stores,
            adjustment_policy=make_verified_adjustment_policy(),
        )
        series = adapter.adjusted_series(
            INSTRUMENT_ID,
            resolution=self.resolution,
            price_basis=PriceBasis.QFQ,
        )
        self.assertEqual([point.point_date for point in series.points], SESSIONS)

    def test_factor_selection_respects_effective_date_cutoff(self) -> None:
        # A factor dated after the cutoff exists in storage but must never
        # be selected; sessions past the cutoff are refused before reads.
        future_day = date(2026, 8, 24)
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
            factor_rows=[make_factor_row(day) for day in SESSIONS]
            + [make_factor_row(future_day)],
        )
        adapter = make_adapter(
            stores,
            adjustment_policy=make_verified_adjustment_policy(),
        )
        series = adapter.adjusted_series(
            INSTRUMENT_ID,
            resolution=self.resolution,
            price_basis=PriceBasis.QFQ,
        )
        self.assertEqual(
            [point.point_date for point in series.points], SESSIONS
        )
        with self.assertRaises(DataCutoffExceededError):
            adapter.resolve(
                INSTRUMENT_ID, sessions=[future_day], data_cutoff=CUTOFF
            )

    def test_missing_factor_blocks_partial_series(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            factor_rows=[make_factor_row(day) for day in SESSIONS[:4]],
        )
        adapter = make_adapter(
            stores,
            adjustment_policy=make_verified_adjustment_policy(),
        )
        resolution = adapter.resolve(INSTRUMENT_ID, sessions=SESSIONS, data_cutoff=CUTOFF)
        with self.assertRaises(HistoryBarsIncompleteError):
            adapter.adjusted_series(
                INSTRUMENT_ID,
                resolution=resolution,
                price_basis=PriceBasis.QFQ,
            )


class SummaryAndHashTestCase(unittest.TestCase):
    def test_bar_evidence_projects_source_revision_and_known_at_none(self) -> None:
        row = make_bar_row(SESSIONS[0], source_revision="rev-1")
        adapter = make_adapter(FakeStores())
        bar = adapter.project_bar(row, INSTRUMENT_ID)
        self.assertEqual(bar.evidence.source_revision, "rev-1")
        self.assertIsNone(bar.evidence.known_at)
        self.assertEqual(bar.evidence.observed_at, row.updated_at)

    def test_data_revision_summary_tracks_legacy_and_accepted_time(self) -> None:
        rows = [
            make_bar_row(SESSIONS[0], source_revision="rev-1"),
            make_bar_row(SESSIONS[1], source_revision=None),
        ]
        adapter = make_adapter(FakeStores())
        summary = adapter.preflight_summary(
            instrument_ids=[INSTRUMENT_ID],
            expected_sessions=SESSIONS[:2],
            bars_by_instrument={INSTRUMENT_ID: SESSIONS[:2]},
            daily_rows=rows,
        )
        revision = summary["source_revisions"]["__data_revision_summary__"]
        self.assertEqual(revision["contract"], "data_revision_summary@1")
        self.assertEqual(revision["status"], "partial")
        self.assertEqual(revision["capabilities"]["bars"]["missing_revision_count"], 1)

    def test_data_revision_summary_counts_only_explicit_correction_audit(self) -> None:
        rows = [
            make_bar_row(SESSIONS[0], source_revision="rev-1", change_kind="metadata_backfill"),
            make_bar_row(SESSIONS[1], source_revision="rev-2", change_kind="correction"),
        ]
        adapter = make_adapter(FakeStores())
        summary = adapter.preflight_summary(
            instrument_ids=[INSTRUMENT_ID], expected_sessions=SESSIONS[:2],
            bars_by_instrument={INSTRUMENT_ID: SESSIONS[:2]}, daily_rows=rows,
        )
        bars = summary["source_revisions"]["__data_revision_summary__"]["capabilities"]["bars"]
        self.assertEqual(bars["correction_count"], 1)
        self.assertEqual(bars["affected_range"], {
            "start": SESSIONS[1].isoformat(), "end": SESSIONS[1].isoformat(),
            "correction_count": 1,
        })

    def test_data_revision_summary_does_not_infer_correction_from_source_revision(self) -> None:
        rows = [make_bar_row(day, source_revision=f"rev-{index}") for index, day in enumerate(SESSIONS[:2])]
        adapter = make_adapter(FakeStores())
        summary = adapter.preflight_summary(
            instrument_ids=[INSTRUMENT_ID], expected_sessions=SESSIONS[:2],
            bars_by_instrument={INSTRUMENT_ID: SESSIONS[:2]}, daily_rows=rows,
        )
        bars = summary["source_revisions"]["__data_revision_summary__"]["capabilities"]["bars"]
        self.assertEqual(bars["correction_count"], 0)
        self.assertEqual(bars["affected_range"], {
            "start": None, "end": None, "correction_count": 0,
        })
    """Matrix items 9-10: revision sensitivity and hash stability."""

    def build_summary(
        self,
        *,
        bar_stamp,
        factor_stamp,
        trading_status_limitation=None,
        trading_status_applicability=None,
    ):
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
            factor_rows=[make_factor_row(day) for day in SESSIONS],
        )
        adapter = make_adapter(stores)
        for row in stores.bar_rows:
            row["updated_at"] = bar_stamp
        for row in stores.factor_rows:
            row["updated_at"] = factor_stamp
        bars_by = {INSTRUMENT_ID: [day for day in SESSIONS]}
        factors_by = {INSTRUMENT_ID: list(SESSIONS)}
        values = dict(
            instrument_ids=[INSTRUMENT_ID],
            expected_sessions=SESSIONS,
            bars_by_instrument=bars_by,
            factors_by_instrument=factors_by,
            daily_rows=list(stores.bar_rows),
            factor_rows=list(stores.factor_rows),
        )
        if trading_status_limitation is not None:
            values["trading_status_limitation"] = trading_status_limitation
        if trading_status_applicability is not None:
            values["trading_status_applicability"] = trading_status_applicability
        return adapter.preflight_summary(**values)

    def test_report_hash_stable_for_identical_facts(self) -> None:
        stamp = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
        first = self.build_summary(bar_stamp=stamp, factor_stamp=stamp)
        second = self.build_summary(bar_stamp=stamp, factor_stamp=stamp)
        self.assertEqual(first["report_hash"], second["report_hash"])

    def test_revision_change_is_detectable(self) -> None:
        stamp_a = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
        stamp_b = datetime(2026, 8, 22, 2, 0, tzinfo=UTC)
        first = self.build_summary(bar_stamp=stamp_a, factor_stamp=stamp_a)
        second = self.build_summary(bar_stamp=stamp_b, factor_stamp=stamp_a)
        self.assertNotEqual(first["report_hash"], second["report_hash"])
        self.assertEqual(
            first["source_revisions"]["daily_bars"]["latest_observed_at"],
            stamp_a.isoformat(),
        )
        self.assertEqual(
            second["source_revisions"]["daily_bars"]["latest_observed_at"],
            stamp_b.isoformat(),
        )
        # The T20 revision summary is additive: legacy observation markers
        # remain available alongside the derived revision-vector fields.
        self.assertIn("revision_vector_hash", first["source_revisions"]["daily_bars"])
        self.assertEqual(
            first["source_revisions"]["__data_revision_summary__"]["contract"],
            "data_revision_summary@1",
        )

    def test_trading_status_summary_is_explicit_without_status_facts(self) -> None:
        stamp = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
        summary = self.build_summary(bar_stamp=stamp, factor_stamp=stamp)
        trading_status = summary["trading_status"]

        self.assertEqual(trading_status["model"], "not_modeled")
        self.assertEqual(
            trading_status["rule_package"],
            {"key": "china_listed_etf_rules", "version": 1},
        )
        self.assertEqual(trading_status["required_dimensions"], [])
        self.assertEqual(
            trading_status["not_applicable_dimensions"],
            ["suspension", "opening_availability", "price_limit_tradability"],
        )
        self.assertFalse(trading_status["provider_required"])
        self.assertFalse(trading_status["coverage_required"])
        self.assertTrue(trading_status["limitation"])
        for key in ("source", "coverage", "status"):
            self.assertNotIn(key, trading_status)

    def test_trading_status_limitation_is_excluded_but_declaration_is_hashed(self) -> None:
        stamp = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
        first = self.build_summary(
            bar_stamp=stamp,
            factor_stamp=stamp,
            trading_status_limitation="首期模型不模拟交易状态",
        )
        second = self.build_summary(
            bar_stamp=stamp,
            factor_stamp=stamp,
            trading_status_limitation="交易状态事实不在本次摘要范围内",
        )
        required = self.build_summary(
            bar_stamp=stamp,
            factor_stamp=stamp,
            trading_status_applicability={
                "suspension": "required",
                "opening_availability": "not_applicable",
                "price_limit_tradability": "not_applicable",
            },
        )

        self.assertEqual(first["report_hash"], second["report_hash"])
        self.assertNotEqual(first["report_hash"], required["report_hash"])

    def test_unrequested_status_fixture_is_absent_from_summary(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
        )
        values = dict(
            instrument_ids=[INSTRUMENT_ID],
            expected_sessions=SESSIONS,
            bars_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
            daily_rows=list(stores.bar_rows),
        )
        plain = make_adapter(stores).preflight_summary(**values)
        unrequested = make_adapter(stores).preflight_summary(
            **values,
            fixtures=(make_status_fixture(),),
        )

        self.assertEqual(unrequested["fixtures"], [])
        self.assertNotIn("status", unrequested["coverage"])
        self.assertEqual(plain["report_hash"], unrequested["report_hash"])

        requested = make_adapter(stores).preflight_summary(
            **values,
            fixtures=(make_status_fixture(),),
            required_capabilities=(DataCapability.BARS, DataCapability.STATUS),
        )
        self.assertEqual(
            [item["capability"] for item in requested["fixtures"]],
            ["trading_status"],
        )
        self.assertNotIn("status", requested["coverage"])


class ResultRecordIntegrationTestCase(unittest.TestCase):
    """Matrix item 11: payloads ride the existing record and API schema."""

    def test_payloads_round_trip_through_record_and_api_schema(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
            factor_rows=[make_factor_row(day) for day in SESSIONS],
        )
        adapter = make_adapter(stores)
        summary = adapter.preflight_summary(
            instrument_ids=[INSTRUMENT_ID],
            expected_sessions=SESSIONS,
            bars_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
            factors_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
            daily_rows=list(stores.bar_rows),
            factor_rows=list(stores.factor_rows),
        )
        payloads = build_data_preflight_payloads(summary)
        record = BacktestDataPreflightRecord(
            run_id=uuid4(),
            phase=DataPhase.ADMISSION,
            status="ready",
            report_hash=summary["report_hash"],
            **payloads,
        )
        item = BacktestDataPreflightItem(
            run_id=record.run_id,
            phase=record.phase.value,
            status=record.status,
            report_hash=record.report_hash,
            capabilities=record.capabilities,
            calendar_summary=record.calendar_summary,
            session_summary=record.session_summary,
            pit_status=record.pit_status,
            coverage=record.coverage,
            source_revisions=record.source_revisions,
        )
        # The adjustment policy is a contract marker, not missing
        # knowledge-time evidence: it must never appear in the run-level
        # non_strict list.
        self.assertEqual(item.pit_status, "non_strict:daily_bars,trading_calendar")
        self.assertIn("daily_bars", item.coverage)
        self.assertIn("latest_observed_at", item.source_revisions["daily_bars"])
        self.assertIn(
            "tushare_adj_factor_native",
            item.capabilities["adjustment_series_policy"]["key"],
        )
        persisted_trading_status = item.coverage["trading_status"]
        self.assertEqual(
            persisted_trading_status["model"],
            summary["trading_status"]["model"],
        )
        self.assertEqual(
            persisted_trading_status["rule_package"],
            summary["trading_status"]["rule_package"],
        )
        self.assertEqual(
            tuple(persisted_trading_status["not_applicable_dimensions"]),
            tuple(summary["trading_status"]["not_applicable_dimensions"]),
        )
        # The activation audit rides the coverage payload.
        validation = item.coverage["adjustment_series_validation"]
        self.assertIs(validation["active"], False)
        self.assertIsNone(validation["verification_evidence"])

    def test_blocked_issue_sets_failure_reason(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[
                make_bar_row(day)
                for day in SESSIONS
                if day != date(2026, 8, 19)
            ],
        )
        adapter = make_adapter(stores)
        summary = adapter.preflight_summary(
            instrument_ids=[INSTRUMENT_ID],
            expected_sessions=SESSIONS,
            bars_by_instrument={
                INSTRUMENT_ID: [day for day in SESSIONS if day != date(2026, 8, 19)]
            },
            factors_by_instrument=None,
            daily_rows=list(stores.bar_rows),
            blocking_issues=[
                {"code": "history_incomplete", "missing_session": "2026-08-19"}
            ],
        )
        payloads = build_data_preflight_payloads(summary)
        self.assertEqual(payloads["session_summary"]["failure_reason"], "history_incomplete")
        coverage = payloads["coverage"]["daily_bars"][str(INSTRUMENT_ID)]
        self.assertEqual(coverage["status"], "partial")


class NoNetworkTestCase(unittest.TestCase):
    """Matrix item 12: the backtest path never imports Tushare."""

    def test_adapter_modules_declare_no_external_source_imports(self) -> None:
        # Static check: neither the adapter package nor any module it
        # imports from the data layer may declare a Tushare (or other
        # network client) import.  A runtime sys.modules check would be
        # polluted by unrelated ingestion tests in a full-suite run.
        import ast
        import inspect

        from app.backtesting.data.adapters import etf as etf_module

        forbidden_prefixes = ("tushare", "requests", "httpx", "aiohttp", "urllib")
        modules = [etf_module]
        for module in list(modules):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        names = [node.module]
                for name in names:
                    for prefix in forbidden_prefixes:
                        self.assertFalse(
                            name == prefix or name.startswith(prefix + "."),
                            f"{module.__name__} imports {name}",
                        )


class QualificationProjectionTestCase(unittest.TestCase):
    """The ETF adapter implements the shared single-instrument port."""

    def _request(
        self,
        *,
        required_capabilities=(DataCapability.BARS,),
        required_fixture_capabilities=(),
        fixtures=(),
    ):
        window = DateRange(SESSIONS[0], SESSIONS[-1])
        return CoverageQualificationRequest(
            instrument_id=INSTRUMENT_ID,
            effective_date=SESSIONS[0],
            requested_window=window,
            formal_envelope=window,
            warmup_envelope=None,
            history_envelope=None,
            required_capabilities=required_capabilities,
            query_boundary=BOUNDARY,
            preflight_profile=INTERNAL_LINK_ACCEPTANCE_PROFILE,
            resolved_calendar_ids=("XSHG",),
            required_fixture_capabilities=required_fixture_capabilities,
            fixtures=fixtures,
        )

    def test_complete_rows_are_eligible(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
        )
        result = make_adapter(stores).qualify(self._request())
        self.assertTrue(result.eligible)
        self.assertEqual(result.coverage_reports[0].complete_count, len(SESSIONS))

    def test_invalid_source_row_remains_invalid(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[
                make_bar_row(
                    SESSIONS[0],
                    open=Decimal("0"),
                )
            ],
        )
        result = make_adapter(stores).qualify(self._request())
        self.assertFalse(result.eligible)
        self.assertEqual(result.coverage_reports[0].quality_status, QualityStatus.INVALID)
        self.assertIn("bar_invalid", result.reason_codes)

    def test_unrequested_status_fixture_is_not_consumed_or_hashed(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
        )
        request = self._request()
        plain = make_adapter(stores).qualify(request)
        with_status_fixture = make_adapter(
            stores, fixtures=(make_status_fixture(),)
        ).qualify(request)

        self.assertTrue(with_status_fixture.eligible)
        self.assertEqual(with_status_fixture.reason_codes, ())
        self.assertEqual(
            [item.capability for item in with_status_fixture.coverage_reports],
            [DataCapability.BARS],
        )
        self.assertEqual(with_status_fixture.evidence_summary["fixtures"], ())
        self.assertEqual(
            with_status_fixture.evidence_summary["request"]["fixtures"], ()
        )
        self.assertEqual(plain.qualification_hash, with_status_fixture.qualification_hash)

    def test_explicit_status_request_retains_existing_fixture_gate(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
        )
        request = self._request(required_capabilities=(DataCapability.BARS, DataCapability.STATUS))

        missing = make_adapter(stores).qualify(request)
        self.assertFalse(missing.eligible)
        self.assertIn("internal_preflight_fixture_missing", missing.reason_codes)

        provided = make_adapter(
            stores, fixtures=(make_status_fixture(),)
        ).qualify(request)
        self.assertTrue(provided.eligible)
        self.assertEqual(
            [item.capability for item in provided.coverage_reports],
            [DataCapability.BARS],
        )
        self.assertEqual(
            [item["capability"] for item in provided.evidence_summary["fixtures"]],
            ["trading_status"],
        )

    def test_formal_profile_still_rejects_attached_status_fixture(self) -> None:
        stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
        )
        request = CoverageQualificationRequest(
            instrument_id=INSTRUMENT_ID,
            effective_date=SESSIONS[0],
            requested_window=DateRange(SESSIONS[0], SESSIONS[-1]),
            formal_envelope=DateRange(SESSIONS[0], SESSIONS[-1]),
            warmup_envelope=None,
            history_envelope=None,
            required_capabilities=(DataCapability.BARS,),
            query_boundary=BOUNDARY,
            preflight_profile=FORMAL_PROFILE,
            resolved_calendar_ids=("XSHG",),
        )

        with self.assertRaises(InvalidDataRequestError):
            make_adapter(stores, fixtures=(make_status_fixture(),)).qualify(request)


class WrongSourceCodeTestCase(unittest.TestCase):
    """P1: rows returned for another source code must be blocked.

    The generic Bar/point envelopes drop ``ts_code``, so a repository bug
    returning another code's rows would silently poison one identity's
    history unless the adapter re-checks the key before projection.
    """

    def setUp(self) -> None:
        self.stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
            factor_rows=[make_factor_row(day) for day in SESSIONS],
        )

    def test_wrong_code_bar_row_blocks(self) -> None:
        # A buggy repository returns rows for *any* code; only the
        # adapter's key re-check stands between the wrong rows and one
        # identity's stitched history.
        poisoned = FakeEtfRow(make_bar_row(SESSIONS[0]))
        poisoned["ts_code"] = "WRONG.SH"
        good_rows = [make_bar_row(day) for day in SESSIONS[1:]]

        def leaky_bars(ts_code, start_date, end_date):
            return [
                row
                for row in [poisoned] + good_rows
                if start_date <= row.trade_date <= end_date
            ]

        stores = FakeStores(mappings=self.stores.mappings)
        adapter = make_adapter(
            stores,
            daily_bars=leaky_bars,
        )
        resolution = adapter.resolve(INSTRUMENT_ID, sessions=SESSIONS, data_cutoff=CUTOFF)
        with self.assertRaises(ProviderContractViolationError):
            adapter.bars(INSTRUMENT_ID, resolution=resolution)

    def test_wrong_code_factor_row_blocks(self) -> None:
        poisoned = FakeEtfRow(make_factor_row(SESSIONS[0]))
        poisoned["ts_code"] = "WRONG.SH"
        good_rows = [make_factor_row(day) for day in SESSIONS[1:]]

        def leaky_factors(ts_code, start_date, end_date):
            return [
                row
                for row in [poisoned] + good_rows
                if start_date <= row.trade_date <= end_date
            ]

        stores = FakeStores(mappings=self.stores.mappings)
        adapter = make_adapter(
            stores,
            adjustment_factors=leaky_factors,
            adjustment_policy=make_verified_adjustment_policy(),
        )
        resolution = adapter.resolve(INSTRUMENT_ID, sessions=SESSIONS, data_cutoff=CUTOFF)
        with self.assertRaises(ProviderContractViolationError):
            adapter.adjusted_series(
                INSTRUMENT_ID, resolution=resolution, price_basis=PriceBasis.QFQ
            )

    def test_row_without_code_fails_closed(self) -> None:
        codeless = FakeEtfRow(make_bar_row(SESSIONS[0]))
        del codeless["ts_code"]
        with self.assertRaises(ProviderContractViolationError):
            make_adapter(FakeStores()).require_row_code(codeless, "510300.SH")


class InstrumentSpecProjectionTestCase(unittest.TestCase):
    """P1: ETF entity rows project to InstrumentSpec / InstrumentDisplay."""

    def make_entity_row(self, **overrides):
        values = dict(
            etf_id=INSTRUMENT_ID,
            ts_code="510300.SH",
            cname="沪深300ETF",
            csname="华泰柏瑞沪深300ETF",
            exchange="SH",
            list_date=date(2012, 5, 28),
            identity_fact=InstrumentIdentityFact(
                instrument_id=INSTRUMENT_ID,
                fact_version=1,
                asset_class="etf",
                exchange="SH",
                currency="CNY",
                calendar_id="XSHG",
                valid_from=date(2012, 5, 28),
                known_at=CUTOFF,
                observed_at=CUTOFF,
                evidence="test://identity",
            ),
        )
        values.update(overrides)
        return FakeEtfRow(values)

    def setUp(self) -> None:
        self.adapter = make_adapter(FakeStores())

        class Provider:
            def __init__(self) -> None:
                self.calls = []

            def resolve_spec(self, instrument_id, *, effective_at, data_cutoff):
                self.calls.append((instrument_id, effective_at, data_cutoff))
                return InstrumentSpec(
                    instrument_id=instrument_id,
                    display=InstrumentDisplay(
                        instrument_id=instrument_id,
                        trading_code="510300",
                        name="沪深300ETF",
                    ),
                    asset_class="etf",
                    exchange="SH",
                    currency="CNY",
                    calendar_id="XSHG",
                    price_precision=3,
                    quantity_precision=0,
                    price_tick="0.001",
                    lot_size="100",
                    minimum_order_quantity="100",
                    contract_multiplier="1",
                    trading_session_template=VersionedReference(
                        key="cn_etf_session_template", version=1
                    ),
                    trading_hours={"timezone": "Asia/Shanghai", "sessions": []},
                    settlement_rule_class="t1_before_open_match",
                    sellable_rule=StrategyRuleDeclaration(
                        ("sell_limited_by_available_position",)
                    ),
                    fee_categories=frozenset({"commission"}),
                    trading_status_policy={
                        "suspension": TradingStatusRequirement.REQUIRED,
                        "opening_availability": TradingStatusRequirement.REQUIRED,
                        "price_limit_tradability": TradingStatusRequirement.NOT_APPLICABLE,
                    },
                    order_types=frozenset({"limit"}),
                    price_limit_rule=VersionedReference(
                        key="cn_etf_price_limit_rule", version=1
                    ),
                    cash_availability_rule=VersionedReference(
                        key="cn_cash_availability_rule", version=1
                    ),
                    position_availability_rule=VersionedReference(
                        key="cn_position_availability_rule", version=1
                    ),
                    capabilities=InstrumentCapabilities(
                        position_sides=frozenset({"long"}),
                        order_types=frozenset({"limit"}),
                        margin_supported=False,
                        corporate_action_requirement=CorporateActionRequirement.NOT_APPLICABLE,
                    ),
                    rule_package_reference=VersionedReference(
                        key="china_listed_etf_rules", version=1
                    ),
                    valid_from=datetime(2012, 5, 28, tzinfo=UTC),
                    valid_to=None,
                )

        self.provider = Provider()

    def test_entity_row_projects_to_display(self) -> None:
        display = self.adapter.project_display(self.make_entity_row())
        self.assertEqual(display.instrument_id, INSTRUMENT_ID)
        self.assertEqual(display.trading_code, "510300")
        self.assertEqual(display.name, "沪深300ETF")

    def test_entity_row_projects_to_complete_spec(self) -> None:
        adapter = make_adapter(FakeStores(), spec_provider=self.provider)
        spec = adapter.project_instrument_spec(
            self.make_entity_row(), data_cutoff=CUTOFF
        )
        self.assertIsInstance(spec, InstrumentSpec)
        self.assertEqual(spec.instrument_id, INSTRUMENT_ID)
        self.assertEqual(spec.asset_class, "etf")
        self.assertEqual(spec.exchange, "SH")
        self.assertEqual(spec.currency, "CNY")
        self.assertEqual(spec.calendar_id, "XSHG")
        self.assertEqual(spec.price_tick, Decimal("0.001"))
        self.assertEqual(spec.lot_size, Decimal("100"))
        self.assertEqual(spec.contract_multiplier, Decimal("1"))
        self.assertEqual(spec.valid_from, datetime(2012, 5, 28, tzinfo=UTC))
        self.assertIsNone(spec.valid_to)
        self.assertEqual(self.provider.calls[0][0], INSTRUMENT_ID)
        self.assertEqual(
            self.provider.calls[0][1], datetime(2012, 5, 28, tzinfo=UTC)
        )

    def test_provider_returning_none_does_not_create_a_half_spec(self) -> None:
        class NullProvider:
            def resolve_spec(self, instrument_id, *, effective_at, data_cutoff):
                return None

        adapter = make_adapter(FakeStores(), spec_provider=NullProvider())
        self.assertIsNone(
            adapter.project_instrument_spec(
                self.make_entity_row(), data_cutoff=CUTOFF
            )
        )

    def test_provider_identity_mismatch_is_rejected(self) -> None:
        class WrongIdentityProvider:
            def resolve_spec(self, instrument_id, *, effective_at, data_cutoff):
                return self._provider.resolve_spec(
                    uuid4(), effective_at=effective_at, data_cutoff=data_cutoff
                )

            _provider = None

        wrong = WrongIdentityProvider()
        wrong._provider = self.provider
        adapter = make_adapter(FakeStores(), spec_provider=wrong)
        with self.assertRaises(ProviderContractViolationError):
            adapter.project_instrument_spec(
                self.make_entity_row(), data_cutoff=CUTOFF
            )

    def test_explicit_effective_date_can_resolve_without_list_date(self) -> None:
        adapter = make_adapter(FakeStores(), spec_provider=self.provider)
        explicit_date = date(2012, 6, 1)
        spec = adapter.project_instrument_spec(
            self.make_entity_row(list_date=None),
            effective_date=explicit_date,
            data_cutoff=CUTOFF,
        )
        self.assertIsInstance(spec, InstrumentSpec)
        self.assertEqual(
            self.provider.calls[-1][1], datetime(2012, 6, 1, tzinfo=UTC)
        )

    def test_adapter_without_spec_provider_blocks_without_defaults(self) -> None:
        with self.assertRaises(InstrumentCalendarUnresolvedError):
            self.adapter.project_instrument_spec(
                self.make_entity_row(), data_cutoff=CUTOFF
            )

    def test_rows_without_mandatory_facts_yield_none(self) -> None:
        # No entity binding: no stable identity to project.
        self.assertIsNone(
            self.adapter.project_instrument_spec(
                self.make_entity_row(etf_id=None)
            )
        )
        # No exchange: trading-critical fact missing.
        self.assertIsNone(
            self.adapter.project_instrument_spec(self.make_entity_row(exchange=None))
        )
        # No listing date cannot be placed on the timeline: setup_date is
        # deliberately NOT a fallback, or unlisted funds would become
        # tradable.
        self.assertIsNone(
            self.adapter.project_instrument_spec(
                self.make_entity_row(list_date=None, setup_date=date(2012, 1, 1))
            )
        )


class CoverageAnomalyTestCase(unittest.TestCase):
    """P2: duplicates and out-of-window returns are explicit, not deduped."""

    def test_duplicate_dates_downgrade_status(self) -> None:
        coverage = EtfFactsAdapter.coverage_summary(
            SESSIONS[:1], [SESSIONS[0], SESSIONS[0]]
        )
        self.assertEqual(coverage["returned_sessions"], 1)
        self.assertEqual(coverage["duplicate_sessions"], [SESSIONS[0].isoformat()])
        self.assertEqual(coverage["status"], "partial")

    def test_out_of_window_dates_are_recorded(self) -> None:
        coverage = EtfFactsAdapter.coverage_summary(
            SESSIONS[:2], list(SESSIONS[:2]) + [date(2030, 1, 1)]
        )
        self.assertEqual(coverage["out_of_window_sessions"], ["2030-01-01"])
        self.assertEqual(coverage["status"], "partial")

    def test_clean_full_coverage_stays_complete(self) -> None:
        coverage = EtfFactsAdapter.coverage_summary(SESSIONS, SESSIONS)
        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(coverage["duplicate_sessions"], [])
        self.assertEqual(coverage["out_of_window_sessions"], [])


class PersistenceChainTestCase(unittest.TestCase):
    """P1: the ETF summary survives a real repository write and read."""

    def setUp(self) -> None:
        self.stores = FakeStores(
            mappings=[make_mapping("510300.SH", date(2020, 1, 1))],
            bar_rows=[make_bar_row(day) for day in SESSIONS],
            factor_rows=[make_factor_row(day) for day in SESSIONS],
        )
        self.adapter = make_adapter(stores=self.stores)

    def test_summary_persists_through_result_repository(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        result_tables = [
            Base.metadata.tables[name] for name in RESULT_TABLE_NAMES
        ]
        Base.metadata.create_all(engine, tables=result_tables)
        session = Session(engine)
        try:
            repo = BacktestResultRepository(
                session, cursor_signing_key="unit-test-signing-key"
            )
            run_id = uuid4()
            resolution = self.adapter.resolve(
                INSTRUMENT_ID, sessions=SESSIONS, data_cutoff=CUTOFF
            )
            summary = self.adapter.preflight_summary(
                instrument_ids=[INSTRUMENT_ID],
                expected_sessions=SESSIONS,
                bars_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
                factors_by_instrument={INSTRUMENT_ID: list(SESSIONS)},
                mappings_by_instrument={INSTRUMENT_ID: self.stores.mappings},
                daily_rows=list(self.stores.bar_rows),
                factor_rows=list(self.stores.factor_rows),
            )
            payloads = build_data_preflight_payloads(summary)
            repo.append(
                "data_preflight",
                BacktestDataPreflightRecord(
                    run_id=run_id,
                    phase=DataPhase.SESSION,
                    status="ready",
                    report_hash=summary["report_hash"],
                    **payloads,
                ),
            )
            page = repo.read_page("data_preflight", run_id=run_id)
            self.assertEqual(len(page.items), 1)
            row = page.items[0]
            self.assertEqual(row.pit_status, "non_strict:daily_bars,trading_calendar")
            self.assertEqual(row.report_hash, summary["report_hash"])
            mapping_summary = row.coverage["instrument_mapping_summary"][
                str(INSTRUMENT_ID)
            ]
            self.assertEqual(mapping_summary[0]["source_code"], "510300.SH")
            self.assertIn("exchange announcement", mapping_summary[0]["evidence"])
            validation = row.coverage["adjustment_series_validation"]
            self.assertIs(validation["active"], False)
            self.assertEqual(
                row.source_revisions["daily_bars"]["latest_observed_at"],
                datetime(2026, 8, 21, 2, 0, tzinfo=UTC).isoformat(),
            )
        finally:
            session.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
