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
                session.commit()

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
            logger.info("task_run_succeeded", task_id=str(task_id), run_id=str(run_id))
        except Exception as exc:
            logger.exception("task_run_failed", run_id=str(run_id))
            with Session(get_engine()) as session:
                SchedulerRepository(session).finish_run(
                    run_id,
                    status=RunStatus.FAILED,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:10_000],
                )
                session.commit()

    def _on_run_future_done(self, future: Future[Any]) -> None:
        with self._futures_lock:
            run_id = self._futures.pop(future, None)
        if run_id is not None and future.cancelled():
            with Session(get_engine()) as session:
                SchedulerRepository(session).finish_run(
                    run_id,
                    status=RunStatus.INTERRUPTED,
                    error_type="SchedulerStopped",
                    error_message="The scheduler stopped before execution started.",
                )
                session.commit()

    def _finish_dispatch_failure(self, run_id: UUID, exc: Exception) -> None:
        with Session(get_engine()) as session:
            SchedulerRepository(session).finish_run(
                run_id,
                status=RunStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            session.commit()

    def _remove_job_if_present(self, job_id: str) -> None:
        if self.scheduler.get_job(job_id) is not None:
            self.scheduler.remove_job(job_id)

    @staticmethod
    def task_job_id(task_id: UUID) -> str:
        return f"{TASK_JOB_PREFIX}{task_id}"
