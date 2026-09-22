"""Typed records exercise the actual shared PostgreSQL publication gates."""
import json
from datetime import datetime, timezone
from uuid import uuid4, UUID
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_sources import execution
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.source_refs import register_baseline
from app.data_foundation.record_work import create_normalization, create_governance, validate_release
from app.data_foundation.record_models import OfficialRecord, RecordSubject, RecordBlockMember
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_issues import record_issue
from app.data_foundation.work_models import Head, IssueScope, Candidate
from app.data_foundation.work import claim
from app.data_foundation.bars import normalize_batch
from app.data_foundation.publication import stage_decisions, publish
from app.data_foundation.service import PageRequest
from app.data_foundation.canonical import FoundationError


def setup_records(session, rows=None, dataset='etf_directory'):
    ex = execution(session)
    rows = rows if rows is not None else [dict(ts_code='TEST.SH', csname='Fixture', exchange='SSE')]
    ref = register_baseline(session, source='tushare', dataset=dataset, scope={}, rows=rows,
        observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key=uuid4().hex)
    work, policy = create_normalization(session, source_ref_id=ref.id, execution_id=ex.id)
    while work.status != 'succeeded':
        c = claim(session, work_id=work.id)
        normalize_batch(session, work.id, c.lease_epoch)
    return work, ex, policy


def release_records(session, fixture, parent=None):
    origin, ex, policy = fixture
    head, guard = session.get(Head, origin.scope_key), session.get(IssueScope, origin.scope_key)
    work = create_governance(session, normalization_id=origin.id, execution_id=ex.id, policy_id=policy.id,
        parent_release_id=parent.id if parent else None, expected_head_revision=head.revision if head else 0,
        expected_issue_epoch=guard.epoch if guard else 0)
    release = None
    while release is None:
        c = claim(session, work_id=work.id)
        release = stage_decisions(session, work.id, c.lease_epoch)
    publish(session, release.id, work.lease_epoch)
    return release


def request(session, **changes):
    return RecordRequirement(**(dict(dataset_id='instrument.reference',
        semantic_series_id='tushare-instrument.reference-observed-v1',
        subjects=list(session.scalars(select(RecordSubject.id))), fields=['source_code', 'name']) | changes))


def test_typed_publication_fixed_reads_without_source_decoder(session, monkeypatch):
    fixture = setup_records(session, [dict(ts_code=f'T{i}.SH', csname=f'Name{i}', exchange='SSE') for i in range(3)])
    first = release_records(session, fixture)
    validate_release(session, first)
    svc = service(session)
    checked = json.loads(svc.check_capability(request(session)))
    assert checked['request_satisfied']
    page = json.loads(svc.query_official(PageRequest(resolution_token=checked['resolution_token'], page_size=1)))
    assert len(page['items']) == 1 and page['next_cursor']
    import app.data_foundation.source_refs as sources
    monkeypatch.setattr(sources, 'read_source', lambda *a: (_ for _ in ()).throw(AssertionError('decoder called')))
    second = json.loads(svc.query_official(PageRequest(resolution_token=checked['resolution_token'], cursor=page['next_cursor'], page_size=2)))
    assert len(second['items']) == 2 and not second['has_more']
    assert {r['source_code'] for r in page['items'] + second['items']} == {'T0.SH','T1.SH','T2.SH'}
    assert json.loads(svc.get_lineage(UUID(page['items'][0]['official_id'])))['source'] == 'tushare'
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(text("UPDATE foundation_record_official_revisions SET body_json='{}'"))


def test_duplicate_business_keys_are_blocked_and_bad_identity_quarantined(session):
    f = setup_records(session, [dict(ts_code='A', csname='one'), dict(ts_code='A', csname='two'), dict(ts_code='B', csname='good'), {}])
    release_records(session, f)
    assert len(session.scalars(select(Candidate).where(Candidate.readiness == 'quarantined')).all()) == 3
    result = json.loads(service(session).query_official(request(session, allow_partial=True)))
    assert [r['source_code'] for r in result['items']] == ['B']
    assert result['state'] == 'partial' and len(result['excluded']) == 1
    assert set(session.scalars(select(RecordBlockMember.state))) == {'blocked', 'value'}


def test_unchanged_retains_revision_and_changed_value_keeps_old_release(session):
    first = release_records(session, setup_records(session))
    original = session.scalar(select(OfficialRecord))
    second = release_records(session, setup_records(session), first)
    assert list(session.scalars(select(OfficialRecord.id))) == [original.id]
    third = release_records(session, setup_records(session, [dict(ts_code='TEST.SH', csname='Changed', exchange='SSE')]), second)
    svc = service(session)
    assert json.loads(svc.query_official(request(session, release=first.id)))['items'][0]['name'] == 'Fixture'
    assert json.loads(svc.query_official(request(session, release=third.id)))['items'][0]['name'] == 'Changed'


def test_issue_epoch_invalidates_tokens_and_resolved_keeps_old_value_restricted(session):
    release = release_records(session, setup_records(session))
    row = session.scalar(select(OfficialRecord))
    svc = service(session)
    checked = json.loads(svc.check_capability(request(session)))
    args = dict(scope_key=release.scope_key, target_key=row.business_key, official_id=row.id,
        fields=['name'], reason='isolated incorrect name', evidence={'fixture': True})
    issue = record_issue(session, **args, state='confirmed')
    with pytest.raises(FoundationError, match='当前读取限制'):
        svc.query_official(PageRequest(resolution_token=checked['resolution_token']))
    record_issue(session, **args, state='resolved', issue_id=issue.issue_id)
    result = json.loads(svc.query_official(request(session)))
    assert not result['items'] and result['excluded'][0]['reason'] == 'CURRENT_ISSUE'
    assert json.loads(svc.query_official(request(session, fields=['source_code'])))['request_satisfied']


def test_calendar_dates_closed_days_and_snapshot_use_same_release(session):
    f = setup_records(session, [dict(exchange='SSE', calendar_date='2026-01-01', is_open=False),
        dict(exchange='SSE', calendar_date='2026-01-02', is_open=True)], 'exchange_calendar')
    release_records(session, f)
    req = request(session, dataset_id='market.calendar', semantic_series_id='tushare-market.calendar-observed-v1',
        business_range={'from':'2026-01-01','to':'2026-01-02'}, fields=['calendar_date','is_open'])
    svc = service(session)
    data = json.loads(svc.query_official(req))
    assert {r['calendar_date']: r['is_open'] for r in data['items']} == {'2026-01-01':False, '2026-01-02':True}
    snap = json.loads(svc.resolve_snapshot([req]))
    assert json.loads(svc.read_snapshot(snap))['items'][0]['release_id'] == data['release_id']
    assert not json.loads(svc.query_official(req.model_copy(update={'time_mode':'strict_public_pit'})))['items']
    assert json.loads(svc.query_official(req.model_copy(update={'require_complete':True})))['state'] == 'unknown'


def test_batch_reconciliation_tracks_quarantine_and_real_release_contributions(session):
    from app.data_foundation.batches import batch_document, register_batch, attach_work, set_controls
    from app.data_foundation.reconciliation import reconcile_batch
    from app.data_foundation.work import finish_batch
    from app.data_foundation.batch_models import ReleaseContribution
    origin, ex, policy = setup_records(session, [dict(ts_code='OK', csname='Valid'), {}])
    document, fingerprint = batch_document(session, scope_key=origin.scope_key, source_ids=[origin.source_ref_id],
        start='0001-01-01', end='9999-12-31', purpose='maintenance')
    batch = register_batch(session, event_key=uuid4().hex, document=document, expected_hash=fingerprint)
    attach_work(session, batch.id, origin.id)
    governance = create_governance(session, normalization_id=origin.id, execution_id=ex.id, policy_id=policy.id)
    attach_work(session, batch.id, governance.id)
    set_controls(session, batch.id, pause_a=False, pause_b=False)
    lease = claim(session, work_id=governance.id)
    release = stage_decisions(session, governance.id, lease.lease_epoch)
    finish_batch(session, governance, status='awaiting_publication')
    result = reconcile_batch(session, batch.id)
    assert result.status == 'explained'
    set_controls(session, batch.id, pause_a=False, pause_b=False, allow_publish=True)
    lease = claim(session, work_id=governance.id)
    publish(session, release.id, lease.lease_epoch)
    assert {(r.work_id,r.role) for r in session.scalars(select(ReleaseContribution).where(ReleaseContribution.release_id == release.id))} == {
        (origin.id,'normalization'),(governance.id,'governance')}


def test_fields_after_first_page_still_fail_full_capability(session):
    rows = [dict(ts_code=f'T{i}', csname=f'Name{i}') for i in range(3)]
    rows[2]['csname'] = None
    release_records(session, setup_records(session, rows))
    svc = service(session)
    result = json.loads(svc.query_official(request(session, page_size=1)))
    assert not result['request_satisfied'] and not result['items']
    assert result['excluded'][0]['reason'] == 'FIELD_UNAVAILABLE'
    partial = json.loads(svc.query_official(request(session, page_size=1, allow_partial=True)))
    assert len(partial['items']) == 1 and partial['next_cursor']


def test_api_schema_accepts_typed_record_and_unknown_domains_fail_closed(session):
    from app.main import app
    from pydantic import TypeAdapter
    from app.data_foundation.query import DataRequirement
    from app.data_foundation.holding_query import ReportRequirement
    release_records(session, setup_records(session))
    req = request(session)
    validated = TypeAdapter(RecordRequirement | ReportRequirement | DataRequirement).validate_python(req.model_dump())
    assert isinstance(validated, RecordRequirement)
    assert 'RecordRequirement' in app.openapi()['components']['schemas']
    with pytest.raises(FoundationError, match='未知正式数据集'):
        service(session).describe_dataset('unknown')


def test_directory_governance_and_release_validation_batch_reference_reads(session):
    """A dense hash partition exposes accidental per-record SQL regressions."""
    from types import SimpleNamespace
    from sqlalchemy import event
    from app.data_foundation.record_adapters import convert
    from app.data_foundation.record_work import record_key, plan_actions
    from app.data_foundation.work_models import CandidateManifest
    src = SimpleNamespace(source='tushare', dataset='etf_directory')
    rows = []
    ordinal = 0
    while len(rows) < 25:
        raw = dict(ts_code=f'DENSE{ordinal}.SH', csname='Dense', exchange='SSE')
        if record_key(src, convert(src, raw)).startswith('00'):
            rows.append(raw)
        ordinal += 1
    first = release_records(session, setup_records(session, rows))
    fixture = setup_records(session, rows)
    manifest = session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == fixture[0].id))
    statements = []
    connection = session.connection()
    def record_sql(*args):
        statements.append(args[2])
    event.listen(connection, 'before_cursor_execute', record_sql)
    try:
        actions = plan_actions(session, manifest.id, fixture[2].id, first.id)
        assert len(actions) == 25 and {a['action'] for a in actions} == {'retain'}
        assert len(statements) < 15
    finally:
        event.remove(connection, 'before_cursor_execute', record_sql)
    second = release_records(session, fixture, first)
    statements.clear()
    event.listen(connection, 'before_cursor_execute', record_sql)
    try:
        validate_release(session, second)
        assert len(statements) < 15
    finally:
        event.remove(connection, 'before_cursor_execute', record_sql)


def test_asset_summary_and_subject_pages_pin_release(session):
    from app.data_foundation.record_service import describe, subjects
    first = release_records(session, setup_records(session, [dict(ts_code=f'T{i}', csname=f'Name{i}') for i in range(3)]))
    svc = service(session)
    summary = describe(svc, 'instrument.reference')
    assert summary['kind'] == 'typed_records'
    assert summary['official_count'] == summary['candidate_count'] == 3
    series = 'tushare-instrument.reference-observed-v1'
    page = subjects(svc, 'instrument.reference', series, first.id, limit=2)
    assert len(page['items']) == 2 and page['next_after']
    release_records(session, setup_records(session, [dict(ts_code='NEW', csname='Later')]), first)
    second = subjects(svc, 'instrument.reference', series, first.id, after=page['next_after'], limit=2)
    assert len(second['items']) == 1 and second['next_after'] is None
    assert {r['source_key'] for r in page['items'] + second['items']} == {'T0', 'T1', 'T2'}
    assert not subjects(svc, 'instrument.reference', series, first.id, search='%')['items']
    with pytest.raises(FoundationError, match='语义不兼容'):
        subjects(svc, 'instrument.reference', 'wrong-series', first.id)
