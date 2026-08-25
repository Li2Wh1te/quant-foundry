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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.backtesting.analysis_inputs import EquityObservation
from app.backtesting.analyzers import (
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
    """Raised when the independent persistence transaction fails.

    The triggering persistence error stays attached as ``__cause__``; the
    original run failure is re-raised separately by the coordinator, so a
    failed finalization can never be mistaken for persisted state.
    """


@dataclass(frozen=True, slots=True)
class AnalysisFinalizationResult:
    """Outcome of one successful terminal persistence."""

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
        },
        "rate_snapshot_hash": rate_snapshot.snapshot_hash,
        "rate_source_versions": {
            "source_key": rate_snapshot.source_key,
            "source_version": rate_snapshot.source_version,
        },
        "missing_ranges": [
            [start.isoformat(), end.isoformat()]
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
        return summary_id, written

    def _base_summary_fields(
        self,
        analysis_snapshot: Any,
        run_uuid: UUID,
    ) -> dict[str, Any]:
        counts = analysis_snapshot.summary_counts()
        now = datetime.now(timezone.utc)
        fields: dict[str, Any] = {
            "run_id": run_uuid,
            "analyzer_snapshot": {
                "specs": [
                    spec.describe() for spec in analysis_snapshot.specs
                ],
            },
            "formula_signature": analysis_snapshot.formula_signature(),
            "input_evidence_signature": (
                analysis_snapshot.input_evidence_signature()
            ),
            "reporting_currency": analysis_snapshot.reporting_currency,
            "initial_equity": counts.get("initial_equity"),
            "valid_day_count": counts.get("valid_day_count"),
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
        completed_through_session: date | None = None,
    ) -> UUID:
        """Upsert a ``partial`` progress summary without writing metrics.

        Partial checkpoints never touch the final metric table; only the
        summary's progress fields and signatures are refreshed.
        """

        now = datetime.now(timezone.utc)
        summary = BacktestAnalysisSummaryRecord(
            **{
                **self._base_summary_fields(
                    analysis_snapshot, _run_uuid_of(analysis_snapshot.run_id)
                ),
                "status": AnalysisStatus.PARTIAL.value,
                "last_chunk_sequence": last_chunk_sequence,
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
    ) -> AnalysisFinalizationResult:
        """Write the frozen ``final`` summary and its complete metrics."""

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
                "last_chunk_sequence": None,
                "completed_through_session": completed_through_session,
                "abort_reason": None,
                "failed_step_sequence": None,
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
        except Exception:
            # Idempotent retry path: keep the results frozen by the first
            # finalization instead of recomputing or overwriting them.
            results = engine.final_results or ()
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
        analysis_snapshot = snapshot.analysis_snapshot
        base_fields = self._base_summary_fields(analysis_snapshot, run_uuid)
        specs_block = base_fields.pop("analyzer_snapshot")
        specs_block["blocked_equity_observation"] = blocked_payload
        now = datetime.now(timezone.utc)
        summary = BacktestAnalysisSummaryRecord(
            **{
                **base_fields,
                "analyzer_snapshot": specs_block,
                "status": AnalysisStatus.ABORTED.value,
                "last_chunk_sequence": snapshot.failed_step_sequence,
                "completed_through_session": None,
                "abort_reason": snapshot.error_message,
                "failed_step_sequence": snapshot.failed_step_sequence,
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
        """Run one slice and persist the matching analysis state.

        Success returns the runner's own result untouched, but only after
        its analysis state was persisted: ``final`` runs get their frozen
        metrics plus the terminal summary, ``partial`` chunks refresh the
        progress summary without touching the metric table.  When the
        runner carries an analyzer engine, ``session_factory`` is
        mandatory — a successful run must never silently skip persistence.

        Only the agreed abort triggers (:class:`ValuationBlockedError`,
        directly or as the wrapped cause of a ``PhaseExecutionError``,
        plus the explicitly listed unrecoverable types) enter failure
        finalization; every other exception propagates unchanged.
        """

        has_engine = bool(getattr(runner, "_analysis_engine", None))
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

        analysis_status = getattr(result, "analysis_status", None)
        if analysis_status == "final":
            engine = runner._analysis_engine
            if engine is not None:
                self._finalizer.persist_final(
                    engine,
                    session_factory=session_factory,
                    completed_through_session=_completed_session_of(runner, result),
                )
        elif analysis_status == "partial":
            snapshot = getattr(runner, "_latest_analysis_snapshot", None)
            if snapshot is not None:
                self._finalizer.persist_partial(
                    snapshot,
                    session_factory=session_factory,
                    last_chunk_sequence=getattr(
                        result, "completed_through_step_sequence", None
                    ),
                    completed_through_session=_completed_session_of(runner, result),
                )
        return result
