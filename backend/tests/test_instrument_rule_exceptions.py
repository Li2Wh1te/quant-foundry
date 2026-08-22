"""Tests for named instrument rule exception sets: hash + persistence."""

import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import VersionedReference
from app.instruments.rule_exceptions_models import (
    InstrumentRuleExceptionEntryRecord,
    InstrumentRuleExceptionSetRecord,
)
from app.instruments.rule_exceptions_repository import (
    PersistedExceptionSet,
    RuleExceptionSetContentDriftError,
    RuleExceptionSetVersionExistsError,
    RuleExceptionSetsRepository,
)
from app.instruments.rules.contracts import (
    FactQualityStatus,
    ResolutionStatus,
    RuleExceptionEntry,
    RuleExceptionSetDefinition,
    RulePackageIssueCode,
    exception_set_content_hash,
)

PACKAGE_REF = VersionedReference(key="china_listed_etf_rules", version=1)
SET_REF = VersionedReference(key="etf_named_exceptions", version=2)
EXCEPTION_FACT_REF = VersionedReference(key="cash_etf_special_rule", version=4)
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)


def complete_fact_fields() -> dict[str, Any]:
    """Fixture-only complete v1 field values shared by resolver tests."""

    return {
        "lot_size": "100",
        "quantity_precision": 0,
        "price_precision": 3,
        "price_tick": "0.001",
        "contract_multiplier": "1",
        "trading_session_template": {
            "key": "cn_etf_session_template",
            "version": 1,
        },
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


def make_set(
    *,
    reference: VersionedReference = SET_REF,
    entries=None,
) -> RuleExceptionSetDefinition:
    if entries is None:
        entries = (
            RuleExceptionEntry(
                instrument_id=uuid4(),
                exception_fact_ref=EXCEPTION_FACT_REF,
                valid_from=date(2026, 1, 1),
                valid_to=None,
            ),
        )
    return RuleExceptionSetDefinition(
        reference=reference,
        package_reference=PACKAGE_REF,
        entries=entries,
    )


class FakeSession:
    """Returns queued result lists in order; records SQL statements."""

    def __init__(self, results: list[list]) -> None:
        self._results = list(results)
        self.statements: list = []
        self.added: list = []

    def execute(self, statement):
        self.statements.append(statement)
        rows = self._results.pop(0) if self._results else []
        scalars = SimpleNamespace(
            first=lambda: rows[0] if rows else None,
            all=lambda: list(rows),
        )
        return SimpleNamespace(scalars=lambda: scalars)

    def add(self, obj):
        self.added.append(obj)


def set_row(definition: RuleExceptionSetDefinition) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        set_key=definition.reference.key,
        set_version=definition.reference.version,
        rule_package_key=definition.package_reference.key,
        rule_package_version=definition.package_reference.version,
        source="exchange_announcement",
        source_revision="notice-42",
        known_at=datetime(2025, 12, 1, tzinfo=UTC),
        observed_at=datetime(2025, 12, 1, tzinfo=UTC),
        quality_status="complete",
        fixture_only=False,
        content_hash=exception_set_content_hash(definition),
    )


def entry_rows(definition: RuleExceptionSetDefinition) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            id=uuid4(),
            set_key=definition.reference.key,
            set_version=definition.reference.version,
            instrument_id=entry.instrument_id,
            exception_fact_key=entry.exception_fact_ref.key,
            exception_fact_version=entry.exception_fact_ref.version,
            valid_from=entry.valid_from,
            valid_to=entry.valid_to,
        )
        for entry in definition.entries
    ]


class ContentHashTestCase(unittest.TestCase):
    """The set hash must be stable under entry input order changes."""

    def test_entry_input_order_does_not_change_the_hash(self) -> None:
        first_instrument = uuid4()
        second_instrument = uuid4()
        entries_a = (
            RuleExceptionEntry(
                instrument_id=first_instrument,
                exception_fact_ref=EXCEPTION_FACT_REF,
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 6, 30),
            ),
            RuleExceptionEntry(
                instrument_id=second_instrument,
                exception_fact_ref=EXCEPTION_FACT_REF,
                valid_from=date(2026, 3, 1),
                valid_to=None,
            ),
        )
        # Same two entries, opposite insertion order.
        entries_b = tuple(reversed(entries_a))
        self.assertEqual(
            exception_set_content_hash(make_set(entries=entries_a)),
            exception_set_content_hash(make_set(entries=entries_b)),
        )

    def test_hash_is_content_sensitive(self) -> None:
        base = make_set()
        changed_entry = RuleExceptionEntry(
            instrument_id=base.entries[0].instrument_id,
            exception_fact_ref=VersionedReference(
                key="cash_etf_special_rule", version=5
            ),
            valid_from=base.entries[0].valid_from,
            valid_to=None,
        )
        self.assertNotEqual(
            exception_set_content_hash(base),
            exception_set_content_hash(make_set(entries=(changed_entry,))),
        )


class AppendAndLoadTestCase(unittest.TestCase):
    """Exact key/version persistence without latest fallback."""

    def test_append_writes_set_and_entries_with_canonical_hash(self) -> None:
        definition = make_set()
        session = FakeSession([[], []])
        repository = RuleExceptionSetsRepository(session)
        returned = repository.append_exception_set(
            definition,
            source="exchange_announcement",
            known_at=datetime(2025, 12, 1, tzinfo=UTC),
            observed_at=datetime(2025, 12, 1, tzinfo=UTC),
        )
        self.assertEqual(returned, SET_REF)
        sets = [obj for obj in session.added if isinstance(
            obj, InstrumentRuleExceptionSetRecord)]
        entries = [obj for obj in session.added if isinstance(
            obj, InstrumentRuleExceptionEntryRecord)]
        self.assertEqual(len(sets), 1)
        self.assertEqual(len(entries), len(definition.entries))
        self.assertEqual(
            sets[0].content_hash, exception_set_content_hash(definition)
        )

    def test_append_rejects_duplicate_set_version(self) -> None:
        definition = make_set()
        session = FakeSession([[uuid4()]])  # pre-check finds an existing row
        repository = RuleExceptionSetsRepository(session)
        with self.assertRaises(RuleExceptionSetVersionExistsError):
            repository.append_exception_set(
                definition,
                source="exchange_announcement",
                known_at=datetime(2025, 12, 1, tzinfo=UTC),
                observed_at=datetime(2025, 12, 1, tzinfo=UTC),
            )

    def _load_session(self, definition: RuleExceptionSetDefinition) -> FakeSession:
        return FakeSession([[set_row(definition)], entry_rows(definition)])

    def test_load_returns_exact_version_with_provenance(self) -> None:
        definition = make_set()
        session = self._load_session(definition)
        repository = RuleExceptionSetsRepository(session)
        loaded = repository.load_exception_set(SET_REF, data_cutoff=CUTOFF)
        self.assertIsInstance(loaded, PersistedExceptionSet)
        assert loaded is not None
        self.assertEqual(loaded.definition, definition)
        self.assertEqual(loaded.source, "exchange_announcement")
        sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
        self.assertIn("set_key =", sql)
        self.assertIn("set_version =", sql)
        self.assertIn("known_at <=", sql)

    def test_missing_version_returns_none_not_latest(self) -> None:
        session = FakeSession([[], []])
        repository = RuleExceptionSetsRepository(session)
        missing = VersionedReference(key="etf_named_exceptions", version=99)
        self.assertIsNone(
            repository.load_exception_set(missing, data_cutoff=CUTOFF)
        )

    def test_edited_entries_fail_the_content_hash_check(self) -> None:
        definition = make_set()
        row = set_row(definition)
        rows = entry_rows(definition)
        # Simulate an in-place edit of an entry's validity window.
        tampered = SimpleNamespace(**{**rows[0].__dict__})
        tampered.valid_from = date(2020, 1, 1)
        session = FakeSession([[row], [tampered]])
        repository = RuleExceptionSetsRepository(session)
        with self.assertRaises(RuleExceptionSetContentDriftError):
            repository.load_exception_set(SET_REF, data_cutoff=CUTOFF)

    def test_load_validates_stored_rows_through_the_domain(self) -> None:
        definition = make_set()
        row = set_row(definition)
        bad_row = SimpleNamespace(**{**entry_rows(definition)[0].__dict__})
        bad_row.exception_fact_version = 0  # corrupted version
        session = FakeSession([[row], [bad_row]])
        repository = RuleExceptionSetsRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.load_exception_set(SET_REF, data_cutoff=CUTOFF)


class ExceptionFactCompletenessTestCase(unittest.TestCase):
    """An exception fact must be complete on its own — no implicit fill."""

    def _resolve_with_exception_fields(self, fields: dict) -> Any:
        from datetime import date as date_cls, datetime as dt_cls, timezone
        from uuid import uuid4 as new_id

        from app.instruments.rules import (
            ParseMode,
            RuleFactCandidate,
            RulePackageRegistry,
            RulePackageResolver,
            register_china_listed_etf_rules,
        )

        instrument_id = new_id()
        normal_ref = VersionedReference(key="etf_rule_fact", version=1)
        normal = RuleFactCandidate(
            fact_reference=normal_ref,
            instrument_id=instrument_id,
            package_reference=PACKAGE_REF,
            source="exchange_rule_book",
            source_revision="rev-1",
            known_at=dt_cls(2025, 12, 1, tzinfo=timezone.utc),
            observed_at=dt_cls(2025, 12, 1, tzinfo=timezone.utc),
            quality_status=FactQualityStatus.COMPLETE,
            fixture_only=False,
            content_hash="a" * 64,
            fields=complete_fact_fields(),
            valid_from=date_cls(2024, 1, 1),
            valid_to=None,
        )
        exception_ref = VersionedReference(key="cash_etf_special_rule", version=4)
        exception = RuleFactCandidate(
            fact_reference=exception_ref,
            instrument_id=instrument_id,
            package_reference=PACKAGE_REF,
            source="exchange_announcement",
            source_revision="notice-42",
            known_at=dt_cls(2025, 12, 1, tzinfo=timezone.utc),
            observed_at=dt_cls(2025, 12, 1, tzinfo=timezone.utc),
            quality_status=FactQualityStatus.COMPLETE,
            fixture_only=False,
            content_hash="b" * 64,
            fields=fields,
            exception_fact_ref=exception_ref,
            valid_from=date_cls(2024, 1, 1),
            valid_to=None,
        )
        entry = RuleExceptionEntry(
            instrument_id=instrument_id,
            exception_fact_ref=exception_ref,
            valid_from=date_cls(2025, 1, 1),
            valid_to=None,
        )
        definition = RuleExceptionSetDefinition(
            reference=SET_REF,
            package_reference=PACKAGE_REF,
            entries=(entry,),
        )
        registry = RulePackageRegistry()
        register_china_listed_etf_rules(registry)
        resolver = RulePackageResolver(registry)
        return resolver.resolve(
            PACKAGE_REF,
            instrument_id=instrument_id,
            asset_class="etf",
            effective_date=dt_cls(2026, 8, 22).date(),
            data_cutoff=dt_cls(2026, 8, 22, tzinfo=timezone.utc),
            facts=[normal, exception],
            exception_sets=[definition],
            mode=ParseMode.FORMAL,
        )

    def test_exception_missing_required_field_blocks_despite_normal_fact(
        self,
    ) -> None:
        # The exception fact omits price_tick; the ordinary fact carries a
        # perfectly good value, but implicit backfill is forbidden.
        incomplete_exception = {
            key: value
            for key, value in complete_fact_fields().items()
            if key != "price_tick"
        }
        resolution = self._resolve_with_exception_fields(
            incomplete_exception
        )
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_REQUIRED_FIELD_MISSING.value,
            {issue.code.value for issue in resolution.issues},
        )
        self.assertTrue(
            any(
                issue.field == "price_tick"
                and "禁止从普通事实隐式补齐" in issue.message
                for issue in resolution.issues
            ),
            resolution.issues,
        )
        self.assertEqual(resolution.normalized_values, {})

    def test_complete_exception_fact_resolves_ready(self) -> None:
        resolution = self._resolve_with_exception_fields(
            complete_fact_fields()
        )
        self.assertIs(resolution.status, ResolutionStatus.READY)


class SchemaShapeTestCase(unittest.TestCase):
    """Entries route references only — production values have no columns."""

    def test_entry_table_has_no_production_value_columns(self) -> None:
        forbidden = {
            "lot_size",
            "price_tick",
            "price_precision",
            "quantity_precision",
            "currency",
            "trading_session_template",
            "settlement_rule_class",
        }
        columns = {
            column.name for column in InstrumentRuleExceptionEntryRecord.__table__.columns
        }
        self.assertFalse(forbidden & columns)
        expected = {
            "id",
            "set_key",
            "set_version",
            "instrument_id",
            "exception_fact_key",
            "exception_fact_version",
            "valid_from",
            "valid_to",
        }
        self.assertEqual(columns, expected)

    def test_set_lookup_requires_both_key_and_version(self) -> None:
        with self.assertRaises(DomainValidationError):
            VersionedReference(key="etf_named_exceptions", version=0)


if __name__ == "__main__":
    unittest.main()
