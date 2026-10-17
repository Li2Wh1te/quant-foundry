"""Batch reuse must preserve source authority, validation and bounded memory."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
import os

import pytest
from sqlalchemy import text

from app.data_store import incremental
from app.data_store.adapters.canonical import NativeInputError
from app.data_store.adapters.contracts import digest, native_json
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.local_sources import NativeSources, SourceLimits, materialize_observation
from tests.test_data_store_incremental import (
    DAY, NOW, Observation, State, Import, Stage, EtfDailyBar,
    EtfDailyBarRevisionAudit, EtfAdjustmentFactor, publish,
)
from tests.test_data_store_kernel import database
from tests.test_data_store_domain_samples import sample
from app.data_store.change_capture import install
from app.data_store.tables import entry_status


postgres = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                              reason='isolated PostgreSQL required')


def test_batch_reuse_is_bounded_and_owns_immutable_record_copies():
    cache = incremental._BatchDependencies(12, max_rows=2)
    row = {'data_json': 'original'}
    cache.remember('first', 'v1', row, 6)
    row['data_json'] = 'consumer change'
    assert cache.rows['first'].row['data_json'] == 'original'
    with pytest.raises(TypeError):
        cache.rows['first'].row['data_json'] = 'another change'
    cache.remember('second', 'v2', {}, 6)
    cache.remember('third', 'v3', {}, 6)
    assert len(cache.rows) <= 2 and cache.bytes <= 12
    assert 'first' not in cache.rows
    # A newly oversized version cannot leave its old version eligible for reuse.
    cache.remember('second', 'changed', {}, 13)
    assert 'second' not in cache.rows and cache.bytes <= 12
    cache.clear()
    assert not cache.rows and cache.bytes == 0


@pytest.fixture
def observations(database):
    """Use the existing random test schema; never require a current-store mount."""
    engine, _ = database
    with engine.begin() as connection:
        for model in (Observation, State, Import, Stage, EtfDailyBar,
                      EtfDailyBarRevisionAudit, EtfAdjustmentFactor):
            model.__table__.create(connection)
        entry_status.create(connection)
        install(connection)
    return engine


def captured_batch(engine):
    body = deepcopy(sample('E50').content)
    point = body['item'][0]
    body['item'] = [dict(point, date_ms=DAY-(100-n)*86400000) for n in range(100)]
    for n in range(4):
        # Large stable point sets produce real delta dependencies. Sparse
        # receipts deliberately leave historical confirmation restrictions so
        # the comparison covers failures as well as successful returned values.
        current = dict(body, fixture_revision=n)
        requests = [{'key_receipt': 'actual_returned_keys_v1', 'parameters': {},
                     'returned_keys': {'date_ms': [body['item'][n]['date_ms']]}}]
        from datetime import timedelta
        publish(engine, body=current, requests=requests, now=NOW+timedelta(seconds=n))
    native = NativeSources(engine)
    entry = BY_ID['E50']
    job = incremental.claim(native, entry)
    pairs = [(job, row) for row in incremental.next_batch(native, entry, job)]
    assert len(pairs) == 4
    with engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM tonghuashun_observations '
                                       'WHERE base_observation_id IS NOT NULL')).scalar_one() >= 2
    return native, entry, pairs


def unit_signature(raws, entry):
    # This includes object keys, confirmation tokens/order, complete rows and
    # every failed unit; comparing only final row counts would be insufficient.
    units = [unit for raw in raws for unit in normalize(entry, raw)]
    return digest([asdict(unit) for unit in units]), Counter(unit.failure for unit in units)


@postgres
def test_batch_reuses_dependencies_but_preserves_complete_inputs_and_accounting(observations, monkeypatch):
    native, entry, pairs = captured_batch(observations)
    decodes = []
    original = incremental._json
    def counted(value, limit):
        decodes.append(value)
        return original(value, limit)
    monkeypatch.setattr(incremental, '_json', counted)
    baseline = []
    metrics = Counter()
    for job, observation in pairs:
        segment = incremental.RangeSources(native, entry, job, observation)
        baseline.extend(segment.iter_entry(entry))
        metrics.update(segment.metrics)
    old_decodes = len(decodes)
    decodes.clear()
    source = incremental.FundBatchSources(native, entry, pairs)
    actual = list(source.iter_entry(entry))
    assert actual == baseline
    assert source.metrics == dict(metrics)
    assert source.summary['complete'] and source.summary['source_rows'] == len(pairs)
    assert unit_signature(actual, entry) == unit_signature(baseline, entry)
    assert len(decodes) < old_decodes


@postgres
def test_repaired_tuple_between_original_snapshots_cannot_hide_hash_failure(observations):
    native, entry, pairs = captured_batch(observations)
    source = incremental.FundBatchSources(native, entry, pairs[1:])
    iterator = source.iter_entry(entry)
    next(iterator)
    # Keep the advertised content hash unchanged. Caching by UUID or hash alone
    # would incorrectly hide this corrupted ancestor in the next snapshot.
    with observations.begin() as connection:
        ancestor = connection.execute(text('SELECT id,data_json FROM tonghuashun_observations '
                                           'WHERE base_observation_id IS NULL')).mappings().one()
        from app.data_store.adapters.contracts import loads
        changed = dict(loads(ancestor['data_json']), tampered=True)
        connection.execute(text('UPDATE tonghuashun_observations SET data_json=:body WHERE id=:id'),
                           {'body': native_json(changed), 'id': ancestor['id']})
    with pytest.raises(NativeInputError) as error:
        next(iterator)
    assert error.value.code == 'SOURCE_HASH_MISMATCH'
    assert source.summary['source_rows'] == 1 and not source.summary['complete']


@postgres
def test_cached_record_does_not_bypass_payload_or_chain_budget(observations):
    native, entry, pairs = captured_batch(observations)
    cache = incremental._BatchDependencies(32*1024**2)
    with native._snapshot() as (connection, _):
        identity = pairs[-1][1]['id']
        row, _ = cache.load(connection, identity, native.limits, dependency=False)
        with pytest.raises(NativeInputError) as payload_error:
            cache.load(connection, identity, replace(native.limits, payload_bytes=1), dependency=True)
        assert payload_error.value.code == 'SOURCE_DEPENDENCY_MISSING'
        def lookup(dependency):
            return cache.load(connection, dependency, native.limits, dependency=True)[0]
        materialize_observation(row, lookup, native.limits)
        with pytest.raises(NativeInputError) as chain_error:
            materialize_observation(row, lookup, replace(native.limits, chain_bytes=1))
        assert chain_error.value.code == 'SOURCE_BUDGET_EXCEEDED'


@postgres
def test_batch_cache_is_released_after_early_close_and_not_reused_on_retry(observations, monkeypatch):
    native, entry, pairs = captured_batch(observations)
    created = []
    original = incremental._BatchDependencies
    def capture(*args, **kwargs):
        cache = original(*args, **kwargs)
        created.append(cache)
        return cache
    monkeypatch.setattr(incremental, '_BatchDependencies', capture)
    source = incremental.FundBatchSources(native, entry, pairs)
    iterator = source.iter_entry(entry)
    next(iterator)
    assert created[0].rows
    iterator.close()
    assert not created[0].rows and created[0].bytes == 0
    list(source.iter_entry(entry))
    assert len(created) == 2 and created[0] is not created[1]
    assert not created[1].rows and created[1].bytes == 0
