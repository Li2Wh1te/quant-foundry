"""Tests for the fixed-instrument rule preflight hard gate."""

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import VersionedReference
from app.instruments.rule_exceptions_repository import PersistedExceptionSet
from app.instruments.rule_preflight import (
    FixedInstrumentRulePreflightRequest,
    FixedInstrumentRulePreflightService,
    InstrumentRulePreflightResult,
    RuleCheckStatus,
)
from app.instruments.rule_snapshots import RunRuleSnapshotBundle
from app.instruments.rules import (
    FactQualityStatus,
    RuleExceptionEntry,
    RuleExceptionSetDefinition,
    RulePackageIssueCode,
    ResolutionStatus,
    exception_set_content_hash,
    register_china_listed_etf_rules,
    RulePackageRegistry,
)

PACKAGE_REF = VersionedReference(key="china_listed_etf_rules", version=1)
SET_REF = VersionedReference(key="etf_named_exceptions", version=2)
EXCEPTION_FACT_REF = VersionedReference(key="cash_etf_special_rule", version=4)
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)
KNOWN_AT = datetime(2025, 12, 1, tzinfo=UTC)
START = date(2026, 1, 5)
END = date(2026, 12, 31)


def complete_fields() -> dict[str, Any]:
    """Fixture-only complete v1 field values."""

    return {
        "lot_size": "100",
        "quantity_precision": 0,
        "price_precision": 3,
        "price_tick": "0.001",
        "contract_multiplier": "1",
        "trading_session_template": {"key": "cn_etf_session_template", "version": 1},
        "settlement_rule_class": "t1_before_open_match",
        "sellable_rule": {"statements": ["sell_limited_by_available_position"]},
        "fee_categories": ["commission"],
        "trading_status_applicability": {
            "suspension": "required",
            "opening_availability": "required",
            "price_limit_tradability": "not_applicable",
        },
        "currency": "cny",
        "order_types": ["limit", "market"],
        "minimum_order_quantity": "100",
        "price_limit_rule": {"key": "cn_etf_price_limit_rule", "version": 1},
        "cash_availability_rule": {"key": "cn_cash_availability_rule", "version": 1},
        "position_availability_rule": {
            "key": "cn_position_availability_rule",
            "version": 1,
        },
    }


def make_fact(
    instrument_id: UUID,
    *,
    fact_version: int = 3,
    fields: dict[str, Any] | None = None,
    valid_from: date = date(2024, 1, 1),
    valid_to: date | None = None,
    known_at: datetime = KNOWN_AT,
    quality_status: FactQualityStatus = FactQualityStatus.COMPLETE,
    fixture_only: bool = False,
    exception_fact_ref: VersionedReference | None = None,
    source: str = "exchange_rule_book",
) -> Any:
    from tests.test_instrument_rule_facts import RuleFactCandidate

    return RuleFactCandidate(
        fact_reference=VersionedReference(
            key=(
                "cash_etf_special_rule"
                if exception_fact_ref is not None
                else "etf_rule_fact"
            ),
            version=fact_version,
        ),
        instrument_id=instrument_id,
        package_reference=PACKAGE_REF,
        source=source,
        source_revision="rev-1",
        known_at=known_at,
        observed_at=known_at,
        quality_status=quality_status,
        fixture_only=fixture_only,
        content_hash="a" * 64,
        fields=complete_fields() if fields is None else fields,
        exception_fact_ref=exception_fact_ref,
        valid_from=valid_from,
        valid_to=valid_to,
    )


class FakeGateway:
    """In-memory gateway over prepared facts and one exception set."""

    def __init__(
        self,
        facts_by_instrument: dict[UUID, list],
        exception_definition: RuleExceptionSetDefinition | None = None,
        missing_status_dimensions: tuple[str, ...] = (),
        exception_set_quality: FactQualityStatus = FactQualityStatus.COMPLETE,
        exception_set_fixture_only: bool = False,
    ) -> None:
        self.facts = facts_by_instrument
        self.exception_definition = exception_definition
        self.missing_status_dimensions = missing_status_dimensions
        self.exception_set_quality = exception_set_quality
        self.exception_set_fixture_only = exception_set_fixture_only
        self.status_queries: list[tuple[UUID, tuple[str, ...], date, date]] = []

    def list_rule_facts(
        self, instrument_id, package_reference, *, start_date, end_date, data_cutoff
    ):
        # Emulate the repository's PIT visibility contract strictly.
        return tuple(
            fact
            for fact in self.facts.get(instrument_id, ())
            if fact.known_at <= data_cutoff
        )

    def resolve_exception_set(self, set_reference, *, data_cutoff):
        if self.exception_definition is None:
            return None
        if set_reference != self.exception_definition.reference:
            return None
        return PersistedExceptionSet(
            definition=self.exception_definition,
            source="exchange_announcement",
            source_revision="notice-42",
            known_at=KNOWN_AT,
            observed_at=KNOWN_AT,
            quality_status=self.exception_set_quality,
            fixture_only=self.exception_set_fixture_only,
            content_hash=exception_set_content_hash(self.exception_definition),
        )

    def check_required_trading_status_facts(
        self, instrument_id, dimensions, *, start_date, end_date, data_cutoff
    ):
        self.status_queries.append(
            (instrument_id, tuple(dimensions), start_date, end_date)
        )
        return tuple(
            dimension
            for dimension in dimensions
            if dimension in self.missing_status_dimensions
        )


def make_request(
    instrument_ids, *, exception_set_reference=None
) -> FixedInstrumentRulePreflightRequest:
    return FixedInstrumentRulePreflightRequest(
        instrument_ids=instrument_ids,
        start_date=START,
        end_date=END,
        data_cutoff=CUTOFF,
        rule_package_reference=PACKAGE_REF,
        exception_set_reference=exception_set_reference,
    )


def make_service(gateway: FakeGateway) -> FixedInstrumentRulePreflightService:
    registry = RulePackageRegistry()
    register_china_listed_etf_rules(registry)
    return FixedInstrumentRulePreflightService(registry, gateway)


def issue_codes(report) -> set[str]:
    return {issue.code.value for issue in report.issues}


class ReadyPathTestCase(unittest.TestCase):
    """Example A: a complete ordinary fact passes and freezes a snapshot."""

    def test_plain_fact_is_ready_with_one_segment(self) -> None:
        instrument_id = uuid4()
        gateway = FakeGateway(
            {instrument_id: [make_fact(instrument_id)]}
        )
        report = make_service(gateway).run(make_request([instrument_id]))
        self.assertIs(report.status, ResolutionStatus.READY)
        self.assertEqual(report.issues, ())
        self.assertIsInstance(report.snapshot_bundle, RunRuleSnapshotBundle)
        bundle = report.snapshot_bundle
        assert bundle is not None
        self.assertEqual(len(bundle.instrument_segments), 1)
        segment = bundle.instrument_segments[0]
        self.assertEqual(segment.effective_from, START)
        self.assertIsNone(segment.effective_to)
        self.assertEqual(segment.normal_fact_reference.key, "etf_rule_fact")
        self.assertEqual(segment.normalized_values["lot_size"], Decimal("100"))
        # Provenance keeps identity, source, and validity — not just values.
        provenance = segment.provenance["normal_fact"]
        self.assertEqual(provenance["fact_version"], 3)
        self.assertEqual(provenance["source"], "exchange_rule_book")
        self.assertEqual(provenance["valid_from"], "2024-01-01")
        self.assertIs(provenance["fixture_only"], False)
        # The snapshot hash is a lowercase SHA-256 suitable for DataRequest.
        self.assertEqual(len(report.snapshot_hash), 64)
        self.assertEqual(report.snapshot_hash, report.snapshot_hash.lower())

    def test_mid_window_fact_change_creates_multiple_segments(self) -> None:
        instrument_id = uuid4()
        first = make_fact(
            instrument_id,
            fact_version=1,
            valid_from=date(2024, 1, 1),
            valid_to=date(2026, 7, 1),
            fields={**complete_fields(), "lot_size": "100"},
        )
        second = make_fact(
            instrument_id,
            fact_version=2,
            valid_from=date(2026, 7, 1),
            valid_to=None,
            fields={
                **complete_fields(),
                "lot_size": "200",
                "minimum_order_quantity": "200",
            },
        )
        gateway = FakeGateway({instrument_id: [first, second]})
        report = make_service(gateway).run(make_request([instrument_id]))
        self.assertIs(report.status, ResolutionStatus.READY)
        bundle = report.snapshot_bundle
        assert bundle is not None
        self.assertEqual(len(bundle.instrument_segments), 2)
        first_segment, second_segment = bundle.instrument_segments
        self.assertEqual(first_segment.effective_from, START)
        self.assertEqual(first_segment.effective_to, date(2026, 7, 1))
        self.assertEqual(
            first_segment.normal_fact_reference.version, 1
        )
        self.assertEqual(
            first_segment.normalized_values["lot_size"], Decimal("100")
        )
        self.assertEqual(second_segment.effective_from, date(2026, 7, 1))
        self.assertIsNone(second_segment.effective_to)
        self.assertEqual(
            second_segment.normalized_values["lot_size"], Decimal("200")
        )
        # The run-level hash covers both segments.
        self.assertEqual(
            len(bundle.instrument_segments), len(
                {segment.sort_key() for segment in bundle.instrument_segments}
            )
        )

    def test_every_fixed_instrument_is_checked(self) -> None:
        # A fixed instrument without any initial position is still gated.
        checked, never_positioned = uuid4(), uuid4()
        gateway = FakeGateway(
            {
                checked: [make_fact(checked)],
                never_positioned: [],
            }
        )
        report = make_service(gateway).run(
            make_request([checked, never_positioned])
        )
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertEqual(
            {result.instrument_id for result in report.checked_instruments},
            {checked, never_positioned},
        )
        self.assertIn("RULE_FACT_MISSING", issue_codes(report))

    def test_not_applicable_declaration_is_preserved_into_the_snapshot(
        self,
    ) -> None:
        instrument_id = uuid4()
        gateway = FakeGateway({instrument_id: [make_fact(instrument_id)]})
        report = make_service(gateway).run(make_request([instrument_id]))
        bundle = report.snapshot_bundle
        assert bundle is not None
        self.assertEqual(
            bundle.instrument_segments[0].capability_declarations[
                "price_limit_tradability"
            ],
            "not_applicable",
        )

    def test_unregistered_package_is_a_structured_block(self) -> None:
        instrument_id = uuid4()
        service = FixedInstrumentRulePreflightService(
            RulePackageRegistry(), FakeGateway({instrument_id: [make_fact(instrument_id)]})
        )
        report = service.run(
            FixedInstrumentRulePreflightRequest(
                instrument_ids=(instrument_id,),
                start_date=START,
                end_date=END,
                data_cutoff=CUTOFF,
                rule_package_reference=VersionedReference(
                    key="china_listed_etf_rules", version=99
                ),
            )
        )
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_PACKAGE_MISMATCH", issue_codes(report))
        self.assertIsNone(report.snapshot_bundle)


class NamedExceptionTestCase(unittest.TestCase):
    """Example B: exceptions route to full alternate facts."""

    def setUp(self) -> None:
        self.instrument_id = uuid4()
        self.entry = RuleExceptionEntry(
            instrument_id=self.instrument_id,
            exception_fact_ref=EXCEPTION_FACT_REF,
            valid_from=date(2026, 1, 1),
            valid_to=None,
        )
        self.exception_set = RuleExceptionSetDefinition(
            reference=SET_REF,
            package_reference=PACKAGE_REF,
            entries=(self.entry,),
        )
        self.normal_fact = make_fact(self.instrument_id)
        self.exception_fact = make_fact(
            self.instrument_id,
            fact_version=4,
            exception_fact_ref=EXCEPTION_FACT_REF,
            fields={
                **complete_fields(),
                "lot_size": "1",
                "price_precision": 4,
                "price_tick": "0.0001",
            },
        )

    def test_matched_exception_is_ready_and_recorded(self) -> None:
        gateway = FakeGateway(
            {self.instrument_id: [self.normal_fact, self.exception_fact]},
            exception_definition=self.exception_set,
        )
        report = make_service(gateway).run(
            make_request([self.instrument_id], exception_set_reference=SET_REF)
        )
        self.assertIs(report.status, ResolutionStatus.READY, report.issues)
        bundle = report.snapshot_bundle
        assert bundle is not None
        self.assertEqual(
            bundle.exception_set_reference, SET_REF
        )
        self.assertEqual(
            bundle.exception_set_hash,
            exception_set_content_hash(self.exception_set),
        )
        segment = bundle.instrument_segments[0]
        self.assertEqual(segment.exception_fact_reference, EXCEPTION_FACT_REF)
        self.assertEqual(segment.normalized_values["lot_size"], Decimal("1"))
        self.assertEqual(segment.provenance["exception_fact"]["fact_version"], 4)
        # The set itself never carries the production numbers.
        self.assertNotIn("lot_size", str(self.entry))

    def test_missing_exception_set_blocks_every_instrument(self) -> None:
        gateway = FakeGateway({self.instrument_id: [self.normal_fact]})
        report = make_service(gateway).run(
            make_request([self.instrument_id], exception_set_reference=SET_REF)
        )
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_EXCEPTION_SET_MISSING", issue_codes(report))
        self.assertIsNone(report.snapshot_bundle)

    def test_exception_without_fact_blocks(self) -> None:
        gateway = FakeGateway(
            {self.instrument_id: [self.normal_fact]},
            exception_definition=self.exception_set,
        )
        report = make_service(gateway).run(
            make_request([self.instrument_id], exception_set_reference=SET_REF)
        )
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_EXCEPTION_FACT_MISSING", issue_codes(report))
        self.assertIsNone(report.snapshot_bundle)
        self.assertEqual(report.snapshot_hash, "")

    def test_exception_stops_at_its_exclusive_valid_to(self) -> None:
        # The entry's valid_to edge must segment the window: after it the
        # ordinary fact applies again instead of the exception persisting.
        ending_entry = RuleExceptionEntry(
            instrument_id=self.instrument_id,
            exception_fact_ref=EXCEPTION_FACT_REF,
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 6, 1),
        )
        ending_set = RuleExceptionSetDefinition(
            reference=SET_REF,
            package_reference=PACKAGE_REF,
            entries=(ending_entry,),
        )
        gateway = FakeGateway(
            {self.instrument_id: [self.normal_fact, self.exception_fact]},
            exception_definition=ending_set,
        )
        report = make_service(gateway).run(
            make_request([self.instrument_id], exception_set_reference=SET_REF)
        )
        self.assertIs(report.status, ResolutionStatus.READY, report.issues)
        bundle = report.snapshot_bundle
        assert bundle is not None
        first, second = bundle.instrument_segments
        self.assertEqual(first.effective_from, START)
        self.assertEqual(first.effective_to, date(2026, 6, 1))
        self.assertEqual(first.exception_fact_reference, EXCEPTION_FACT_REF)
        self.assertEqual(first.normalized_values["lot_size"], Decimal("1"))
        # After the exclusive valid_to the exception no longer applies.
        self.assertEqual(second.effective_from, date(2026, 6, 1))
        self.assertIsNone(second.exception_fact_reference)
        self.assertEqual(second.normalized_values["lot_size"], Decimal("100"))

    def test_wrong_fact_version_never_satisfies_the_entry(self) -> None:
        # A different version of the same exception-fact key must not be
        # accepted for an entry that declares cash_etf_special_rule@4.
        wrong_version = make_fact(
            self.instrument_id,
            fact_version=7,
            exception_fact_ref=EXCEPTION_FACT_REF,
            fields={
                **complete_fields(),
                "lot_size": "1",
                "price_precision": 4,
                "price_tick": "0.0001",
            },
        )
        gateway = FakeGateway(
            {self.instrument_id: [self.normal_fact, wrong_version]},
            exception_definition=self.exception_set,
        )
        report = make_service(gateway).run(
            make_request([self.instrument_id], exception_set_reference=SET_REF)
        )
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_EXCEPTION_FACT_MISSING", issue_codes(report))
        self.assertIsNone(report.snapshot_bundle)

    def test_incomplete_exception_set_is_blocked_in_formal_mode(self) -> None:
        gateway = FakeGateway(
            {self.instrument_id: [self.normal_fact, self.exception_fact]},
            exception_definition=self.exception_set,
            exception_set_quality=FactQualityStatus.INCOMPLETE,
        )
        report = make_service(gateway).run(
            make_request([self.instrument_id], exception_set_reference=SET_REF)
        )
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_FACT_NOT_COMPLETE", issue_codes(report))
        self.assertIsNone(report.snapshot_bundle)

    def test_fixture_exception_set_is_blocked_in_formal_mode(self) -> None:
        gateway = FakeGateway(
            {self.instrument_id: [self.normal_fact, self.exception_fact]},
            exception_definition=self.exception_set,
            exception_set_fixture_only=True,
        )
        report = make_service(gateway).run(
            make_request([self.instrument_id], exception_set_reference=SET_REF)
        )
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_FIXTURE_SOURCE_FORBIDDEN", issue_codes(report))
        self.assertIsNone(report.snapshot_bundle)

    def test_overlapping_exception_intervals_block(self) -> None:
        overlapping = RuleExceptionSetDefinition(
            reference=SET_REF,
            package_reference=PACKAGE_REF,
            entries=(
                self.entry,
                RuleExceptionEntry(
                    instrument_id=self.instrument_id,
                    exception_fact_ref=EXCEPTION_FACT_REF,
                    valid_from=date(2026, 6, 1),
                    valid_to=None,
                ),
            ),
        )
        gateway = FakeGateway(
            {self.instrument_id: [self.normal_fact, self.exception_fact]},
            exception_definition=overlapping,
        )
        report = make_service(gateway).run(
            make_request([self.instrument_id], exception_set_reference=SET_REF)
        )
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_EXCEPTION_INTERVAL_CONFLICT", issue_codes(report))


class BlockingTestCase(unittest.TestCase):
    """Missing, conflicting, incomplete, and fixture facts all block."""

    def setUp(self) -> None:
        self.instrument_id = uuid4()

    def _run(self, facts, **gateway_kwargs):
        gateway = FakeGateway({self.instrument_id: facts}, **gateway_kwargs)
        return make_service(gateway).run(make_request([self.instrument_id]))

    def test_missing_fact_blocks_with_no_defaults(self) -> None:
        report = self._run([])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_FACT_MISSING", issue_codes(report))
        self.assertIsNone(report.snapshot_bundle)

    def test_known_at_after_cutoff_is_invisible(self) -> None:
        # Example D: the fact exists but was learned after the cutoff, so
        # the run cannot see it and the gate blocks.
        late = make_fact(
            self.instrument_id,
            known_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        report = self._run([late])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_FACT_MISSING", issue_codes(report))

    def test_validity_gap_blocks(self) -> None:
        first = make_fact(
            self.instrument_id,
            fact_version=1,
            valid_from=date(2024, 1, 1),
            valid_to=date(2026, 3, 1),
        )
        second = make_fact(
            self.instrument_id,
            fact_version=2,
            valid_from=date(2026, 6, 1),
            valid_to=None,
        )
        report = self._run([first, second])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_FACT_MISSING", issue_codes(report))

    def test_overlapping_facts_conflict_without_insert_order_preference(
        self,
    ) -> None:
        first = make_fact(self.instrument_id, source="provider_one")
        second = make_fact(
            self.instrument_id,
            fact_version=4,
            source="provider_two",
        )
        report = self._run([first, second])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_FACT_CONFLICT", issue_codes(report))

    def test_incomplete_quality_blocks(self) -> None:
        fact = make_fact(
            self.instrument_id,
            quality_status=FactQualityStatus.INCOMPLETE,
        )
        report = self._run([fact])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_FACT_NOT_COMPLETE", issue_codes(report))

    def test_fixture_fact_is_blocked_in_formal_mode(self) -> None:
        fact = make_fact(self.instrument_id, fixture_only=True)
        report = self._run([fact])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_FIXTURE_SOURCE_FORBIDDEN", issue_codes(report))

    def test_missing_required_field_blocks(self) -> None:
        fields = complete_fields()
        del fields["price_tick"]
        report = self._run([make_fact(self.instrument_id, fields=fields)])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_REQUIRED_FIELD_MISSING", issue_codes(report))

    def test_non_first_phase_settlement_class_blocks(self) -> None:
        # Example E: same_day is known but never silently converted.
        fields = complete_fields()
        fields["settlement_rule_class"] = "same_day"
        report = self._run([make_fact(self.instrument_id, fields=fields)])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_SETTLEMENT_UNSUPPORTED", issue_codes(report))

    def test_blocked_report_carries_no_bundle_and_no_snapshot_hash(self) -> None:
        report = self._run([])
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIsNone(report.snapshot_bundle)
        self.assertEqual(report.snapshot_hash, "")

    def test_one_blocking_instrument_blocks_the_whole_report(self) -> None:
        good, bad = uuid4(), uuid4()
        gateway = FakeGateway(
            {
                good: [make_fact(good)],
                bad: [],
            }
        )
        report = make_service(gateway).run(make_request([good, bad]))
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIsNone(report.snapshot_bundle)
        statuses = {
            result.instrument_id: result.status
            for result in report.checked_instruments
        }
        self.assertEqual(
            statuses,
            {good: ResolutionStatus.READY, bad: ResolutionStatus.BLOCKED},
        )


class CapabilityCheckTestCase(unittest.TestCase):
    """Required capability dimensions demand point-in-time facts."""

    def setUp(self) -> None:
        self.instrument_id = uuid4()

    def test_missing_required_capability_fact_blocks(self) -> None:
        gateway = FakeGateway(
            {self.instrument_id: [make_fact(self.instrument_id)]},
            missing_status_dimensions=("suspension",),
        )
        report = make_service(gateway).run(make_request([self.instrument_id]))
        self.assertIs(report.status, ResolutionStatus.BLOCKED)
        self.assertIn("RULE_CAPABILITY_FACT_MISSING", issue_codes(report))
        result = report.checked_instruments[0]
        self.assertIs(
            result.capability_check_status, RuleCheckStatus.BLOCKED
        )
        self.assertIs(result.rules_check_status, RuleCheckStatus.OK)

    def test_required_dimensions_are_queried_per_segment(self) -> None:
        gateway = FakeGateway(
            {self.instrument_id: [make_fact(self.instrument_id)]}
        )
        report = make_service(gateway).run(make_request([self.instrument_id]))
        self.assertIs(report.status, ResolutionStatus.READY)
        # suspension and opening_availability are declared required;
        # price_limit_tradability is not_applicable and never queried.
        self.assertEqual(
            gateway.status_queries,
            [
                (
                    self.instrument_id,
                    ("opening_availability", "suspension"),
                    START,
                    END,
                )
            ],
        )

    def test_last_segment_checks_status_facts_until_window_end(self) -> None:
        # An open-ended final segment must be checked through the backtest
        # end date, not just its first day.
        gateway = FakeGateway(
            {self.instrument_id: [make_fact(self.instrument_id)]},
            missing_status_dimensions=("suspension",),
        )
        make_service(gateway).run(make_request([self.instrument_id]))
        self.assertTrue(gateway.status_queries)
        for _, _, query_start, query_end in gateway.status_queries:
            self.assertEqual(query_start, START)
            self.assertEqual(query_end, END)


class ReportBindingTestCase(unittest.TestCase):
    """A READY report is structurally bound to its verified bundle."""

    def _ready_report(self):
        instrument_id = uuid4()
        gateway = FakeGateway({instrument_id: [make_fact(instrument_id)]})
        return make_service(gateway).run(make_request([instrument_id]))

    def _rebuild(self, report, **overrides):
        values = dict(
            status=report.status,
            rule_package_reference=report.rule_package_reference,
            rule_package_semantic_hash=report.rule_package_semantic_hash,
            exception_set_reference=report.exception_set_reference,
            exception_set_hash=report.exception_set_hash,
            data_cutoff=report.data_cutoff,
            start_date=report.start_date,
            end_date=report.end_date,
            checked_instruments=report.checked_instruments,
            issues=(),
            snapshot_bundle=report.snapshot_bundle,
            snapshot_hash=report.snapshot_hash,
        )
        values.update(overrides)
        return type(report)(**values)

    def test_forged_snapshot_hash_cannot_be_constructed(self) -> None:
        import dataclasses

        report = self._ready_report()
        with self.assertRaises(DomainValidationError):
            dataclasses.replace(report, snapshot_hash="ab" * 32)

    def test_status_must_be_a_resolution_status(self) -> None:
        report = self._ready_report()
        for bogus in ("bogus", None, 1):
            with self.assertRaises(DomainValidationError):
                self._rebuild(report, status=bogus)

    def test_detached_bundle_cannot_be_constructed(self) -> None:
        import dataclasses

        report = self._ready_report()
        with self.assertRaises(DomainValidationError):
            dataclasses.replace(report, snapshot_bundle=None)
        with self.assertRaises(DomainValidationError):
            dataclasses.replace(report, snapshot_bundle=None, snapshot_hash="")

    def test_bundle_metadata_must_match_the_report(self) -> None:
        report = self._ready_report()
        original = report.snapshot_bundle
        # Rebuild the bundle with a shifted cutoff: internally consistent
        # hash, but it no longer binds to the report's declared cutoff.
        shifted_segments = tuple(
            type(segment)(
                instrument_id=segment.instrument_id,
                effective_from=segment.effective_from,
                effective_to=segment.effective_to,
                normal_fact_reference=segment.normal_fact_reference,
                exception_fact_reference=segment.exception_fact_reference,
                normalized_values=dict(segment.normalized_values),
                capability_declarations=dict(segment.capability_declarations),
                provenance=dict(segment.provenance),
                resolution_hash=segment.resolution_hash,
            )
            for segment in original.instrument_segments
        )
        from datetime import timedelta as _td

        shifted = RunRuleSnapshotBundle(
            rule_package_reference=original.rule_package_reference,
            rule_package_semantic_hash=original.rule_package_semantic_hash,
            parser_revision=original.parser_revision,
            exception_set_reference=original.exception_set_reference,
            exception_set_hash=original.exception_set_hash,
            data_cutoff=CUTOFF + _td(days=1),
            instrument_segments=shifted_segments,
        )
        with self.assertRaises(DomainValidationError):
            self._rebuild(
                report,
                snapshot_bundle=shifted,
                snapshot_hash=shifted.snapshot_hash,
            )

    def test_ready_report_rejects_blocked_results_or_missing_segments(
        self,
    ) -> None:
        from app.instruments.rules import ResolutionStatus as _RS

        report = self._ready_report()

        blocked_result = InstrumentRulePreflightResult(
            instrument_id=uuid4(),
            status=_RS.BLOCKED,
            rules_check_status=RuleCheckStatus.BLOCKED,
            capability_check_status=RuleCheckStatus.OK,
            resolved_segments=(),
            selected_fact_references=(),
            issues=(),
        )
        with self.assertRaises(DomainValidationError):
            self._rebuild(report, checked_instruments=(blocked_result,))

        orphan_result = InstrumentRulePreflightResult(
            instrument_id=uuid4(),
            status=_RS.READY,
            rules_check_status=RuleCheckStatus.OK,
            capability_check_status=RuleCheckStatus.OK,
            resolved_segments=(),
            selected_fact_references=(),
            issues=(),
        )
        with self.assertRaises(DomainValidationError):
            self._rebuild(
                report,
                checked_instruments=(
                    *report.checked_instruments,
                    orphan_result,
                ),
            )


class ReportIdentityTestCase(unittest.TestCase):
    """Report hashes are stable, content sensitive, and message free."""

    def test_identical_inputs_produce_identical_report_hashes(self) -> None:
        instrument_id = uuid4()
        gateway = FakeGateway({instrument_id: [make_fact(instrument_id)]})
        first = make_service(gateway).run(make_request([instrument_id]))
        gateway = FakeGateway({instrument_id: [make_fact(instrument_id)]})
        second = make_service(gateway).run(make_request([instrument_id]))
        self.assertEqual(first.report_hash, second.report_hash)
        self.assertEqual(first.snapshot_hash, second.snapshot_hash)

    def test_blocked_reports_differ_by_issue_content(self) -> None:
        instrument_id = uuid4()
        empty = FakeGateway({instrument_id: []})
        first = make_service(empty).run(make_request([instrument_id]))
        incomplete = FakeGateway(
            {
                instrument_id: [
                    make_fact(
                        instrument_id,
                        quality_status=FactQualityStatus.INCOMPLETE,
                    )
                ]
            }
        )
        second = make_service(incomplete).run(make_request([instrument_id]))
        self.assertNotEqual(first.report_hash, second.report_hash)

    def test_request_rejects_non_formal_mode_and_bad_ids(self) -> None:
        from app.instruments.rules import ParseMode

        with self.assertRaises(DomainValidationError):
            make_request([uuid4()]).__class__(
                instrument_ids=[uuid4()],
                start_date=START,
                end_date=END,
                data_cutoff=CUTOFF,
                rule_package_reference=PACKAGE_REF,
                mode=ParseMode.PHASE1_FIXTURE,
            )
        with self.assertRaises(DomainValidationError):
            make_request([])
        with self.assertRaises(DomainValidationError):
            make_request(["not-a-uuid"])


if __name__ == "__main__":
    unittest.main()
