"""Run-scoped append writer for persisted backtest result facts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import UUID
import logging

from sqlalchemy import update

from .models import BacktestRunRecord
from .result_repository import BacktestResultRepository

logger = logging.getLogger("backtesting.result_writer")

_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "indeterminate", "terminal"}
)
_ACTIVE_STATUSES = frozenset({"starting", "running", "cancel_requested"})


def _json_value(value: Any) -> Any:
    """Convert immutable runtime values to exact JSON-safe primitives."""

    if isinstance(value, (datetime, date, UUID)):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        return _json_value(enum_value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _display_for(result: Any, instrument_id: UUID):
    """Return the last frozen event display for one instrument, if present."""

    from app.backtesting.result_models import InstrumentDisplaySnapshot

    for event in reversed(tuple(getattr(result, "events", ()) or ())):
        snapshot = getattr(event, "display_snapshot", None)
        if snapshot is not None and snapshot.instrument_id == instrument_id:
            return snapshot
        snapshots = getattr(event, "display_snapshots", {}) or {}
        snapshot = snapshots.get(instrument_id)
        if snapshot is not None:
            return snapshot
    return InstrumentDisplaySnapshot(instrument_id=instrument_id)


def _result_summary(result: Any) -> dict[str, Any]:
    """Build a compact run summary from already frozen runtime projections."""

    samples = tuple(getattr(result, "equity_curve", ()) or ())
    last = samples[-1] if samples else None
    return _json_value({
        "schema_version": "result-v1",
        "analysis_status": getattr(result, "analysis_status", None),
        "event_count": len(tuple(getattr(result, "events", ()) or ())),
        "components": getattr(result, "components", {}),
        "random_seed": getattr(result, "random_seed", None),
        "rule_snapshot_hash": getattr(result, "rule_snapshot_hash", None),
        "final_valuation": None if last is None else {
            "as_of": last.as_of,
            "valuation_status": last.valuation_status,
            "equity": last.equity,
            "cumulative_fees": last.cumulative_fees,
        },
        "universe": getattr(result, "universe_eligibility_summary", {}),
    })



@dataclass(frozen=True, slots=True)
class BacktestResultContext:
    """Immutable binding passed from a runner; deliberately contains no ORM/session."""
    run_id: UUID
    run_kind: str
    profile: str
    config_hash: str
    owner_scope: str = "default"
    schema_version: str = "result-v1"
    launch_id: UUID | None = None

    def __post_init__(self) -> None:
        expected_map = {
            "backtest_run": "formal@1",
            "internal_link_acceptance": "internal_link_acceptance@1",
        }
        expected_profile = expected_map.get(self.run_kind)
        if expected_profile is None:
            raise ValueError("unsupported run kind")
        if self.profile != expected_profile:
            raise ValueError("run kind/profile mismatch")
        if len(self.config_hash) != 64:
            raise ValueError("config_hash must be a sha256 digest")


@dataclass(frozen=True, slots=True)
class ResultBatch:
    """A bounded chunk of already validated DTOs."""
    steps: Sequence[Any] = field(default_factory=tuple)
    decisions: Sequence[Any] = field(default_factory=tuple)
    orders: Sequence[Any] = field(default_factory=tuple)
    order_updates: Sequence[Any] = field(default_factory=tuple)
    fills: Sequence[Any] = field(default_factory=tuple)
    positions: Sequence[Any] = field(default_factory=tuple)
    equity_curve: Sequence[Any] = field(default_factory=tuple)
    metrics: Sequence[Any] = field(default_factory=tuple)
    data_chunks: Sequence[Any] = field(default_factory=tuple)
    progress: float | None = None
    current_step: int | None = None
    current_date: str | None = None
    checkpoint: Mapping[str, Any] | None = None
    result_summary: Mapping[str, Any] | None = None
    # Preserve the runtime-observed component snapshot as a first-class batch
    # value instead of relying on callers to unpack a generic summary.
    component_snapshot: Mapping[str, Any] | None = None
    events: Sequence[Any] = field(default_factory=tuple)


class BacktestResultPersistenceService:
    """Short-transaction writer and restricted lifecycle evidence port."""
    def __init__(self, session, context: BacktestResultContext, *, cursor_signing_key: str = "writer"):
        self.session = session
        self.context = context
        self.repository = BacktestResultRepository(session, cursor_signing_key=cursor_signing_key)

    def _root(self) -> BacktestRunRecord:
        root = self.session.get(BacktestRunRecord, self.context.run_id)
        if root is None or root.run_kind != self.context.run_kind or root.profile != self.context.profile:
            raise ValueError("result root is missing or has incompatible kind/profile")
        if root.config_hash != self.context.config_hash or root.tenant_id != self.context.owner_scope:
            raise ValueError("result context does not match run root")
        if self.context.launch_id is None:
            # Run identity alone is not enough for a Worker-facing writer:
            # an old launch could otherwise append facts after a new launch
            # has claimed the same run root.
            raise ValueError("result context launch_id is required")
        if root.launch_id is None or str(root.launch_id) != str(self.context.launch_id):
            raise ValueError("result context launch_id does not match run root")
        if root.status in _TERMINAL_STATUSES or root.terminal_status is not None:
            raise ValueError("terminal run cannot accept result chunks")
        if root.status not in _ACTIVE_STATUSES:
            raise ValueError("result root is not in an active launch state")
        return root

    def persist_result_batch(self, batch: ResultBatch, *, commit: bool = False) -> int:
        """Persist one bounded batch and optionally make it durable immediately."""
        transaction = self.session.begin_nested()
        try:
            total = self._persist_result_batch(batch)
            transaction.commit()
            if commit:
                self.session.commit()
            return total
        except Exception:
            transaction.rollback()
            raise

    def _persist_result_batch(self, batch: ResultBatch) -> int:
        root = self._root()
        total = 0
        counts = dict(root.result_counts or {})
        for kind, values in (("events", batch.events), ("steps", batch.steps), ("decisions", batch.decisions), ("orders", batch.orders), ("order_updates", batch.order_updates), ("fills", batch.fills), ("positions", batch.positions), ("equity_curve", batch.equity_curve), ("data_chunks", batch.data_chunks)):
            if values:
                if kind == "order_updates":
                    added = sum(self.repository.append_order_update_transaction(dto) for dto in values)
                else:
                    added = self.repository.append_idempotent(kind, *values)
                total += added
                counts[kind] = int(counts.get(kind, 0)) + added
        if batch.metrics:
            added = self.repository.append_metrics(*batch.metrics)
            total += added
            counts["metrics"] = int(counts.get("metrics", 0)) + added
        if total:
            now = datetime.now().astimezone()
            if root.first_result_at is None:
                root.first_result_at = now
            root.last_result_at = now
            root.result_counts = counts
        if batch.result_summary is not None or batch.component_snapshot is not None:
            summary = dict(root.result_summary or {})
            if batch.result_summary is not None:
                summary.update(_json_value(batch.result_summary))
            if batch.component_snapshot is not None:
                summary["components"] = _json_value(batch.component_snapshot)
            root.result_summary = summary
        if batch.progress is not None:
            self.record_progress(batch.progress, current_step=batch.current_step, current_date=batch.current_date, checkpoint=batch.checkpoint)
        self.session.flush()
        logger.info(f"回测结果块已提交，运行 {self.context.run_id}，写入 {total} 条事实，checkpoint 已处理。", extra={"event": "backtest_result_chunk_committed", "run_id": str(self.context.run_id), "run_kind": self.context.run_kind, "result_count": total})
        return total

    def persist_runtime_result(self, result: Any, *, commit: bool = True) -> int:
        """Persist every result projection from one immutable runtime snapshot."""

        from app.backtesting.result_models import (
            BacktestDecisionRecord,
            BacktestEventRecord,
            BacktestEquityCurveRecord,
            BacktestFillRecord,
            BacktestOrderRecord,
            BacktestOrderUpdateRecord,
            BacktestPositionRecord,
            BacktestStepRecord,
            DataQualityStatus,
            DecisionValidationStatus,
            StepPhase,
            ValuationStatus,
        )

        run_id = self.context.run_id
        events = tuple(
            BacktestEventRecord(
                run_id=run_id,
                event_sequence=event.event_sequence,
                step_sequence=event.step_sequence,
                phase_sequence=event.phase_sequence,
                phase_key=event.phase_key,
                event_type=event.event_type,
                event_time=event.event_time,
                payload=_json_value(event.payload),
                event_version=getattr(event, "event_version", 1),
            )
            for event in getattr(result, "events", ())
        )
        steps = tuple(
            BacktestStepRecord(
                run_id=run_id,
                step_sequence=sample.step_sequence,
                time_start=sample.time_start or sample.as_of,
                time_end=sample.time_end or sample.as_of,
                data_cutoff_at=sample.data_cutoff_at or sample.as_of,
                phase=StepPhase.VALUATION,
                data_quality=(
                    DataQualityStatus.BLOCKED
                    if sample.equity is None
                    else DataQualityStatus.OK
                ),
            )
            for sample in getattr(result, "equity_curve", ())
        )
        decisions = tuple(
            BacktestDecisionRecord(
                run_id=run_id,
                decision_id=decision.decision_id,
                step_sequence=decision.step_sequence,
                decision_time=decision.decision_time,
                mode=decision.mode,
                validation_status=getattr(
                    decision, "validation_status", DecisionValidationStatus.ACCEPTED
                ),
                targets={str(k): str(v) for k, v in (decision.targets or {}).items()},
                validation_issues=getattr(decision, "validation_issues", ()),
                duration_ms=getattr(decision, "duration_ms", None),
                error=getattr(decision, "error", None),
            )
            for decision in getattr(result, "decisions", ())
        )
        orders = tuple(
            BacktestOrderRecord(
                run_id=run_id,
                order_id=order.order_id,
                instrument_id=order.instrument_id,
                display=_display_for(result, order.instrument_id),
                side=order.side,
                order_type=getattr(order.order_type, "value", order.order_type),
                price=getattr(order, "price", None),
                quantity=order.quantity,
                status=order.status,
                submitted_at=order.submitted_at,
                intent_id=order.intent_id,
                decision_id=order.decision_id,
                filled_quantity=order.filled_quantity,
                status_reason=order.status_reason,
            )
            for order in getattr(result, "order_outcomes", ())
        )
        order_updates = tuple(
            BacktestOrderUpdateRecord(
                run_id=run_id,
                order_id=update_record.order_id,
                update_sequence=update_record.update_sequence,
                old_status=update_record.old_status,
                new_status=update_record.new_status,
                updated_at=update_record.updated_at,
                reason=update_record.reason,
            )
            for update_record in getattr(result, "order_updates", ())
        )
        fills = []
        for sequence, event in enumerate(
            (
                event
                for event in getattr(result, "events", ())
                if getattr(event, "event_type", "") == "fill_created"
            ),
            start=1,
        ):
            payload = dict(event.payload)
            try:
                instrument_id = UUID(str(payload["instrument_id"]))
                fills.append(
                    BacktestFillRecord(
                        run_id=run_id,
                        fill_id=UUID(str(payload["fill_id"])),
                        order_id=UUID(str(payload["order_id"])),
                        instrument_id=instrument_id,
                        display=getattr(event, "display_snapshot", None)
                        or _display_for(result, instrument_id),
                        side=payload["side"],
                        timestamp=event.event_time,
                        fill_sequence=sequence,
                        reference_price=payload.get("reference_price"),
                        price=payload["execution_price"],
                        quantity=payload["quantity"],
                        fees=payload.get("fees", 0),
                        slippage_bps=payload.get("slippage_bps"),
                        slippage_amount=payload.get("slippage_amount"),
                        slippage_model_key=payload.get("slippage_model_key"),
                        slippage_model_version=payload.get("slippage_model_version"),
                        currency=payload.get("currency", "CNY"),
                        contract_multiplier=payload.get("contract_multiplier", "1"),
                        gross_notional=payload.get(
                            "gross_notional", payload.get("notional")
                        ),
                        fee_breakdown=payload.get("fee_breakdown"),
                        settlement_lot_id=(
                            UUID(str(payload["settlement_lot_id"]))
                            if payload.get("settlement_lot_id") is not None
                            else None
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                from app.backtesting.domain import DomainValidationError

                raise DomainValidationError(
                    f"invalid fill result for event {sequence}: {exc}"
                ) from exc
        equities = []
        positions = []
        for sample in getattr(result, "equity_curve", ()):
            status = (
                ValuationStatus.BLOCKED
                if sample.equity is None
                else ValuationStatus.COMPLETE
            )
            if sample.cash is None:
                raise ValueError(
                    f"valuation sample {sample.step_sequence} has no point-in-time cash"
                )
            equities.append(
                BacktestEquityCurveRecord(
                    run_id=run_id,
                    sequence=sample.step_sequence,
                    as_of=sample.as_of,
                    valuation_status=status,
                    cash=sample.cash,
                    market_value=sample.market_value,
                    equity=sample.equity,
                    period_return=sample.period_return,
                    total_pnl=sample.total_pnl,
                    cumulative_return=sample.cumulative_return,
                    drawdown=sample.drawdown,
                    cumulative_fees=sample.cumulative_fees,
                    valuation_reason=(
                        "runtime valuation blocked" if sample.equity is None else None
                    ),
                )
            )
            if sample.portfolio_snapshot is not None:
                positions.extend(
                    BacktestPositionRecord(
                        run_id=run_id,
                        as_of=sample.as_of,
                        instrument_id=position.instrument_id,
                        display=_display_for(result, position.instrument_id),
                        side=position.side,
                        quantity=position.quantity,
                        available_quantity=position.available_quantity,
                        average_price=position.average_price,
                        mark_price=position.mark_price,
                        realized_pnl=position.realized_pnl,
                        unrealized_pnl=position.unrealized_pnl,
                    )
                    for position in sample.portfolio_snapshot.positions
                )
        # Analyzer metrics are finalized by AnalysisFinalizer, which first
        # persists the terminal summary required by append_metrics. Passing
        # raw MetricResult objects here used to fail the default result sink.
        batch = ResultBatch(
            events=events,
            steps=steps,
            decisions=decisions,
            orders=orders,
            order_updates=order_updates,
            fills=tuple(fills),
            positions=tuple(positions),
            equity_curve=tuple(equities),
            result_summary=_result_summary(result),
            component_snapshot=getattr(result, "components", {}),
        )
        return self.persist_result_batch(batch, commit=commit)

    def persist_runtime_failure(self, result: Any) -> int:
        """Commit already-determined rows before Supervisor handles failure."""

        return self.persist_runtime_result(result, commit=True)

    def persist_session_preflight(self, outcome: Any, *, commit: bool = True) -> int:
        """Persist the Worker's authoritative preflight and root hash pointer.

        Admission evidence is immutable input, while this session report is
        runtime evidence.  The existing root columns deliberately keep those
        hashes separate, and the phase-keyed result table retains the complete
        report needed by operators and result APIs.
        """

        from app.backtesting.data.preflight_service import DataPreflightService

        root = self._root()
        session_hash = getattr(outcome, "base_report_hash", None)
        if not isinstance(session_hash, str) or not session_hash:
            raise ValueError("session preflight must expose a base report hash")
        existing_hash = root.data_preflight_hash
        # Older queued rows initialized this field with the admission hash.
        # Permit that one legacy placeholder to advance, but never replace a
        # different session hash after authoritative evidence was committed.
        if existing_hash not in (
            None,
            root.data_admission_preflight_hash,
            session_hash,
        ):
            raise ValueError("authoritative session preflight hash is immutable")

        transaction = self.session.begin_nested()
        try:
            added = DataPreflightService.persist_session_report(
                self.repository,
                run_id=self.context.run_id,
                outcome=outcome,
            )
            root.data_preflight_hash = session_hash
            self.session.flush()
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        if commit:
            # The evidence must survive a later strategy/runtime failure;
            # execute_runtime may roll back its remaining unit of work.
            self.session.commit()
        return added

    def persist_data_chunk_evidence(
        self,
        evidence: Any,
        *,
        time_start: datetime,
        time_end: datetime,
        started_at: datetime | None = None,
    ) -> int:
        """Persist one consistency result before executing its time chunk."""

        from app.backtesting.result_models import (
            BacktestDataChunkRecord,
            ChunkValidationStatus,
            ConsistencyMode,
            DataPhase,
        )

        validation_status = (
            ChunkValidationStatus.PASSED
            if evidence.validation_status.value == "valid"
            else ChunkValidationStatus.FAILED
        )
        finished_at = evidence.validated_at or datetime.now().astimezone()
        dto = BacktestDataChunkRecord(
            run_id=self.context.run_id,
            phase=DataPhase.SESSION,
            chunk_sequence=evidence.chunk_index,
            time_start=time_start,
            time_end=time_end,
            chunk_strategy_version="fixed_trading_sessions@1",
            token_digest=evidence.token_digest,
            consistency_mode=ConsistencyMode(evidence.mode.value),
            coverage_summary=_json_value(evidence.coverage_summary),
            failure_phase=(
                None
                if validation_status is ChunkValidationStatus.PASSED
                else str(evidence.coverage_summary.get("failure_phase", "data_consistency"))
            ),
            validation_status=validation_status,
            started_at=started_at or finished_at,
            finished_at=finished_at,
            failure_reason=(
                None
                if validation_status is ChunkValidationStatus.PASSED
                else evidence.failure_reason or "consistency validation failed"
            ),
        )
        return self.persist_result_batch(
            ResultBatch(data_chunks=(dto,)),
            commit=True,
        )

    def record_progress(self, progress: float, *, current_step: int | None = None, current_date: str | None = None, checkpoint: Mapping[str, Any] | None = None) -> None:
        if not 0 <= progress <= 1:
            raise ValueError("progress must be monotonic in [0,1]")
        # Reuse the canonical repository write so result chunks and the
        # standalone heartbeat adapter share launch/status/CAS semantics.
        root = self._root()
        if progress < float(root.progress or 0):
            raise ValueError("progress must be monotonic in [0,1]")
        current_trading_date = (
            date.fromisoformat(current_date) if current_date is not None else None
        )
        from .run_repository import DatabaseRunRepository

        changed = DatabaseRunRepository(self.session).record_progress(
            self.context.run_id,
            progress,
            launch_id=self.context.launch_id,
            current_trading_date=current_trading_date,
            current_step=(str(current_step) if current_step is not None else None),
            checkpoint=checkpoint,
        )
        if changed is None or changed is False:
            raise ValueError("progress write lost run or launch condition")

    def record_completion_marker(self, marker: Mapping[str, Any], *, exit_code: int | None = None) -> None:
        """Persist worker completion evidence after result rows are committed."""

        from .runner_protocol import (
            EXIT_CODE_PROTOCOL,
            map_exit_code,
            require_valid_completion_marker,
        )

        validated = require_valid_completion_marker(
            marker,
            run_id=self.context.run_id,
            config_hash=self.context.config_hash,
        )
        if exit_code is not None:
            category = map_exit_code(exit_code)
            if category != validated.get("declared_category"):
                raise ValueError("completion marker category conflicts with worker exit code")
        root = self._root()
        # Completion evidence is append-only.  The run/launch/status/empty
        # marker predicates are all part of one conditional update, so a stale
        # Worker cannot publish evidence for a newer launch and a concurrent
        # Worker cannot replace the first marker.  Replaying the exact marker
        # remains idempotent after the CAS loses a race.
        values = {
            "completion_marker": dict(validated),
            "runner_exit_code": exit_code,
            "runner_exit_code_protocol": EXIT_CODE_PROTOCOL,
            "runner_exit_category": map_exit_code(exit_code),
            "completion_marker_protocol": validated.get("protocol_version"),
            "completion_marker_validation": {"valid": True, "errors": []},
        }
        statement = update(BacktestRunRecord).where(
            BacktestRunRecord.id == self.context.run_id,
            BacktestRunRecord.launch_id == self.context.launch_id,
            BacktestRunRecord.status.in_(tuple(_ACTIVE_STATUSES)),
            BacktestRunRecord.completion_marker.is_(None),
        )
        changed = self.session.execute(statement.values(**values)).rowcount
        self.session.flush()
        if changed == 1:
            return
        current = self.session.get(BacktestRunRecord, self.context.run_id)
        if current is not None and (
            current.completion_marker == dict(validated)
            and current.runner_exit_code == exit_code
        ):
            return
        if current is not None and current.completion_marker is not None:
            raise ValueError("completion marker already exists")
        raise ValueError("completion marker write lost run or launch condition")

    def record_terminal_summary(self, *, terminal_status: str, integrity_status: str | None = None, result_counts: Mapping[str, Any] | None = None) -> None:
        """Reject terminal writes from the worker-facing persistence service.

        Final status is a Supervisor decision (completion marker, exit code and
        integrity evidence).  Keeping this method fail-closed prevents a
        child process from fabricating ``succeeded`` or other terminal states.
        """
        logger.warning(f"回测终态写入已拒绝，运行 {self.context.run_id}，终态由 Supervisor 裁决。", extra={"event": "backtest_terminal_write_rejected", "run_id": str(self.context.run_id), "run_kind": self.context.run_kind})
        raise PermissionError("terminal status is Supervisor-owned")

    def apply_supervisor_terminal_summary(
        self,
        *,
        terminal_status: str,
        supervisor_lock: Any = None,
        terminal_decision_reason: str | None = None,
        integrity_evidence: Mapping[str, Any] | None = None,
        integrity_status: str | None = None,
        result_counts: Mapping[str, Any] | None = None,
    ) -> bool:
        """Delegate the summary to the canonical locked Supervisor CAS.

        The result writer is a Worker-facing component and therefore cannot
        retain a private token that permits direct root mutation.  The
        Supervisor must provide its live advisory-lock capability; the shared
        repository then writes the status and evidence atomically and keeps
        first-writer-wins semantics for repeated reconciliation.
        """

        from .run_repository import DatabaseRunRepository

        reason = terminal_decision_reason
        if terminal_status == "indeterminate" and not reason:
            reason = "terminal_summary"
        repository = DatabaseRunRepository(self.session)
        changed = repository.set_terminal(
            self.context.run_id,
            terminal_status,
            supervisor_lock=supervisor_lock,
            terminal_decision_reason=reason,
            evidence=integrity_evidence,
            result_integrity_status=integrity_status,
            result_counts=result_counts,
        )
        return changed is not None and getattr(changed, "status", None) == terminal_status
