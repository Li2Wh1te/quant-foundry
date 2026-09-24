"""Opt-in foundation worker, separate from ingestion and backtest queues."""
import argparse
import json
import structlog
import signal
import traceback
from pathlib import Path
from threading import Event, Thread
from uuid import UUID
from sqlalchemy import text, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.db.session import get_engine
from app.data_foundation.work import claim, heartbeat, fenced, finish_batch, HEARTBEAT_SECONDS
from app.data_foundation.bars import normalize_batch
from app.data_foundation.publication import stage_decisions, publish
from app.data_foundation.execution import verify_execution
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.work_models import Work, Release

logger = structlog.get_logger(__name__)
SINGLETON_KEY = 714604920128
SCOPED_SLOT_KEYS = tuple(SINGLETON_KEY + offset for offset in range(1, 7))


def acquire_gate(connection, wait_seconds=0, *, shared=False):
    """Optionally join PostgreSQL's lock queue instead of racing busy workers.

    The unscoped worker retains its exclusive singleton gate. Explicit work
    drivers share that gate, then take one bounded slot and one scope lock.
    A short bounded wait lets periodic updates obtain a turn between units.
    Restore the pooled connection's timeout even on a failed acquisition.
    """
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 10 or type(shared) is not bool:
        raise ValueError('Worker gate wait must be an integer from 0 to 10 seconds')
    try_name = 'pg_try_advisory_lock_shared' if shared else 'pg_try_advisory_lock'
    wait_name = 'pg_advisory_lock_shared' if shared else 'pg_advisory_lock'
    if not wait_seconds:
        return bool(connection.scalar(text(f'SELECT {try_name}(:key)'), {'key': SINGLETON_KEY}))
    previous = connection.scalar(text('SHOW lock_timeout'))
    try:
        connection.execute(text("SELECT set_config('lock_timeout', :value, false)"), {'value': f'{wait_seconds}s'})
        try:
            connection.execute(text(f'SELECT {wait_name}(:key)'), {'key': SINGLETON_KEY})
            return True
        except DBAPIError as exc:
            if getattr(exc.orig, 'sqlstate', None) == '55P03':
                return False
            raise
    finally:
        connection.execute(text("SELECT set_config('lock_timeout', :value, false)"), {'value': previous})


def acquire_scoped_slot(connection):
    """Cap all explicit update and backfill workers at six simultaneous units."""
    for key in SCOPED_SLOT_KEYS:
        if connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}):
            return key
    return None


def scoped_gate_key(scope):
    return int(digest('foundation-worker-scope-gate', scope)[:15], 16)


def run_once(engine, *, preferred_kind='A', work_id=None, runtime_digest='', stop=None, archive_root='/app/data/foundation-runtime-archives', lock_wait_seconds=0):
    """Own the advisory connection for one fenced work unit.

    Unscoped workers retain the old exclusive singleton behavior. Explicit
    drivers may run on independent scopes under six total slots; work in the
    same scope stays serialized even across scheduler and backfill processes.
    Unit operations use independent short sessions and a fenced lease.
    """
    stop = stop or Event()
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as leader:
        scoped = work_id is not None
        if not acquire_gate(leader, lock_wait_seconds, shared=scoped):
            return None
        slot_key = None
        scope_key = None
        scope_locked = False
        try:
            if scoped:
                slot_key = acquire_scoped_slot(leader)
                if slot_key is None:
                    return None
                with Session(engine) as session:
                    target = session.get(Work, work_id)
                    if target is None:
                        return None
                    scope_key = scoped_gate_key(target.scope_key)
                scope_locked = bool(leader.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': scope_key}))
                if not scope_locked:
                    return None
            if stop.is_set(): return None
            with Session(engine) as session, session.begin():
                row = claim(session,preferred_kind,work_id)
                if row is None: return None
                wid,epoch,kind = row.id,row.lease_epoch,row.kind
            finished = Event()
            def pulse():
                while not finished.wait(HEARTBEAT_SECONDS):
                    try:
                        with Session(engine) as session, session.begin():
                            session.execute(text("SET LOCAL lock_timeout = '5s'"))
                            heartbeat(session,wid,epoch)
                    except Exception:
                        # A long unit may briefly hold the same row lock. Lease
                        # expiry is still checked before commit; no unbounded retry.
                        return
            thread = Thread(target=pulse,daemon=True)
            thread.start()
            try:
                with Session(engine) as session:
                    row=session.get(Work,wid)
                    verify_execution(session,row,runtime_digest,archive_root)
                leader.execute(text('SELECT 1'))
                with Session(engine) as session, session.begin():
                    session.execute(text("SET LOCAL statement_timeout = '30s'"))
                    if kind=='A':
                        normalize_batch(session,wid,epoch)
                        release_id=None
                    else:
                        row=session.scalar(select(Release).where(Release.work_id==wid))
                        release= row or stage_decisions(session,wid,epoch)
                        release_id=release.id if release else None
                    leader.execute(text('SELECT 1'))
                if release_id:
                    # Publish in a fresh transaction with the approved lock order;
                    # the potentially expensive decode/staging transaction is over.
                    with Session(engine) as session, session.begin():
                        from app.data_foundation.batches import may_publish
                        row=session.get(Work,wid)
                        if may_publish(session,row):
                            publish(session,release_id,epoch)
                        else:
                            row=fenced(session,wid,epoch)
                            finish_batch(session,row,status='awaiting_publication')
            except FoundationError as exc:
                with Session(engine) as session, session.begin():
                    try:
                        row=fenced(session,wid,epoch)
                    except FoundationError:
                        return kind
                    finish_batch(session,row,status='dependency_missing' if exc.code=='DEPENDENCY_MISSING' else 'failed',error_code=exc.code,error_details={'message':str(exc)})
            except Exception as exc:
                with Session(engine) as session, session.begin():
                    try: row=fenced(session,wid,epoch)
                    except FoundationError: return kind
                    params=json.loads(row.parameters_json)
                    label = '基金持仓报告底座' if params['dataset'] == 'fund.holdings_report' else '日线底座'
                    unit = '份报告' if params['dataset'] == 'fund.holdings_report' else '行'
                    interval = f"{params['start']} 至 {params['end']}"
                    if params['domain'] == 'typed-record-v1':
                        from app.data_foundation.record_schemas import schema_for
                        label, unit = schema_for(params['dataset']).name, '条记录'
                        interval = '固定来源的完整业务范围'
                    failure_message=f"{label} {interval}处理失败1次，累计提交{row.cursor}{unit}，检查点位于{row.cursor}。"
                    finish_batch(session,row,status='failed',error_code=type(exc).__name__,
                        error_details={'frames':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name} for frame in traceback.extract_tb(exc.__traceback__)[-20:]]})
                logger.error('foundation_work_failed', message=failure_message, work_id=str(wid), error_type=type(exc).__name__)
            finally:
                finished.set();thread.join(timeout=6)
            return kind
        finally:
            if scope_locked:
                leader.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': scope_key})
            if slot_key is not None:
                leader.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': slot_key})
            unlock_name = 'pg_advisory_unlock_shared' if scoped else 'pg_advisory_unlock'
            leader.execute(text(f'SELECT {unlock_name}(:key)'), {'key': SINGLETON_KEY})


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',type=UUID,help='Run only this explicitly approved work manifest')
    args=parser.parse_args()
    settings=get_settings()
    if not settings.foundation_worker_enabled and args.once is None:
        return
    stop=Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    preferred='A'
    while not stop.is_set():
        kind=run_once(get_engine(),preferred_kind=preferred,work_id=args.once,
            runtime_digest=settings.foundation_runtime_image_digest,stop=stop)
        if args.once:return
        if kind:preferred='B' if kind=='A' else 'A'
        else:stop.wait(1)


if __name__=='__main__':
    main()
