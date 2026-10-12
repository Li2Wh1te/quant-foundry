"""One expiring, signed local runner check; no deployment/market history.

The web process reads this current value through the existing result root. It
never receives a Docker socket. Only the trusted runner checks the daemon and
renews it; workers cannot mount the result root or obtain the signing key.
"""
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

from .runtime_contracts import RuntimeFault

NAME = '.qf-s3-runtime-ready.json'
TTL_NS = 30_000_000_000
REFRESH_NS = 5_000_000_000


def _fault():
    return RuntimeFault('CAPABILITY_UNAVAILABLE', '可信runner当前隔离检查缺失、过期或与worker镜像不一致')


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _boot():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _directory(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & stat.S_IWOTH:
        os.close(fd)
        raise _fault()
    return fd


def _read(directory):
    fd = os.open(NAME, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not 1 <= info.st_size <= 4096):
            raise _fault()
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise _fault()
        return json.loads(raw)
    finally:
        os.close(fd)


def _verify(value, key):
    if (type(value) is not dict or set(value) != {'payload', 'signature'}
            or type(value['payload']) is not dict or type(value['signature']) is not str
            or len(key) < 32):
        raise _fault()
    payload = value['payload']
    if set(payload) != {'kind', 'boot', 'monotonic_ns', 'worker_image', 'publisher'}:
        raise _fault()
    if (payload['kind'] != 'qf.s3.runtime_ready.v1'
            or any(type(v) is not str for v in payload.values())
            or not re.fullmatch(r'[0-9a-f]{32}', payload['publisher'])
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', payload['worker_image'])
            or not re.fullmatch(r'[0-9]{1,20}', payload['monotonic_ns'])
            or not hmac.compare_digest(value['signature'], hmac.new(key, _encoded(payload), hashlib.sha256).hexdigest())):
        raise _fault()
    return payload


def check_runtime_ready(root, key, image):
    directory = None
    try:
        directory = _directory(root)
        payload = _verify(_read(directory), key)
        age = time.monotonic_ns() - int(payload['monotonic_ns'])
        if payload['boot'] != _boot() or payload['worker_image'] != image or not 0 <= age <= TTL_NS:
            raise _fault()
    except (OSError, ValueError, TypeError):
        raise _fault() from None
    finally:
        if directory is not None:
            os.close(directory)


class RuntimeReadiness:
    def __init__(self, root, key, process):
        self.root, self.key, self.process = Path(root), key, process
        self.publisher, self.last = uuid4().hex, 0

    def refresh(self):
        now = time.monotonic_ns()
        if self.last and now - self.last < REFRESH_NS:
            return
        # Actual read-only checks happen only in the trusted runner. Runtime
        # binding and WorkerProcess still refuse unavailable isolation.
        self.process.root.check()
        self.process.daemon.preflight(self.process.image)
        if len(self.key) < 32:
            raise _fault()
        directory = _directory(self.root)
        temporary = NAME + '.' + self.publisher + '.' + uuid4().hex
        try:
            fcntl.flock(directory, fcntl.LOCK_EX)
            try:
                # Reserved name alone never authorizes replacing an unknown
                # file/link. Only a valid existing current check is replaceable.
                _verify(_read(directory), self.key)
            except FileNotFoundError:
                pass
            payload = dict(kind='qf.s3.runtime_ready.v1', boot=_boot(),
                monotonic_ns=str(time.monotonic_ns()), worker_image=self.process.image, publisher=self.publisher)
            value = _encoded(dict(payload=payload,
                signature=hmac.new(self.key, _encoded(payload), hashlib.sha256).hexdigest()))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                pending = memoryview(value)
                while pending:
                    written = os.write(fd, pending)
                    if not written:
                        raise OSError('incomplete current runtime check')
                    pending = pending[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, NAME, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            self.last = now
        except (OSError, ValueError):
            raise _fault() from None
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def close(self):
        directory = None
        try:
            directory = _directory(self.root)
            fcntl.flock(directory, fcntl.LOCK_EX)
            payload = _verify(_read(directory), self.key)
            if payload['publisher'] == self.publisher:
                os.unlink(NAME, dir_fd=directory)
                os.fsync(directory)
        except FileNotFoundError:
            pass
        finally:
            if directory is not None:
                os.close(directory)
