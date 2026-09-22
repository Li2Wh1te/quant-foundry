"""Bounded SQL and immutable conflict checks for dense typed-record pages."""
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import event, select, func
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_sources import execution
from app.data_foundation.source_refs import register_baseline
from app.data_foundation.record_work import create_normalization, normalize_batch, create_governance, stage_decisions
from app.data_foundation.work import claim
from app.data_foundation.work_models import Candidate
from app.data_foundation.record_bulk import register_assessments
from app.data_foundation.canonical import FoundationError, digest


def test_dense_normalization_page_uses_bounded_sql(session):
    ex = execution(session)
    ref = register_baseline(session, source='tushare', dataset='etf_directory', scope={},
        rows=[dict(ts_code=f'BULK{i}', csname=f'Name{i}') for i in range(210)],
        observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key=uuid4().hex)
    work, policy = create_normalization(session, source_ref_id=ref.id, execution_id=ex.id)
    lease = claim(session, work_id=work.id)
    statements = []
    def observed(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(session.bind, 'before_cursor_execute', observed)
    try:
        normalize_batch(session, work.id, lease.lease_epoch)
    finally:
        event.remove(session.bind, 'before_cursor_execute', observed)
    assert work.cursor == 200 and work.status == 'queued'
    assert len(statements) <= 60, len(statements)
    assert session.scalar(select(func.count()).select_from(Candidate).where(Candidate.work_id == work.id)) == 200
    lease = claim(session, work_id=work.id)
    normalize_batch(session, work.id, lease.lease_epoch)
    assert work.status == 'succeeded' and work.cursor == 210
    governance = create_governance(session, normalization_id=work.id, execution_id=ex.id, policy_id=policy.id)
    lease = claim(session, work_id=governance.id)
    statements.clear()
    event.listen(session.bind, 'before_cursor_execute', observed)
    try:
        assert stage_decisions(session, governance.id, lease.lease_epoch) is None
    finally:
        event.remove(session.bind, 'before_cursor_execute', observed)
    assert governance.cursor == 200 and len(statements) <= 60, len(statements)
    lease = claim(session, work_id=governance.id)
    release = stage_decisions(session, governance.id, lease.lease_epoch)
    assert release.status == 'sealed'


def test_bulk_assessment_reuses_exact_rows_and_rejects_conflicts(session):
    base = dict(input_hash=digest('test', uuid4()), rule_hash=digest('rule', 1),
        scope_hash=digest('scope', 1), status='pass', results={'known':True})
    original = register_assessments(session, [base])
    repeated = register_assessments(session, [base, base])
    assert [r.id for r in original.values()] == [r.id for r in repeated.values()]
    with pytest.raises(FoundationError, match='不能变更'):
        register_assessments(session, [{**base, 'results':{'known':False}}])
    with pytest.raises(FoundationError, match='同页'):
        register_assessments(session, [base, {**base, 'status':'fail'}])
