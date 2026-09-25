"""Bounded cleanup, batch-only logs and scoped shared-task summary retention."""
from __future__ import annotations

from dataclasses import asdict
import fcntl
import json
import os
import re
import stat
import time
from uuid import uuid4

from sqlalchemy import text

from .errors import DataStoreError, MESSAGES
from .locking import _deadline, _scope_key
from .schema import identifier
from .values import control_json

# Keep completed-run retention scoped to the one registered local task type.
TASK_TYPES = ('data_store.update_local',)
_LOG = re.compile(r'(\d{20})-[0-9a-f]{32}\.jsonl\Z')


def cleanup(store, dataset: str, *, limit: int | None = None):
    """Never infer liveness from age alone. Acquire writer then commit lock.

    The queue also holds pre-commit file intents, so no unbounded filesystem
    discovery scan is needed. Grace applies to prepared files; retired files
    can be removed as soon as preceding legitimate readers have left.
    """
    identifier(dataset)
    limit = store.limits.cleanup_batch if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= store.limits.cleanup_batch:
        raise DataStoreError('INVALID_CONFIGURATION')
    deleted = 0
    with store.locks.writer(dataset, timeout_ms=store.limits.lock_timeout_ms) as guard:
        with guard.commit(timeout_ms=store.limits.lock_timeout_ms):
            with store.catalog.transaction() as c:
                entries = c.execute(text('SELECT * FROM data_store_garbage WHERE dataset=:d '
                    'AND not_before<=clock_timestamp() AND retry_after<=clock_timestamp() '
                    'ORDER BY retry_after,path LIMIT :n'), {'d': dataset, 'n': limit}).mappings().all()
            for entry in entries:
                with store.catalog.transaction() as c:
                    referenced = c.execute(text('SELECT EXISTS(SELECT 1 FROM data_store_files WHERE path=:p)'),
                                           {'p': entry['path']}).scalar_one()
                if referenced:
                    # Current wins, including recovery from an uncertain acknowledgement.
                    continue
                try:
                    if not entry['path'].startswith('objects/'+_scope_key(dataset)+'/'):
                        raise DataStoreError('UNSAFE_STORAGE_PATH')
                    store.fault('before_delete')
                    store.files.delete_object(entry['path'])
                    store.fault('after_delete')
                except (OSError, DataStoreError):
                    with store.catalog.transaction() as c:
                        c.execute(text('UPDATE data_store_garbage SET '
                            'attempts=LEAST(attempts,2147483646)+1,error_code=:e,'
                            "retry_after=clock_timestamp()+:delay*interval '1 second' WHERE path=:p"),
                            {'e': 'STORAGE_UNAVAILABLE', 'p': entry['path'],
                             'delay': min(3600, 2 ** min(entry['attempts'], 12))})
                    continue
                with store.catalog.transaction() as c:
                    c.execute(text('DELETE FROM data_store_garbage WHERE path=:p'), {'p': entry['path']})
                deleted += 1
            with store.catalog.transaction() as c:
                pending = c.execute(text('SELECT count(*) FROM data_store_garbage WHERE dataset=:d'),
                                    {'d': dataset}).scalar_one()
    result = {'deleted': deleted, 'pending': pending}
    try:
        result['logs'] = prune_summary_logs(store)
    except (OSError, DataStoreError):
        result['log_cleanup_pending'] = True
    return result


def _log_entries(store, fd, now, reserve_bytes=0):
    """Called only under the shared log lock; returns retained ordered segments."""
    entries = []
    with os.scandir(fd) as iterator:
        for count, e in enumerate(iterator):
            if count >= 2048:
                raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
            match = _LOG.fullmatch(e.name)
            s = e.stat(follow_symlinks=False)
            if (not match or not stat.S_ISREG(s.st_mode) or s.st_nlink != 1
                    or s.st_mode & stat.S_IWOTH):
                raise DataStoreError('UNSAFE_STORAGE_PATH')
            created = int(match[1])
            if created <= now - store.limits.log_days * 86400 * 10**9:
                os.unlink(e.name, dir_fd=fd)
            else:
                entries.append([created, e.name, s.st_size])
    entries.sort()
    total = sum(e[2] for e in entries)
    while entries and total + reserve_bytes > store.limits.log_bytes:
        _, name, size = entries.pop(0)
        os.unlink(name, dir_fd=fd)
        total -= size
    return entries


def prune_summary_logs(store):
    """Enforce age/bytes during maintenance even when no new logs are written."""
    with store.locks._hold('store.internal', 'logs', fcntl.LOCK_EX,
                           _deadline(store.limits.lock_timeout_ms), None):
        with store.files.directory('.logs') as fd:
            entries = _log_entries(store, fd, time.time_ns())
            os.fsync(fd)
            return {'retained_segments': len(entries), 'retained_bytes': sum(e[2] for e in entries)}


def write_summary(store, dataset: str, outcome: str, metrics):
    """Exactly one bounded summary per batch, success AND errors share a cap.

    All processes serialize rotation via the same local lock. Segment timestamps
    record creation (not last append), so infrequent writes cannot keep old logs
    forever by touching their mtime. No row values/source payloads are accepted.
    """
    identifier(dataset)
    if outcome not in {'committed', 'noop', 'failed'} | MESSAGES.keys():
        outcome = 'failed'
    now = time.time_ns()
    line = (control_json({'at_ns': now, 'dataset': dataset, 'outcome': outcome,
                          'metrics': asdict(metrics)}, max_bytes=2048)+'\n').encode()
    if len(line) > store.limits.log_bytes:
        raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
    with store.locks._hold('store.internal', 'logs', fcntl.LOCK_EX,
                           _deadline(store.limits.lock_timeout_ms), None):
        with store.files.directory('.logs') as fd:
            entries = _log_entries(store, fd, now, len(line))
            target = None
            segment_limit = max(len(line), min(1024**2, store.limits.log_bytes//2))
            if entries:
                created, name, size = entries[-1]
                if created//(86400*10**9) == now//(86400*10**9) and size+len(line) <= segment_limit:
                    target = name
            flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
            if target is None:
                target = f'{now:020}-{uuid4().hex}.jsonl'
                flags |= os.O_CREAT | os.O_EXCL
            handle = os.open(target, flags, 0o660, dir_fd=fd)
            try:
                store.files._regular(handle)
                offset = 0
                while offset < len(line):
                    offset += os.write(handle, line[offset:])
                os.fsync(handle)
            finally:
                os.close(handle)
            os.fsync(fd)


def prune_completed_runs(catalog, *, batch: int = 1000) -> int:
    """Only terminal LF store task types, 30 days AND max 1000 per task.

    Rows selected via a bounded result; database restriction constraints still
    protect any unexpected shared dependency. No CASCADE or prefix deletion.
    Active tasks and unresolved data_store_issues are never touched.
    """
    if type(batch) is not int or not 1 <= batch <= 1000:
        raise DataStoreError('INVALID_CONFIGURATION')
    with catalog.transaction() as c:
        result = c.execute(text("""
            WITH terminal AS (
                SELECT id, finished_at,
                    row_number() OVER (PARTITION BY task_id ORDER BY finished_at DESC, id DESC) AS ordinal
                FROM task_runs WHERE task_type = :task_type
                  AND status IN ('succeeded','failed','skipped','interrupted','cancelled','timed_out','indeterminate')
                  AND finished_at IS NOT NULL
            ), expired AS (
                SELECT id FROM terminal WHERE ordinal>:cap
                  OR finished_at < clock_timestamp()-:days*interval '1 day'
                ORDER BY finished_at,id LIMIT :batch
            )
            DELETE FROM task_runs WHERE id IN (SELECT id FROM expired) RETURNING id
        """), {'task_type': TASK_TYPES[0], 'cap': catalog.limits.completed_runs_per_task,
                 'days': catalog.limits.completed_run_days, 'batch': batch})
        return len(result.all())
