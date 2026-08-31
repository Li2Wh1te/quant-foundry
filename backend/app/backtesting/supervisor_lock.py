"""Long-lived PostgreSQL advisory lock for the backtest Supervisor.

The lock is session-level rather than transaction-level.  The connection is
kept open for the complete Supervisor lifetime, so a disconnect releases the
lock at PostgreSQL level and a second Supervisor can take over.  The fallback
in this module is only for SQLite/unit-test engines; production PostgreSQL
never relies on an application-process mutex.
"""

from __future__ import annotations

from hashlib import sha256
import threading
from typing import Any

try:  # SQLAlchemy is a runtime dependency, but keep import-time diagnostics clear.
    from sqlalchemy import text
except ImportError:  # pragma: no cover - only minimal static tooling lacks SQLAlchemy
    text = None  # type: ignore[assignment]


ADVISORY_LOCK_NAME = "quant-foundry:backtest-runner-supervisor:v1"


class SupervisorLockNotHeld(RuntimeError):
    """Raised when a Supervisor attempts a protected operation without its lock."""


def assert_supervisor_lock_held(lock: Any) -> None:
    """Require the live advisory-lock capability for a protected write.

    Terminal state writers receive the exact lock object owned by the
    ``RunnerSupervisor``.  Checking both the cheap ``held`` flag and the
    optional connection probe keeps test doubles useful while ensuring a
    dropped PostgreSQL session fails closed before the CAS is attempted.
    """

    if lock is None or not bool(getattr(lock, "held", False)):
        raise SupervisorLockNotHeld(
            "terminal state writes require the Supervisor advisory lock"
        )
    assert_held = getattr(lock, "assert_held", None)
    if callable(assert_held):
        assert_held()


def advisory_lock_key(lock_name: str = ADVISORY_LOCK_NAME) -> int:
    """Derive a stable signed PostgreSQL bigint from the documented lock name."""

    if not isinstance(lock_name, str) or not lock_name.strip():
        raise ValueError("lock_name must be non-blank text")
    value = int.from_bytes(sha256(lock_name.encode("utf-8")).digest()[:8], "big", signed=False)
    # PostgreSQL's bigint parameter is signed.  Keep the full 64-bit entropy
    # while mapping the upper half into its negative representation.
    return value - (1 << 64) if value >= (1 << 63) else value


_LOCAL_LOCKS: dict[int, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock(key: int) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


class PostgresAdvisoryLock:
    """Own a session-level advisory lock and its long-lived connection."""

    def __init__(
        self,
        engine_or_connection: Any,
        *,
        lock_name: str = ADVISORY_LOCK_NAME,
        lock_key: int | None = None,
        allow_test_fallback: bool = True,
    ) -> None:
        self.owner = engine_or_connection
        self.lock_name = lock_name
        self.lock_key = advisory_lock_key(lock_name) if lock_key is None else int(lock_key)
        self.allow_test_fallback = allow_test_fallback
        self._connection: Any | None = None
        self._local: threading.Lock | None = None
        self._held = False
        self.backend: str | None = None

    @property
    def held(self) -> bool:
        return self._held

    @property
    def connection(self) -> Any | None:
        return self._connection

    def _dialect_name(self) -> str | None:
        dialect = getattr(self.owner, "dialect", None)
        if dialect is None:
            bind = getattr(self.owner, "bind", None)
            dialect = getattr(bind, "dialect", None)
        return getattr(dialect, "name", None)

    def acquire(self) -> bool:
        """Try once; never block a second Supervisor on the first instance."""

        if self._held:
            return True
        dialect = self._dialect_name()
        if dialect != "postgresql":
            if not self.allow_test_fallback:
                raise RuntimeError("RunnerSupervisor requires a PostgreSQL advisory lock")
            # Unit tests commonly use SQLite or a fake engine.  A process-local
            # lock keeps those tests deterministic but is never selected for a
            # PostgreSQL deployment.
            local = _local_lock(self.lock_key)
            if not local.acquire(blocking=False):
                return False
            self._local = local
            self._held = True
            self.backend = "local-test"
            return True
        if text is None:  # pragma: no cover
            raise RuntimeError("SQLAlchemy is required for PostgreSQL advisory locks")
        connection = None
        try:
            # Engines expose ``connect``; an injected Connection is already
            # session scoped and must not be closed by another owner.
            if callable(getattr(self.owner, "connect", None)):
                connection = self.owner.connect()
                owns_connection = True
            else:
                connection = self.owner
                owns_connection = False
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": self.lock_key},
                ).scalar()
            )
            if not acquired:
                if owns_connection:
                    connection.close()
                return False
            # End SQLAlchemy's implicit transaction while retaining the
            # session-level lock; advisory locks survive COMMIT.
            commit = getattr(connection, "commit", None)
            if callable(commit):
                commit()
            self._connection = connection
            self._held = True
            self.backend = "postgresql"
            return True
        except Exception:
            if connection is not None and connection is not self.owner:
                try:
                    connection.close()
                except Exception:
                    pass
            raise

    try_acquire = acquire

    def connection_is_alive(self) -> bool:
        """Probe the owning session so a dropped connection stops work.

        PostgreSQL releases a session advisory lock when its connection dies;
        probing the same long-lived session lets the Supervisor fail closed
        before it claims or finalizes another run.  The local test fallback is
        considered alive while its process-local lock remains held.
        """

        if not self._held:
            return False
        if self.backend == "local-test":
            return True
        connection = self._connection
        if connection is None or text is None:
            self._held = False
            return False
        try:
            result = connection.execute(text("SELECT 1"))
            scalar = getattr(result, "scalar", None)
            if callable(scalar):
                scalar()
            return True
        except Exception:
            # Do not keep an in-memory leadership flag after the session has
            # become unusable; PostgreSQL has already released the lock.
            self._held = False
            return False

    def assert_held(self) -> None:
        if not self.connection_is_alive():
            raise SupervisorLockNotHeld("RunnerSupervisor does not hold its advisory lock")

    def release(self) -> None:
        """Unlock and close the owned connection; close also releases on failure."""

        if not self._held:
            return
        try:
            if self.backend == "postgresql" and self._connection is not None and text is not None:
                self._connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self.lock_key},
                )
                commit = getattr(self._connection, "commit", None)
                if callable(commit):
                    commit()
        finally:
            if self._local is not None:
                self._local.release()
                self._local = None
            if self._connection is not None and self._connection is not self.owner:
                try:
                    self._connection.close()
                except Exception:
                    pass
            self._connection = None
            self._held = False
            self.backend = None

    close = release

    def __enter__(self) -> "PostgresAdvisoryLock":
        if not self.acquire():
            raise SupervisorLockNotHeld("another RunnerSupervisor already holds the advisory lock")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


# Stable aliases for callers that describe this as a session lock rather than
# a PostgreSQL-specific adapter.
SessionAdvisoryLock = PostgresAdvisoryLock
LongLivedAdvisoryLock = PostgresAdvisoryLock
SupervisorLock = PostgresAdvisoryLock
AdvisoryLock = PostgresAdvisoryLock
LockNotHeld = SupervisorLockNotHeld


__all__ = [
    "ADVISORY_LOCK_NAME",
    "AdvisoryLock",
    "LongLivedAdvisoryLock",
    "PostgresAdvisoryLock",
    "SessionAdvisoryLock",
    "SupervisorLock",
    "SupervisorLockNotHeld",
    "LockNotHeld",
    "assert_supervisor_lock_held",
    "advisory_lock_key",
]
