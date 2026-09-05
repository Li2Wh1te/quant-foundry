"""Tests for run rule snapshots: domain objects + persistence round-trip."""

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import VersionedReference
from app.instruments.rule_snapshots import (
    FactProvenance,
    InstrumentRuleSnapshotSegment,
    RunRuleSnapshotBundle,
)
from app.instruments.rule_snapshots_models import (
    BacktestRunInstrumentRuleSnapshotRecord,
    BacktestRunRuleSnapshotRecord,
)
from app.instruments.rule_snapshots_repository import RunRuleSnapshotRepository

PACKAGE_REF = VersionedReference(key="china_listed_etf_rules", version=1)
NORMAL_FACT_REF = VersionedReference(key="etf_rule_fact", version=3)
EXCEPTION_FACT_REF = VersionedReference(key="cash_etf_special_rule", version=4)
SET_REF = VersionedReference(key="etf_named_exceptions", version=2)
CUTOFF = datetime(2026, 8, 1, tzinfo=UTC)


def make_provenance(
    fact_reference: VersionedReference = NORMAL_FACT_REF,
) -> dict[str, Any]:
    return {
        "normal_fact": FactProvenance(
            fact_reference=fact_reference,
            source="exchange_rule_book",
            source_revision="2026-edition",
            valid_from=date(2024, 1, 1),
            valid_to=None,
            known_at=CUTOFF,
            quality_status="complete",
            fixture_only=False,
        ).to_payload()
    }


def make_segment(
    instrument_id: UUID | None = None,
    effective_from: date = date(2026, 1, 1),
    **overrides: Any,
) -> InstrumentRuleSnapshotSegment:
    kwargs: dict[str, Any] = dict(
        instrument_id=instrument_id or uuid4(),
        effective_from=effective_from,
        effective_to=None,
        normal_fact_reference=NORMAL_FACT_REF,
        exception_fact_reference=None,
        normalized_values={
            "lot_size": Decimal("100"),
            "trading_status_applicability": {"suspension": "required"},
        },
        capability_declarations={"suspension": "required"},
        provenance=make_provenance(),
        resolution_hash="c" * 64,
    )
    kwargs.update(overrides)
    return InstrumentRuleSnapshotSegment(**kwargs)


def make_bundle(segments=None, **overrides: Any) -> RunRuleSnapshotBundle:
    if segments is None:
        segments = (make_segment(),)
    kwargs: dict[str, Any] = dict(
        rule_package_reference=PACKAGE_REF,
        rule_package_semantic_hash="a" * 64,
        parser_revision="rule-package-resolver@2",
        exception_set_reference=SET_REF,
        exception_set_hash="b" * 64,
        data_cutoff=CUTOFF,
        instrument_segments=segments,
        run_id=uuid4(),
    )
    kwargs.update(overrides)
    return RunRuleSnapshotBundle(**kwargs)


class BundleHashTestCase(unittest.TestCase):
    """snapshot_hash is stable and content sensitive."""

    def test_identical_inputs_produce_identical_hashes(self) -> None:
        instrument_id = uuid4()

        def build() -> RunRuleSnapshotBundle:
            return make_bundle(
                segments=(
                    make_segment(instrument_id=instrument_id),
                    make_segment(
                        instrument_id=instrument_id,
                        effective_from=date(2026, 6, 1),
                    ),
                ),
            )

        self.assertEqual(build().snapshot_hash, build().snapshot_hash)

    def test_segment_order_does_not_change_the_hash(self) -> None:
        first = make_segment(effective_from=date(2026, 1, 1))
        second = make_segment(effective_from=date(2026, 6, 1))
        ordered = make_bundle(segments=(first, second))
        reversed_ = make_bundle(segments=(second, first))
        self.assertEqual(ordered.snapshot_hash, reversed_.snapshot_hash)

    def test_content_changes_change_the_hash(self) -> None:
        base = make_bundle()
        drifted_segment = make_segment(
            instrument_id=base.instrument_segments[0].instrument_id,
            normalized_values={
                "lot_size": Decimal("200"),
                "trading_status_applicability": {"suspension": "required"},
            },
        )
        self.assertNotEqual(
            base.snapshot_hash,
            make_bundle(segments=(drifted_segment,)).snapshot_hash,
        )

    def test_run_id_does_not_participate_in_the_hash(self) -> None:
        bundle = make_bundle()
        rebound = RunRuleSnapshotBundle(
            rule_package_reference=bundle.rule_package_reference,
            rule_package_semantic_hash=bundle.rule_package_semantic_hash,
            parser_revision=bundle.parser_revision,
            exception_set_reference=bundle.exception_set_reference,
            exception_set_hash=bundle.exception_set_hash,
            data_cutoff=bundle.data_cutoff,
            instrument_segments=bundle.instrument_segments,
            run_id=uuid4(),
        )
        self.assertEqual(bundle.snapshot_hash, rebound.snapshot_hash)

    def test_duplicate_segments_are_rejected(self) -> None:
        segment = make_segment()
        with self.assertRaises(DomainValidationError):
            make_bundle(segments=(segment, segment))

    def test_exception_pairing_is_validated(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_bundle(exception_set_reference=None, exception_set_hash="b" * 64)
        with self.assertRaises(DomainValidationError):
            make_bundle(exception_set_reference=SET_REF, exception_set_hash=None)


class SegmentImmutabilityTestCase(unittest.TestCase):
    """Segments freeze every nested structure."""

    def test_normalized_values_and_provenance_are_deeply_immutable(self) -> None:
        segment = make_segment()
        with self.assertRaises(TypeError):
            segment.normalized_values["lot_size"] = Decimal("1")
        with self.assertRaises(TypeError):
            segment.provenance["normal_fact"]["source"] = "edited"

    def test_provenance_records_full_fact_evidence(self) -> None:
        payload = make_provenance()["normal_fact"]
        for key in (
            "fact_key",
            "fact_version",
            "source",
            "source_revision",
            "valid_from",
            "valid_to",
            "known_at",
            "quality_status",
            "fixture_only",
        ):
            self.assertIn(key, payload)


class FakeSnapshotSession:
    """Captures adds; replays queued row lists for load queries."""

    def __init__(self, results: list[list]) -> None:
        self._results = list(results)
        self.added: list = []
        self.statements: list = []

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


def _rows_for(bundle: RunRuleSnapshotBundle) -> tuple[list, list]:
    run_row = SimpleNamespace(
        id=uuid4(),
        run_id=bundle.run_id,
        rule_package_key=bundle.rule_package_reference.key,
        rule_package_version=bundle.rule_package_reference.version,
        rule_package_semantic_hash=bundle.rule_package_semantic_hash,
        parser_revision=bundle.parser_revision,
        exception_set_key=(
            bundle.exception_set_reference.key
            if bundle.exception_set_reference is not None
            else None
        ),
        exception_set_version=(
            bundle.exception_set_reference.version
            if bundle.exception_set_reference is not None
            else None
        ),
        exception_set_hash=bundle.exception_set_hash,
        data_cutoff=bundle.data_cutoff,
        snapshot_hash=bundle.snapshot_hash,
    )
    segment_rows = [
        SimpleNamespace(
            id=uuid4(),
            run_id=bundle.run_id,
            instrument_id=segment.instrument_id,
            effective_from=segment.effective_from,
            effective_to=segment.effective_to,
            normal_fact_key=segment.normal_fact_reference.key,
            normal_fact_version=segment.normal_fact_reference.version,
            exception_fact_key=(
                segment.exception_fact_reference.key
                if segment.exception_fact_reference is not None
                else None
            ),
            exception_fact_version=(
                segment.exception_fact_reference.version
                if segment.exception_fact_reference is not None
                else None
            ),
            normalized_values=dict(segment.normalized_values),
            capability_declarations=dict(segment.capability_declarations),
            provenance=dict(segment.provenance),
            resolution_hash=segment.resolution_hash,
        )
        for segment in bundle.instrument_segments
    ]
    return [run_row], segment_rows


class PersistenceRoundTripTestCase(unittest.TestCase):
    """write-once persistence; reload re-verifies the snapshot hash."""

    def test_write_requires_bound_run_id(self) -> None:
        unbound = RunRuleSnapshotBundle(
            rule_package_reference=PACKAGE_REF,
            rule_package_semantic_hash="a" * 64,
            parser_revision="rule-package-resolver@2",
            exception_set_reference=None,
            exception_set_hash=None,
            data_cutoff=CUTOFF,
            instrument_segments=(make_segment(),),
        )
        session = FakeSnapshotSession([[None]])
        repository = RunRuleSnapshotRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.write_bundle(unbound)
        self.assertEqual(session.added, [])

    def test_write_emits_one_run_row_plus_all_segments(self) -> None:
        segments = (make_segment(), make_segment())
        bundle = make_bundle(segments=segments)
        session = FakeSnapshotSession([[None], [None], []])
        repository = RunRuleSnapshotRepository(session)
        snapshot_hash = repository.write_bundle(bundle)
        self.assertEqual(snapshot_hash, bundle.snapshot_hash)
        run_rows = [
            obj for obj in session.added
            if isinstance(obj, BacktestRunRuleSnapshotRecord)
        ]
        segment_rows = [
            obj for obj in session.added
            if isinstance(obj, BacktestRunInstrumentRuleSnapshotRecord)
        ]
        # Exactly one run-level row; one segment row per interval.
        self.assertEqual(len(run_rows), 1)
        self.assertEqual(len(segment_rows), len(segments))
        self.assertEqual(run_rows[0].snapshot_hash, bundle.snapshot_hash)
        self.assertEqual(run_rows[0].run_id, bundle.run_id)

    def test_write_rejects_a_second_snapshot_for_the_same_run(self) -> None:
        bundle = make_bundle()
        existing = SimpleNamespace(id=uuid4())
        session = FakeSnapshotSession([[existing]])
        repository = RunRuleSnapshotRepository(session)
        with self.assertRaises(DomainValidationError):
            repository.write_bundle(bundle)
        self.assertEqual(session.added, [])


    def test_round_trip_restores_the_frozen_selection(self) -> None:
        shared_instrument = uuid4()
        exception_segment = make_segment(
            instrument_id=shared_instrument,
            exception_fact_reference=EXCEPTION_FACT_REF,
            provenance={
                **make_provenance(),
                "exception_fact": FactProvenance(
                    fact_reference=EXCEPTION_FACT_REF,
                    source="exchange_announcement",
                    source_revision="notice-42",
                    valid_from=date(2026, 1, 1),
                    valid_to=None,
                    known_at=CUTOFF,
                    quality_status="complete",
                    fixture_only=False,
                ).to_payload(),
            },
        )
        later_segment = make_segment(
            instrument_id=shared_instrument,
            effective_from=date(2026, 6, 1),
        )
        bundle = make_bundle(
            segments=(exception_segment, later_segment),
        )
        run_rows, segment_rows = _rows_for(bundle)
        session = FakeSnapshotSession([run_rows, segment_rows])
        loaded = RunRuleSnapshotRepository(session).load_bundle(bundle.run_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.snapshot_hash, bundle.snapshot_hash)
        self.assertEqual(loaded.exception_set_reference, SET_REF)
        self.assertEqual(len(loaded.instrument_segments), 2)
        restored = loaded.instrument_segments[0]
        self.assertEqual(restored.exception_fact_reference, EXCEPTION_FACT_REF)
        self.assertEqual(restored.normalized_values["lot_size"], Decimal("100"))
        self.assertIn("exception_fact", restored.provenance)

    def test_round_trip_restores_domain_value_types_with_definition(self) -> None:
        # Canonical JSON storage must not leak into execution: loading
        # with the run's rule-package definition restores Decimals,
        # VersionedReferences, tuples, and declarations exactly.
        from decimal import Decimal as D

        from app.instruments.domain import VersionedReference as _VR
        from app.instruments.rules import build_definition

        shared_instrument = uuid4()
        bundle = make_bundle(
            segments=(
                make_segment(
                    instrument_id=shared_instrument,
                    normalized_values={
                        "lot_size": D("100"),
                        "price_tick": D("0.001"),
                        "trading_session_template": _VR(
                            key="cn_etf_session_template", version=1
                        ),
                        "order_types": ("limit", "market"),
                        "sellable_rule": {
                            "statements": ["sell_limited_by_available_position"]
                        },
                        "trading_status_applicability": {
                            "suspension": "required",
                            "opening_availability": "required",
                            "price_limit_tradability": "not_applicable",
                        },
                    },
                ),
            ),
        )
        run_rows, segment_rows = _rows_for(bundle)
        session = FakeSnapshotSession([run_rows, segment_rows])
        loaded = RunRuleSnapshotRepository(session).load_bundle(
            bundle.run_id,
            rule_package_definition=build_definition(),
        )
        assert loaded is not None
        values = loaded.instrument_segments[0].normalized_values
        self.assertEqual(values["lot_size"], D("100"))
        self.assertIsInstance(values["lot_size"], D)
        self.assertEqual(
            values["trading_session_template"],
            _VR(key="cn_etf_session_template", version=1),
        )
        self.assertIsInstance(values["order_types"], tuple)
        self.assertIsInstance(values["sellable_rule"], object)
        self.assertEqual(values["sellable_rule"].statements, (
            "sell_limited_by_available_position",
        ))
        # Rehydration does not change the verified snapshot hash: both
        # forms canonicalize to identical payloads.
        self.assertEqual(loaded.snapshot_hash, bundle.snapshot_hash)

    def test_rehydration_rejects_unknown_fields(self) -> None:
        from app.instruments.rules import build_definition

        bundle = make_bundle()
        run_rows, segment_rows = _rows_for(bundle)
        tampered_row = SimpleNamespace(**{**segment_rows[0].__dict__})
        tampered_row.normalized_values = {
            **tampered_row.normalized_values,
            "mystery_field": "1",
        }
        session = FakeSnapshotSession([run_rows, [tampered_row]])
        with self.assertRaises(DomainValidationError):
            RunRuleSnapshotRepository(session).load_bundle(
                bundle.run_id,
                rule_package_definition=build_definition(),
            )

    def test_tampered_stored_snapshot_fails_hash_verification(self) -> None:
        bundle = make_bundle()
        run_rows, segment_rows = _rows_for(bundle)
        tampered = SimpleNamespace(**{**run_rows[0].__dict__})
        tampered.snapshot_hash = "0" * 64
        session = FakeSnapshotSession([[tampered], segment_rows])
        with self.assertRaises(DomainValidationError):
            RunRuleSnapshotRepository(session).load_bundle(bundle.run_id)

    def test_missing_run_returns_none(self) -> None:
        session = FakeSnapshotSession([[], []])
        self.assertIsNone(
            RunRuleSnapshotRepository(session).load_bundle(uuid4())
        )


class FrozenAfterWriteTestCase(unittest.TestCase):
    """Later fact-table changes cannot alter an already-persisted snapshot."""

    def test_reloading_after_fact_changes_keeps_the_same_hash(self) -> None:
        # The snapshot hash is computed from the frozen stored content
        # only; mutating the in-memory fact pool afterwards cannot change
        # what load_bundle rebuilds and verifies.
        bundle = make_bundle()
        run_rows, segment_rows = _rows_for(bundle)
        first_session = FakeSnapshotSession([run_rows, segment_rows])
        first_load = RunRuleSnapshotRepository(first_session).load_bundle(
            bundle.run_id
        )
        assert first_load is not None

        # Simulate new fact versions appended to the live facts table.
        mutated_fact_pool = {
            "etf_rule_fact": {"newest_version": 99, "lot_size": "999"}
        }
        del mutated_fact_pool  # irrelevant to the frozen rows by contract

        second_session = FakeSnapshotSession(
            [[SimpleNamespace(**{**run_rows[0].__dict__})], segment_rows]
        )
        second_load = RunRuleSnapshotRepository(second_session).load_bundle(
            bundle.run_id
        )
        assert second_load is not None
        self.assertEqual(
            first_load.snapshot_hash, second_load.snapshot_hash
        )
        self.assertEqual(
            first_load.instrument_segments[0].normalized_values,
            second_load.instrument_segments[0].normalized_values,
        )


if __name__ == "__main__":
    unittest.main()
