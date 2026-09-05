"""Canonical result-row digest and counter contracts."""

from __future__ import annotations

from decimal import Decimal

from app.backtesting.runner_integrity import (
    canonicalize_jcs,
    compute_result_integrity,
    verify_result_integrity,
)
from app.backtesting.runner_protocol import COVERED_RESULT_TABLES


def _rows() -> dict[str, list[dict[str, object]]]:
    return {table: [] for table in COVERED_RESULT_TABLES}


def test_rfc8785_number_and_key_vectors_are_canonical() -> None:
    value = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
        "literals": [None, True, False],
    }
    assert canonicalize_jcs(value) == (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27]}'
    )


def test_digest_is_stable_for_row_and_mapping_order_but_tracks_facts() -> None:
    config_hash = "a" * 64
    rows = _rows()
    rows["backtest_orders"] = [
        {"order_id": "2", "quantity": Decimal("10")},
        {"quantity": Decimal("20"), "order_id": "1"},
    ]
    reordered = {table: list(values) for table, values in rows.items()}
    reordered["backtest_orders"].reverse()
    first = compute_result_integrity(rows, config_hash=config_hash)
    second = compute_result_integrity(reordered, config_hash=config_hash)
    assert first.valid and first.digest == second.digest
    assert first.counts["orders"] == 2

    changed = {table: list(values) for table, values in rows.items()}
    changed["backtest_orders"][0] = {"order_id": "2", "quantity": Decimal("11")}
    assert compute_result_integrity(changed, config_hash=config_hash).digest != first.digest
    assert compute_result_integrity(rows, config_hash="b" * 64).digest != first.digest


def test_auxiliary_tables_are_excluded_but_missing_result_tables_are_unavailable() -> None:
    config_hash = "c" * 64
    rows = _rows()
    rows["backtest_data_preflight"] = [{"hash": "ignored"}]
    assert compute_result_integrity(rows, config_hash=config_hash).valid
    incomplete = {"backtest_orders": []}
    evidence = compute_result_integrity(incomplete, config_hash=config_hash)
    assert evidence.status == "unavailable"
    assert not evidence.valid


def test_duplicate_business_keys_make_integrity_unprovable() -> None:
    config_hash = "e" * 64
    rows = _rows()
    rows["backtest_orders"] = [
        {"order_id": "duplicate", "quantity": Decimal("10")},
        {"order_id": "duplicate", "quantity": Decimal("11")},
    ]

    evidence = compute_result_integrity(rows, config_hash=config_hash)

    assert not evidence.valid
    assert evidence.status == "failed"
    assert any("duplicate" in error for error in evidence.errors)


def test_duplicate_business_keys_across_table_aliases_are_rejected() -> None:
    config_hash = "f" * 64
    rows = _rows()
    rows["backtest_orders"] = [{"order_id": "same", "quantity": Decimal("10")}]
    rows["orders"] = [{"order_id": "same", "quantity": Decimal("10")}]

    evidence = compute_result_integrity(rows, config_hash=config_hash)

    assert not evidence.valid
    assert evidence.status == "failed"
    assert any("duplicate" in error for error in evidence.errors)


def test_marker_verification_requires_all_counts_and_digest() -> None:
    config_hash = "d" * 64
    rows = _rows()
    evidence = compute_result_integrity(rows, config_hash=config_hash)
    marker = {
        "result_integrity": {"digest": evidence.digest},
        "result_counts": dict(evidence.counts),
    }
    verification = verify_result_integrity(marker, rows, config_hash=config_hash)
    assert verification.valid
    marker["result_counts"]["orders"] = 1
    assert not verify_result_integrity(marker, rows, config_hash=config_hash).valid


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
