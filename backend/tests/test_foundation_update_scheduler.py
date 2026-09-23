"""Operator outcomes for partial, waiting and failed local update runs."""
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.data_foundation import scheduler_tasks as tasks
from app.scheduling.registry import TaskContext, task_registry


def parameters(**overrides):
    return tasks.LocalUpdateParameters(**(dict(native_dataset='fund_company',
        backfill_campaign_id=uuid4(), execution_id=uuid4(), runtime_digest='sha256:'+'a'*64) | overrides))


def test_registered_without_enabling_vendor_polling():
    definition = task_registry.require('foundation.formalize_local_updates')
    assert definition.name == '本地数据持续正式化'
    assert definition.english_name == 'Local Data Formalization Updates'
    assert definition.source_key is None
    for override in ({'native_dataset': 'unknown'}, {'source_limit': 1}, {'steps_per_source': 11}, {'runtime_digest': 'local'}):
        with pytest.raises(ValidationError):
            parameters(**override)


def test_partial_failure_is_not_success_and_retains_chinese_summary(monkeypatch):
    stamp = datetime(2026, 9, 23, tzinfo=timezone.utc)
    monkeypatch.setattr(tasks, 'get_engine', lambda: None)
    monkeypatch.setattr(tasks, 'advance_updates', lambda *a, **k: dict(status='advanced', items=[
        dict(observation_id=uuid4(), observed_at=stamp, status='published', steps=2),
        dict(observation_id=uuid4(), observed_at=stamp, status='failed', error_code='SOURCE_INVALID')]))
    with pytest.raises(tasks.FoundationUpdateError, match='发布 1 个，失败 1 个') as exc:
        tasks.execute(TaskContext(uuid4(), uuid4()), parameters())
    assert '2026-09-23 至 2026-09-23' in str(exc.value)
    assert '检查点已推进' in str(exc.value)


def test_waiting_and_no_change_never_claim_completed_publication(monkeypatch):
    monkeypatch.setattr(tasks, 'get_engine', lambda: None)
    monkeypatch.setattr(tasks, 'advance_updates', lambda *a, **k: dict(status='waiting_backfill', pending_backfill=17, items=[]))
    result = tasks.execute(TaskContext(uuid4(), uuid4()), parameters())
    assert result['pending_backfill'] == 17 and result['published'] == 0
    assert '固定全量发布尚未完成' in result['message']
    assert '检查点未推进' in result['message']
