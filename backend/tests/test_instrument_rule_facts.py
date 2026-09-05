"""Tests for versioned instrument rule facts: domain contract + persistence."""

import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import VersionedReference
from app.instruments.rule_facts_models import InstrumentRuleFactRecord
from app.instruments.rule_facts_repository import (
    RuleFactVersionExistsError,
    RuleFactsRepository,
)
from app.instruments.rules.contracts import (
    FactQualityStatus,
    RuleFactCandidate,
    rule_fact_content_hash,
)

PACKAGE_REF = VersionedReference(key="china_listed_etf_rules", version=1)
FACT_REF = VersionedReference(key="etf_rule_fact", version=3)
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)
KNOWN_AT = datetime(2025, 12, 1, tzinfo=UTC)


def complete_fields() -> dict[str, Any]:
    """Fixture-only complete v1 field values; never engine defaults."""

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


def make_candidate(**overrides: Any) -> RuleFactCandidate:
    kwargs: dict[str, Any] = dict(
        fact_reference=FACT_REF,
        instrument_id=uuid4(),
        package_reference=PACKAGE_REF,
        source="exchange_rule_book",
        source_revision="2026-edition",
        known_at=KNOWN_AT,
        observed_at=KNOWN_AT,
        quality_status=FactQualityStatus.COMPLETE,
        fixture_only=False,
        content_hash="a" * 64,
        fields=complete_fields(),
        valid_from=date(2024, 1, 1),
        valid_to=None,
    )
    kwargs.update(overrides)
    # Compute the canonical content hash unless the caller supplied one
    # on purpose (e.g., mismatch tests); the repository verifies it.
    if overrides.get("content_hash") is None:
        kwargs["content_hash"] = rule_fact_content_hash(
            fact_reference=kwargs["fact_reference"],
            instrument_id=kwargs["instrument_id"],
            package_reference=kwargs["package_reference"],
            exception_fact_ref=kwargs.get("exception_fact_ref"),
            valid_from=kwargs["valid_from"],
            valid_to=kwargs["valid_to"],
            fields=kwargs["fields"],
            source=kwargs["source"],
            source_revision=kwargs["source_revision"],
            known_at=kwargs["known_at"],
            observed_at=kwargs["observed_at"],
            quality_status=kwargs["quality_status"],
            fixture_only=kwargs["fixture_only"],
        )
    return RuleFactCandidate(**kwargs)


def make_row(candidate: RuleFactCandidate) -> SimpleNamespace:
    """A stand-in ORM row; projection re-validates every field."""

    exception_ref = candidate.exception_fact_ref
    return SimpleNamespace(
        id=uuid4(),
        fact_key=candidate.fact_reference.key,
        fact_version=candidate.fact_reference.version,
        instrument_id=candidate.instrument_id,
        rule_package_key=candidate.package_reference.key,
        rule_package_version=candidate.package_reference.version,
        rule_exception_key=(
            exception_ref.key if exception_ref is not None else None
        ),
        rule_exception_version=(
            exception_ref.version if exception_ref is not None else None
        ),
        valid_from=candidate.valid_from,
        valid_to=candidate.valid_to,
        fields=dict(candidate.fields),
        source=candidate.source,
        source_revision=candidate.source_revision,
        known_at=candidate.known_at,
        observed_at=candidate.observed_at,
        quality_status=candidate.quality_status.value,
        fixture_only=candidate.fixture_only,
        content_hash=candidate.content_hash,
    )


def make_session(rows) -> object:
    scalars = SimpleNamespace(
        first=lambda: rows[0] if rows else None,
        all=lambda: list(rows),
    )
    statements: list = []

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(scalars=lambda: scalars)

    return SimpleNamespace(execute=execute, statements=statements)


class CandidateContractTestCase(unittest.TestCase):
    """fact_reference and content_hash are mandatory identity fields."""

    def test_candidate_requires_fact_reference(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_candidate(fact_reference=None)

    def test_candidate_requires_content_hash(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_candidate(content_hash="")
        with self.assertRaises(DomainValidationError):
            make_candidate(content_hash="   ")

    def test_candidate_requires_a_valid_interval_start(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_candidate(valid_from=None)

    def test_candidate_rejects_malformed_content_hash(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_candidate(content_hash="z" * 64)

    def test_decimal_spellings_share_one_content_hash(self) -> None:
        first = make_candidate()
        fields = complete_fields()
        fields.update(
            {
                "lot_size": "1E+2",
                "price_tick": "0.0010",
                "contract_multiplier": 1,
                "minimum_order_quantity": "100.00",
            }
        )
        equivalent = make_candidate(
            instrument_id=first.instrument_id,
            fact_reference=first.fact_reference,
            fields=fields,
        )
        self.assertEqual(first.content_hash, equivalent.content_hash)

    def test_non_finite_decimal_is_rejected_before_hashing(self) -> None:
        fields = complete_fields()
        fields["price_tick"] = "NaN"
        candidate = make_candidate(fields=fields, content_hash="a" * 64)
        payload = dict(
            fact_reference=candidate.fact_reference,
            instrument_id=candidate.instrument_id,
            package_reference=candidate.package_reference,
            exception_fact_ref=candidate.exception_fact_ref,
            valid_from=candidate.valid_from,
            valid_to=candidate.valid_to,
            fields=dict(candidate.fields),
            source=candidate.source,
            source_revision=candidate.source_revision,
            known_at=candidate.known_at,
            observed_at=candidate.observed_at,
            quality_status=candidate.quality_status,
            fixture_only=candidate.fixture_only,
        )
        with self.assertRaises(DomainValidationError):
            rule_fact_content_hash(**payload)

    def test_content_hash_helper_is_stable_and_content_sensitive(self) -> None:
        first = make_candidate()
        payload = dict(
            fact_reference=first.fact_reference,
            instrument_id=first.instrument_id,
            package_reference=first.package_reference,
            exception_fact_ref=first.exception_fact_ref,
            valid_from=first.valid_from,
            valid_to=first.valid_to,
            fields=dict(first.fields),
            source=first.source,
            source_revision=first.source_revision,
            known_at=first.known_at,
            observed_at=first.observed_at,
            quality_status=first.quality_status,
            fixture_only=first.fixture_only,
        )
        computed = rule_fact_content_hash(**payload)
        self.assertEqual(computed, rule_fact_content_hash(**payload))
        drifted = dict(payload)
        drifted["fields"] = {**payload["fields"], "lot_size": "200"}
        self.assertNotEqual(computed, rule_fact_content_hash(**drifted))


class GetFactTestCase(unittest.TestCase):
    """Exact key/version retrieval with strict PIT visibility."""

    def setUp(self) -> None:
        self.candidate = make_candidate()

    def test_get_fact_filters_by_exact_key_version_and_cutoff(self) -> None:
        session = make_session([make_row(self.candidate)])
        repository = RuleFactsRepository(session)
        loaded = repository.get_fact(
            VersionedReference(key="etf_rule_fact", version=3),
            data_cutoff=CUTOFF,
        )
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.fact_reference, FACT_REF)
        sql = str(
            session.statements[0].compile(dialect=postgresql.dialect())
        )
        self.assertIn("fact_key =", sql)
        self.assertIn("fact_version =", sql)
        self.assertIn("known_at <=", sql)

    def test_missing_row_returns_none_instead_of_latest_fallback(self) -> None:
        # An empty result stays empty: the repository must never pick a
        # different version of the same key as a fallback.
        session = make_session([])
        repository = RuleFactsRepository(session)
        missing = VersionedReference(key="etf_rule_fact", version=99)
        self.assertIsNone(repository.get_fact(missing, data_cutoff=CUTOFF))

    def test_projection_revalidates_stored_rows(self) -> None:
        row = make_row(self.candidate)
        row.fact_version = 0  # corrupted/hand-edited row
        session = make_session([row])
        repository = RuleFactsRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.get_fact(FACT_REF, data_cutoff=CUTOFF)

    def test_projection_rejects_tampered_fields_via_content_hash(self) -> None:
        # Editing stored fields without republishing a new fact version
        # must fail the recomputed content hash at read time.
        row = make_row(self.candidate)
        row.fields = {**row.fields, "lot_size": "999"}
        session = make_session([row])
        repository = RuleFactsRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.get_fact(FACT_REF, data_cutoff=CUTOFF)

    def test_projection_wraps_malformed_storage_values(self) -> None:
        row = make_row(self.candidate)
        row.quality_status = "unknown"
        session = make_session([row])
        repository = RuleFactsRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.get_fact(FACT_REF, data_cutoff=CUTOFF)

        row = make_row(self.candidate)
        row.fields = []
        session = make_session([row])
        repository = RuleFactsRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.get_fact(FACT_REF, data_cutoff=CUTOFF)


class AppendFactTestCase(unittest.TestCase):
    """Append-only semantics with duplicate-version rejection."""

    def test_append_rejects_duplicate_fact_version(self) -> None:
        existing = make_row(make_candidate())
        session = make_session([existing])
        repository = RuleFactsRepository(session)
        with self.assertRaises(RuleFactVersionExistsError):
            repository.append_fact(make_candidate())
        self.assertEqual(len(session.statements), 1)  # pre-check only

    def test_append_rejects_candidate_with_wrong_content_hash(self) -> None:
        session = make_session([])
        repository = RuleFactsRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.append_fact(make_candidate(content_hash="0" * 64))
        self.assertEqual(len(session.statements), 0)

    def test_append_inserts_canonical_decimal_strings(self) -> None:
        added: list = []

        class AddSession(SimpleNamespace):
            pass

        session = make_session([])

        def execute(statement):  # noqa: ANN001
            session.statements.append(statement)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(first=lambda: None)
            )

        session.execute = execute
        session.add = lambda obj: added.append(obj)
        repository = RuleFactsRepository(session)
        returned = repository.append_fact(make_candidate())
        self.assertEqual(returned, FACT_REF)
        self.assertEqual(len(added), 1)
        row = added[0]
        self.assertIsInstance(row, InstrumentRuleFactRecord)
        self.assertEqual(row.fact_key, "etf_rule_fact")
        self.assertEqual(row.fact_version, 3)
        self.assertEqual(row.fields["lot_size"], "100")
        self.assertIsInstance(row.fields["lot_size"], str)
        self.assertIs(row.fixture_only, False)
        self.assertEqual(row.quality_status, "complete")

    def test_append_validates_the_domain_object_first(self) -> None:
        session = make_session([])
        repository = RuleFactsRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.append_fact("not-a-candidate")


class ListFactsTestCase(unittest.TestCase):
    """PIT window queries without latest fallback or silent repair."""

    def setUp(self) -> None:
        self.instrument_id = uuid4()

    def test_query_filters_by_window_package_and_cutoff(self) -> None:
        covering = make_candidate(instrument_id=self.instrument_id)
        session = make_session([make_row(covering)])
        repository = RuleFactsRepository(session)
        facts = repository.list_facts(
            self.instrument_id,
            PACKAGE_REF,
            start_date=date(2024, 1, 1),
            end_date=date(2026, 12, 31),
            data_cutoff=CUTOFF,
        )
        self.assertEqual(len(facts), 1)
        sql = str(
            session.statements[0].compile(dialect=postgresql.dialect())
        )
        self.assertIn("instrument_id =", sql)
        self.assertIn("rule_package_key =", sql)
        self.assertIn("rule_package_version =", sql)
        self.assertIn("valid_from <=", sql)
        self.assertIn("valid_to >", sql)
        self.assertIn("known_at <=", sql)
        self.assertIn("ORDER BY", sql)

    def test_empty_result_is_returned_as_a_gap_not_filled(self) -> None:
        # No rows: the caller sees an empty tuple and must raise its own
        # structured issue; the repository never borrows current facts to
        # fill historical gaps.
        session = make_session([])
        repository = RuleFactsRepository(session)
        facts = repository.list_facts(
            uuid4(),
            PACKAGE_REF,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            data_cutoff=CUTOFF,
        )
        self.assertEqual(facts, ())

    def test_incomplete_quality_rows_are_returned_not_skipped(self) -> None:
        incomplete = make_candidate(
            quality_status=FactQualityStatus.INCOMPLETE,
            instrument_id=self.instrument_id,
        )
        session = make_session([make_row(incomplete)])
        repository = RuleFactsRepository(session)
        facts = repository.list_facts(
            self.instrument_id,
            PACKAGE_REF,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            data_cutoff=CUTOFF,
        )
        self.assertEqual(
            [fact.quality_status.value for fact in facts],
            ["incomplete"],
        )

    def test_overlapping_candidates_are_both_returned(self) -> None:
        # Conflicts stay visible: both equally applicable candidates are
        # handed back so the upper layer raises RULE_FACT_CONFLICT.
        first = make_candidate(instrument_id=self.instrument_id)
        second = make_candidate(
            instrument_id=self.instrument_id,
            fact_reference=VersionedReference(key="etf_rule_fact", version=4),
            source="another_provider",
        )
        session = make_session([make_row(first), make_row(second)])
        repository = RuleFactsRepository(session)
        facts = repository.list_facts(
            self.instrument_id,
            PACKAGE_REF,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            data_cutoff=CUTOFF,
        )
        self.assertEqual({fact.source for fact in facts}, {
            "exchange_rule_book",
            "another_provider",
        })

    def test_window_arguments_are_validated(self) -> None:
        session = make_session([])
        repository = RuleFactsRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.list_facts(
                uuid4(),
                PACKAGE_REF,
                start_date=date(2025, 1, 1),
                end_date=date(2024, 1, 1),
                data_cutoff=CUTOFF,
            )


class ExceptionSourcedProjectionTestCase(unittest.TestCase):
    """Exception-sourced rows round-trip their named-exception reference."""

    def test_exception_reference_round_trips(self) -> None:
        exception_ref = VersionedReference(key="cash_etf_special_rule", version=4)
        candidate = make_candidate(exception_fact_ref=exception_ref)
        session = make_session([make_row(candidate)])
        repository = RuleFactsRepository(session)
        loaded = repository.get_fact(FACT_REF, data_cutoff=CUTOFF)
        assert loaded is not None
        self.assertEqual(loaded.exception_fact_ref, exception_ref)


if __name__ == "__main__":
    unittest.main()
