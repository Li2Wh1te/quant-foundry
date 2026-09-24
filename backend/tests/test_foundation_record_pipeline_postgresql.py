"""Real transactional pipeline restart and authorization behavior on PostgreSQL."""
import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path
from contextlib import ExitStack
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, event, select, func, text
from sqlalchemy.orm import Session
from tests.test_foundation_publication_postgresql import pytestmark
from app.core.config import get_settings
from app.data_foundation.__main__ import local_execution
from app.data_foundation.execution import register_archive
from app.data_foundation.source_refs import register_baseline
from app.data_foundation.record_pipeline import register_job, advance_job
from app.data_foundation.batches import set_controls
from app.data_foundation.batch_models import BatchControl, BatchWork
from app.data_foundation.work_models import Work, Release, BlockRef
from app.data_foundation.canonical import FoundationError


@pytest.fixture(scope='module')
def pipeline_engine():
    """Committed restart tests own a disposable, fully migrated database.

    Immutable publication evidence cannot be rolled back after a worker uses
    another connection. Keeping it in the shared suite database would corrupt
    later tests' empty-database assumptions, even with separate semantic series.
    """
    name = 'qf_pipeline_' + uuid4().hex
    url = get_settings().database_url
    admin = create_engine(url)
    engine = create_engine(url.set(database=name))
    with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        connection.execute(text(f'CREATE DATABASE {name}'))
    try:
        environment = {**os.environ, 'QF_DATABASE_NAME': name}
        subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            check=True, capture_output=True, text=True)
        yield engine
    finally:
        engine.dispose()
        with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            connection.execute(text(f'DROP DATABASE {name}'))
        admin.dispose()


@pytest.fixture
def session(pipeline_engine):
    with Session(pipeline_engine) as session:
        yield session
        session.rollback()


@pytest.fixture(autouse=True)
def isolated_series(monkeypatch):
    # These tests intentionally commit across independent worker connections.
    # Give each test a real, separate semantic series so its published head
    # cannot contaminate tests that roll back the default series afterwards.
    import app.data_foundation.record_work as records
    original = records.series_for
    register = records.register_definition
    suffix = uuid4().hex
    monkeypatch.setattr(records, 'series_for', lambda dataset, source: original(dataset, source)+'-'+suffix)
    def scoped_definition(session, *, kind, name, version, definition):
        # A policy's immutable content includes its semantic series. Namespace
        # the test policy too, rather than mutating the default policy version.
        return register(session, kind=kind, name=name+'-'+suffix if kind == 'policy' else name,
                        version=version, definition=definition)
    monkeypatch.setattr(records, 'register_definition', scoped_definition)


def fixture(session, tmp_path, count=1, preserve_head=False):
    """Synthetic archive bytes test verification, not Docker restoration."""
    image = 'sha256:' + uuid4().hex + uuid4().hex
    execution = local_execution(session, 'a'*40, image)
    path = tmp_path / (image.removeprefix('sha256:') + '.tar')
    path.write_bytes(b'isolated pipeline verification fixture')
    evidence = dict(image_digest=image, archive_key=path.name,
        archive_hash=hashlib.sha256(path.read_bytes()).hexdigest(), byte_count=path.stat().st_size,
        verification=dict(restored_image_digest=image, network='none', python=platform.python_version(),
            lock_hash=hashlib.sha256((Path(__file__).resolve().parents[1]/'uv.lock').read_bytes()).hexdigest()))
    register_archive(session, execution.id, evidence, tmp_path)
    key = uuid4().hex
    source = register_baseline(session, source='tushare', dataset='etf_directory', scope={},
        rows=[dict(ts_code=f'{key}-{index}', csname='Pipeline fixture') for index in range(count)],
        observed_at=datetime.now(timezone.utc), decoder_id=execution.id, event_key=key)
    batch = register_job(session, source_ref_id=source.id, execution_id=execution.id,
        runtime_digest=image, archive_root=tmp_path, preserve_head=preserve_head)
    ids = batch.id, source.id, execution.id
    session.commit()
    return ids, image


def test_bounded_restart_requires_explicit_publication_then_is_idempotent(session, tmp_path):
    (batch, source, execution), image = fixture(session, tmp_path, 201)
    options = dict(runtime_digest=image, archive_root=tmp_path)
    first = advance_job(session.bind, batch, steps=1, **options)
    assert first['steps'] == 1 and first['status'] == 'step_ready'
    # Each invocation uses fresh connections; no process-local cursor is kept.
    for _ in range(20):
        staged = advance_job(session.bind, batch, steps=1, **options)
        if staged['status'] == 'awaiting_publication':
            break
    assert staged['status'] == 'awaiting_publication'
    session.expire_all()
    assert not session.get(BatchControl, batch).allow_publish
    works = select(BatchWork.work_id).where(BatchWork.batch_id == batch)
    assert session.scalar(select(func.count()).select_from(Release).where(
        Release.work_id.in_(works), Release.status == 'published')) == 0
    session.rollback()
    result = advance_job(session.bind, batch, steps=10, publish=True, **options)
    assert result['status'] == 'published'
    again = advance_job(session.bind, batch, steps=10, publish=True, **options)
    assert again['release_id'] == result['release_id'] and again['steps'] == 0
    assert register_job(session, source_ref_id=source, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path).id == batch


def test_child_release_loads_inherited_block_hashes_in_bounded_queries(session, tmp_path):
    # A growing scope has many unchanged partitions. Publishing one new key
    # must validate the same inherited manifest without fetching every parent
    # block in a separate database round trip.
    (first_batch, _, _), first_image = fixture(session, tmp_path, 80)
    first = advance_job(session.bind, first_batch, runtime_digest=first_image,
                        archive_root=tmp_path, steps=10, publish=True)
    assert first['status'] == 'published'
    partitions = session.scalar(select(func.count()).select_from(BlockRef)
        .where(BlockRef.release_id == first['release_id']))
    assert partitions >= 25

    (child_batch, _, _), child_image = fixture(session, tmp_path)
    block_reads = []
    reconciliation_rows = []
    def count_block_reads(connection, cursor, statement, parameters, context, executemany):
        if 'FROM foundation_release_blocks' in statement:
            block_reads.append(statement)
    def count_reconciliation_rows(connection, cursor, statement, parameters, context, executemany):
        if 'SELECT foundation_record_block_members.decision_id' in statement:
            reconciliation_rows.append(cursor.rowcount)
    event.listen(session.bind, 'before_cursor_execute', count_block_reads)
    event.listen(session.bind, 'after_cursor_execute', count_reconciliation_rows)
    try:
        child = advance_job(session.bind, child_batch, runtime_digest=child_image,
                            archive_root=tmp_path, steps=10, publish=True)
    finally:
        event.remove(session.bind, 'before_cursor_execute', count_block_reads)
        event.remove(session.bind, 'after_cursor_execute', count_reconciliation_rows)
    assert child['status'] == 'published'
    assert len(block_reads) <= 10
    # Reconciliation must transfer only this child's decision, even though the
    # published release inherits many complete parent blocks.
    assert reconciliation_rows and all(0 <= count <= 1 for count in reconciliation_rows)


def test_repeated_registration_preserves_pause_and_missing_archive_never_advances(session, tmp_path):
    (batch, source, execution), image = fixture(session, tmp_path)
    set_controls(session, batch, pause_a=True, pause_b=True)
    session.commit()
    register_job(session, source_ref_id=source, execution_id=execution, runtime_digest=image, archive_root=tmp_path)
    session.commit()
    assert advance_job(session.bind, batch, runtime_digest=image, archive_root=tmp_path)['status'] == 'paused'
    set_controls(session, batch, pause_a=False, pause_b=False)
    session.commit()
    with pytest.raises(FoundationError, match='不一致'):
        advance_job(session.bind, batch, runtime_digest='sha256:'+'0'*64, archive_root=tmp_path)
    (tmp_path / (image.removeprefix('sha256:')+'.tar')).unlink()
    with pytest.raises(FoundationError, match='归档'):
        advance_job(session.bind, batch, runtime_digest=image, archive_root=tmp_path)
    with Session(session.bind) as check:
        work = check.scalar(select(Work).join(BatchWork, BatchWork.work_id == Work.id).where(BatchWork.batch_id == batch))
        assert work.cursor == 0 and work.status == 'queued'


def test_global_worker_contention_yields_without_skipping_source(session, tmp_path):
    from app.data_foundation.worker import SINGLETON_KEY
    (batch, _, _), image = fixture(session, tmp_path)
    with session.bind.connect().execution_options(isolation_level='AUTOCOMMIT') as owner:
        owner.execute(text('SELECT pg_advisory_lock(:key)'), {'key': SINGLETON_KEY})
        try:
            result = advance_job(session.bind, batch, runtime_digest=image, archive_root=tmp_path, steps=5)
            assert result['status'] == 'busy' and result['steps'] == 0
        finally:
            owner.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': SINGLETON_KEY})
    result = advance_job(session.bind, batch, runtime_digest=image, archive_root=tmp_path, steps=10, publish=True)
    assert result['status'] == 'published'


def test_scoped_worker_slots_and_scope_exclusion_are_database_enforced(session, tmp_path):
    from app.data_foundation.worker import (
        SCOPED_SLOT_KEYS, acquire_gate, acquire_scoped_slot, scoped_gate_key)
    (batch, _, _), image = fixture(session, tmp_path)
    origin = session.scalar(select(Work).join(BatchWork, BatchWork.work_id == Work.id)
        .where(BatchWork.batch_id == batch, Work.kind == 'A'))
    scope_gate = scoped_gate_key(origin.scope_key)
    with ExitStack() as stack:
        owners = []
        for _ in SCOPED_SLOT_KEYS:
            owner = stack.enter_context(session.bind.connect().execution_options(isolation_level='AUTOCOMMIT'))
            stack.callback(lambda connection=owner: connection.execute(text('SELECT pg_advisory_unlock_all()')))
            assert acquire_gate(owner, shared=True)
            slot = acquire_scoped_slot(owner)
            assert slot is not None
            owners.append((owner, slot))
        assert {slot for _, slot in owners} == set(SCOPED_SLOT_KEYS)
        assert acquire_gate(owners[0][0]) is False
        extra = stack.enter_context(session.bind.connect().execution_options(isolation_level='AUTOCOMMIT'))
        stack.callback(lambda: extra.execute(text('SELECT pg_advisory_unlock_all()')))
        assert acquire_gate(extra, shared=True)
        assert acquire_scoped_slot(extra) is None
        assert owners[0][0].scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': scope_gate})
        # A different business scope is independently eligible to run.
        other_scope_gate = scoped_gate_key(origin.scope_key + ':independent')
        assert other_scope_gate != scope_gate
        assert extra.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': other_scope_gate})
        # Releasing one slot leaves the target scope protected by the other
        # connection, so the work cannot be claimed or partly advanced.
        owners[0][0].execute(text('SELECT pg_advisory_unlock(:key)'), {'key': owners[0][1]})
        result = advance_job(session.bind, batch, runtime_digest=image,
            archive_root=tmp_path, steps=1, publish=True)
        assert result['status'] == 'busy' and result['steps'] == 0
        session.expire_all()
        assert session.get(Work, origin.id).status == 'queued'
    assert advance_job(session.bind, batch, runtime_digest=image,
        archive_root=tmp_path, steps=10, publish=True)['status'] == 'published'


def test_periodic_update_can_wait_for_a_backfill_boundary(session, tmp_path):
    from threading import Event, Thread
    from app.data_foundation.worker import SINGLETON_KEY
    (batch, _, _), image = fixture(session, tmp_path)
    acquired = Event()
    def backfill_unit():
        with session.bind.connect().execution_options(isolation_level='AUTOCOMMIT') as owner:
            owner.execute(text('SELECT pg_advisory_lock(:key)'), {'key': SINGLETON_KEY})
            acquired.set()
            # Simulate an in-flight unit, not a permanent fixture lock.
            Event().wait(0.5)
            owner.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': SINGLETON_KEY})
    thread = Thread(target=backfill_unit)
    thread.start()
    try:
        assert acquired.wait(5)
        result = advance_job(session.bind, batch, runtime_digest=image, archive_root=tmp_path,
                             steps=1, lock_wait_seconds=2)
        assert result['steps'] == 1 and result['status'] == 'step_ready'
    finally:
        thread.join(5)


def test_gate_timeout_is_bounded_and_restores_connection_settings(session):
    from time import monotonic
    from app.data_foundation.worker import SINGLETON_KEY, acquire_gate
    with session.bind.connect().execution_options(isolation_level='AUTOCOMMIT') as owner:
        owner.execute(text('SELECT pg_advisory_lock(:key)'), {'key': SINGLETON_KEY})
        try:
            with session.bind.connect().execution_options(isolation_level='AUTOCOMMIT') as waiter:
                waiter.execute(text("SET lock_timeout='7s'"))
                started = monotonic()
                assert acquire_gate(waiter, 1) is False
                assert monotonic()-started < 4
                assert waiter.scalar(text('SHOW lock_timeout')) == '7s'
                assert waiter.scalar(text('SELECT 1')) == 1
                with pytest.raises(ValueError):
                    acquire_gate(waiter, True)
                waiter.execute(text("SET lock_timeout='0'"))
        finally:
            owner.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': SINGLETON_KEY})


def test_terminal_failure_is_reported_without_implicit_retry(session, tmp_path):
    from app.data_foundation.work import claim, finish_batch
    (batch, _, _), image = fixture(session, tmp_path)
    origin = session.scalar(select(Work).join(BatchWork, BatchWork.work_id == Work.id)
        .where(BatchWork.batch_id == batch, Work.kind == 'A'))
    leased = claim(session, work_id=origin.id)
    finish_batch(session, leased, status='failed')
    session.commit()
    result = advance_job(session.bind, batch, runtime_digest=image, archive_root=tmp_path, publish=True)
    assert result['status'] == 'failed' and result['steps'] == 0
    session.expire_all()
    assert session.get(Work, origin.id).lease_epoch == leased.lease_epoch
    assert session.scalar(select(func.count()).select_from(BatchWork).where(BatchWork.batch_id == batch)) == 1


def test_historical_job_mode_survives_restart_without_creating_a_head(session, tmp_path):
    from app.data_foundation.work_models import Head
    (batch, source, execution), image = fixture(session, tmp_path, preserve_head=True)
    result = advance_job(session.bind, batch, runtime_digest=image, archive_root=tmp_path, steps=10, publish=True)
    assert result['status'] == 'published' and result['head_activated'] is False
    session.expire_all()
    release = session.get(Release, result['release_id'])
    assert session.get(Head, release.scope_key) is None
    assert register_job(session, source_ref_id=source, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, preserve_head=True).id == batch


def test_source_changes_during_staging_cannot_activate_old_observation(session, tmp_path):
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation, TonghuashunCollectionState as State
    from app.data_ingestion.tonghuashun.contracts import exact_json, content_hash
    from app.data_foundation.source_refs import register_observation
    from app.data_foundation.work_models import Head
    (_, _, execution), image = fixture(session, tmp_path)
    subject = 'update-'+uuid4().hex
    def observed(name):
        payload = {'item': [{'manager_id': subject, 'manager_name': name}]}
        row = Observation(id=uuid4(), dataset='fund_manager', subject=subject, variant='default',
            observed_at=datetime.now(timezone.utc), request_json='{}', data_json=exact_json(payload),
            content_hash=content_hash(payload), row_count=1, chain_depth=0)
        session.add(row); session.flush()
        return row, register_observation(session, row.id, execution)
    old, old_ref = observed('Old')
    state = State(dataset='fund_manager', subject=subject, variant='default', observation_id=old.id,
        revision=1, status='ready', attempted_at=datetime.now(timezone.utc))
    session.add(state); session.flush()
    job = register_job(session, source_ref_id=old_ref.id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, source_revision=1)
    job_id, source_id = job.id, old_ref.id
    session.commit()
    options = dict(runtime_digest=image, archive_root=tmp_path, steps=10)
    assert advance_job(session.bind, job_id, **options)['status'] == 'awaiting_publication'
    newer, new_ref = observed('New')
    state.observation_id, state.revision = newer.id, 2
    session.commit()
    assert advance_job(session.bind, job_id, publish=True, **options)['status'] == 'superseded'
    history = register_job(session, source_ref_id=source_id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, preserve_head=True)
    history_id = history.id
    current = register_job(session, source_ref_id=new_ref.id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, source_revision=2)
    current_id = current.id
    session.commit()
    historical_result = advance_job(session.bind, history_id, publish=True, **options)
    assert historical_result['status'] == 'published' and not historical_result['head_activated']
    current_result = advance_job(session.bind, current_id, publish=True, **options)
    assert current_result['status'] == 'published' and current_result['head_activated']
    session.expire_all()
    release = session.get(Release, current_result['release_id'])
    assert session.get(Head, release.scope_key).release_id == release.id


def test_changed_head_replans_without_mutating_or_discarding_old_work(session, tmp_path):
    (first, _, _), image = fixture(session, tmp_path)
    options = dict(runtime_digest=image, archive_root=tmp_path, steps=10)
    assert advance_job(session.bind, first, **options)['status'] == 'awaiting_publication'
    (second, _, _), second_image = fixture(session, tmp_path)
    assert advance_job(session.bind, second, runtime_digest=second_image, archive_root=tmp_path,
        steps=10, publish=True)['status'] == 'published'
    # The first call observes the stale parent at the actual publication gate;
    # its bounded follow-up rebuilds governance against the new fixed parent.
    result = advance_job(session.bind, first, publish=True, **options)
    assert result['status'] == 'published'
    with Session(session.bind) as check:
        states = list(check.scalars(select(Work.status).join(BatchWork, BatchWork.work_id == Work.id)
            .where(BatchWork.batch_id == first, Work.kind == 'B')))
        assert sorted(states) == ['succeeded', 'superseded']
