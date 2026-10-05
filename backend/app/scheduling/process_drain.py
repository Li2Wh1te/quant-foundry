"""Finite deployment admission fence; no task/configuration state is changed.

Run from an isolated candidate container using existing application credentials.
The scheduler's own sorted source-row locks suspend new enqueues/claims while
accepted handlers retain their ordinary unlocked credential reads and commits.
After `drained`, the operator stops the old backend, checks runner idleness,
stops the runner, then releases this fence before starting replacement services.
There is no forced stop, task pause window, persistent lease or supplier call.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import select
import sys
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.data_sources.service import lock_source_gates


class DrainRefused(RuntimeError):
    pass


def task_fingerprint(session):
    rows = [dict(row) for row in session.execute(text(
        'SELECT id::text,state,version FROM scheduled_tasks ORDER BY id')).mappings()]
    return hashlib.sha256(json.dumps(rows, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest(), len(rows)


@contextmanager
def process_gate(engine, registry, expected_fingerprint):
    """Hold exactly the existing admission locks, always releasing by rollback."""
    if not re.fullmatch(r'[0-9a-f]{64}', expected_fingerprint):
        raise DrainRefused('INVALID_TASK_FINGERPRINT')
    with Session(engine) as session:
        try:
            session.execute(text("SET LOCAL lock_timeout='5s'"))
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            if task_fingerprint(session)[0] != expected_fingerprint:
                raise DrainRefused('TASK_DEFINITIONS_CHANGED')
            gates = lock_source_gates(session, registry)
            if not gates:
                raise DrainRefused('ADMISSION_GATES_MISSING')
            yield session
        finally:
            # No loaded source configuration, original task, queue item or
            # accepted run is modified. Closing even a failed attempt releases
            # PostgreSQL's admission locks without a separate recovery window.
            session.rollback()


def idle_state(session):
    row = session.execute(text("""SELECT
        (SELECT count(*) FROM task_runs WHERE status='running') AS accepted_runs,
        (SELECT count(*) FROM backtest_runs WHERE finished_at IS NULL) AS backtests""")).mappings().one()
    return dict(row)


def emit(stage, message, **fields):
    print(json.dumps({'stage': stage, 'message': message, **fields},
                     ensure_ascii=False), flush=True)


def monotonic_namespace():
    """Expose the real Linux clock domain, never a renewed deployment lease.

    CLOCK_MONOTONIC is comparable between the host and a candidate only when
    both use the same time namespace. The host controller refuses an unknown
    or different namespace instead of granting a new window when stdout is
    delayed. Tests exercise this on Linux; unsupported hosts fail closed.
    """
    return os.readlink('/proc/self/ns/time')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-task-fingerprint', required=True)
    parser.add_argument('--drain-seconds', type=int, default=300)
    parser.add_argument('--hold-seconds', type=int, default=120)
    args = parser.parse_args(argv)
    if not 1 <= args.drain_seconds <= 600 or not 1 <= args.hold_seconds <= 240:
        parser.error('Use finite drain/hold bounds of at most 600/240 seconds')
    from app.db.session import get_engine
    from app.scheduling.registry import task_registry
    try:
        with process_gate(get_engine(), task_registry, args.expect_task_fingerprint) as session:
            deadline = time.monotonic() + args.drain_seconds
            previous = None
            while True:
                state = idle_state(session)
                if state != previous:
                    emit('waiting', '部署准入已暂缓，等待已接受的采集与回测完成；原任务和数据源配置保持原样。', **state)
                    previous = state
                if not any(state.values()):
                    break
                if time.monotonic() >= deadline:
                    raise DrainRefused('ACCEPTED_WORK_NOT_IDLE')
                time.sleep(.25)
            fingerprint, count = task_fingerprint(session)
            if fingerprint != args.expect_task_fingerprint:
                raise DrainRefused('TASK_DEFINITIONS_CHANGED')
            deadline = time.monotonic() + args.hold_seconds
            emit('drained', '已接受运行全部终态，原任务定义未变；部署可停止旧进程，准入锁等待显式释放。',
                 task_count=count, task_fingerprint=fingerprint,
                 release_deadline_monotonic=deadline,
                 monotonic_namespace=monotonic_namespace(), **state)
            while time.monotonic() < deadline:
                if select.select([sys.stdin], [], [], .25)[0]:
                    line = sys.stdin.readline().strip()
                    if time.monotonic() >= deadline:
                        raise DrainRefused('RELEASE_WINDOW_EXPIRED')
                    if line != 'release ' + fingerprint:
                        raise DrainRefused('EXPLICIT_RELEASE_REQUIRED')
                    if any(idle_state(session).values()):
                        raise DrainRefused('IN_FLIGHT_AFTER_DRAIN')
                    if task_fingerprint(session)[0] != fingerprint:
                        raise DrainRefused('TASK_DEFINITIONS_CHANGED')
                    # A read or SQL check may straddle the original deadline.
                    # Neither receiving a line nor completing a query renews
                    # the fence; an expired attempt must never report release.
                    if time.monotonic() >= deadline:
                        raise DrainRefused('RELEASE_WINDOW_EXPIRED')
                    break
            else:
                raise DrainRefused('RELEASE_WINDOW_EXPIRED')
        emit('released', '部署准入锁已释放，原任务、运行记录和数据源配置均未被此入口修改。')
        return 0
    except DrainRefused as error:
        emit('refused', '本次安全切换未完成，准入锁已释放；旧服务必须保持，不能强制停止。', reason=str(error))
        return 2
    except Exception:
        # SQL/provider configuration exceptions can contain secrets or a DSN.
        emit('refused', '本次安全切换检查失败，准入锁已释放；旧服务必须保持。', reason='ADMISSION_GATE_UNAVAILABLE')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
