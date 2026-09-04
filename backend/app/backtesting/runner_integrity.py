"""Canonical result-row integrity evidence for the runner boundary.

The existing result repository owns DTO validation and persistence.  This
module only projects already persisted rows into the canonical protocol scope
used by ``completion_marker@1``; it never recalculates a trade, account, or
metric.  A supervisor can therefore verify a worker's claim without changing
domain result semantics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
import hashlib
import json
from enum import Enum
import math
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
    "backtest_events": "events",
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
    "events": "backtest_events",
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

# Result pagination in task 09 defines these as the stable business ordering
# columns.  They are kept here as a read-only projection contract so an ORM
# query order or an input mapping insertion order cannot change the digest.
_TABLE_SORT_COLUMNS = {
    "backtest_steps": ("step_sequence",),
    "backtest_events": ("event_sequence",),
    "backtest_decisions": ("step_sequence", "decision_time", "decision_id"),
    "backtest_orders": ("submitted_at", "order_id"),
    "backtest_order_updates": ("updated_at", "order_id", "update_sequence"),
    "backtest_fills": ("timestamp", "fill_sequence", "fill_id"),
    "backtest_positions": ("as_of", "instrument_id", "side"),
    "backtest_equity_curve": ("as_of", "sequence"),
    "backtest_metrics": ("metric_key", "formula_version"),
}
_TABLE_REQUIRED_IDENTITY_COLUMNS = {
    "backtest_steps": ("step_sequence",),
    "backtest_events": ("event_sequence",),
    "backtest_decisions": ("decision_id",),
    "backtest_orders": ("order_id",),
    "backtest_order_updates": ("order_id", "update_sequence"),
    "backtest_fills": ("fill_id",),
    "backtest_positions": ("as_of", "instrument_id", "side"),
    "backtest_equity_curve": ("sequence",),
    "backtest_metrics": ("metric_key", "formula_version"),
}
_TABLE_INTEGRITY_COLUMNS = {
    "backtest_steps": (
        "run_id", "step_sequence", "time_start", "time_end", "data_cutoff_at",
        "phase", "data_quality",
    ),
    "backtest_events": (
        "run_id", "event_sequence", "step_sequence", "phase_sequence",
        "phase_key", "event_type", "event_time", "payload",
    ),
    "backtest_decisions": (
        "run_id", "decision_id", "step_sequence", "decision_time", "mode",
        "targets", "validation_status", "validation_issues", "duration_ms", "error",
    ),
    "backtest_orders": (
        "run_id", "order_id", "intent_id", "instrument_id", "event_trading_code",
        "event_name", "event_display_name", "side", "order_type", "price",
        "quantity", "filled_quantity", "status", "status_reason", "submitted_at",
    ),
    "backtest_order_updates": (
        "run_id", "order_id", "update_sequence", "old_status", "new_status",
        "updated_at", "reason",
    ),
    "backtest_fills": (
        "run_id", "fill_id", "fill_sequence", "order_id", "instrument_id",
        "event_trading_code", "event_name", "event_display_name", "side", "timestamp",
        "reference_price", "price", "quantity", "fees", "slippage_bps",
        "slippage_amount", "slippage_model_key", "slippage_model_version", "currency",
        "contract_multiplier", "gross_notional", "fee_breakdown", "settlement_calendar_id",
        "settlement_due_session", "settlement_boundary_id",
    ),
    "backtest_positions": (
        "run_id", "as_of", "instrument_id", "event_trading_code", "event_name",
        "event_display_name", "side", "quantity", "available_quantity", "average_price",
        "mark_price", "realized_pnl", "unrealized_pnl",
    ),
    "backtest_equity_curve": (
        "run_id", "sequence", "as_of", "valuation_status", "valuation_reason", "cash",
        "market_value", "equity", "period_return", "total_pnl", "cumulative_return",
        "drawdown", "cumulative_fees",
    ),
    "backtest_metrics": (
        "run_id", "metric_key", "formula_version", "value", "unit", "annualization_factor",
        "risk_free_rate_note", "sample_count", "unavailable_reason", "analyzer_key",
        "analyzer_version", "analyzer_metadata",
    ),
}


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
    status_value: str | None = None
    algorithm: str = RESULT_INTEGRITY_ALGORITHM
    canonicalization: str = RESULT_INTEGRITY_CANONICALIZATION
    scope: str = RESULT_INTEGRITY_SCOPE
    covered_tables: tuple[str, ...] = COVERED_RESULT_TABLES

    @property
    def status(self) -> str:
        if self.status_value is not None:
            return self.status_value
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
            "algorithm": self.algorithm,
            "canonicalization": self.canonicalization,
            "scope": self.scope,
            "covered_tables": list(self.covered_tables),
            "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest,
            "expected_counts": dict(self.expected_counts),
            "actual_counts": dict(self.actual_counts),
            "digest": self.actual_digest,
            "counts": dict(self.actual_counts),
            "errors": list(self.errors),
        }


def _jcs_value(value: Any) -> Any:
    """Convert supported result row values to deterministic JSON values.

    Persisted result DTOs reject binary floating-point values.  Keeping that
    rule at this boundary prevents a caller from silently changing a Decimal
    into a platform-dependent IEEE-754 value while constructing evidence.
    """

    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, Enum):
        return _jcs_value(value.value)
    if isinstance(value, Decimal):
        # Result DTOs use decimal strings at the API boundary.  Keeping the
        # exact coefficient here avoids binary floating-point drift.
        if not value.is_finite():
            raise ResultIntegrityError("NaN and Infinity are not valid result values")
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ResultIntegrityError("naive datetime cannot be canonicalized")
        normalized = value.astimezone(UTC).isoformat(timespec="microseconds")
        return normalized.replace("+00:00", "Z")
    if isinstance(value, (date, time)):
        if isinstance(value, time) and value.tzinfo is not None:
            # A time with an offset is unusual in result JSON, but retaining a
            # normalized UTC representation keeps equivalent values stable.
            value = value.replace(tzinfo=None)
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResultIntegrityError("JSON object keys must be strings")
            normalized[key] = _jcs_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_jcs_value(item) for item in value]
    raise ResultIntegrityError(
        f"result row contains unsupported value type {type(value).__name__}"
    )


def _quote_json_string(value: str) -> str:
    """Quote one JSON string using the shortest standards-compliant form."""

    # ``json.dumps`` is used only for string escaping.  Object member order,
    # number formatting, and separators are emitted by ``_jcs_serialize``.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ResultIntegrityError("unpaired UTF-16 surrogate is not valid JCS")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _jcs_number(value: float | int) -> str:
    """Render an IEEE-754 number with the RFC 8785 exponent thresholds.

    Result rows normally contain Decimal values converted to strings, but a
    public canonicalizer still needs to handle finite JSON numbers.  Python's
    shortest round-trip representation supplies the significant digits; this
    routine applies ECMAScript/JCS notation rules around 1e-6 and 1e21.
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ResultIntegrityError("NaN and Infinity are not valid JCS numbers")
    if value == 0:
        return "0"
    text = repr(value).lower()
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    mantissa, separator, raw_exponent = text.partition("e")
    exponent = int(raw_exponent) if separator else 0
    before, dot, after = mantissa.partition(".")
    raw_digits = before + (after if dot else "")
    # The shortest representation can carry insignificant zeros (for example
    # repr(100.0) == '100.0').  Preserve the numeric value while removing them.
    decimal_exponent = (len(before) + exponent) - len(raw_digits)
    digits = raw_digits.lstrip("0") or "0"
    if digits == "0":
        return "0"
    trailing = len(digits) - len(digits.rstrip("0"))
    if trailing:
        digits = digits[:-trailing]
        decimal_exponent += trailing
    scientific_exponent = decimal_exponent + len(digits) - 1
    if -6 <= scientific_exponent < 21:
        decimal_position = len(digits) + decimal_exponent
        if decimal_position <= 0:
            rendered = "0." + ("0" * -decimal_position) + digits
        elif decimal_position >= len(digits):
            rendered = digits + ("0" * (decimal_position - len(digits)))
        else:
            rendered = digits[:decimal_position] + "." + digits[decimal_position:]
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return sign + rendered
    mantissa_text = digits[0]
    if len(digits) > 1:
        mantissa_text += "." + digits[1:]
    exponent_text = str(scientific_exponent)
    if scientific_exponent >= 0:
        exponent_text = "+" + exponent_text
    return sign + mantissa_text + "e" + exponent_text


def _utf16_sort_key(value: str) -> bytes:
    """Return the RFC 8785 UTF-16 code-unit ordering key."""

    return value.encode("utf-16be", "strict")


def _jcs_serialize(value: Any) -> str:
    """Serialize JSON-compatible values according to RFC 8785/JCS."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _jcs_number(value)
    if isinstance(value, str):
        return _quote_json_string(value)
    if isinstance(value, Mapping):
        members: list[str] = []
        keys: list[str] = []
        for key in value:
            if not isinstance(key, str):
                raise ResultIntegrityError("JSON object keys must be strings")
            keys.append(key)
        for key in sorted(keys, key=_utf16_sort_key):
            members.append(_quote_json_string(key) + ":" + _jcs_serialize(value[key]))
        return "{" + ",".join(members) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs_serialize(item) for item in value) + "]"
    raise ResultIntegrityError(f"value of type {type(value).__name__} is not JSON")


def canonicalize_jcs(value: Any) -> str:
    """Expose the RFC 8785 canonicalizer for protocol vector tests."""

    return _jcs_serialize(value)


def _canonical_json(value: Any) -> str:
    """Serialize an already normalized value using RFC 8785/JCS."""

    return _jcs_serialize(value)


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
    raise ResultIntegrityError(f"result row {type(row).__name__} is not mappable")


def normalize_result_row(row: Any, *, table: str | None = None) -> dict[str, Any]:
    """Return one stable, JSON-compatible row projection.

    Surrogate identifiers and insertion timestamps are intentionally absent
    from ``_TABLE_INTEGRITY_COLUMNS``.  They are storage metadata, not result
    facts, and including them would make an otherwise identical replay hash
    differently merely because rows were inserted in another order.
    """

    source = _row_mapping(row)
    if table is not None:
        if table not in _TABLE_INTEGRITY_COLUMNS:
            raise ResultIntegrityError(f"unsupported result integrity table: {table}")
        source = {
            key: source[key]
            for key in _TABLE_INTEGRITY_COLUMNS[table]
            if key in source
        }
        if not source:
            raise ResultIntegrityError(f"{table} row has no stable result columns")
    normalized = _jcs_value(source)
    if not isinstance(normalized, dict):  # pragma: no cover - guarded above
        raise ResultIntegrityError("normalized result row must be an object")
    return normalized


def _stable_row_key(table: str, row: Mapping[str, Any]) -> tuple[str, str]:
    """Sort by task-09 business keys, then canonical content as a tie-breaker."""

    sort_columns = _TABLE_SORT_COLUMNS[table]
    # Compatibility callers may supply a compact row mapping.  Such rows are
    # still ordered deterministically by their canonical content; full ORM
    # projections use all task-09 business keys before that tie-breaker.
    business_key = tuple(_canonical_json(row[column]) for column in sort_columns if column in row)
    return ("\x1f".join(business_key), _canonical_json(row))


def normalize_result_rows(
    rows_by_table: Mapping[str, Iterable[Any]],
    *,
    require_all_tables: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Normalize aliases and sort all nine result tables deterministically."""

    if not isinstance(rows_by_table, Mapping):
        raise ResultIntegrityError("rows_by_table must be a mapping")
    normalized: dict[str, list[dict[str, Any]]] = {name: [] for name in COVERED_RESULT_TABLES}
    seen_identity_keys_by_table: dict[str, set[tuple[str, ...]]] = {
        name: set() for name in COVERED_RESULT_TABLES
    }
    present_tables: set[str] = set()
    for raw_name, rows in rows_by_table.items():
        if str(raw_name) in _AUXILIARY_TABLES:
            # These rows are useful execution evidence but deliberately sit
            # outside the canonical nine-table result scope.
            continue
        table = _TABLE_ALIASES.get(str(raw_name))
        if table is None:
            # Data preflight/chunk/analysis rows intentionally sit outside the
            # canonical nine-table result scope and must not silently affect
            # a result digest.
            raise ResultIntegrityError(f"unsupported result integrity table: {raw_name}")
        present_tables.add(table)
        if rows is None:
            continue
        try:
            values = [normalize_result_row(row, table=table) for row in rows]
        except TypeError as exc:
            raise ResultIntegrityError(f"rows for {table} must be iterable") from exc
        required_columns = _TABLE_REQUIRED_IDENTITY_COLUMNS[table]
        for value in values:
            if any(column not in value for column in required_columns):
                raise ResultIntegrityError(
                    f"{table} row is missing stable identity/sort columns: "
                    + ", ".join(required_columns)
                )
            # A duplicate business key makes the row order ambiguous and can
            # hide a repository/worker race behind an apparently valid hash.
            # Treat it as unprovable evidence instead of inventing a tie-break
            # based on storage metadata such as ORM ids or insertion times.
            identity_key = tuple(_canonical_json(value[column]) for column in required_columns)
            seen_identity_keys = seen_identity_keys_by_table[table]
            if identity_key in seen_identity_keys:
                raise ResultIntegrityError(
                    f"{table} contains duplicate stable identity key"
                )
            seen_identity_keys.add(identity_key)
        normalized[table].extend(values)
    for table in COVERED_RESULT_TABLES:
        normalized[table].sort(key=lambda row, table=table: _stable_row_key(table, row))
    if require_all_tables:
        # Pre-event integrity callers may omit the newly introduced audit
        # stream; treat that legacy shape as an empty stream. Repository
        # reads always include the key, so a current run cannot accidentally
        # hide event rows behind this compatibility branch.
        missing = [
            table
            for table in COVERED_RESULT_TABLES
            if table not in present_tables and table != "backtest_events"
        ]
        if missing:
            raise ResultIntegrityError(
                "result integrity rows are incomplete; missing tables: "
                + ", ".join(missing)
            )
    return normalized


def result_counts(rows_by_table: Mapping[str, Iterable[Any]]) -> dict[str, int]:
    """Count canonical rows using marker counter names."""

    normalized = normalize_result_rows(rows_by_table, require_all_tables=True)
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
    normalized = normalize_result_rows(rows_by_table, require_all_tables=True)
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
    """Recompute the canonical digest and all nine result counters."""

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
            status=(
                "unavailable"
                if "rows are incomplete" in str(exc)
                else ("failed" if isinstance(exc, ResultIntegrityError) else "unavailable")
            ),
        )


recompute_result_integrity = compute_result_integrity
compute_integrity = compute_result_integrity
compute_result_counts = result_counts


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
    valid = evidence.valid and not errors
    return IntegrityVerification(
        valid,
        expected_digest,
        evidence.digest,
        expected_counts,
        dict(evidence.counts),
        tuple(errors),
        status_value=evidence.status if not valid else "passed",
        algorithm=evidence.algorithm,
        canonicalization=evidence.canonicalization,
        scope=evidence.scope,
        covered_tables=evidence.covered_tables,
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
                status=("failed" if isinstance(exc, ResultIntegrityError) else "unavailable"),
            )

    def verify(self, marker: Mapping[str, Any]) -> IntegrityVerification:
        try:
            return verify_result_integrity(
                marker, self.read_rows(), config_hash=self.config_hash
            )
        except Exception as exc:
            expected_integrity = (
                marker.get("result_integrity") if isinstance(marker, Mapping) else None
            )
            expected_digest = (
                expected_integrity.get("digest")
                if isinstance(expected_integrity, Mapping)
                else None
            )
            expected_raw = marker.get("result_counts") if isinstance(marker, Mapping) else None
            expected_counts = (
                {key: expected_raw.get(key) for key in RESULT_COUNT_KEYS}
                if isinstance(expected_raw, Mapping)
                else {}
            )
            return IntegrityVerification(
                valid=False,
                expected_digest=expected_digest,
                actual_digest=None,
                expected_counts=expected_counts,
                actual_counts={key: 0 for key in RESULT_COUNT_KEYS},
                errors=(f"{type(exc).__name__}: {str(exc)[:300]}",),
                status_value="unavailable",
            )


__all__ = [
    "COVERED_RESULT_TABLES",
    "IntegrityEvidence",
    "IntegrityVerification",
    "ResultIntegrityChecker",
    "ResultIntegrityError",
    "TABLE_TO_COUNT_KEY",
    "canonical_result_json",
    "canonical_result_payload",
    "canonicalize_jcs",
    "compute_integrity",
    "compute_result_counts",
    "compute_result_integrity",
    "normalize_result_row",
    "normalize_result_rows",
    "recompute_result_integrity",
    "result_counts",
    "verify_result_integrity",
]
