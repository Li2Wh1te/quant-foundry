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
import time
from typing import Any, Callable
from uuid import UUID


HEARTBEAT_POLICY = "step_plus_time_fallback@1"
DEFAULT_HEARTBEAT_MAX_INTERVAL_SECONDS = 15
DEFAULT_PROGRESS_PERSIST_INTERVAL_SECONDS = 5
DEFAULT_LOST_HEARTBEAT_SECONDS = 60


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

    @property
    def current_date(self) -> str | None:
        return self.current_trading_date

    def as_dict(self) -> dict[str, Any]:
        return {
            "progress": str(self.progress),
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
    # The first form is the recommended stable callback contract.  The
    # fallback keeps adapters concise for repositories whose methods use
    # explicit keyword arguments.
    try:
        return callback(snapshot)
    except TypeError:
        if heartbeat_only:
            return callback(
                current_trading_date=snapshot.current_trading_date,
                current_step=snapshot.current_step,
                heartbeat_at=snapshot.heartbeat_at,
            )
        return callback(
            progress=snapshot.progress,
            current_trading_date=snapshot.current_trading_date,
            current_step=snapshot.current_step,
            heartbeat_at=snapshot.heartbeat_at,
        )


class ProgressReporter:
    """Step-driven reporter with bounded progress and heartbeat writes."""

    def __init__(
        self,
        run_id: UUID | str,
        *,
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
        self._last_heartbeat_at: datetime | None = None
        self._last_snapshot: ProgressSnapshot | None = None

    @property
    def last_heartbeat_at(self) -> datetime | None:
        return self._last_heartbeat_at

    @property
    def last_progress(self) -> Decimal:
        return self._last_progress

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
        if ratio < self._last_progress:
            raise ValueError("run progress must be monotonic")
        normalized_date = _normalize_date(current_trading_date)
        timestamp = self._now(now)
        snapshot = ProgressSnapshot(ratio, current_step, normalized_date, timestamp)
        self._last_snapshot = snapshot
        self._last_step = current_step
        self._last_date = normalized_date

        progress_due = (
            force
            or self._last_progress_persisted_at is None
            or timestamp - self._last_progress_persisted_at >= self.progress_persist_interval
        )
        heartbeat_due = (
            force
            or self._last_heartbeat_at is None
            or timestamp - self._last_heartbeat_at >= self.heartbeat_max_interval
        )
        progress_persisted = False
        heartbeat_persisted = False
        if progress_due:
            _invoke(self.persist_progress, snapshot, heartbeat_only=False)
            progress_persisted = True
            heartbeat_persisted = True
            self._last_progress_persisted_at = timestamp
            self._last_heartbeat_at = timestamp
        elif heartbeat_due:
            _invoke(self.persist_heartbeat, snapshot, heartbeat_only=True)
            heartbeat_persisted = True
            self._last_heartbeat_at = timestamp
        self._last_progress = ratio
        result = ProgressSnapshot(
            ratio,
            current_step,
            normalized_date,
            timestamp,
            progress_persisted,
            heartbeat_persisted,
        )
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
        )
        _invoke(self.persist_heartbeat, snapshot, heartbeat_only=True)
        self._last_heartbeat_at = timestamp
        self._last_snapshot = ProgressSnapshot(
            snapshot.progress,
            snapshot.current_step,
            snapshot.current_trading_date,
            timestamp,
            False,
            True,
        )
        return self._last_snapshot

    def flush(self, *, now: datetime | None = None) -> ProgressSnapshot | None:
        """Flush the current observation at shutdown if one exists."""

        if self._last_snapshot is None:
            return None
        snapshot = self._last_snapshot
        return self.report(
            snapshot.progress,
            current_step=snapshot.current_step,
            current_trading_date=snapshot.current_trading_date,
            now=now,
            force=True,
        )

    def is_lost(self, *, now: datetime | None = None) -> bool:
        if self._last_heartbeat_at is None:
            return False
        timestamp = self._now(now)
        return timestamp - self._last_heartbeat_at >= self.lost_heartbeat_interval


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


# Names used by the scheduler package are kept as aliases for callers that
# already consume the generic progress shape; implementation and settings stay
# in the backtesting namespace and do not share scheduler configuration.
BacktestProgressReporter = ProgressReporter
RunProgressReporter = ProgressReporter
FrozenBacktestTimelineProgress = FrozenTimelineProgress


__all__ = [
    "BacktestProgressReporter",
    "DEFAULT_HEARTBEAT_MAX_INTERVAL_SECONDS",
    "DEFAULT_LOST_HEARTBEAT_SECONDS",
    "DEFAULT_PROGRESS_PERSIST_INTERVAL_SECONDS",
    "FrozenBacktestTimelineProgress",
    "FrozenTimelineProgress",
    "HEARTBEAT_POLICY",
    "ProgressReporter",
    "ProgressSnapshot",
    "RunProgressReporter",
    "is_lost_heartbeat",
    "progress_from_timeline",
]
