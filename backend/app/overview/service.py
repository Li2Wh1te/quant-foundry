"""Aggregate full scheduler history instead of counting a recent API page.

Only bounded presentation lists are limited. Counts, source refresh timestamps,
and latest-outcome selection operate on every matching persisted record.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskRegistry
from app.overview.schemas import (
    AttentionTask, OperationsOverview, OverviewMetrics, OverviewRun, SourceOverview,
)


ATTENTION_STATUSES = ("failed", "interrupted", "timed_out", "indeterminate")


def shanghai_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Use a half-open local calendar day, independent of the server timezone."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("overview time must be timezone-aware")
    zone = ZoneInfo("Asia/Shanghai")
    day = now.astimezone(zone).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


class OverviewService:
    def __init__(self, session: Session, registry: TaskRegistry) -> None:
        self.session = session
        # The application currently has one implemented provider. Adding a
        # provider requires an explicit catalog/configuration mapping here;
        # a registry entry alone must never imply an available connection.
        self.definitions = {
            item.key: item for item in registry.list() if item.source_key == "tushare"
        }

    def read(self, *, tushare_configured: bool, now: datetime | None = None) -> OperationsOverview:
        now = now or datetime.now(UTC)
        day_start, day_end = shanghai_day_bounds(now)
        keys = tuple(self.definitions)
        active = ScheduledTask.state != "archived"
        task_scope = ScheduledTask.task_type.in_(keys)
        run_scope = TaskRun.task_type.in_(keys)

        active_tasks = self.session.scalar(select(func.count()).select_from(ScheduledTask).where(
            task_scope, ScheduledTask.state == "active",
        )) or 0
        # Historical day totals include archived tasks: archiving a definition
        # must not erase executions which actually started today.
        today = self.session.execute(select(
            func.count(TaskRun.id),
            func.count(TaskRun.id).filter(TaskRun.status == "succeeded"),
        ).where(run_scope, TaskRun.started_at >= day_start, TaskRun.started_at < day_end)).one()
        pending = self.session.execute(select(
            func.count(TaskRun.id).filter(TaskRun.status == "queued"),
            func.count(TaskRun.id).filter(TaskRun.status == "running"),
        ).join(ScheduledTask, ScheduledTask.id == TaskRun.task_id).where(
            run_scope, task_scope, active,
        )).one()

        # Rank terminal *actual executions* before filtering for failures. A
        # later success/cancellation clears an older error; a skipped dispatch
        # does not. During a running retry, the last failure remains visible.
        ranked = select(
            TaskRun.id.label("run_id"),
            func.row_number().over(
                partition_by=TaskRun.task_id,
                order_by=(TaskRun.started_at.desc(), TaskRun.created_at.desc(), TaskRun.id.desc()),
            ).label("rank"),
        ).where(
            run_scope, TaskRun.started_at.is_not(None),
            TaskRun.status.not_in(("queued", "running", "skipped")),
        ).subquery()
        issues = select(TaskRun.id).join(ranked, ranked.c.run_id == TaskRun.id).join(
            ScheduledTask, ScheduledTask.id == TaskRun.task_id,
        ).where(ranked.c.rank == 1, TaskRun.status.in_(ATTENTION_STATUSES), task_scope, active)
        attention_count = self.session.scalar(select(func.count()).select_from(issues.subquery())) or 0
        order = (TaskRun.created_at.desc(), TaskRun.id.desc())
        issue_rows = self.session.execute(select(TaskRun, ScheduledTask.name).join(
            ScheduledTask, ScheduledTask.id == TaskRun.task_id,
        ).where(TaskRun.id.in_(issues)).order_by(*order).limit(10)).all()
        issue_ids = [run.task_id for run, _ in issue_rows]
        pending_attempts = self.session.execute(select(
            TaskRun.task_id, TaskRun.created_at, TaskRun.started_at,
        ).where(
            run_scope, TaskRun.task_id.in_(issue_ids), TaskRun.status.in_(("queued", "running")),
        )).all() if issue_ids else []
        failures = {run.task_id: run for run, _ in issue_rows}
        # An older concurrent execution is not a retry of a newer failure.
        # Running attempts follow actual start order; queued attempts have no
        # start time, so compare their enqueue time with the failed outcome.
        retries = {
            task_id for task_id, created_at, started_at in pending_attempts
            if (started_at is not None and started_at > failures[task_id].started_at)
            or (started_at is None and created_at > failures[task_id].finished_at)
        }
        recent = self.session.execute(select(TaskRun, ScheduledTask.name).join(
            ScheduledTask, ScheduledTask.id == TaskRun.task_id,
        ).where(run_scope).order_by(*order).limit(4)).all()
        last_success = self.session.scalar(select(func.max(TaskRun.finished_at)).where(
            run_scope, TaskRun.status == "succeeded", TaskRun.started_at.is_not(None),
        ))
        return OperationsOverview(
            generated_at=now, day_start=day_start, day_end=day_end,
            metrics=OverviewMetrics(
                configured_sources=int(tushare_configured), total_sources=1,
                active_tasks=active_tasks, queued_runs=pending[0], running_runs=pending[1],
                today_runs=today[0], today_succeeded=today[1], attention_tasks=attention_count,
            ),
            sources=[SourceOverview(
                key="tushare", name="Tushare", configured=tushare_configured,
                active_tasks=active_tasks, last_success_at=last_success,
            )],
            recent_runs=[self._run(run, name) for run, name in recent],
            attention=[AttentionTask(run=self._run(run, name), retrying=run.task_id in retries)
                       for run, name in issue_rows],
        )

    def _run(self, run: TaskRun, task_name: str) -> OverviewRun:
        definition = self.definitions[run.task_type]
        duration = None
        if run.started_at is not None and run.finished_at is not None:
            duration = max(0.0, (run.finished_at - run.started_at).total_seconds())
        return OverviewRun(
            id=run.id, task_id=run.task_id, task_name=task_name, task_type=run.task_type,
            task_type_name=definition.name, task_type_english_name=definition.english_name,
            source_key=definition.source_key, status=run.status,
            created_at=run.created_at, started_at=run.started_at, finished_at=run.finished_at,
            duration_seconds=duration,
        )
