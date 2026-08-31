"""Canonical result-row integrity evidence for the runner boundary.

The existing result repository owns DTO validation and persistence.  This
module only projects already persisted rows into the canonical protocol scope
used by ``completion_marker@1``; it never recalculates a trade, account, or
metric.  A supervisor can therefore verify a worker's claim without changing
domain result semantics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from enum import Enum
from typing import Any, Iterable, Mapping
from uuid import UUID

from .runner_protocol import (
    COVERED_RESULT_TABLES,
    RESULT_COUNT_KEYS,
    RESULT_INTEGRITY_ALGORITHM,
    RESULT_INTEGRITY_CANONICALIZATION,
    RESULT_INTEGRITY_SCOPE,
)


TABLE_TO_COUNT_KEY = {
    "backtest_steps": "steps",
    "backtest_decisions": "decisions",
    "backtest_orders": "orders",
    "backtest_order_updates": "order_updates",
    "backtest_fills": "fills",
    "backtest_positions": "positions",
    "backtest_equity_curve": "equity_points",
    "backtest_metrics": "metrics",
}
_TABLE_ALIASES = {
    **{name: name for name in COVERED_RESULT_TABLES},
    "steps": "backtest_steps",
    "decisions": "backtest_decisions",
    "orders": "backtest_orders",
    "order_updates": "backtest_order_updates",
    "fills": "backtest_fills",
    "positions": "backtest_positions",
    "equity_curve": "backtest_equity_curve",
    "equity_points": "backtest_equity_curve",
    "metrics": "backtest_metrics",
}
_AUXILIARY_TABLES = frozenset(
    {
        "backtest_data_preflight",
        "backtest_data_chunks",
        "backtest_analysis_summaries",
        "data_preflight",
        "data_chunks",
        "analysis_summaries",
    }
)


class ResultIntegrityError(ValueError):
    """Raised when canonical result evidence cannot be produced."""


@dataclass(frozen=True, slots=True)
class IntegrityEvidence:
    """Digest and counts produced by one canonical result projection."""

    valid: bool
    digest: str | None
    counts: Mapping[str, int]
    errors: tuple[str, ...] = ()
    status: str = "passed"
    algorithm: str = RESULT_INTEGRITY_ALGORITHM
    canonicalization: str = RESULT_INTEGRITY_CANONICALIZATION
    scope: str = RESULT_INTEGRITY_SCOPE
    covered_tables: tuple[str, ...] = COVERED_RESULT_TABLES

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", dict(self.counts))
        if not self.valid and self.status == "passed":
            object.__setattr__(self, "status", "failed")

    @property
    def passed(self) -> bool:
        return self.valid and self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "status": self.status,
            "algorithm": self.algorithm,
            "canonicalization": self.canonicalization,
            "scope": self.scope,
            "covered_tables": list(self.covered_tables),
            "digest": self.digest,
            "counts": dict(self.counts),
            "result_counts": dict(self.counts),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class IntegrityVerification:
    """Comparison between a marker claim and freshly read result evidence."""

    valid: bool
    expected_digest: str | None
    actual_digest: str | None
    expected_counts: Mapping[str, int]
    actual_counts: Mapping[str, int]
    errors: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "passed" if self.valid else "failed"

    @property
    def digest(self) -> str | None:
        return self.actual_digest

    @property
    def counts(self) -> Mapping[str, int]:
        return self.actual_counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "status": self.status,
            "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest,
            "expected_counts": dict(self.expected_counts),
            "actual_counts": dict(self.actual_counts),
            "digest": self.actual_digest,
            "counts": dict(self.actual_counts),
            "errors": list(self.errors),
        }


def _jcs_value(value: Any) -> Any:
    """Convert supported result row values to deterministic JSON values."""

    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, Enum):
        return _jcs_value(value.value)
    if isinstance(value, Decimal):
        # Result DTOs use decimal strings at the API boundary.  Keeping the
        # exact coefficient here avoids binary floating-point drift.
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jcs_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jcs_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_jcs_value(item) for item in value]
        return sorted(normalized, key=_canonical_json)
    raise ResultIntegrityError(
        f"result row contains unsupported value type {type(value).__name__}"
    )


def _canonical_json(value: Any) -> str:
    """Serialize the already normalized value using JCS-compatible JSON."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _row_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return {str(key): value for key, value in row.items()}
    if is_dataclass(row):
        return asdict(row)
    model_dump = getattr(row, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="python")
        if isinstance(value, Mapping):
            return dict(value)
    # SQLAlchemy ORM instances expose the mapped column names in ``__table__``
    # and also keep an internal ``_sa_instance_state`` in ``__dict__``.  The
    # explicit column projection avoids accidentally hashing ORM bookkeeping.
    table = getattr(row, "__table__", None)
    columns = getattr(table, "columns", None)
    if columns is not None:
        names = [column.name for column in columns]
        return {name: getattr(row, name) for name in names if hasattr(row, name)}
    values = getattr(row, "__dict__", None)
    if isinstance(values, Mapping):
        return {str(key): value for key, value in values.items() if not str(key).startswith("_")}
    raise ResultIntegrityError(f"result row {type(row).__name__} is not mappable")


def normalize_result_row(row: Any) -> dict[str, Any]:
    """Return one stable, JSON-compatible row projection."""

    source = _row_mapping(row)
    normalized = _jcs_value(source)
    if not isinstance(normalized, dict):  # pragma: no cover - guarded above
        raise ResultIntegrityError("normalized result row must be an object")
    return normalized


def _stable_row_key(row: Mapping[str, Any]) -> str:
    """Sort by the canonical row content, independent of query row order."""

    # The canonical content includes all persisted columns.  Sorting by it is
    # deterministic even for legacy rows that lack one of the newer business
    # identity columns; database primary keys remain part of that content.
    return _canonical_json(row)


def normalize_result_rows(
    rows_by_table: Mapping[str, Iterable[Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Normalize aliases and sort all eight result tables deterministically."""

    if not isinstance(rows_by_table, Mapping):
        raise ResultIntegrityError("rows_by_table must be a mapping")
    normalized: dict[str, list[dict[str, Any]]] = {name: [] for name in COVERED_RESULT_TABLES}
    for raw_name, rows in rows_by_table.items():
        if str(raw_name) in _AUXILIARY_TABLES:
            # These rows are useful execution evidence but deliberately sit
            # outside the canonical eight-table result scope.
            continue
        table = _TABLE_ALIASES.get(str(raw_name))
        if table is None:
            # Data preflight/chunk/analysis rows intentionally sit outside the
            # canonical eight-table result scope and must not silently affect
            # a result digest.
            raise ResultIntegrityError(f"unsupported result integrity table: {raw_name}")
        if rows is None:
            continue
        try:
            values = [normalize_result_row(row) for row in rows]
        except TypeError as exc:
            raise ResultIntegrityError(f"rows for {table} must be iterable") from exc
        normalized[table].extend(values)
    for table in COVERED_RESULT_TABLES:
        normalized[table].sort(key=_stable_row_key)
    return normalized


def result_counts(rows_by_table: Mapping[str, Iterable[Any]]) -> dict[str, int]:
    """Count canonical rows using marker counter names."""

    normalized = normalize_result_rows(rows_by_table)
    return {
        key: len(normalized[table])
        for table, key in TABLE_TO_COUNT_KEY.items()
    }


def canonical_result_payload(
    rows_by_table: Mapping[str, Iterable[Any]],
    *,
    config_hash: str,
) -> dict[str, Any]:
    """Build the exact scope payload hashed by ``compute_result_integrity``."""

    if not isinstance(config_hash, str) or len(config_hash) != 64:
        raise ResultIntegrityError("config_hash must be a 64-character digest")
    if any(character not in "0123456789abcdef" for character in config_hash):
        raise ResultIntegrityError("config_hash must be lowercase hexadecimal")
    normalized = normalize_result_rows(rows_by_table)
    return {
        "scope": RESULT_INTEGRITY_SCOPE,
        "config_hash": config_hash,
        "tables": [
            {"name": table, "rows": normalized[table]}
            for table in COVERED_RESULT_TABLES
        ],
    }


def canonical_result_json(
    rows_by_table: Mapping[str, Iterable[Any]],
    *,
    config_hash: str,
) -> str:
    """Return canonical JSON bytes as text for diagnostic reproducibility."""

    return _canonical_json(canonical_result_payload(rows_by_table, config_hash=config_hash))


def compute_result_integrity(
    rows_by_table: Mapping[str, Iterable[Any]],
    *,
    config_hash: str,
) -> IntegrityEvidence:
    """Recompute the canonical digest and all eight result counters."""

    try:
        payload = canonical_result_payload(rows_by_table, config_hash=config_hash)
        encoded = _canonical_json(payload).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        counts = {
            key: len(payload["tables"][index]["rows"])
            for index, key in enumerate(RESULT_COUNT_KEYS)
        }
        return IntegrityEvidence(True, digest, counts)
    except Exception as exc:
        # A failed re-read is itself evidence of an indeterminate outcome; do
        # not let the supervisor infer a business failure from this platform
        # error.
        return IntegrityEvidence(
            False,
            None,
            {key: 0 for key in RESULT_COUNT_KEYS},
            (f"{type(exc).__name__}: {str(exc)[:300]}",),
            status="unavailable",
        )


recompute_result_integrity = compute_result_integrity
compute_integrity = compute_result_integrity


def verify_result_integrity(
    marker: Mapping[str, Any],
    rows_by_table: Mapping[str, Iterable[Any]],
    *,
    config_hash: str,
) -> IntegrityVerification:
    """Compare a marker claim with newly computed rows and configuration."""

    expected_integrity = marker.get("result_integrity") if isinstance(marker, Mapping) else None
    expected_digest = expected_integrity.get("digest") if isinstance(expected_integrity, Mapping) else None
    expected_counts_raw = marker.get("result_counts") if isinstance(marker, Mapping) else None
    expected_counts = {
        key: expected_counts_raw.get(key)
        for key in RESULT_COUNT_KEYS
    } if isinstance(expected_counts_raw, Mapping) else {}
    evidence = compute_result_integrity(rows_by_table, config_hash=config_hash)
    errors = list(evidence.errors)
    if expected_digest != evidence.digest:
        errors.append("result integrity digest mismatch")
    if expected_counts != dict(evidence.counts):
        errors.append("result integrity counts mismatch")
    return IntegrityVerification(
        evidence.valid and not errors,
        expected_digest,
        evidence.digest,
        expected_counts,
        dict(evidence.counts),
        tuple(errors),
    )


class ResultIntegrityChecker:
    """Small dependency-injection friendly adapter used by a Supervisor."""

    def __init__(self, rows_provider: Any, *, config_hash: str):
        self.rows_provider = rows_provider
        self.config_hash = config_hash

    def read_rows(self) -> Mapping[str, Iterable[Any]]:
        provider = self.rows_provider
        if callable(provider):
            value = provider()
        elif hasattr(provider, "read_integrity_rows"):
            value = provider.read_integrity_rows()
        elif hasattr(provider, "rows_by_table"):
            value = provider.rows_by_table()
        else:
            value = provider
        if not isinstance(value, Mapping):
            raise ResultIntegrityError("integrity rows provider returned a non-mapping")
        return value

    def compute(self) -> IntegrityEvidence:
        try:
            return compute_result_integrity(self.read_rows(), config_hash=self.config_hash)
        except Exception as exc:
            return IntegrityEvidence(
                False,
                None,
                {key: 0 for key in RESULT_COUNT_KEYS},
                (f"{type(exc).__name__}: {str(exc)[:300]}",),
                status="unavailable",
            )

    def verify(self, marker: Mapping[str, Any]) -> IntegrityVerification:
        return verify_result_integrity(marker, self.read_rows(), config_hash=self.config_hash)


__all__ = [
    "COVERED_RESULT_TABLES",
    "IntegrityEvidence",
    "IntegrityVerification",
    "ResultIntegrityChecker",
    "ResultIntegrityError",
    "TABLE_TO_COUNT_KEY",
    "canonical_result_json",
    "canonical_result_payload",
    "compute_integrity",
    "compute_result_integrity",
    "normalize_result_row",
    "normalize_result_rows",
    "recompute_result_integrity",
    "result_counts",
    "verify_result_integrity",
]
