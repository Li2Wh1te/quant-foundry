"""Terminal persistence with real CHECKs, transactions and concurrent row locks.

Each test creates its own schema in the explicitly enabled disposable test DB.
No supplier, production process, or previously existing task is accessed.
"""

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from threading import Event
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import BaseModel
import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.config import get_settings
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskDefinition, TaskRegistry
from app.scheduling.repository import SchedulerRepository
from app.scheduling.runtime import SchedulerRuntime
from app.scheduling.schemas import RunStatus, TriggerType


pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL"
)


class EmptyParameters(BaseModel):
    pass


@pytest.fixture
def engine():
    base = create_engine(get_settings().database_url)
    schema = "scheduler_terminal_" + uuid4().hex
    with base.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = base.execution_options(schema_translate_map={None: schema})
    ScheduledTask.__table__.create(isolated)
    TaskRun.__table__.create(isolated)
    try:
        yield isolated
    finally:
        with base.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        base.dispose()


@pytest.fixture
def runtime(engine, monkeypatch):
    monkeypatch.setattr("app.scheduling.runtime.get_engine", lambda: engine)
    monkeypatch.setattr("app.scheduling.supervisor.get_engine", lambda: engine)
    registry = TaskRegistry()
    registry.register(TaskDefinition(
        key="data.ths.terminal_fixture", name="终态测试", english_name="Terminal test",
        parameters_model=EmptyParameters, handler=Mock(return_value={"collected": 3}),
    ))
    settings = get_settings().model_copy(update={"scheduler_enabled": False, "scheduler_max_workers": 2})
    instance = SchedulerRuntime(settings, registry)
    instance.test_log = Mock()
    monkeypatch.setattr("app.scheduling.runtime.logger", instance.test_log)
    try:
        yield instance
    finally:
        instance.stop()
        instance.executor.shutdown(wait=True, cancel_futures=True)


def make_run(engine, *, key="data.ths.terminal_fixture", running=True):
    with Session(engine) as session:
        task = ScheduledTask(
            name="终态测试", task_type=key, parameters={}, parameter_version=1,
            schedule={"type": "interval", "seconds": 60}, state="paused",
        )
        session.add(task)
        session.flush()
        repository = SchedulerRepository(session)
        run = repository.add_run(task, trigger_type=TriggerType.MANUAL, status=RunStatus.QUEUED)
        # A run normally spends time in the queue before it is claimed, as in
        # the production case. Keep creation before the simulated clock step.
        run.created_at -= timedelta(minutes=1)
        session.flush()
        if running:
            assert repository.claim_queued_runs(1, task_types=[key]) == [run.id]
        session.commit()
        return run.id


def read_run(engine, run_id):
    with Session(engine) as session:
        return session.get(TaskRun, run_id)


def execute_to_future(runtime, run_id):
    # Drive the actual worker and done callback deterministically, including
    # an escaping BaseException from a crashed worker on the unfixed version.
    future = Future()
    with runtime._futures_lock:
        runtime._futures[future] = run_id
    future.add_done_callback(runtime._on_run_future_done)
    try:
        outcome = runtime._execute_run(run_id)
    except BaseException as exc:
        future.set_exception(exc)
    else:
        future.set_result(outcome)
    return future


def logged(runtime, event_name):
    return [item for item in runtime.test_log.info.call_args_list if item.args[0] == event_name]


def test_application_clock_reversal_does_not_lose_success(engine, runtime):
    run_id = make_run(engine)
    started = read_run(engine, run_id).started_at
    # Production's ordered log events reverse by ~200 ms. Exercise all three
    # old persistence attempts with a real PostgreSQL constraint, not SQLite.
    with patch("app.scheduling.repository.datetime") as application_clock:
        application_clock.now.return_value = started - timedelta(milliseconds=200)
        future = execute_to_future(runtime, run_id)
    run = read_run(engine, run_id)
    assert run.status == "succeeded"
    assert run.result == {"collected": 3}
    assert run.finished_at >= started
    assert run.completion_marker == "reported"
    assert future not in runtime._futures
    assert runtime.registry.require(run.task_type).handler.call_count == 1
    assert len(logged(runtime, "task_run_succeeded")) == 1


def test_database_clock_reversal_retains_real_outcome_until_catchup(engine, runtime, monkeypatch):
    run_id = make_run(engine)
    started = read_run(engine, run_id).started_at
    real_clock = SchedulerRepository.current_time
    clock_log = Mock()
    monkeypatch.setattr("app.scheduling.repository.logger", clock_log)
    monkeypatch.setattr(SchedulerRepository, "current_time", lambda _: started - timedelta(milliseconds=200))
    future = execute_to_future(runtime, run_id)
    run = read_run(engine, run_id)
    assert future.done() and future in runtime._futures
    assert run.status == "running" and run.finished_at is None
    assert not logged(runtime, "task_run_terminal_written")
    assert not logged(runtime, "task_run_succeeded")
    assert clock_log.warning.call_args.kwargs["observed_at"] == (started - timedelta(milliseconds=200)).isoformat()
    assert clock_log.warning.call_args.kwargs["earlier_at"] == started.isoformat()
    monkeypatch.setattr(SchedulerRepository, "current_time", real_clock)
    runtime.dispatch_queued_runs()
    run = read_run(engine, run_id)
    assert run.status == "succeeded" and run.result == {"collected": 3}
    assert run.started_at == started and run.finished_at >= started
    assert future not in runtime._futures
    assert runtime.registry.require(run.task_type).handler.call_count == 1


@pytest.mark.parametrize("phase", ["update", "commit"])
def test_transient_persistence_error_retries_without_handler_replay(engine, runtime, monkeypatch, phase):
    run_id = make_run(engine)
    remaining = [2]
    finish = SchedulerRepository.finish_run

    def fail_update(repository, *args, **kwargs):
        if remaining[0]:
            remaining[0] -= 1
            raise OperationalError("synthetic terminal update", {}, ConnectionError("DB unavailable"))
        return finish(repository, *args, **kwargs)

    class FailCommit(Session):
        def commit(self):
            if remaining[0]:
                remaining[0] -= 1
                raise OperationalError("synthetic terminal commit", {}, ConnectionError("DB unavailable"))
            super().commit()

    if phase == "update":
        monkeypatch.setattr(SchedulerRepository, "finish_run", fail_update)
    else:
        monkeypatch.setattr("app.scheduling.runtime.Session", FailCommit)
    future = execute_to_future(runtime, run_id)
    runtime.dispatch_queued_runs()
    assert read_run(engine, run_id).status == "running"
    assert future in runtime._futures
    assert not logged(runtime, "task_run_terminal_written")
    runtime.dispatch_queued_runs()
    assert read_run(engine, run_id).status == "succeeded"
    assert read_run(engine, run_id).result == {"collected": 3}
    assert future not in runtime._futures
    assert runtime.registry.require("data.ths.terminal_fixture").handler.call_count == 1
    assert len(logged(runtime, "task_run_terminal_written")) == 1


def test_unknown_commit_result_observes_existing_terminal_without_rewrite(engine, runtime, monkeypatch):
    run_id = make_run(engine)
    fail_once = [True]

    class CommitThenDisconnect(Session):
        def commit(self):
            super().commit()
            if fail_once[0]:
                fail_once[0] = False
                raise OperationalError("synthetic lost commit reply", {}, ConnectionError("reply lost"))

    monkeypatch.setattr("app.scheduling.runtime.Session", CommitThenDisconnect)
    future = execute_to_future(runtime, run_id)
    committed = read_run(engine, run_id)
    assert committed.status == "succeeded" and future in runtime._futures
    runtime.dispatch_queued_runs()
    observed = read_run(engine, run_id)
    assert future not in runtime._futures
    assert observed.finished_at == committed.finished_at
    assert observed.result == committed.result
    assert not logged(runtime, "task_run_terminal_written")
    assert runtime.registry.require("data.ths.terminal_fixture").handler.call_count == 1


def test_old_transaction_uses_realtime_database_clock(engine):
    run_id = make_run(engine, running=False)
    with Session(engine) as old_transaction:
        transaction_started = old_transaction.scalar(select(func.now()))
        with Session(engine) as claimant:
            claimant.execute(select(func.pg_sleep(0.002)))
            assert SchedulerRepository(claimant).claim_queued_runs(1) == [run_id]
            claimant.commit()
        started = read_run(engine, run_id).started_at
        assert transaction_started < started
        assert SchedulerRepository(old_transaction).finish_run(run_id, status=RunStatus.FAILED)
        old_transaction.commit()
    assert read_run(engine, run_id).finished_at >= started


def test_terminal_clock_is_sampled_after_waiting_for_row_lock(engine):
    run_id = make_run(engine)
    lock_query = Event()

    def before_cursor(connection, cursor, statement, parameters, context, many):
        if "FOR UPDATE" in statement and "task_runs.created_at" in statement:
            lock_query.set()

    def finish():
        with Session(engine) as session:
            changed = SchedulerRepository(session).finish_run(run_id, status=RunStatus.FAILED)
            session.commit()
            return changed

    with Session(engine) as holder, ThreadPoolExecutor(max_workers=1) as pool:
        run = holder.scalar(select(TaskRun).where(TaskRun.id == run_id).with_for_update())
        event.listen(engine, "before_cursor_execute", before_cursor)
        try:
            future = pool.submit(finish)
            assert lock_query.wait(3)
            run.started_at = holder.scalar(select(func.clock_timestamp()))
            started = run.started_at
            holder.commit()
            assert future.result(timeout=3)
        finally:
            holder.rollback()
            event.remove(engine, "before_cursor_execute", before_cursor)
    assert read_run(engine, run_id).finished_at >= started


@pytest.mark.parametrize("status", list(RunStatus)[2:])
def test_existing_terminal_wins_over_late_worker_outcome(engine, runtime, status):
    from app.scheduling.runtime import _RunOutcome
    run_id = make_run(engine)
    with Session(engine) as session:
        assert SchedulerRepository(session).finish_run(
            run_id, status=status, result={"audit": "existing"},
            error_type="ExistingDecision", completion_marker="prior",
        )
        session.commit()
    before = read_run(engine, run_id)
    future = runtime._retain_outcome(run_id, _RunOutcome(status=RunStatus.SUCCEEDED, result={"late": True}))
    runtime._on_run_future_done(future)
    after = read_run(engine, run_id)
    assert future not in runtime._futures
    assert (after.status, after.result, after.finished_at, after.error_type, after.completion_marker) == (
        before.status, before.result, before.finished_at, before.error_type, before.completion_marker)
    assert not logged(runtime, "task_run_succeeded")
    assert not logged(runtime, "task_run_terminal_written")


def test_dispatch_never_finalizes_an_active_future_even_with_old_heartbeat(engine, runtime):
    run_id = make_run(engine)
    with Session(engine) as session:
        run = session.get(TaskRun, run_id)
        run.last_heartbeat_at = run.started_at - timedelta(days=10)
        session.commit()
    live = Future()
    runtime._futures[live] = run_id
    runtime.dispatch_queued_runs()
    after = read_run(engine, run_id)
    assert after.status == "running" and after.finished_at is None
    assert runtime._futures[live] == run_id
    assert not logged(runtime, "task_run_terminal_written")
    runtime._futures.clear()


def test_handler_failure_is_failed_with_original_evidence(engine, runtime):
    run_id = make_run(engine)
    handler = runtime.registry.require("data.ths.terminal_fixture").handler
    handler.side_effect = ValueError("synthetic bad collection")
    execute_to_future(runtime, run_id)
    after = read_run(engine, run_id)
    assert after.status == "failed" and after.result is None
    assert after.error_type == "ValueError" and after.error_message == "synthetic bad collection"
    assert after.failure_phase == "handler" and after.completion_marker is None
    assert handler.call_count == 1


def test_cooperative_stop_preserves_checkpoint_result_and_cancelled_status(engine, runtime):
    run_id = make_run(engine)
    with Session(engine) as session:
        assert SchedulerRepository(session).request_cancellation(run_id)
        session.commit()
    result = {"yield_reason": "stopped", "checkpoint": "retained"}
    runtime.registry.require("data.ths.terminal_fixture").handler.return_value = result
    execute_to_future(runtime, run_id)
    after = read_run(engine, run_id)
    assert after.status == "cancelled" and after.result == result
    assert after.cancellation_requested_at is not None and after.finished_at >= after.started_at
    assert after.completion_marker == "cancelled"
    assert not logged(runtime, "task_run_succeeded")


def test_cancelled_future_and_dispatch_failure_remain_retryable(engine, runtime, monkeypatch):
    cancelled_id, rejected_id = make_run(engine), make_run(engine)
    cancelled = Future()
    runtime._futures[cancelled] = cancelled_id
    cancelled.cancel()
    real_finish = SchedulerRepository.finish_run
    monkeypatch.setattr(SchedulerRepository, "finish_run", Mock(side_effect=ConnectionError("DB unavailable")))
    runtime._on_run_future_done(cancelled)
    runtime._finish_dispatch_failure(rejected_id, RuntimeError("Executor stopped"))
    assert len(runtime._futures) == 2
    assert read_run(engine, cancelled_id).status == "running"
    assert read_run(engine, rejected_id).status == "running"
    monkeypatch.setattr(SchedulerRepository, "finish_run", real_finish)
    runtime.dispatch_queued_runs()
    assert not runtime._futures
    assert read_run(engine, cancelled_id).status == "interrupted"
    assert read_run(engine, cancelled_id).error_type == "SchedulerStopped"
    assert read_run(engine, rejected_id).status == "failed"
    assert read_run(engine, rejected_id).failure_phase == "dispatch"


def test_restart_recovery_keeps_future_timestamps_until_clock_catches_up(engine, runtime, monkeypatch):
    run_id, queued_id = make_run(engine), make_run(engine, running=False)
    started = read_run(engine, run_id).started_at
    real_clock = SchedulerRepository.current_time
    monkeypatch.setattr(SchedulerRepository, "current_time", lambda _: started - timedelta(milliseconds=200))
    runtime.settings = runtime.settings.model_copy(update={"scheduler_enabled": True})
    runtime.scheduler = Mock(state=0)
    runtime.start()
    assert read_run(engine, run_id).status == "running"
    assert list(runtime._futures.values()) == [run_id]
    monkeypatch.setattr(SchedulerRepository, "current_time", real_clock)
    runtime.dispatch_queued_runs()
    # Wait for any normally admitted queued work before reading its outcome.
    runtime.executor.shutdown(wait=True)
    after = read_run(engine, run_id)
    assert after.status == "interrupted" and after.error_type == "ProcessRestarted"
    assert after.started_at == started and after.finished_at >= started
    assert read_run(engine, queued_id).status == "succeeded"


def test_second_start_does_not_interrupt_an_active_runtime(engine, runtime):
    run_id = make_run(engine)
    runtime.settings = runtime.settings.model_copy(update={"scheduler_enabled": True})
    runtime.scheduler = Mock(state=1)
    runtime.start()
    assert read_run(engine, run_id).status == "running"
    runtime.scheduler.start.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
def test_ingestion_facts_and_checkpoint_keep_their_transaction_boundary(engine, runtime, monkeypatch, fails):
    monkeypatch.setattr("app.scheduling.runtime.TushareClient", Mock(return_value=object()))
    metadata = MetaData()
    facts = Table("fixture_facts", metadata, Column("id", Integer, primary_key=True))
    checkpoints = Table("fixture_checkpoints", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    handler_calls = []

    def handler(context, parameters):
        handler_calls.append(context.run_id)
        context.session.execute(facts.insert().values(id=1))
        context.session.execute(checkpoints.insert().values(id=1))
        if fails:
            raise ValueError("checkpoint must roll back")
        return {"checkpoint_advanced": True}

    key = "data.sync_trading_status"
    runtime.registry.register(TaskDefinition(key=key, name="事务测试", english_name="Transaction test",
        parameters_model=EmptyParameters, handler=handler))
    run_id = make_run(engine, key=key)
    real_finish = SchedulerRepository.finish_run
    monkeypatch.setattr(SchedulerRepository, "finish_run", Mock(side_effect=ConnectionError("DB unavailable")))
    future = execute_to_future(runtime, run_id)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(facts)) == (0 if fails else 1)
        assert session.scalar(select(func.count()).select_from(checkpoints)) == (0 if fails else 1)
    assert future in runtime._futures
    monkeypatch.setattr(SchedulerRepository, "finish_run", real_finish)
    runtime.dispatch_queued_runs()
    after = read_run(engine, run_id)
    assert after.status == ("failed" if fails else "succeeded")
    assert after.result == (None if fails else {"checkpoint_advanced": True})
    assert handler_calls == [run_id]


def test_real_thread_pool_completion_retries_persistence_only(engine, runtime, monkeypatch):
    run_id = make_run(engine, running=False)
    attempts = []
    real_finish = SchedulerRepository.finish_run

    def fail_once(repository, *args, **kwargs):
        attempts.append(args[0])
        if len(attempts) == 1:
            raise ConnectionError("synthetic first terminal write failure")
        return real_finish(repository, *args, **kwargs)

    monkeypatch.setattr(SchedulerRepository, "finish_run", fail_once)
    runtime.dispatch_queued_runs()
    runtime.executor.shutdown(wait=True)
    assert len(runtime._futures) == 1
    future = next(iter(runtime._futures))
    assert future.done() and read_run(engine, run_id).status == "running"
    runtime.dispatch_queued_runs()
    assert not runtime._futures
    assert read_run(engine, run_id).status == "succeeded"
    assert attempts == [run_id, run_id]
    assert runtime.registry.require("data.ths.terminal_fixture").handler.call_count == 1


def test_callback_and_dispatch_do_not_finalize_same_future_concurrently(engine, runtime, monkeypatch):
    from app.scheduling.runtime import _RunOutcome
    run_id = make_run(engine)
    future = runtime._retain_outcome(run_id, _RunOutcome(status=RunStatus.SUCCEEDED))
    finalizing, release = Event(), Event()
    real_finish = runtime._finish_running_run
    attempts = []

    def paused_finish(*args, **kwargs):
        attempts.append(args[0])
        finalizing.set()
        assert release.wait(3)
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(runtime, "_finish_running_run", paused_finish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        callback = pool.submit(runtime._on_run_future_done, future)
        try:
            assert finalizing.wait(3)
            runtime.dispatch_queued_runs()
            assert attempts == [run_id]
        finally:
            release.set()
        callback.result(timeout=3)
    assert future not in runtime._futures
    assert read_run(engine, run_id).status == "succeeded"


def test_existing_lost_heartbeat_grace_preserves_terminal_and_active_runs(engine, runtime):
    from app.scheduling.supervisor import RunSupervisor
    lost_id, active_id = make_run(engine), make_run(engine)
    with Session(engine) as session:
        now = session.scalar(select(func.clock_timestamp()))
        session.get(TaskRun, lost_id).last_heartbeat_at = now - timedelta(seconds=61)
        session.commit()

    def terminate(run_id):
        with Session(engine) as session:
            assert SchedulerRepository(session).finish_run(
                run_id, status=RunStatus.CANCELLED, result={"checkpoint": "saved"})
            session.commit()

    kill = Mock()
    assert RunSupervisor().terminate_lost_runs(now=now, terminate=terminate, kill=kill,
        sleep=lambda _: None) == (lost_id,)
    kill.assert_not_called()
    assert read_run(engine, lost_id).status == "cancelled"
    assert read_run(engine, lost_id).result == {"checkpoint": "saved"}
    assert read_run(engine, active_id).status == "running"


def test_claim_defers_if_clock_regresses_while_acquiring_locks(engine, monkeypatch):
    run_id = make_run(engine, running=False)
    run = read_run(engine, run_id)
    created = run.created_at
    real_clock = SchedulerRepository.current_time
    readings = iter([run.available_at + timedelta(seconds=1), created - timedelta(milliseconds=1)])
    monkeypatch.setattr(SchedulerRepository, "current_time", lambda _: next(readings))
    with Session(engine) as session:
        assert SchedulerRepository(session).claim_queued_runs(1) == []
        session.commit()
    assert read_run(engine, run_id).status == "queued"
    monkeypatch.setattr(SchedulerRepository, "current_time", real_clock)
    with Session(engine) as session:
        assert SchedulerRepository(session).claim_queued_runs(1) == [run_id]
        session.commit()
    assert read_run(engine, run_id).started_at >= created


def test_clock_guard_keeps_transaction_usable_and_original_times(engine, monkeypatch):
    from app.scheduling.repository import RunClockRegression
    run_id = make_run(engine)
    before = read_run(engine, run_id)
    monkeypatch.setattr(SchedulerRepository, "current_time",
                        lambda _: before.started_at - timedelta(milliseconds=200))
    with Session(engine) as session:
        with pytest.raises(RunClockRegression):
            SchedulerRepository(session).finish_run(run_id, status=RunStatus.SUCCEEDED)
        assert session.scalar(select(1)) == 1
        session.commit()
    after = read_run(engine, run_id)
    assert after.started_at == before.started_at and after.finished_at is None
    assert after.status == "running"


@pytest.mark.parametrize("boundary", ["source_disabled", "archived", "admission_skipped"])
def test_skipped_lifecycle_uses_database_clock_with_application_skew(engine, boundary):
    from app.data_sources.service import skip_source_queue
    run_id = make_run(engine, running=False)
    run = read_run(engine, run_id)
    with Session(engine) as session, patch("app.scheduling.repository.datetime") as repository_clock, \
            patch("app.data_sources.service.datetime") as source_clock:
        repository_clock.now.return_value = source_clock.now.return_value = run.created_at - timedelta(seconds=1)
        if boundary == "source_disabled":
            assert skip_source_queue(session, [run.task_type], "Synthetic disabled source") == 1
        elif boundary == "archived":
            assert SchedulerRepository(session).skip_queued_runs_for_archived_task(run.task_id) == 1
        else:
            skipped = SchedulerRepository(session).add_run(session.get(ScheduledTask, run.task_id),
                trigger_type=TriggerType.MANUAL, status=RunStatus.SKIPPED)
            run_id = skipped.id
        session.commit()
    after = read_run(engine, run_id)
    assert after.status == "skipped" and after.finished_at >= after.created_at


def test_invalid_json_handler_result_fails_instead_of_sticking_in_recovery(engine, runtime):
    run_id = make_run(engine)
    runtime.registry.require("data.ths.terminal_fixture").handler.return_value = {"unsupported": object()}
    future = execute_to_future(runtime, run_id)
    after = read_run(engine, run_id)
    assert after.status == "failed" and after.error_type == "TypeError"
    assert after.result is None and after.failure_phase == "handler"
    assert future not in runtime._futures
    assert not logged(runtime, "task_run_succeeded")


def test_missing_worker_outcome_does_not_fabricate_success(engine, runtime):
    run_id = make_run(engine)
    future = Future()
    runtime._futures[future] = run_id
    future.set_result(None)
    runtime._on_run_future_done(future)
    after = read_run(engine, run_id)
    assert after.status == "indeterminate" and after.error_type == "WorkerResultMissing"
    assert after.completion_marker is None and after.result is None
    assert future not in runtime._futures
    assert not logged(runtime, "task_run_succeeded")


def test_lost_heartbeat_cutoff_ignores_faster_application_clock(engine, runtime):
    from app.scheduling.supervisor import RunSupervisor
    run_id = make_run(engine)
    with patch("app.scheduling.supervisor.datetime") as application_clock:
        application_clock.now.return_value = read_run(engine, run_id).started_at + timedelta(hours=1)
        terminate, kill = Mock(), Mock()
        assert RunSupervisor().terminate_lost_runs(terminate=terminate, kill=kill, sleep=lambda _: None) == ()
    terminate.assert_not_called()
    kill.assert_not_called()
    assert read_run(engine, run_id).status == "running"


def test_progress_throttling_uses_elapsed_time_despite_wall_clock_reversal(engine, runtime, monkeypatch):
    from app.scheduling.supervisor import FrozenTimelineProgress, RunProgressReporter
    run_id = make_run(engine)
    reporter = RunProgressReporter(run_id)
    clock = Mock()
    clock.monotonic.side_effect = [100.0, 106.0]
    monkeypatch.setattr("app.scheduling.supervisor.time", clock)
    with patch("app.scheduling.supervisor.datetime") as wall_clock:
        wall_clock.now.side_effect = [datetime.now(UTC), datetime.now(UTC) - timedelta(minutes=1)]
        reporter.report(FrozenTimelineProgress(total_steps=10, completed_steps=1, current_step="first"))
        reporter.report(FrozenTimelineProgress(total_steps=10, completed_steps=2, current_step="second"))
    after = read_run(engine, run_id)
    assert after.progress == 0.2 and after.current_step == "second"
