"""Optional child-process coordinator; the original backtest entry is unchanged."""
import structlog
import signal
import subprocess
import sys
import time
from threading import Event

logger=structlog.get_logger(__name__)


def run(stop=None, spawn=subprocess.Popen, clock=time.monotonic):
    stop=stop or Event()
    backtest=spawn([sys.executable,'-c','from app.runner.__main__ import backtest_main; backtest_main()'])
    foundation=None
    restart_at=0.0
    backoff=1.0
    try:
        while not stop.is_set():
            # A backtest supervisor exit retains the existing container restart
            # behavior. A foundation exit affects only its own child process.
            if backtest.poll() is not None:
                raise RuntimeError('Backtest supervisor exited')
            if foundation is not None and foundation.poll() is not None:
                foundation=None
                restart_at=clock()+backoff
                backoff=min(backoff*2,30)
                logger.error('foundation_worker_restarting', message='数据底座处理进程已退出，将退避重启；回测进程继续运行。')
            if foundation is None and clock()>=restart_at:
                foundation=spawn([sys.executable,'-m','app.data_foundation.worker'])
            stop.wait(.2)
    finally:
        children=[p for p in (foundation,backtest) if p is not None]
        for child in children:
            if child.poll() is None: child.terminate()
        # One shared deadline fits inside compose's 30-second grace. No task is
        # force-reported as successful when a transaction has to be rolled back.
        deadline=clock()+25
        for child in children:
            try: child.wait(timeout=max(0,deadline-clock()))
            except subprocess.TimeoutExpired:
                child.kill();child.wait()


def main():
    stop=Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    run(stop)
