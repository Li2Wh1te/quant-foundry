"""Single-host admission for explicitly adopted, in-process local updates.

This is not a pause/resume mechanism. Legacy processes and subprocess owners
cannot participate. Adoption requires an externally coordinated, approved drain
of every old owner; a process-tree snapshot cannot establish that precondition.
There is no expiring lease or independent coordinator. A crash leaves a durable
closeout fence, even after the kernel lock disappears.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import threading
from uuid import UUID, uuid4

from .errors import DataStoreError
from .filesystem import LocalFiles
from .locking import DatasetLocks

PROTOCOL = 'cooperative-in-process-v1'
JOURNAL = 'closeout-handoff.json'
ADOPTION = 'closeout-handoff-adoption.json'
MAX_STATE_BYTES = 262_144
MAX_RECOVERIES = 128


def _fail(code):
    raise DataStoreError(code)


def _encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':'), sort_keys=True).encode('ascii')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate key')
        value[key] = item
    return value


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('Invalid identity')
    return value


def _identity(value):
    if (type(value) is not dict or set(value) != {'boot_id', 'pid', 'start_ticks', 'pid_namespace'}
            or type(value['pid']) is not int or value['pid'] <= 0
            or type(value['pid_namespace']) is not int or value['pid_namespace'] <= 0
            or type(value['start_ticks']) is not int or value['start_ticks'] <= 0):
        raise ValueError('Invalid process identity')
    _uuid(value['boot_id'])
    return value


def _boot_id():
    return _uuid(Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def _pid_namespace():
    return os.stat('/proc/self/ns/pid').st_ino


def _start_ticks(pid):
    # comm (field 2) may contain spaces or parentheses. Fields following its
    # final ')' start at field 3; starttime is field 22. A zombie is not treated
    # as resource-free proof: recovery waits for its owner to be reaped.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    start = int(fields[19])
    if start <= 0:
        raise ValueError('Invalid process observation')
    return start


def process_identity():
    try:
        return _identity({'boot_id': _boot_id(), 'pid': os.getpid(),
                          'start_ticks': _start_ticks(os.getpid()),
                          'pid_namespace': _pid_namespace()})
    except (OSError, ValueError, IndexError):
        _fail('HANDOFF_OWNER_UNVERIFIED')


def process_dead(identity):
    """Prove the exact process is absent, without sending any signal.

    Missing /proc access, malformed observations and permission failures remain
    unknown. A reused PID is a different process; no action targets that PID.
    """
    try:
        _identity(identity)
        if _boot_id() != identity['boot_id']:
            return True
        if _pid_namespace() != identity['pid_namespace']:
            _fail('HANDOFF_OWNER_UNVERIFIED')
        try:
            return _start_ticks(identity['pid']) != identity['start_ticks']
        except FileNotFoundError:
            return True
    except (OSError, ValueError, IndexError):
        _fail('HANDOFF_OWNER_UNVERIFIED')


def validate_spec(spec):
    if (type(spec) is not dict or set(spec) != {
            'entries', 'deadline', 'max_attempts', 'pass_seconds', 'max_passes'}
            or type(spec['entries']) is not list or not 1 <= len(spec['entries']) <= 60
            or any(type(e) is not str or not e.isascii() or not e.isalnum()
                   or len(e) > 16 for e in spec['entries'])
            or len(spec['entries']) != len(set(spec['entries']))
            or type(spec['deadline']) not in (int, float)
            or not math.isfinite(spec['deadline'])
            or type(spec['max_attempts']) is not int or not 1 <= spec['max_attempts'] <= 128
            or type(spec['pass_seconds']) is not int or not 1 <= spec['pass_seconds'] <= 840
            or type(spec['max_passes']) is not int or not 1 <= spec['max_passes'] <= 4096):
        raise ValueError('Invalid finite scope')


def _validate_checkpoint(checkpoint, spec):
    from .closeout import validate_state
    validate_state(checkpoint, entries=spec['entries'], deadline=spec['deadline'],
                   max_attempts=spec['max_attempts'], pass_seconds=spec['pass_seconds'])


@contextmanager
def scheduler_admission(root, epoch=None):
    """Opt in explicitly; an adopted root may never silently bypass the gate.

    A store without a journal retains its existing, uncooperative scheduler
    behavior and is ineligible for closeout handoff. A present (even broken)
    journal requires the configured epoch; no fallback on invalid state.
    """
    if epoch is None:
        if any(os.path.lexists(Path(root) / '.locks' / name) for name in (JOURNAL, ADOPTION)):
            _fail('HANDOFF_EPOCH_REQUIRED')
        yield
    else:
        with HandoffGate(root) as gate, gate.scheduler(epoch):
            yield


class HandoffGate:
    """One permanent gate and authoritative journal in an existing store root.

    The same root/epoch must be used by ALL admitted schedulers. Work is performed
    in the caller's process; callbacks must close reservations, transactions and
    store contexts before returning. Forking/launching resource-owning children
    is outside this protocol. Instances and sessions are thread/process bound.
    """

    def __init__(self, root):
        self.files = LocalFiles(root)
        self.locks = None
        self._pid, self._thread = os.getpid(), threading.get_ident()
        try:
            self.locks = DatasetLocks(self.files.root / '.locks')
            fd = os.open('.store-id', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                         os.O_NONBLOCK, dir_fd=self.files.fd)
            try:
                self.files._regular(fd)
                token = _uuid(os.read(fd, 38).decode('ascii'))
            finally:
                os.close(fd)
            self.binding = {'token': token, 'device': self.files.identity[0],
                            'inode': self.files.identity[1]}
        except BaseException:
            self.close()
            raise

    @property
    def state_path(self):
        return self.files.root / '.locks' / JOURNAL

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.locks is not None:
            self.locks.close()
        self.files.close()

    def _check(self):
        if self._pid != os.getpid() or self._thread != threading.get_ident():
            _fail('LOCK_CONTEXT_EXPIRED')
        self.files.check()
        self.locks._current_directory()

    def _private(self, fd):
        info = self.files._regular(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            _fail('UNSAFE_STORAGE_PATH')

    def _read(self):
        self._check()
        with self.files.directory('.locks') as parent:
            adoption = self._adoption(parent)
            try:
                fd = os.open(JOURNAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                             os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                _fail('HANDOFF_STATE_INVALID')
            try:
                self._private(fd)
                data = os.read(fd, MAX_STATE_BYTES + 1)
                if len(data) > MAX_STATE_BYTES:
                    _fail('HANDOFF_STATE_INVALID')
            finally:
                os.close(fd)
        try:
            value = json.loads(data, object_pairs_hook=_unique)
            if type(value) is not dict:
                raise ValueError('Invalid journal')
            digest = value.pop('digest')
            if type(digest) is not str or not hmac.compare_digest(
                    digest, hashlib.sha256(_encode(value)).hexdigest()):
                raise ValueError('Invalid journal checksum')
            self._validate(value)
            if adoption != {'epoch': value['epoch'], 'root': value['root'], 'protocol': PROTOCOL}:
                _fail('HANDOFF_STATE_INVALID')
            return value
        except (ValueError, KeyError, TypeError, UnicodeError):
            _fail('HANDOFF_STATE_INVALID')

    def _adoption(self, parent):
        try:
            fd = os.open(ADOPTION, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                         os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            _fail('HANDOFF_ADOPTION_REQUIRED')
        try:
            self._private(fd)
            data = os.read(fd, 1025)
        finally:
            os.close(fd)
        try:
            value = json.loads(data, object_pairs_hook=_unique)
            if (len(data) > 1024 or type(value) is not dict
                    or set(value) != {'epoch', 'root', 'protocol'}
                    or value['root'] != self.binding or value['protocol'] != PROTOCOL):
                raise ValueError('Invalid adoption')
            _uuid(value['epoch'])
            return value
        except (ValueError, KeyError, TypeError, UnicodeError):
            _fail('HANDOFF_STATE_INVALID')

    def _validate(self, value):
        if (type(value) is not dict or set(value) != {
                'version', 'protocol', 'epoch', 'root', 'phase', 'run'}
                or type(value['version']) is not int or value['version'] != 1
                or value['protocol'] != PROTOCOL or value['root'] != self.binding
                or value['phase'] not in ('scheduler', 'closeout')):
            raise ValueError('Invalid journal')
        _uuid(value['epoch'])
        run = value['run']
        if run is None:
            if value['phase'] != 'scheduler':
                raise ValueError('Missing owner')
            return
        if type(run) is not dict or set(run) != {
                'id', 'spec', 'origin', 'owner', 'recoveries', 'checkpoint', 'terminal'}:
            raise ValueError('Invalid owner')
        _uuid(run['id'])
        validate_spec(run['spec'])
        _identity(run['origin'])
        _identity(run['owner'])
        if (type(run['recoveries']) is not int or not 0 <= run['recoveries'] <= MAX_RECOVERIES
                or type(run['terminal']) is not bool
                or run['terminal'] != (value['phase'] == 'scheduler')):
            raise ValueError('Invalid transition')
        if run['checkpoint'] is not None:
            _validate_checkpoint(run['checkpoint'], run['spec'])
            if run['terminal'] and type(run['checkpoint'].get('stop_reason')) is not str:
                raise ValueError('Missing terminal reason')
        elif run['terminal']:
            raise ValueError('Missing final checkpoint')

    def _write(self, value, *, creating=False):
        self._check()
        self._validate(value)
        data = _encode(dict(value, digest=hashlib.sha256(_encode(value)).hexdigest()))
        if len(data) > MAX_STATE_BYTES:
            _fail('HANDOFF_STATE_INVALID')
        with self.files.directory('.locks') as parent:
            # Check an existing destination before replacing it. Temporary files
            # are exclusive and private, never truncate a shared or linked file.
            try:
                fd = os.open(JOURNAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                             os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                if not creating:
                    _fail('HANDOFF_STATE_INVALID')
            else:
                try:
                    self._private(fd)
                    if creating:
                        _fail('HANDOFF_ALREADY_ADOPTED')
                finally:
                    os.close(fd)
            pending = f'.handoff-{uuid4().hex}.pending'
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check()
                os.replace(pending, JOURNAL, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                # Only this write's exclusive unpublished temporary file.
                try:
                    os.unlink(pending, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def adopt(self, epoch, *, execution_model=PROTOCOL):
        """Initialize ONCE, only after an approved external offline drain.

        This method cannot verify uncooperative owners or stop future legacy
        admission. It is deliberately not exposed as an automatic CLI action.
        Deployers must establish exclusive membership before calling it.
        """
        if execution_model != PROTOCOL:
            _fail('HANDOFF_UNSUPPORTED_OWNER')
        _uuid(epoch)
        with self.locks.writer('store.handoff', timeout_ms=0):
            # This permanent, exclusive marker prevents missing journal state
            # from falling back to an old uncooperative scheduler after adoption.
            # A crash between marker and journal creation remains fail closed.
            with self.files.directory('.locks') as parent:
                if any(os.path.lexists(self.files.root / '.locks' / name)
                       for name in (ADOPTION, JOURNAL)):
                    _fail('HANDOFF_ALREADY_ADOPTED')
                fd = os.open(ADOPTION, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
                with os.fdopen(fd, 'w') as handle:
                    json.dump({'epoch': epoch, 'root': self.binding, 'protocol': PROTOCOL}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.fsync(parent)
            self._write({'version': 1, 'protocol': PROTOCOL, 'epoch': epoch,
                         'root': self.binding, 'phase': 'scheduler', 'run': None},
                        creating=True)

    def _epoch(self, value, epoch):
        if value['epoch'] != epoch:
            _fail('HANDOFF_EPOCH_MISMATCH')

    @contextmanager
    def scheduler(self, epoch, *, execution_model=PROTOCOL):
        """Admit one in-process scheduler call, including its finalization.

        An active scheduler drains naturally. The next admission rechecks the
        journal under the same lock, so starting in a gap cannot overlap it.
        """
        if execution_model != PROTOCOL:
            _fail('HANDOFF_UNSUPPORTED_OWNER')
        self._check()
        with self.locks.writer('store.handoff', timeout_ms=0):
            value = self._read()
            self._epoch(value, epoch)
            if value['phase'] != 'scheduler':
                _fail('HANDOFF_RECOVERY_REQUIRED')
            yield
            self._check()

    def execute(self, epoch, run_id, spec, work, *, resume=False,
                execution_model=PROTOCOL):
        """Run or recover closeout under admission; return only after release.

        A failed callback leaves the fence. Release is recorded only AFTER work
        returns, including all in-process resource context finalizers. Deadline
        expiry never unlocks a still-running owner. A terminal run is immutable:
        resuming returns its original checkpoint and never replays work.
        """
        if execution_model != PROTOCOL:
            _fail('HANDOFF_UNSUPPORTED_OWNER')
        _uuid(run_id)
        validate_spec(spec)
        self._check()
        with self.locks.writer('store.handoff', timeout_ms=0):
            value = self._read()
            self._epoch(value, epoch)
            run = value['run']
            if run is None:
                if resume:
                    _fail('HANDOFF_RUN_MISMATCH')
                owner = process_identity()
                run = {'id': run_id, 'spec': deepcopy(spec), 'origin': owner,
                       'owner': owner, 'recoveries': 0, 'checkpoint': None,
                       'terminal': False}
                value.update(phase='closeout', run=run)
            else:
                if not resume or run['id'] != run_id or run['spec'] != spec:
                    _fail('HANDOFF_RUN_MISMATCH')
                if run['terminal']:
                    return deepcopy(run['checkpoint'])
                if not process_dead(run['owner']):
                    _fail('HANDOFF_OWNER_STILL_LIVE')
                if run['recoveries'] >= MAX_RECOVERIES:
                    _fail('HANDOFF_RECOVERY_LIMIT')
                run.update(owner=process_identity(), recoveries=run['recoveries'] + 1)
            self._write(value)  # Fence is durable before any resource acquisition.
            session = _Session(self, value)
            try:
                from .closeout import terminal_reason
                final_reason = terminal_reason(run['checkpoint'])
                if final_reason is None:
                    result = work(session)
                else:
                    # A crash after a persisted cancel/fatal/expiry/completion
                    # decision cannot re-enter work, even before fence release.
                    result = session.checkpoint
                    result['stop_reason'] = final_reason
                    session.save(result)
                session.check()
                if result != run['checkpoint'] or result is None:
                    _fail('HANDOFF_CHECKPOINT_MISMATCH')
                # No automatic release on exceptions, process loss or disconnect.
                run['terminal'] = True
                value['phase'] = 'scheduler'
                self._write(value)
                return deepcopy(result)
            finally:
                session.active = False


class _Session:
    def __init__(self, gate, value):
        self.gate, self.value, self.active = gate, value, True

    def check(self):
        self.gate._check()
        if not self.active or process_identity() != self.value['run']['owner']:
            _fail('LOCK_CONTEXT_EXPIRED')

    @property
    def checkpoint(self):
        self.check()
        return deepcopy(self.value['run']['checkpoint'])

    def save(self, checkpoint):
        self.check()
        run = self.value['run']
        _validate_checkpoint(checkpoint, run['spec'])
        previous = run['checkpoint']
        if previous is not None:
            for entry, job in checkpoint['jobs'].items():
                old = previous['jobs'][entry]
                if (not old['attempts'] <= job['attempts'] <= old['attempts'] + 1
                        or old['status'] == 'done' and job != old):
                    _fail('HANDOFF_CHECKPOINT_MISMATCH')
        run['checkpoint'] = deepcopy(checkpoint)
        self.gate._write(self.value)
