"""Real isolated PostgreSQL/Parquet recovery of exact sealed fund membership."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_incremental import ready, publish, update, NOW
from tests.test_data_store_domain_samples import sample
from tests.test_data_store_sealed_continuation import nav_body, pending_path, continuation, no_new_claim
from app.data_ingestion.tonghuashun.confirmation import returned_keys
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_store import active_input, incremental, pipeline
from app.data_store.adapters.registry import BY_ID
from app.data_store.errors import DataStoreError
from app.data_store.local_sources import NativeSources
from app.data_store.merge import MergeSpool
from app.data_store.pipeline import PipelineOptions, read_entry_status, run_local


pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PostgreSQL required')


def body_for(entry, subject, months=6):
    if entry == 'E44':
        return nav_body(months, subject=subject)
    manager, period = subject.rsplit('.', 1)
    body = deepcopy(sample('E23').content)
    body['collection_scope'] = {'manager_id': manager, 'range': period}
    point = body['item'][0]
    # The real provider's millisecond encoding is a Shanghai midnight, not a
    # UTC timestamp boundary. Keep that source contract in synthetic fixtures.
    body['item'] = [dict(point, date_ms=int(datetime(1999 + i // 12, i % 12 + 1, 2,
                            tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)) for i in range(months)]
    return body


def queue(st):
    with st.catalog.transaction() as c:
        return [row[0] for row in c.execute(text(
            'SELECT to_jsonb(r) FROM data_store_source_ranges r ORDER BY source,dataset,subject,variant,range_key'))]


def blocked(st, entry, subjects, *, old_observation=False):
    """Create real failed claims before the original finite batch is captured."""
    for subject in subjects:
        if old_observation:
            body = body_for(entry, subject, 2)
            publish(st.catalog.engine, entry, subject=subject, body=body,
                    requests=[{'parameters': {}, **returned_keys(body)}])
        with Session(st.catalog.engine) as session, session.begin():
            repo = CollectionRepository(session)
            previous = repo.read(BY_ID[entry].native, subject, 'default', with_data=False)
            repo.fail(BY_ID[entry].native, subject, 'default', expected=previous.revision,
                      kind='temporary', now=NOW + timedelta(seconds=1))
    sources = NativeSources(st.catalog.engine)
    jobs = incremental.claim_funds(sources, BY_ID[entry], len(subjects))
    assert len(jobs) == len(subjects)
    for job in jobs:
        assert not incremental.check_state(sources, BY_ID[entry], job)
    # Bootstrap the ordinary entry contract without decoding or clearing the
    # failed claims; this also permits real active-input planning below.
    update(st, entry, options=PipelineOptions(maximum_claim_batches=1))


def capture(st, entry, subjects, *, months=6, legacy=False):
    for subject in subjects:
        body = body_for(entry, subject, months)
        publish(st.catalog.engine, entry, subject=subject, body=body,
                requests=[{'parameters': {}, **returned_keys(body)}])
    sources = NativeSources(st.catalog.engine)
    if legacy:
        incremental.claim_funds(sources, BY_ID[entry], len(subjects))
        plan = active_input.active_input_plan(st, BY_ID[entry], sources).plan
        control = PipelineOptions(admit_active_only=True, maximum_claim_batches=1,
            maximum_partition_passes=4096, expected_input_identity=plan['input_identity'],
            expected_source_selection=plan['source_selection'])
    else:
        control = PipelineOptions(maximum_claim_batches=1, maximum_partition_passes=4096)
    committed = pipeline._commit_partition
    cancelled = [False]
    def cancel_after_commit(*args, **kwargs):
        result = committed(*args, **kwargs)
        cancelled[0] = True
        return result
    # The actual production seal was retained by cancellation, including its
    # existing active-observation checkpoint. Reproduce that exit through the
    # real merger rather than fabricating its IDs or SQLite partition cursor.
    with patch.object(pipeline, '_commit_partition', cancel_after_commit):
        try:
            result = update(st, entry, options=control, cancelled=lambda: cancelled[0])
        except DataStoreError as caught:
            assert caught.code == 'OPERATION_CANCELLED'
        else:
            pytest.fail(json.dumps({'unexpected_return': result, 'queue': queue(st)}, default=str))
    path = pending_path(st)
    with st.budget.reserve('read', pending='pipeline.' + entry) as space:
        progress = MergeSpool.read_sealed_progress(space)
    assert len(progress['source_members']) == len(subjects)
    if legacy:
        # Model the existing admitted pre-upgrade format by removing only the
        # newly introduced field. All member IDs and both fences were produced
        # by the actual planner/pipeline, never assembled as a synthetic list.
        progress.pop('source_members')
        with sqlite3.connect(path) as db:
            db.execute('UPDATE progress SET body=? WHERE id=1', (json.dumps(progress),))
    return progress


def recover(st, entry, subjects):
    for subject in subjects:
        body = body_for(entry, subject, 3)
        publish(st.catalog.engine, entry, subject=subject, body=body,
                now=NOW + timedelta(hours=2), requests=[{'parameters': {}, **returned_keys(body)}])


def no_expansion(monkeypatch):
    monkeypatch.setattr(incremental, 'existing_claims', no_new_claim)
    monkeypatch.setattr(incremental, 'claim_funds', no_new_claim)
    monkeypatch.setattr(incremental.FundBatchSources, 'iter_entry', no_new_claim)


@pytest.mark.parametrize('entry', ['E23', 'E44'])
@pytest.mark.parametrize('old_observation', [False, True])
@pytest.mark.parametrize('legacy', [False, True])
def test_newly_eligible_active_claims_stay_outside_original_seal(ready, monkeypatch, entry, old_observation, legacy):
    extra = ['RECOVERED.month'] if entry == 'E23' else ['RECOVERED.FUND']
    original = ['ORIGINAL.month'] if entry == 'E23' else ['ORIGINAL.FUND']
    blocked(ready, entry, extra, old_observation=old_observation)
    progress = capture(ready, entry, original, legacy=legacy)
    recover(ready, entry, extra)
    before = [row for row in queue(ready) if row['subject'] in extra]
    assert before[0]['active']['blocked']
    assert (before[0]['active']['stop'] is not None) == old_observation
    no_expansion(monkeypatch)
    result = update(ready, entry, options=continuation(progress))
    assert result['sealed_continuation']['complete']
    assert result['source_metrics']['payload_rows'] == result['source_metrics']['decode_calls'] == 0
    assert [row for row in queue(ready) if row['subject'] in extra] == before
    assert 'pipeline.' + entry not in ready.budget.pending_keys()


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('changed', ['claim_id', 'stop', 'observation', 'missing_member'])
def test_changed_or_missing_original_member_refuses_before_refresh(ready, monkeypatch, legacy, changed):
    blocked(ready, 'E44', ['RECOVERED.FUND'])
    progress = capture(ready, 'E44', ['ORIGINAL.FUND'], legacy=legacy)
    with ready.catalog.engine.begin() as c:
        if changed == 'missing_member':
            c.execute(text("DELETE FROM data_store_source_ranges WHERE subject='ORIGINAL.FUND'"))
        elif changed == 'observation':
            identity = read_entry_status(ready, 'E44')['incremental']['active_observation']['batch'][0]['id']
            c.execute(text("UPDATE tonghuashun_observations SET content_hash=:hash WHERE id=CAST(:id AS uuid)"),
                      {'hash': '0' * 64, 'id': identity})
        elif changed == 'claim_id':
            c.execute(text("UPDATE data_store_source_ranges SET active=jsonb_set(active,'{id}',to_jsonb('changed'::text)) WHERE subject='ORIGINAL.FUND'"))
        else:
            # Widening the stop still selects the same first observation. The
            # complete member descriptor/old active-input hash must refuse it.
            c.execute(text("UPDATE data_store_source_ranges SET active=jsonb_set(active,'{stop,at}',to_jsonb('2030-01-01 00:00:00+00'::text)) WHERE subject='ORIGINAL.FUND'"))
    before = queue(ready)
    status = read_entry_status(ready, 'E44')
    spool_before = pending_path(ready).read_bytes()
    no_expansion(monkeypatch)
    with pytest.raises(DataStoreError) as caught:
        update(ready, 'E44', options=continuation(progress))
    assert caught.value.code == 'SOURCE_CONFLICT' and caught.value.preserve_continuation
    assert queue(ready) == before and read_entry_status(ready, 'E44') == status
    assert pending_path(ready).read_bytes() == spool_before
    with ready.budget.reserve('read', pending='pipeline.E44') as space:
        assert MergeSpool.read_sealed_progress(space) == progress


def test_real_59_member_legacy_batch_resumes_only_remaining_118_with_five_untouched(ready, monkeypatch):
    extra = ['RECOVERED.' + period for period in ('month', 'now', 'nowyear', 'tmonth', 'year')]
    blocked(ready, 'E23', extra)
    # Synthetic identities deliberately share one calendar bucket so119 real
    # monthly partitions give an exact118 suffix after the first real commit.
    # This is fixture construction, never a production identity/fence search.
    entry = BY_ID['E23']
    representation = sample('E23').representation_key
    original = []
    for i in range(4096):
        subject = f'ORIGINAL{i:04}.month'
        if entry.spec.partitioner((representation, subject, '1999-01-02', 'root')).endswith('.b00'):
            original.append(subject)
        if len(original) == 59:
            break
    assert len(original) == 59
    progress = capture(ready, 'E23', original, months=119, legacy=True)
    with sqlite3.connect(pending_path(ready)) as db:
        assert db.execute('SELECT count(*) FROM partitions WHERE p>?', (progress['after'],)).fetchone()[0] == 118
    recover(ready, 'E23', extra)
    before = [row for row in queue(ready) if row['subject'] in extra]
    original_before = {row['subject']: row for row in queue(ready) if row['subject'] in original}
    no_expansion(monkeypatch)
    result = update(ready, 'E23', options=continuation(progress, partitions=128))
    assert result['sealed_continuation']['complete'] and result['committed_partitions'] == 118
    assert result['sealed_continuation']['source_rows'] == 59
    assert result['sealed_continuation']['input_failures'] == 0
    assert result['source_metrics']['payload_rows'] == result['source_metrics']['decode_calls'] == 0
    assert [row for row in queue(ready) if row['subject'] in extra] == before
    for row in queue(ready):
        if row['subject'] in original:
            assert row['active']['id'] == original_before[row['subject']]['active']['id']
            assert row['active']['after'] == original_before[row['subject']]['active']['stop']
    assert 'pipeline.E23' not in ready.budget.pending_keys()
    assert not list(ready.files.root.glob('.scratch/*/spill/*.sqlite'))


@pytest.mark.parametrize('failure', ['cancel', 'unknown_commit'])
def test_failed_continuation_keeps_original_checkpoint_and_releases_resources(ready, monkeypatch, failure):
    blocked(ready, 'E44', ['RECOVERED.FUND'])
    progress = capture(ready, 'E44', ['ORIGINAL.FUND'], legacy=True)
    recover(ready, 'E44', ['RECOVERED.FUND'])
    before = queue(ready)
    original = pipeline._commit_partition
    cancelled = [False]
    def stop(*args, **kwargs):
        if failure == 'unknown_commit':
            raise DataStoreError('COMMIT_UNKNOWN')
        result = original(*args, **kwargs)
        cancelled[0] = True
        return result
    monkeypatch.setattr(pipeline, '_commit_partition', stop)
    no_expansion(monkeypatch)
    # The first real cancellation correctly installed ordinary retry backoff.
    # Advance only this synthetic scheduler clock past it; do not alter the
    # checkpoint or falsely treat a skipped invocation as a continuation.
    resume_at = read_entry_status(ready, 'E44')['refresh']['next_retry_at'] + 1
    with pytest.raises(DataStoreError) as caught:
        run_local(ready, NativeSources(ready.catalog.engine), entries=[BY_ID['E44']],
                  options=continuation(progress), cancelled=lambda: cancelled[0], clock=lambda: resume_at)
    assert caught.value.code == ('OPERATION_CANCELLED' if failure == 'cancel' else 'COMMIT_UNKNOWN')
    assert queue(ready) == before
    with ready.budget.reserve('read', pending='pipeline.E44') as space:
        checkpoint = MergeSpool.read_sealed_progress(space)
    assert checkpoint['identity'] == progress['identity']
    assert checkpoint['source_selection'] == progress['source_selection']
    assert (checkpoint['after'] > progress['after']) == (failure == 'cancel')
    # A second normal entry invocation reacquires its pipeline and quota. The
    # uncertain-commit case remains stopped; cancellation can resume in a test.
    if failure == 'cancel':
        monkeypatch.setattr(pipeline, '_commit_partition', original)
        assert update(ready, 'E44', options=continuation(progress))['sealed_continuation']['complete']


def test_old_checkpoint_without_member_proof_is_refused(ready):
    blocked(ready, 'E44', ['RECOVERED.FUND'])
    progress = capture(ready, 'E44', ['ORIGINAL.FUND'], legacy=True)
    progress.pop('active_input_fence')
    with sqlite3.connect(pending_path(ready)) as db:
        db.execute('UPDATE progress SET body=? WHERE id=1', (json.dumps(progress),))
    with pytest.raises(DataStoreError) as caught:
        update(ready, 'E44', options=continuation(progress))
    assert caught.value.code == 'SEALED_CONTINUATION_REQUIRED'
