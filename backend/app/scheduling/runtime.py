from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from uuid import UUID

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from sqlalchemy.orm import Session
import structlog
from structlog.contextvars import bound_contextvars

from app.core.config import Settings
from app.db.session import get_engine
from app.scheduling.registry import TaskContext, TaskRegistry, task_registry
from app.scheduling.repository import SchedulerRepository
from app.scheduling.schemas import (
    RunStatus,
    OnceSchedule,
    TaskState,
    TriggerType,
    schedule_adapter,
)
from app.scheduling.service import SchedulerService, TaskConflictError
from app.scheduling.triggers import build_trigger


logger = structlog.get_logger(__name__)
TASK_JOB_PREFIX = "scheduled-task:"
DISPATCH_JOB_ID = "scheduler:dispatch"


class SchedulerDisabledError(Exception):
    pass


class SchedulerRuntime:
    def __init__(
        self,
        settings: Settings,
        registry: TaskRegistry = task_registry,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.scheduler = BackgroundScheduler(timezone=UTC)
        self.executor = ThreadPoolExecutor(
            max_workers=settings.scheduler_max_workers,
            thread_name_prefix="task-worker",
        )
        self._futures: dict[Future[Any], UUID] = {}
        self._futures_lock = Lock()
        self._dispatch_lock = Lock()
        self._enqueue_lock = Lock()

    @property
    def running(self) -> bool:
        return self.scheduler.state != STATE_STOPPED

    def start(self) -> None:
        if not self.settings.scheduler_enabled:
            logger.info("scheduler_disabled")
            return

        with Session(get_engine()) as session:
            repository = SchedulerRepository(session)
            interrupted = repository.interrupt_running_runs()
            active_task_ids = repository.list_active_task_ids()
            session.commit()

        self.scheduler.start(paused=True)
        for task_id in active_task_ids:
            try:
                self.sync_task(task_id)
            except Exception:
                # One stale task definition must not prevent the API from starting.
                logger.exception("task_sync_failed", task_id=str(task_id))
        self.scheduler.add_job(
            self.dispatch_queued_runs,
            trigger="interval",
            seconds=self.settings.scheduler_dispatch_interval_ms / 1_000,
            id=DISPATCH_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.resume()
        logger.info(
            "scheduler_started",
            active_tasks=len(active_task_ids),
            interrupted_runs=interrupted,
            max_workers=self.settings.scheduler_max_workers,
        )

    def stop(self) -> None:
        if self.running:
            self.scheduler.shutdown(wait=False)
        self.executor.shutdown(wait=False, cancel_futures=True)
        logger.info("scheduler_stopped")

    def sync_task(self, task_id: UUID) -> None:
        if not self.settings.scheduler_enabled:
            return
        with Session(get_engine()) as session:
            task = SchedulerRepository(session).get_task(task_id)
            job_id = self.task_job_id(task_id)
            if task is None or task.state != TaskState.ACTIVE.value:
                self._remove_job_if_present(job_id)
                return

            schedule = schedule_adapter.validate_python(task.schedule)
            if isinstance(schedule, OnceSchedule) and schedule.run_at < (
                datetime.now(UTC)
                - timedelta(seconds=self.settings.scheduler_misfire_grace_seconds)
            ):
                SchedulerRepository(session).add_run(
                    task,
                    trigger_type=TriggerType.SCHEDULED,
                    status=RunStatus.SKIPPED,
                    error_message="The one-time schedule exceeded its misfire grace period.",
                    scheduled_at=schedule.run_at,
                )
                task.state = TaskState.COMPLETED.value
                task.version += 1
                task.updated_at = datetime.now(UTC)
                session.commit()
                self._remove_job_if_present(job_id)
                return
            trigger = build_trigger(schedule)
            self.scheduler.add_job(
                self._enqueue_scheduled_run,
                trigger=trigger,
                id=job_id,
                args=[task.id],
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=self.settings.scheduler_misfire_grace_seconds,
            )

    def next_run_at(self, task_id: UUID) -> datetime | None:
        if not self.running:
            return None
        job = self.scheduler.get_job(self.task_job_id(task_id))
        return job.next_run_time if job is not None else None

    def enqueue_manual_run(self, task_id: UUID):
        if not self.settings.scheduler_enabled:
            raise SchedulerDisabledError("scheduler is disabled")
        with self._enqueue_lock:
            with Session(get_engine()) as session:
                service = SchedulerService(session, self.registry)
                run = service.enqueue_run(
                    task_id,
                    trigger_type=TriggerType.MANUAL,
                    max_queued_runs=self.settings.scheduler_max_queued_runs,
                )
                session.commit()
                session.refresh(run)
                return run

    def dispatch_queued_runs(self) -> None:
        if not self._dispatch_lock.acquire(blocking=False):
            return
        try:
            with self._futures_lock:
                slots = self.settings.scheduler_max_workers - len(self._futures)
            if slots <= 0:
                return

            with Session(get_engine()) as session:
                repository = SchedulerRepository(session)
                run_ids = repository.claim_queued_runs(slots)
                run_context = {
                    run.id: (run.task_id, run.task_type)
                    for run in (repository.get_run(run_id) for run_id in run_ids)
                    if run is not None
                }
                session.commit()

            for run_id in run_ids:
                logger.info(
                    "task_run_claimed",
                    message="Supervisor 已领取任务运行，准备启动执行器。",
                    run_id=str(run_id),
                    task_id=str(run_context.get(run_id, (None, None))[0]) if run_id in run_context else None,
                    task_type=run_context.get(run_id, (None, None))[1] if run_id in run_context else None,
                    source="scheduler",
                    worker_id=None,
                    started=False,
                )

            for run_id in run_ids:
                try:
                    future = self.executor.submit(self._execute_run, run_id)
                except RuntimeError as exc:
                    self._finish_dispatch_failure(run_id, exc)
                    continue
                with self._futures_lock:
                    self._futures[future] = run_id
                future.add_done_callback(self._on_run_future_done)
        finally:
            self._dispatch_lock.release()

    def _enqueue_scheduled_run(self, task_id: UUID) -> None:
        try:
            with self._enqueue_lock:
                with Session(get_engine()) as session:
                    SchedulerService(session, self.registry).enqueue_run(
                        task_id,
                        trigger_type=TriggerType.SCHEDULED,
                        max_queued_runs=self.settings.scheduler_max_queued_runs,
                        scheduled_at=datetime.now(UTC),
                    )
                    session.commit()
        except TaskConflictError:
            logger.info("scheduled_run_ignored", task_id=str(task_id))
        except Exception:
            logger.exception("scheduled_run_enqueue_failed", task_id=str(task_id))

    def _execute_run(self, run_id: UUID) -> None:
        task_id: UUID | None = None
        task_type: str | None = None
        try:
            with Session(get_engine()) as session:
                repository = SchedulerRepository(session)
                run = repository.get_run(run_id)
                if run is None or run.status != RunStatus.RUNNING.value:
                    return
                task = repository.get_task(run.task_id)
                if task is None or task.state == TaskState.ARCHIVED.value:
                    repository.finish_run(
                        run_id,
                        status=RunStatus.SKIPPED,
                        error_message="Task was archived before execution.",
                    )
                    session.commit()
                    return
                task_id = run.task_id
                task_type = run.task_type
                parameters = dict(run.parameters)
                parameter_version = run.parameter_version

            # Context variables make every nested task log searchable by this run.
            with bound_contextvars(
                task_id=str(task_id), run_id=str(run_id), task_type=task_type
            ):
                logger.info(
                    "task_run_started",
                    message="Supervisor 已启动任务运行，执行范围按冻结参数开始。",
                    parameter_version=parameter_version,
                    worker_id=f"task-worker:{run_id}",
                    source="scheduler", scope="frozen_parameters",
                )
                definition = self.registry.require(task_type)
                if parameter_version != definition.parameter_version:
                    raise ValueError(
                        f"unsupported parameter version {parameter_version} "
                        f"for task type {task_type}"
                    )
                validated_parameters = definition.parameters_model.model_validate(parameters)
                result = definition.handler(
                    TaskContext(task_id=task_id, run_id=run_id),
                    validated_parameters,
                )
                if result is not None and not isinstance(result, dict):
                    raise TypeError("task handlers must return a dictionary or None")

                with Session(get_engine()) as session:
                    SchedulerRepository(session).finish_run(
                        run_id,
                        status=RunStatus.SUCCEEDED,
                        result=result,
                    )
                    session.commit()
                logger.info(
                    "task_run_succeeded",
                    message=f"任务运行执行成功：范围为task={task_id}, run={run_id}, task_type={task_type}，结果已写入。",
                    has_result=result is not None,
                    task_id=str(task_id), run_id=str(run_id), task_type=task_type,
                    source="scheduler", worker_id=f"task-worker:{run_id}", exit_code=0, completion_marker="reported",
                )
                logger.info(
                    "task_run_terminal_written",
                    message=f"任务运行成功终态已写入：范围为task={task_id}, run={run_id}, task_type={task_type}，退出码0。",
                    task_id=str(task_id), run_id=str(run_id), task_type=task_type,
                    source="scheduler", worker_id=f"task-worker:{run_id}", exit_code=0,
                    completion_marker="reported", error_type=None,
                )
        except Exception as exc:
            logger.exception(
                "task_run_failed",
                message=f"任务运行执行失败：范围为task={task_id}, run={run_id}, task_type={task_type}，错误为{type(exc).__name__}。",
                run_id=str(run_id),
                task_id=str(task_id) if task_id is not None else None,
                task_type=task_type,
                source="scheduler", worker_id=f"task-worker:{run_id}", exit_code=None, completion_marker=None,
                error_type=type(exc).__name__, error_message=str(exc)[:10_000],
                failure_phase="handler",
            )
            with Session(get_engine()) as session:
                SchedulerRepository(session).finish_run(
                    run_id,
                    status=RunStatus.FAILED,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:10_000],
                )
                session.commit()
            logger.info("task_run_terminal_written", message=f"任务运行失败终态已写入：范围为task={task_id}, run={run_id}, task_type={task_type}，错误为{type(exc).__name__}。", task_id=str(task_id) if task_id else None, run_id=str(run_id), task_type=task_type, source="scheduler", worker_id=f"task-worker:{run_id}", exit_code=None, completion_marker=None, error_type=type(exc).__name__, error_message=str(exc)[:10_000])

    def _on_run_future_done(self, future: Future[Any]) -> None:
        with self._futures_lock:
            run_id = self._futures.pop(future, None)
        if run_id is None:
            return
        # Metadata enrichment is best effort: an unavailable database must not
        # prevent the callback from compensating a crashed worker's RUNNING row.
        run_meta = {
            "task_id": None,
            "task_type": None,
            "worker_id": f"task-worker:{run_id}",
        }
        try:
            with Session(get_engine()) as session:
                observed = SchedulerRepository(session).get_run(run_id)
                if observed is not None:
                    run_meta = {
                        "task_id": str(observed.task_id) if observed.task_id else None,
                        "task_type": observed.task_type,
                        "worker_id": observed.worker_id or f"task-worker:{run_id}",
                    }
        except Exception as exc:
            logger.warning(
                "task_run_metadata_unavailable",
                message="任务运行日志元数据读取失败，将使用运行标识继续处理终态。",
                run_id=str(run_id),
                source="scheduler",
                **run_meta,
                error_type=type(exc).__name__,
            )
        if future.cancelled():
            logger.warning("task_run_cancel_requested", message="收到任务取消请求，正在结束运行。", run_id=str(run_id), source="scheduler", worker_id=None, exit_code=None, completion_marker=None, error_type="SchedulerStopped")
            logger.info("task_run_worker_exited", message="任务执行器因调度器停止而退出，运行已取消。", run_id=str(run_id), source="scheduler", **run_meta, exit_code=None, completion_marker="cancelled", error_type="SchedulerStopped")
            self._finish_running_run(
                run_id,
                status=RunStatus.INTERRUPTED,
                error_type="SchedulerStopped",
                error_message="The scheduler stopped before execution started.",
            )
            return

        try:
            future.result()
            logger.info("task_run_worker_exited", message="任务执行器正常退出，运行结果已处理。", run_id=str(run_id), source="scheduler", **run_meta, exit_code=0, completion_marker="reported", error_type=None)
        except BaseException as exc:
            # _execute_run normally records handler failures itself. This covers
            # failures while recording that result, so a dead worker cannot leave
            # the database row consuming the task's only concurrency slot.
            logger.exception(
                "task_run_worker_crashed",
                message="任务执行器异常退出，正在补偿结束运行记录。",
                run_id=str(run_id),
                source="scheduler", **run_meta, exit_code=None, completion_marker=None,
                failure_phase="worker_crash",
            )
            try:
                self._finish_running_run(
                    run_id,
                    status=RunStatus.FAILED,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:10_000]
                    or "Task worker exited before reporting its result.",
                )
                logger.info(
                    "task_run_terminal_written",
                    message=f"任务运行失败终态已写入：运行执行器异常退出，run={run_id}，错误为{type(exc).__name__}。",
                    run_id=str(run_id), source="scheduler", **run_meta,
                    exit_code=None, completion_marker=None,
                    error_type=type(exc).__name__, error_message=str(exc)[:10_000],
                )
            except Exception:
                logger.exception(
                    "task_run_failure_recovery_failed",
                    message="任务执行器异常后的运行记录补偿失败。",
                    run_id=str(run_id),
                )

    def _finish_dispatch_failure(self, run_id: UUID, exc: Exception) -> None:
        self._finish_running_run(
            run_id,
            status=RunStatus.FAILED,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    def _finish_running_run(
        self,
        run_id: UUID,
        *,
        status: RunStatus,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Finish only a lingering running row without overwriting terminal state.

        The conditional repository update makes this method safe for worker and
        callback recovery paths to race: whichever finishes first wins, while a
        completed, skipped, or interrupted run remains unchanged.
        """
        with Session(get_engine()) as session:
            finished = SchedulerRepository(session).finish_run(
                run_id,
                status=status,
                error_type=error_type,
                error_message=error_message,
            )
            session.commit()
        if finished:
            logger.info(
                "task_run_terminal_written",
                message=f"任务运行终态已写入：run={run_id}，状态={status.value}，调度路径已退出。",
                run_id=str(run_id), source="scheduler", worker_id=f"task-worker:{run_id}",
                exit_code=None, completion_marker="cancelled" if status is RunStatus.INTERRUPTED else None,
                error_type=error_type, error_message=error_message,
            )
        return finished

    def _remove_job_if_present(self, job_id: str) -> None:
        if self.scheduler.get_job(job_id) is not None:
            self.scheduler.remove_job(job_id)

    @staticmethod
    def task_job_id(task_id: UUID) -> str:
        return f"{TASK_JOB_PREFIX}{task_id}"
