"""Trusted single-host filesystem, unique files and explicit durability points."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from . import locking
from .errors import DataStoreError

_HEX = re.compile(r'[0-9a-f]{64}\Z')
_FILE = re.compile(r'[0-9a-f]{32}\.parquet\Z')


class LocalFiles:
    """Call initialize() explicitly on a pre-created, trusted empty/new root.

    All lock/scratch/object/log directories share the root's local mount. Paths
    in the catalog are private implementation details, not public API inputs.
    Readers pin opened descriptors; all unlinks are relative to checked parents.
    No directory glob supplies the set of queryable business files.
    """
    def __init__(self, root: str | Path):
        self.root = Path(root)
        try:
            self.fd = locking._directory(self.root)
            s = os.fstat(self.fd)
            self.identity = (s.st_dev, s.st_ino)
            if locking._filesystem_name(self.fd) not in locking._LOCAL_FILESYSTEMS:
                raise DataStoreError('UNSUPPORTED_FILESYSTEM')
        except BaseException:
            if hasattr(self, 'fd'):
                os.close(self.fd)
            raise
        self.pid = os.getpid()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def check(self):
        if self.fd is None or self.pid != os.getpid():
            raise DataStoreError('LOCK_CONTEXT_EXPIRED')
        fd = locking._directory(self.root)
        try:
            s = os.fstat(fd)
            if (s.st_dev, s.st_ino) != self.identity:
                raise DataStoreError('UNSAFE_STORAGE_PATH')
        finally:
            os.close(fd)

    def initialize(self):
        self.check()
        for name in ('.locks', '.scratch', 'objects', '.logs'):
            with self.directory(name, create=True):
                pass
        os.fsync(self.fd)

    @contextmanager
    def directory(self, relative: str, *, create: bool = False):
        self.check()
        parts = relative.split('/')
        if any(p in ('', '.', '..') or not re.fullmatch(r'[a-zA-Z0-9_.-]+', p) for p in parts):
            raise DataStoreError('UNSAFE_STORAGE_PATH')
        fd = os.dup(self.fd)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o770, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  dir_fd=fd)
                s = os.fstat(next_fd)
                if s.st_dev != self.identity[0] or s.st_mode & stat.S_IWOTH:
                    os.close(next_fd)
                    raise DataStoreError('UNSAFE_STORAGE_PATH')
                os.close(fd)
                fd = next_fd
            yield fd
        except OSError as error:
            code = 'UNSAFE_STORAGE_PATH' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'STORAGE_UNAVAILABLE'
            raise DataStoreError(code) from None
        finally:
            os.close(fd)

    def root_token(self, locks) -> UUID:
        with locks.writer('store.root'):
            try:
                fd = os.open('.store-id', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o660, dir_fd=self.fd)
            except FileExistsError:
                fd = os.open('.store-id', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
                try:
                    self._regular(fd)
                    return UUID(os.read(fd, 37).decode('ascii'))
                except (ValueError, UnicodeError):
                    raise DataStoreError('CATALOG_MISMATCH') from None
                finally:
                    os.close(fd)
            else:
                try:
                    token = uuid4()
                    os.write(fd, str(token).encode('ascii'))
                    os.fsync(fd)
                    os.fsync(self.fd)
                    return token
                finally:
                    os.close(fd)

    def _regular(self, fd: int):
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_dev != self.identity[0]
                or s.st_nlink != 1 or s.st_mode & stat.S_IWOTH):
            raise DataStoreError('UNSAFE_STORAGE_PATH')
        return s

    def object_path(self, dataset: str) -> str:
        directory = 'objects/' + locking._scope_key(dataset)
        with self.directory(directory, create=True):
            pass
        return directory + '/' + uuid4().hex + '.parquet'

    def object_parts(self, path: str) -> tuple[str, str]:
        parts = path.split('/')
        if len(parts) != 3 or parts[0] != 'objects' or not _HEX.fullmatch(parts[1]) or not _FILE.fullmatch(parts[2]):
            raise DataStoreError('UNSAFE_STORAGE_PATH')
        return '/'.join(parts[:2]), parts[2]

    @contextmanager
    def open_object(self, path: str):
        directory, name = self.object_parts(path)
        with self.directory(directory) as parent:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        try:
            self._regular(fd)
            yield fd
        finally:
            os.close(fd)

    def promote(self, scratch: Path, final: str, *, fault):
        """Scratch is closed+fsynced; link is exclusive (unlike overwriting rename).

        A brief two-link window is private to the writer. An interrupted move
        remains tracked as prepared garbage. Directory fsync precedes PG commit.
        """
        directory, name = self.object_parts(final)
        with self.directory(directory) as parent:
            os.link(scratch, name, dst_dir_fd=parent, follow_symlinks=False)
            fault('after_link')
            scratch.unlink()
            with self.directory(str(scratch.parent.relative_to(self.root))) as origin:
                os.fsync(origin)
            os.fsync(parent)
            fault('after_directory_fsync')
        self.check()

    def delete_object(self, path: str):
        directory, name = self.object_parts(path)
        with self.directory(directory) as parent:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                os.fsync(parent)
                return
            try:
                s = self._regular(fd)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (s.st_dev, s.st_ino) != (current.st_dev, current.st_ino):
                    raise DataStoreError('UNSAFE_STORAGE_PATH')
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(fd)


def digest_file(path: Path, check=lambda: None) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            check()
            digest.update(chunk)
    return digest.hexdigest()
