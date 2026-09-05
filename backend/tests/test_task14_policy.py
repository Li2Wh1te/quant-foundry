"""Acceptance checks for task 14-03 policy description and activation gate."""

from __future__ import annotations

import unittest
import copy
from dataclasses import FrozenInstanceError

from app.backtesting.data.adjustment_policy import (
    ADJUSTMENT_SERIES_POLICY_KEY,
    ADJUSTMENT_SERIES_POLICY_VERSION,
    AdjustmentPolicyStatus,
    AdjustmentSeriesPolicy,
    INACTIVE_ADJUSTMENT_POLICY,
    get_registered_adjustment_policy,
    registered_adjustment_policies,
)
from app.backtesting.data.errors import InvalidDataRequestError
from app.backtesting.data.adapters import EtfFactsAdapter
from app.backtesting.data.adjustment_verification import artifact_hashes, load_artifact
from app.strategy_protocol.data_view import AdjustmentPolicyGate


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def verified_artifact(**overrides: object) -> dict[str, object]:
    """Build a JSON-shaped artifact without credentials or live-source calls."""

    artifact: dict[str, object] = {
        "policy": {"key": ADJUSTMENT_SERIES_POLICY_KEY, "version": 1},
        "adapter": {"version": "etf_raw_bar_adapter@1"},
        "source": {"name": "tushare", "batch_id": "batch-202608"},
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
            "summary": "real source rows matched at declared precision",
            "status": "verified",
            "published": True,
            "input_hash": HASH_A,
            "output_hash": HASH_B,
            "evidence_hash": HASH_C,
        },
    }
    artifact.update(overrides)
    return artifact


class Task14PolicyTestCase(unittest.TestCase):
    def test_only_one_registered_inactive_policy_exists(self) -> None:
        policy = get_registered_adjustment_policy()
        self.assertIs(policy, INACTIVE_ADJUSTMENT_POLICY)
        self.assertEqual(policy.policy_key, "tushare_adj_factor_native@1")
        self.assertIs(policy.status, AdjustmentPolicyStatus.INACTIVE)
        self.assertEqual(set(registered_adjustment_policies()), {policy.policy_key})

    def test_unknown_key_or_version_is_rejected(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            AdjustmentSeriesPolicy(key="other", version=1)
        with self.assertRaises(InvalidDataRequestError):
            get_registered_adjustment_policy(version=2)
        with self.assertRaises(InvalidDataRequestError):
            get_registered_adjustment_policy(version=True)  # type: ignore[arg-type]
        with self.assertRaises(InvalidDataRequestError):
            AdjustmentSeriesPolicy(version=1.0)  # type: ignore[arg-type]

    def test_active_requires_published_complete_artifact(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            AdjustmentSeriesPolicy.active(
                qfq_formula="q",
                hfq_formula="h",
                qfq_anchor="q-anchor",
                hfq_anchor="h-anchor",
                precision=6,
                rounding="half-up",
                verification_summary="summary",
                verification_status="verified",
                verification_input_hash=HASH_A,
                verification_output_hash=HASH_B,
                verification_evidence_hash=HASH_C,
                verification_published=False,
            )
        policy = AdjustmentSeriesPolicy.from_verification_artifact(verified_artifact())
        self.assertTrue(policy.is_active())
        self.assertEqual(policy.source, "tushare")
        self.assertEqual(policy.factor_field, "adj_factor")
        self.assertEqual(policy.cutoff_rule, "effective_date <= data_cutoff")
        self.assertEqual(policy.verification_evidence_hash, HASH_C)

    def test_policy_and_serialized_description_are_read_only(self) -> None:
        policy = AdjustmentSeriesPolicy.from_verification_artifact(verified_artifact())
        with self.assertRaises(FrozenInstanceError):
            policy.status = AdjustmentPolicyStatus.INACTIVE  # type: ignore[misc]
        description = policy.as_dict()
        with self.assertRaises(TypeError):
            description["status"] = "inactive"  # type: ignore[index]
        self.assertEqual(description["policy_key"], "tushare_adj_factor_native@1")

    def test_adapter_legacy_boolean_cannot_bypass_policy_evidence(self) -> None:
        kwargs = dict(
            code_mappings=lambda *args, **kwargs: (),
            daily_bars=lambda *args, **kwargs: (),
            adjustment_factors=lambda *args, **kwargs: (),
            trading_days=lambda *args, **kwargs: (),
        )
        with self.assertRaises(InvalidDataRequestError):
            EtfFactsAdapter(
                **kwargs,
                adjustment_active=True,
                adjustment_verification_evidence="verified",
            )

    def test_adapter_accepts_only_an_evidence_backed_policy(self) -> None:
        kwargs = dict(
            code_mappings=lambda *args, **kwargs: (),
            daily_bars=lambda *args, **kwargs: (),
            adjustment_factors=lambda *args, **kwargs: (),
            trading_days=lambda *args, **kwargs: (),
        )
        policy = AdjustmentSeriesPolicy.from_verification_artifact(
            verified_artifact()
        )
        adapter = EtfFactsAdapter(**kwargs, adjustment_policy=policy)
        self.assertTrue(adapter.adjustment_policy.is_active())
        self.assertTrue(adapter.adjustment_active)

    def test_strategy_gate_can_only_be_opened_from_policy(self) -> None:
        policy = AdjustmentSeriesPolicy.from_verification_artifact(
            verified_artifact()
        )
        gate = AdjustmentPolicyGate.from_policy(policy)
        self.assertTrue(gate.is_active())
        self.assertIs(gate.policy, policy)
        with self.assertRaises(AttributeError):
            gate._AdjustmentPolicyGate__active = False  # type: ignore[attr-defined]
        inactive = AdjustmentPolicyGate.from_policy(INACTIVE_ADJUSTMENT_POLICY)
        self.assertFalse(inactive.is_active())

    def test_checked_in_verifier_shape_can_issue_policy_after_native_outputs_pass(self) -> None:
        artifact = copy.deepcopy(load_artifact())
        rows = [
            {
                "ts_code": "513100.SH",
                "trade_date": day,
                "open": "2.300",
                "high": "2.310",
                "low": "2.290",
                "close": "2.305",
            }
            for day in ("2019-09-24", "2019-09-25", "2019-09-26")
        ]
        artifact["output"] = {
            "source_native": {"qfq": rows, "hfq": copy.deepcopy(rows)},
            "adapter": {"qfq": copy.deepcopy(rows), "hfq": copy.deepcopy(rows)},
        }
        artifact["verification"]["status"] = "passed"
        artifact["verification"]["hashes"] = artifact_hashes(artifact).as_dict()
        policy = AdjustmentSeriesPolicy.from_verification_artifact(artifact)
        self.assertTrue(policy.is_active())
        self.assertEqual(policy.adapter_version, "etf_raw_bar_adapter@1")


if __name__ == "__main__":
    unittest.main()
