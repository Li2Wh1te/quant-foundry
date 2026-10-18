"""Finite continuation eligibility, independent from whole-domain acceptance."""
import hashlib
import json

from sqlalchemy import text

from .local_sources import NativeSources


WAITING = frozenset({'SCRATCH_ADMISSION_DEFERRED', 'WRITE_ADMISSION_DEFERRED'})
FATAL = frozenset({'OPERATION_CANCELLED','CATALOG_UNAVAILABLE','CATALOG_MISMATCH',
    'DATA_STORE_NOT_INITIALIZED','DATA_STORE_REBUILDING','DISK_PRESSURE',
    'UNSAFE_STORAGE_PATH','UNSUPPORTED_FILESYSTEM','STORAGE_UNAVAILABLE','MEMORY_PRESSURE',
    'COMMIT_UNKNOWN','GARBAGE_BUDGET_EXCEEDED'})
PENDING = WAITING | {'PASS_BUDGET_EXCEEDED', 'NATIVE_RANGES_PENDING',
                     'LOCAL_UPDATE_BUDGET_EXCEEDED', 'RETRY_BACKOFF'}


def source_revision(sources, entry):
    """Hash current source-head metadata only; this is never quality evidence.

    A blocked native scope may be reconsidered after a real source revision,
    including a changed failure state. Stream every identity/revision rather
    than using MAX(timestamp), which misses corrections to an older subject.
    Neither payloads nor provider requests are needed to detect that change.
    """
    if type(sources) is not NativeSources or entry.id not in {'E23','E44','E50','E51','E52'}:
        return None
    result = hashlib.sha256()
    with sources._snapshot() as (connection, _):
        rows = connection.execution_options(stream_results=True).execute(text(
            'SELECT subject,variant,revision,status,observation_id '
            'FROM tonghuashun_collection_states WHERE dataset=:dataset ORDER BY subject,variant'
        ), {'dataset': entry.native}).yield_per(512)
        for row in rows:
            sources._check()
            result.update(json.dumps(list(row), separators=(',', ':'), default=str).encode())
            result.update(b'\n')
    return result.hexdigest()


def disposition(result, *, now, sealed=False):
    """Describe execution eligibility without granting coverage or clearing issues."""
    reason = result.get('reason')
    progress = bool(result.get('committed_partitions') or result.get('acknowledged_ranges'))
    if result.get('complete') and result.get('qualified'):
        state, eligible = 'complete', False
    elif reason in FATAL or (result.get('outcome')=='backoff' and
                            (result.get('refresh') or {}).get('reason') in FATAL):
        state, eligible = 'blocked_global_error', False
    elif (reason in PENDING and (progress or sealed)
          and not (result.get('overflow_restriction') or {}).get('blocking_objects')):
        # Remaining sealed partitions are new disposition work, even when the
        # captured input contains invalid objects. Draining them never replays
        # already committed partitions or grants the input a valid/whole scope.
        # Its restrictions remain until a later exact valid source resolves them.
        state = 'progressed_pending_with_issues' if result.get('input_failures') or result.get('new_issues') else 'progressed_pending'
        eligible = True
    elif (result.get('overflow_restriction') or {}).get('blocking_objects') or result.get('input_failures') or result.get('new_issues') or reason in {
            'SOURCE_INPUT_UNCHANGED', 'SOURCE_RANGES_BLOCKED'}:
        state, eligible = 'blocked_source', False
    elif reason in PENDING:
        eligible = bool(progress or sealed or result.get('outcome') in {'deferred', 'backoff'})
        state = 'progressed_pending' if progress else 'deferred' if eligible else 'blocked_no_progress'
    else:
        state, eligible = 'blocked_quality_or_error', False
    refresh = result.get('refresh') or {}
    # A genuine capacity wait must not become a tight resubmission loop. Other
    # positive progress can continue promptly, with the scheduler's normal age
    # ordering giving already waiting work priority over the new continuation.
    delay = 30 if reason in WAITING or not progress else 1
    return {'version': 1, 'state': state, 'eligible': eligible,
            'available_at': max(int(now) + delay, refresh.get('next_retry_at') or 0),
            'checkpoint_advanced': progress}
