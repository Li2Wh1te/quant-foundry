"""Existing deployment entry for the S3 trusted supervisor.

D13 registers the accepted repository/gateway/result services in runtime_services.
No old tasks/checkpoints or old engine are automatically executed by this entry.
"""

import signal
from threading import Event

from app.backtest_service.supervisor import RunnerSupervisor


def build_supervisor(repository, process, services):
    return RunnerSupervisor(repository, process, services)


def backtest_main():
    from app.backtest_service.runtime_services import create_services

    repository, process, services = create_services()
    supervisor = build_supervisor(repository, process, services)
    stopped = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    supervisor.run_forever(stop_event=stopped)


def main():
    backtest_main()


if __name__ == "__main__":
    main()

__all__ = ["main", "build_supervisor"]
