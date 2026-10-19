"""Recover bounded local-update continuations from durable entry metadata."""
import json
from datetime import UTC, datetime

from sqlalchemy import inspect, select

from .models import ScheduledTask, TaskRun
from .repository import SchedulerRepository, LOCAL_UPDATE_TASK_KEY
from .schemas import RunStatus, TaskState, TriggerType

def enqueue_pending_local_updates(session, registry, *, maximum_queued):
    """Keep at most one covering execution, including after process restarts.

    Continuation intent lives in data_store_entry_status, not an in-memory
    Future or a successful TaskRun. A worker crash therefore cannot erase it.
    Source gates precede task-row locks, matching ordinary enqueue/dispatch.
    The existing plan freezes parameters for every child run unchanged.
    """
    if registry.get(LOCAL_UPDATE_TASK_KEY) is None:
        return []
    connection = session.connection()
    if not inspect(connection).has_table('data_store_entry_status'):
        return []
    from app.data_sources.service import lock_source_gates
    from app.data_store.adapters.registry import ENTRIES
    from app.data_store.tables import entry_status
    from .service import SchedulerService

    lock_source_gates(session, registry)
    pending = {}
    for eid, raw in session.execute(select(entry_status.c.entry_id, entry_status.c.summary_json)):
        value = json.loads(raw) if isinstance(raw, str) else raw
        state = (value.get('refresh') or {}).get('continuation') or {}
        if state.get('eligible') is True and type(state.get('available_at')) is int:
            pending[eid] = state
    if not pending:
        return []
    repository = SchedulerRepository(session)
    if repository.count_runs(task_id=None, statuses=(RunStatus.QUEUED.value,)) >= maximum_queued:
        return []
    business = {e.spec.name: e.id for e in ENTRIES if e.business}

    def entries(parameters):
        return {business[name] for name in parameters.get('datasets', []) if name in business} if parameters.get('datasets') else set(business.values())

    tasks = list(session.scalars(select(ScheduledTask).where(
        ScheduledTask.task_type == LOCAL_UPDATE_TASK_KEY,
        ScheduledTask.state == TaskState.ACTIVE.value,
    ).order_by(ScheduledTask.id)))
    # Overlapping plans have one deterministic automatic owner. Existing
    # scheduled/manual runs covering an entry win over a generated follow-up.
    owner = {}
    for task in tasks:
        for eid in entries(task.parameters):
            owner.setdefault(eid, task.id)
    covered = set()
    for parameters in session.scalars(select(TaskRun.parameters).where(
            TaskRun.task_type == LOCAL_UPDATE_TASK_KEY,
            TaskRun.status.in_((RunStatus.QUEUED.value, RunStatus.RUNNING.value)))):
        covered.update(entries(parameters))
    created = []
    for task in tasks:
        wanted = [eid for eid in entries(task.parameters)
                  if eid in pending and eid not in covered and owner.get(eid) == task.id]
        if not wanted:
            continue
        if entries(task.parameters) & covered:
            continue
        locked = session.scalar(select(ScheduledTask).where(
            ScheduledTask.id == task.id, ScheduledTask.state == TaskState.ACTIVE.value,
        ).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if locked is None or not any(eid in entries(locked.parameters) for eid in wanted):
            continue
        if entries(locked.parameters) & covered:
            continue
        if repository.count_runs(task_id=task.id, statuses=(RunStatus.QUEUED.value, RunStatus.RUNNING.value)):
            continue
        if repository.count_runs(task_id=None, statuses=(RunStatus.QUEUED.value,)) >= maximum_queued:
            break
        latest = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id,
            TaskRun.started_at.is_not(None)).order_by(TaskRun.created_at.desc(), TaskRun.id.desc()).limit(1))
        if latest is not None and (latest.result or {}).get('continuation', {}).get('global_stop'):
            continue
        run = SchedulerService(session, registry).enqueue_run(task.id,
            trigger_type=TriggerType.SCHEDULED, max_queued_runs=maximum_queued,
            scheduled_at=repository.current_time())
        if run.status != RunStatus.QUEUED.value:
            continue
        # Preserve an unserved intent's age across process restarts, but a
        # completed execution has already received its turn. A follow-up must
        # not reuse that old age to jump ahead of other waiting writers on
        # every dispatch tick. Future backoff and newly advanced checkpoints
        # retain their actual eligibility time, without changing task priority.
        run.available_at = datetime.fromtimestamp(min(pending[eid]['available_at'] for eid in wanted), UTC)
        if latest is not None and latest.finished_at is not None:
            run.available_at = max(run.available_at, latest.finished_at)
        covered.update(entries(task.parameters))
        created.append(run.id)
    session.flush()
    return created
