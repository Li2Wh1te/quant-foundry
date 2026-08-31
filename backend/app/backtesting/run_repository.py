"""Run root state and supervisor-facing evidence adapter."""
from __future__ import annotations
from dataclasses import replace
from typing import Mapping, Any
from .run_binding import BacktestRun
from .run_execution import RunStateMachine, decide_terminal
from app.core.logging import backtest_event_message
from sqlalchemy import func, select, text
from .models import BacktestRunRecord
from .run_binding import IdempotencyKeyReusedError, QueueFullError

class RunRepository:
    def __init__(self):
        self._rows = {}; self._states = RunStateMachine(); self._queue_state = {}; self._cancel_evidence = {}
    def add(self, run: BacktestRun): self._rows[run.run_id] = run; return run
    def get(self, run_id): return self._rows.get(run_id)
    def transition(self, run_id, target: str):
        row = self._rows[run_id]; self._states.transition(row.status, target)
        row = replace(row, status=target); self._rows[run_id] = row
        self.last_message = backtest_event_message("回测状态变更", str(run_id), f"状态为 {target}")
        return row
    def request_cancel(self, run_id): return self.transition(run_id, "cancel_requested")
    def mark_queued(self, run_id, kind):
        self._queue_state[run_id] = {"kind": kind, "state": "queued"}
    def mark_claimed(self, run_id):
        if run_id in self._queue_state:
            self._queue_state[run_id]["state"] = "claimed"
    def record_cancel_evidence(self, run_id, *, grace_seconds: int, forced: bool = False, terminated_at=None):
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        self._cancel_evidence[run_id] = {"grace_seconds": grace_seconds, "forced": bool(forced), "terminated_at": terminated_at}
        return self._cancel_evidence[run_id]
    def cancel_evidence(self, run_id):
        return self._cancel_evidence.get(run_id)
    def adjudicate(self, run_id, *, marker: Mapping[str, Any] | None, exit_code: int | None, expected_count: int | None = None):
        row = self._rows[run_id]
        status = decide_terminal(marker=marker, exit_code=exit_code, expected_count=expected_count, run_id=str(run_id), config_hash=row.binding.config_hash)
        self._rows[run_id] = replace(row, status="terminal", terminal_status=status)
        self.last_message = backtest_event_message("回测终态裁决", str(run_id), status)
        return status
    def recover(self):
        """Return non-terminal roots for supervisor scan; never auto-requeues."""
        return tuple(r for r in self._rows.values() if r.status != "terminal")

    def recoverable(self):
        """Supervisor scan returns roots with evidence, without changing state."""
        rows = tuple({"run_id": r.run_id, "status": r.status, "cancel_evidence": self.cancel_evidence(r.run_id)} for r in self.recover())
        self.last_message = backtest_event_message("回测恢复扫描", f"非终态运行 {len(rows)} 个", "扫描完成，未自动重排队")
        return rows

class DatabaseRunCreationRepository:
    """PostgreSQL transaction boundary for idempotency and logical queues."""
    def __init__(self, session, *, formal_limit: int = 32, internal_limit: int | None = None):
        self.session = session
        self.limits = {"backtest_run": formal_limit, "internal_link_acceptance": internal_limit}

    def create(self, binding, *, tenant_id: str, idempotency_key: str):
        # Serialize capacity checks per logical queue inside the caller's DB transaction.
        self.session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:queue_kind))"), {"queue_kind": binding.run_kind})
        existing = self.session.scalar(select(BacktestRunRecord).where(BacktestRunRecord.tenant_id == tenant_id, BacktestRunRecord.idempotency_key == idempotency_key))
        if existing is not None:
            if existing.config_hash != binding.config_hash:
                raise IdempotencyKeyReusedError("idempotency key already used with different request")
            return existing
        limit = self.limits[binding.run_kind]
        if limit is None:
            raise QueueFullError("internal backtest queue is disabled")
        queued = self.session.scalar(select(func.count()).select_from(BacktestRunRecord).where(BacktestRunRecord.run_kind == binding.run_kind, BacktestRunRecord.status == "queued")) or 0
        if queued >= limit:
            raise QueueFullError("backtest queue is full")
        row = BacktestRunRecord(
            tenant_id=tenant_id, idempotency_key=idempotency_key,
            run_kind=binding.run_kind, profile=binding.profile, status="queued",
            config_hash=binding.config_hash, backtest_config=dict(binding.config),
            parameters=dict(binding.strategy.get("parameters", {})),
            initial_cash=str(binding.spec.initial_cash),
            initial_positions=list(binding.config["spec"]["initial_positions"]),
            data_request=dict(binding.data_request),
            fee_schedule_snapshot=dict(binding.account.get("fee_schedule", {})),
            analyzer_specs=list(binding.components.get("analyzers", ())),
        )
        self.session.add(row)
        self.session.flush()
        return row
