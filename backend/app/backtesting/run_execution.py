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
# ``terminal`` is retained as an in-memory compatibility marker for the
# earlier task-08 adapter.  Persisted roots use one of the five explicit
# terminal status values and never store this marker.
ALLOWED = {
    "queued": {"starting", "cancel_requested"},
    "starting": {"running", "cancel_requested", *TERMINAL},
    "running": {"cancel_requested", *TERMINAL, "terminal"},
    "cancel_requested": {*TERMINAL, "terminal"},
    "terminal": set(),
    **{status: set() for status in TERMINAL},
}

class InvalidRunTransition(ValueError): pass

class RunStateMachine:
    def transition(self, current: str, target: str) -> str:
        if target not in ALLOWED.get(current, set()): raise InvalidRunTransition(f"{current} -> {target} is not allowed")
        return target

class BacktestQueue:
    def __init__(self, formal_limit: int = 32, internal_limit: int | None = None, workers: int | None = None, repository=None):
        if (
            not isinstance(formal_limit, int)
            or isinstance(formal_limit, bool)
            or formal_limit > 32
            or formal_limit < 1
            or (
                internal_limit is not None
                and (
                    not isinstance(internal_limit, int)
                    or isinstance(internal_limit, bool)
                    or internal_limit < 1
                    or internal_limit >= formal_limit
                    or internal_limit >= 32
                )
            )
        ):
            raise ValueError("invalid backtest queue limit")
        if workers is not None and (not isinstance(workers, int) or isinstance(workers, bool) or workers < 1):
            raise ValueError("workers must be a positive integer")
        # The in-memory adapter historically represented a queue only and did
        # not track active children.  Keep that direct-test behaviour when no
        # worker quota is supplied; production callers pass the explicit
        # configured quota and receive the shared-slot guard.
        self.formal_limit, self.internal_limit = formal_limit, internal_limit
        self.workers = workers or 1
        self._enforce_worker_limit = workers is not None
        self.repository = repository
        self._formal: list[UUID] = []; self._internal: list[UUID] = []
        self._active: set[UUID] = set()
        self._cancelled: set[UUID] = set()
    def enqueue(self, run_id: UUID, kind: str) -> None:
        if kind not in {"backtest_run", "internal_link_acceptance"}:
            raise ValueError("unsupported backtest queue kind")
        q = self._internal if kind == "internal_link_acceptance" else self._formal
        limit = self.internal_limit if kind == "internal_link_acceptance" else self.formal_limit
        if limit is None or len(q) >= limit: raise RuntimeError("backtest queue is full or disabled")
        q.append(run_id)
        if self.repository is not None and hasattr(self.repository, "mark_queued"):
            self.repository.mark_queued(run_id, kind)
    @property
    def active_workers(self) -> int:
        return len(self._active)

    def request_cancel(self, run_id: UUID) -> bool:
        """Mark a queued id for supervisor-side cancelled-before-start closure."""

        for queue in (self._formal, self._internal):
            if run_id in queue:
                self._cancelled.add(run_id)
                return True
        return False

    def claim(self) -> UUID | None:
        if self._enforce_worker_limit and len(self._active) >= self.workers:
            return None
        # Formal queue is always selected first.  Internal work only proceeds
        # when no formal row is waiting, preserving the documented priority.
        queue = self._formal if self._formal else self._internal
        while queue:
            value = queue.pop(0)
            if value in self._cancelled:
                self._cancelled.remove(value)
                if self.repository is not None and hasattr(self.repository, "mark_claimed"):
                    self.repository.mark_claimed(value)
                continue
            self._active.add(value)
            break
        else:
            return None
        if self.repository is not None and hasattr(self.repository, "mark_claimed"):
            self.repository.mark_claimed(value)
        logger.info(backtest_event_message("回测运行领取", str(value), "已领取并进入启动阶段"), extra={"event": "backtest_claimed", "run_id": str(value), "status": "starting"})
        return value

    def release(self, run_id: UUID) -> bool:
        """Release one active worker slot after the supervisor reaps it."""

        if run_id not in self._active:
            return False
        self._active.remove(run_id)
        return True

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
        logger.info(backtest_event_message("回测分块写入", f"chunk-{commit.sequence}", f"已完成（进度 {commit.progress:.0%}）"), extra={"event": "backtest_chunk", "chunk_sequence": commit.sequence, "checkpoint": dict(commit.checkpoint)})
        return old or commit
    @property
    def commits(self): return tuple(self._commits[k] for k in sorted(self._commits))

def decide_terminal(*, marker: Mapping[str, Any] | None, exit_code: int | None,
                    expected_count: int | None = None, run_id: str | None = None,
                    config_hash: str | None = None, integrity: Any = None,
                    forced: bool = False) -> str:
    """Return one terminal status while retaining the legacy test shape.

    Task-08 callers used a compact ``protocol/status/result_count`` marker.
    New runner calls use the canonical task-23 marker and an independent
    integrity evidence object.  Keeping the compatibility branch here avoids
    changing old result semantics while ensuring every new marker follows the
    conservative three-evidence truth table.
    """
    if marker and marker.get("protocol") == "completion_marker@1":
        if expected_count is not None and marker.get("result_count") != expected_count:
            return "indeterminate"
        if run_id is not None and marker.get("run_id") != str(run_id):
            return "indeterminate"
        if config_hash is not None and marker.get("config_hash") != config_hash:
            return "indeterminate"
        declared = marker.get("status")
        if declared not in TERMINAL:
            return "indeterminate"
        if declared == "succeeded" and exit_code not in (0, None):
            return "indeterminate"
        return str(declared)
    from .runner_protocol import decide_terminal as decide_runner_terminal
    return decide_runner_terminal(
        marker=marker,
        exit_code=exit_code,
        integrity=integrity,
        run_id=run_id,
        config_hash=config_hash,
        forced=forced,
    )

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
