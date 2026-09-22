"""Real database retention, exact membership, and process-restart acceptance."""
from uuid import uuid4

import pytest
from sqlalchemy import select, update, delete
from sqlalchemy.exc import DBAPIError

from app.data_foundation.intake import register_scope, set_paused, start_scan, scan_page
from app.data_foundation.intake_models import ScanTarget, ScanSeal, Scan, IntakeItem
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_sources import execution, observations


def fixed_scan(session):
    key = uuid4().hex
    anchor, delta = observations(session)
    anchor.subject = delta.subject = key
    session.flush()
    scope = register_scope(session, dataset=anchor.dataset, subject=key, variant=anchor.variant,
        selector={'scope': 'all_local'}, decoder_id=execution(session).id)
    set_paused(session, scope.id, False)
    scan = start_scan(session, scope.id, key, mode='frozen')
    return scope, scan, anchor, delta


def test_capture_is_sealed_and_retains_original_versions(session):
    scope, scan, anchor, delta = fixed_scan(session)
    assert session.get(ScanSeal, scan.id).target_count == 2
    for statement in (
        update(ScanTarget).where(ScanTarget.scan_id == scan.id).values(content_hash='0'*64),
        delete(ScanTarget).where(ScanTarget.scan_id == scan.id),
        update(Observation).where(Observation.id == anchor.id).values(subject='reassigned'),
        delete(Observation).where(Observation.id == delta.id),
    ):
        with pytest.raises(DBAPIError):
            with session.begin_nested():
                session.execute(statement)
    late = Observation(id=uuid4(), dataset=anchor.dataset, subject=anchor.subject, variant=anchor.variant,
        observed_at=anchor.observed_at, request_json='{}', data_json=anchor.data_json,
        content_hash=anchor.content_hash, row_count=anchor.row_count, chain_depth=0)
    session.add(late)
    session.flush()
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.add(ScanTarget(scan_id=scan.id, observation_id=late.id, content_hash=late.content_hash))
            session.flush()
    result = scan_page(session, scan.id)
    assert result['seen'] == 2 and result['registration_complete']
    assert len(session.scalars(select(IntakeItem).where(IntakeItem.scope_id == scope.id)).all()) == 2


def test_page_rollback_recovers_fixed_input_without_partial_evidence(session):
    scope, scan, _, _ = fixed_scan(session)
    with pytest.raises(RuntimeError, match='simulated worker interruption'):
        with session.begin_nested():
            assert scan_page(session, scan.id, limit=1)['seen'] == 1
            raise RuntimeError('simulated worker interruption')
    session.expire_all()
    restored = session.get(Scan, scan.id)
    assert restored.seen == 0 and restored.cursor_id is None
    assert session.scalar(select(IntakeItem).where(IntakeItem.scope_id == scope.id)) is None
    result = scan_page(session, restored.id)
    assert result['registered'] == 2 and result['registration_complete']
