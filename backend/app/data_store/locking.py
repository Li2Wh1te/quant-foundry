"""Single-host dataset locks, separate long writer planning from short commit.

All processes must use the same trusted, locally mounted lock directory. Lock
files are permanent per dataset, never removed by garbage collection. This is
not a distributed lease or a database/filesystem transaction implementation.
Linux ext4/XFS/Btrfs are the deployment allowlist; overlay, NFS and unknown
mounts fail closed. Tests may inject a filesystem probe, never disable flock.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator

from .errors import DataStoreError

_LOCAL_FILESYSTEMS = frozenset({'ext4', 'xfs', 'btrfs'})
_SCOPE = re.compile(r'[a-z][a-z0-9_.:-]{0,127}\Z')
_FDS: set[int] = set()
_FDS_GUARD = threading.Lock()


def _after_fork_child():
    # Closing (NOT LOCK_UN) our inherited duplicates cannot unlock the parent's
    # open file description. It prevents an idle child from extending its life.
    try:
        for fd in _FDS:
            try:
                os.close(fd)
            except OSError:
                pass
        _FDS.clear()
    finally:
        _FDS_GUARD.release()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_FDS_GUARD.acquire,
                        after_in_parent=_FDS_GUARD.release,
                        after_in_child=_after_fork_child)


def _close(fd: int):
    with _FDS_GUARD:
        _FDS.discard(fd)
        os.close(fd)


def _directory(path: Path) -> int:
    """Open every component relative to its parent; no symlink traversal."""
    if not path.is_absolute() or '..' in path.parts or path == Path('/'):
        raise DataStoreError('UNSAFE_STORAGE_PATH')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for name in path.parts[1:]:
            next_fd = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if os.fstat(fd).st_mode & stat.S_IWOTH:
            raise DataStoreError('UNSAFE_STORAGE_PATH')
        result, fd = fd, -1
        return result
    finally:
        if fd != -1:
            os.close(fd)


def _filesystem_name(fd: int) -> str:
    """Use the fd's actual mount ID, not a guessed path-prefix match."""
    with open(f'/proc/self/fdinfo/{fd}', encoding='ascii') as handle:
        info = handle.read(16_385)
    if len(info) > 16_384:
        raise DataStoreError('UNSUPPORTED_FILESYSTEM')
    mount_ids = [line.split(':', 1)[1].strip() for line in info.splitlines()
                 if line.startswith('mnt_id:')]
    if len(mount_ids) != 1 or not mount_ids[0].isdigit():
        raise DataStoreError('UNSUPPORTED_FILESYSTEM')
    with open('/proc/self/mountinfo', encoding='utf-8') as handle:
        mounts = handle.read(1_048_577)
    if len(mounts) > 1_048_576:
        raise DataStoreError('UNSUPPORTED_FILESYSTEM')
    for line in mounts.splitlines():
        left, separator, right = line.partition(' - ')
        if separator and left.split()[0] == mount_ids[0]:
            return right.split()[0]
    raise DataStoreError('UNSUPPORTED_FILESYSTEM')


def _deadline(timeout_ms: int) -> int:
    if type(timeout_ms) is not int or not 0 <= timeout_ms <= 2**63 - 1:
        raise DataStoreError('INVALID_CONFIGURATION')
    return time.monotonic_ns() + timeout_ms * 1_000_000


def _scope_key(dataset: str) -> str:
    if type(dataset) is not str or not _SCOPE.fullmatch(dataset):
        raise DataStoreError('INVALID_VALUE')
    return hashlib.sha256(dataset.encode('ascii')).hexdigest()


def _acquire(fd: int, operation: int, deadline: int,
             cancelled: Callable[[], bool] | None):
    while True:
        if cancelled is not None and cancelled():
            raise DataStoreError('OPERATION_CANCELLED')
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno == errno.EINTR:
                pass
            elif error.errno not in (errno.EAGAIN, errno.EACCES):
                raise DataStoreError('STORAGE_UNAVAILABLE') from None
        remaining = deadline - time.monotonic_ns()
        if remaining <= 0:
            raise DataStoreError('LOCK_TIMEOUT')
        time.sleep(min(0.01, remaining / 1_000_000_000))


class DatasetLocks:
    """One writer lock and one read/commit lock per registered dataset scope.

    The caller creates the trusted directory explicitly. Construction never
    creates directories or changes existing permissions. Use a context manager
    or close(); instances and writer guards cannot be reused across fork.
    """

    def __init__(self, directory: str | Path):
        if sys.platform != 'linux':
            raise DataStoreError('UNSUPPORTED_FILESYSTEM')
        try:
            self.path = Path(directory)
        except (TypeError, ValueError):
            raise DataStoreError('INVALID_CONFIGURATION') from None
        self._pid = os.getpid()
        self._fd: int | None = None
        try:
            fd = _directory(self.path)
            try:
                if _filesystem_name(fd) not in _LOCAL_FILESYSTEMS:
                    raise DataStoreError('UNSUPPORTED_FILESYSTEM')
                self._identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                with _FDS_GUARD:
                    _FDS.add(fd)
                    self._fd = fd
            except BaseException:
                os.close(fd)
                raise
        except OSError as error:
            code = ('UNSAFE_STORAGE_PATH' if error.errno in (errno.ELOOP, errno.ENOTDIR)
                    else 'STORAGE_UNAVAILABLE')
            raise DataStoreError(code) from None

    def __enter__(self):
        try:
            self._current_directory()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._fd is not None:
            if self._pid == os.getpid():
                _close(self._fd)
            self._fd = None

    def _current_directory(self):
        if self._pid != os.getpid() or self._fd is None:
            raise DataStoreError('LOCK_CONTEXT_EXPIRED')
        try:
            fd = _directory(self.path)
            try:
                found = os.fstat(fd)
                if (found.st_dev, found.st_ino) != self._identity:
                    raise DataStoreError('UNSAFE_STORAGE_PATH')
            finally:
                os.close(fd)
        except OSError:
            raise DataStoreError('UNSAFE_STORAGE_PATH') from None

    @contextmanager
    def _hold(self, dataset: str, role: str, operation: int, deadline: int,
              cancelled: Callable[[], bool] | None) -> Iterator[None]:
        key = _scope_key(dataset)
        self._current_directory()
        pid = os.getpid()
        try:
            with _FDS_GUARD:
                fd = os.open(f'{key}.{role}.lock', os.O_RDWR | os.O_CREAT |
                             os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             0o660, dir_fd=self._fd)
                _FDS.add(fd)
        except OSError:
            raise DataStoreError('UNSAFE_STORAGE_PATH') from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_dev != self._identity[0] or info.st_mode & stat.S_IWOTH):
                raise DataStoreError('UNSAFE_STORAGE_PATH')
            _acquire(fd, operation, deadline, cancelled)
            # A directory or lock replacement during the wait must not create a
            # second lock namespace that can independently authorize a commit.
            self._current_directory()
            try:
                linked = os.stat(f'{key}.{role}.lock', dir_fd=self._fd, follow_symlinks=False)
            except OSError:
                raise DataStoreError('UNSAFE_STORAGE_PATH') from None
            if (linked.st_dev, linked.st_ino, linked.st_nlink) != (info.st_dev, info.st_ino, 1):
                raise DataStoreError('UNSAFE_STORAGE_PATH')
            if cancelled is not None and cancelled():
                raise DataStoreError('OPERATION_CANCELLED')
            yield
        finally:
            if os.getpid() == pid:
                # Last close releases flock even when cancellation/error occurs.
                _close(fd)

    @contextmanager
    def writer(self, dataset: str, *, timeout_ms: int = 5000,
               cancelled: Callable[[], bool] | None = None) -> Iterator[WriterGuard]:
        with self._hold(dataset, 'writer', fcntl.LOCK_EX, _deadline(timeout_ms), cancelled):
            guard = WriterGuard(self, dataset)
            try:
                yield guard
            finally:
                guard._active = False

    @contextmanager
    def read(self, dataset: str, *, timeout_ms: int = 5000,
             cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        with self._hold(dataset, 'commit', fcntl.LOCK_SH, _deadline(timeout_ms), cancelled):
            yield

    @contextmanager
    def read_many(self, datasets: Iterable[str], *, timeout_ms: int = 5000,
                  cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        deadline, keys = _deadline(timeout_ms), set()
        for count, dataset in enumerate(datasets):
            if count >= 64:
                raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
            _scope_key(dataset)
            keys.add(dataset)
        with ExitStack() as stack:
            # All callers use the same order and ONE wait budget for the set.
            for dataset in sorted(keys):
                stack.enter_context(self._hold(dataset, 'commit', fcntl.LOCK_SH,
                                               deadline, cancelled))
            yield


class WriterGuard:
    """An active writer may request the short exclusive commit section."""

    def __init__(self, locks: DatasetLocks, dataset: str):
        self._locks, self._dataset = locks, dataset
        self._pid, self._thread, self._active = os.getpid(), threading.get_ident(), True

    @contextmanager
    def commit(self, *, timeout_ms: int = 5000,
               cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        if (not self._active or self._pid != os.getpid()
                or self._thread != threading.get_ident()):
            raise DataStoreError('LOCK_CONTEXT_EXPIRED')
        with self._locks._hold(self._dataset, 'commit', fcntl.LOCK_EX,
                               _deadline(timeout_ms), cancelled):
            yield
