"""Opt-in product-owned recovery of operational backfill pauses.

No paused task is guessed to be an operational pause. Enrollment requires an
explicit ID/version plan. Any later operator edit invalidates that enrollment;
resumption uses the ordinary scheduler service with optimistic version checks.
"""
import argparse
import json
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import select, exists, text
from sqlalchemy.orm import Session

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.performance_models import BackfillResumeWatch
from app.scheduling.models import ScheduledTask, TaskRun

logger = structlog.get_logger(__name__)
TASK_TYPES = ('foundation.formalize_local_updates', 'foundation.formalize_local_table_updates')


def parameters_hash(task):
    return digest('foundation-resume-task-v1', [task.task_type, task.parameter_version, task.parameters])


def _eligible(session, task):
    if task.state != 'paused' or task.task_type not in TASK_TYPES or task.schedule.get('type') == 'once':
        return False
    if session.scalar(select(exists().where(TaskRun.task_id == task.id, TaskRun.status.in_(('queued', 'running'))))):
        return False
    last = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id)
        .order_by(TaskRun.created_at.desc(), TaskRun.id.desc()).limit(1))
    return last is not None and isinstance(last.result, dict) and last.result.get('status') == 'waiting_backfill'


def plan(session):
    """Read-only candidate plan; operators must review before enrollment."""
    result = []
    for task in session.scalars(select(ScheduledTask).where(ScheduledTask.state == 'paused',
            ScheduledTask.task_type.in_(TASK_TYPES)).order_by(ScheduledTask.id)):
        if _eligible(session, task):
            result.append(dict(task_id=str(task.id), version=task.version, name=task.name,
                native_dataset=task.parameters.get('native_dataset'), parameters_hash=parameters_hash(task)))
    return dict(schema_version=1, tasks=result)


def enroll(session, document):
    if (set(document) != {'schema_version', 'tasks'} or document['schema_version'] != 1
            or not isinstance(document['tasks'], list) or len(document['tasks']) > 1000):
        raise ValueError('Invalid resumption plan')
    selected = set()
    for item in document['tasks']:
        tid = UUID(item['task_id'])
        if tid in selected or type(item['version']) is not int:
            raise ValueError('Duplicate or invalid task version')
        selected.add(tid)
        task = session.scalar(select(ScheduledTask).where(ScheduledTask.id == tid).with_for_update())
        if (task is None or task.version != item['version'] or not _eligible(session, task)
                or parameters_hash(task) != item['parameters_hash']):
            raise FoundationError('RESUME_PLAN_CHANGED', '暂停任务已变化或仍有运行实例，请重新生成接管计划。')
        key = (task.id, task.version)
        if session.get(BackfillResumeWatch, key) is None:
            session.add(BackfillResumeWatch(task_id=task.id, task_version=task.version,
                parameters_hash=parameters_hash(task), state='waiting', reason=None,
                created_at=now(), checked_at=now()))
    session.flush()
    return len(selected)


def _ready(session, task):
    params = task.parameters
    native = params['native_dataset']
    if task.task_type == TASK_TYPES[1]:
        from app.data_foundation.table_record_updates import ready_capture
        return ready_capture(session, capture_ref_id=UUID(params['capture_ref_id']), native_dataset=native)['status'] == 'ready'
    from app.data_foundation.intake_models import CampaignScan, Scan, ScanSeal, ScanFailure
    from app.data_foundation.record_adapters import SOURCE_DATASETS
    from app.data_foundation.record_work import series_for
    from app.data_foundation.work import scope_key
    from app.data_foundation.backfill_settlement import probe_page
    link = session.get(CampaignScan, (UUID(params['backfill_campaign_id']), native))
    if link is None:
        return False
    scan = session.get(Scan, link.scan_id)
    if (scan.status != 'completed' or session.get(ScanSeal, scan.id) is None
            or session.scalar(select(exists().where(ScanFailure.scan_id == scan.id)))):
        return False
    if session.get(ScanSeal, scan.id).target_count == 0:
        # Zero business inputs still require their separately published empty
        # scope receipt; a captured empty scan alone is not final acceptance.
        from app.data_foundation.models import SourceRef, Baseline
        from app.data_foundation.scope_settlement import DATASET, DOMAIN
        from app.data_foundation.full_formalization import published_sources
        event = digest('empty-local-scope-v1', ['tonghuashun', native, scan.id,
                                               UUID(params['backfill_campaign_id'])])
        source = session.scalar(select(SourceRef).join(Baseline, Baseline.id == SourceRef.baseline_id)
            .where(Baseline.source == 'foundation', Baseline.dataset == DATASET, Baseline.event_key == event))
        return source is not None and source.id in published_sources(session, [source.id], 'foundation', DOMAIN)
    dataset = SOURCE_DATASETS[('tonghuashun', native)]
    scope = scope_key(dataset, 1, 'default', series_for(dataset, 'tonghuashun'))
    # The recovery gate is strict: processed quarantines do not resume a pause
    # enrolled under the handoff's fixed == accepted publication policy.
    return probe_page(session, native_dataset=native, scan_id=scan.id, scope=scope)['accepted_complete']


def advance(engine, registry, *, limit=4, runtime_digest=''):
    from app.scheduling.service import SchedulerService
    from app.scheduling.schemas import TaskState
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Resume limit must be 1 to 20 tasks')
    key = int(digest('foundation-resume-supervisor', 'v1')[:15], 16)
    resumed = []
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as gate:
        if not gate.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}):
            return resumed
        try:
            with Session(engine) as session:
                keys = session.execute(select(BackfillResumeWatch.task_id, BackfillResumeWatch.task_version)
                    .where(BackfillResumeWatch.state.in_(('waiting', 'activated')))
                    .order_by(BackfillResumeWatch.checked_at, BackfillResumeWatch.task_id).limit(limit)).all()
            for tid, version in keys:
                try:
                    with Session(engine) as session, session.begin():
                        session.execute(text("SET LOCAL lock_timeout = '2s'"))
                        session.execute(text("SET LOCAL statement_timeout = '5s'"))
                        watch = session.get(BackfillResumeWatch, (tid, version))
                        task = session.get(ScheduledTask, tid)
                        watch.checked_at = now()
                        if watch.state == 'activated':
                            # A crash between the state transition and in-memory
                            # scheduling must not lose the activation. Return it
                            # again until sync_task explicitly acknowledges it.
                            if (task is not None and task.state == 'active' and task.version == version + 1
                                    and parameters_hash(task) == watch.parameters_hash):
                                resumed.append(tid)
                            else:
                                watch.state, watch.reason = 'cancelled', 'TASK_CHANGED'
                            continue
                        if (task is None or task.state != 'paused' or task.version != version
                                or task.task_type not in TASK_TYPES or parameters_hash(task) != watch.parameters_hash):
                            watch.state, watch.reason = 'cancelled', 'TASK_CHANGED'
                            continue
                        if not runtime_digest or task.parameters.get('runtime_digest') != runtime_digest:
                            watch.reason = 'RUNTIME_NOT_CURRENT'
                            continue
                        if session.scalar(select(exists().where(TaskRun.task_id == tid, TaskRun.status.in_(('queued', 'running'))))):
                            watch.reason = 'RUNS_OUTSTANDING'
                            continue
                        if not _ready(session, task):
                            watch.reason = 'WAITING_ACCEPTED_BACKFILL'
                            continue
                        SchedulerService(session, registry).change_state(tid, expected_version=version, target=TaskState.ACTIVE)
                        watch.state, watch.reason = 'activated', None
                    resumed.append(tid)
                except Exception:
                    # A lock timeout or stale operator version cannot prevent
                    # another independently enrolled task from recovering.
                    logger.exception('foundation_resume_deferred', task_id=str(tid),
                        message='回填恢复检查未完成，保留接管记录待下次核查。')
                    # Rotate failed checks too, without recording readiness or
                    # swallowing a manual state/version change.
                    try:
                        with Session(engine) as session, session.begin():
                            session.execute(text("SET LOCAL lock_timeout = '1s'"))
                            watch = session.get(BackfillResumeWatch, (tid, version))
                            if watch is not None and watch.state == 'waiting':
                                watch.checked_at, watch.reason = now(), 'CHECK_DEFERRED'
                    except Exception:
                        logger.warning('foundation_resume_rotation_deferred', task_id=str(tid))
        finally:
            gate.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
    return resumed


def acknowledge(engine, task_id):
    """Record successful scheduler synchronization, never alter a task here."""
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL lock_timeout = '2s'"))
        task = session.get(ScheduledTask, task_id)
        for watch in session.scalars(select(BackfillResumeWatch).where(
                BackfillResumeWatch.task_id == task_id, BackfillResumeWatch.state == 'activated').with_for_update()):
            watch.checked_at = now()
            if (task is not None and task.state == 'active' and task.version == watch.task_version + 1
                    and parameters_hash(task) == watch.parameters_hash):
                watch.state, watch.reason = 'resumed', None
            else:
                watch.state, watch.reason = 'cancelled', 'TASK_CHANGED'


def main():
    parser = argparse.ArgumentParser(description='接管已明确批准的临时回填暂停；不自动接管人工暂停。')
    parser.add_argument('command', choices=['plan', 'enroll'])
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    import app.models  # noqa: F401
    from app.db.session import get_engine
    with Session(get_engine()) as session:
        if args.command == 'plan':
            document = plan(session)
            # Do not silently overwrite a previously reviewed enrollment plan.
            with args.plan.open('x', encoding='utf-8') as output:
                output.write(encode(document)+'\n')
            print(encode(dict(status='review_required', tasks=len(document['tasks']), path=str(args.plan))))
        else:
            if not args.apply:
                parser.error('enroll requires --apply after reviewing the plan')
            count = enroll(session, json.loads(args.plan.read_text(encoding='utf-8')))
            session.commit()
            print(encode(dict(status='enrolled', tasks=count)))


if __name__ == '__main__':
    main()
