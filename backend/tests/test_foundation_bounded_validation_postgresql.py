"""Cold validation checkpoints resume without publishing drafts or stale work."""
from datetime import date, timedelta
from threading import Event
import json

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session, isolated_series
from tests.test_foundation_records_postgresql import setup_records, release_records
from app.data_foundation import record_work as records
from app.data_foundation import record_validation_driver as driver
from app.data_foundation.record_validation_models import RecordValidationRoot, RecordBlockPageVerification
from app.data_foundation.record_models import RecordBlockVerification
from app.data_foundation.work_models import Work, Release, BlockRef, Head
from app.data_foundation.work import claim, heartbeat, cancel
from app.data_foundation.canonical import FoundationError
from app.data_foundation.publication import publish


def draft(session, count=401):
    rows = [dict(ts_code='BOUNDED', trade_date=str(date(2020, 1, 1)+timedelta(days=i)),
                 adj_factor='1.123456789012') for i in range(count)]
    origin, execution, policy = setup_records(session, rows, 'etf_adjustment_factors')
    work = records.create_governance(session, normalization_id=origin.id,
        execution_id=execution.id, policy_id=policy.id)
    session.info['foundation_defer_record_validation'] = True
    release = None
    while release is None:
        owned = claim(session, work_id=work.id)
        release = records.stage_decisions(session, work.id, owned.lease_epoch)
    ids = work.id, work.lease_epoch, release.id, release.scope_key
    assert release.status == 'draft'
    assert session.get(RecordValidationRoot, release.id) is not None
    session.commit()
    return ids


def test_cold_block_validates_two_hundred_rows_per_commit_and_resumes_exact_proofs(session, monkeypatch):
    wid, epoch, rid, scope = draft(session)
    sizes = []
    original = driver.check_members
    def traced(current_session, members, **kwargs):
        sizes.append(len(members))
        with Session(session.bind) as pulse, pulse.begin():
            pulse.execute(text("SET LOCAL lock_timeout='200ms'"))
            heartbeat(pulse, wid, epoch)
        return original(current_session, members, **kwargs)
    monkeypatch.setattr(driver, 'check_members', traced)
    assert not driver.advance_validation(session.bind, wid, epoch, stop=Event(), max_pages=1)
    with Session(session.bind) as check:
        block = check.scalar(select(BlockRef.block_id).where(BlockRef.release_id == rid))
        assert check.get(Release, rid).status == 'draft' and check.get(Head, scope) is None
        assert check.get(RecordBlockVerification, (block, records.validation_hash())) is None
        assert check.scalar(select(func.count()).select_from(RecordBlockPageVerification).where(
            RecordBlockPageVerification.block_id == block)) == 1
        with pytest.raises(DBAPIError), check.begin_nested():
            check.execute(text('DELETE FROM foundation_record_block_page_verifications WHERE block_id=:id'), {'id': block})
        with pytest.raises(DBAPIError), check.begin_nested():
            check.execute(text('''INSERT INTO foundation_release_block_refs (release_id,partition_key,block_id)
                SELECT release_id,partition_key||'x',block_id FROM foundation_release_block_refs WHERE release_id=:id'''), {'id': rid})
    assert not driver.advance_validation(session.bind, wid, epoch, stop=Event(), max_pages=1)
    assert driver.advance_validation(session.bind, wid, epoch, stop=Event(), max_pages=1)
    assert sizes == [200, 200, 1]
    with Session(session.bind) as check, check.begin():
        release = check.get(Release, rid)
        assert release.status == 'sealed'
        assert check.get(Head, scope) is None
        proof = check.get(RecordBlockVerification, (block, records.validation_hash()))
        assert proof.row_count == 401
        # A full audit deliberately re-reads values despite all page receipts.
        checked = []
        body_validator = records.validate_body
        monkeypatch.setattr(records, 'validate_body', lambda *args: (checked.append(1), body_validator(*args))[1])
        records.validate_release(check, release)
        assert len(checked) == 401
        publish(check, rid, epoch)
        assert release.status == 'published'
    with Session(session.bind) as check:
        assert check.get(Head, scope).release_id == rid
        assert check.scalar(select(func.count()).select_from(Release).where(Release.work_id == wid)) == 1


def test_cancel_during_proof_read_rolls_back_page_and_never_activates_head(session, monkeypatch):
    wid, epoch, rid, scope = draft(session, 201)
    original = driver.check_members
    def cancelled(current_session, members, **kwargs):
        result = original(current_session, members, **kwargs)
        with Session(session.bind) as other, other.begin():
            cancel(other, wid)
        return result
    monkeypatch.setattr(driver, 'check_members', cancelled)
    with pytest.raises(FoundationError, match='租约'):
        driver.advance_validation(session.bind, wid, epoch, stop=Event())
    with Session(session.bind) as check:
        bid = check.scalar(select(BlockRef.block_id).where(BlockRef.release_id == rid))
        assert check.scalar(select(func.count()).select_from(RecordBlockPageVerification).where(
            RecordBlockPageVerification.block_id == bid)) == 0
        assert check.get(Release, rid).status == 'draft'
        assert check.get(Head, scope) is None
        assert check.get(Work, wid).status == 'cancelled'


def test_different_validator_rechecks_inherited_blocks_without_relabelling_old_receipts(session, monkeypatch):
    rows = [dict(ts_code=f'OLD{i}', trade_date='2026-01-01', adj_factor='1') for i in range(3)]
    parent = release_records(session, setup_records(session, rows, 'etf_adjustment_factors'))
    old_hash = records.validation_hash()
    old_blocks = list(session.scalars(select(BlockRef.block_id).where(BlockRef.release_id == parent.id)))
    origin, execution, policy = setup_records(session, [dict(ts_code='NEW', trade_date='2026-01-01', adj_factor='1')], 'etf_adjustment_factors')
    head = session.get(Head, origin.scope_key)
    work = records.create_governance(session, normalization_id=origin.id, execution_id=execution.id,
        policy_id=policy.id, parent_release_id=parent.id, expected_head_revision=head.revision)
    session.info['foundation_defer_record_validation'] = True
    owned = claim(session, work_id=work.id)
    monkeypatch.setattr(records, 'validation_hash', lambda: 'e'*64)
    child = records.stage_decisions(session, work.id, owned.lease_epoch)
    wid, epoch, rid = work.id, work.lease_epoch, child.id
    session.commit()
    assert not driver.advance_validation(session.bind, wid, epoch, stop=Event(), max_pages=1)
    assert driver.advance_validation(session.bind, wid, epoch, stop=Event(), max_pages=10)
    with Session(session.bind) as check:
        for block in old_blocks:
            assert check.get(RecordBlockVerification, (block, old_hash)) is not None
            assert check.get(RecordBlockVerification, (block, 'e'*64)) is not None
        assert check.get(Release, rid).status == 'sealed'
