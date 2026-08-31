"""Focused completion-marker and runner-exit protocol contracts."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.backtesting.runner_integrity import compute_result_integrity
from app.backtesting.runner_protocol import (
    COVERED_RESULT_TABLES,
    RESULT_COUNT_KEYS,
    CompletionMarker,
    build_completion_marker,
    map_runner_exit_code,
    validate_completion_marker,
)


def _integrity(config_hash: str):
    return compute_result_integrity(
        {table: [] for table in COVERED_RESULT_TABLES}, config_hash=config_hash
    )


def test_exit_protocol_preserves_unknown_and_signal_evidence() -> None:
    assert map_runner_exit_code(0).as_dict() == {
        "protocol_version": "runner_exit_code@1",
        "raw_exit_code": 0,
        "signal_number": None,
        "category": "succeeded",
        "mapped": True,
        "reason": "mapped_exit_code",
    }
    assert map_runner_exit_code(10).category == "failed"
    assert map_runner_exit_code(20).category == "cancelled"
    assert map_runner_exit_code(30).category == "timed_out"
    unknown = map_runner_exit_code(31)
    assert unknown.category == "unmapped" and not unknown.mapped
    signalled = map_runner_exit_code(-9)
    assert signalled.category == "unmapped"
    assert signalled.signal_number == 9


def test_marker_validation_is_structured_and_fail_closed() -> None:
    run_id = uuid4()
    config_hash = "a" * 64
    evidence = _integrity(config_hash)
    marker = build_completion_marker(
        run_id=run_id,
        declared_category="succeeded",
        digest=evidence.digest,
        result_counts=evidence.counts,
        config_hash=config_hash,
    )
    parsed = CompletionMarker.from_mapping(
        marker, run_id=run_id, config_hash=config_hash
    )
    assert parsed.run_id == run_id
    assert validate_completion_marker(
        marker, run_id=run_id, config_hash=config_hash
    ).as_dict()["errors"] == []

    malformed = dict(marker)
    malformed["unknown"] = True
    malformed["result_integrity"] = dict(marker["result_integrity"])
    malformed["result_integrity"]["extra"] = 1
    malformed["result_counts"] = {
        key: (False if key == RESULT_COUNT_KEYS[0] else 0)
        for key in RESULT_COUNT_KEYS
    }
    result = validate_completion_marker(malformed, run_id=run_id)
    assert not result.valid
    assert not result.integrity_shape_valid
    assert not result.counts_valid
    assert result.errors


def test_non_success_marker_requires_both_failure_fields() -> None:
    run_id = uuid4()
    evidence = _integrity("b" * 64)
    marker = build_completion_marker(
        run_id=run_id,
        declared_category="failed",
        digest=evidence.digest,
        result_counts=evidence.counts,
        failure_phase="runtime",
        failure_type="WorkerError",
    )
    marker["failure_type"] = ""
    validation = validate_completion_marker(marker, run_id=run_id)
    assert not validation.valid
    assert not validation.failure_fields_valid


def test_success_marker_requires_explicit_null_failure_fields() -> None:
    run_id = uuid4()
    config_hash = "c" * 64
    evidence = _integrity(config_hash)
    marker = build_completion_marker(
        run_id=run_id,
        declared_category="succeeded",
        digest=evidence.digest,
        result_counts=evidence.counts,
    )
    marker.pop("failure_phase")

    validation = validate_completion_marker(marker, run_id=run_id)

    assert not validation.valid
    assert any("required fields" in error for error in validation.errors)


if __name__ == "__main__":
    pytest.main([__file__])
