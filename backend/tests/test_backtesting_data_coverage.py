"""Acceptance tests for the 16A coverage fact and pure aggregation layer."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime
from types import MappingProxyType
from uuid import uuid4

from app.backtesting.data import (
    ContractRef,
    CoverageApplicability,
    DataCapability,
    DataCoverageFact,
    FactEvidence,
    QualityStatus,
    aggregate_coverage,
    canonical_hash,
    evaluate_coverage,
)
from app.backtesting.data.errors import (
    CoverageFactInvalidError,
    ERROR_CODES,
)


RULE = ContractRef(key="bar_validation", version=1)
OTHER_RULE = ContractRef(key="bar_validation", version=2)
INSTRUMENT = uuid4()
OTHER_INSTRUMENT = uuid4()
SESSION_1 = date(2026, 1, 2)
SESSION_2 = date(2026, 1, 5)
SESSION_3 = date(2026, 1, 6)


def evidence(status: QualityStatus = QualityStatus.COMPLETE) -> FactEvidence:
    return FactEvidence(
        source="named-fixture",
        observed_at=datetime(2026, 1, 7, 12, tzinfo=UTC),
        known_at=datetime(2026, 1, 7, 11, tzinfo=UTC),
        quality_status=status,
        source_revision="fixture@1",
    )


def fact(
    session_date: date,
    field: str = "close",
    quality: QualityStatus = QualityStatus.COMPLETE,
    *,
    instrument_id=INSTRUMENT,
    rule=RULE,
    details=None,
    issue_codes=(),
    applicability=CoverageApplicability.REQUIRED,
) -> DataCoverageFact:
    return DataCoverageFact(
        instrument_id=instrument_id,
        session_date=session_date,
        capability=DataCapability.BARS,
        field=field,
        validation_rule=rule,
        applicability=applicability,
        quality_status=quality,
        evidence=(None if quality is QualityStatus.UNAVAILABLE else evidence(quality)),
        details=(
            details
            if details is not None
            else ({"reason": "fixture_invalid"} if quality is QualityStatus.INVALID else {})
        ),
        issue_codes=issue_codes,
    )


class DataCoverageFactTests(unittest.TestCase):
    def test_fact_is_deeply_immutable_and_has_stable_key(self) -> None:
        item = fact(SESSION_1, details={"raw_value": "101.2", "nested": {"b": 2, "a": 1}}, issue_codes=("z_issue", "a_issue"))

        self.assertIsInstance(item.details, MappingProxyType)
        self.assertIsInstance(item.details["nested"], MappingProxyType)
        self.assertEqual(item.issue_codes, ("a_issue", "z_issue"))
        self.assertEqual(item.logical_key[0], INSTRUMENT)
        self.assertEqual(item.logical_key[1], SESSION_1)
        self.assertEqual(item.logical_key[2], DataCapability.BARS)
        self.assertEqual(item.logical_key[-1], (RULE.key, RULE.version))
        self.assertEqual(item.fact_key, item.logical_key)
        with self.assertRaises(TypeError):
            item.details["new"] = "value"  # type: ignore[index]
        with self.assertRaises((AttributeError, TypeError)):
            item.field = "open"  # type: ignore[misc]
        self.assertEqual(json.loads(json.dumps(item.as_dict()))["field"], "close")

    def test_invalid_fact_inputs_are_rejected_with_stable_code(self) -> None:
        base = dict(
            instrument_id=INSTRUMENT,
            session_date=SESSION_1,
            capability=DataCapability.BARS,
            field="close",
            validation_rule=RULE,
            quality_status=QualityStatus.COMPLETE,
            evidence=evidence(),
        )
        cases = (
            {"instrument_id": "not-a-uuid"},
            {"session_date": datetime(2026, 1, 2, tzinfo=UTC)},
            {"field": ""},
            {"validation_rule": "latest"},
            {"quality_status": QualityStatus.COMPLETE, "evidence": None},
            {"applicability": "not_applicable", "validation_rule": None},
            {"details": {"api_token": "must-not-enter-facts"}},
        )
        for override in cases:
            with self.subTest(override=override):
                with self.assertRaises(CoverageFactInvalidError) as caught:
                    DataCoverageFact(**{**base, **override})
                self.assertEqual(caught.exception.code, "coverage_fact_invalid")
                json.dumps(dict(caught.exception.details))

    def test_not_applicable_requires_an_explicit_rule_and_complete_evidence(self) -> None:
        item = fact(
            SESSION_1,
            applicability=CoverageApplicability.NOT_APPLICABLE,
        )
        self.assertEqual(item.applicability, CoverageApplicability.NOT_APPLICABLE)
        self.assertEqual(item.validation_rule, RULE)


class CoverageAggregationTests(unittest.TestCase):
    def test_all_quality_states_and_missing_ranges_use_resolved_sessions(self) -> None:
        rows = [
            fact(SESSION_1, quality=QualityStatus.COMPLETE),
            fact(SESSION_2, quality=QualityStatus.PARTIAL),
            fact(
                SESSION_3,
                quality=QualityStatus.INVALID,
                details={"raw_close": "-1", "failed_rule": "positive_close@1"},
                issue_codes=("bar_invalid",),
            ),
        ]
        report = evaluate_coverage(
            [INSTRUMENT],
            [SESSION_1, SESSION_2, SESSION_3],
            {"close": RULE, "open": RULE},
            rows,
            DataCapability.BARS,
        )
        self.assertEqual(report.expected_count, 6)
        self.assertEqual(report.complete_count, 1)
        self.assertEqual(report.partial_count, 1)
        self.assertEqual(report.invalid_count, 1)
        self.assertEqual(report.unavailable_count, 3)
        self.assertEqual(report.quality_status, QualityStatus.INVALID)
        self.assertEqual(
            [(item.start_date, item.end_date) for item in report.missing_ranges],
            [(SESSION_1, SESSION_3)],
        )
        self.assertIn("coverage_required_field_missing", {item.code for item in report.issues})
        invalid_issue = next(item for item in report.issues if item.code == "coverage_fact_invalid")
        self.assertEqual(invalid_issue.details["fact_details"]["raw_close"], "-1")

    def test_input_order_and_duplicate_equal_facts_do_not_change_hash(self) -> None:
        rows = [fact(SESSION_2), fact(SESSION_1), fact(SESSION_3)]
        forward = aggregate_coverage(
            [INSTRUMENT],
            [SESSION_1, SESSION_2, SESSION_3],
            ["close"],
            rows,
            DataCapability.BARS,
        )
        reverse = aggregate_coverage(
            [INSTRUMENT],
            [SESSION_3, SESSION_1, SESSION_2],
            ["close"],
            [rows[0], rows[2], rows[1], rows[1]],
            DataCapability.BARS,
        )
        self.assertEqual(forward.machine_content(), reverse.machine_content())
        self.assertEqual(canonical_hash(forward.machine_content()), canonical_hash(reverse.machine_content()))
        self.assertEqual(forward.expected_count, 3)
        self.assertEqual(forward.complete_count, 3)

    def test_conflicting_materializations_are_reported_and_invalid(self) -> None:
        conflicting_a = fact(SESSION_1, details={"raw": "1"})
        conflicting_b = fact(SESSION_1, details={"raw": "2"})
        report = evaluate_coverage(
            [INSTRUMENT],
            [SESSION_1],
            ["close"],
            [conflicting_b, conflicting_a],
            DataCapability.BARS,
        )
        self.assertEqual(report.expected_count, 1)
        self.assertEqual(report.invalid_count, 1)
        self.assertEqual(report.quality_status, QualityStatus.INVALID)
        self.assertIn("coverage_fact_conflict", {item.code for item in report.issues})
        self.assertIn("coverage_provider_contract_violation", {item.code for item in report.issues})
        conflict_issue = next(item for item in report.issues if item.code == "coverage_fact_conflict")
        self.assertEqual(conflict_issue.details["instrument_id"], str(INSTRUMENT))
        self.assertEqual(conflict_issue.details["session_date"], SESSION_1.isoformat())
        self.assertEqual(conflict_issue.details["capability"], DataCapability.BARS.value)
        self.assertEqual(conflict_issue.details["field"], "close")
        self.assertEqual(conflict_issue.details["rule"], {"key": RULE.key, "version": RULE.version})
        canonical_hash(conflict_issue.details)

    def test_out_of_scope_fact_is_not_used_to_fill_a_missing_slot(self) -> None:
        report = evaluate_coverage(
            [INSTRUMENT],
            [SESSION_1],
            ["close"],
            [fact(SESSION_1, instrument_id=OTHER_INSTRUMENT)],
            DataCapability.BARS,
        )
        self.assertEqual(report.unavailable_count, 1)
        self.assertEqual(report.quality_status, QualityStatus.INVALID)
        self.assertIn("coverage_provider_contract_violation", {item.code for item in report.issues})

    def test_explicit_not_applicable_is_removed_but_absence_is_unavailable(self) -> None:
        report = evaluate_coverage(
            [INSTRUMENT],
            [SESSION_1],
            ["close", "open"],
            [fact(SESSION_1, applicability=CoverageApplicability.NOT_APPLICABLE)],
            DataCapability.BARS,
        )
        self.assertEqual(report.expected_count, 1)
        self.assertEqual(report.unavailable_count, 1)
        self.assertEqual(report.quality_status, QualityStatus.UNAVAILABLE)

    def test_raw_coverage_does_not_require_an_adjustment_fact(self) -> None:
        report = evaluate_coverage(
            [INSTRUMENT],
            [SESSION_1],
            ["close"],
            [fact(SESSION_1)],
            DataCapability.BARS,
        )
        self.assertEqual(report.quality_status, QualityStatus.COMPLETE)
        self.assertEqual(report.unavailable_count, 0)


class CoverageErrorCodeTests(unittest.TestCase):
    def test_16a_error_codes_are_registered(self) -> None:
        expected = {
            "coverage_fact_invalid",
            "coverage_fact_conflict",
            "coverage_incomplete",
            "coverage_required_field_missing",
            "coverage_provider_contract_violation",
            "internal_preflight_profile_mismatch",
            "internal_preflight_fixture_missing",
            "internal_preflight_fixture_out_of_scope",
            "internal_preflight_degraded_forbidden",
            "data_preflight_report_hash_mismatch",
        }
        self.assertTrue(expected.issubset(ERROR_CODES))


if __name__ == "__main__":
    unittest.main()
