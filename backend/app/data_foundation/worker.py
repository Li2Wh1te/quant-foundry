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
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.db.session import get_engine
from app.data_foundation.work import claim, heartbeat, fenced, finish_batch, HEARTBEAT_SECONDS
from app.data_foundation.bars import normalize_batch
from app.data_foundation.publication import stage_decisions, publish
from app.data_foundation.execution import verify_execution
from app.data_foundation.canonical import FoundationError
from app.data_foundation.work_models import Work, Release

logger = structlog.get_logger(__name__)
SINGLETON_KEY = 714604920128


def run_once(engine, *, preferred_kind='A', work_id=None, runtime_digest='', stop=None, archive_root='/app/data/foundation-runtime-archives'):
    """A single process owns the singleton advisory connection for this call.

    Unit operations have independent short sessions and a fenced lease. The
    heartbeat is also independent, with a bounded lock timeout, so loss of a
    worker never creates an immortal running job.
    """
    stop = stop or Event()
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as leader:
        if not leader.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key':SINGLETON_KEY}):
            return None
        try:
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
                        publish(session,release_id,epoch)
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
                    failure_message=f"日线底座 {params['start']} 至 {params['end']} 处理失败1次，累计提交{row.cursor}行，检查点位于{row.cursor}。"
                    finish_batch(session,row,status='failed',error_code=type(exc).__name__,
                        error_details={'frames':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name} for frame in traceback.extract_tb(exc.__traceback__)[-20:]]})
                logger.error('foundation_work_failed', message=failure_message, work_id=str(wid), error_type=type(exc).__name__)
            finally:
                finished.set();thread.join(timeout=6)
            return kind
        finally:
            leader.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':SINGLETON_KEY})


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
