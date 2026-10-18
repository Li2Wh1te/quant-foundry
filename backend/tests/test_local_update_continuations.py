"""Real PostgreSQL queue/progress protocol; no generated business payloads."""
from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.data_store.continuation import disposition
from app.data_store.scheduler_tasks import LocalUpdateParameters, TASK_KEY, update_local
from app.scheduling.continuations import enqueue_pending_local_updates
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskDefinition, TaskRegistry
from app.scheduling.repository import SchedulerRepository
from app.scheduling.schemas import RunStatus
from tests.test_data_store_kernel import database
from tests.test_data_store_local_update_admission import install_admission

pytestmark = pytest.mark.skipif(__import__('os').getenv('POSTGRES_TEST_ENABLED') != '1',
                                reason='requires disposable PostgreSQL')


@pytest.fixture
def queue(database):
    engine, _ = database
    install_admission(engine)
    with engine.begin() as connection:
        # Replace the admission-only fixture stubs with the actual production
        # scheduler structures inside this fixture's random, disposable schema.
        connection.exec_driver_sql('DROP TABLE task_runs')
        connection.exec_driver_sql('DROP TABLE scheduled_tasks')
        ScheduledTask.__table__.create(connection)
        TaskRun.__table__.create(connection)
    registry = TaskRegistry()
    registry.register(TaskDefinition(key=TASK_KEY, name='本地数据更新', english_name='Local Data Update',
        parameters_model=LocalUpdateParameters, handler=update_local))
    with Session(engine) as session:
        task = ScheduledTask(name='基金净值待办续作', task_type=TASK_KEY,
            parameters={'datasets':['fund.nav.reported'],'maximum_passes':256,'pass_seconds':180},
            parameter_version=1, schedule={'type':'interval','seconds':600,
                'start_at':datetime.now(UTC).isoformat()}, state='active', priority=100)
        session.add(task); session.commit(); identity=task.id
    return engine, registry, identity


def intent(engine, *, eligible=True, when=None):
    when = when or int((datetime.now(UTC)-timedelta(hours=2)).timestamp())
    status = {'entry_id':'E44','complete':False,'qualified':False,
              'refresh':{'continuation':{'version':1,'state':'progressed_pending',
                  'eligible':eligible,'available_at':when}}}
    with engine.begin() as c:
        c.execute(text('INSERT INTO data_store_entry_status(entry_id,summary_json) VALUES (:id,:body) '
            'ON CONFLICT(entry_id) DO UPDATE SET summary_json=EXCLUDED.summary_json'),
            {'id':'E44','body':json.dumps(status)})
    return when


def test_durable_intent_deduplicates_and_preserves_actual_eligibility_age(queue):
    engine, registry, task = queue
    due = intent(engine)
    with Session(engine) as session:
        created = enqueue_pending_local_updates(session, registry, maximum_queued=10)
        assert len(created) == 1
        run = session.get(TaskRun, created[0])
        assert run.task_id == task and run.status == 'queued'
        assert int(run.available_at.timestamp()) == due
        assert run.parameters['maximum_passes'] == 256
        assert run.available_at < run.created_at and run.result is None
        session.commit()
    with Session(engine) as restarted:
        assert enqueue_pending_local_updates(restarted, registry, maximum_queued=10) == []
        assert restarted.scalar(select(TaskRun.id)) == created[0]


def test_interrupted_worker_recovers_from_entry_intent_without_result(queue):
    engine, registry, _ = queue
    intent(engine)
    with Session(engine) as session:
        original = enqueue_pending_local_updates(session, registry, maximum_queued=10)[0]
        session.commit()
    with Session(engine) as session:
        repo = SchedulerRepository(session)
        assert repo.claim_queued_runs(1) == [original]
        assert repo.finish_run(original, status=RunStatus.INTERRUPTED, error_type='ProcessRestarted')
        session.commit()
    with Session(engine) as restarted:
        following = enqueue_pending_local_updates(restarted, registry, maximum_queued=10)
        assert len(following) == 1 and following[0] != original
        assert restarted.get(TaskRun, original).result is None
        assert enqueue_pending_local_updates(restarted, registry, maximum_queued=10) == []
        restarted.commit()


@pytest.mark.parametrize('state',['paused','archived'])
def test_paused_or_archived_owner_does_not_receive_followups(queue,state):
    engine, registry, identity = queue
    intent(engine)
    with Session(engine) as session:
        session.get(ScheduledTask,identity).state=state; session.commit()
    with Session(engine) as session:
        assert enqueue_pending_local_updates(session,registry,maximum_queued=10)==[]


def test_backoff_is_queued_once_but_not_claimed_early(queue):
    engine,registry,_=queue
    due=int((datetime.now(UTC)+timedelta(minutes=5)).timestamp());intent(engine,when=due)
    with Session(engine) as session:
        assert len(enqueue_pending_local_updates(session,registry,maximum_queued=10))==1
        session.commit()
    with Session(engine) as session:
        assert SchedulerRepository(session).claim_queued_runs(1)==[]
        assert enqueue_pending_local_updates(session,registry,maximum_queued=10)==[]


def test_overlapping_plan_waits_for_an_existing_covering_run(queue):
    engine,registry,identity=queue
    intent(engine)
    with Session(engine) as session:
        original=session.get(ScheduledTask,identity)
        original.parameters=dict(original.parameters,datasets=['fund.nav.reported','market.stock_daily.reported'])
        other=ScheduledTask(name='已在排队的日线更新',task_type=TASK_KEY,
            parameters={'datasets':['market.stock_daily.reported']},parameter_version=1,
            schedule={'type':'interval','seconds':600,'start_at':datetime.now(UTC).isoformat()},state='active')
        session.add(other);session.flush()
        from app.scheduling.schemas import TriggerType
        from app.scheduling.service import SchedulerService
        SchedulerService(session,registry).enqueue_run(other.id,
            trigger_type=TriggerType.SCHEDULED,max_queued_runs=10)
        session.commit()
    with Session(engine) as session:
        assert enqueue_pending_local_updates(session,registry,maximum_queued=10)==[]


def test_blocked_whole_scope_does_not_become_complete_or_autoqueued(queue):
    engine,registry,_=queue
    intent(engine,eligible=False)
    with Session(engine) as session:
        assert enqueue_pending_local_updates(session,registry,maximum_queued=10)==[]
        raw=session.scalar(text("SELECT summary_json FROM data_store_entry_status WHERE entry_id='E44'"))
        assert json.loads(raw)['qualified'] is False


def test_disposition_distinguishes_unprocessed_seal_from_replayed_failure():
    row={'reason':'NATIVE_RANGES_PENDING','complete':False,'qualified':False,
         'input_failures':5,'new_issues':2,'committed_partitions':1}
    work=disposition(row,now=1000,sealed=True)
    assert work['eligible'] and work['state']=='progressed_pending_with_issues'
    assert row['qualified'] is False and row['complete'] is False
    assert not disposition(dict(row,reason='SOURCE_INPUT_UNCHANGED',committed_partitions=0),now=1000)['eligible']


def test_retry_backoff_keeps_true_eligibility_and_fatal_errors_stopped():
    row={'reason':'RETRY_BACKOFF','outcome':'backoff','refresh':{
        'reason':'LOCK_TIMEOUT','next_retry_at':1500}}
    state=disposition(row,now=1000)
    assert state['eligible'] and state['available_at']==1500
    row['refresh']['reason']='COMMIT_UNKNOWN'
    assert disposition(row,now=1000)['eligible'] is False


def test_two_enqueue_transactions_persist_only_one_followup(queue):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    engine,registry,_=queue;intent(engine)
    begin,decided=Barrier(2),Barrier(2)
    def enqueue():
        with Session(engine) as session:
            begin.wait(timeout=10)
            result=enqueue_pending_local_updates(session,registry,maximum_queued=10)
            decided.wait(timeout=10)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:enqueue(),range(2)))
    assert sorted(map(len,outcomes))==[0,1]
    with Session(engine) as session:
        assert len(list(session.scalars(select(TaskRun.id))))==1


def test_global_stop_receipt_prevents_automatic_replay(queue):
    engine,registry,_=queue;intent(engine)
    with Session(engine) as session:
        identity=enqueue_pending_local_updates(session,registry,maximum_queued=10)[0]
        session.commit()
    with Session(engine) as session:
        repository=SchedulerRepository(session)
        assert repository.claim_queued_runs(1)==[identity]
        repository.finish_run(identity,status=RunStatus.FAILED,
            result={'complete':False,'continuation':{'global_stop':True}},error_type='COMMIT_UNKNOWN')
        session.commit()
    with Session(engine) as session:
        assert enqueue_pending_local_updates(session,registry,maximum_queued=10)==[]


def test_bad_continuation_metadata_does_not_block_unrelated_collector(queue,monkeypatch):
    from app.core.config import get_settings
    from app.scheduling import runtime as scheduling_runtime
    from app.scheduling.schemas import TriggerType
    from app.scheduling.service import SchedulerService
    engine,registry,local=queue
    registry.register(TaskDefinition(key='test.queue.protocol',name='队列协议验证',english_name='Queue protocol',
        parameters_model=LocalUpdateParameters,handler=lambda *_:{'message':'协议检查已完成。'}))
    with engine.begin() as c:
        c.execute(text("INSERT INTO data_store_entry_status(entry_id,summary_json) VALUES ('E44','{')"))
    with Session(engine) as session:
        collector=ScheduledTask(name='独立采集协议',task_type='test.queue.protocol',parameters={},
            parameter_version=1,state='active',schedule={'type':'interval','seconds':600,
                'start_at':datetime.now(UTC).isoformat()})
        session.add(collector);session.flush()
        a=SchedulerService(session,registry).enqueue_run(local,
            trigger_type=TriggerType.SCHEDULED,max_queued_runs=10)
        b=SchedulerService(session,registry).enqueue_run(collector.id,
            trigger_type=TriggerType.SCHEDULED,max_queued_runs=10)
        local_run,other_run=a.id,b.id;session.commit()
    monkeypatch.setattr(scheduling_runtime,'get_engine',lambda:engine)
    instance=scheduling_runtime.SchedulerRuntime(
        get_settings().model_copy(update={'scheduler_enabled':False,'scheduler_max_workers':2}),registry)
    try:instance.dispatch_queued_runs()
    finally:instance.executor.shutdown(wait=True)
    with Session(engine) as session:
        assert session.get(TaskRun,local_run).status=='queued'
        assert session.get(TaskRun,other_run).status=='succeeded'
