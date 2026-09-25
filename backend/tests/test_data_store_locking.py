"""Real flock/process tests; no database or production filesystem is touched.

The isolated test directory may be overlayfs. For lock behavior ONLY, inject an
ext4 probe result; flock itself is never mocked. Separate tests assert that the
real default rejects overlay/network/unknown filesystems. This does not claim
fsync durability or cross-container mounts have been validated.
"""
from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import threading
import sys
import time
from unittest.mock import patch

import pytest

from app.data_store import locking
from app.data_store.errors import DataStoreError

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux flock implementation')


@contextmanager
def locks_at(path):
    with patch.object(locking, '_filesystem_name', return_value='ext4'):
        with locking.DatasetLocks(path) as locks:
            yield locks


def hold_in_process(path, role, ready):
    with locks_at(path) as locks:
        with getattr(locks, role)('market.bars'):
            ready.set()
            time.sleep(60)


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / 'locks'
    path.mkdir(mode=0o700)
    return path


def assert_code(expected, action):
    with pytest.raises(DataStoreError) as caught:
        with action:
            pass
    assert caught.value.code == expected


def test_writer_planning_does_not_block_reads_but_commit_does(directory):
    with locks_at(directory) as first, locks_at(directory) as second:
        with first.writer('market.bars') as writer:
            with second.read('market.bars', timeout_ms=0):
                pass
            assert_code('LOCK_TIMEOUT', second.writer('market.bars', timeout_ms=20))
            with writer.commit():
                assert_code('LOCK_TIMEOUT', second.read('market.bars', timeout_ms=20))
                with second.read('market.nav', timeout_ms=0):
                    pass
                with second.writer('market.nav', timeout_ms=0) as other:
                    with other.commit(timeout_ms=0):
                        pass
        with second.writer('market.bars', timeout_ms=0):
            pass


def test_reader_keeps_current_files_safe_from_commit(directory):
    with locks_at(directory) as first, locks_at(directory) as second:
        with first.read('market.bars'):
            with second.read('market.bars', timeout_ms=0):
                pass
            with second.writer('market.bars') as writer:
                assert_code('LOCK_TIMEOUT', writer.commit(timeout_ms=20))


@pytest.mark.parametrize('role', ['writer', 'read'])
def test_process_termination_releases_kernel_lock(directory, role):
    ctx = multiprocessing.get_context('spawn')
    ready = ctx.Event()
    holder = ctx.Process(target=hold_in_process, args=(str(directory), role, ready))
    holder.start()
    try:
        assert ready.wait(15), 'Child did not acquire the kernel lock'
        with locks_at(directory) as locks:
            if role == 'writer':
                assert_code('LOCK_TIMEOUT', locks.writer('market.bars', timeout_ms=20))
            else:
                with locks.writer('market.bars') as writer:
                    assert_code('LOCK_TIMEOUT', writer.commit(timeout_ms=20))
        holder.kill()
        holder.join(10)
        assert not holder.is_alive()
        with locks_at(directory) as locks:
            with locks.writer('market.bars', timeout_ms=100) as writer:
                with writer.commit(timeout_ms=100):
                    pass
    finally:
        if holder.is_alive():
            holder.kill()
        holder.join(10)


def fork_checks(directory, result):
    # Fork while an actual writer fd is held. The child must close its inherited
    # duplicate WITHOUT unlocking the parent; an independent new fd still fails.
    read_end, write_end = os.pipe()
    child = None
    try:
        with locks_at(directory) as locks:
            with locks.writer('market.bars') as guard:
                child = os.fork()
                if child == 0:
                    os.close(read_end)
                    try:
                        assert_code('LOCK_CONTEXT_EXPIRED', guard.commit(timeout_ms=0))
                        with locks_at(directory) as other:
                            assert_code('LOCK_TIMEOUT', other.writer('market.bars', timeout_ms=0))
                        os.write(write_end, b'Y')
                    except BaseException:
                        os.write(write_end, b'N')
                    os._exit(0)
                import select
                assert select.select([read_end], [], [], 5)[0]
                assert os.read(read_end, 1) == b'Y'
            with locks_at(directory) as other:
                with other.writer('market.bars', timeout_ms=0):
                    pass
    finally:
        os.close(read_end)
        os.close(write_end)
        if child:
            finished, _ = os.waitpid(child, os.WNOHANG)
            if finished == 0:
                import signal
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
    result.send('passed')
    result.close()


def test_fork_child_cannot_hold_parent_locks_alive(directory):
    # Use a fresh interpreter: other tests (notably Arrow) can start threads.
    ctx = multiprocessing.get_context('spawn')
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=fork_checks, args=(directory, send))
    process.start()
    send.close()
    try:
        assert receive.poll(15), 'Fork checks did not finish within the test budget'
        assert receive.recv() == 'passed'
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        receive.close()


def test_cancelled_and_expired_guards_cannot_commit(directory):
    before = len(locking._FDS)
    with locks_at(directory) as locks:
        assert_code('OPERATION_CANCELLED', locks.writer('market.bars', cancelled=lambda: True))
        with locks.writer('market.bars') as writer:
            assert_code('OPERATION_CANCELLED', writer.commit(cancelled=lambda: True))
            failures = []
            def different_thread():
                try:
                    with writer.commit(timeout_ms=0):
                        pass
                except DataStoreError as error:
                    failures.append(error.code)
            thread = threading.Thread(target=different_thread)
            thread.start(); thread.join(5)
            assert failures == ['LOCK_CONTEXT_EXPIRED']
        assert_code('LOCK_CONTEXT_EXPIRED', writer.commit())
    assert len(locking._FDS) == before
    assert_code('LOCK_CONTEXT_EXPIRED', locks.read('market.bars'))


@pytest.mark.parametrize('filesystem', ['nfs', 'nfs4', 'cifs', 'overlay', 'tmpfs', 'unknown'])
def test_unsupported_filesystems_rejected_without_creating_locks(directory, filesystem):
    with patch.object(locking, '_filesystem_name', return_value=filesystem):
        with pytest.raises(DataStoreError) as error:
            locking.DatasetLocks(directory)
        assert error.value.code == 'UNSUPPORTED_FILESYSTEM'
    assert list(directory.iterdir()) == []


def test_default_probes_the_actual_mount(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        actual = locking._filesystem_name(fd)
    finally:
        os.close(fd)
    if actual in locking._LOCAL_FILESYSTEMS:
        with locking.DatasetLocks(directory) as locks:
            with locks.writer('probe'):
                pass
    else:
        with pytest.raises(DataStoreError) as error:
            locking.DatasetLocks(directory)
        assert error.value.code == 'UNSUPPORTED_FILESYSTEM'


def test_symlink_ancestor_and_lock_file_and_hard_link_rejected(directory, tmp_path):
    alias = tmp_path / 'alias'; alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(DataStoreError):
        with locks_at(alias):
            pass
    key = locking._scope_key('market.bars')
    victim = tmp_path / 'protected'; victim.write_text('must remain unchanged')
    lock = directory / f'{key}.writer.lock'
    lock.symlink_to(victim)
    with locks_at(directory) as locks:
        assert_code('UNSAFE_STORAGE_PATH', locks.writer('market.bars'))
        lock.unlink()
        os.link(victim, lock)
        assert_code('UNSAFE_STORAGE_PATH', locks.writer('market.bars'))
    assert victim.read_text() == 'must remain unchanged'


def test_changed_lock_directory_does_not_create_new_namespace(directory):
    with locks_at(directory) as locks:
        with locks.writer('market.bars') as writer:
            directory.rename(directory.with_name('previous-locks'))
            directory.mkdir(mode=0o700)
            assert_code('UNSAFE_STORAGE_PATH', writer.commit())


def test_wait_set_uses_one_deadline_sorted_keys_and_releases_on_failure(directory):
    with locks_at(directory) as locks:
        events = []
        original = locks._hold
        @contextmanager
        def record(dataset, role, operation, deadline, cancelled):
            events.append((dataset, deadline))
            with original(dataset, role, operation, deadline, cancelled):
                yield
        with patch.object(locks, '_hold', record):
            with locks.read_many(['z', 'a', 'z', 'b']):
                pass
        assert [key for key, _ in events] == ['a', 'b', 'z']
        assert len({deadline for _, deadline in events}) == 1
        with locks.writer('b') as writer:
            with writer.commit():
                assert_code('LOCK_TIMEOUT', locks.read_many(['a', 'b'], timeout_ms=20))
                with locks.writer('a') as first:
                    with first.commit(timeout_ms=0):
                        pass
        assert_code('CONTROL_BUDGET_EXCEEDED', locks.read_many(['a'] * 65))


def test_repeated_locking_has_constant_files_and_no_descriptor_leak(directory):
    initial = len(locking._FDS)
    with locks_at(directory) as locks:
        for _ in range(100):
            with locks.writer('market.bars') as writer:
                with writer.commit():
                    pass
        assert len(list(directory.iterdir())) == 2
    assert len(locking._FDS) == initial


@pytest.mark.parametrize('scope', ['../escape', 'market/quote', '', 'a' * 129, 1, 'a\x00b'])
def test_invalid_scope_never_becomes_a_path(directory, scope):
    with locks_at(directory) as locks:
        assert_code('INVALID_VALUE', locks.writer(scope))
    assert not list(directory.iterdir())


def test_invalid_wait_budget_and_world_writable_directory(directory):
    with locks_at(directory) as locks:
        for timeout in (-1, True, 1.1, None):
            assert_code('INVALID_CONFIGURATION', locks.read('a', timeout_ms=timeout))
    directory.chmod(0o777)
    try:
        with pytest.raises(DataStoreError) as error:
            with locks_at(directory):
                pass
        assert error.value.code == 'UNSAFE_STORAGE_PATH'
    finally:
        directory.chmod(0o700)


def test_removed_lock_during_acquire_returns_safe_error(directory):
    with locks_at(directory) as locks:
        original = locking._acquire
        def remove_after_acquiring(fd, operation, deadline, cancelled):
            original(fd, operation, deadline, cancelled)
            (directory / f'{locking._scope_key("a")}.writer.lock').unlink()
        with patch.object(locking, '_acquire', remove_after_acquiring):
            assert_code('UNSAFE_STORAGE_PATH', locks.writer('a'))
