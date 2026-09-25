"""Finite shared staging/spill reservations and measured operation guards.

A bounded number of local lock slots is sufficient on one host. Closing a slot
releases admission; crash leftovers are measured and removed only after that
slot is acquired exclusively. Quotas are current coordination, not run history.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import io
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

import duckdb

from .errors import DataStoreError
from .locking import _deadline
from .limits import StoreLimits

_TEMP = re.compile(r'[0-9a-f]{32}\.parquet\Z')


def rss_bytes() -> int:
    with open('/proc/self/statm', encoding='ascii') as f:
        return int(f.read(128).split()[1]) * os.sysconf('SC_PAGE_SIZE')


@dataclass
class Metrics:
    input_rows: int = 0
    input_bytes: int = 0
    current_read_bytes: int = 0
    written_bytes: int = 0
    files_read: int = 0
    files_written: int = 0
    files_retired: int = 0
    catalog_file_changes: int = 0
    scratch_peak_bytes: int = 0
    rss_peak_bytes: int = 0


class Budget:
    def __init__(self, files, locks, limits: StoreLimits):
        self.files, self.locks, self.limits = files, locks, limits
        for i in range(limits.operation_slots):
            with files.directory(f'.scratch/{i:03}', create=True):
                pass
            with files.directory(f'.scratch/{i:03}/spill', create=True):
                pass

    def _hold(self, role, timeout=0):
        return self.locks._hold('store.internal', role, fcntl.LOCK_EX, _deadline(timeout), None)

    def _quota(self, slot: int) -> tuple[str, int]:
        with self.files.directory(f'.scratch/{slot:03}') as fd:
            handle = os.open('quota', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                self.files._regular(handle)
                value = json.loads(os.read(handle, 257))
                if (value['kind'] not in ('write', 'read') or type(value['bytes']) is not int
                        or not 0 < value['bytes'] <= self.limits.scratch_bytes):
                    raise ValueError()
                return value['kind'], value['bytes']
            except (ValueError, KeyError, TypeError):
                raise DataStoreError('SCRATCH_BUDGET_EXCEEDED') from None
            finally:
                os.close(handle)

    def _clean_slot(self, slot: int):
        for suffix in ('/spill', ''):
            relative = f'.scratch/{slot:03}' + suffix
            with self.files.directory(relative) as fd:
                with os.scandir(fd) as entries:
                    for count, e in enumerate(entries):
                        if count > self.limits.changed_files + 512:
                            raise DataStoreError('SCRATCH_BUDGET_EXCEEDED')
                        if not suffix and e.name in ('quota', 'spill'):
                            continue
                        s = e.stat(follow_symlinks=False)
                        if (not stat.S_ISREG(s.st_mode) or s.st_mode & stat.S_IWOTH
                                or (not suffix and not _TEMP.fullmatch(e.name))):
                            raise DataStoreError('UNSAFE_STORAGE_PATH')
                        # Only this acquired slot's scratch files. Never objects/raw.
                        os.unlink(e.name, dir_fd=fd)
                os.fsync(fd)

    def sweep(self) -> int:
        """Recover only inactive scratch slots, including below the free-space floor."""
        cleaned = 0
        with self._hold('admission', self.limits.lock_timeout_ms):
            for i in range(self.limits.operation_slots):
                hold = self._hold(f'scratch-{i}')
                try:
                    hold.__enter__()
                except DataStoreError as error:
                    if error.code != 'LOCK_TIMEOUT':
                        raise
                else:
                    try:
                        self._clean_slot(i)
                        cleaned += 1
                    finally:
                        hold.__exit__(None, None, None)
        return cleaned

    @contextmanager
    def reserve(self, kind: str, *, cancelled=None, metrics: Metrics | None = None):
        if kind not in ('write', 'read'):
            raise DataStoreError('INVALID_CONFIGURATION')
        quota = min(self.limits.scratch_bytes,
                    self.limits.commit_bytes * 3 + self.limits.duckdb_memory_bytes
                    if kind == 'write' else self.limits.duckdb_memory_bytes)
        held = ExitStack()
        slot = None
        try:
            with self._hold('admission', self.limits.lock_timeout_ms):
                used, writers = 0, 0
                for i in range(self.limits.operation_slots):
                    candidate = self._hold(f'scratch-{i}')
                    try:
                        candidate.__enter__()
                    except DataStoreError as error:
                        if error.code != 'LOCK_TIMEOUT':
                            raise
                        mode, size = self._quota(i)
                        used += size
                        writers += mode == 'write'
                    else:
                        try:
                            self._clean_slot(i)
                        except BaseException:
                            candidate.__exit__(None, None, None)
                            raise
                        if slot is None:
                            slot = i
                            held.callback(candidate.__exit__, None, None, None)
                        else:
                            candidate.__exit__(None, None, None)
                if slot is None or used + quota > self.limits.scratch_bytes:
                    raise DataStoreError('SCRATCH_BUDGET_EXCEEDED')
                if kind == 'write' and writers >= self.limits.parallel_writers:
                    raise DataStoreError('LOCK_TIMEOUT')
                available = os.fstatvfs(self.files.fd)
                if available.f_bavail * available.f_frsize - used - quota < self.limits.minimum_free_bytes:
                    raise DataStoreError('DISK_PRESSURE')
                with self.files.directory(f'.scratch/{slot:03}') as fd:
                    handle = os.open('quota', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                                     0o660, dir_fd=fd)
                    try:
                        self.files._regular(handle)
                        os.write(handle, json.dumps({'kind': kind, 'bytes': quota}).encode())
                        os.fsync(handle)
                    finally:
                        os.close(handle)
            space = Reservation(self, slot, quota, kind, cancelled, metrics or Metrics())
            space.check()
            try:
                yield space
            finally:
                # The slot is still locked. Clean even after cancellation.
                try:
                    self._clean_slot(slot)
                except (OSError, DataStoreError):
                    # A failed cleanup must not turn an acknowledged directory
                    # commit into an apparent failed write. Next admission must
                    # recover this unlocked slot before accepting more work.
                    space.cleanup_pending = True
        finally:
            held.close()


class Reservation:
    def __init__(self, budget, slot, quota, kind, cancelled, metrics):
        self.budget, self.slot, self.quota = budget, slot, quota
        self.kind, self.cancelled, self.metrics = kind, cancelled, metrics
        self.path = budget.files.root / f'.scratch/{slot:03}'
        self.spill = self.path / 'spill'
        self.stage_limit = quota // 2 if kind == 'write' else 0
        self.spill_limit = quota - self.stage_limit
        self.promoted_bytes = 0
        self.cleanup_pending = False
        self.deadline = time.monotonic() + (budget.limits.write_timeout_ms if kind == 'write'
                                          else budget.limits.query_timeout_ms) / 1000

    def usage(self) -> tuple[int, int]:
        sizes = []
        for path in (self.path, self.spill):
            size = 0
            with os.scandir(path) as entries:
                for count, e in enumerate(entries):
                    if count > self.budget.limits.changed_files + 512:
                        raise DataStoreError('SCRATCH_BUDGET_EXCEEDED')
                    if e.name in ('quota', 'spill') and path == self.path:
                        continue
                    try:
                        s = e.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # native DuckDB may just have retired its spill file
                    if not stat.S_ISREG(s.st_mode):
                        raise DataStoreError('UNSAFE_STORAGE_PATH')
                    size += s.st_size
            sizes.append(size)
        return sizes[0] + self.promoted_bytes, sizes[1]

    def check(self, *, additional: int = 0):
        if self.cancelled and self.cancelled():
            raise DataStoreError('OPERATION_CANCELLED')
        if time.monotonic() >= self.deadline:
            raise DataStoreError('QUERY_TIMEOUT')
        rss = rss_bytes()
        self.metrics.rss_peak_bytes = max(self.metrics.rss_peak_bytes, rss)
        if rss > self.budget.limits.process_memory_bytes:
            raise DataStoreError('MEMORY_PRESSURE')
        self.budget.files.check()
        usage, spill = self.usage()
        self.metrics.scratch_peak_bytes = max(self.metrics.scratch_peak_bytes, usage + spill + additional)
        if usage + additional > self.stage_limit or spill > self.spill_limit:
            raise DataStoreError('SCRATCH_BUDGET_EXCEEDED')
        fs = os.fstatvfs(self.budget.files.fd)
        if fs.f_bavail * fs.f_frsize - additional < self.budget.limits.minimum_free_bytes:
            raise DataStoreError('DISK_PRESSURE')

    def new_file(self) -> Path:
        self.check()
        return self.path / (uuid4().hex + '.parquet')

    @contextmanager
    def connection(self):
        """Memory-only DuckDB; finite native memory/spill and interrupt watchdog.

        This connection is private to the implementation. No arbitrary SQL or
        path is exposed to callers; all file inputs are pinned current/staged FDs.
        """
        self.check()
        connection = duckdb.connect(':memory:', config={
            'threads': str(self.budget.limits.duckdb_threads),
            'memory_limit': str(self.budget.limits.duckdb_memory_bytes) + 'B',
            'temp_directory': str(self.spill),
            'max_temp_directory_size': str(self.spill_limit) + 'B',
            'autoload_known_extensions': 'false', 'autoinstall_known_extensions': 'false',
            'allow_unsigned_extensions': 'false', 'preserve_insertion_order': 'false',
        })
        stop, errors = threading.Event(), []

        def watch():
            while not stop.wait(0.01):
                try:
                    self.check()
                except DataStoreError as error:
                    errors.append(error)
                    connection.interrupt()
                    return
                except OSError:
                    errors.append(DataStoreError('STORAGE_UNAVAILABLE'))
                    connection.interrupt()
                    return

        monitor = threading.Thread(target=watch, daemon=True)
        monitor.start()
        try:
            yield connection
            if errors:
                raise errors[0]
            self.check()
        except duckdb.Error as error:
            if errors:
                raise errors[0] from None
            if isinstance(error, duckdb.OutOfMemoryException):
                raise DataStoreError('MEMORY_PRESSURE') from None
            raise DataStoreError('FILE_INVALID') from None
        finally:
            stop.set()
            monitor.join()
            connection.close()


class QuotaFile(io.RawIOBase):
    """Arrow output sink checks staging allocation BEFORE each buffered write."""
    def __init__(self, path: Path, space: Reservation):
        super().__init__()
        self.space = space
        self.handle = open(path, 'xb', buffering=0)
        self.path, self.count = path, 0

    def writable(self):
        return True

    def tell(self):
        return self.count

    def write(self, data):
        self.space.check(additional=len(data))
        if self.count + len(data) > self.space.budget.limits.file_bytes:
            raise DataStoreError('BATCH_BUDGET_EXCEEDED')
        result = self.handle.write(data)
        self.count += result
        return result

    def flush(self):
        if not self.handle.closed:
            self.handle.flush()

    def close(self):
        if not self.closed:
            self.flush()
            self.handle.close()
        super().close()
