"""Prove reusable validation never makes a mutable or mismatched block trusted."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_records_postgresql import setup_records, release_records
from app.data_foundation.canonical import FoundationError
from app.data_foundation.catalog import now
from app.data_foundation.record_models import RecordBlockVerification, RecordBlockMember
from app.data_foundation.record_work import validate_release
from app.data_foundation.work_models import BlockRef, ReleaseBlock


def factors(count):
    return [dict(ts_code=f'F{i}', trade_date='2026-01-01', adj_factor='1.123456789012') for i in range(count)]


def test_factor_update_reuses_unaffected_subject_blocks(session):
    first = release_records(session, setup_records(session, factors(12), 'etf_adjustment_factors'))
    before = dict(session.execute(select(BlockRef.partition_key, BlockRef.block_id).where(BlockRef.release_id == first.id)).all())
    assert len(before) == 12 and all(key.startswith('subject:') for key in before)
    second = release_records(session, setup_records(session, [dict(ts_code='F0', trade_date='2026-01-02', adj_factor='2')],
        'etf_adjustment_factors'), first)
    after = dict(session.execute(select(BlockRef.partition_key, BlockRef.block_id).where(BlockRef.release_id == second.id)).all())
    assert set(before) == set(after)
    assert sum(before[key] == after[key] for key in before) == 11
    assert sum(session.get(ReleaseBlock, bid).row_count for bid in after.values()) == 13
    assert sum(session.get(ReleaseBlock, bid).row_count for bid in before.values()) == 12
    validate_release(session, first)
    validate_release(session, second)


def test_receipts_skip_only_previously_checked_blocks_and_full_audit_remains(session, monkeypatch):
    from app.data_foundation import record_work
    release = release_records(session, setup_records(session, factors(15), 'etf_adjustment_factors'))
    blocks = list(session.scalars(select(BlockRef.block_id).where(BlockRef.release_id == release.id)))
    assert len(list(session.scalars(select(RecordBlockVerification).where(RecordBlockVerification.block_id.in_(blocks))))) == 15
    original = record_work.validate_body
    checked = []
    def traced(dataset, body):
        checked.append(body['source_code'])
        return original(dataset, body)
    monkeypatch.setattr(record_work, 'validate_body', traced)
    statements = []
    def sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(session.connection(), 'before_cursor_execute', sql)
    try:
        validate_release(session, release, reuse_verified=True)
    finally:
        event.remove(session.connection(), 'before_cursor_execute', sql)
    assert checked == [] and len(statements) <= 6
    validate_release(session, release)
    assert len(checked) == 15
    # A cached block never authenticates a different release root or scope.
    changed = SimpleNamespace(id=release.id, work_id=release.work_id, scope_key=release.scope_key, manifest_hash='0'*64)
    with pytest.raises(FoundationError, match='清单摘要'):
        validate_release(session, changed, reuse_verified=True)
    changed.scope_key = '1'*64
    with pytest.raises(FoundationError, match='校验凭据'):
        validate_release(session, changed, reuse_verified=True)
    # A different code/dependency identity must perform a fresh full check,
    # even when the exact same immutable block already has another receipt.
    checked.clear()
    monkeypatch.setattr(record_work, 'validation_hash', lambda: 'f'*64)
    validate_release(session, release, reuse_verified=True)
    assert len(checked) == 15


def test_closed_block_and_receipt_cannot_be_modified_or_extended(session):
    release = release_records(session, setup_records(session, factors(1), 'etf_adjustment_factors'))
    bid = session.scalar(select(BlockRef.block_id).where(BlockRef.release_id == release.id))
    member = session.scalar(select(RecordBlockMember).where(RecordBlockMember.block_id == bid))
    for statement, params in [
        ('UPDATE foundation_record_block_verifications SET row_count=2 WHERE block_id=:id', {'id':bid}),
        ('DELETE FROM foundation_record_block_verifications WHERE block_id=:id', {'id':bid}),
        ('UPDATE foundation_record_official_revisions SET body_json=\'{}\' WHERE id=:id', {'id':member.official_id}),
        ('''INSERT INTO foundation_record_block_members (block_id,target_key,subject_id,business_date,state,official_id,decision_id)
            SELECT block_id,repeat('f',64),subject_id,business_date,state,official_id,decision_id
            FROM foundation_record_block_members WHERE block_id=:id''', {'id':bid}),
    ]:
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(statement), params)
    block = ReleaseBlock(partition_key='00', row_count=0, content_hash='0'*64, created_at=now())
    session.add(block)
    session.flush()
    with pytest.raises(DBAPIError, match='closed matching block'), session.begin_nested():
        session.add(RecordBlockVerification(block_id=block.id, validator_hash='0'*64, scope_key='0'*64,
            schema_key='market.adjustment_factor', content_hash=block.content_hash, row_count=0, verified_at=now(),
            governance_work_ids_json='[]', normalization_work_ids_json='[]'))
        session.flush()


def test_legacy_factor_partition_lineage_does_not_mix_schemes(session, monkeypatch):
    from app.data_foundation import record_work
    original = record_work.subject_partitioned
    monkeypatch.setattr(record_work, 'subject_partitioned', lambda dataset, partitions: False)
    first = release_records(session, setup_records(session, factors(2), 'etf_adjustment_factors'))
    monkeypatch.setattr(record_work, 'subject_partitioned', original)
    second = release_records(session, setup_records(session, [dict(ts_code='F0', trade_date='2026-01-02', adj_factor='2')],
        'etf_adjustment_factors'), first)
    keys = list(session.scalars(select(BlockRef.partition_key).where(BlockRef.release_id == second.id)))
    assert keys and all(len(key) == 2 for key in keys)
    validate_release(session, second)
    with pytest.raises(FoundationError, match='分块方式'):
        original('market.adjustment_factor', ['00', 'subject:' + str(uuid4())])


def test_contributions_use_exact_current_blocks_and_drop_replaced_ancestors(session):
    from app.data_foundation.batches import record_contributions
    from app.data_foundation.batch_models import ReleaseContribution
    first_fixture = setup_records(session, factors(2), 'etf_adjustment_factors')
    first = release_records(session, first_fixture)
    second_fixture = setup_records(session, [dict(ts_code='F0', trade_date='2026-01-01', adj_factor='2')], 'etf_adjustment_factors')
    second = release_records(session, second_fixture, first)
    third_fixture = setup_records(session, [dict(ts_code='F1', trade_date='2026-01-01', adj_factor='3')], 'etf_adjustment_factors')
    third = release_records(session, third_fixture, second)
    actual = set(session.execute(select(ReleaseContribution.work_id, ReleaseContribution.role)
        .where(ReleaseContribution.release_id == third.id)).all())
    assert actual == {(third.work_id, 'governance'), (third_fixture[0].id, 'normalization'),
        (second.work_id, 'inherited'), (second_fixture[0].id, 'inherited')}
    assert first.work_id not in {wid for wid, _ in actual}
    statements = []
    def sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(session.connection(), 'before_cursor_execute', sql)
    try:
        record_contributions(session, third)
    finally:
        event.remove(session.connection(), 'before_cursor_execute', sql)
    assert not any('foundation_record_block_members' in query for query in statements)
    validate_release(session, third)
