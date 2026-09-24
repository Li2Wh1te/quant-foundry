"""Prepared input preserves original hashes, full-source quality and fencing."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from uuid import uuid4
import json

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session, fixture, isolated_series
from app.data_foundation import record_work as records
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.record_preparation import prepare_step
from app.data_foundation.performance_models import RecordPreparation, RecordPreparedPage
from app.data_foundation.record_models import CandidateRecord
from app.data_foundation.work_models import Work, Candidate, Assessment
from app.data_foundation.batch_models import BatchWork
from app.data_foundation.work import claim, heartbeat, finish_batch, cancel


def work_for(session, batch):
    return session.scalar(select(Work).join(BatchWork, BatchWork.work_id == Work.id)
        .where(BatchWork.batch_id == batch, Work.kind == 'A'))


def test_two_thousand_rows_convert_once_and_cross_page_duplicates_keep_exact_hashes(session, tmp_path, monkeypatch):
    from tests.test_foundation_sources import execution
    from app.data_foundation.source_refs import register_baseline
    ex = execution(session)
    raw = [dict(ts_code=f'P{i}', csname=f'Row {i}') for i in range(2000)]
    raw[1850] = dict(raw[150], csname='Same identity on a different page')
    raw[1999] = {}
    source = register_baseline(session, source='tushare', dataset='etf_directory', scope={}, rows=raw,
        observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key=uuid4().hex)
    work, _ = records.create_normalization(session, source_ref_id=source.id, execution_id=ex.id)
    original, reader = records.convert, records.read_source
    converted, reads = [], []
    def convert(src, item):
        converted.append(item)
        return original(src, item)
    def read(*args):
        reads.append(1)
        return reader(*args)
    monkeypatch.setattr(records, 'convert', convert)
    monkeypatch.setattr(records, 'read_source', read)
    for _ in range(10):
        owned = claim(session, work_id=work.id)
        records.normalize_batch(session, work.id, owned.lease_epoch)
    assert work.status == 'succeeded' and work.cursor == 2000
    assert len(converted) == 2000 and len(reads) == 1
    assert session.scalar(select(func.count()).select_from(RecordPreparedPage).where(
        RecordPreparedPage.work_id == work.id)) == 10
    candidates = list(session.scalars(select(Candidate).where(Candidate.work_id == work.id).order_by(Candidate.occurrence)))
    assert Counter(c.readiness for c in candidates) == {'ready': 1997, 'quarantined': 3}
    for index in (0, 150, 200, 1850, 1999):
        candidate = candidates[index]
        assessment = session.get(Assessment, candidate.assessment_id)
        assert assessment.input_hash == digest('typed-record-input', [work.fingerprint, index, raw[index]])
        if index != 1999:
            typed = session.get(CandidateRecord, candidate.id)
            expected = original(source, raw[index])
            assert typed.body_json == encode(expected['body'])
            assert typed.field_quality_json == encode(expected['field_quality'])
            assert typed.business_key == records.record_key(source, expected)
            assert candidate.values_hash == records.value_hash(typed)
        else:
            assert candidate.values_hash == digest('quarantined-record', raw[index])
        if index in (150, 1850):
            assert json.loads(assessment.results_json)['reason'] == 'DUPLICATE_BUSINESS_KEY'
    for sql in (
        'UPDATE foundation_record_prepared_pages SET payload_json=\'[]\' WHERE work_id=:id',
        'DELETE FROM foundation_record_prepared_pages WHERE work_id=:id',
        'UPDATE foundation_record_preparations SET sealed=false WHERE work_id=:id',
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(sql), {'id': work.id})


def test_committed_preparation_pages_survive_crash_without_candidate_progress(session, tmp_path, monkeypatch):
    (batch, _, _), _ = fixture(session, tmp_path, 401)
    work = work_for(session, batch)
    owned = claim(session, work_id=work.id)
    wid, epoch = owned.id, owned.lease_epoch
    session.commit()
    original = records.convert
    calls = Counter()
    def interrupted(source, raw):
        index = int(raw['ts_code'].rsplit('-', 1)[1])
        calls[index] += 1
        if index == 210:
            raise RuntimeError('simulated process exit before page commit')
        # An independent heartbeat must be able to lock Work while conversion
        # is running; this would timeout under the previous giant transaction.
        if index in (0, 200):
            with Session(session.bind) as pulse, pulse.begin():
                pulse.execute(text("SET LOCAL lock_timeout='200ms'"))
                heartbeat(pulse, wid, epoch)
        return original(source, raw)
    monkeypatch.setattr(records, 'convert', interrupted)
    with pytest.raises(RuntimeError, match='simulated process exit'):
        prepare_step(session.bind, wid, epoch, stop=Event(), budget_seconds=60)
    with Session(session.bind) as check:
        assert check.scalar(select(func.count()).select_from(RecordPreparedPage).where(
            RecordPreparedPage.work_id == wid)) == 1
        assert not check.get(RecordPreparation, wid).sealed
        assert check.get(Work, wid).cursor == 0
        assert check.scalar(select(func.count()).select_from(Candidate).where(Candidate.work_id == wid)) == 0
    monkeypatch.setattr(records, 'convert', lambda source, raw: (
        calls.update([int(raw['ts_code'].rsplit('-', 1)[1])]) or original(source, raw)))
    assert prepare_step(session.bind, wid, epoch, stop=Event(), budget_seconds=60)
    assert all(calls[i] == 1 for i in range(200))
    assert calls[200] == 2 and calls[400] == 1
    monkeypatch.setattr(records, 'read_source', lambda *_: pytest.fail('sealed input reread'))
    assert prepare_step(session.bind, wid, epoch, stop=Event())
    with Session(session.bind) as finish, finish.begin():
        records.normalize_batch(finish, wid, epoch)
        assert finish.get(Work, wid).cursor == 200


def test_preparation_yields_and_stale_or_cancelled_owner_cannot_commit(session, tmp_path, monkeypatch):
    (batch, _, _), _ = fixture(session, tmp_path, 201)
    owned = claim(session, work_id=work_for(session, batch).id)
    wid, epoch = owned.id, owned.lease_epoch
    session.commit()
    ticks = iter((0, 100))
    assert not prepare_step(session.bind, wid, epoch, stop=Event(), clock=lambda: next(ticks))
    with Session(session.bind) as check, check.begin():
        assert check.get(Work, wid).cursor == 0
        assert not check.get(RecordPreparation, wid).sealed
        cancel(check, wid)
    with pytest.raises(FoundationError, match='租约'):
        prepare_step(session.bind, wid, epoch, stop=Event())
    with Session(session.bind) as check:
        assert check.scalar(select(func.count()).select_from(RecordPreparedPage).where(
            RecordPreparedPage.work_id == wid)) == 1
        assert check.get(Work, wid).status == 'cancelled'
