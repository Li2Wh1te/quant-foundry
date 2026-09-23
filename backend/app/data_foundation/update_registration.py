"""One-time handoff of all local formalization schedules after deployment.

Run inside the newly deployed backend with --apply. Existing update tasks are
paused and their active invocations finish before parameters change. Backfill
workers are untouched. Source discovery still gates each dataset on its own
actual fixed release, so a schedule cannot turn a capture into publication.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data_foundation.__main__ import local_execution
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.execution import register_archive
from app.data_foundation.record_adapters import SOURCE_DATASETS
from app.data_foundation.scope_settlement import TABLE_NAMES
from app.data_ingestion.tonghuashun.contracts import DATASETS
from app.db.session import get_engine

THS_TASK = 'foundation.formalize_local_updates'
TABLE_TASK = 'foundation.formalize_local_table_updates'


def api(method, path, payload=None):
    settings = get_settings()
    request = Request(f'http://127.0.0.1:{settings.server_port}/api/admin{path}',
        data=encode(payload).encode() if payload is not None else None,
        method=method, headers={'Authorization': 'Bearer ' + settings.api_token.get_secret_value(),
                                'Content-Type': 'application/json'})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def existing_tasks():
    tasks = []
    for offset in range(0, 20000, 500):
        page = api('GET', f'/tasks?limit=500&offset={offset}')
        tasks.extend(page)
        if len(page) < 500:
            return tasks
    raise FoundationError('TASK_ENUMERATION_INCOMPLETE', '持续任务清单超过安全分页范围。')


def desired(campaign_id, table_capture_ref_id, execution_id, runtime_digest):
    items = []
    for source, native in sorted(SOURCE_DATASETS):
        if source == 'tonghuashun':
            items.append(dict(task_type=THS_TASK, native_dataset=native,
                title=DATASETS[native].name,
                parameters=dict(native_dataset=native, backfill_campaign_id=str(campaign_id),
                    execution_id=str(execution_id), runtime_digest=runtime_digest,
                    source_limit=4, steps_per_source=10)))
        elif source == 'tushare':
            items.append(dict(task_type=TABLE_TASK, native_dataset=native,
                title=TABLE_NAMES[native],
                parameters=dict(native_dataset=native, capture_ref_id=str(table_capture_ref_id),
                    execution_id=str(execution_id), runtime_digest=runtime_digest,
                    source_limit=4, steps_per_source=10)))
    return items


def wait_task_runs(tasks, *, timeout_seconds=900):
    active = {task['id'] for task in tasks}
    deadline = monotonic() + timeout_seconds
    while active:
        remaining = set()
        for task_id in active:
            # A task can have more than ten recent runs. Inspect the complete
            # bounded server pagination before replacing its frozen inputs.
            for offset in range(0, 20000, 500):
                runs = api('GET', f'/tasks/{task_id}/runs?limit=500&offset={offset}')
                if any(row['status'] in ('queued', 'running') for row in runs):
                    remaining.add(task_id)
                    break
                if len(runs) < 500:
                    break
            else:
                raise FoundationError('RUN_ENUMERATION_INCOMPLETE', '旧持续任务运行记录超过安全分页范围。')
        if not remaining:
            return
        if monotonic() >= deadline:
            raise FoundationError('UPDATE_RUNS_ACTIVE', '旧持续任务仍有排队或运行调用，任务参数尚未切换。')
        active = remaining
        sleep(5)


def register_tasks(specs, *, apply=False):
    if len({(spec['task_type'], spec['native_dataset']) for spec in specs}) != len(specs):
        raise FoundationError('DUPLICATE_UPDATE_SPEC', '持续正式化提交清单含重复领域。')
    existing = existing_tasks()
    definitions = {row['key'] for row in api('GET', '/task-types')}
    if not {THS_TASK, TABLE_TASK} <= definitions:
        raise FoundationError('TASK_TYPE_MISSING', '新部署缺少本地正式化任务类型。')
    matched, unmatched = [], []
    for spec in specs:
        candidates = [task for task in existing if task['task_type'] == spec['task_type']
                      and task['parameters'].get('native_dataset') == spec['native_dataset']]
        if len(candidates) > 1:
            raise FoundationError('DUPLICATE_UPDATE_TASK', '同一领域存在多个持续正式化任务。')
        if candidates:
            matched.append((spec, candidates[0]))
        else:
            unmatched.append(spec)
    if not apply:
        return dict(status='planned_not_submitted', desired=len(specs), existing=len(matched),
                    new=len(unmatched), domains=[spec['native_dataset'] for spec in specs])
    changing = [(spec, task) for spec, task in matched if task['parameters'] != spec['parameters']]
    paused = []
    for _, task in changing:
        if task['state'] == 'active':
            task = api('POST', f"/tasks/{task['id']}/pause?version={task['version']}")
        if task['state'] != 'paused':
            raise FoundationError('UPDATE_TASK_STATE', '旧持续任务无法进入已暂停状态。')
        paused.append(task)
    wait_task_runs(paused)
    results = []
    start = datetime.now(timezone.utc) + timedelta(minutes=10)
    for ordinal, spec in enumerate(specs):
        candidates = [task for task in existing if task['task_type'] == spec['task_type']
                      and task['parameters'].get('native_dataset') == spec['native_dataset']]
        task = candidates[0] if candidates else None
        schedule = dict(type='interval', seconds=900,
                        start_at=(start + timedelta(seconds=ordinal * 4)).isoformat())
        if task:
            task = api('GET', f"/tasks/{task['id']}")
            if task['parameters'] != spec['parameters']:
                if task['state'] != 'paused':
                    raise FoundationError('UPDATE_TASK_STATE', '更换执行依赖前旧任务未暂停。')
                task = api('PATCH', f"/tasks/{task['id']}", dict(version=task['version'],
                    parameters=spec['parameters'], schedule=schedule, priority=90))
        else:
            task = api('POST', '/tasks', dict(name='持续正式化 · ' + spec['title'],
                description='按固定来源和正式发布凭据接续本地变化；历史观察保留独立发布。',
                task_type=spec['task_type'], parameters=spec['parameters'], schedule=schedule,
                concurrency_limit=1, overlap_policy='skip', priority=90))
        if task['state'] == 'paused':
            task = api('POST', f"/tasks/{task['id']}/resume?version={task['version']}")
        results.append(dict(dataset=spec['native_dataset'], task_type=spec['task_type'],
                            task_id=task['id'], state=task['state'], next_run_at=task.get('next_run_at')))
    return dict(status='submitted', desired=len(specs), tasks=results)


def main():
    import app.models  # All model edges must exist before execution registration.
    parser = argparse.ArgumentParser(description='统一接通全部本地来源的持续正式化更新。')
    parser.add_argument('--campaign-id', required=True, type=UUID)
    parser.add_argument('--table-capture-ref-id', required=True, type=UUID)
    parser.add_argument('--evidence-path', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    evidence = json.loads(args.evidence_path.read_text())
    image = evidence['image_digest']
    commit = os.environ.get('QF_RELEASE_COMMIT')
    if args.apply and (commit is None or len(commit) != 40):
        parser.error('正式提交需要 QF_RELEASE_COMMIT 指向合并后 main 的完整提交')
    if args.apply:
        with Session(get_engine()) as session, session.begin():
            execution = local_execution(session, commit, image)
            archive = register_archive(session, execution.id, evidence, args.evidence_path.parent)
            execution_id, archive_id = execution.id, archive.id
    else:
        execution_id, archive_id = UUID(int=0), None
    specs = desired(args.campaign_id, args.table_capture_ref_id, execution_id, image)
    outcome = register_tasks(specs, apply=args.apply)
    print(encode(dict(**outcome, execution_id=execution_id, archive_id=archive_id,
                      runtime_digest=image, campaign_id=args.campaign_id,
                      table_capture_ref_id=args.table_capture_ref_id)), flush=True)


if __name__ == '__main__':
    main()
