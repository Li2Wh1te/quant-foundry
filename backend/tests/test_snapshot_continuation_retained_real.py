"""Exercise snapshot retry admission with retained provider records only.

The private fixture contains an actual snapshot history and an actual failed
scope. Only the first phase's current-head metadata is rewound to its retained
predecessor; provider payloads and dependency chains remain unchanged. The
fixture, source values, and execution receipts must stay outside the repository.
"""
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import time
from uuid import UUID

import pyarrow.parquet as pq
import pytest
from sqlalchemy import DateTime, Uuid, text

from app.data_ingestion.models.tonghuashun import TonghuashunObservation, TonghuashunCollectionState
from app.data_store.adapters.registry import BY_ID
from app.data_store.coverage import check_coverage
from app.data_store.limits import MiB, StoreLimits
from app.data_store.local_sources import NativeSources
from app.data_store.merge import value_hash
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_entry
from app.data_store.updates import RetryPolicy
from tests.test_data_store_kernel import database, store
from tests.test_data_store_incremental import ready


pytestmark = pytest.mark.skipif(
    os.getenv('POSTGRES_TEST_ENABLED') != '1' or not os.getenv('QF_RETAINED_E57_GUARD_INPUT'),
    reason='requires private retained snapshot input and disposable PostgreSQL',
)


@pytest.fixture
def limits():
    # Keep the normal kernel memory allowance for the retained wide snapshot;
    # only its private disk/log bounds are reduced for this admission regression.
    return replace(StoreLimits(), pipeline_spill_bytes=64*MiB, log_bytes=4096)


def decoded(table, row):
    """Restore database types without rewriting any retained source payload."""
    value = {key: row[key] for key in table.c.keys() if key in row}
    for column in table.c:
        if value.get(column.name) is None:
            continue
        if isinstance(column.type, DateTime):
            value[column.name] = datetime.fromisoformat(value[column.name].replace('Z', '+00:00'))
        elif isinstance(column.type, Uuid):
            value[column.name] = UUID(value[column.name])
    return value


def test_real_E57_unchanged_failed_snapshot_waits_and_later_original_head_is_admitted(ready):
    fixture = json.loads(Path(os.environ['QF_RETAINED_E57_GUARD_INPUT']).read_text())
    assert fixture['format'] == 'qf-r01-retained-e57-guard-v1'
    assert fixture['synthetic_business_payloads'] is False and fixture['production_writes'] == 0
    latest = fixture['latest_original_id']
    previous = fixture['observations'][fixture['previous_original_id']]
    good = next(row for row in fixture['states'] if row['subject'] == fixture['good_subject'])
    older_head = dict(good, observation_id=previous['id'], status='succeeded', error_kind=None,
                      revision=max(1, good['revision']-1), attempted_at=previous['observed_at'],
                      succeeded_at=previous['observed_at'])

    with ready.catalog.engine.begin() as connection:
        pending = {key: row for key, row in fixture['observations'].items() if key != latest}
        copied = set()
        while pending:
            batch = [(key, row) for key, row in pending.items()
                     if not row.get('base_observation_id') or row['base_observation_id'] in copied]
            assert batch, 'retained predecessor dependency is missing'
            connection.execute(TonghuashunObservation.__table__.insert(),
                               [decoded(TonghuashunObservation.__table__, row) for _, row in batch])
            for key, _ in batch:
                copied.add(key)
                pending.pop(key)
        connection.execute(TonghuashunCollectionState.__table__.insert(), [
            decoded(TonghuashunCollectionState.__table__,
                    older_head if row['subject'] == good['subject'] else row)
            for row in fixture['states']
        ])

    entry = BY_ID['E57']
    sources = NativeSources(ready.catalog.engine)
    options = PipelineOptions(maximum_passes=32, pass_seconds=120)
    clock = [int(time.time())]
    policy = RetryPolicy()
    first = run_entry(ready, entry, sources, options=options, _policy=policy, clock=lambda: clock[0])
    assert first['complete'] and first['qualified'] is False and first['input_failures'] > 0
    catalogue = ready.catalog.dataset(entry.spec.name)
    issues = ready.catalog.issues(entry.spec.name, limit=100)
    status = read_entry_status(ready, entry.id)

    # A normal recurring invocation after the ordinary backoff must not replay
    # unchanged snapshot history or refresh the failure/coverage clocks. Its
    # zero counts describe this invocation, while the original restrictions stay.
    clock[0] += 31
    unchanged = run_entry(ready, entry, sources, options=options,
                          _policy=policy, clock=lambda: clock[0])
    assert (unchanged['reason'], unchanged['source_rows'], unchanged['committed_partitions']) == (
        'SOURCE_INPUT_UNCHANGED', 0, 0)
    assert unchanged['budget_units'] == 0 and unchanged['attempted'] is False
    assert ready.catalog.dataset(entry.spec.name) == catalogue
    assert ready.catalog.issues(entry.spec.name, limit=100) == issues
    assert read_entry_status(ready, entry.id) == status

    # Publish the actual later immutable provider record and its captured head.
    # The guard must reconsider it, retain the unrelated genuine failed scope,
    # and keep whole-domain coverage closed while publishing the valid snapshot.
    with ready.catalog.engine.begin() as connection:
        connection.execute(TonghuashunObservation.__table__.insert(),
                           decoded(TonghuashunObservation.__table__, fixture['observations'][latest]))
        connection.execute(TonghuashunCollectionState.__table__.update().where(
            TonghuashunCollectionState.dataset == entry.native,
            TonghuashunCollectionState.subject == good['subject'],
            TonghuashunCollectionState.variant == good['variant'],
        ).values(**decoded(TonghuashunCollectionState.__table__, good)))
    clock[0] += 31
    later = run_entry(ready, entry, sources, options=options, _policy=policy, clock=lambda: clock[0])
    assert later['source_rows'] > 0 and later['committed_partitions'] > 0
    assert later['complete'] and later['qualified'] is False
    with ready.catalog.transaction() as connection:
        assert check_coverage(connection, entry, read_entry_status(ready, entry.id))['satisfied'] is False
        paths = list(connection.scalars(text('SELECT path FROM data_store_files WHERE dataset=:d'),
                                        {'d': entry.spec.name}))
    rows = [row for path in paths for row in pq.read_table(ready.files.root/path).to_pylist()
            if row['subject'] == good['subject'] and row['object_key'] == 'current']
    expected = fixture['expected_latest']
    assert len(rows) == expected['rows'] and value_hash(rows) == expected['value_hash']
    assert all(row['basis_state'] == 'valid' and str(row['basis_ns']) == expected['basis_ns']
               and row['basis_group'] == expected['basis_group']
               and row['basis_token'] == expected['basis_token'] for row in rows)
    assert any(fixture['real_failed_subject'] in row['target_json']
               for row in ready.catalog.issues(entry.spec.name, limit=100))
