"""Small run-facing adapters for queueing, chunk commits and terminal evidence."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID
import logging
from app.core.logging import backtest_event_message

logger = logging.getLogger("backtesting.run")

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "indeterminate"}
CHUNK_SIZE_SESSIONS = 20
MAX_LOOKBACK_SESSIONS = 512
ALLOWED = {"queued": {"starting", "cancel_requested"}, "starting": {"running", "cancel_requested"},
           "running": {"cancel_requested", "terminal"}, "cancel_requested": {"terminal"}, "terminal": set()}

class InvalidRunTransition(ValueError): pass

class RunStateMachine:
    def transition(self, current: str, target: str) -> str:
        if target not in ALLOWED.get(current, set()): raise InvalidRunTransition(f"{current} -> {target} is not allowed")
        return target

class BacktestQueue:
    def __init__(self, formal_limit: int = 32, internal_limit: int | None = None, workers: int = 1, repository=None):
        if formal_limit > 32 or formal_limit < 1 or (internal_limit is not None and (internal_limit < 1 or internal_limit >= 32)):
            raise ValueError("invalid backtest queue limit")
        self.formal_limit, self.internal_limit, self.workers = formal_limit, internal_limit, workers
        self.repository = repository
        self._formal: list[UUID] = []; self._internal: list[UUID] = []
    def enqueue(self, run_id: UUID, kind: str) -> None:
        q = self._internal if kind == "internal_link_acceptance" else self._formal
        limit = self.internal_limit if kind == "internal_link_acceptance" else self.formal_limit
        if limit is None or len(q) >= limit: raise RuntimeError("backtest queue is full or disabled")
        q.append(run_id)
        if self.repository is not None and hasattr(self.repository, "mark_queued"):
            self.repository.mark_queued(run_id, kind)
    def claim(self) -> UUID | None:
        if self._formal:
            value = self._formal.pop(0)
        elif self._internal:
            value = self._internal.pop(0)
        else:
            return None
        if self.repository is not None and hasattr(self.repository, "mark_claimed"):
            self.repository.mark_claimed(value)
        logger.info("backtest_claimed", extra={"event": "backtest_claimed", "message": backtest_event_message("回测运行领取", str(value), "已领取并进入启动阶段"), "run_id": str(value), "status": "starting"})
        return value

@dataclass(frozen=True)
class ChunkCommit:
    sequence: int; token_digest: str; progress: float; checkpoint: Mapping[str, Any]

class ChunkResultWriter:
    """Idempotent run-level writer.

    ``persist`` is an optional callback supplied by the existing result
    repository.  The writer owns only chunk identity/checkpoint semantics and
    deliberately does not define result DTOs (those belong to task 09).
    """
    def __init__(self, persist=None, *, chunk_size: int = CHUNK_SIZE_SESSIONS,
                 max_lookback: int = MAX_LOOKBACK_SESSIONS):
        if chunk_size != CHUNK_SIZE_SESSIONS or max_lookback != MAX_LOOKBACK_SESSIONS:
            raise ValueError("task 08 fixes chunk size to 20 and lookback to 512")
        self._commits: dict[int, ChunkCommit] = {}
        self._persist = persist
    def append(self, commit: ChunkCommit) -> ChunkCommit:
        if commit.sequence < 0 or not 0 <= float(commit.progress) <= 1:
            raise ValueError("invalid chunk progress or sequence")
        if not isinstance(commit.token_digest, str) or not commit.token_digest:
            raise ValueError("chunk token digest is required")
        if not isinstance(commit.checkpoint, Mapping):
            raise ValueError("checkpoint must be a mapping")
        old = self._commits.get(commit.sequence)
        if old and old != commit: raise ValueError("chunk sequence conflict")
        self._commits[commit.sequence] = commit
        if old is None and self._persist is not None:
            self._persist(commit)
        logger.info("backtest_chunk", extra={"event": "backtest_chunk", "message": backtest_event_message("回测分块写入", f"chunk-{commit.sequence}", f"已完成（进度 {commit.progress:.0%}）"), "chunk_sequence": commit.sequence, "checkpoint": dict(commit.checkpoint)})
        return old or commit
    @property
    def commits(self): return tuple(self._commits[k] for k in sorted(self._commits))

def decide_terminal(*, marker: Mapping[str, Any] | None, exit_code: int | None, expected_count: int | None = None, run_id: str | None = None, config_hash: str | None = None) -> str:
    if not marker or marker.get("protocol") != "completion_marker@1": return "indeterminate"
    if expected_count is not None and marker.get("result_count") != expected_count: return "indeterminate"
    if run_id is not None and marker.get("run_id") != str(run_id): return "indeterminate"
    if config_hash is not None and marker.get("config_hash") != config_hash: return "indeterminate"
    declared = marker.get("status")
    if declared not in TERMINAL: return "indeterminate"
    if declared == "succeeded" and exit_code not in (0, None): return "indeterminate"
    return str(declared)

class RunExecutionAdapter:
    def __init__(self, session_gate, analysis_gate=None, result_writer=None): self.session_gate = session_gate; self.analysis_gate = analysis_gate; self.result_writer = result_writer
    def execute(self, binding, *, session: Any, runner: Any):
        # Formal execution stays closed until the canonical Runner/Supervisor
        # contract is present; this adapter never fabricates a successful run.
        if getattr(binding, "run_kind", None) not in {"backtest_run", "internal_link_acceptance"}:
            return {"status": "blocked", "reason": "invalid_run_kind"}
        if runner is None or not callable(getattr(runner, "run", None)):
            return {"status": "blocked", "reason": "canonical_runner_unavailable"}
        if session is None or not callable(getattr(session, "chunks", None)):
            return {"status": "blocked", "reason": "canonical_data_session_unavailable"}
        # Authoritative session gate runs before strategy/runner invocation.
        verdict = self.session_gate.validate_session(session)
        if verdict is False or getattr(verdict, "allowed", True) is False: return {"status": "failed", "reason": "session_preflight_blocked"}
        if self.analysis_gate is not None:
            admission = self.analysis_gate(binding)
            if admission is False or getattr(admission, "allowed", True) is False:
                return {"status": "failed", "reason": "analysis_admission_blocked"}
        results = []
        for sequence, chunk in enumerate(session.chunks()):
            session_count = getattr(chunk, "session_count", None)
            if session_count is not None and session_count > CHUNK_SIZE_SESSIONS:
                return {"status": "failed", "reason": "chunk_size_exceeded"}
            result = runner.run(chunk)
            results.append(result)
            if self.result_writer is not None:
                token_digest = getattr(chunk, "token_digest", None) or getattr(result, "token_digest", None)
                checkpoint = getattr(result, "checkpoint", None) or {"chunk_sequence": sequence}
                progress = getattr(result, "progress", min(1.0, float(sequence + 1) / max(1, len(getattr(session, "formal_sessions", ())) or sequence + 1)))
                self.result_writer.append(ChunkCommit(sequence, str(token_digest or "unavailable"), progress, checkpoint))
        return {"status": "succeeded", "chunks": results}
