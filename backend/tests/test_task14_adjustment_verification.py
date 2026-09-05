"""Acceptance checks for task 14-02 real-source verification artifacts."""

from __future__ import annotations

import copy
import json
import unittest

from app.backtesting.data.adjustment_verification import (
    DEFAULT_ARTIFACT_PATH,
    VerificationHashes,
    artifact_hashes,
    build_artifact,
    load_artifact,
    verify_artifact,
    verify_artifact_file,
)


def _native_rows() -> list[dict[str, str]]:
    return [
        {
            "ts_code": "513100.SH",
            "trade_date": "2019-09-24",
            "open": "2.300",
            "high": "2.310",
            "low": "2.290",
            "close": "2.305",
        },
        {
            "ts_code": "513100.SH",
            "trade_date": "2019-09-25",
            "open": "2.310",
            "high": "2.320",
            "low": "2.300",
            "close": "2.315",
        },
        {
            "ts_code": "513100.SH",
            "trade_date": "2019-09-26",
            "open": "2.320",
            "high": "2.330",
            "low": "2.310",
            "close": "2.325",
        },
    ]


class RealSourceArtifactTestCase(unittest.TestCase):
    def _builder_kwargs(self) -> dict[str, object]:
        factors = [
            {
                "ts_code": "513100.SH",
                "trade_date": day,
                "adj_factor": "1.000000000000",
            }
            for day in ("2019-09-24", "2019-09-25", "2019-09-26")
        ]
        rows = _native_rows()
        return {
            "factor_rows": factors,
            "source_native": {"qfq": rows, "hfq": copy.deepcopy(rows)},
            "adapter": {"qfq": copy.deepcopy(rows), "hfq": copy.deepcopy(rows)},
            "cutoff_cases": [
                {
                    "data_cutoff": "2019-09-24T15:00:00+08:00",
                    "visible_effective_dates": ["2019-09-24"],
                },
                {
                    "data_cutoff": "2019-09-26T15:00:00+08:00",
                    "visible_effective_dates": [
                        "2019-09-24",
                        "2019-09-25",
                        "2019-09-26",
                    ],
                },
            ],
            "boundary_effective_date": "2019-09-25",
            "adapter_version": "etf_raw_bar_adapter@1",
            "source_batch": "test-batch",
            "mapping": {
                "source_code_field": "ts_code",
                "source_date_field": "trade_date",
                "factor_field": "adj_factor",
                "effective_date": "trade_date",
                "qfq_fields": {
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                },
                "hfq_fields": {
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                },
            },
            "semantics": {
                "qfq_formula": "tushare.pro_bar.native.qfq",
                "hfq_formula": "tushare.pro_bar.native.hfq",
                "qfq_anchor": "native end_date latest visible factor",
                "hfq_anchor": "native factor at each effective date",
                "precision": {
                    "price_decimal_places": 3,
                    "factor_decimal_places": 12,
                },
                "rounding": "source native decimal output; no local rounding",
                "cutoff_rule": "effective_date <= data_cutoff",
            },
        }

    def test_build_artifact_marks_complete_capture_as_passed(self) -> None:
        artifact = build_artifact(**self._builder_kwargs())
        self.assertEqual(artifact["verification"]["status"], "passed")
        result = verify_artifact(artifact)
        self.assertTrue(result.passed)

    def test_checked_in_artifact_is_real_source_and_fails_closed_without_native_rows(self) -> None:
        artifact = load_artifact()
        self.assertEqual(artifact["source"]["capture_mode"], "real_source")
        self.assertEqual(artifact["input"]["factor_rows"][0]["ts_code"], "513100.SH")
        self.assertEqual(len(artifact["input"]["factor_rows"]), 3)
        self.assertEqual(
            artifact["verification"]["hashes"], artifact_hashes(artifact).as_dict()
        )
        result = verify_artifact_file()
        self.assertFalse(result.passed)
        self.assertEqual(result.status, "failed")
        self.assertIn("source-native and adapter qfq outputs", result.errors[0])

    def test_native_outputs_are_compared_for_both_bases(self) -> None:
        artifact = copy.deepcopy(load_artifact())
        rows = _native_rows()
        artifact["output"] = {
            "source_native": {"qfq": rows, "hfq": copy.deepcopy(rows)},
            "adapter": {"qfq": copy.deepcopy(rows), "hfq": copy.deepcopy(rows)},
        }
        artifact["verification"]["status"] = "passed"
        artifact["verification"]["hashes"] = artifact_hashes(artifact).as_dict()
        result = verify_artifact(artifact)
        self.assertTrue(result.passed)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.hashes, artifact_hashes(artifact))
        self.assertIsInstance(result.hashes, VerificationHashes)

    def test_native_outputs_must_cover_every_factor_date(self) -> None:
        artifact = copy.deepcopy(load_artifact())
        rows = _native_rows()
        artifact["output"] = {
            "source_native": {"qfq": rows[:2], "hfq": copy.deepcopy(rows)},
            "adapter": {"qfq": rows[:2], "hfq": copy.deepcopy(rows)},
        }
        artifact["verification"]["status"] = "passed"
        artifact["verification"]["hashes"] = artifact_hashes(artifact).as_dict()
        result = verify_artifact(artifact)
        self.assertFalse(result.passed)
        self.assertIn("every factor row", result.errors[0])

    def test_precision_mismatch_fails_even_when_hashes_are_recomputed(self) -> None:
        artifact = copy.deepcopy(load_artifact())
        rows = _native_rows()
        adapter_rows = copy.deepcopy(rows)
        adapter_rows[1]["close"] = "2.327"
        artifact["output"] = {
            "source_native": {"qfq": rows, "hfq": copy.deepcopy(rows)},
            "adapter": {"qfq": adapter_rows, "hfq": copy.deepcopy(rows)},
        }
        artifact["verification"]["status"] = "passed"
        artifact["verification"]["hashes"] = artifact_hashes(artifact).as_dict()
        result = verify_artifact(artifact)
        self.assertFalse(result.passed)
        self.assertIn("qfq", result.errors[0])

    def test_stored_hash_tampering_fails(self) -> None:
        artifact = copy.deepcopy(load_artifact())
        rows = _native_rows()
        artifact["output"] = {
            "source_native": {"qfq": rows, "hfq": copy.deepcopy(rows)},
            "adapter": {"qfq": copy.deepcopy(rows), "hfq": copy.deepcopy(rows)},
        }
        artifact["verification"]["status"] = "passed"
        artifact["verification"]["hashes"] = artifact_hashes(artifact).as_dict()
        artifact["verification"]["hashes"]["input_hash"] = "0" * 64
        result = verify_artifact(artifact, require_hashes=True)
        self.assertFalse(result.passed)
        self.assertIn("input_hash", result.errors[0])

    def test_artifact_path_is_versioned_and_contains_no_credential_key(self) -> None:
        self.assertTrue(DEFAULT_ARTIFACT_PATH.name.endswith("@1.json"))
        payload = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("token", json.dumps(payload, ensure_ascii=False).lower())

    def test_authorization_shaped_artifact_fields_fail_closed(self) -> None:
        artifact = copy.deepcopy(load_artifact())
        artifact["source"]["authorization"] = "Bearer redacted-test-value"
        result = verify_artifact(artifact)
        self.assertFalse(result.passed)
        self.assertIn("credential-shaped", result.errors[0])


if __name__ == "__main__":  # pragma: no cover - unittest discovery is canonical
    unittest.main()
