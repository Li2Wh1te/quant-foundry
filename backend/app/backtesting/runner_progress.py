"""Progress and heartbeat protocol for a backtest worker.

The reporter is transport-agnostic.  A worker supplies short database-write
callbacks, while tests can use lists or counters.  The implementation keeps
progress based on the frozen execution timeline and treats heartbeats as
liveness evidence only; a heartbeat never advances progress or terminal
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import inspect
import logging
from threading import Event, Lock, RLock, Thread, current_thread
from typing import Any, Callable
from uuid import UUID


HEARTBEAT_POLICY = "step_plus_time_fallback@1"
DEFAULT_HEARTBEAT_MAX_INTERVAL_SECONDS = 15
DEFAULT_PROGRESS_PERSIST_INTERVAL_SECONDS = 5
DEFAULT_LOST_HEARTBEAT_SECONDS = 60

logger = logging.getLogger("backtesting.runner.progress")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_date(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        return text or None
    raise ValueError("current trading date must be date or text")


@dataclass(frozen=True, slots=True)
class FrozenTimelineProgress:
    """Progress derived solely from a fixed session/step timeline."""

    total_steps: int
    completed_steps: int
    current_step: str | int | None = None
    current_trading_date: date | datetime | str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.total_steps, bool) or not isinstance(self.total_steps, int) or self.total_steps <= 0:
            raise ValueError("total_steps must be a positive integer")
        if isinstance(self.completed_steps, bool) or not isinstance(self.completed_steps, int):
            raise ValueError("completed_steps must be an integer")
        if not 0 <= self.completed_steps <= self.total_steps:
            raise ValueError("completed_steps must be within the frozen timeline")
        if self.current_step is not None and isinstance(self.current_step, str) and not self.current_step.strip():
            raise ValueError("current_step must not be blank")

    @property
    def ratio(self) -> Decimal:
        """Return an exact 0..1 decimal ratio, never a wall-clock estimate."""

        return Decimal(self.completed_steps) / Decimal(self.total_steps)

    @property
    def current_date(self) -> str | None:
        return _normalize_date(self.current_trading_date)


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """One in-memory progress/heartbeat observation."""

    progress: Decimal
    current_step: str | int | None
    current_trading_date: str | None
    heartbeat_at: datetime
    progress_persisted: bool = False
    heartbeat_persisted: bool = False
    # Launch identity travels with every persistence callback.  Keeping it
    # on the immutable snapshot lets a transport enforce the ``run_id`` /
    # ``launch_id`` predicate without giving the runner a database handle.
    run_id: UUID | str | None = None
    launch_id: UUID | str | None = None
    worker_id: str | None = None

    @property
    def progress_ratio(self) -> Decimal:
        """Canonical name used by run/API projections."""

        return self.progress

    @property
    def current_date(self) -> str | None:
        return self.current_trading_date

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id) if self.run_id is not None else None,
            "launch_id": str(self.launch_id) if self.launch_id is not None else None,
            "worker_id": self.worker_id,
            "progress": str(self.progress),
            "progress_ratio": str(self.progress),
            "current_step": self.current_step,
            "current_trading_date": self.current_trading_date,
            "current_date": self.current_trading_date,
            "last_heartbeat_at": self.heartbeat_at,
            "heartbeat_policy": HEARTBEAT_POLICY,
        }


def progress_from_timeline(total_steps: int, completed_steps: int) -> Decimal:
    """Calculate a bounded exact ratio for a frozen timeline."""

    return FrozenTimelineProgress(total_steps, completed_steps).ratio


def _invoke(callback: Callable[..., Any] | None, snapshot: ProgressSnapshot, *, heartbeat_only: bool) -> Any:
    if callback is None:
        return None
    # The first form is the recommended stable callback contract.  Inspect
    # the signature before invoking it instead of catching ``TypeError``
    # from inside the callback: a database adapter may legitimately raise a
    # TypeError, and retrying it with another shape could duplicate a write.
    try:
        parameters = tuple(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return callback(snapshot)
    positional = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return callback(snapshot)
    if len(positional) == 1 and not any(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
        for parameter in parameters
    ):
        return callback(snapshot)
    payload: dict[str, Any] = {
        "run_id": snapshot.run_id,
        "launch_id": snapshot.launch_id,
        "worker_id": snapshot.worker_id,
        "heartbeat_at": snapshot.heartbeat_at,
        "current_trading_date": snapshot.current_trading_date,
        "current_date": snapshot.current_trading_date,
        "current_step": snapshot.current_step,
        "progress": snapshot.progress,
        "progress_ratio": snapshot.progress,
    }
    if heartbeat_only:
        payload.pop("progress", None)
        payload.pop("progress_ratio", None)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )
    if accepts_kwargs:
        return callback(**payload)
    accepted = {
        parameter.name
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return callback(**{name: value for name, value in payload.items() if name in accepted})


class ProgressReporter:
    """Step-driven reporter with bounded progress and heartbeat writes."""

    def __init__(
        self,
        run_id: UUID | str,
        *,
        launch_id: UUID | str | None = None,
        worker_id: str | None = None,
        persist_progress: Callable[..., Any] | None = None,
        persist_heartbeat: Callable[..., Any] | None = None,
        persist: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        heartbeat_max_interval_seconds: float = DEFAULT_HEARTBEAT_MAX_INTERVAL_SECONDS,
        progress_persist_interval_seconds: float = DEFAULT_PROGRESS_PERSIST_INTERVAL_SECONDS,
        lost_heartbeat_seconds: float = DEFAULT_LOST_HEARTBEAT_SECONDS,
    ) -> None:
        if heartbeat_max_interval_seconds <= 0:
            raise ValueError("heartbeat_max_interval_seconds must be positive")
        if progress_persist_interval_seconds <= 0:
            raise ValueError("progress_persist_interval_seconds must be positive")
        if progress_persist_interval_seconds > heartbeat_max_interval_seconds:
            raise ValueError("progress persistence interval must not exceed heartbeat interval")
        if lost_heartbeat_seconds < 3 * heartbeat_max_interval_seconds:
            raise ValueError("lost heartbeat threshold must be at least three heartbeat intervals")
        self.run_id = run_id
        self.launch_id = launch_id
        self.worker_id = worker_id
        self.persist_progress = persist_progress or persist
        self.persist_heartbeat = persist_heartbeat or persist
        self.clock = clock or _utc_now
        self.heartbeat_max_interval = timedelta(seconds=float(heartbeat_max_interval_seconds))
        self.progress_persist_interval = timedelta(seconds=float(progress_persist_interval_seconds))
        self.lost_heartbeat_interval = timedelta(seconds=float(lost_heartbeat_seconds))
        self._last_progress = Decimal("0")
        self._last_step: str | int | None = None
        self._last_date: str | None = None
        self._last_progress_persisted_at: datetime | None = None
        self._last_successful_persist_at: datetime | None = None
        self._last_persisted_progress: Decimal | None = None
        self._last_heartbeat_at: datetime | None = None
        self._last_snapshot: ProgressSnapshot | None = None
        self._state_lock = RLock()
        self._write_lock = Lock()
        self._stop_event: Event | None = None
        self._heartbeat_thread: Thread | None = None
        self._stop_requested = False
        self._stopped = False

    @property
    def last_heartbeat_at(self) -> datetime | None:
        with self._state_lock:
            return self._last_heartbeat_at

    @property
    def last_progress(self) -> Decimal:
        with self._state_lock:
            return self._last_progress

    @property
    def last_progress_persisted_at(self) -> datetime | None:
        with self._state_lock:
            return self._last_progress_persisted_at

    @property
    def last_progress_persist_at(self) -> datetime | None:
        """Compatibility spelling used by the protocol document."""

        return self.last_progress_persisted_at

    @property
    def last_successful_persist_at(self) -> datetime | None:
        with self._state_lock:
            return self._last_successful_persist_at

    @property
    def dirty_progress(self) -> bool:
        with self._state_lock:
            return self._last_persisted_progress != self._last_progress

    @property
    def latest_current_trading_date(self) -> str | None:
        with self._state_lock:
            return self._last_date

    @property
    def latest_current_step(self) -> str | int | None:
        with self._state_lock:
            return self._last_step

    @property
    def latest_progress_ratio(self) -> Decimal:
        return self.last_progress

    @property
    def latest_in_memory_heartbeat(self) -> datetime | None:
        with self._state_lock:
            return self._last_snapshot.heartbeat_at if self._last_snapshot else None

    @property
    def snapshot(self) -> ProgressSnapshot | None:
        return self._last_snapshot

    def _now(self, now: datetime | None) -> datetime:
        value = now or self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("progress timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def report(
        self,
        progress: FrozenTimelineProgress | Decimal | float | int,
        *,
        current_step: str | int | None = None,
        current_trading_date: date | datetime | str | None = None,
        now: datetime | None = None,
        force: bool = False,
    ) -> ProgressSnapshot:
        """Record a step observation and persist within the protocol bounds."""

        with self._state_lock:
            if self._stop_requested or self._stopped:
                # Late callbacks after marker preparation are deliberately
                # ignored.  Returning a snapshot keeps legacy callers from
                # turning shutdown races into a strategy failure.
                current = self._last_snapshot
                if current is not None:
                    return current
        if isinstance(progress, FrozenTimelineProgress):
            ratio = progress.ratio
            if current_step is None:
                current_step = progress.current_step
            if current_trading_date is None:
                current_trading_date = progress.current_trading_date
        else:
            try:
                ratio = Decimal(str(progress))
            except Exception as exc:
                raise ValueError("progress must be a finite numeric value") from exc
        if ratio.is_nan() or ratio.is_infinite() or not Decimal("0") <= ratio <= Decimal("1"):
            raise ValueError("progress must be between 0 and 1")
        normalized_date = _normalize_date(current_trading_date)
        timestamp = self._now(now)
        with self._state_lock:
            if ratio < self._last_progress:
                raise ValueError("run progress must be monotonic")
            snapshot = ProgressSnapshot(
                ratio,
                current_step,
                normalized_date,
                timestamp,
                run_id=self.run_id,
                launch_id=self.launch_id,
                worker_id=self.worker_id,
            )
            self._last_snapshot = snapshot
            self._last_step = current_step
            self._last_date = normalized_date
            # Memory always tracks the latest valid observation, even when a
            # database callback fails.  The persisted timestamps below only
            # move after a callback reports success.
            self._last_progress = ratio
            last_progress_persisted_at = self._last_progress_persisted_at
            last_heartbeat_at = self._last_heartbeat_at

        progress_due = (
            force
            or last_progress_persisted_at is None
            or timestamp - last_progress_persisted_at >= self.progress_persist_interval
        )
        heartbeat_due = (
            force
            or last_heartbeat_at is None
            or timestamp - last_heartbeat_at >= self.heartbeat_max_interval
        )
        progress_persisted = False
        heartbeat_persisted = False
        if progress_due:
            with self._write_lock:
                with self._state_lock:
                    allowed = not self._stop_requested and not self._stopped
                if allowed:
                    if self.persist_progress is not None:
                        persisted = _invoke(
                            self.persist_progress,
                            snapshot,
                            heartbeat_only=False,
                        )
                        progress_persisted = persisted is not False
                        heartbeat_persisted = progress_persisted
                    elif self.persist_heartbeat is not None:
                        # A heartbeat-only adapter can still keep liveness
                        # current, but it is not counted as a progress write.
                        persisted = _invoke(
                            self.persist_heartbeat,
                            snapshot,
                            heartbeat_only=True,
                        )
                        heartbeat_persisted = persisted is not False
                    if heartbeat_persisted:
                        with self._state_lock:
                            self._last_heartbeat_at = timestamp
                            self._last_successful_persist_at = timestamp
                            if progress_persisted:
                                self._last_progress_persisted_at = timestamp
                                self._last_persisted_progress = ratio
        elif heartbeat_due:
            with self._write_lock:
                with self._state_lock:
                    allowed = not self._stop_requested and not self._stopped
                if allowed:
                    persisted = _invoke(self.persist_heartbeat, snapshot, heartbeat_only=True)
                    heartbeat_persisted = (
                        self.persist_heartbeat is not None and persisted is not False
                    )
                    if heartbeat_persisted:
                        with self._state_lock:
                            self._last_heartbeat_at = timestamp
                            self._last_successful_persist_at = timestamp
        result = ProgressSnapshot(
            ratio,
            current_step,
            normalized_date,
            timestamp,
            progress_persisted,
            heartbeat_persisted,
            self.run_id,
            self.launch_id,
            self.worker_id,
        )
        with self._state_lock:
            self._last_snapshot = result
        return result

    def heartbeat(
        self,
        *,
        current_step: str | int | None = None,
        current_trading_date: date | datetime | str | None = None,
        now: datetime | None = None,
    ) -> ProgressSnapshot:
        """Force a liveness write without changing the progress ratio."""

        timestamp = self._now(now)
        with self._state_lock:
            if self._stop_requested or self._stopped:
                return self._last_snapshot or ProgressSnapshot(
                    self._last_progress,
                    self._last_step,
                    self._last_date,
                    timestamp,
                    run_id=self.run_id,
                    launch_id=self.launch_id,
                    worker_id=self.worker_id,
                )
            if current_step is None:
                current_step = self._last_step
            if current_trading_date is None:
                current_trading_date = self._last_date
        snapshot = ProgressSnapshot(
            self._last_progress,
            current_step,
            _normalize_date(current_trading_date),
            timestamp,
            False,
            False,
            self.run_id,
            self.launch_id,
            self.worker_id,
        )
        with self._write_lock:
            with self._state_lock:
                allowed = not self._stop_requested and not self._stopped
            persisted = (
                allowed
                and self.persist_heartbeat is not None
                and _invoke(self.persist_heartbeat, snapshot, heartbeat_only=True) is not False
            )
        if persisted:
            with self._state_lock:
                self._last_heartbeat_at = timestamp
                self._last_successful_persist_at = timestamp
        result = ProgressSnapshot(
            snapshot.progress,
            snapshot.current_step,
            snapshot.current_trading_date,
            timestamp,
            False,
            persisted,
            self.run_id,
            self.launch_id,
            self.worker_id,
        )
        with self._state_lock:
            self._last_snapshot = result
        return result

    def phase_started(
        self,
        trading_date: date | datetime | str | None,
        current_step: str | int | None,
        completed_steps: int,
        total_steps: int,
        *,
        now: datetime | None = None,
    ) -> ProgressSnapshot:
        """Accept a phase-boundary event from ``DeterministicBacktestRunner``."""

        return self.report(
            FrozenTimelineProgress(
                total_steps,
                completed_steps,
                current_step,
                trading_date,
            ),
            now=now,
        )

    def step_completed(
        self,
        trading_date: date | datetime | str | None,
        current_step: str | int | None,
        completed_steps: int,
        total_steps: int,
        *,
        now: datetime | None = None,
    ) -> ProgressSnapshot:
        """Accept a successful formal-step boundary event."""

        return self.report(
            FrozenTimelineProgress(
                total_steps,
                completed_steps,
                current_step,
                trading_date,
            ),
            now=now,
        )

    def flush(self, *, now: datetime | None = None) -> ProgressSnapshot | None:
        """Flush the current observation at shutdown if one exists."""

        with self._state_lock:
            snapshot = self._last_snapshot
        if snapshot is None:
            return None
        return self.report(
            snapshot.progress,
            current_step=snapshot.current_step,
            current_trading_date=snapshot.current_trading_date,
            now=now,
            force=True,
        )

    def tick(self, *, now: datetime | None = None) -> ProgressSnapshot | None:
        """Run one deterministic heartbeat fallback check.

        Tests and embedding workers can call this directly; ``start`` uses
        the same method from a bounded background thread.
        """

        timestamp = self._now(now)
        with self._state_lock:
            snapshot = self._last_snapshot
            last_heartbeat_at = self._last_heartbeat_at
            if snapshot is None or self._stop_requested or self._stopped:
                return None
            due = (
                last_heartbeat_at is None
                or timestamp - last_heartbeat_at >= self.heartbeat_max_interval
            )
        if not due:
            return snapshot
        return self.heartbeat(
            current_step=snapshot.current_step,
            current_trading_date=snapshot.current_trading_date,
            now=timestamp,
        )

    def start(self) -> "ProgressReporter":
        """Start the bounded heartbeat fallback worker once."""

        with self._state_lock:
            if self._stopped:
                raise RuntimeError("progress reporter cannot be restarted after stop")
            if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
                return self
            self._stop_requested = False
            self._stop_event = Event()
            self._heartbeat_thread = Thread(
                target=self._heartbeat_loop,
                name=f"backtest-heartbeat-{self.run_id}",
                daemon=True,
            )
            thread = self._heartbeat_thread
        thread.start()
        return self

    def _heartbeat_loop(self) -> None:
        event = self._stop_event
        if event is None:
            return
        # A short polling bound avoids exceeding the configured 15-second
        # maximum when a scheduler wake-up is delayed by normal OS jitter.
        wait_seconds = max(0.05, min(self.heartbeat_max_interval.total_seconds() / 4, 1.0))
        while not event.wait(wait_seconds):
            try:
                self.tick()
            except Exception:
                # A failed heartbeat write is liveness evidence of its own,
                # not a strategy/business failure.  Keep retrying until stop.
                logger.warning(
                    "回测 worker 心跳写入失败，将继续等待下一次兜底写入。",
                    exc_info=True,
                    extra={"event": "backtest_runner_heartbeat_persist_failed", "run_id": str(self.run_id)},
                )

    def stop(self, *, flush: bool = True, timeout_seconds: float | None = None) -> ProgressSnapshot | None:
        """Stop the fallback worker and optionally flush one final observation."""

        with self._state_lock:
            if self._stopped:
                return self._last_snapshot
            self._stop_requested = True
            event = self._stop_event
            thread = self._heartbeat_thread
        if event is not None:
            event.set()
        if thread is not None and thread is not current_thread():
            thread.join(timeout_seconds)
        result: ProgressSnapshot | None = None
        try:
            if flush:
                # ``report`` sees ``_stop_requested`` and would skip the
                # write; temporarily clear it while the serialized write
                # lock is held.
                with self._state_lock:
                    self._stop_requested = False
                result = self.flush()
            return result
        finally:
            # Even a failed final write closes this launch's reporter.  A
            # caller may retry the whole run, but must not issue duplicate
            # marker-adjacent heartbeat writes from a half-stopped thread.
            with self._state_lock:
                self._stopped = True
                self._stop_requested = True
                self._stop_event = None
                self._heartbeat_thread = None

    close = stop

    def __enter__(self) -> "ProgressReporter":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.stop()

    def is_lost(self, *, now: datetime | None = None) -> bool:
        with self._state_lock:
            last_heartbeat_at = self._last_heartbeat_at
        if last_heartbeat_at is None:
            return False
        timestamp = self._now(now)
        return timestamp - last_heartbeat_at >= self.lost_heartbeat_interval


def is_lost_heartbeat(
    last_heartbeat_at: datetime | None,
    *,
    now: datetime | None = None,
    started_at: datetime | None = None,
    lost_heartbeat_seconds: float = DEFAULT_LOST_HEARTBEAT_SECONDS,
) -> bool:
    """Return whether the persisted heartbeat has exceeded the loss bound."""

    # ``started_at`` closes the first-heartbeat gap: a worker that never
    # writes a heartbeat must still become eligible for lost-run handling.
    if last_heartbeat_at is None:
        last_heartbeat_at = started_at
    if last_heartbeat_at is None:
        return False
    if last_heartbeat_at.tzinfo is None or last_heartbeat_at.utcoffset() is None:
        raise ValueError("last heartbeat must be timezone-aware")
    current = now or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if lost_heartbeat_seconds <= 0:
        raise ValueError("lost heartbeat threshold must be positive")
    return current.astimezone(UTC) - last_heartbeat_at.astimezone(UTC) >= timedelta(seconds=lost_heartbeat_seconds)


class DatabaseProgressPersistence:
    """Persist runner snapshots through a fresh, short-lived DB session.

    The runtime only emits immutable snapshots.  This adapter is the narrow
    worker-side bridge that opens an independent session for each write,
    checks the current launch identity, and acknowledges success only after
    the transaction commits.  A stale launch therefore cannot refresh a new
    worker's liveness record.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        repository_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        self.session_factory = session_factory
        self.repository_factory = repository_factory

    def _persist(self, snapshot: ProgressSnapshot) -> bool:
        if snapshot.run_id is None or snapshot.launch_id is None:
            # Launch identity is mandatory for production persistence.  A
            # reporter without it may still be used with an in-memory test
            # callback, but must not claim a durable heartbeat.
            return False
        session = self.session_factory()
        close = getattr(session, "close", None)
        try:
            repository = (
                self.repository_factory(session)
                if self.repository_factory is not None
                else self._default_repository(session)
            )
            raw_date = snapshot.current_trading_date
            current_date = date.fromisoformat(raw_date) if raw_date else None
            changed = repository.record_progress(
                UUID(str(snapshot.run_id)),
                snapshot.progress,
                current_trading_date=current_date,
                current_step=(
                    str(snapshot.current_step)
                    if snapshot.current_step is not None
                    else None
                ),
                heartbeat_at=snapshot.heartbeat_at,
                persisted_at=snapshot.heartbeat_at,
                launch_id=UUID(str(snapshot.launch_id)),
            )
            if changed is None or changed is False:
                # No row or a launch/status predicate miss is not a valid
                # heartbeat, even when the session itself commits cleanly.
                session.rollback()
                return False
            committed = session.commit()
            if committed is False:
                session.rollback()
                return False
            return True
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        finally:
            if callable(close):
                close()

    @staticmethod
    def _default_repository(session: Any) -> Any:
        from .run_repository import DatabaseRunRepository

        return DatabaseRunRepository(session)

    def persist_progress(self, snapshot: ProgressSnapshot) -> bool:
        return self._persist(snapshot)

    def persist_heartbeat(self, snapshot: ProgressSnapshot) -> bool:
        return self._persist(snapshot)


# Names used by the scheduler package are kept as aliases for callers that
# already consume the generic progress shape; implementation and settings stay
# in the backtesting namespace and do not share scheduler configuration.
BacktestProgressReporter = ProgressReporter
RunProgressReporter = ProgressReporter
RunnerProgressReporter = ProgressReporter
BacktestProgressPersistence = DatabaseProgressPersistence
FrozenBacktestTimelineProgress = FrozenTimelineProgress


__all__ = [
    "BacktestProgressReporter",
    "BacktestProgressPersistence",
    "DEFAULT_HEARTBEAT_MAX_INTERVAL_SECONDS",
    "DEFAULT_LOST_HEARTBEAT_SECONDS",
    "DEFAULT_PROGRESS_PERSIST_INTERVAL_SECONDS",
    "DatabaseProgressPersistence",
    "FrozenBacktestTimelineProgress",
    "FrozenTimelineProgress",
    "HEARTBEAT_POLICY",
    "ProgressReporter",
    "ProgressSnapshot",
    "RunnerProgressReporter",
    "RunProgressReporter",
    "is_lost_heartbeat",
    "progress_from_timeline",
]
