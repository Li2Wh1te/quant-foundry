"""Only explicitly enrolled, unchanged operational pauses may auto-resume."""
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
import pytest
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session
from app.data_foundation import update_resumption as recovery
from app.data_foundation.performance_models import BackfillResumeWatch
from app.data_foundation.catalog import now
from app.data_foundation.canonical import FoundationError
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskRegistry


def paused(session):
    task = ScheduledTask(name='Recovery '+uuid4().hex, task_type=recovery.TASK_TYPES[0],
        parameters={'native_dataset':'fund_company', 'runtime_digest':'sha256:'+'a'*64},
        parameter_version=1, schedule={'type':'interval','seconds':900}, state='paused')
    session.add(task);session.flush()
    at = now()
    session.add(TaskRun(task_id=task.id, task_version=task.version, task_type=task.task_type,
        trigger_type='scheduled', status='succeeded', parameters=task.parameters, parameter_version=1,
        priority=0, available_at=at, created_at=at, started_at=at, finished_at=at,
        result={'status':'waiting_backfill'}))
    session.flush()
    return task


def plan_for(session, *tasks):
    ids = {str(t.id) for t in tasks}
    plan = recovery.plan(session)
    plan['tasks'] = [item for item in plan['tasks'] if item['task_id'] in ids]
    assert len(plan['tasks']) == len(tasks)
    return plan


def test_enrollment_is_explicit_and_manual_version_changes_disarm_it(session, monkeypatch):
    task, manual = paused(session), paused(session)
    reviewed = plan_for(session, task)
    assert recovery.enroll(session, reviewed) == 1
    tid, mid = task.id, manual.id
    task.version += 1
    session.commit()
    monkeypatch.setattr(recovery, '_ready', lambda *_: pytest.fail('modified task was inspected'))
    assert recovery.advance(session.bind, TaskRegistry(), runtime_digest='sha256:'+'a'*64) == []
    with Session(session.bind) as check:
        assert check.get(ScheduledTask, tid).state == check.get(ScheduledTask, mid).state == 'paused'
        assert check.get(BackfillResumeWatch, (tid, 1)).state == 'cancelled'
        assert check.scalar(select(BackfillResumeWatch).where(BackfillResumeWatch.task_id == mid)) is None
        with pytest.raises(FoundationError, match='变化'):
            recovery.enroll(check, reviewed)


def test_resume_waits_for_accepted_backfill_and_retries_unacknowledged_activation(session, monkeypatch):
    task = paused(session)
    recovery.enroll(session, plan_for(session, task))
    tid = task.id
    session.commit()
    monkeypatch.setattr(recovery, '_ready', lambda *_: False)
    assert recovery.advance(session.bind, TaskRegistry(), runtime_digest='sha256:'+'a'*64) == []
    with Session(session.bind) as check:
        assert check.get(ScheduledTask, tid).state == 'paused'
        assert check.get(BackfillResumeWatch, (tid, 1)).reason == 'WAITING_ACCEPTED_BACKFILL'
    monkeypatch.setattr(recovery, '_ready', lambda *_: True)
    assert recovery.advance(session.bind, TaskRegistry(), runtime_digest='sha256:'+'b'*64) == []
    assert recovery.advance(session.bind, TaskRegistry(), runtime_digest='sha256:'+'a'*64) == [tid]
    with Session(session.bind) as check:
        assert check.get(ScheduledTask, tid).state == 'active'
        assert check.get(ScheduledTask, tid).version == 2
        assert check.get(BackfillResumeWatch, (tid, 1)).state == 'activated'
    # Simulate death before sync_task. A fresh process gets the same activation,
    # without bumping the task version or repeating its readiness gate.
    monkeypatch.setattr(recovery, '_ready', lambda *_: pytest.fail('activation readiness repeated'))
    assert recovery.advance(session.bind, TaskRegistry(), runtime_digest='sha256:'+'a'*64) == [tid]
    recovery.acknowledge(session.bind, tid)
    assert recovery.advance(session.bind, TaskRegistry(), runtime_digest='sha256:'+'a'*64) == []
    with Session(session.bind) as check:
        assert check.get(BackfillResumeWatch, (tid, 1)).state == 'resumed'
        assert check.get(ScheduledTask, tid).version == 2
