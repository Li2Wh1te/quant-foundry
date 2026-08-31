"""Pure adapters shared by the runner Worker and Supervisor.

Task 23 deliberately keeps terminal reconciliation free of SQLAlchemy and
process-management side effects.  The Supervisor owns the transaction and
advisory lock; this module only turns the immutable evidence available after a
child exits into a complete ``TerminalDecision`` value object.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping
from uuid import UUID

from .runner_integrity import IntegrityVerification
from .runner_protocol import (
    COVERED_RESULT_TABLES,
    RESULT_COUNT_KEYS,
    RESULT_INTEGRITY_ALGORITHM,
    RESULT_INTEGRITY_CANONICALIZATION,
    RESULT_INTEGRITY_SCOPE,
    MarkerValidation,
    RunnerExitClassification,
    evaluate_terminal,
    map_runner_exit_code,
    validate_completion_marker,
)


_CONFIG_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class TerminalDecision:
    """Complete, side-effect-free output of terminal evidence reconciliation."""

    terminal_status: str
    exit_classification: RunnerExitClassification
    marker_validation: MarkerValidation
    integrity_validation: IntegrityVerification
    terminal_decision_reason: str
    failure_phase: str | None
    failure_type: str | None
    preserve_evidence: bool = True

    @property
    def status(self) -> str:
        """Compatibility alias used by older Supervisor call sites."""

        return self.terminal_status

    @property
    def reason(self) -> str:
        return self.terminal_decision_reason

    @property
    def exit_category(self) -> str:
        return self.exit_classification.category

    @property
    def marker_valid(self) -> bool:
        return self.marker_validation.valid

    @property
    def integrity_valid(self) -> bool:
        return self.integrity_validation.valid

    @property
    def determinate(self) -> bool:
        return self.terminal_status != "indeterminate"

    @property
    def errors(self) -> tuple[str, ...]:
        """Expose bounded validation errors for structured audit storage."""

        return self.marker_validation.errors + self.integrity_validation.errors

    def as_dict(self) -> dict[str, Any]:
        """Return detached evidence suitable for a JSON column or API DTO."""

        return {
            "terminal_status": self.terminal_status,
            "status": self.terminal_status,
            "exit_classification": self.exit_classification.as_dict(),
            "marker_validation": self.marker_validation.as_dict(),
            "integrity_validation": self.integrity_validation.as_dict(),
            "terminal_decision_reason": self.terminal_decision_reason,
            "failure_phase": self.failure_phase,
            "failure_type": self.failure_type,
            "preserve_evidence": self.preserve_evidence,
            "errors": list(self.errors),
        }


def _evidence_value(evidence: Any, name: str, default: Any = None) -> Any:
    if isinstance(evidence, Mapping):
        return evidence.get(name, default)
    return getattr(evidence, name, default)


def _is_forced_termination(evidence: Any) -> bool:
    """Extract only explicit force evidence; a Supervisor reason is not proof."""

    if evidence is None:
        return False
    if isinstance(evidence, bool):
        return evidence
    return bool(
        _evidence_value(evidence, "forced", False)
        or _evidence_value(evidence, "force_kill", False)
        or _evidence_value(evidence, "force_kill_sent", False)
    )


def _counts_from(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        candidate = value.get("counts", value.get("result_counts"))
    else:
        candidate = getattr(value, "counts", getattr(value, "result_counts", None))
    return candidate if isinstance(candidate, Mapping) else None


def _digest_from(value: Any) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get("digest", value.get("actual_digest"))
    else:
        candidate = getattr(value, "digest", getattr(value, "actual_digest", None))
    return candidate if isinstance(candidate, str) else None


def _integrity_validation(
    marker: Mapping[str, Any] | None,
    evidence: Any,
) -> IntegrityVerification:
    """Normalize all accepted integrity adapter shapes to one result type."""

    marker_integrity = marker.get("result_integrity") if isinstance(marker, Mapping) else None
    expected_digest = (
        marker_integrity.get("digest")
        if isinstance(marker_integrity, Mapping)
        else None
    )
    marker_counts = marker.get("result_counts") if isinstance(marker, Mapping) else None
    expected_counts: dict[str, Any] = (
        {key: marker_counts.get(key) for key in RESULT_COUNT_KEYS}
        if isinstance(marker_counts, Mapping)
        else {}
    )

    if isinstance(evidence, IntegrityVerification):
        return evidence

    status = _evidence_value(evidence, "status", None)
    valid = bool(_evidence_value(evidence, "valid", _evidence_value(evidence, "passed", False)))
    actual_digest = _digest_from(evidence)
    actual_counts = _counts_from(evidence)
    normalized_counts: dict[str, Any] = (
        {key: actual_counts.get(key) for key in RESULT_COUNT_KEYS}
        if actual_counts is not None
        else {}
    )
    raw_errors = _evidence_value(evidence, "errors", ()) or ()
    errors: list[str] = [raw_errors] if isinstance(raw_errors, str) else list(raw_errors)
    if evidence is None or evidence is False:
        errors.append("result integrity is unavailable")
        status = "unavailable"
    if not isinstance(actual_digest, str):
        errors.append("result integrity digest is invalid or missing")
    if actual_counts is None or set(actual_counts) != set(RESULT_COUNT_KEYS):
        errors.append("result integrity counts are invalid or incomplete")
    elif any(
        isinstance(actual_counts[key], bool)
        or not isinstance(actual_counts[key], int)
        or actual_counts[key] < 0
        for key in RESULT_COUNT_KEYS
    ):
        errors.append("result integrity counts contain invalid values")
    if expected_digest != actual_digest:
        errors.append("result integrity digest mismatch")
    if expected_counts != normalized_counts:
        errors.append("result integrity counts mismatch")
    if not errors and not valid:
        errors.append("result integrity is not proven")
    return IntegrityVerification(
        valid=valid and not errors,
        expected_digest=expected_digest,
        actual_digest=actual_digest,
        expected_counts=expected_counts,
        actual_counts=normalized_counts,
        errors=tuple(str(error) for error in errors),
        status_value=("passed" if valid and not errors else status or "failed"),
        algorithm=_evidence_value(evidence, "algorithm", RESULT_INTEGRITY_ALGORITHM),
        canonicalization=_evidence_value(
            evidence, "canonicalization", RESULT_INTEGRITY_CANONICALIZATION
        ),
        scope=_evidence_value(evidence, "scope", RESULT_INTEGRITY_SCOPE),
        covered_tables=tuple(
            _evidence_value(evidence, "covered_tables", COVERED_RESULT_TABLES)
            or COVERED_RESULT_TABLES
        ),
    )


def reconcile_terminal_evidence(
    run_id: UUID | str,
    raw_exit_code: int | None = None,
    signal_number: int | None = None,
    completion_marker: Mapping[str, Any] | None = None,
    recomputed_integrity: Any = None,
    expected_config_hash: str | None = None,
    termination_evidence: Any = None,
    **aliases: Any,
) -> TerminalDecision:
    """Reconcile exit, marker, and complete result evidence without writes.

    ``marker``, ``exit_code``, and ``integrity`` aliases are accepted only to
    keep existing Supervisor adapters source-compatible.  They are normalized
    immediately into the canonical argument names above.  No argument is
    mutated and no repository/process callback is invoked.
    """

    if completion_marker is None and "marker" in aliases:
        completion_marker = aliases["marker"]
    if raw_exit_code is None and "exit_code" in aliases:
        raw_exit_code = aliases["exit_code"]
    if recomputed_integrity is None:
        recomputed_integrity = aliases.get("integrity", aliases.get("integrity_evidence"))
    if expected_config_hash is None:
        expected_config_hash = aliases.get("config_hash")
    if termination_evidence is None:
        termination_evidence = aliases.get("termination")

    if not isinstance(completion_marker, Mapping):
        as_dict = getattr(completion_marker, "as_dict", None)
        if callable(as_dict):
            completion_marker = as_dict()

    marker_validation = validate_completion_marker(
        completion_marker,
        run_id=run_id,
        config_hash=expected_config_hash,
    )
    config_hash_valid = isinstance(expected_config_hash, str) and bool(
        _CONFIG_HASH_RE.fullmatch(expected_config_hash)
    )
    if not config_hash_valid:
        # A digest cannot be proven without the frozen run configuration.  Do
        # not let a matching row hash accidentally turn a missing root value
        # into a determinate terminal state.
        marker_validation = MarkerValidation(
            False,
            marker_validation.errors + ("expected config_hash is missing or invalid",),
            marker_validation.marker,
        )
    integrity_validation = _integrity_validation(
        completion_marker, recomputed_integrity
    )
    forced = _is_forced_termination(termination_evidence)
    # A signal is never interpreted as a normal business exit.  Keep the raw
    # value separately in the classification while making the evaluator see a
    # missing code, which makes the unmapped branch explicit and conservative.
    evaluator_exit_code = None if signal_number is not None else raw_exit_code
    evaluation = evaluate_terminal(
        marker=completion_marker,
        exit_code=evaluator_exit_code,
        integrity=integrity_validation,
        run_id=run_id,
        config_hash=expected_config_hash,
        forced=forced,
    )
    exit_classification = map_runner_exit_code(raw_exit_code, signal_number=signal_number)

    failure_phase = (
        completion_marker.get("failure_phase")
        if isinstance(completion_marker, Mapping)
        else None
    )
    failure_type = (
        completion_marker.get("failure_type")
        if isinstance(completion_marker, Mapping)
        else None
    )
    if failure_phase is None:
        failure_phase = _evidence_value(termination_evidence, "failure_phase")
    if failure_type is None:
        failure_type = _evidence_value(termination_evidence, "failure_type")

    # ``evaluate_terminal`` owns the frozen truth table and stable reason
    # strings.  Keep an explicit signal reason here because its raw code may be
    # absent and therefore cannot be inferred from ``map_exit_code`` alone.
    terminal_status = evaluation.status
    reason = evaluation.reason
    if signal_number is not None:
        reason = "runner_exit_code_unmapped"
    if not config_hash_valid:
        terminal_status = "indeterminate"
        reason = "config_hash_unavailable"
    return TerminalDecision(
        terminal_status=terminal_status,
        exit_classification=exit_classification,
        marker_validation=marker_validation,
        integrity_validation=integrity_validation,
        terminal_decision_reason=reason,
        failure_phase=failure_phase,
        failure_type=failure_type,
        preserve_evidence=True,
    )


# Short aliases used by integrations that refer to the operation as a
# ``terminal_reconcile`` rather than a ``run supervision adapter``.
reconcile_terminal = reconcile_terminal_evidence


__all__ = [
    "TerminalDecision",
    "reconcile_terminal",
    "reconcile_terminal_evidence",
]
