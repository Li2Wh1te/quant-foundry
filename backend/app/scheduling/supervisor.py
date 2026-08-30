"""Deterministic run progress and lost-heartbeat supervision policy.

The policy is intentionally independent of the worker transport. A process
launcher may provide TERM/KILL callbacks, while tests can use in-memory
callbacks without pretending that a missing heartbeat proves business failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Callable
import time
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.scheduling.repository import SchedulerRepository
from app.scheduling.schemas import RunStatus


logger = structlog.get_logger(__name__)
HEARTBEAT_INTERVAL = timedelta(seconds=15)
LOST_HEARTBEAT_AFTER = timedelta(seconds=60)
CANCELLATION_GRACE = timedelta(seconds=10)


@dataclass(frozen=True, slots=True)
class FrozenTimelineProgress:
    """Progress calculated only from completed steps on a frozen timeline."""

    total_steps: int
    completed_steps: int
    current_step: str
    current_trading_date: date | None = None

    def __post_init__(self) -> None:
        if self.total_steps <= 0:
            raise ValueError("total_steps must be positive")
        if not 0 <= self.completed_steps <= self.total_steps:
            raise ValueError("completed_steps must be within the frozen timeline")
        if not self.current_step.strip():
            raise ValueError("current_step must not be blank")

    @property
    def ratio(self) -> float:
        return self.completed_steps / self.total_steps


class RunProgressReporter:
    """Persist exact monotonic progress; heartbeats never alter progress."""

    def __init__(self, run_id: UUID, worker_id: str | None = None) -> None:
        self.run_id = run_id
        self.worker_id = worker_id
        self._last_ratio = 0.0
        self._last_persisted_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None

    def report(self, progress: FrozenTimelineProgress) -> None:
        if progress.ratio < self._last_ratio:
            raise ValueError("run progress must be monotonic")
        now = datetime.now(UTC)
        # Step changes are persisted at most every five seconds; heartbeat
        # writes still occur within the fifteen-second liveness budget.
        if self._last_persisted_at is not None and now - self._last_persisted_at < timedelta(seconds=5):
            if self._last_heartbeat_at is None or now - self._last_heartbeat_at >= HEARTBEAT_INTERVAL:
                self.heartbeat()
            return
        with Session(get_engine()) as session:
            changed = SchedulerRepository(session).update_progress(
                self.run_id,
                current_trading_date=(
                    progress.current_trading_date.isoformat()
                    if progress.current_trading_date is not None
                    else None
                ),
                current_step=progress.current_step,
                progress=progress.ratio,
                worker_id=self.worker_id,
            )
            session.commit()
        if changed:
            self._last_ratio = progress.ratio
            self._last_persisted_at = now
            self._last_heartbeat_at = now

    def heartbeat(self) -> None:
        with Session(get_engine()) as session:
            SchedulerRepository(session).heartbeat(self.run_id, worker_id=self.worker_id)
            session.commit()
        self._last_heartbeat_at = datetime.now(UTC)


class RunSupervisor:
    """Apply cancellation/lost-worker terminal semantics without run reuse."""

    def terminate_lost_runs(
        self,
        *,
        now: datetime | None = None,
        terminate: Callable[[UUID], None] | None = None,
        kill: Callable[[UUID], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        grace_seconds: float | None = None,
    ) -> tuple[UUID, ...]:
        current = now or datetime.now(UTC)
        with Session(get_engine()) as session:
            repository = SchedulerRepository(session)
            run_ids = repository.list_lost_heartbeat_runs(
                cutoff=current - LOST_HEARTBEAT_AFTER
            )
            for run_id in run_ids:
                run = repository.get_run(run_id)
                metadata = {"run_id": str(run_id), "task_id": str(run.task_id) if run else None, "task_type": run.task_type if run else None, "worker_id": run.worker_id if run else None}
                logger.warning("task_run_cancel_requested", message="Supervisor 请求取消失联任务，进入取消宽限期。", source="scheduler", cancellation_grace_seconds=grace_seconds or CANCELLATION_GRACE.total_seconds(), **metadata)
                if terminate is not None:
                    terminate(run_id)
                sleep(grace_seconds if grace_seconds is not None else CANCELLATION_GRACE.total_seconds())
                session.expire_all()
                current_run = repository.get_run(run_id)
                if current_run is None or current_run.status != RunStatus.RUNNING.value:
                    logger.info("task_run_terminal_observed", message="失联任务在终止前已形成一致终态，保留原终态。", source="scheduler", status=current_run.status if current_run else None, completion_marker=getattr(current_run, "completion_marker", None), exit_code=getattr(current_run, "exit_code", None), error_type=getattr(current_run, "error_type", None), **metadata)
                    continue
                if kill is not None:
                    kill(run_id)
                repository.finish_run(
                    run_id,
                    status=RunStatus.INDETERMINATE,
                    error_type="RunnerLostHeartbeat",
                    error_message="运行子进程连续 60 秒无有效心跳，已终止且不会自动重启。",
                    failure_phase="runner_lost_heartbeat",
                )
                logger.error(
                    "task_run_lost_heartbeat_terminated",
                    message="任务运行连续 60 秒无有效心跳，已在取消宽限期后终止，原运行不会重启或复用。",
                    **metadata,
                    cancellation_grace_seconds=int(CANCELLATION_GRACE.total_seconds()),
                    completion_marker=None,
                )
                logger.info("task_run_worker_exited", message="任务子进程已退出，完成标记缺失，运行进入不确定终态。", source="scheduler", **metadata, exit_code=None, completion_marker=None, error_type="RunnerLostHeartbeat")
                logger.info("task_run_terminal_written", message="已写入任务运行终态：失联终止且不会自动重启。", source="scheduler", status=RunStatus.INDETERMINATE.value, failure_phase="runner_lost_heartbeat", **metadata)
            session.commit()
        return tuple(run_ids)
