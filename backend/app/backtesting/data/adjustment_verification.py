"""Reproducible real-source verification for ETF adjustment semantics.

The ETF adjustment table stores factors, but a factor value does not by
itself prove how a provider's native ``qfq`` and ``hfq`` series are produced.
This module keeps that proof as a small, versioned JSON artifact.  It is
deliberately independent from the backtest adapter: verification must compare
captured provider output with adapter output, never derive a missing formula
from bars, corporate actions, or an empirical guess.

The checked-in artifact is allowed to be ``failed``.  That is the safe state
when the provider has not supplied both native ETF outputs; callers must use
``assert_verified`` before activating a policy.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.backtesting.data.reports import canonical_hash

__all__ = [
    "ADJUSTMENT_POLICY_KEY",
    "ADJUSTMENT_POLICY_VERSION",
    "ADJUSTMENT_POLICY_REF",
    "ARTIFACT_SCHEMA_VERSION",
    "DEFAULT_ARTIFACT_PATH",
    "VerificationError",
    "VerificationHashes",
    "VerificationResult",
    "artifact_hashes",
    "assert_verified",
    "build_artifact",
    "load_artifact",
    "verify_artifact",
    "verify_artifact_file",
]


ADJUSTMENT_POLICY_KEY = "tushare_adj_factor_native"
ADJUSTMENT_POLICY_VERSION = 1
ADJUSTMENT_POLICY_REF = (
    f"{ADJUSTMENT_POLICY_KEY}@{ADJUSTMENT_POLICY_VERSION}"
)
ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_ARTIFACT_PATH = (
    Path(__file__).resolve().parent
    / "verification_artifacts"
    / f"{ADJUSTMENT_POLICY_REF}.json"
)

_PRICE_FIELDS = ("open", "high", "low", "close")
_VOLATILE_KEYS = frozenset(
    {
        "captured_at",
        "created_at",
        "generated_at",
        "generation_time",
        "retrieved_at",
        "verified_at",
    }
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:access[_-]?token|api[_-]?key|authorization|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:token|password|secret|api[_-]?key|authorization)\s*[:=]",
    re.IGNORECASE,
)


class VerificationError(ValueError):
    """Raised when a verification artifact cannot be trusted."""


@dataclass(frozen=True, slots=True)
class VerificationHashes:
    """The three stable SHA-256 digests carried by an artifact."""

    input_hash: str
    output_hash: str
    evidence_hash: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-native representation for policy evidence."""

        return {
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Immutable result of validating one real-source artifact."""

    status: str
    passed: bool
    errors: tuple[str, ...]
    checks: tuple[str, ...]
    hashes: VerificationHashes
    artifact_id: str | None = None

    @property
    def evidence_hash(self) -> str:
        """Expose the evidence digest for policy and run-record callers."""

        return self.hashes.evidence_hash

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-native result without credentials or timestamps."""

        return {
            "status": self.status,
            "passed": self.passed,
            "errors": list(self.errors),
            "checks": list(self.checks),
            "hashes": self.hashes.as_dict(),
            "artifact_id": self.artifact_id,
        }


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{field} must be non-blank text")
    return value.strip()


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationError(f"{field} must be an integer")
    return value


def _decimal(value: object, field: str) -> Decimal:
    # JSON artifacts use strings for decimal values.  Accepting binary floats
    # here would make a successful verification depend on serialization noise.
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(
        value, (Decimal, int, str)
    ):
        raise VerificationError(f"{field} must be a decimal string or integer")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise VerificationError(f"{field} is not a valid decimal") from exc
    if not result.is_finite():
        raise VerificationError(f"{field} must be finite")
    return result


def _day(value: object, field: str) -> date:
    if isinstance(value, datetime):
        raise VerificationError(f"{field} must be a date, not a datetime")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise VerificationError(f"{field} must be YYYYMMDD or ISO date text")
    raw = value.strip()
    try:
        if re.fullmatch(r"\d{8}", raw):
            return datetime.strptime(raw, "%Y%m%d").date()
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise VerificationError(f"{field} is not a valid date") from exc


def _cutoff_day(value: object, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise VerificationError(f"{field} must be an ISO date or datetime")
    raw = value.strip()
    try:
        if "T" in raw or " " in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        return _day(raw, field)
    except ValueError as exc:
        raise VerificationError(f"{field} is not a valid cutoff") from exc


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not _is_mapping(value):
        raise VerificationError(f"{field} must be an object")
    return value  # type: ignore[return-value]


def _contains_sensitive(value: object, *, where: str = "artifact") -> bool:
    """Reject credential-shaped keys and values before any hash is emitted."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                return True
            if _contains_sensitive(item, where=f"{where}.{key}"):
                return True
        return False
    if isinstance(value, str):
        return bool(_SENSITIVE_VALUE_RE.search(value))
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive(item, where=where) for item in value)
    return False


def _without_volatile(value: object) -> object:
    """Remove capture timestamps from the canonical evidence payload."""

    if isinstance(value, Mapping):
        return {
            str(key): _without_volatile(item)
            for key, item in value.items()
            if str(key).lower() not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_without_volatile(item) for item in value]
    return value


def _hash_input(artifact: Mapping[str, object]) -> str:
    return canonical_hash(
        _without_volatile(
            {
                "policy": artifact.get("policy"),
                "adapter": artifact.get("adapter"),
                "source": artifact.get("source"),
                "mapping": artifact.get("mapping"),
                "semantics": artifact.get("semantics"),
                "input": artifact.get("input"),
            }
        )
    )


def _hash_output(artifact: Mapping[str, object]) -> str:
    return canonical_hash(_without_volatile({"output": artifact.get("output")}))


def artifact_hashes(artifact: Mapping[str, object]) -> VerificationHashes:
    """Compute reproducible hashes without trusting stored hash fields."""

    if _contains_sensitive(artifact):
        raise VerificationError("artifact contains credential-shaped material")
    input_hash = _hash_input(artifact)
    output_hash = _hash_output(artifact)
    verification = artifact.get("verification")
    verification_payload = (
        dict(verification) if isinstance(verification, Mapping) else {}
    )
    verification_payload.pop("hashes", None)
    verification_payload.pop("input_hash", None)
    verification_payload.pop("output_hash", None)
    verification_payload.pop("evidence_hash", None)
    evidence_hash = canonical_hash(
        _without_volatile(
            {
                "policy": artifact.get("policy"),
                "adapter": artifact.get("adapter"),
                "source": artifact.get("source"),
                "mapping": artifact.get("mapping"),
                "semantics": artifact.get("semantics"),
                "input_hash": input_hash,
                "output_hash": output_hash,
                "verification": verification_payload,
            }
        )
    )
    return VerificationHashes(input_hash, output_hash, evidence_hash)


def build_artifact(
    *,
    factor_rows: Sequence[Mapping[str, object]],
    source_native: Mapping[str, Sequence[Mapping[str, object]]],
    adapter: Mapping[str, Sequence[Mapping[str, object]]],
    cutoff_cases: Sequence[Mapping[str, object]],
    boundary_effective_date: str | date,
    adapter_version: str,
    source_batch: str,
    mapping: Mapping[str, object],
    semantics: Mapping[str, object],
    artifact_id: str | None = None,
    documentation_urls: Sequence[str] = (),
) -> dict[str, object]:
    """Build a versioned artifact from captured source and adapter rows.

    This helper only records values supplied by the caller.  It intentionally
    contains no adjustment formula, so an unavailable native output produces
    a failed artifact instead of an inferred one.
    """

    artifact: dict[str, object] = {
        "artifact_id": artifact_id or f"{ADJUSTMENT_POLICY_REF}/{source_batch}",
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "policy": {"key": ADJUSTMENT_POLICY_KEY, "version": ADJUSTMENT_POLICY_VERSION},
        "adapter": {"key": "etf_raw_bar_adapter", "version": _text(adapter_version, "adapter_version")},
        "source": {
            "name": "tushare",
            "capture_mode": "real_source",
            "endpoint": "fund_adj + pro_bar",
            "batch": _text(source_batch, "source_batch"),
            "documentation_urls": list(documentation_urls),
            "redaction": "no credentials captured",
        },
        "mapping": dict(mapping),
        "semantics": dict(semantics),
        "input": {
            "boundary_effective_date": boundary_effective_date,
            "factor_rows": [dict(row) for row in factor_rows],
            "cutoff_cases": [dict(case) for case in cutoff_cases],
        },
        "output": {
            "source_native": {
                basis: [dict(row) for row in rows]
                for basis, rows in source_native.items()
            },
            "adapter": {
                basis: [dict(row) for row in rows]
                for basis, rows in adapter.items()
            },
        },
        "verification": {"status": "pending", "checks": [], "hashes": {}},
    }
    # Do not return a failed artifact that still carries a credential-shaped
    # field.  Hashing such a payload would make the secret durable in an
    # evidence file even though verification correctly failed.
    if _contains_sensitive(artifact):
        raise VerificationError("artifact contains credential-shaped material")
    verification = artifact["verification"]
    assert isinstance(verification, dict)
    # ``verify_artifact`` validates the declared lifecycle status as part of
    # its fail-closed contract.  A newly assembled artifact starts without a
    # status, so temporarily declare the candidate as passed while checking
    # the captured rows.  The result below immediately replaces that marker
    # with ``failed`` when any source/adapter check does not pass.
    verification["status"] = "passed"
    provisional = verify_artifact(artifact, require_hashes=False)
    # Hashes are still written for a failed artifact, because a failed
    # evidence package must remain reproducible and auditable.
    verification["status"] = "passed" if provisional.passed else "failed"
    verification["checks"] = list(provisional.checks)
    if provisional.errors:
        verification["failure_reason"] = provisional.errors[0]
    verification["hashes"] = artifact_hashes(artifact).as_dict()
    return artifact


def _precision_places(precision: object, field: str) -> int:
    if isinstance(precision, Mapping):
        value = precision.get(field)
        if value is None and field == "price_decimal_places":
            value = precision.get("decimal_places")
    else:
        value = None
    if value is None and isinstance(precision, str):
        match = re.search(r"(\d+)\s*decimal", precision, re.IGNORECASE)
        value = int(match.group(1)) if match else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 18:
        raise VerificationError(
            f"semantics.precision.{field} must declare decimal places"
        )
    return value


def _record_key(row: Mapping[str, object], where: str) -> tuple[str, date]:
    code = _text(row.get("ts_code"), f"{where}.ts_code")
    day = _day(row.get("trade_date"), f"{where}.trade_date")
    return code, day


def _validate_factor_rows(
    rows: object,
    *,
    mapping: Mapping[str, object],
) -> tuple[dict[tuple[str, date], dict[str, object]], list[str]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise VerificationError("input.factor_rows must be an array")
    factor_field = _text(mapping.get("factor_field"), "mapping.factor_field")
    source_date_field = _text(
        mapping.get("source_date_field", "trade_date"),
        "mapping.source_date_field",
    )
    source_code_field = _text(
        mapping.get("source_code_field", "ts_code"),
        "mapping.source_code_field",
    )
    parsed: dict[tuple[str, date], dict[str, object]] = {}
    for index, raw in enumerate(rows):
        row = _require_mapping(raw, f"input.factor_rows[{index}]")
        source_code = _text(row.get(source_code_field), f"factor_rows[{index}].{source_code_field}")
        effective = _day(row.get(source_date_field), f"factor_rows[{index}].{source_date_field}")
        if "effective_date" in row and _day(
            row.get("effective_date"), f"factor_rows[{index}].effective_date"
        ) != effective:
            raise VerificationError(
                f"factor_rows[{index}] source date and effective_date disagree"
            )
        factor = _decimal(row.get(factor_field), f"factor_rows[{index}].{factor_field}")
        if factor <= 0:
            raise VerificationError(f"factor_rows[{index}].{factor_field} must be positive")
        key = (source_code, effective)
        if key in parsed:
            raise VerificationError(
                f"duplicate source factor row for {source_code} {effective.isoformat()}"
            )
        parsed[key] = {
            "ts_code": source_code,
            "trade_date": effective.isoformat(),
            "effective_date": effective.isoformat(),
            "adj_factor": format(factor, "f"),
        }
    if len(parsed) < 2:
        raise VerificationError("at least two factor effective dates are required")
    return parsed, ["source_factor_rows", "effective_date_normalization"]


def _validate_semantics(semantics: Mapping[str, object]) -> tuple[int, int, list[str]]:
    qfq_formula = _text(semantics.get("qfq_formula"), "semantics.qfq_formula")
    hfq_formula = _text(semantics.get("hfq_formula"), "semantics.hfq_formula")
    qfq_anchor = _text(semantics.get("qfq_anchor"), "semantics.qfq_anchor")
    hfq_anchor = _text(semantics.get("hfq_anchor"), "semantics.hfq_anchor")
    cutoff_rule = _text(semantics.get("cutoff_rule"), "semantics.cutoff_rule")
    precision = semantics.get("precision")
    rounding = _text(semantics.get("rounding"), "semantics.rounding")
    if qfq_formula == hfq_formula:
        raise VerificationError("qfq_formula and hfq_formula must be distinct")
    if qfq_anchor == hfq_anchor:
        raise VerificationError("qfq_anchor and hfq_anchor must be distinct")
    # The research adapter deliberately supports only the two identifiers
    # registered for this policy.  Rejecting an unknown source declaration at
    # verification time prevents a seemingly passed artifact from producing
    # an adjusted series that can only fail later during a strategy read.
    try:
        from app.backtesting.data.etf_adjustment import _anchor_index, _formula_kind
        from app.backtesting.data.requests import PriceBasis

        _formula_kind(qfq_formula, PriceBasis.QFQ)
        _formula_kind(hfq_formula, PriceBasis.HFQ)
        _anchor_index(qfq_anchor, PriceBasis.QFQ)
        _anchor_index(hfq_anchor, PriceBasis.HFQ)
    except Exception as exc:
        raise VerificationError(
            "semantics formula or anchor is not a registered source-native identifier"
        ) from exc
    if "effective_date" not in cutoff_rule or "data_cutoff" not in cutoff_rule:
        raise VerificationError(
            "semantics.cutoff_rule must state effective_date <= data_cutoff"
        )
    if rounding.lower() in {"unknown", "unconfirmed", "unspecified"}:
        raise VerificationError("semantics.rounding must be confirmed")
    price_places = _precision_places(precision, "price_decimal_places")
    factor_places = _precision_places(precision, "factor_decimal_places")
    return price_places, factor_places, [
        "formula_identifiers",
        "anchors",
        "precision",
        "rounding",
        "cutoff_rule",
    ]


def _validate_mapping(mapping: Mapping[str, object]) -> list[str]:
    for field in ("source_code_field", "source_date_field", "factor_field"):
        _text(mapping.get(field), f"mapping.{field}")
    _text(mapping.get("effective_date"), "mapping.effective_date")
    for basis in ("qfq", "hfq"):
        fields = _require_mapping(mapping.get(f"{basis}_fields"), f"mapping.{basis}_fields")
        for field in _PRICE_FIELDS:
            _text(fields.get(field), f"mapping.{basis}_fields.{field}")
    return ["source_to_normalized_fields", "price_field_mapping"]


def _validate_cutoffs(
    cases: object,
    factor_dates: set[date],
    *,
    boundary: date | None,
) -> list[str]:
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
        raise VerificationError("input.cutoff_cases must be an array")
    if boundary is None:
        raise VerificationError("input.boundary_effective_date is required")
    before = after = False
    for index, raw in enumerate(cases):
        case = _require_mapping(raw, f"input.cutoff_cases[{index}]")
        cutoff = _cutoff_day(case.get("data_cutoff"), f"cutoff_cases[{index}].data_cutoff")
        visible_raw = case.get("visible_effective_dates")
        if not isinstance(visible_raw, Sequence) or isinstance(visible_raw, (str, bytes)):
            raise VerificationError(
                f"cutoff_cases[{index}].visible_effective_dates must be an array"
            )
        visible = {_day(item, f"cutoff_cases[{index}].visible_effective_dates") for item in visible_raw}
        expected = {day for day in factor_dates if day <= cutoff}
        if visible != expected:
            raise VerificationError(
                f"cutoff_cases[{index}] does not apply effective_date <= data_cutoff"
            )
        if cutoff < boundary and boundary not in visible:
            before = True
        if cutoff >= boundary and boundary in visible:
            after = True
    if not before or not after:
        raise VerificationError(
            "cutoff cases must cover a boundary-before and boundary-after sample"
        )
    return ["cutoff_before_boundary", "cutoff_after_boundary"]


def _compare_native_outputs(
    output: Mapping[str, object],
    factor_keys: set[tuple[str, date]],
    *,
    price_places: int,
) -> list[str]:
    native = _require_mapping(output.get("source_native"), "output.source_native")
    adapter = _require_mapping(output.get("adapter"), "output.adapter")
    quantum = Decimal(1).scaleb(-price_places)
    checks: list[str] = []
    for basis in ("qfq", "hfq"):
        native_rows = native.get(basis)
        adapter_rows = adapter.get(basis)
        if not isinstance(native_rows, Sequence) or isinstance(native_rows, (str, bytes)):
            raise VerificationError(f"output.source_native.{basis} must be an array")
        if not isinstance(adapter_rows, Sequence) or isinstance(adapter_rows, (str, bytes)):
            raise VerificationError(f"output.adapter.{basis} must be an array")
        if not native_rows or not adapter_rows:
            raise VerificationError(
                f"source-native and adapter {basis} outputs are both required"
            )
        native_index: dict[tuple[str, date], Mapping[str, object]] = {}
        adapter_index: dict[tuple[str, date], Mapping[str, object]] = {}
        for index, raw in enumerate(native_rows):
            row = _require_mapping(raw, f"output.source_native.{basis}[{index}]")
            key = _record_key(row, f"source_native.{basis}[{index}]")
            if key in native_index:
                raise VerificationError(f"duplicate source-native {basis} output {key}")
            if key not in factor_keys:
                raise VerificationError(f"source-native {basis} output has no factor row {key}")
            native_index[key] = row
        for index, raw in enumerate(adapter_rows):
            row = _require_mapping(raw, f"output.adapter.{basis}[{index}]")
            key = _record_key(row, f"adapter.{basis}[{index}]")
            if key in adapter_index:
                raise VerificationError(f"duplicate adapter {basis} output {key}")
            if key not in factor_keys:
                raise VerificationError(f"adapter {basis} output has no factor row {key}")
            adapter_index[key] = row
        if set(native_index) != set(adapter_index):
            raise VerificationError(f"{basis} native and adapter dates do not correspond")
        if set(native_index) != factor_keys:
            missing = sorted(
                factor_keys - set(native_index), key=lambda item: (item[0], item[1])
            )
            raise VerificationError(
                f"source-native {basis} output does not cover every factor row; "
                f"missing={missing}"
            )
        if set(adapter_index) != factor_keys:
            missing = sorted(
                factor_keys - set(adapter_index), key=lambda item: (item[0], item[1])
            )
            raise VerificationError(
                f"adapter {basis} output does not cover every factor row; "
                f"missing={missing}"
            )
        for key in sorted(native_index, key=lambda item: item[1]):
            native_row = native_index[key]
            adapter_row = adapter_index[key]
            for field in _PRICE_FIELDS:
                left = _decimal(native_row.get(field), f"source_native.{basis}.{key}.{field}")
                right = _decimal(adapter_row.get(field), f"adapter.{basis}.{key}.{field}")
                if left.quantize(quantum) != right.quantize(quantum):
                    raise VerificationError(
                        f"{basis} {key} {field} differs outside declared precision"
                    )
        checks.append(f"{basis}_native_vs_adapter")
    return checks


def _validate_artifact(
    artifact: Mapping[str, object],
    *,
    adapter_output: Mapping[str, object] | Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
) -> tuple[list[str], str | None]:
    if _contains_sensitive(artifact):
        raise VerificationError("artifact contains credential-shaped material")
    schema = _integer(artifact.get("artifact_schema_version"), "artifact_schema_version")
    if schema != ARTIFACT_SCHEMA_VERSION:
        raise VerificationError(f"unsupported artifact schema version {schema}")
    policy = _require_mapping(artifact.get("policy"), "policy")
    if _text(policy.get("key"), "policy.key") != ADJUSTMENT_POLICY_KEY:
        raise VerificationError("artifact policy key does not match the fixed policy")
    if _integer(policy.get("version"), "policy.version") != ADJUSTMENT_POLICY_VERSION:
        raise VerificationError("artifact policy version does not match the fixed policy")
    adapter = _require_mapping(artifact.get("adapter"), "adapter")
    adapter_version = _text(adapter.get("version"), "adapter.version")
    source = _require_mapping(artifact.get("source"), "source")
    _text(source.get("name"), "source.name")
    capture_mode = _text(source.get("capture_mode"), "source.capture_mode").lower()
    if capture_mode not in {"real_source", "real-source"}:
        raise VerificationError("source.capture_mode must be real_source")
    _text(source.get("batch"), "source.batch")
    _text(source.get("endpoint"), "source.endpoint")
    mapping = _require_mapping(artifact.get("mapping"), "mapping")
    checks = _validate_mapping(mapping)
    semantics = _require_mapping(artifact.get("semantics"), "semantics")
    price_places, _factor_places, semantic_checks = _validate_semantics(semantics)
    checks.extend(semantic_checks)
    input_payload = _require_mapping(artifact.get("input"), "input")
    factor_index, factor_checks = _validate_factor_rows(
        input_payload.get("factor_rows"), mapping=mapping
    )
    checks.extend(factor_checks)
    factor_keys = set(factor_index)
    codes = {key[0] for key in factor_keys}
    if len(codes) != 1:
        raise VerificationError("verification must cover exactly one real instrument")
    boundary_value = input_payload.get("boundary_effective_date")
    boundary = _day(boundary_value, "input.boundary_effective_date") if boundary_value is not None else None
    checks.extend(
        _validate_cutoffs(
            input_payload.get("cutoff_cases"),
            {key[1] for key in factor_keys},
            boundary=boundary,
        )
    )
    output_payload = _require_mapping(artifact.get("output"), "output")
    if adapter_output is not None:
        if callable(adapter_output):
            generated = adapter_output(artifact)
        else:
            generated = adapter_output
        if not isinstance(generated, Mapping):
            raise VerificationError("adapter_output must be an object")
        output_payload = dict(output_payload)
        output_payload["adapter"] = generated
    checks.extend(_compare_native_outputs(output_payload, factor_keys, price_places=price_places))
    return checks, _text(artifact.get("artifact_id"), "artifact_id") if artifact.get("artifact_id") is not None else None


def verify_artifact(
    artifact: Mapping[str, object],
    *,
    adapter_output: Mapping[str, object] | Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
    require_hashes: bool = True,
) -> VerificationResult:
    """Validate a captured artifact and return a fail-closed result.

    ``adapter_output`` is optional for checking a checked-in artifact.  When
    supplied, it replaces the artifact's recorded adapter output, allowing a
    release test to compare the current adapter implementation against the
    captured native rows without changing the artifact on disk.
    """

    if not isinstance(artifact, Mapping):
        hashes = VerificationHashes("", "", "")
        return VerificationResult("failed", False, ("artifact must be an object",), (), hashes)
    try:
        checks, artifact_id = _validate_artifact(artifact, adapter_output=adapter_output)
        hashes = artifact_hashes(artifact)
        verification = _require_mapping(artifact.get("verification"), "verification")
        stored = verification.get("hashes", verification)
        if require_hashes:
            stored_map = _require_mapping(stored, "verification.hashes")
            for field, value in hashes.as_dict().items():
                if stored_map.get(field) != value:
                    raise VerificationError(f"{field} does not match reproducible artifact hash")
        declared_status = str(verification.get("status", "passed")).strip().lower()
        if declared_status != "passed":
            raise VerificationError(
                "artifact verification status is not passed; policy must remain inactive"
            )
        return VerificationResult("passed", True, (), tuple(checks), hashes, artifact_id)
    except (VerificationError, TypeError, ValueError) as exc:
        try:
            hashes = artifact_hashes(artifact)
        except Exception:
            hashes = VerificationHashes("", "", "")
        return VerificationResult(
            "failed",
            False,
            (str(exc),),
            (),
            hashes,
            artifact.get("artifact_id") if isinstance(artifact.get("artifact_id"), str) else None,
        )


def assert_verified(
    artifact: Mapping[str, object],
    *,
    adapter_output: Mapping[str, object] | Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
) -> VerificationResult:
    """Raise ``VerificationError`` unless every real-source check passes."""

    result = verify_artifact(artifact, adapter_output=adapter_output)
    if not result.passed:
        reason = result.errors[0] if result.errors else "verification failed"
        raise VerificationError(reason)
    return result


def load_artifact(path: str | Path = DEFAULT_ARTIFACT_PATH) -> dict[str, object]:
    """Load one versioned artifact without executing any code from it."""

    artifact_path = Path(path)
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot load verification artifact {artifact_path}") from exc
    if not isinstance(payload, dict):
        raise VerificationError("verification artifact root must be an object")
    return payload


def verify_artifact_file(
    path: str | Path = DEFAULT_ARTIFACT_PATH,
    *,
    require_hashes: bool = True,
) -> VerificationResult:
    """Load and verify a versioned artifact file."""

    try:
        artifact = load_artifact(path)
    except VerificationError as exc:
        return VerificationResult(
            "failed", False, (str(exc),), (), VerificationHashes("", "", "")
        )
    return verify_artifact(artifact, require_hashes=require_hashes)


def _main() -> int:
    parser = argparse.ArgumentParser(description="verify ETF adjustment artifact")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_ARTIFACT_PATH))
    args = parser.parse_args()
    result = verify_artifact_file(args.path)
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover - exercised by the smoke command
    raise SystemExit(_main())
