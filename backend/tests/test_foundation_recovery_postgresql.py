"""Restore evidence covers restrictions and exact immutable references."""
from sqlalchemy import text
import pytest
from app.data_foundation.recovery import snapshot_evidence, ensure_serving
from app.data_foundation.canonical import FoundationError, encode
from tests.test_foundation_publication_postgresql import session, pytestmark, setup, normalize, governance
from app.data_foundation.publication import publish


def test_snapshot_evidence_is_stable_and_includes_the_formal_chain(session):
    fixture=setup(session)
    work,release=governance(session,fixture,normalize(session,fixture))
    publish(session,release.id,work.lease_epoch)
    first=snapshot_evidence(session)
    session.expunge_all()
    assert encode(snapshot_evidence(session))==encode(first)
    counts={row['name']:row['rows'] for row in first['tables']}
    assert counts['foundation_bar_official_revisions']>=2
    assert counts['foundation_candidates']>=2
    assert counts['foundation_issue_scopes']>=1
    assert any(str(row['release_id'])==str(release.id) for row in first['heads'])


def test_restore_gate_rejects_reads_until_review(session):
    from app.core.auth import AuthenticatedPrincipal
    from app.data_foundation.service import FoundationService
    ensure_serving(session)
    service = FoundationService(session, lambda: AuthenticatedPrincipal('recovery-test'), 'test-key')
    session.execute(text("SET LOCAL qf.foundation_recovery_pending='on'"))
    with pytest.raises(FoundationError,match='恢复数据'):ensure_serving(session)
    with pytest.raises(FoundationError, match='恢复数据'):service.describe_dataset()
    with pytest.raises(FoundationError, match='恢复数据'):
        FoundationService(session, lambda: AuthenticatedPrincipal('recovery-test'), 'test-key')


def test_http_gate_rejects_even_direct_evidence_routes(session):
    import asyncio
    from tests.test_auth import request_status
    from app.main import create_app
    from app.core.config import get_settings
    from app.db.session import get_db_session
    settings = get_settings()
    app = create_app(settings)
    app.dependency_overrides[get_db_session] = lambda: session
    session.execute(text("SET LOCAL qf.foundation_recovery_pending='on'"))
    # Do not start the normal lifespan: restored suppliers and schedules must
    # remain untouched until recovery review has completed.
    for path in ('datasets', 'processing', 'source-refs/780f9afb-1e31-4470-a998-308e025c6b19'):
        status = asyncio.run(request_status(app, '/api/admin/data-foundation/' + path,
                                            settings.api_token.get_secret_value()))
        assert status == 503
