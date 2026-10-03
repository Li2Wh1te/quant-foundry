"""Atomic admission uses isolated PostgreSQL and real current-only Parquet."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import os
import threading

import pytest
from sqlalchemy import text

from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_incremental import ready, publish, update, NOW
from tests.test_data_store_domain_samples import sample
from tests.test_data_store_sealed_continuation import nav_body, pending_path, seal, continuation
from app.data_ingestion.tonghuashun.confirmation import returned_keys
from app.data_store import active_input, incremental
from app.data_store.adapters.registry import BY_ID
from app.data_store.errors import DataStoreError
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_local


pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PostgreSQL required')


def changed_body(entry, value):
    body = deepcopy(sample(entry).content)
    body['item'][0]['unit_nav' if entry == 'E44' else 'manager_return_pct'] = Decimal(value)
    return body


def captured(st, entry):
    body = sample(entry).content
    publish(st.catalog.engine, entry, requests=[{'parameters': {}, **returned_keys(body)}])
    update(st, entry)
    body = changed_body(entry, '2.2')
    publish(st.catalog.engine, entry, body=body, now=NOW + timedelta(hours=1),
            requests=[{'parameters': {}, **returned_keys(body)}])
    incremental.claim_funds(NativeSources(st.catalog.engine), BY_ID[entry], 1)
    return active_input.active_input_plan(st, BY_ID[entry], NativeSources(st.catalog.engine)).plan


def options(plan):
    return PipelineOptions(admit_active_only=True, maximum_claim_batches=1,
        expected_input_identity=plan['input_identity'], expected_source_selection=plan['source_selection'])


def queue(st):
    with st.catalog.transaction() as connection:
        return [dict(row) for row in connection.execute(text(
            'SELECT * FROM data_store_source_ranges ORDER BY source,dataset,subject,variant,range_key')).mappings()]


def forbidden(*args, **kwargs):
    raise AssertionError('approved admission must not claim, reseed or expand its batch')


@pytest.mark.parametrize('entry', ['E23', 'E44'])
def test_plan_is_read_only_and_once_only_admission_preserves_later_pending(ready, monkeypatch, entry):
    plan = captured(ready, entry)
    before = queue(ready)
    status = read_entry_status(ready, entry)
    assert active_input.active_input_plan(ready, BY_ID[entry], NativeSources(ready.catalog.engine)).plan == plan
    assert queue(ready) == before and read_entry_status(ready, entry) == status
    body = changed_body(entry, '3.3')
    publish(ready.catalog.engine, entry, body=body, now=NOW + timedelta(hours=2),
            requests=[{'parameters': {}, **returned_keys(body)}])
    late_queue = queue(ready)
    monkeypatch.setattr(incremental, 'claim_funds', forbidden)
    result = update(ready, entry, options=options(plan))
    assert result['active_input_admission']['batch_complete']
    assert result['active_input_admission']['scope_count'] == 1
    assert result['source_metrics']['payload_rows'] == 1
    assert result['source_metrics']['decode_calls'] == 1
    after = queue(ready)[0]
    assert after['pending'] and after['lower_at'] == late_queue[0]['lower_at']
    # The cursor advances only to the plan's original observation, not to the
    # producer's subsequent pending observation or latest source head.
    assert after['active']['after'] == {k: plan['scopes'][0]['observation'][k] for k in ('at', 'id')}
    assert after['active']['stop'] == before[0]['active']['stop']
    assert not result['complete']


@pytest.mark.parametrize('changed', ['identity', 'selection', 'cursor', 'observation', 'receipt'])
def test_stale_plan_refuses_before_refresh_catalog_or_scratch_mutation(ready, changed):
    plan = captured(ready, 'E44')
    control = options(plan)
    if changed == 'identity':
        control = replace(control, expected_input_identity='0' * 64)
    elif changed == 'selection':
        control = replace(control, expected_source_selection='0' * 64)
    else:
        with ready.catalog.engine.begin() as connection:
            if changed == 'cursor':
                connection.execute(text("UPDATE data_store_source_ranges SET active=jsonb_set(active,'{id}',to_jsonb('changed'::text))"))
            elif changed == 'observation':
                connection.execute(text("UPDATE tonghuashun_observations SET content_hash=:hash WHERE id=CAST(:id AS uuid)"),
                    {'hash': '1' * 64, 'id': plan['scopes'][0]['observation']['id']})
            else:
                connection.execute(text("UPDATE tonghuashun_observations SET request_json='[]' WHERE id=CAST(:id AS uuid)"),
                    {'id': plan['scopes'][0]['observation']['id']})
    before = queue(ready)
    status = read_entry_status(ready, 'E44')
    metadata = ready.catalog.dataset(BY_ID['E44'].spec.name)
    scratch = list(ready.files.root.glob('.scratch/**/*'))
    with pytest.raises(DataStoreError) as error:
        update(ready, 'E44', options=control)
    assert error.value.code == 'SOURCE_CONFLICT' and error.value.preserve_continuation
    assert queue(ready) == before and read_entry_status(ready, 'E44') == status
    assert ready.catalog.dataset(BY_ID['E44'].spec.name) == metadata
    assert list(ready.files.root.glob('.scratch/**/*')) == scratch


def test_fence_refusal_through_normal_scheduler_wrapper_keeps_persisted_status(ready):
    plan = captured(ready, 'E44')
    before = read_entry_status(ready, 'E44')
    result = run_local(ready, NativeSources(ready.catalog.engine), entries=[BY_ID['E44']],
        options=replace(options(plan), expected_source_selection='0' * 64))
    assert result[0]['reason'] == 'SOURCE_CONFLICT'
    assert not result[0]['complete'] and read_entry_status(ready, 'E44') == before


def test_oversized_scope_is_refused_instead_of_truncated(ready):
    captured(ready, 'E44')
    with ready.catalog.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO data_store_source_ranges(source,dataset,subject,variant,range_key,active) "
            "SELECT 'tonghuashun','fund_nav','oversized-'||n,'default','all','{}'::jsonb "
            "FROM generate_series(1,65) n"))
    with pytest.raises(DataStoreError) as error:
        active_input.active_input_plan(ready, BY_ID['E44'], NativeSources(ready.catalog.engine))
    assert error.value.code == 'ACTIVE_INPUT_REQUIRED'


def test_atomic_row_locks_keep_producer_append_outside_admitted_boundary(ready, monkeypatch):
    plan = captured(ready, 'E44')
    locked, entered, published = threading.Event(), threading.Event(), threading.Event()
    failures = []
    original = active_input._read_selection
    def inspect_lock(connection, entry, sources, *, lock):
        result = original(connection, entry, sources, lock=lock)
        if lock:
            locked.set()
            assert entered.wait(2)
            assert not published.wait(.1)
        return result
    def producer():
        try:
            assert locked.wait(2)
            entered.set()
            body = changed_body('E44', '3.3')
            publish(ready.catalog.engine, 'E44', body=body, now=NOW + timedelta(hours=2),
                    requests=[{'parameters': {}, **returned_keys(body)}])
            published.set()
        except Exception as error:
            failures.append(type(error).__name__)
    monkeypatch.setattr(active_input, '_read_selection', inspect_lock)
    worker = threading.Thread(target=producer)
    worker.start()
    try:
        admitted = active_input.active_input_plan(ready, BY_ID['E44'], NativeSources(ready.catalog.engine), options=options(plan))
    finally:
        worker.join(5)
    assert not worker.is_alive() and not failures and published.is_set()
    assert admitted.plan == plan
    assert queue(ready)[0]['pending'] and queue(ready)[0]['active']['after'] is None


def test_direct_incremental_call_cannot_skip_atomic_admission(ready):
    plan = captured(ready, 'E44')
    with pytest.raises(DataStoreError) as error:
        incremental.run(ready, BY_ID['E44'], NativeSources(ready.catalog.engine), options=options(plan))
    assert error.value.code == 'ACTIVE_INPUT_REQUIRED'


def test_new_acquisition_mode_cannot_replace_an_existing_ordinary_seal(ready):
    progress = seal(ready, 6)
    plan = active_input.active_input_plan(ready, BY_ID['E44'], NativeSources(ready.catalog.engine)).plan
    path = pending_path(ready)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    generation = ready.catalog.dataset(BY_ID['E44'].spec.name)['generation']
    with pytest.raises(DataStoreError) as error:
        update(ready, 'E44', options=options(plan))
    assert error.value.code == 'SOURCE_CONFLICT' and error.value.preserve_continuation
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert ready.catalog.dataset(BY_ID['E44'].spec.name)['generation'] == generation
    assert update(ready, 'E44', options=continuation(progress))['sealed_continuation']['complete']


def fixed_seal(st):
    captured(st, 'E44')
    update(st, 'E44')
    body = nav_body(6, invalid=True)
    publish(st.catalog.engine, 'E44', body=body, now=NOW + timedelta(hours=2),
            requests=[{'parameters': {}, **returned_keys(body)}])
    incremental.claim_funds(NativeSources(st.catalog.engine), BY_ID['E44'], 1)
    plan = active_input.active_input_plan(st, BY_ID['E44'], NativeSources(st.catalog.engine)).plan
    result = update(st, 'E44', options=replace(options(plan), maximum_partition_passes=1))
    assert not result['active_input_admission']['batch_complete']
    assert result['active_input_admission']['input_disposition']['input_failures'] == 1
    return plan


def test_fixed_seal_resumes_without_decode_and_preserves_original_failures(ready, monkeypatch):
    plan = fixed_seal(ready)
    monkeypatch.setattr(incremental.FundBatchSources, 'iter_entry', forbidden)
    result = update(ready, 'E44', options=options(plan))
    assert result['active_input_admission']['batch_complete']
    assert result['source_metrics']['decode_calls'] == result['input_failures'] == 0
    assert result['active_input_admission']['input_disposition']['input_failures'] == 1
    assert not result['qualified']


def test_changed_receipt_plan_cannot_reuse_previous_fixed_seal(ready):
    original = fixed_seal(ready)
    with ready.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE tonghuashun_observations SET request_json='[]' WHERE id=CAST(:id AS uuid)"),
            {'id': original['scopes'][0]['observation']['id']})
    fresh = active_input.active_input_plan(ready, BY_ID['E44'], NativeSources(ready.catalog.engine)).plan
    assert fresh['source_selection'] != original['source_selection']
    path = pending_path(ready)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    generation = ready.catalog.dataset(BY_ID['E44'].spec.name)['generation']
    with pytest.raises(DataStoreError) as error:
        update(ready, 'E44', options=options(fresh))
    assert error.value.code == 'SOURCE_CONFLICT' and error.value.preserve_continuation
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert ready.catalog.dataset(BY_ID['E44'].spec.name)['generation'] == generation
