"""Protocol and integrity tests that do not start a worker process."""

from __future__ import annotations

import unittest
from uuid import uuid4

from app.backtesting.runner_integrity import compute_result_integrity, verify_result_integrity
from app.backtesting.runner_protocol import (
    COVERED_RESULT_TABLES,
    RESULT_COUNT_KEYS,
    build_completion_marker,
    evaluate_terminal,
    map_exit_code,
    map_runner_exit_code,
    validate_completion_marker,
)


class RunnerProtocolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = uuid4()
        self.config_hash = "a" * 64
        self.rows = {table: [] for table in COVERED_RESULT_TABLES}
        self.integrity = compute_result_integrity(self.rows, config_hash=self.config_hash)

    def marker(self, category: str = "succeeded") -> dict:
        return build_completion_marker(
            run_id=self.run_id,
            declared_category=category,
            digest=self.integrity.digest,
            result_counts=self.integrity.counts,
            failure_phase=None if category == "succeeded" else "runtime",
            failure_type=None if category == "succeeded" else "WorkerError",
            config_hash=self.config_hash,
        )

    def test_exit_code_protocol_is_one_to_one(self) -> None:
        self.assertEqual(map_exit_code(0), "succeeded")
        self.assertEqual(map_exit_code(10), "failed")
        self.assertEqual(map_exit_code(20), "cancelled")
        self.assertEqual(map_exit_code(30), "timed_out")
        self.assertEqual(map_exit_code(31), "unmapped")
        self.assertEqual(map_exit_code(-9), "unmapped")

    def test_structured_exit_classification_preserves_signal_evidence(self) -> None:
        mapped = map_runner_exit_code(10)
        self.assertEqual(mapped.protocol_version, "runner_exit_code@1")
        self.assertEqual(mapped.category, "failed")
        self.assertTrue(mapped.mapped)
        signalled = map_runner_exit_code(None, signal_number=9)
        self.assertEqual(signalled.category, "unmapped")
        self.assertEqual(signalled.signal_number, 9)
        self.assertFalse(signalled.mapped)

    def test_consistent_success_and_non_success_are_determinate(self) -> None:
        for code, category in ((0, "succeeded"), (10, "failed"), (20, "cancelled"), (30, "timed_out")):
            result = evaluate_terminal(
                marker=self.marker(category),
                exit_code=code,
                integrity=self.integrity,
                run_id=self.run_id,
                config_hash=self.config_hash,
            )
            self.assertEqual(result.status, category)

    def test_conflict_missing_marker_and_integrity_are_indeterminate(self) -> None:
        self.assertEqual(
            evaluate_terminal(
                marker=self.marker("failed"),
                exit_code=0,
                integrity=self.integrity,
                run_id=self.run_id,
                config_hash=self.config_hash,
            ).status,
            "indeterminate",
        )
        self.assertEqual(
            evaluate_terminal(
                marker=None,
                exit_code=0,
                integrity=self.integrity,
                run_id=self.run_id,
                config_hash=self.config_hash,
            ).status,
            "indeterminate",
        )
        self.assertEqual(
            evaluate_terminal(
                marker=self.marker(),
                exit_code=0,
                integrity=False,
                run_id=self.run_id,
                config_hash=self.config_hash,
            ).status,
            "indeterminate",
        )

    def test_marker_requires_all_counters_and_failure_evidence(self) -> None:
        marker = self.marker()
        marker["result_counts"] = {key: 0 for key in RESULT_COUNT_KEYS[:-1]}
        self.assertFalse(validate_completion_marker(marker, run_id=self.run_id))
        failed = self.marker("failed")
        failed["failure_type"] = None
        self.assertFalse(validate_completion_marker(failed, run_id=self.run_id))

    def test_jcs_digest_is_independent_of_row_order_and_mapping_order(self) -> None:
        rows = {table: [] for table in COVERED_RESULT_TABLES}
        rows["backtest_orders"] = [
            {"order_id": "2", "quantity": "10"},
            {"quantity": "20", "order_id": "1"},
        ]
        reversed_rows = {table: list(value) for table, value in rows.items()}
        reversed_rows["backtest_orders"].reverse()
        first = compute_result_integrity(rows, config_hash=self.config_hash)
        second = compute_result_integrity(reversed_rows, config_hash=self.config_hash)
        self.assertEqual(first.digest, second.digest)
        self.assertTrue(
            verify_result_integrity(
                self.marker_from(first), rows, config_hash=self.config_hash
            ).valid
        )

    def marker_from(self, integrity):
        return build_completion_marker(
            run_id=self.run_id,
            declared_category="succeeded",
            digest=integrity.digest,
            result_counts=integrity.counts,
        )


if __name__ == "__main__":
    unittest.main()
