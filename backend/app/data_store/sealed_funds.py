"""Recover one charged fund batch from its existing control checkpoint.

Members are bounded source-boundary metadata, never payloads or a completed-row
ledger. New seals keep them in the same disposable SQLite progress record. Old
operator seals can use the entry's active-observation checkpoint only when the
original active-input digest proves every reconstructed claim and observation.
"""
from dataclasses import dataclass
import json
import re
import time
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .adapters.contracts import digest
from .merge import MergeSpool
from .pipeline import continuation_refused, validate_sealed_continuation


CLAIM_FIELDS = ('source', 'dataset', 'subject', 'variant', 'range_key', 'active')


@dataclass(frozen=True)
class SealedFundBatch:
    pairs: tuple
    progress: dict


def _observation(value):
    """Refuse missing/duplicate/noncanonical IDs rather than guessing a scope."""
    if (not isinstance(value, dict) or not isinstance(value.get('at'), str) or
            not isinstance(value.get('id'), str)):
        raise continuation_refused('SOURCE_CONFLICT')
    try:
        if str(UUID(value['id'])) != value['id']:
            raise ValueError('Noncanonical observation ID')
    except ValueError:
        raise continuation_refused('SOURCE_CONFLICT') from None
    return {key: value[key] for key in ('at', 'id')}


def _manifest(sources, entry, selected, *, recorded=False, cancelled=None):
    """Read only the recorded observations and their exact original claims.

    There is no eligible-set query here. A recovered unrelated blocked claim,
    including a state-only claim with stop=null, cannot enter this transaction.
    Producers can append pending work; the original active boundary must match.
    The shared metadata deadline also bounds the predecessor/cursor checks.
    """
    from .active_input import MAX_METADATA_SECONDS, _metadata_check
    from .incremental import next_observation
    if not isinstance(selected, (list, tuple)) or not 1 <= len(selected) <= 64:
        raise continuation_refused('SOURCE_CONFLICT')
    if recorded and any(not isinstance(item, dict) or 'observation' not in item for item in selected):
        raise continuation_refused('SOURCE_CONFLICT')
    observations = [_observation(item['observation'] if recorded else item) for item in selected]
    if len({item['id'] for item in observations}) != len(observations):
        raise continuation_refused('SOURCE_CONFLICT')
    deadline = time.monotonic() + MAX_METADATA_SECONDS
    manifest, pairs = [], []
    try:
        with sources.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                connection.execute(text('SET TRANSACTION READ ONLY'))
                connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                connection.execute(text("SET LOCAL lock_timeout='5s'"))
                for selected_item, observation in zip(selected, observations):
                    _metadata_check(connection, deadline, cancelled)
                    metadata = connection.execute(text(
                        'SELECT dataset,subject,variant,observed_at::text AS at,id::text AS id,'
                        'content_hash,chain_depth,base_observation_id::text AS base_id,'
                        "encode(sha256(convert_to(request_json,'UTF8')),'hex') AS request_sha256 "
                        'FROM tonghuashun_observations WHERE id=CAST(:id AS uuid)'),
                        observation).mappings().first()
                    if (metadata is None or metadata['dataset'] != entry.native or
                            {key: metadata[key] for key in ('at', 'id')} != observation):
                        raise continuation_refused('SOURCE_CONFLICT')
                    params = dict(source=entry.source, dataset=entry.native,
                                  subject=metadata['subject'], variant=metadata['variant'])
                    where = 'source=:source AND dataset=:dataset AND subject=:subject AND variant=:variant'
                    if recorded:
                        if any(selected_item.get(key) != params[key] for key in params):
                            raise continuation_refused('SOURCE_CONFLICT')
                        params['range_key'] = selected_item.get('range_key')
                        where += ' AND range_key=:range_key'
                    _metadata_check(connection, deadline, cancelled)
                    claims = connection.execute(text(
                        'SELECT source,dataset,subject,variant,range_key,active '
                        'FROM data_store_source_ranges WHERE ' + where + ' LIMIT 2'), params).mappings().all()
                    if len(claims) != 1 or not isinstance(claims[0]['active'], dict):
                        raise continuation_refused('SOURCE_CONFLICT')
                    job = dict(claims[0])
                    if recorded and any(selected_item.get(key) != job[key] for key in CLAIM_FIELDS):
                        # This includes the complete original stop, lower,
                        # cursor and claim ID, even if next_observation stayed
                        # equal after someone widened the original stop.
                        raise continuation_refused('SOURCE_CONFLICT')
                    _metadata_check(connection, deadline, cancelled)
                    if next_observation(sources, entry, job, connection=connection) != observation:
                        raise continuation_refused('SOURCE_CONFLICT')
                    descriptor = {**job, 'observation': {key: metadata[key] for key in
                        ('at', 'id', 'content_hash', 'chain_depth', 'base_id', 'request_sha256')}}
                    if recorded and descriptor != selected_item:
                        raise continuation_refused('SOURCE_CONFLICT')
                    manifest.append(descriptor)
                    pairs.append((job, observation))
                _metadata_check(connection, deadline, cancelled)
                namespace = connection.execute(text('SELECT current_database(),current_schema()')).one()
                _metadata_check(connection, deadline, cancelled)
                version = connection.execute(text(
                    'SELECT version FROM data_store_capture_version WHERE singleton=1')).scalar_one()
    except SQLAlchemyError:
        raise continuation_refused('CATALOG_UNAVAILABLE') from None
    # The namespace formula is the existing active-input contract. Keeping it
    # unchanged makes old admitted seals verifiable without a migration or a
    # hand-edited member list. Capture version changes invalidate that proof.
    native_selection = digest(['native', sources.engine.url.host, sources.engine.url.port, *namespace])
    active_fence = digest(['active-input@1', entry.id, native_selection, version, manifest])
    if len(json.dumps(manifest).encode()) > 60000:
        raise continuation_refused('SOURCE_CONFLICT')
    return manifest, tuple(pairs), active_fence


def checkpoint_members(sources, entry, pairs, *, expected_active_fence=None, cancelled=None):
    """Capture bounded member metadata into the existing new-seal progress."""
    selected = [observation for _, observation in pairs]
    manifest, current_pairs, active_fence = _manifest(sources, entry, selected, cancelled=cancelled)
    if expected_active_fence is not None and active_fence != expected_active_fence:
        # Admission and decoding use separate metadata transactions. Recheck
        # the original full fence before the payload scan, including metadata
        # changes that would not change the first observation's ID or time.
        raise continuation_refused('SOURCE_CONFLICT')
    if [(job['active'], item) for job, item in current_pairs] != [
            (job['active'], item) for job, item in pairs]:
        raise continuation_refused('SOURCE_CONFLICT')
    return manifest


def restore_fund_batch(store, entry, sources, options, status, *, cancelled=None):
    """Validate original membership and both fences before a refresh write."""
    from .incremental import FundBatchSources, available
    if (status.get('incremental', {}).get('contract') != digest(entry.spec.descriptor()) or
            not available(sources.engine, entry) or
            'pipeline.' + entry.id not in store.budget.pending_keys()):
        raise continuation_refused('SEALED_CONTINUATION_REQUIRED')
    with store.budget.reserve('read', pending='pipeline.' + entry.id, cancelled=cancelled,
                              quota_bytes=options.pipeline_spill_bytes) as space:
        progress = MergeSpool.read_sealed_progress(space)
    if (progress.get('identity') != options.expected_input_identity or
            progress.get('source_selection') != options.expected_source_selection):
        raise continuation_refused('SOURCE_CONFLICT')
    selected = progress.get('source_members')
    recorded = selected is not None
    if not recorded:
        # Pre-upgrade operator seals already have a complete active-input hash
        # and a bounded active-observation checkpoint. Both are required: a
        # bare selection digest cannot prove an old member's original stop.
        fence = progress.get('active_input_fence')
        if not isinstance(fence, str) or not re.fullmatch(r'[0-9a-f]{64}', fence):
            raise continuation_refused('SEALED_CONTINUATION_REQUIRED')
        marker = status.get('incremental', {}).get('active_observation')
        selected = marker.get('batch') if isinstance(marker, dict) else None
    manifest, pairs, active_fence = _manifest(sources, entry, selected,
        recorded=recorded, cancelled=cancelled)
    if progress.get('active_input_fence') is not None and progress['active_input_fence'] != active_fence:
        raise continuation_refused('SOURCE_CONFLICT')
    segment = FundBatchSources(sources, entry, pairs)
    validate_sealed_continuation(store, entry, segment, options, cancelled=cancelled)
    return SealedFundBatch(pairs, progress)
