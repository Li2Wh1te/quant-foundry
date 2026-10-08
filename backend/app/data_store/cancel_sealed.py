"""End one explicitly identified obsolete seal without acknowledging input."""
import fcntl
import json
import re

from sqlalchemy import text

from .locking import _deadline
from .merge import MergeSpool
from .pipeline import continuation_refused, _pending_operations
from .values import control_json


def cancel_sealed(store, entry, *, expected_identity, expected_selection, cancelled=None):
    """Preserve current files, issues and source cursors; release only this spool.

    The ordinary entry/slot locks fence consumers. A durable current status is
    written before disposal so interruption cannot make cancellation appear as
    successful consumption. Repeating an interrupted cancellation requires the
    same two original fences. No source-range claim is cleared or advanced.
    """
    if (not entry.business or any(not isinstance(value, str) or
            re.fullmatch(r'[0-9a-f]{64}', value) is None
            for value in (expected_identity, expected_selection))):
        raise continuation_refused('INVALID_CONFIGURATION')
    owner = 'pipeline.' + entry.id
    marker = {'identity': expected_identity, 'source_selection': expected_selection,
              'reason': 'OBSOLETE_SEALED_INPUT', 'input_acknowledged': False}
    with store.locks._hold(entry.spec.name, 'pipeline', fcntl.LOCK_EX,
                           _deadline(store.limits.lock_timeout_ms), cancelled):
        if owner not in store.budget.pending_keys():
            with store.catalog.transaction() as connection:
                raw = connection.execute(text(
                    'SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i'
                ), {'i': entry.id}).scalar_one_or_none()
            if not raw or json.loads(raw).get('cancelled_seal') != marker:
                raise continuation_refused('SEALED_CONTINUATION_REQUIRED')
            return {'entry_id': entry.id,
                    'message': f'{entry.native} 过期待处理批次已取消，已提交内容和来源游标保留，暂存配额已释放。',
                    'cancelled': True, 'idempotent': True,
                    'scratch_released': True, 'input_acknowledged': False}
        with store.budget.reserve('read', pending=owner, cancelled=cancelled) as space:
            progress = MergeSpool.read_sealed_progress(space)
            if (progress.get('identity') != expected_identity or
                    progress.get('source_selection') != expected_selection):
                raise continuation_refused('SOURCE_CONFLICT')
            with store.catalog.transaction() as connection:
                raw = connection.execute(text(
                    'SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i FOR UPDATE'
                ), {'i': entry.id}).scalar_one_or_none()
                summary = json.loads(raw) if raw else {'entry_id': entry.id}
                summary.update(cancelled_seal=marker, state='incomplete', complete=False,
                    qualified=False, reason='OBSOLETE_SEALED_INPUT',
                    coverage_pending=_pending_operations(summary, 'entry'))
                connection.execute(text(
                    'INSERT INTO data_store_entry_status(entry_id,summary_json) VALUES (:i,:j) '
                    'ON CONFLICT(entry_id) DO UPDATE SET summary_json=EXCLUDED.summary_json, '
                    'updated_at=clock_timestamp()'
                ), {'i': entry.id, 'j': control_json(summary)})
            # The reservation holds the exact idle slot until its normal cleanup
            # runs. Failure leaves charged work recoverable under the same fence.
            space.discard = True
        released = owner not in store.budget.pending_keys()
        return {'entry_id': entry.id,
                'message': f'{entry.native} 过期待处理批次已取消，已提交内容和来源游标保留，暂存配额' +
                    ('已释放。' if released else '等待安全清理。'),
                'cancelled': True, 'idempotent': False,
                'scratch_released': released, 'cleanup_pending': not released,
                'input_acknowledged': False}
