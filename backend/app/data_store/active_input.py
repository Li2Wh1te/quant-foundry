"""Read-only plans and atomic admission for one existing fund-object batch.

The plan contains at most 64 source-boundary descriptors, never business rows
or a successful-row ledger. Admission holds the ordinary entry pipeline lock
and locks these range rows in one repeatable-read transaction. Producers may
append later pending work after that transaction; it remains outside this batch.
"""
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .adapters.contracts import digest
from .incremental import FundBatchSources, available, next_observation
from .local_sources import NativeSources
from .pipeline import PipelineOptions, continuation_refused, input_identity, read_entry_status


MAX_SCOPES = 64


@dataclass(frozen=True)
class ActiveInput:
    plan: dict
    pairs: tuple


def _read_selection(connection, entry, sources, *, lock):
    # The extra row detects an oversized active set rather than silently
    # shrinking an operator-approved scope to the first 64 eligible ranges.
    rows = connection.execute(text(
        'SELECT source,dataset,subject,variant,range_key,active '
        'FROM data_store_source_ranges r '
        'WHERE source=:source AND dataset=:dataset AND active IS NOT NULL '
        "AND (active->'blocked' IS NULL OR EXISTS(SELECT 1 FROM tonghuashun_collection_states s "
        "WHERE s.dataset=r.dataset AND s.subject=r.subject AND s.variant=r.variant AND s.status='succeeded')) "
        'ORDER BY subject,variant,range_key LIMIT :limit' + (' FOR UPDATE OF r' if lock else '')),
        {'source': entry.source, 'dataset': entry.native, 'limit': MAX_SCOPES + 1}).mappings().all()
    if not rows or len(rows) > MAX_SCOPES:
        raise continuation_refused('ACTIVE_INPUT_REQUIRED')
    pairs, manifest = [], []
    for row in rows:
        job = dict(row)
        observation = next_observation(sources, entry, job, connection=connection)
        if observation is None:
            # State-only or exhausted claims need their normal consumer, not
            # an implicit acknowledgement inside an approved decoding attempt.
            raise continuation_refused('ACTIVE_INPUT_REQUIRED')
        metadata = connection.execute(text(
            "SELECT content_hash,chain_depth,base_observation_id::text AS base_id,"
            "encode(sha256(convert_to(request_json,'UTF8')),'hex') AS request_sha256 "
            'FROM tonghuashun_observations WHERE id=CAST(:id AS uuid)'), observation).mappings().one()
        manifest.append({**job, 'observation': {**observation, **dict(metadata)}})
        pairs.append((job, observation))
    namespace = connection.execute(text('SELECT current_database(),current_schema()')).one()
    capture_version = connection.execute(text(
        'SELECT version FROM data_store_capture_version WHERE singleton=1')).scalar_one()
    native_selection = digest(['native', sources.engine.url.host, sources.engine.url.port, *namespace])
    segment = FundBatchSources(sources, entry, pairs)
    plan = {
        'version': 1, 'entry_id': entry.id, 'source': entry.source, 'dataset': entry.native,
        'scope_count': len(pairs), 'scopes': manifest,
        'capture_version': capture_version,
        'input_identity': input_identity(entry, segment, PipelineOptions()),
        'source_selection': digest(['active-input@1', entry.id, native_selection, capture_version, manifest]),
        'pipeline_source_selection': digest([native_selection, [(j['active']['id'], o) for j, o in pairs]]),
        'claim_batches': 1, 'supplier_network_used': False, 'production_executed': False,
    }
    return ActiveInput(plan, tuple(pairs))


def active_input_plan(store, entry, sources, *, options=None, cancelled=None):
    """With options, validate both fences before any refresh/catalog mutation.

    This function never creates a claim, changes its cursor, registers a source
    contract, seeds a queue, or decodes a source payload. Read-only planning does
    not take PostgreSQL row locks; approved admission does, then releases them
    before filesystem work. The enclosing pipeline lock remains owned by the
    normal processing entrypoint, excluding another CurrentStore consumer.
    """
    if type(sources) is not NativeSources or entry.id not in ('E23', 'E44'):
        raise continuation_refused('ACTIVE_INPUT_REQUIRED')
    if cancelled and cancelled():
        raise continuation_refused('OPERATION_CANCELLED')
    old = read_entry_status(store, entry.id)
    if (old.get('incremental', {}).get('contract') != digest(entry.spec.descriptor()) or
            not available(sources.engine, entry)):
        raise continuation_refused('ACTIVE_INPUT_REQUIRED')
    try:
        with sources.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                if options is None:
                    connection.execute(text('SET TRANSACTION READ ONLY'))
                connection.execute(text("SELECT set_config('statement_timeout','5000',true)"))
                connection.execute(text("SELECT set_config('lock_timeout','5000',true)"))
                admitted = _read_selection(connection, entry, sources, lock=options is not None)
                if options is not None and (
                        admitted.plan['input_identity'] != options.expected_input_identity or
                        admitted.plan['source_selection'] != options.expected_source_selection):
                    raise continuation_refused('SOURCE_CONFLICT')
                if cancelled and cancelled():
                    raise continuation_refused('OPERATION_CANCELLED')
        return admitted
    except SQLAlchemyError:
        # A serialization/lock/database failure grants no partial admission and
        # must not leak a SQL statement, DSN, or provider payload into a result.
        raise continuation_refused('CATALOG_UNAVAILABLE') from None
