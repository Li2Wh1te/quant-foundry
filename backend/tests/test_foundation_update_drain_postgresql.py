"""Deployment drains reuse committed candidates rather than restart the source."""
import importlib.util
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import session, pipeline_engine, fixture, isolated_series
from app.data_foundation.canonical import FoundationError
from app.data_foundation.source_refs import register_observation
from app.data_foundation.record_pipeline import register_job, advance_job
from app.data_foundation.batch_models import BatchWork
from app.data_foundation.work_models import Work, Candidate
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from app.data_ingestion.tonghuashun.contracts import exact_json, content_hash
from app.scheduling.models import ScheduledTask

spec = importlib.util.spec_from_file_location('drain_updates', Path(__file__).resolve().parents[2]/'scripts/drain_foundation_updates.py')
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


def setup(session, tmp_path, count=401):
    (_, _, execution), image = fixture(session, tmp_path)
    body = {'item': [{'thscode': f'DRAIN{i}.SH', 'asset_type': 'fund-etf'} for i in range(count)]}
    obs = Observation(id=uuid4(), dataset='tickers', subject='fund-etf', variant='default',
        observed_at=datetime.now(timezone.utc), request_json='{}', data_json=exact_json(body),
        content_hash=content_hash(body), row_count=count, chain_depth=0)
    session.add(obs); session.flush()
    source = register_observation(session, obs.id, execution)
    batch = register_job(session, source_ref_id=source.id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path)
    origin = session.scalar(select(Work).join(BatchWork, BatchWork.work_id == Work.id).where(BatchWork.batch_id == batch.id))
    task = ScheduledTask(name='部署收尾测试', task_type=operator.TASK_TYPE,
        parameters={'execution_id': str(execution), 'runtime_digest': image, 'native_dataset': 'tickers'},
        schedule={'type': 'interval', 'seconds': 180}, state='paused')
    session.add(task); session.flush()
    ids = batch.id, origin.id, task.id
    session.commit()
    return ids, dict(execution_id=execution, runtime_digest=image, archive_root=tmp_path)


def test_dry_run_then_bounded_drain_keeps_origin_cursor_candidates_and_publication(session, tmp_path):
    (batch, origin, task), options = setup(session, tmp_path)
    advance_job(session.bind, batch, runtime_digest=options['runtime_digest'], archive_root=tmp_path, steps=1)
    session.expire_all()
    before = set(session.scalars(select(Candidate.id).where(Candidate.work_id == origin)))
    assert len(before) == session.get(Work, origin).cursor == 200
    session.rollback()
    dry = operator.drain(session.bind, **options)
    assert dry['status'] == 'pending' and dry['pending_batches'] == 1 and not dry['items']
    step = operator.drain(session.bind, **options, publish=True, steps=1)
    assert step['status'] == 'pending'
    session.expire_all()
    after = set(session.scalars(select(Candidate.id).where(Candidate.work_id == origin)))
    assert before < after and len(after) == session.get(Work, origin).cursor == 400
    session.rollback()
    done = operator.drain(session.bind, **options, publish=True, steps=10)
    assert done['status'] == 'drained' and done['published'] == 1 and done['quarantined'] == 0
    again = operator.drain(session.bind, **options, publish=True, steps=10)
    assert again['status'] == 'drained' and not again['items']
    assert session.scalar(select(func.count()).select_from(Work).where(Work.source_ref_id == session.get(Work, origin).source_ref_id)) == 1


def test_active_schedule_and_wrong_runtime_refuse_advancement(session, tmp_path):
    (_, origin, task_id), options = setup(session, tmp_path, 1)
    task = session.get(ScheduledTask, task_id)
    task.state = 'active'; session.commit()
    with pytest.raises(FoundationError, match='全部暂停'):
        operator.drain(session.bind, **options, publish=True)
    task.state = 'paused'; session.commit()
    with pytest.raises(FoundationError, match='依赖不一致'):
        operator.drain(session.bind, **(options | {'runtime_digest': 'sha256:'+'0'*64}), publish=True)
    session.expire_all()
    assert session.get(Work, origin).cursor == 0


def test_archive_and_operator_pause_are_still_authoritative(session, tmp_path):
    from app.data_foundation.batches import set_controls
    (batch, origin, _), options = setup(session, tmp_path, 1)
    set_controls(session, batch, pause_a=True, pause_b=True)
    session.commit()
    paused = operator.drain(session.bind, **options, publish=True)
    assert paused['status'] == 'pending' and paused['items'][0]['status'] == 'paused'
    set_controls(session, batch, pause_a=False, pause_b=False)
    session.commit()
    (tmp_path/(options['runtime_digest'].removeprefix('sha256:')+'.tar')).unlink()
    with pytest.raises(FoundationError, match='归档'):
        operator.drain(session.bind, **options, publish=True)
    session.expire_all()
    assert session.get(Work, origin).cursor == 0


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_paused_schedule_with_live_invocation_must_settle_first(session, tmp_path, status):
    from app.scheduling.models import TaskRun
    (_, origin, task_id), options = setup(session, tmp_path, 1)
    stamp = datetime.now(timezone.utc)
    task = session.get(ScheduledTask, task_id)
    session.add(TaskRun(task_id=task_id, task_version=task.version, task_type=operator.TASK_TYPE,
        trigger_type='manual', status=status, parameters=task.parameters, parameter_version=1,
        created_at=stamp, started_at=stamp if status == 'running' else None))
    session.commit()
    with pytest.raises(FoundationError, match='排队或运行'):
        operator.drain(session.bind, **options, publish=True)
    session.expire_all()
    assert session.get(Work, origin).cursor == 0
