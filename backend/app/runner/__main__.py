"""Command-line entry point for the isolated backtest Supervisor.

The API application is intentionally not imported here.  A deployment starts
this module as a separate OS process using the same backend image and database
configuration.
"""

from __future__ import annotations

from threading import Event

from app.backtesting.run_repository import DatabaseRunRepository
from app.backtesting.runner_supervisor import RunnerSupervisor
from app.backtesting.supervisor_lock import PostgresAdvisoryLock
from app.core.config import get_settings
from app.db.session import get_engine


def main() -> None:
    settings = get_settings()
    engine = get_engine()
    from sqlalchemy.orm import Session

    stop_event = Event()
    with Session(engine) as session:
        repository = DatabaseRunRepository(
            session,
            formal_limit=settings.backtest_max_queued_runs,
            internal_limit=settings.backtest_internal_max_queued_runs,
        )
        lock = PostgresAdvisoryLock(engine)
        supervisor = RunnerSupervisor(
            repository=repository,
            lock=lock,
            settings=settings,
        )
        try:
            supervisor.run_forever(stop_event=stop_event)
        finally:
            supervisor.stop()


if __name__ == "__main__":  # pragma: no cover - launched by deployment
    main()


__all__ = ["main"]
