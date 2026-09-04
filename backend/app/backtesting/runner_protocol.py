"""Versioned protocol helpers for the backtest worker boundary.

The worker and ``RunnerSupervisor`` communicate through durable evidence rather
than through an inferred process exit status.  This module is deliberately
free of database and process imports so its validators can be exercised in
unit tests without starting a worker or a PostgreSQL connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID


COMPLETION_MARKER_PROTOCOL = "completion_marker@1"
EXIT_CODE_PROTOCOL = "runner_exit_code@1"
RESULT_INTEGRITY_ALGORITHM = "sha256"
RESULT_INTEGRITY_CANONICALIZATION = "jcs@1"
# Version 2 includes the event payload version and direct order-to-decision
# linkage in canonical result rows.  Reusing v1 here would make old completion
# markers claim coverage they do not actually provide.
RESULT_INTEGRITY_SCOPE = "backtest_result_rows@2"

# The order is part of the protocol.  It is used by both the marker validator
# and the canonical result digest implementation; changing it would make old
# completion evidence impossible to reproduce.
COVERED_RESULT_TABLES = (
    "backtest_steps",
    "backtest_events",
    "backtest_decisions",
    "backtest_orders",
    "backtest_order_updates",
    "backtest_fills",
    "backtest_positions",
    "backtest_equity_curve",
    "backtest_metrics",
)
RESULT_COUNT_KEYS = (
    "steps",
    "events",
    "decisions",
    "orders",
    "order_updates",
    "fills",
    "positions",
    "equity_points",
    "metrics",
)


class ExitCategory(StrEnum):
    """The only business categories represented by a worker exit code."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNMAPPED = "unmapped"


DETERMINATE_CATEGORIES = frozenset(
    {
        ExitCategory.SUCCEEDED.value,
        ExitCategory.FAILED.value,
        ExitCategory.CANCELLED.value,
        ExitCategory.TIMED_OUT.value,
    }
)
EXIT_CODE_TO_CATEGORY = MappingProxyType(
    {
        0: ExitCategory.SUCCEEDED.value,
        10: ExitCategory.FAILED.value,
        20: ExitCategory.CANCELLED.value,
        30: ExitCategory.TIMED_OUT.value,
    }
)
CATEGORY_TO_EXIT_CODE = MappingProxyType(
    {value: key for key, value in EXIT_CODE_TO_CATEGORY.items()}
)
_HEX_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONFIG_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MARKER_FIELDS = frozenset(
    {
        "protocol_version",
        "run_id",
        "declared_category",
        "result_integrity",
        "result_counts",
        "failure_phase",
        "failure_type",
        # The run root remains authoritative, but this optional extension
        # lets a worker carry the frozen input identity in its evidence.
        "config_hash",
    }
)
_REQUIRED_MARKER_FIELDS = frozenset(
    {
        "protocol_version",
        "run_id",
        "declared_category",
        "result_integrity",
        "result_counts",
        "failure_phase",
        "failure_type",
    }
)
_INTEGRITY_FIELDS = frozenset(
    {"algorithm", "canonicalization", "scope", "covered_tables", "digest"}
)


class CompletionMarkerValidationError(ValueError):
    """Raised when a completion marker cannot be trusted."""

    def __init__(self, errors: tuple[str, ...] | list[str] | str):
        if isinstance(errors, str):
            normalized = (errors,)
        else:
            normalized = tuple(errors)
        self.errors = normalized
        super().__init__("; ".join(normalized) or "invalid completion marker")


@dataclass(frozen=True, slots=True)
class MarkerValidation:
    """Structured marker validation result.

    ``bool(result)`` is intentionally supported so callers can use this
    result in a guard while still retaining all failure evidence for logs and
    the run root.  The input mapping is never returned as a mutable alias.
    """

    valid: bool
    errors: tuple[str, ...] = ()
    marker: Mapping[str, Any] | None = None

    def __bool__(self) -> bool:
        return self.valid

    # These properties expose the protocol-level dimensions without making
    # callers parse error strings.  They are intentionally derived from the
    # bounded error list so the result remains immutable and serializable.
    @property
    def protocol_version_valid(self) -> bool:
        return self.marker is not None and not any(
            "protocol_version" in error for error in self.errors
        )

    @property
    def run_id_valid(self) -> bool:
        return self.marker is not None and not any(
            "run_id" in error for error in self.errors
        )

    @property
    def category_valid(self) -> bool:
        return self.marker is not None and not any(
            "declared_category" in error for error in self.errors
        )

    @property
    def integrity_shape_valid(self) -> bool:
        return self.marker is not None and not any(
            "result_integrity" in error for error in self.errors
        )

    @property
    def counts_valid(self) -> bool:
        return self.marker is not None and not any(
            "result_counts" in error for error in self.errors
        )

    @property
    def failure_fields_valid(self) -> bool:
        return self.marker is not None and not any(
            "failure" in error for error in self.errors
        )

    @property
    def normalized(self) -> dict[str, Any] | None:
        return dict(self.marker) if self.marker is not None else None

    def as_dict(self) -> dict[str, Any]:
        """Return bounded validation evidence suitable for a JSON column."""

        return {
            "valid": self.valid,
            "protocol_version_valid": self.protocol_version_valid,
            "run_id_valid": self.run_id_valid,
            "category_valid": self.category_valid,
            "integrity_shape_valid": self.integrity_shape_valid,
            "counts_valid": self.counts_valid,
            "failure_fields_valid": self.failure_fields_valid,
            "errors": list(self.errors),
        }

    def raise_for_error(self) -> Mapping[str, Any]:
        if not self.valid or self.marker is None:
            raise CompletionMarkerValidationError(self.errors)
        return self.marker


@dataclass(frozen=True, slots=True)
class CompletionMarker:
    """Typed representation of one canonical completion marker.

    The dataclass is intentionally a value object.  It does not know how to
    write a database row; callers must first commit result rows and then use a
    launch-aware writer.  ``from_mapping`` always runs the same strict
    validator used by the Supervisor, preventing a second marker schema from
    emerging in an adapter.
    """

    protocol_version: str
    run_id: UUID
    declared_category: str
    result_integrity: Mapping[str, Any]
    result_counts: Mapping[str, int]
    failure_phase: str | None
    failure_type: str | None
    config_hash: str | None = None

    def __post_init__(self) -> None:
        """Reject invalid values even when callers bypass ``from_mapping``."""

        validation = validate_completion_marker(
            self.as_dict(), run_id=self.run_id, config_hash=self.config_hash
        )
        if not validation.valid:
            raise CompletionMarkerValidationError(validation.errors)

    @classmethod
    def from_mapping(
        cls,
        marker: Mapping[str, Any],
        *,
        run_id: UUID | str | None = None,
        config_hash: str | None = None,
    ) -> "CompletionMarker":
        """Parse a marker only after all protocol constraints are checked."""

        validation = validate_completion_marker(
            marker, run_id=run_id, config_hash=config_hash
        )
        normalized = validation.raise_for_error()
        return cls(
            protocol_version=normalized["protocol_version"],
            run_id=UUID(str(normalized["run_id"])),
            declared_category=normalized["declared_category"],
            result_integrity=dict(normalized["result_integrity"]),
            result_counts=dict(normalized["result_counts"]),
            failure_phase=normalized["failure_phase"],
            failure_type=normalized["failure_type"],
            config_hash=normalized.get("config_hash"),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible marker mapping."""

        payload: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "run_id": str(self.run_id),
            "declared_category": self.declared_category,
            "result_integrity": dict(self.result_integrity),
            "result_counts": dict(self.result_counts),
            "failure_phase": self.failure_phase,
            "failure_type": self.failure_type,
        }
        if self.config_hash is not None:
            payload["config_hash"] = self.config_hash
        return payload


@dataclass(frozen=True, slots=True)
class TerminalEvaluation:
    """All evidence used for one immutable terminal decision."""

    status: str
    reason: str
    exit_category: str
    marker_valid: bool
    integrity_valid: bool
    errors: tuple[str, ...] = ()

    @property
    def determinate(self) -> bool:
        return self.status != "indeterminate"

    @property
    def terminal_status(self) -> str:
        """Canonical field name used by the Supervisor adapter."""

        return self.status


@dataclass(frozen=True, slots=True)
class RunnerExitClassification:
    """Structured ``runner_exit_code@1`` classification.

    Keeping the raw code and signal separate prevents a POSIX signal from
    being accidentally coerced into a business exit category by a caller.
    """

    protocol_version: str
    raw_exit_code: int | None
    signal_number: int | None
    category: str
    mapped: bool
    reason: str

    @property
    def exit_category(self) -> str:
        return self.category

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "raw_exit_code": self.raw_exit_code,
            "signal_number": self.signal_number,
            "category": self.category,
            "mapped": self.mapped,
            "reason": self.reason,
        }


def map_exit_code(exit_code: int | None) -> str:
    """Map the frozen numeric protocol; signals and unknown values are unmapped."""

    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return ExitCategory.UNMAPPED.value
    return EXIT_CODE_TO_CATEGORY.get(exit_code, ExitCategory.UNMAPPED.value)


def map_runner_exit_code(
    raw_exit_code: int | None,
    signal_number: int | None = None,
) -> RunnerExitClassification:
    """Return the complete, lossless ``runner_exit_code@1`` classification.

    A signal is kept as a separate field and always remains ``unmapped``.
    The optional signal argument is useful for platforms that expose a
    negative return code as well as for persisted recovery evidence.
    """

    valid_signal = (
        None
        if signal_number is None or isinstance(signal_number, bool)
        else int(signal_number)
        if isinstance(signal_number, int)
        else None
    )
    # ``subprocess`` commonly reports a POSIX signal as a negative return
    # code.  Preserve that raw evidence while exposing the positive signal
    # number so callers do not accidentally map ``-9`` to a business failure.
    if valid_signal is None and isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool) and raw_exit_code < 0:
        valid_signal = -raw_exit_code
    if valid_signal is not None:
        return RunnerExitClassification(
            EXIT_CODE_PROTOCOL,
            raw_exit_code if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool) else None,
            valid_signal,
            ExitCategory.UNMAPPED.value,
            False,
            "signal_termination",
        )
    category = map_exit_code(raw_exit_code)
    if category == ExitCategory.UNMAPPED.value:
        reason = "unknown_exit_code" if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool) else "missing_or_invalid_exit_code"
        return RunnerExitClassification(
            EXIT_CODE_PROTOCOL,
            raw_exit_code if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool) else None,
            None,
            category,
            False,
            reason,
        )
    return RunnerExitClassification(
        EXIT_CODE_PROTOCOL,
        raw_exit_code,
        None,
        category,
        True,
        "mapped_exit_code",
    )


# Explicit aliases make the protocol name discoverable to callers that use
# either the document's wording or the conventional ``classify`` wording.
classify_exit_code = map_exit_code
exit_code_category = map_exit_code


def _as_run_id(value: Any) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value.strip():
        try:
            return str(UUID(value))
        except ValueError:
            return None
    return None


def _validate_digest(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _HEX_DIGEST_RE.fullmatch(value) is None:
        errors.append(f"{field} must be sha256:<64 lowercase hex digits>")


def _validate_config_hash(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _CONFIG_HASH_RE.fullmatch(value) is None:
        errors.append(f"{field} must be 64 lowercase hexadecimal characters")


def validate_completion_marker(
    marker: Mapping[str, Any] | None,
    *,
    run_id: UUID | str | None = None,
    config_hash: str | None = None,
    raise_on_error: bool = False,
) -> MarkerValidation:
    """Validate the complete ``completion_marker@1`` shape.

    Validation is fail-closed: missing fields, extra count/table names,
    mismatched run identity, and malformed failure evidence all invalidate the
    marker.  ``config_hash`` is optional at the marker boundary because the
    canonical digest carries it as part of its scope; when a marker includes a
    ``config_hash`` extension it is still validated and compared.
    """

    # Accept the typed value object without creating a second validation path.
    # The conversion is detached, so callers cannot mutate evidence while it
    # is being checked.
    if not isinstance(marker, Mapping):
        as_dict = getattr(marker, "as_dict", None)
        if callable(as_dict):
            marker = as_dict()
    errors: list[str] = []
    if not isinstance(marker, Mapping):
        errors.append("marker must be an object")
        result = MarkerValidation(False, tuple(errors), None)
        if raise_on_error:
            result.raise_for_error()
        return result

    unknown_fields = set(marker).difference(_MARKER_FIELDS)
    if unknown_fields:
        errors.append(
            "marker contains unsupported fields: "
            + ", ".join(sorted(str(field) for field in unknown_fields))
        )
    missing_fields = _REQUIRED_MARKER_FIELDS.difference(marker)
    if missing_fields:
        errors.append(
            "marker is missing required fields: "
            + ", ".join(sorted(missing_fields))
        )

    expected_run_id = _as_run_id(run_id)
    protocol = marker.get("protocol_version")
    if protocol != COMPLETION_MARKER_PROTOCOL:
        errors.append("protocol_version must be completion_marker@1")

    marker_run_id = marker.get("run_id")
    normalized_marker_run_id = _as_run_id(marker_run_id)
    if normalized_marker_run_id is None:
        errors.append("run_id must be a UUID")
    elif expected_run_id is not None and normalized_marker_run_id != expected_run_id:
        errors.append("run_id does not match the run root")

    declared = marker.get("declared_category")
    if declared not in DETERMINATE_CATEGORIES:
        errors.append("declared_category is not a supported terminal category")

    integrity = marker.get("result_integrity")
    if not isinstance(integrity, Mapping):
        errors.append("result_integrity must be an object")
    else:
        unknown_integrity_fields = set(integrity).difference(_INTEGRITY_FIELDS)
        if unknown_integrity_fields:
            errors.append(
                "result_integrity contains unsupported fields: "
                + ", ".join(sorted(str(field) for field in unknown_integrity_fields))
            )
        if integrity.get("algorithm") != RESULT_INTEGRITY_ALGORITHM:
            errors.append("result_integrity.algorithm must be sha256")
        if integrity.get("canonicalization") != RESULT_INTEGRITY_CANONICALIZATION:
            errors.append("result_integrity.canonicalization must be jcs@1")
        if integrity.get("scope") != RESULT_INTEGRITY_SCOPE:
            errors.append(
        f"result_integrity.scope must be {RESULT_INTEGRITY_SCOPE}"
    )
        covered = integrity.get("covered_tables")
        normalized_covered = tuple(covered) if isinstance(covered, list) else None
        if normalized_covered != COVERED_RESULT_TABLES:
            errors.append("result_integrity.covered_tables must match the nine fixed tables")
        _validate_digest(integrity.get("digest"), "result_integrity.digest", errors)

    counts = marker.get("result_counts")
    if not isinstance(counts, Mapping):
        errors.append("result_counts must be an object")
    else:
        if set(counts) != set(RESULT_COUNT_KEYS):
            errors.append("result_counts must contain exactly the nine fixed counters")
        for key in RESULT_COUNT_KEYS:
            value = counts.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"result_counts.{key} must be a non-negative integer")

    failure_phase = marker.get("failure_phase")
    failure_type = marker.get("failure_type")
    if declared == ExitCategory.SUCCEEDED.value:
        if failure_phase is not None or failure_type is not None:
            errors.append("successful markers must not carry failure fields")
    elif declared in DETERMINATE_CATEGORIES:
        if not isinstance(failure_phase, str) or not failure_phase.strip():
            errors.append("non-success markers require failure_phase")
        if not isinstance(failure_type, str) or not failure_type.strip():
            errors.append("non-success markers require failure_type")

    # Some early worker builds persisted this as an extension.  It is safe to
    # accept only a correctly shaped value; digest calculation still includes
    # the authoritative config hash supplied by the run root.
    marker_config_hash = marker.get("config_hash")
    if marker_config_hash is not None:
        _validate_config_hash(marker_config_hash, "config_hash", errors)
        if config_hash is not None and marker_config_hash != config_hash:
            errors.append("config_hash does not match the run root")
    elif config_hash is not None and not _CONFIG_HASH_RE.fullmatch(config_hash):
        errors.append("config_hash must be 64 lowercase hexadecimal characters")

    # Marker evidence is persisted as JSONB.  Reject custom Python objects at
    # the protocol boundary instead of allowing a later database encoder to
    # fail after the worker has already reported a business exit.
    try:
        json.dumps(marker, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        errors.append("marker must be JSON serializable")

    result = MarkerValidation(not errors, tuple(errors), dict(marker))
    if raise_on_error:
        result.raise_for_error()
    return result


def is_valid_completion_marker(
    marker: Mapping[str, Any] | None,
    *,
    run_id: UUID | str | None = None,
    config_hash: str | None = None,
) -> bool:
    """Boolean convenience wrapper for code that does not need error details."""

    return bool(validate_completion_marker(marker, run_id=run_id, config_hash=config_hash))


def require_valid_completion_marker(
    marker: Mapping[str, Any] | None,
    *,
    run_id: UUID | str | None = None,
    config_hash: str | None = None,
) -> Mapping[str, Any]:
    """Return a marker or raise a structured validation error."""

    return validate_completion_marker(
        marker, run_id=run_id, config_hash=config_hash, raise_on_error=True
    ).raise_for_error()


def build_completion_marker(
    *,
    run_id: UUID | str,
    declared_category: str,
    digest: str,
    result_counts: Mapping[str, int],
    failure_phase: str | None = None,
    failure_type: str | None = None,
    config_hash: str | None = None,
) -> dict[str, Any]:
    """Build and validate a marker before a worker persists it.

    The function does not write a database row.  The caller is responsible for
    committing result rows before invoking its marker writer.
    """

    if not isinstance(result_counts, Mapping) or set(result_counts) != set(RESULT_COUNT_KEYS):
        raise CompletionMarkerValidationError(
            "result_counts must contain exactly the nine fixed counters"
        )
    for key in RESULT_COUNT_KEYS:
        value = result_counts[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CompletionMarkerValidationError(
                f"result_counts.{key} must be a non-negative integer"
            )
    marker: dict[str, Any] = {
        "protocol_version": COMPLETION_MARKER_PROTOCOL,
        "run_id": str(run_id),
        "declared_category": declared_category,
        "result_integrity": {
            "algorithm": RESULT_INTEGRITY_ALGORITHM,
            "canonicalization": RESULT_INTEGRITY_CANONICALIZATION,
            "scope": RESULT_INTEGRITY_SCOPE,
            "covered_tables": list(COVERED_RESULT_TABLES),
            "digest": digest,
        },
        "result_counts": {key: result_counts[key] for key in RESULT_COUNT_KEYS},
        "failure_phase": failure_phase,
        "failure_type": failure_type,
    }
    if config_hash is not None:
        marker["config_hash"] = config_hash
    require_valid_completion_marker(marker, run_id=run_id, config_hash=config_hash)
    return marker


def _integrity_values(integrity: Any) -> tuple[bool, str | None, Mapping[str, Any] | None, tuple[str, ...]]:
    """Normalize checker results without importing the integrity module."""

    # A bare boolean is not integrity evidence: a supervisor must compare a
    # digest and all result counters against the marker.  Accepting ``True``
    # here would let an adapter accidentally bypass that proof requirement.
    if integrity is True:
        return False, None, None, ("result integrity requires digest and counts",)
    if integrity is False or integrity is None:
        return False, None, None, ("result integrity is unavailable",)
    if isinstance(integrity, Mapping):
        valid = integrity.get("valid", integrity.get("passed", integrity.get("status") == "passed"))
        status = integrity.get("status")
        digest = integrity.get("digest")
        counts = integrity.get("counts", integrity.get("result_counts"))
        errors = integrity.get("errors", ())
        if isinstance(errors, str):
            errors = (errors,)
        normalized_counts = counts if isinstance(counts, Mapping) else None
        if not isinstance(digest, str) or _HEX_DIGEST_RE.fullmatch(digest) is None:
            errors = tuple(errors) + ("result integrity digest is invalid or missing",)
        if normalized_counts is None or set(normalized_counts) != set(RESULT_COUNT_KEYS):
            errors = tuple(errors) + ("result integrity counts are invalid or incomplete",)
        elif any(
            isinstance(normalized_counts[key], bool)
            or not isinstance(normalized_counts[key], int)
            or normalized_counts[key] < 0
            for key in RESULT_COUNT_KEYS
        ):
            errors = tuple(errors) + ("result integrity counts contain invalid values",)
        if status not in (None, "passed"):
            errors = tuple(errors) + (f"result integrity status is {status!r}",)
        return bool(valid) and not errors, digest, normalized_counts, tuple(errors)
    valid = bool(getattr(integrity, "valid", getattr(integrity, "passed", False)))
    status = getattr(integrity, "status", None)
    digest = getattr(integrity, "digest", None)
    counts = getattr(integrity, "counts", getattr(integrity, "result_counts", None))
    errors = getattr(integrity, "errors", ())
    if isinstance(errors, str):
        errors = (errors,)
    normalized_counts = counts if isinstance(counts, Mapping) else None
    if not isinstance(digest, str) or _HEX_DIGEST_RE.fullmatch(digest) is None:
        errors = tuple(errors) + ("result integrity digest is invalid or missing",)
    if normalized_counts is None or set(normalized_counts) != set(RESULT_COUNT_KEYS):
        errors = tuple(errors) + ("result integrity counts are invalid or incomplete",)
    elif any(
        isinstance(normalized_counts[key], bool)
        or not isinstance(normalized_counts[key], int)
        or normalized_counts[key] < 0
        for key in RESULT_COUNT_KEYS
    ):
        errors = tuple(errors) + ("result integrity counts contain invalid values",)
    if status not in (None, "passed"):
        errors = tuple(errors) + (f"result integrity status is {status!r}",)
    return valid and not errors, digest, normalized_counts, tuple(errors)


def _integrity_metadata(integrity: Any) -> dict[str, Any]:
    """Extract optional protocol metadata from checker evidence."""

    if isinstance(integrity, Mapping):
        return {
            key: integrity.get(key)
            for key in ("algorithm", "canonicalization", "scope", "covered_tables")
            if key in integrity
        }
    return {
        key: getattr(integrity, key)
        for key in ("algorithm", "canonicalization", "scope", "covered_tables")
        if hasattr(integrity, key)
    }


def evaluate_terminal(
    *,
    marker: Mapping[str, Any] | None,
    exit_code: int | None,
    integrity: Any = None,
    integrity_evidence: Any = None,
    run_id: UUID | str | None = None,
    config_hash: str | None = None,
    forced: bool = False,
) -> TerminalEvaluation:
    """Apply the conservative completion truth table.

    ``forced`` denotes KILL, signal termination, or another supervisor-forced
    stop.  Such a stop is indeterminate even if a stale marker happens to be
    present; only a naturally completed worker can claim a determinate result.
    """

    validation = validate_completion_marker(marker, run_id=run_id, config_hash=config_hash)
    exit_category = map_runner_exit_code(exit_code).category
    evidence = integrity if integrity_evidence is None else integrity_evidence
    integrity_valid, integrity_digest, integrity_counts, integrity_errors = _integrity_values(evidence)
    errors = list(validation.errors)
    errors.extend(integrity_errors)

    if forced:
        errors.append("worker was forcibly terminated")
    if exit_category == ExitCategory.UNMAPPED.value:
        errors.append("worker exit code is unmapped")
    if not validation.valid:
        errors.append("completion marker is invalid or missing")
    if not integrity_valid:
        errors.append("result integrity is not proven")

    # ``validate_completion_marker`` also accepts the typed CompletionMarker
    # value object.  Always continue with its detached normalized mapping so
    # the reconciliation checks below cannot be skipped merely because the
    # caller chose the typed API instead of a raw JSON mapping.
    normalized_marker = validation.marker
    if validation.valid and isinstance(normalized_marker, Mapping):
        marker_integrity = normalized_marker.get("result_integrity")
        marker_digest = marker_integrity.get("digest") if isinstance(marker_integrity, Mapping) else None
        if isinstance(marker_integrity, Mapping):
            metadata = _integrity_metadata(evidence)
            expected_metadata = {
                "algorithm": RESULT_INTEGRITY_ALGORITHM,
                "canonicalization": RESULT_INTEGRITY_CANONICALIZATION,
                "scope": RESULT_INTEGRITY_SCOPE,
                "covered_tables": COVERED_RESULT_TABLES,
            }
            for key, expected in expected_metadata.items():
                # A checker that returns metadata must agree with the marker;
                # a checker that omits metadata remains compatible with the
                # compact IntegrityEvidence adapter.
                actual = metadata.get(key)
                if actual is not None:
                    if key == "covered_tables":
                        actual = tuple(actual) if isinstance(actual, (list, tuple)) else actual
                    if actual != expected:
                        errors.append(f"result integrity {key} differs from protocol")
                elif key in marker_integrity and marker_integrity.get(key) != expected:
                    # Marker validation already catches malformed marker
                    # metadata, but retain an explicit reconciliation error.
                    errors.append(f"result integrity {key} differs from protocol")
        if integrity_digest is not None and marker_digest != integrity_digest:
            errors.append("result integrity digest differs from completion marker")
        marker_counts = normalized_marker.get("result_counts")
        if integrity_counts is not None and dict(marker_counts or {}) != dict(integrity_counts):
            errors.append("result counts differ from completion marker")
        if normalized_marker.get("declared_category") != exit_category:
            errors.append("completion marker category conflicts with exit code")

    if errors:
        # Stable reason strings are intentionally short; detailed errors are
        # retained separately so API summaries never leak unbounded evidence.
        if forced:
            reason = "forced_termination_without_provable_completion"
        elif not validation.valid:
            reason = "completion_marker_invalid_or_missing"
        elif not integrity_valid:
            reason = "result_integrity_unproven"
        elif exit_category == ExitCategory.UNMAPPED.value:
            reason = "runner_exit_code_unmapped"
        else:
            reason = "completion_evidence_conflict"
        return TerminalEvaluation(
            "indeterminate", reason, exit_category, validation.valid, integrity_valid, tuple(errors)
        )
    return TerminalEvaluation(
        exit_category,
        "completion_evidence_consistent",
        exit_category,
        True,
        True,
        (),
    )


def decide_terminal(**kwargs: Any) -> str:
    """Return only the final status for small callers and compatibility tests."""

    return evaluate_terminal(**kwargs).status


def reconcile_terminal_evidence(*args: Any, **kwargs: Any) -> Any:
    """Delegate to the canonical pure supervision adapter.

    The lazy import keeps this low-level protocol module independent from the
    integrity adapter while preserving one discoverable reconciliation entry
    point for callers that historically imported protocol helpers here.
    """

    from .run_supervision_adapter import reconcile_terminal_evidence as reconcile

    return reconcile(*args, **kwargs)


def __getattr__(name: str) -> Any:
    """Expose the adapter decision type without introducing an import cycle."""

    if name == "TerminalDecision":
        from .run_supervision_adapter import TerminalDecision

        return TerminalDecision
    raise AttributeError(name)


__all__ = [
    "CATEGORY_TO_EXIT_CODE",
    "COMPLETION_MARKER_PROTOCOL",
    "COVERED_RESULT_TABLES",
    "DETERMINATE_CATEGORIES",
    "EXIT_CODE_PROTOCOL",
    "EXIT_CODE_TO_CATEGORY",
    "ExitCategory",
    "MarkerValidation",
    "TerminalEvaluation",
    "CompletionMarkerValidationError",
    "CompletionMarker",
    "RESULT_COUNT_KEYS",
    "RESULT_INTEGRITY_ALGORITHM",
    "RESULT_INTEGRITY_CANONICALIZATION",
    "RESULT_INTEGRITY_SCOPE",
    "RunnerExitClassification",
    "build_completion_marker",
    "classify_exit_code",
    "decide_terminal",
    "evaluate_terminal",
    "exit_code_category",
    "is_valid_completion_marker",
    "map_exit_code",
    "map_runner_exit_code",
    "require_valid_completion_marker",
    "reconcile_terminal_evidence",
    "validate_completion_marker",
]
