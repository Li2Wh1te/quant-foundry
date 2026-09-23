"""Late snapshots are official and readable without replacing current values."""
import json
from uuid import uuid4
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_records_postgresql import setup_records, release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.record_work import create_governance, verify_governance_plan
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Head, IssueScope
from app.data_foundation.work import claim
from app.data_foundation.publication import stage_decisions, publish


def staged_history(session, fixture, parent):
    origin, ex, policy = fixture
    head, guard = session.get(Head, origin.scope_key), session.get(IssueScope, origin.scope_key)
    work = create_governance(session, normalization_id=origin.id, execution_id=ex.id,
        policy_id=policy.id, parent_release_id=parent.id, expected_head_revision=head.revision,
        expected_issue_epoch=guard.epoch, preserve_head=True)
    verify_governance_plan(session, work, force=True)
    release = None
    while release is None:
        lease = claim(session, work_id=work.id)
        release = stage_decisions(session, work.id, lease.lease_epoch)
    return release, work


def test_late_history_preserves_newer_head_and_is_independently_readable(session):
    code = 'history-'+uuid4().hex
    first_fixture = setup_records(session, [dict(ts_code=code, csname='Current')])
    old_head = session.get(Head, first_fixture[0].scope_key)
    from app.data_foundation.work_models import Release
    parent = session.get(Release, old_head.release_id) if old_head else None
    first = release_records(session, first_fixture, parent)
    historical, work = staged_history(session,
        setup_records(session, [dict(ts_code=code, csname='Late older observation')]), first)
    # Another valid current update can publish while the detached history is
    # staged. The historical publication must neither revert nor advance it.
    newer = release_records(session, setup_records(session, [dict(ts_code=code, csname='Newest')]), first)
    head = session.get(Head, work.scope_key)
    revision = head.revision
    assert publish(session, historical.id, work.lease_epoch).status == 'published'
    assert head.release_id == newer.id and head.revision == revision
    subject = session.scalar(select(RecordSubject.id).where(RecordSubject.source_key == code))
    request = RecordRequirement(dataset_id='instrument.reference',
        semantic_series_id='tushare-instrument.reference-observed-v1', subjects=[subject], fields=['name'])
    assert json.loads(service(session).query_official(request))['items'][0]['name'] == 'Newest'
    prior = json.loads(service(session).query_official(request.model_copy(update={'release': historical.id})))
    assert prior['items'][0]['name'] == 'Late older observation'
    assert publish(session, historical.id, work.lease_epoch).id == historical.id
    assert head.revision == revision


def test_historical_publication_keeps_issue_epoch_gate(session):
    fixture = setup_records(session, [dict(ts_code='issue-history-'+uuid4().hex)])
    from app.data_foundation.work_models import Release
    head = session.get(Head, fixture[0].scope_key)
    first = release_records(session, fixture, session.get(Release, head.release_id) if head else None)
    historical, work = staged_history(session, setup_records(session), first)
    session.get(IssueScope, work.scope_key).epoch += 1
    session.flush()
    assert publish(session, historical.id, work.lease_epoch) is None
    assert historical.status == 'sealed' and work.status == 'superseded'
    assert session.get(Head, work.scope_key).release_id == first.id
