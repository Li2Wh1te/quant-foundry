"""Run-failure finalization boundary for the metric analyzer subsystem.

This module is the single agreed landing point for ``aborted`` analysis
finalization (task package 06 section 10.2).  The deterministic runner
stays fail-fast; this boundary owns everything that happens *after* the
runner stopped:

* :class:`AnalysisFailureSnapshot` -- immutable evidence bundle produced by
  ``DeterministicBacktestRunner.build_analysis_failure_snapshot()``;
* :class:`AnalysisFinalizationCoordinator.execute_steps(...)` -- thin slice
  executor that returns the runner's own ``BacktestRunResult`` on success,
  and on an agreed valuation-blocked failure persists the already
  determined analysis through :class:`AnalysisFinalizer` before re-raising
  the original exception;
* :class:`AnalysisFinalizer.finalize_aborted(...)` -- opens its own
  SQLAlchemy Session via the injected ``session_factory`` and writes the
  ``aborted`` run summary plus the determined metric rows in exactly one
  independent transaction.

The coordinator never converts a failure into a normal run result, never
swallows the original exception, and never touches the runner's execution
transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.backtesting.analysis_inputs import EquityObservation
from app.backtesting.analyzers import (
    AnalysisStateConflictError,
    AnalysisStatus,
    AnalyzerEngine,
)
from app.backtesting.domain import DomainValidationError
from app.backtesting.result_models import BacktestAnalysisSummaryRecord
from app.backtesting.runtime import ValuationBlockedError

__all__ = [
    "ABORTED_ERROR_TYPES",
    "ANALYSIS_FINALIZATION_CURSOR_SIGNING_KEY",
    "AnalysisFailureSnapshot",
    "AnalysisFinalizationCoordinator",
    "AnalysisFinalizationError",
    "AnalysisFinalizationResult",
    "AnalysisFinalizer",
    "unwrap_valuation_blocked_error",
]


#: Unrecoverable error types, beyond ValuationBlockedError, whose runs are
#: finalized as aborted instead of being left without analysis evidence.
ABORTED_ERROR_TYPES: tuple[type[BaseException], ...] = ()

#: The write-only finalization repository never builds cursors; a fixed
#: non-blank key satisfies the repository's construction contract without
#: introducing configuration surface.
ANALYSIS_FINALIZATION_CURSOR_SIGNING_KEY = "internal:analysis-finalization"


class AnalysisFinalizationError(DomainValidationError):
    """Raised when the independent aborted-persistence transaction fails.

    The triggering persistence error stays attached as ``__cause__``; the
    original run failure is re-raised separately by the coordinator, so a
    failed finalization can never be mistaken for persisted state.
    """


@dataclass(frozen=True, slots=True)
class AnalysisFinalizationResult:
    """Outcome of one successful aborted finalization."""

    status: str
    persisted_metric_count: int
    summary_id: UUID


def unwrap_valuation_blocked_error(
    exc: BaseException,
) -> ValuationBlockedError | None:
    """Find the agreed abort trigger inside an exception chain.

    The runner wraps phase failures into ``PhaseExecutionError``, so the
    original ``ValuationBlockedError`` usually appears as the cause; both
    the direct type and the declared ``error_type`` name are accepted.
    """

    current: BaseException | None = exc
    for _ in range(16):
        if current is None:
            break
        if isinstance(current, ValuationBlockedError):
            return current
        error_type = getattr(current, "error_type", None)
        if isinstance(error_type, str) and error_type == (
            ValuationBlockedError.__name__
        ):
            return current  # type: ignore[return-value]
        current = current.__cause__
    return None


@dataclass(frozen=True, slots=True)
class AnalysisFailureSnapshot:
    """Immutable bundle of everything known after a mid-run failure."""

    run_id: str
    failed_step_sequence: int
    error_type: str
    error_message: str
    analysis_snapshot: Any
    analyzer_engine: AnalyzerEngine
    formula_signature: str
    input_evidence_signature: str
    valid_day_count: int
    fill_count: int
    failed_session_date: date | None = None
    blocked_equity_observation: EquityObservation | None = None


class _SessionFactory(Protocol):
    def __call__(self) -> Session: ...


class AnalysisFinalizer:
    """Persist determined analysis results of an aborted run."""

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

        engine = snapshot.analyzer_engine
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
        try:
            results = engine.finalize(
                AnalysisStatus.ABORTED, failure=failure_payload
            )
        except AnalysisStateConflictError:
            # Idempotent retry path: keep the results frozen by the first
            # finalization instead of recomputing or overwriting them.
            results = engine.final_results or ()
        try:
            run_uuid = UUID(snapshot.run_id)
        except ValueError as exc:
            raise AnalysisFinalizationError(
                f"run_id {snapshot.run_id!r} is not a UUID; formal result "
                "rows require UUID run identities"
            ) from exc

        try:
            session = session_factory()
            from app.backtesting.result_repository import BacktestResultRepository

            repository = BacktestResultRepository(
                session,
                cursor_signing_key=ANALYSIS_FINALIZATION_CURSOR_SIGNING_KEY,
            )
            summary = self._build_summary(snapshot, run_uuid)
            persisted = repository.upsert_analysis_summary(summary)
            metric_dtos = [
                _metric_result_to_dto(result, run_uuid) for result in results
            ]
            written = repository.append_metrics(*metric_dtos)
            session.commit()
        except Exception as exc:
            # A failed independent transaction must roll back cleanly and
            # surface as a stable finalization error; the original run
            # failure is re-raised separately by the coordinator.
            try:
                session.rollback()
            except Exception:
                pass
            raise AnalysisFinalizationError(
                f"the aborted analysis persistence transaction failed: {exc}"
            ) from exc
        return AnalysisFinalizationResult(
            status=AnalysisStatus.ABORTED.value,
            persisted_metric_count=written,
            summary_id=persisted.id,
        )

    def _build_summary(
        self,
        snapshot: AnalysisFailureSnapshot,
        run_uuid: UUID,
    ) -> BacktestAnalysisSummaryRecord:
        analysis_snapshot = snapshot.analysis_snapshot
        counts = analysis_snapshot.summary_counts()
        rate_snapshot = analysis_snapshot.rate_snapshot
        rate_payload: dict[str, Any] | None = None
        source_versions: dict[str, Any] | None = None
        missing_ranges: list[list[str]] | None = None
        if rate_snapshot is not None:
            rate_payload = {
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
                "query_parameters": dict(rate_snapshot.query_parameters),
            }
            source_versions = {
                "source_key": rate_snapshot.source_key,
                "source_version": rate_snapshot.source_version,
            }
            missing_ranges = [
                [start.isoformat(), end.isoformat()]
                for start, end in (rate_snapshot.missing_ranges or ())
            ]
        now = datetime.now(timezone.utc)
        blocked_payload = (
            snapshot.blocked_equity_observation.evidence_payload()
            if snapshot.blocked_equity_observation is not None
            else None
        )
        if blocked_payload is not None:
            # Evidence payloads carry domain types (dates, Decimals); render
            # them through the canonical JSON contract before persisting.
            from app.backtesting.analysis_inputs import (
                canonical_evidence_json,
            )

            import json as _json

            blocked_payload = _json.loads(
                canonical_evidence_json(blocked_payload)
            )
        return BacktestAnalysisSummaryRecord(
            run_id=run_uuid,
            status=AnalysisStatus.ABORTED.value,
            analyzer_snapshot={
                "specs": [
                    spec.describe() for spec in analysis_snapshot.specs
                ],
                "blocked_equity_observation": blocked_payload,
            },
            formula_signature=snapshot.formula_signature,
            input_evidence_signature=snapshot.input_evidence_signature,
            reporting_currency=analysis_snapshot.reporting_currency,
            initial_equity=counts.get("initial_equity"),
            valid_day_count=snapshot.valid_day_count,
            fill_count=snapshot.fill_count,
            gross_traded_notional=counts.get("gross_traded_notional"),
            cumulative_fees=counts.get("cumulative_fees"),
            rate_snapshot=rate_payload,
            rate_snapshot_hash=(
                rate_snapshot.snapshot_hash if rate_snapshot is not None else None
            ),
            rate_source_versions=source_versions,
            missing_ranges=missing_ranges,
            last_chunk_sequence=snapshot.failed_step_sequence,
            completed_through_session=None,
            abort_reason=snapshot.error_message,
            failed_step_sequence=snapshot.failed_step_sequence,
            created_at=now,
            updated_at=now,
            finalized_at=now,
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


class AnalysisFinalizationCoordinator:
    """Execute runner steps with aborted-finalization semantics."""

    def __init__(
        self,
        *,
        finalizer: AnalysisFinalizer | None = None,
        abort_on_error_types: Sequence[type[BaseException]] = (),
    ) -> None:
        self._finalizer = finalizer or AnalysisFinalizer()
        self._abort_on_error_types = tuple(abort_on_error_types)

    def execute_steps(
        self,
        runner: Any,
        steps: Sequence[Any],
        *,
        next_after_last: Any | None = None,
        session_factory: Callable[[], Session] | None = None,
    ):
        """Run one slice; persist aborted analysis before re-raising.

        Success returns the runner's own result untouched.  Only the agreed
        abort triggers (:class:`ValuationBlockedError`, directly or as the
        wrapped cause of a ``PhaseExecutionError``, plus the explicitly
        listed unrecoverable types) enter failure finalization; every other
        exception propagates unchanged.
        """

        try:
            return runner.run_steps(steps, next_after_last=next_after_last)
        except BaseException as exc:
            blocked = unwrap_valuation_blocked_error(exc)
            listed = isinstance(exc, self._abort_on_error_types)
            if blocked is None and not listed:
                raise
            if not hasattr(runner, "build_analysis_failure_snapshot"):
                raise
            failure_snapshot = runner.build_analysis_failure_snapshot(exc)
            if session_factory is not None:
                self._finalizer.finalize_aborted(
                    failure_snapshot, session_factory
                )
            raise
