"""Run-failure and success finalization boundary for the analyzer subsystem.

This module is the single agreed landing point for run-level analysis
persistence (task package 06 section 10).  The deterministic runner stays
fail-fast and database-free; this boundary owns everything that happens
around ``runner.run_steps``:

* :class:`AnalysisFailureSnapshot` -- immutable evidence bundle produced by
  ``DeterministicBacktestRunner.build_analysis_failure_snapshot()``;
* :class:`AnalysisFinalizationCoordinator.execute_steps(...)` -- slice
  executor that returns the runner's own result on success while
  persisting the matching analysis state (``partial`` progress or frozen
  ``final`` metrics), and on an agreed valuation-blocked failure persists
  the already determined analysis before re-raising the original
  exception;
* :class:`AnalysisFinalizer` -- opens its own SQLAlchemy Session via the
  injected ``session_factory`` and writes summaries/metric rows in exactly
  one independent transaction per call.

A ``session_factory`` is mandatory whenever the runner carries an analyzer
engine: skipping persistence silently would let a "successful" run leave
no queryable results at all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.backtesting.analysis_inputs import EquityObservation, canonical_evidence_json
from app.backtesting.analyzers import (
    AnalysisStatus,
    AnalysisSnapshot,
    AnalyzerEngine,
    compute_terminal_fingerprint,
)
from app.backtesting.domain import DomainValidationError
from app.backtesting.result_models import BacktestAnalysisSummaryRecord
from app.backtesting.runtime import ValuationBlockedError

__all__ = [
    "ABORTED_ERROR_TYPES",
    "ANALYSIS_EQUIVALENCE_EXCLUDED_FIELDS",
    "ANALYSIS_FINALIZATION_CURSOR_SIGNING_KEY",
    "AnalysisFailureSnapshot",
    "AnalysisFinalizationCoordinator",
    "AnalysisFinalizationError",
    "AnalysisFinalizationResult",
    "AnalysisFinalizer",
    "analysis_equivalence_projection",
    "unwrap_valuation_blocked_error",
]


#: v1 deliberately has no additional catch-all error set.  Only
#: ``ValuationBlockedError`` (including its wrapped cause chain) enters the
#: aborted transition; configuration, persistence, and programming failures
#: propagate unchanged.
ABORTED_ERROR_TYPES: tuple[type[BaseException], ...] = ()

# Full-run/chunked-run equivalence intentionally excludes orchestration and
# wall-clock audit fields.  All formula/input evidence, terminal metrics,
# events, counts and accounting aggregates remain comparable.
ANALYSIS_EQUIVALENCE_EXCLUDED_FIELDS = frozenset(
    {
        "chunk_sequence",
        "analysis_chunk_token",
        "last_chunk_sequence",
        "last_chunk_token",
        "created_at",
        "updated_at",
        "finalized_at",
        "partial_checkpoint_count",
    }
)

#: The write-only finalization repository never builds cursors; a fixed
#: non-blank key satisfies the repository's construction contract without
#: introducing configuration surface.
ANALYSIS_FINALIZATION_CURSOR_SIGNING_KEY = "internal:analysis-finalization"


class AnalysisFinalizationError(DomainValidationError):
    """Raised when the independent persistence transaction fails.

    The coordinator exposes the original run failure as ``__cause__`` for
    aborted runs. The independent transaction failure remains available via
    ``persistence_error`` for operator diagnostics and retry decisions.
    """

    persistence_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class AnalysisFinalizationResult:
    """Outcome of one successful terminal persistence."""

    status: str
    persisted_metric_count: int
    summary_id: UUID


def analysis_equivalence_projection(result: Any, analysis_snapshot: Any) -> dict[str, Any]:
    """Return the frozen fields that must match across chunk boundaries.

    Partial checkpoint count, tokens/sequences and audit timestamps are
    orchestration evidence and are excluded by contract.  Event order,
    terminal metric content, signatures, counts and rate evidence are not.
    """

    counts = analysis_snapshot.summary_counts()
    metrics = getattr(result, "analysis_metrics", ())
    initial_equity_evidence = json.loads(
        canonical_evidence_json(
            analysis_snapshot.initial_equity_snapshot.evidence_payload()
        )
    )
    return {
        "run_id": getattr(result, "run_id", None),
        "analysis_status": getattr(result, "analysis_status", None),
        "events": tuple(getattr(result, "events", ())),
        "equity_curve": tuple(getattr(result, "equity_curve", ())),
        "final_snapshot": getattr(result, "final_snapshot", None),
        # The complete E0 evidence is explicit in the fixed projection rather
        # than represented only by its digest. It includes the authoritative
        # portfolio id/hash, held-mark PIT provenance, and formal timeline.
        "initial_equity_evidence": initial_equity_evidence,
        "formula_signature": analysis_snapshot.formula_signature(),
        "input_evidence_signature": analysis_snapshot.input_evidence_signature(),
        "formal_timeline": analysis_snapshot.formal_timeline.as_payload(),
        "summary_counts": counts,
        "rate_snapshot_hash": getattr(
            getattr(analysis_snapshot, "rate_snapshot", None),
            "snapshot_hash",
            None,
        ),
        "metrics": tuple(
            (
                metric.metric_key,
                metric.formula_version,
                metric.analyzer_key,
                metric.analyzer_version,
                metric.status.value,
                metric.value,
                metric.unit,
                metric.sample_count,
                metric.unavailable_reason,
                canonical_evidence_json(dict(metric.analyzer_metadata)),
            )
            for metric in metrics
        ),
    }


def unwrap_valuation_blocked_error(
    exc: BaseException,
) -> ValuationBlockedError | None:
    """Find the agreed abort trigger inside an exception chain.

    The runner wraps phase failures into ``PhaseExecutionError``, so the
    original ``ValuationBlockedError`` usually appears as the cause.  The
    structured ``error_type`` string is deliberately ignored: a caller must
    provide an actual exception instance in the chain, never merely a forged
    type name.
    """

    pending: list[BaseException] = [exc]
    visited: set[int] = set()
    while pending and len(visited) < 16:
        current = pending.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, ValuationBlockedError):
            return current
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return None


@dataclass(frozen=True, slots=True)
class AnalysisFailureSnapshot:
    """Immutable bundle of everything known after a mid-run failure."""

    run_id: str
    failed_step_sequence: int
    error_type: str
    error_message: str
    analysis_snapshot: Any
    # Only immutable analyzer state crosses the failure boundary. Keeping a
    # live engine here would allow later mutations to change failure evidence.
    admission_token: object
    formula_signature: str
    input_evidence_signature: str
    valid_day_count: int
    fill_count: int
    snapshot_binding: str
    failed_session_date: date | None = None
    blocked_equity_observation: EquityObservation | None = None
    terminal_fingerprint: str | None = None
    last_chunk_sequence: int | None = None
    last_chunk_token: str | None = None
    completed_through_session: date | None = None

    def __post_init__(self) -> None:
        present = (
            self.last_chunk_sequence is not None,
            self.last_chunk_token is not None,
            self.completed_through_session is not None,
        )
        if any(present) and not all(present):
            raise AnalysisFinalizationError(
                "failure checkpoint sequence, token, and session must be paired"
            )
        if self.last_chunk_sequence is not None and (
            isinstance(self.last_chunk_sequence, bool)
            or not isinstance(self.last_chunk_sequence, int)
            or self.last_chunk_sequence < 0
        ):
            raise AnalysisFinalizationError(
                "failure checkpoint sequence must be a non-negative integer"
            )
        if self.last_chunk_token is not None and (
            not isinstance(self.last_chunk_token, str)
            or len(self.last_chunk_token) != 71
            or not self.last_chunk_token.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in self.last_chunk_token[7:])
        ):
            raise AnalysisFinalizationError(
                "failure checkpoint token must be sha256:<64 lowercase hex digits>"
            )
        if self.completed_through_session is not None and (
            isinstance(self.completed_through_session, datetime)
            or not isinstance(self.completed_through_session, date)
        ):
            raise AnalysisFinalizationError(
                "failure completed session must be a calendar date"
            )


class _SessionFactory(Protocol):
    def __call__(self) -> Session: ...


def _require_success_checkpoint(
    *,
    sequence: int | None,
    token: str | None,
    completed_session: date | None,
) -> None:
    """Validate the checkpoint emitted by one fully successful runtime slice."""

    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or not isinstance(completed_session, date)
        or isinstance(completed_session, datetime)
        or not isinstance(token, str)
        or len(token) != 71
        or not token.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in token[7:])
    ):
        raise AnalysisFinalizationError(
            "successful analysis persistence requires a non-negative sequence, "
            "sha256 token, and completed calendar session"
        )


def _metric_result_to_dto(result: Any, run_uuid: UUID) -> Any:
    """Convert an analyzer MetricResult into its persistence DTO."""

    from app.backtesting.result_models import BacktestMetricRecord

    metadata = dict(result.analyzer_metadata or {})
    return BacktestMetricRecord(
        run_id=run_uuid,
        metric_key=result.metric_key,
        formula_version=result.formula_version,
        value=result.value,
        unit=result.unit,
        sample_count=result.sample_count,
        unavailable_reason=result.unavailable_reason,
        annualization_factor=metadata.get("annualization_factor"),
        risk_free_rate_note=metadata.get("risk_free_rate_note"),
        analyzer_key=result.analyzer_key,
        analyzer_version=result.analyzer_version,
        analyzer_metadata=metadata,
    )


def _run_uuid_of(run_id: str) -> UUID:
    try:
        return UUID(run_id)
    except ValueError as exc:
        raise AnalysisFinalizationError(
            f"run_id {run_id!r} is not a UUID; formal result rows require "
            "UUID run identities"
        ) from exc


def _rate_evidence(analysis_snapshot: Any) -> dict[str, Any]:
    """JSON-safe rate evidence block extracted from a partial snapshot."""

    rate_snapshot = analysis_snapshot.rate_snapshot
    if rate_snapshot is None:
        return {
            "rate_snapshot": None,
            "rate_snapshot_hash": None,
            "rate_source_versions": None,
            "missing_ranges": None,
        }
    return {
        "rate_snapshot": {
            "contract_version": "pit_rate_snapshot_v1",
            "rate_unit": rate_snapshot.rate_unit,
            "rate_convention": rate_snapshot.rate_convention,
            "effective_at": rate_snapshot.effective_at,
            "session_mapping": rate_snapshot.session_mapping,
            "data_cutoff_semantics": rate_snapshot.data_cutoff_semantics,
            "cutoff_boundary": rate_snapshot.cutoff_boundary,
            "rates": {
                day.isoformat(): format(rate, "f")
                for day, rate in sorted(rate_snapshot.rates.items())
            },
            "coverage_start": (
                rate_snapshot.coverage_start.isoformat()
                if rate_snapshot.coverage_start is not None
                else None
            ),
            "coverage_end": (
                rate_snapshot.coverage_end.isoformat()
                if rate_snapshot.coverage_end is not None
                else None
            ),
            "expected_sessions": [
                day.isoformat() for day in rate_snapshot.expected_sessions
            ],
            "query_parameters": dict(rate_snapshot.query_parameters),
            "fact_evidence": {
                day: json.loads(canonical_evidence_json(dict(provenance)))
                for day, provenance in sorted(rate_snapshot.fact_evidence.items())
            },
        },
        "rate_snapshot_hash": rate_snapshot.snapshot_hash,
        "rate_source_versions": {
            "source_key": rate_snapshot.source_key,
            "source_version": rate_snapshot.source_version,
        },
        "missing_ranges": [
            {
                "start_session": start.isoformat(),
                "end_session": end.isoformat(),
            }
            for start, end in (rate_snapshot.missing_ranges or ())
        ],
    }


class AnalysisFinalizer:
    """Persist analyzer summaries and metrics in independent transactions."""

    # -- shared internals ---------------------------------------------------

    def _persist(
        self,
        summary: BacktestAnalysisSummaryRecord,
        metric_dtos: list[Any],
        session_factory: _SessionFactory,
    ) -> tuple[UUID, int]:
        """Write one summary plus its metrics in one new transaction.

        Returns the persisted summary's id; values are captured before
        commit because the ORM instance is detached afterwards.
        """

        session = None
        try:
            session = session_factory()
            from app.backtesting.result_repository import BacktestResultRepository

            repository = BacktestResultRepository(
                session,
                cursor_signing_key=ANALYSIS_FINALIZATION_CURSOR_SIGNING_KEY,
            )
            persisted = repository.upsert_analysis_summary(summary)
            summary_id = persisted.id
            written = repository.append_metrics(*metric_dtos)
            session.commit()
        except Exception as exc:
            # A failed independent transaction must roll back cleanly and
            # surface as a stable finalization error.
            try:
                session.rollback()
            except Exception:
                pass
            raise AnalysisFinalizationError(
                f"the analysis persistence transaction failed: {exc}"
            ) from exc
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
        return summary_id, written

    def _base_summary_fields(
        self,
        analysis_snapshot: Any,
        run_uuid: UUID,
    ) -> dict[str, Any]:
        counts = analysis_snapshot.summary_counts()
        now = datetime.now(timezone.utc)
        registry_snapshot = json.loads(
            canonical_evidence_json(analysis_snapshot.registry_snapshot)
        )
        fields: dict[str, Any] = {
            "run_id": run_uuid,
            "analyzer_snapshot": {
                "specs": [
                    spec.describe() for spec in analysis_snapshot.specs
                ],
                **registry_snapshot,
                "formula_signature": analysis_snapshot.formula_signature(),
                "formal_timeline": analysis_snapshot.formal_timeline.as_payload(),
            },
            "formal_timeline": analysis_snapshot.formal_timeline.as_payload(),
            "formula_signature": analysis_snapshot.formula_signature(),
            "input_evidence_signature": (
                analysis_snapshot.input_evidence_signature()
            ),
            "reporting_currency": analysis_snapshot.reporting_currency,
            "initial_equity": counts.get("initial_equity"),
            "valid_day_count": counts.get("valid_day_count"),
            "candidate_return_count": counts.get("candidate_return_count"),
            "fill_count": counts.get("fill_count"),
            "gross_traded_notional": counts.get("gross_traded_notional"),
            "cumulative_fees": counts.get("cumulative_fees"),
            "created_at": now,
            "updated_at": now,
        }
        fields.update(_rate_evidence(analysis_snapshot))
        return fields

    # -- success paths --------------------------------------------------------

    def persist_partial(
        self,
        analysis_snapshot: Any,
        *,
        session_factory: _SessionFactory,
        last_chunk_sequence: int | None = None,
        last_chunk_token: str | None = None,
        completed_through_session: date | None = None,
    ) -> UUID:
        """Upsert a ``partial`` progress summary without writing metrics.

        Partial checkpoints never touch the final metric table; only the
        summary's progress fields and signatures are refreshed.
        """

        from app.backtesting.analyzers import _is_admission_token_valid

        _require_success_checkpoint(
            sequence=last_chunk_sequence,
            token=last_chunk_token,
            completed_session=completed_through_session,
        )

        if not isinstance(analysis_snapshot, AnalysisSnapshot):
            raise AnalysisFinalizationError(
                "partial persistence requires an AnalysisSnapshot"
            )

        if getattr(analysis_snapshot, "status", None) != AnalysisStatus.PARTIAL:
            raise AnalysisFinalizationError(
                "only a partial analysis snapshot may be persisted as partial"
            )
        admission_token = getattr(analysis_snapshot, "admission_token", None)
        if not _is_admission_token_valid(
            admission_token,
            run_id=getattr(analysis_snapshot, "run_id", None),
            initial_equity_hash=getattr(
                analysis_snapshot.initial_equity_snapshot,
                "evidence_hash",
                None,
            ),
            rate_snapshot_hash=getattr(
                getattr(analysis_snapshot, "rate_snapshot", None),
                "snapshot_hash",
                None,
            ),
            analysis_snapshot=analysis_snapshot,
            require_failure_binding=False,
            require_snapshot_binding=True,
        ):
            raise AnalysisFinalizationError(
                "the partial analysis snapshot is not bound to a coordinator "
                "admission"
            )
        now = datetime.now(timezone.utc)
        summary = BacktestAnalysisSummaryRecord(
            **{
                **self._base_summary_fields(
                    analysis_snapshot, _run_uuid_of(analysis_snapshot.run_id)
                ),
                "status": AnalysisStatus.PARTIAL.value,
                "last_chunk_sequence": last_chunk_sequence,
                "last_chunk_token": last_chunk_token,
                "completed_through_session": completed_through_session,
                "abort_reason": None,
                "failed_step_sequence": None,
                "finalized_at": None,
                "updated_at": now,
            }
        )
        persisted_id, _ = self._persist(summary, [], session_factory)
        return persisted_id

    def persist_final(
        self,
        engine: AnalyzerEngine,
        *,
        session_factory: _SessionFactory,
        completed_through_session: date | None = None,
        last_chunk_sequence: int | None = None,
        last_chunk_token: str | None = None,
    ) -> AnalysisFinalizationResult:
        """Write the frozen ``final`` summary and its complete metrics."""

        from app.backtesting.analyzers import _is_coordinator_admitted

        _require_success_checkpoint(
            sequence=last_chunk_sequence,
            token=last_chunk_token,
            completed_session=completed_through_session,
        )

        admission_token = getattr(engine, "_admission_token", None)
        if not _is_coordinator_admitted(engine, admission_token):
            raise AnalysisFinalizationError(
                "the analyzer engine was not created by the run-admission "
                "coordinator; refusing to persist analysis results"
            )
        status = engine.finalized_status
        if status is not AnalysisStatus.FINAL:
            raise AnalysisFinalizationError(
                "the analyzer engine has not been finalized as final; "
                "refusing to persist non-terminal results"
            )
        analysis_snapshot = engine.snapshot()
        now = datetime.now(timezone.utc)
        summary = BacktestAnalysisSummaryRecord(
            **{
                **self._base_summary_fields(analysis_snapshot, _run_uuid_of(analysis_snapshot.run_id)),
                "status": AnalysisStatus.FINAL.value,
                "last_chunk_sequence": last_chunk_sequence,
                "last_chunk_token": last_chunk_token,
                "completed_through_session": completed_through_session,
                "abort_reason": None,
                "failed_step_sequence": None,
                "terminal_fingerprint": engine.terminal_fingerprint,
                "finalized_at": now,
                "updated_at": now,
            }
        )
        run_uuid = _run_uuid_of(analysis_snapshot.run_id)
        metric_dtos = [
            _metric_result_to_dto(result, run_uuid)
            for result in (engine.final_results or ())
        ]
        persisted_id, written = self._persist(summary, metric_dtos, session_factory)
        return AnalysisFinalizationResult(
            status=AnalysisStatus.FINAL.value,
            persisted_metric_count=written,
            summary_id=persisted_id,
        )

    # -- abort path -------------------------------------------------------------

    def finalize_aborted(
        self,
        snapshot: AnalysisFailureSnapshot,
        session_factory: _SessionFactory,
    ) -> AnalysisFinalizationResult:
        """Write the aborted summary and metrics in one new transaction.

        A repeated invocation is idempotent at both layers: an engine that
        was already finalized as ``aborted`` reuses its frozen results, and
        the repository's terminal-state protection rejects any conflicting
        overwrite while accepting identical retries.
        """

        if not isinstance(snapshot, AnalysisFailureSnapshot):
            raise AnalysisFinalizationError(
                "aborted finalization requires an AnalysisFailureSnapshot"
            )
        if (
            not isinstance(snapshot.run_id, str)
            or not snapshot.run_id.strip()
            or not isinstance(snapshot.error_message, str)
            or not snapshot.error_message.strip()
            or not isinstance(snapshot.error_type, str)
            or not snapshot.error_type.strip()
            or isinstance(snapshot.failed_step_sequence, bool)
            or not isinstance(snapshot.failed_step_sequence, int)
            or snapshot.failed_step_sequence < 0
            or isinstance(snapshot.valid_day_count, bool)
            or not isinstance(snapshot.valid_day_count, int)
            or snapshot.valid_day_count < 0
            or isinstance(snapshot.fill_count, bool)
            or not isinstance(snapshot.fill_count, int)
            or snapshot.fill_count < 0
            or not isinstance(snapshot.formula_signature, str)
            or not snapshot.formula_signature.strip()
            or not isinstance(snapshot.input_evidence_signature, str)
            or not snapshot.input_evidence_signature.strip()
            or (
                snapshot.failed_session_date is not None
                and not isinstance(snapshot.failed_session_date, date)
            )
            or not isinstance(snapshot.snapshot_binding, str)
            or not snapshot.snapshot_binding.strip()
            or not isinstance(snapshot.terminal_fingerprint, str)
            or not snapshot.terminal_fingerprint.strip()
        ):
            raise AnalysisFinalizationError(
                "aborted finalization carries an invalid failure envelope"
            )

        from app.backtesting.analyzers import _is_admission_token_valid

        analysis_snapshot = snapshot.analysis_snapshot
        if not isinstance(analysis_snapshot, AnalysisSnapshot):
            raise AnalysisFinalizationError(
                "aborted finalization requires an AnalysisSnapshot"
            )
        if analysis_snapshot.admission_token is not snapshot.admission_token:
            raise AnalysisFinalizationError(
                "the failure snapshot admission token does not match its "
                "analysis snapshot"
            )
        if not isinstance(snapshot.blocked_equity_observation, EquityObservation):
            raise AnalysisFinalizationError(
                "aborted finalization requires a valid blocked equity observation"
            )
        if (
            isinstance(snapshot.failed_session_date, datetime)
            or snapshot.failed_session_date
            != snapshot.blocked_equity_observation.session_date
            or snapshot.failed_step_sequence
            != snapshot.blocked_equity_observation.step_sequence
        ):
            raise AnalysisFinalizationError(
                "aborted finalization carries a failure location that does "
                "not match the blocked observation"
            )
        if snapshot.terminal_fingerprint is None:
            raise AnalysisFinalizationError(
                "aborted finalization requires a precomputed terminal "
                "fingerprint"
            )
        try:
            formula_signature = analysis_snapshot.formula_signature()
            input_evidence_signature = analysis_snapshot.input_evidence_signature()
            summary_counts = analysis_snapshot.summary_counts()
        except Exception as exc:
            raise AnalysisFinalizationError(
                f"the failure snapshot cannot be verified: {exc}"
            ) from exc
        if snapshot.formula_signature != formula_signature:
            raise AnalysisFinalizationError(
                "the failure snapshot carries a conflicting formula signature"
            )
        if snapshot.input_evidence_signature != input_evidence_signature:
            raise AnalysisFinalizationError(
                "the failure snapshot carries a conflicting input evidence signature"
            )
        if snapshot.valid_day_count != summary_counts.get("valid_day_count"):
            raise AnalysisFinalizationError(
                "the failure snapshot carries a conflicting valid-day count"
            )
        if snapshot.fill_count != summary_counts.get("fill_count"):
            raise AnalysisFinalizationError(
                "the failure snapshot carries a conflicting fill count"
            )
        observations = tuple(analysis_snapshot.equity_observations)
        expected_blocked_observation = (
            observations[-1]
            if observations and not observations[-1].is_valid
            else None
        )
        if snapshot.blocked_equity_observation != expected_blocked_observation:
            raise AnalysisFinalizationError(
                "the failure snapshot carries a blocked observation that "
                "differs from the frozen analysis timeline"
            )
        failure_envelope = {
            "error_message": snapshot.error_message,
            "error_type": snapshot.error_type,
            "failed_step_sequence": snapshot.failed_step_sequence,
            "failed_session_date": (
                snapshot.failed_session_date.isoformat()
                if snapshot.failed_session_date is not None
                else None
            ),
            "blocked_equity_observation": snapshot.blocked_equity_observation.evidence_payload(),
            "last_chunk_sequence": snapshot.last_chunk_sequence,
            "last_chunk_token": snapshot.last_chunk_token,
            "completed_through_session": (
                snapshot.completed_through_session.isoformat()
                if snapshot.completed_through_session is not None
                else None
            ),
        }
        if not _is_admission_token_valid(
            snapshot.admission_token,
            run_id=snapshot.run_id,
            initial_equity_hash=getattr(
                snapshot.analysis_snapshot.initial_equity_snapshot,
                "evidence_hash",
                None,
            ),
            rate_snapshot_hash=getattr(
                getattr(analysis_snapshot, "rate_snapshot", None),
                "snapshot_hash",
                None,
            ),
            analysis_snapshot=analysis_snapshot,
            failure_snapshot_binding=snapshot.snapshot_binding,
            failure_envelope=failure_envelope,
            terminal_fingerprint=snapshot.terminal_fingerprint,
        ):
            raise AnalysisFinalizationError(
                "the failure snapshot differs from its coordinator-bound "
                "admission or runtime evidence"
            )
        status_before = snapshot.analysis_snapshot.status
        status_value = getattr(status_before, "value", status_before)
        if status_value == AnalysisStatus.FINAL.value:
            # A run already finalized as final must never be rewritten as
            # aborted; this is a hard conflict, not an idempotent retry.
            raise AnalysisFinalizationError(
                "the analyzer engine was already finalized as final; "
                "refusing to persist aborted results over it"
            )
        if status_value not in (
            AnalysisStatus.PARTIAL.value,
            AnalysisStatus.ABORTED.value,
        ):
            raise AnalysisFinalizationError(
                "aborted finalization requires a partial or aborted analysis "
                "snapshot"
            )
        failure_payload = {
            "abort_reason": snapshot.error_message,
            "failed_step_sequence": snapshot.failed_step_sequence,
            "failed_session_date": (
                snapshot.failed_session_date.isoformat()
                if snapshot.failed_session_date is not None
                else None
            ),
            "error_type": snapshot.error_type,
        }
        if getattr(status_before, "value", status_before) == AnalysisStatus.ABORTED.value:
            # Idempotent retry path: the recorded failure evidence must
            # match the replay exactly, otherwise this is a conflicting
            # write against a terminal state.
            recorded_failure = snapshot.analysis_snapshot.failure
            if not isinstance(recorded_failure, Mapping):
                raise AnalysisFinalizationError(
                    "aborted analysis snapshot carries invalid failure evidence"
                )
            recorded = dict(recorded_failure)
            if recorded != failure_payload:
                raise AnalysisFinalizationError(
                    "conflicting aborted retry: the persisted failure evidence "
                    "differs from the replayed evidence"
                )
            results = snapshot.analysis_snapshot.compute_provisional_results()
        else:
            try:
                results = snapshot.analysis_snapshot.compute_provisional_results()
            except Exception as exc:
                # Real metric-computation failures are never disguised as
                # idempotent retries.
                raise AnalysisFinalizationError(
                    f"aborted metric computation failed: {exc}"
                ) from exc
        run_uuid = _run_uuid_of(snapshot.run_id)

        blocked_payload = (
            snapshot.blocked_equity_observation.evidence_payload()
            if snapshot.blocked_equity_observation is not None
            else None
        )
        if blocked_payload is not None:
            # Evidence payloads carry domain types (dates, Decimals);
            # render them through the canonical JSON contract first.
            import json

            from app.backtesting.analysis_inputs import canonical_evidence_json

            blocked_payload = json.loads(
                canonical_evidence_json(blocked_payload)
            )
        terminal_fingerprint = compute_terminal_fingerprint(
            status=AnalysisStatus.ABORTED,
            analysis_snapshot=analysis_snapshot,
            results=results,
            failure=failure_payload,
        )
        if snapshot.terminal_fingerprint != terminal_fingerprint:
            raise AnalysisFinalizationError(
                "the immutable failure snapshot carries a conflicting terminal "
                "fingerprint"
            )
        base_fields = self._base_summary_fields(analysis_snapshot, run_uuid)
        specs_block = base_fields.pop("analyzer_snapshot")
        specs_block["blocked_equity_observation"] = blocked_payload
        now = datetime.now(timezone.utc)
        summary = BacktestAnalysisSummaryRecord(
            **{
                **base_fields,
                "analyzer_snapshot": specs_block,
                "status": AnalysisStatus.ABORTED.value,
                "last_chunk_sequence": snapshot.last_chunk_sequence,
                "last_chunk_token": snapshot.last_chunk_token,
                "completed_through_session": snapshot.completed_through_session,
                "abort_reason": snapshot.error_message,
                "failed_step_sequence": snapshot.failed_step_sequence,
                "terminal_fingerprint": terminal_fingerprint,
                "finalized_at": now,
                "updated_at": now,
            }
        )
        metric_dtos = [
            _metric_result_to_dto(result, run_uuid) for result in results
        ]
        persisted_id, written = self._persist(summary, metric_dtos, session_factory)
        return AnalysisFinalizationResult(
            status=AnalysisStatus.ABORTED.value,
            persisted_metric_count=written,
            summary_id=persisted_id,
        )


def _completed_session_of(runner: Any, result: Any) -> date | None:
    """Resolve the last completed step's official session from the axis."""

    sequence = getattr(result, "completed_through_step_sequence", None)
    axis = getattr(runner, "_axis", None)
    if sequence is None or axis is None:
        return None
    try:
        session_text = axis.at(sequence).metadata.get("session_date")
        return date.fromisoformat(session_text) if isinstance(session_text, str) else None
    except Exception:
        return None


class AnalysisFinalizationCoordinator:
    """Execute runner steps with full analysis-persistence semantics."""

    def __init__(
        self,
        *,
        finalizer: AnalysisFinalizer | None = None,
    ) -> None:
        self._finalizer = finalizer or AnalysisFinalizer()

    def execute_steps(
        self,
        runner: Any,
        steps: Sequence[Any],
        *,
        next_after_last: Any | None = None,
        session_factory: Callable[[], Session] | None = None,
    ):
        """Run one slice and persist the matching analysis state.

        Success returns the runner's own result untouched, but only after
        its analysis state was persisted: ``final`` runs get their frozen
        metrics plus the terminal summary, ``partial`` chunks refresh the
        progress summary without touching the metric table.  When the
        runner carries an analyzer engine, ``session_factory`` is
        mandatory — a successful run must never silently skip persistence.

        Only :class:`ValuationBlockedError`, directly or as the wrapped cause
        of a ``PhaseExecutionError``, enters failure finalization.  Every
        other exception propagates unchanged.
        """

        has_engine = getattr(runner, "_analysis_engine", None) is not None
        if has_engine and session_factory is None:
            raise DomainValidationError(
                "execute_steps requires a session_factory when the runner "
                "carries an analyzer engine; analysis results must always "
                "be persisted, never silently dropped"
            )

        try:
            result = runner.run_steps(steps, next_after_last=next_after_last)
        except BaseException as exc:
            blocked = unwrap_valuation_blocked_error(exc)
            if blocked is None:
                raise
            if not has_engine:
                # Legacy runners have no analyzer state to persist.  Preserve
                # the original valuation exception instead of manufacturing a
                # secondary "no analyzer engine" domain error.
                raise
            if not hasattr(runner, "build_analysis_failure_snapshot"):
                raise
            # Freeze the failure envelope before transitioning the analyzer so
            # the abort payload is derived from the original valuation error.
            failure_snapshot = runner.build_analysis_failure_snapshot(exc)
            # Advance the live engine to ``aborted`` exactly once, but keep
            # persistence based on a snapshot captured after this transition.
            # Capturing it before ``engine.finalize`` leaves a PARTIAL snapshot
            # bound to an ABORTED engine and invalidates the admission binding,
            # preventing the independent transaction from writing the abort.
            # A final engine is left untouched and is rejected as a conflict by
            # the finalizer.
            engine = getattr(runner, "_analysis_engine", None)
            if engine is not None and engine.finalized_status is None:
                failure_payload = {
                    "abort_reason": failure_snapshot.error_message,
                    "failed_step_sequence": failure_snapshot.failed_step_sequence,
                    "failed_session_date": (
                        failure_snapshot.failed_session_date.isoformat()
                        if failure_snapshot.failed_session_date is not None
                        else None
                    ),
                    "error_type": failure_snapshot.error_type,
                }
                try:
                    engine.finalize(
                        AnalysisStatus.ABORTED, failure=failure_payload
                    )
                except Exception as finalize_exc:
                    raise AnalysisFinalizationError(
                        f"aborted metric computation failed: {finalize_exc}"
                    ) from finalize_exc
            # Rebuild after the terminal transition so the snapshot binding
            # reflects the analyzer's ABORTED status and remains replayable.
            failure_snapshot = runner.build_analysis_failure_snapshot(exc)
            if session_factory is not None:
                try:
                    self._finalizer.finalize_aborted(
                        failure_snapshot, session_factory
                    )
                except AnalysisFinalizationError as persistence_exc:
                    wrapped = AnalysisFinalizationError(
                        "aborted analysis persistence failed; the original run "
                        f"failure is preserved as cause: {persistence_exc}"
                    )
                    wrapped.persistence_error = persistence_exc
                    raise wrapped from blocked
            raise

        analysis_status = getattr(result, "analysis_status", None)
        if analysis_status == "final":
            engine = runner._analysis_engine
            if engine is not None:
                self._finalizer.persist_final(
                    engine,
                    session_factory=session_factory,
                    completed_through_session=_completed_session_of(runner, result),
                    last_chunk_sequence=getattr(result, "chunk_sequence", None),
                    last_chunk_token=getattr(result, "analysis_chunk_token", None),
                )
        elif analysis_status == "partial":
            snapshot = getattr(runner, "_latest_analysis_snapshot", None)
            if snapshot is None:
                raise AnalysisFinalizationError(
                    "analyzer-enabled partial run produced no analysis snapshot"
                )
            self._finalizer.persist_partial(
                snapshot,
                session_factory=session_factory,
                last_chunk_sequence=getattr(
                    result, "chunk_sequence", None
                ),
                last_chunk_token=getattr(result, "analysis_chunk_token", None),
                completed_through_session=_completed_session_of(runner, result),
            )
        elif has_engine:
            # An analyzer-enabled runner must explicitly report one of the
            # two persistence states.  Silently returning a custom result
            # with ``analysis_status=None`` would recreate the old hole where
            # a successful run had no queryable analysis summary.
            raise AnalysisFinalizationError(
                "analyzer-enabled run returned an invalid analysis status"
            )
        return result
