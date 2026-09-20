"""Count database work after expunging ORM state, not wall-clock CI timing."""
from uuid import uuid4
from sqlalchemy import event
from app.data_foundation.batches import batch_document, register_batch, attach_work
from app.data_foundation.reconciliation import reconcile_batch
from app.data_foundation.publication import publish
from tests.test_foundation_publication_postgresql import session, pytestmark, setup, normalize, governance


def test_reconciliation_queries_do_not_grow_as_candidate_decision_cross_product(session):
    fixture = setup(session, count=80)
    manifest = normalize(session, fixture)
    work, release = governance(session, fixture, manifest)
    publish(session, release.id, work.lease_epoch)
    document, fingerprint = batch_document(session, scope_key=work.scope_key,
        source_ids=[fixture['ref'].id], start='2026-01-01', end='2026-12-31', purpose='shadow')
    batch = register_batch(session, event_key=uuid4().hex, document=document, expected_hash=fingerprint)
    attach_work(session, batch.id, fixture['work'].id)
    attach_work(session, batch.id, work.id)
    batch_id = batch.id
    expected = reconcile_batch(session, batch_id).evidence_hash
    session.flush()
    session.expunge_all()
    statements = []
    def observe(conn, cursor, sql, params, context, many):
        statements.append(sql)
    event.listen(session.bind, 'before_cursor_execute', observe)
    try:
        actual = reconcile_batch(session, batch_id)
    finally:
        event.remove(session.bind, 'before_cursor_execute', observe)
    assert actual.evidence_hash == expected
    assert actual.status == 'explained'
    # The old nested lookup used over 6400 statements for these 80 keys.
    # Allow a fixed margin for dependency checks, not per-row round trips.
    assert len(statements) < 60, len(statements)
