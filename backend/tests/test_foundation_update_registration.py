"""One handoff submits every supported local channel without duplicate tasks."""
from copy import deepcopy
from uuid import UUID

from app.data_foundation import update_registration as registration
from app.data_foundation.scheduler_tasks import LocalUpdateParameters, TableUpdateParameters


def specs():
    return registration.desired(UUID(int=1), UUID(int=2), UUID(int=3), 'sha256:' + 'a' * 64)


def test_complete_schedule_manifest_has_valid_distinct_domains():
    planned = specs()
    assert len(planned) == 70
    assert len({(item['task_type'], item['native_dataset']) for item in planned}) == 70
    assert sum(item['task_type'] == registration.THS_TASK for item in planned) == 57
    assert sum(item['task_type'] == registration.TABLE_TASK for item in planned) == 13
    for item in planned:
        model = LocalUpdateParameters if item['task_type'] == registration.THS_TASK else TableUpdateParameters
        assert model.model_validate(item['parameters']).native_dataset == item['native_dataset']


def test_handoff_pauses_changed_task_and_is_repeatable(monkeypatch):
    selected = specs()[:2]
    old = dict(id='existing', task_type=selected[0]['task_type'],
               parameters=dict(selected[0]['parameters'], execution_id=str(UUID(int=9))),
               state='active', version=1, next_run_at=None)
    tasks = [old]
    calls = []

    def fake_api(method, path, payload=None):
        calls.append((method, path, deepcopy(payload)))
        if path == '/task-types':
            return [{'key': registration.THS_TASK}, {'key': registration.TABLE_TASK}]
        if path.startswith('/tasks?'):
            return deepcopy(tasks)
        if path == '/tasks/existing/runs?limit=500&offset=0':
            return []
        if method == 'GET' and path.startswith('/tasks/'):
            return deepcopy(next(task for task in tasks if task['id'] == path.split('/')[2]))
        if method == 'POST' and path.startswith('/tasks/existing/pause?'):
            old['state'] = 'paused'
            old['version'] += 1
            return deepcopy(old)
        if method == 'PATCH' and path == '/tasks/existing':
            assert old['state'] == 'paused'
            assert payload['version'] == old['version']
            old.update(parameters=payload['parameters'], schedule=payload['schedule'],
                       priority=payload['priority'])
            old['version'] += 1
            return deepcopy(old)
        if method == 'POST' and path.startswith('/tasks/existing/resume?'):
            old['state'] = 'active'
            old['version'] += 1
            return deepcopy(old)
        if method == 'POST' and path == '/tasks':
            task = dict(id='created', task_type=payload['task_type'],
                        parameters=payload['parameters'], state='active', version=1,
                        schedule=payload['schedule'], next_run_at=None)
            tasks.append(task)
            return deepcopy(task)
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(registration, 'api', fake_api)
    assert registration.register_tasks(selected)['status'] == 'planned_not_submitted'
    assert len(tasks) == 1
    result = registration.register_tasks(selected, apply=True)
    assert result['status'] == 'submitted' and len(result['tasks']) == 2
    assert old['parameters'] == selected[0]['parameters']
    assert any(path.startswith('/tasks/existing/pause?') for _, path, _ in calls)
    assert registration.register_tasks(selected, apply=True)['status'] == 'submitted'
    assert len(tasks) == 2
    assert sum(path == '/tasks' and method == 'POST' for method, path, _ in calls) == 1
