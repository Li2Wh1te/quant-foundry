"""Run-scoped append writer for persisted backtest result facts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import UUID
import logging

from sqlalchemy import select

from .models import BacktestRunRecord
from .result_repository import BacktestResultRepository, ResultRecordConflictError

logger = logging.getLogger("backtesting.result_writer")


@dataclass(frozen=True, slots=True)
class BacktestResultContext:
    """Immutable binding passed from a runner; deliberately contains no ORM/session."""
    run_id: UUID
    run_kind: str
    profile: str
    config_hash: str
    owner_scope: str = "default"
    schema_version: str = "result-v1"

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
        if root.status == "terminal":
            raise ValueError("terminal run cannot accept result chunks")
        return root

    def persist_result_batch(self, batch: ResultBatch) -> int:
        """Persist one bounded batch inside a short savepoint transaction."""
        transaction = self.session.begin_nested()
        try:
            total = self._persist_result_batch(batch)
            transaction.commit()
            return total
        except Exception:
            transaction.rollback()
            raise

    def _persist_result_batch(self, batch: ResultBatch) -> int:
        root = self._root()
        total = 0
        counts = dict(root.result_counts or {})
        for kind, values in (("steps", batch.steps), ("decisions", batch.decisions), ("orders", batch.orders), ("order_updates", batch.order_updates), ("fills", batch.fills), ("positions", batch.positions), ("equity_curve", batch.equity_curve), ("data_chunks", batch.data_chunks)):
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
        if batch.progress is not None:
            self.record_progress(batch.progress, current_step=batch.current_step, current_date=batch.current_date, checkpoint=batch.checkpoint)
        self.session.flush()
        logger.info("backtest_result_chunk_committed", extra={"event": "backtest_result_chunk_committed", "message": f"回测结果块已提交，运行 {self.context.run_id}，写入 {total} 条事实，checkpoint 已处理。", "run_id": str(self.context.run_id), "run_kind": self.context.run_kind, "result_count": total})
        return total

    def persist_runtime_result(self, result: Any) -> int:
        """Adapt a completed runtime projection into a persistence batch.

        Runtime DTO conversion is intentionally explicit: only already
        validated result-record DTOs are forwarded, while unsupported event
        shapes are left to the caller's adapter instead of being guessed.
        """
        from app.backtesting.result_models import (
            BacktestDecisionRecord, BacktestEquityCurveRecord,
            BacktestFillRecord, BacktestOrderRecord, BacktestPositionRecord,
            BacktestStepRecord, DataQualityStatus, DecisionValidationStatus,
            ResultOrderStatus, StepPhase, ValuationStatus, InstrumentDisplaySnapshot,
        )
        run_id = self.context.run_id
        steps = []
        for sample in getattr(result, "equity_curve", ()):
            steps.append(BacktestStepRecord(run_id=run_id, step_sequence=sample.step_sequence,
                time_start=sample.as_of, time_end=sample.as_of, data_cutoff_at=sample.as_of,
                phase=StepPhase.VALUATION, data_quality=DataQualityStatus.BLOCKED if sample.equity is None else DataQualityStatus.OK))
        decisions = []
        for decision in getattr(result, "decisions", ()):
            decisions.append(BacktestDecisionRecord(run_id=run_id, decision_id=decision.decision_id,
                step_sequence=decision.step_sequence, decision_time=decision.decision_time,
                mode=decision.mode, validation_status=DecisionValidationStatus.ACCEPTED,
                targets={str(k): str(v) for k, v in (decision.targets or {}).items()},
                validation_issues=(), error=None))
        orders = []
        for order in getattr(result, "order_outcomes", ()):
            orders.append(BacktestOrderRecord(run_id=run_id, order_id=order.order_id,
                instrument_id=order.instrument_id, display=InstrumentDisplaySnapshot(instrument_id=order.instrument_id),
                side=order.side, order_type="market", quantity=order.quantity,
                status=order.status, submitted_at=order.submitted_at, intent_id=order.intent_id,
                filled_quantity=order.quantity if order.status == "filled" else 0,
                status_reason=order.status_reason))
        fills = []
        for sequence, event in enumerate((e for e in getattr(result, "events", ()) if getattr(e, "event_type", "") == "fill_created"), start=1):
            payload = dict(event.payload)
            try:
                fills.append(BacktestFillRecord(run_id=run_id, fill_id=UUID(str(payload["fill_id"])),
                    order_id=UUID(str(payload["order_id"])), instrument_id=UUID(str(payload["instrument_id"])),
                    display=event.display_snapshot or InstrumentDisplaySnapshot(instrument_id=UUID(str(payload["instrument_id"]))),
                    side=payload["side"], timestamp=event.event_time, fill_sequence=sequence,
                    reference_price=payload.get("reference_price"), price=payload.get("execution_price"),
                    quantity=payload.get("quantity"), fees=payload.get("fees", 0),
                    slippage_bps=payload.get("slippage_bps"), slippage_model_key=payload.get("slippage_model_key"),
                    slippage_model_version=payload.get("slippage_model_version")))
            except (KeyError, TypeError, ValueError):
                continue
        equities = []
        for sample in getattr(result, "equity_curve", ()):
            status = ValuationStatus.BLOCKED if sample.equity is None else ValuationStatus.COMPLETE
            cash = result.final_snapshot.account.cash_balances.get("CNY", 0)
            equities.append(BacktestEquityCurveRecord(run_id=run_id, sequence=sample.step_sequence,
                as_of=sample.as_of, valuation_status=status, cash=cash,
                market_value=None if sample.equity is None else sample.equity-cash,
                equity=sample.equity, period_return=None if sample.equity is None else 0,
                total_pnl=None if sample.equity is None else 0,
                cumulative_return=None if sample.equity is None else 0,
                drawdown=None if sample.equity is None else 0,
                valuation_reason="runtime valuation blocked" if sample.equity is None else None))
        positions = []
        if getattr(result, "equity_curve", ()):
            last_as_of = result.equity_curve[-1].as_of
            for position in getattr(result.final_snapshot, "positions", ()):
                positions.append(BacktestPositionRecord(run_id=run_id, as_of=last_as_of,
                    instrument_id=position.instrument_id, display=InstrumentDisplaySnapshot(instrument_id=position.instrument_id),
                    side=position.side, quantity=position.quantity, available_quantity=position.available_quantity,
                    average_price=position.average_price, mark_price=position.mark_price,
                    realized_pnl=position.realized_pnl, unrealized_pnl=position.unrealized_pnl))
        metrics = tuple(getattr(result, "analysis_metrics", ()) or ())
        return self.persist_result_batch(ResultBatch(steps=tuple(steps), decisions=tuple(decisions), orders=tuple(orders), fills=tuple(fills), positions=tuple(positions), equity_curve=tuple(equities), metrics=metrics))

    def record_progress(self, progress: float, *, current_step: int | None = None, current_date: str | None = None, checkpoint: Mapping[str, Any] | None = None) -> None:
        root = self._root()
        if not 0 <= progress <= 1 or progress < float(root.progress or 0):
            raise ValueError("progress must be monotonic in [0,1]")
        root.progress = progress; root.current_step = current_step; root.current_date = current_date
        if checkpoint is not None: root.checkpoint = dict(checkpoint)

    def record_completion_marker(self, marker: Mapping[str, Any], *, exit_code: int | None = None) -> None:
        root = self._root(); root.completion_marker = dict(marker); root.runner_exit_code = exit_code

    def record_terminal_summary(self, *, terminal_status: str, integrity_status: str | None = None, result_counts: Mapping[str, Any] | None = None) -> None:
        """Reject terminal writes from the worker-facing persistence service.

        Final status is a Supervisor decision (completion marker, exit code and
        integrity evidence).  Keeping this method fail-closed prevents a
        child process from fabricating ``succeeded`` or other terminal states.
        """
        logger.warning("backtest_terminal_write_rejected", extra={"event": "backtest_terminal_write_rejected", "message": f"回测终态写入已拒绝，运行 {self.context.run_id}，终态由 Supervisor 裁决。", "run_id": str(self.context.run_id), "run_kind": self.context.run_kind})
        raise PermissionError("terminal status is Supervisor-owned")

    def apply_supervisor_terminal_summary(self, *, terminal_status: str, integrity_status: str | None = None, result_counts: Mapping[str, Any] | None = None, supervisor_token: object) -> None:
        """Apply a terminal decision only with the private Supervisor token."""
        if supervisor_token is not _SUPERVISOR_TOKEN:
            raise PermissionError("invalid Supervisor authorization")
        root = self.session.get(BacktestRunRecord, self.context.run_id)
        if root is None:
            raise ValueError("run root not found")
        if root.status == "terminal" and root.terminal_status != terminal_status:
            raise ValueError("terminal state is immutable")
        root.status = "terminal"
        root.terminal_status = terminal_status
        root.result_integrity_status = integrity_status
        if result_counts is not None:
            root.result_counts = dict(result_counts)


_SUPERVISOR_TOKEN = object()
