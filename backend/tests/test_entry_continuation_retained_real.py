"""Check recurring admission with bounded, unchanged retained provider scopes.

The private input contains actual native states, original payloads, dependency
chains, and one real unresolved whole-subject restriction. The second phase
adds another retained native scope without inventing a payload or rewriting a
provider timestamp. Runtime data and receipts remain outside the repository.
"""
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import time
from uuid import UUID

import pytest
from sqlalchemy import DateTime, Uuid, text

from app.data_ingestion.models.tonghuashun import TonghuashunCollectionState, TonghuashunObservation
from app.data_store.adapters.registry import BY_ID
from app.data_store.catalog import Issue
from app.data_store.coverage import check_coverage
from app.data_store.continuation import source_revision
from app.data_store.errors import DataStoreError
from app.data_store.limits import MiB, StoreLimits
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_entry
from app.data_store.updates import RetryPolicy
from tests.test_data_store_kernel import database, store
from tests.test_data_store_incremental import ready


pytestmark = pytest.mark.skipif(
    os.getenv('POSTGRES_TEST_ENABLED') != '1' or not os.getenv('QF_RETAINED_ENTRY_GUARD_INPUT'),
    reason='requires private retained provider scopes and disposable PostgreSQL',
)
FIRST_ENTRIES = ('E04', 'E10', 'E24', 'E25', 'E29', 'E36', 'E37', 'E38', 'E40', 'E43', 'E47')


@pytest.fixture(scope='module')
def retained():
    value = json.loads(Path(os.environ['QF_RETAINED_ENTRY_GUARD_INPUT']).read_text())
    assert value['format'] == 'qf-r01-retained-entry-guard-v1'
    assert value['synthetic_business_payloads'] is False and value['production_writes'] == 0
    assert set(value['cases']) == set(FIRST_ENTRIES)
    return value


@pytest.fixture
def limits():
    # Keep the ordinary kernel memory allowance and reduce only private test
    # disk/log allowances. These fixtures never mount a production data root.
    return replace(StoreLimits(), pipeline_spill_bytes=128 * MiB, log_bytes=4096)


def decoded(table, row):
    """Restore database scalar types without changing source JSON bytes."""
    value = {key: row[key] for key in table.c.keys() if key in row}
    for column in table.c:
        if value.get(column.name) is None:
            continue
        if isinstance(column.type, DateTime):
            value[column.name] = datetime.fromisoformat(value[column.name].replace('Z', '+00:00'))
        elif isinstance(column.type, Uuid):
            value[column.name] = UUID(value[column.name])
    return value


def insert_originals(current, observations, states):
    """Install actual immutable chains before their captured native heads."""
    with current.catalog.engine.begin() as connection:
        pending = dict(observations)
        copied = set()
        while pending:
            batch = [(key, row) for key, row in pending.items()
                     if not row.get('base_observation_id') or row['base_observation_id'] in copied]
            assert batch, 'retained dependency chain is incomplete'
            connection.execute(TonghuashunObservation.__table__.insert(),
                               [decoded(TonghuashunObservation.__table__, row) for _, row in batch])
            for key, _ in batch:
                copied.add(key)
                pending.pop(key)
        connection.execute(TonghuashunCollectionState.__table__.insert(),
                           [decoded(TonghuashunCollectionState.__table__, row) for row in states])


def assert_unchanged_wait(current, entry, options, clock):
    """Compare complete persisted state, so a zero counter cannot hide writes."""
    def file_metadata():
        with current.catalog.transaction() as connection:
            return [dict(row) for row in connection.execute(text(
                'SELECT partition_key,path,content_hash,row_count,byte_count,key_min,key_max '
                'FROM data_store_files WHERE dataset=:dataset ORDER BY partition_key,path'
            ), {'dataset': entry.spec.name}).mappings()]

    catalogue = current.catalog.dataset(entry.spec.name)
    issues = current.catalog.issues(entry.spec.name, limit=1000)
    files = file_metadata()
    status = read_entry_status(current, entry.id)
    clock[0] += 1801
    result = run_entry(current, entry, NativeSources(current.catalog.engine), options=options,
                       _policy=RetryPolicy(), clock=lambda: clock[0])
    assert result['reason'] == 'SOURCE_INPUT_UNCHANGED'
    assert result['attempted'] is False and result['source_scanned'] is False
    assert all(result.get(key) == 0 for key in (
        'source_rows', 'normalized_units', 'committed_partitions', 'budget_units',
        'new_issues', 'resolved', 'input_failures'))
    assert current.catalog.dataset(entry.spec.name) == catalogue
    assert current.catalog.issues(entry.spec.name, limit=1000) == issues
    assert file_metadata() == files
    assert read_entry_status(current, entry.id) == status


@pytest.mark.parametrize('entry_id', FIRST_ENTRIES)
def test_real_uncovered_scope_waits_unchanged_and_accepts_actual_added_scope(ready, retained, entry_id):
    case = retained['cases'][entry_id]
    insert_originals(ready, case['base_observations'], case['base_states'])
    entry = BY_ID[entry_id]
    options = PipelineOptions(maximum_passes=128, pass_seconds=240)
    clock = [int(time.time())]
    first = run_entry(ready, entry, NativeSources(ready.catalog.engine), options=options,
                      _policy=RetryPolicy(), clock=lambda: clock[0])
    assert first['complete'] and first['qualified'] is False
    assert first['input_failures'] > 0 and first['source_rows'] > 0
    assert_unchanged_wait(ready, entry, options, clock)

    # This is a different actual source identity, including observations older
    # than the prior fixture scope. A MAX(timestamp) shortcut cannot replace
    # the complete identity/revision fingerprint when accepting this change.
    insert_originals(ready, case['later_observations'], [case['later_state']])
    clock[0] += 1801
    later = run_entry(ready, entry, NativeSources(ready.catalog.engine), options=options,
                      _policy=RetryPolicy(), clock=lambda: clock[0])
    assert later['source_rows'] > 0 and later['budget_units'] > 0
    assert later['complete'] and later['qualified'] is False
    assert any(case['actual_failed_subject'] in row['target_json']
               for row in ready.catalog.issues(entry.spec.name, limit=1000))
    with ready.catalog.transaction() as connection:
        assert not check_coverage(connection, entry, read_entry_status(ready, entry.id))['satisfied']
    assert_unchanged_wait(ready, entry, options, clock)


def test_real_valid_report_windows_do_not_replay_an_unresolved_whole_subject(ready, retained):
    case = retained['legacy_unresolved_case']
    entry = BY_ID[case['entry_id']]
    insert_originals(ready, case['base_observations'], case['base_states'])
    ready.register(entry.spec)
    record = case['actual_legacy_issue']
    issue = Issue(record['issue_key'], record['scope_key'], record['reason'], record['evidence_token'],
                  json.loads(record['target_json']), json.loads(record['resolution_json']))
    with ready.catalog.transaction() as connection:
        ready.catalog.change_issues(connection, entry.spec.name, (issue,), {})
    options = PipelineOptions(maximum_passes=128, pass_seconds=240)
    clock = [int(time.time())]
    result = run_entry(ready, entry, NativeSources(ready.catalog.engine), options=options,
                       _policy=RetryPolicy(), clock=lambda: clock[0])
    assert result['complete'] and result['qualified'] is False
    assert result['input_failures'] == 0 and result['new_issues'] == 0
    assert result['unresolved_issues'] > 0 and result['source_scanned']
    assert_unchanged_wait(ready, entry, options, clock)


def test_interrupted_actual_failed_input_is_not_a_completed_blocked_source(ready, retained, monkeypatch):
    import app.data_store.pipeline as pipeline
    case = retained['cases']['E10']
    entry = BY_ID['E10']
    insert_originals(ready, case['base_observations'], case['base_states'])
    original = pipeline.normalize

    def interrupted(selected, source):
        for unit in original(selected, source):
            yield unit
            if unit.failure:
                raise DataStoreError('LOCAL_UPDATE_BUDGET_EXCEEDED')

    # Interrupt the real normalizer after one actual failed unit. Seeing that
    # failure is insufficient proof that the remaining source was acquired.
    monkeypatch.setattr(pipeline, 'normalize', interrupted)
    with pytest.raises(DataStoreError) as caught:
        run_entry(ready, entry, NativeSources(ready.catalog.engine),
                  options=PipelineOptions(maximum_passes=128, pass_seconds=240), _policy=RetryPolicy())
    assert caught.value.code == 'LOCAL_UPDATE_BUDGET_EXCEEDED'
    status = read_entry_status(ready, entry.id)
    assert not status.get('source_scanned')
    assert not status['refresh'].get('blocked_input_revision')


def test_global_commit_error_after_actual_acquisition_does_not_pin_source_version(ready, retained, monkeypatch):
    import app.data_store.pipeline as pipeline
    case = retained['cases']['E10']
    entry = BY_ID['E10']
    insert_originals(ready, case['base_observations'], case['base_states'])

    def unavailable(*args, **kwargs):
        raise DataStoreError('MEMORY_PRESSURE')

    # Acquisition uses real partial reports and their genuine failed scope.
    # Simulate only the commit's global failure after EOF, where those failed
    # units must not hide the actual memory problem behind an unchanged guard.
    monkeypatch.setattr(pipeline, '_commit_partition', unavailable)
    with pytest.raises(DataStoreError) as caught:
        run_entry(ready, entry, NativeSources(ready.catalog.engine),
                  options=PipelineOptions(maximum_passes=128, pass_seconds=240), _policy=RetryPolicy())
    assert caught.value.code == 'MEMORY_PRESSURE'
    status = read_entry_status(ready, entry.id)
    assert status.get('source_scanned') and status['input_failures'] > 0
    assert status['refresh']['continuation']['state'] == 'blocked_global_error'
    assert not status['refresh'].get('blocked_input_revision')


@pytest.mark.parametrize('changed_while_sealed', (False, True))
def test_real_sealed_disposal_binds_the_acquired_revision(ready, retained, monkeypatch,
                                                        changed_while_sealed):
    case = retained['cases']['E40']
    entry = BY_ID['E40']
    insert_originals(ready, case['base_observations'], case['base_states'])
    acquired = source_revision(NativeSources(ready.catalog.engine), entry)
    clock = [int(time.time())]
    options = PipelineOptions(maximum_passes=128, pass_seconds=240)
    first = run_entry(ready, entry, NativeSources(ready.catalog.engine),
                      options=replace(options, maximum_passes=1),
                      _policy=RetryPolicy(), clock=lambda: clock[0])
    assert first['source_scanned'] and first['source_rows'] > 0
    assert not first['complete'] and first['full_coverage']['remaining'] > 0
    if changed_while_sealed:
        # Add a second actual retained identity while the first acquisition is
        # sealed. Draining that seal cannot acknowledge this newer source.
        insert_originals(ready, case['later_observations'], [case['later_state']])
        assert source_revision(NativeSources(ready.catalog.engine), entry) != acquired

    def no_reacquisition(*args, **kwargs):
        raise AssertionError('sealed disposal must not reread provider history')

    clock[0] += 1801
    with monkeypatch.context() as patch:
        patch.setattr(NativeSources, 'iter_entry', no_reacquisition)
        drained = run_entry(ready, entry, NativeSources(ready.catalog.engine), options=options,
                            _policy=RetryPolicy(), clock=lambda: clock[0])
    assert drained['resumed'] and drained['source_rows'] == 0
    assert drained['complete'] and drained['qualified'] is False
    assert drained['refresh']['blocked_input_revision'] == acquired
    if changed_while_sealed:
        clock[0] += 1801
        later = run_entry(ready, entry, NativeSources(ready.catalog.engine), options=options,
                          _policy=RetryPolicy(), clock=lambda: clock[0])
        assert later['source_scanned'] and later['source_rows'] > 0
        assert later['complete'] and later['qualified'] is False
        assert later['refresh']['blocked_input_revision'] != acquired
    assert_unchanged_wait(ready, entry, options, clock)


def test_real_legacy_seal_drains_without_inventing_its_source_revision(ready, retained, monkeypatch):
    case = retained['cases']['E40']
    entry = BY_ID['E40']
    insert_originals(ready, case['base_observations'], case['base_states'])
    clock = [int(time.time())]
    options = PipelineOptions(maximum_passes=128, pass_seconds=240)
    # An acquisition without recurring admission has no captured head revision,
    # matching the durable metadata available in a pre-upgrade sealed spool.
    first = run_entry(ready, entry, NativeSources(ready.catalog.engine),
                      options=replace(options, maximum_passes=1), clock=lambda: clock[0])
    assert not first['complete'] and first['full_coverage']['remaining'] > 0

    def no_reacquisition(*args, **kwargs):
        raise AssertionError('legacy seal must be drained before new acquisition')

    clock[0] += 1801
    with monkeypatch.context() as patch:
        patch.setattr(NativeSources, 'iter_entry', no_reacquisition)
        drained = run_entry(ready, entry, NativeSources(ready.catalog.engine), options=options,
                            _policy=RetryPolicy(), clock=lambda: clock[0])
    assert drained['resumed'] and drained['source_rows'] == 0
    assert drained['complete'] and drained['qualified'] is False
    assert not drained['refresh'].get('blocked_input_revision')
    clock[0] += 1801
    baseline = run_entry(ready, entry, NativeSources(ready.catalog.engine), options=options,
                         _policy=RetryPolicy(), clock=lambda: clock[0])
    assert baseline['source_scanned'] and baseline['source_rows'] > 0
    assert baseline['complete'] and baseline['qualified'] is False
    assert_unchanged_wait(ready, entry, options, clock)
