"""Report adapters exercise shared work, immutable values and atomic release."""
from datetime import date, datetime, timezone
from uuid import uuid4
from decimal import Decimal
import json
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_sources import execution
from tests.test_foundation_holdings import fixture, receipts
from app.data_foundation.catalog import register_dependencies
from app.data_foundation.contracts import register_holdings_catalog
from app.data_foundation.identity import register_report_binding
from app.data_foundation.source_refs import register_baseline
from app.data_foundation.work import create_work, claim
from app.data_foundation.holding_work import (DOMAIN_KEY, domain_hash, normalize_batch,
    create_governance, stage_decisions, validate_release)
from app.data_foundation.holdings import DATASET, SERIES
from app.data_foundation.holding_models import OfficialReport, OfficialReportMember, ReportBlockMember
from app.data_foundation.work_models import Candidate, Head, Release, Work
from app.data_foundation.publication import publish
from app.data_foundation.canonical import FoundationError


def setup_reports(session, *, data=None, unresolved=False, source='tonghuashun', identities=None, subject='TEST.OF'):
    ex = execution(session)
    contract, policy = register_holdings_catalog(session)
    fund_id, stock_id = identities or (uuid4(), uuid4())
    key = uuid4().hex
    rows = [{'source_code': full, 'member_code': code, 'asset_class': kind, 'instrument_id': str(iid)}
        for full, code, kind, iid in [(subject, subject, 'fund_share', fund_id), ('123456.SH', '123456', 'stock', stock_id)]]
    directory = register_baseline(session, source=source, dataset='report_identity_directory', scope={},
        rows=rows, observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key=key)
    bindings = []
    for r in rows:
        if unresolved and r['asset_class'] == 'stock':
            continue
        iid = fund_id if r['asset_class'] == 'fund_share' else stock_id
        binding = register_report_binding(session, source_ref_id=directory.id, source_code=r['source_code'],
            member_code=r['member_code'], asset_class=r['asset_class'], instrument_id=iid,
            valid_from=date(2025, 1, 1), valid_to=date(2026, 1, 1), binding_version=key,
            evidence={'reviewer': 'isolated-test', 'reference': key, 'valid_from': '2025-01-01',
                'valid_to': '2026-01-01', 'source_code': r['source_code'], 'asset_class': r['asset_class']})
        bindings.append(binding)
    dep = register_dependencies(session, [{'binding_id': b.id, 'purpose': 'report-identity'} for b in bindings]
                                + [{'source_ref_id': directory.id, 'purpose': 'directory'}])
    native = data if data is not None else fixture()
    if source == 'isolated-second-source':
        # A second native envelope uses different collection and member names;
        # SourceRef retains this raw shape before the fixture reader maps it.
        native = {'periods': native['report_directory']['item'], 'reports': [
            {'key': r['report_key'], 'period': r['report'], 'positions': r['data']['item']} for r in native['item']]}
    source_ref = register_baseline(session, source=source, dataset='fund_stock_history', scope={},
        rows=[{'subject': subject, 'data': native, 'requests': [{**r, 'parameters': {**r['parameters'], 'thscode': subject}} for r in receipts()]}],
        observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key=key)
    params = dict(dataset=DATASET, major=1, profile='default', series=SERIES,
        start='2025-01-01', end='2025-03-31', domain=DOMAIN_KEY, domain_hash=domain_hash(), report_keys=['2025-03-31:quarter'])
    work = create_work(session, kind='A', contract_id=contract.id, execution_id=ex.id,
        dependency_id=dep.id, parameters=params, source_ref_id=source_ref.id)
    c = claim(session, work_id=work.id)
    normalize_batch(session, work.id, c.lease_epoch)
    return work, ex, policy, fund_id, stock_id


def release_reports(session, f, parent=None):
    head = session.get(Head, f[0].scope_key)
    work = create_governance(session, normalization_id=f[0].id, execution_id=f[1].id, policy_id=f[2].id,
        parent_release_id=parent.id if parent else None, expected_head_revision=head.revision if head else 0)
    release = None
    while release is None:
        c = claim(session, work_id=work.id)
        release = stage_decisions(session, work.id, c.lease_epoch)
    publish(session, release.id, work.lease_epoch)
    return release


def test_report_persists_same_version_members_and_reuses_shared_release(session):
    f = setup_reports(session)
    release = release_reports(session, f)
    validate_release(session, release)
    assert release.status == 'published'
    head = session.scalar(select(OfficialReport))
    members = session.scalars(select(OfficialReportMember).where(OfficialReportMember.official_id == head.id)
                              .order_by(OfficialReportMember.member_ordinal)).all()
    assert len(members) == head.member_count == 2
    assert members[0].hold_ratio == Decimal('0.0431')
    assert members[0].market_value is None
    assert session.get(Head, f[0].scope_key).release_id == release.id
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(text('UPDATE foundation_report_official_members SET hold_ratio=1'))


def test_member_identity_failure_blocks_entire_report(session):
    f = setup_reports(session, unresolved=True)
    candidate = session.scalar(select(Candidate).where(Candidate.work_id == f[0].id))
    assert candidate.readiness == 'quarantined'
    release = release_reports(session, f)
    assert session.scalar(select(OfficialReport)) is None
    assert session.scalar(select(ReportBlockMember)).state == 'blocked'
    assert release.status == 'published'


def test_replacement_never_mixes_old_members(session):
    old = setup_reports(session)
    first = release_reports(session, old)
    data = fixture();data['item'][0]['data']['item'].pop()
    data['item'][0]['data']['item'][0]['hold_ratio'] = '8.2'
    new = setup_reports(session, data=data, identities=old[3:])
    second = release_reports(session, new, first)
    validate_release(session, first);validate_release(session, second)
    objects = session.scalars(select(OfficialReport).order_by(OfficialReport.created_at)).all()
    assert [r.member_count for r in objects] == [2, 1]
    assert second.parent_id == first.id


def install_second_reader(monkeypatch):
    from app.data_foundation.holding_work import SOURCE_READERS
    def reader(native):
        return {'report_directory': {'item': native['periods']}, 'item': [
            {'report_key': r['key'], 'report': r['period'], 'data': {'item': r['positions']}} for r in native['reports']]}
    monkeypatch.setitem(SOURCE_READERS, 'isolated-second-source', reader)


def test_second_source_cannot_change_default_admission(session, monkeypatch):
    install_second_reader(monkeypatch)
    f = setup_reports(session, source='isolated-second-source')
    release_reports(session, f)
    assert session.scalar(select(OfficialReport)) is None
    assert session.scalar(select(ReportBlockMember)).state == 'gap'


def service(session):
    from app.core.auth import AuthenticatedPrincipal
    from app.data_foundation.service import FoundationService
    return FoundationService(session, lambda: AuthenticatedPrincipal(owner_scope='test'), 'a' * 64)


def request(f, release, **changes):
    from app.data_foundation.holding_query import ReportRequirement
    return ReportRequirement(**(dict(dataset_id=DATASET, subjects=[f[3]],
        business_range={'from': '2025-01-01', 'to': '2025-03-31'}, release=release.id) | changes))


def test_report_query_fields_pages_and_new_head_do_not_change_fixed_values(session):
    from app.data_foundation.service import PageRequest
    f = setup_reports(session);release = release_reports(session, f)
    svc = service(session)
    check = json.loads(svc.check_capability(request(f, release)))
    assert check['state'] == 'available'
    objects = json.loads(svc.query_official(PageRequest(resolution_token=check['resolution_token'])))
    key = objects['items'][0]['target_key']
    req = request(f, release, selected_report=key, page_size=1)
    first = json.loads(svc.query_official(req))
    second = json.loads(svc.query_official(PageRequest(resolution_token=first['resolution_token'], cursor=first['next_cursor'], page_size=1)))
    assert first['items'][0]['hold_ratio'] == second['items'][0]['hold_ratio'] == '0.0431'
    assert [first['items'][0]['member_ordinal'], second['items'][0]['member_ordinal']] == [0, 1]
    assert not second['has_more']
    missing = json.loads(svc.query_official(request(f, release, selected_report=key, fields=['market_value'])))
    assert missing['state'] == 'unavailable' and missing['items'] == []


def test_report_token_cannot_bypass_new_issue_and_old_revision_remains_restricted(session):
    from app.data_foundation.service import PageRequest
    from app.data_foundation.holding_issues import record_report_issue
    f = setup_reports(session);release = release_reports(session, f);svc = service(session)
    result = json.loads(svc.query_official(request(f, release)))
    item = result['items'][0]
    token = result['resolution_token']
    issue = record_report_issue(session, scope_key=f[0].scope_key, target=item['target_key'], fund_share_id=f[3],
        period_end=date(2025, 3, 31), fields=['hold_ratio'], state='confirmed', reason='隔离测试错误',
        evidence={'test': True}, official_id=__import__('uuid').UUID(item['official_id']))
    with pytest.raises(FoundationError, match='限制'):
        svc.query_official(PageRequest(resolution_token=token))
    after = json.loads(svc.check_capability(request(f, release)))
    assert after['state'] == 'unavailable'
    record_report_issue(session, scope_key=f[0].scope_key, target=item['target_key'], fund_share_id=f[3],
        period_end=date(2025, 3, 31), fields=['hold_ratio'], state='resolved', reason='隔离测试已修复',
        evidence={'test': True}, official_id=__import__('uuid').UUID(item['official_id']), issue_id=issue.issue_id)
    assert json.loads(svc.check_capability(request(f, release)))['state'] == 'unavailable'


def test_report_snapshot_requires_no_provider_and_rechecks_original_release(session):
    f = setup_reports(session);release = release_reports(session, f);svc = service(session)
    snapshot = json.loads(svc.resolve_snapshot([request(f, release)]))
    from unittest.mock import patch
    with patch('app.data_foundation.source_refs.read_source', side_effect=AssertionError('read cannot replay source')):
        read = json.loads(svc.read_snapshot(snapshot))
    assert read['items'][0]['release_id'] == str(release.id)


def test_withdrawal_is_a_new_release_and_does_not_resurrect_parent(session):
    from app.data_foundation.catalog import register_definition
    f = setup_reports(session);first = release_reports(session, f)
    svc = service(session)
    item = json.loads(svc.query_official(request(f, first)))['items'][0]
    policy = register_definition(session, kind='policy', name='withdraw-test', version='1',
        definition=json.loads(f[2].definition_json) | {'withdrawn_report_keys': {item['target_key']: '证据错误，撤回整份报告'}})
    second = release_reports(session, (f[0], f[1], policy, *f[3:]), first)
    assert json.loads(svc.query_official(request(f, second)))['items'] == []
    assert json.loads(svc.query_official(request(f, first)))['items'][0] == item
    latest = json.loads(svc.describe_dataset(DATASET))
    assert latest['official_count'] == latest['member_count'] == 0
    assert latest['manifest_object_count'] == 1
    from app.data_foundation.views import release_changes
    assert release_changes(session, request(f, first), first.id, second.id, svc.owner)['items'][0]['kind'] == 'withdrawn'


def test_empty_report_is_distinct_from_unknown_and_preserves_snapshot_binding(session):
    from app.data_foundation.service import PageRequest
    data = fixture();data['item'][0]['data']['item'] = []
    f = setup_reports(session, data=data);release = release_reports(session, f);svc = service(session)
    item = json.loads(svc.query_official(request(f, release)))['items'][0]
    assert item['member_count'] == 0 and item['transport_complete'] is True
    req = request(f, release, selected_report=item['target_key'], require_complete=True)
    result = json.loads(svc.query_official(req))
    assert result['request_satisfied'] and result['items'] == []
    assert not json.loads(svc.check_capability(req.model_copy(update={'require_portfolio_complete': True})))['request_satisfied']
    snapshot = json.loads(svc.resolve_snapshot([req]))
    checked = json.loads(svc.read_snapshot(snapshot))['items'][0]
    fetched = json.loads(svc.query_official(PageRequest(resolution_token=checked['resolution_token'])))
    assert fetched['snapshot_hash'] == snapshot['snapshot_hash']


def test_new_head_cannot_change_old_report_token_or_add_late_members(session):
    from app.data_foundation.service import PageRequest
    f = setup_reports(session);first = release_reports(session, f);svc = service(session)
    initial = json.loads(svc.query_official(request(f, first)))
    token = json.loads(svc.check_capability(request(f, first).model_copy(update={'release':'latest'})))['resolution_token']
    data = fixture();data['item'][0]['data']['item'].pop()
    later = setup_reports(session, data=data, identities=f[3:]);second = release_reports(session, later, first)
    assert json.loads(svc.query_official(PageRequest(resolution_token=token)))['items'] == initial['items']
    assert json.loads(svc.query_official(request(f, second)))['items'][0]['member_count'] == 1
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(text('INSERT INTO foundation_report_official_members SELECT official_id, 999, member_instrument_id, binding_id, source_member_code, hold_ratio, market_value, period_change_ratio, rank, field_quality_json FROM foundation_report_official_members LIMIT 1'))
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(text('INSERT INTO foundation_report_candidate_members SELECT candidate_id, 999, member_instrument_id, binding_id, source_member_code, hold_ratio, market_value, period_change_ratio, rank, field_quality_json FROM foundation_report_candidate_members LIMIT 1'))


def test_second_source_reuses_candidate_after_explicit_admission(session, monkeypatch):
    install_second_reader(monkeypatch)
    from app.data_foundation.catalog import register_definition
    f = setup_reports(session, source='isolated-second-source');first = release_reports(session, f)
    policy = register_definition(session, kind='policy', name='second-source-review', version='1',
        definition=json.loads(f[2].definition_json) | {'source_order': ['isolated-second-source']})
    second = release_reports(session, (f[0], f[1], policy, *f[3:]), first)
    result = json.loads(service(session).query_official(request(f, second)))
    assert result['request_satisfied'] and result['items'][0]['member_count'] == 2
    assert len(session.scalars(select(Candidate).where(Candidate.work_id == f[0].id)).all()) == 1


def test_report_change_comparison_respects_whole_object_and_field_issues(session):
    from app.data_foundation.views import release_changes
    from app.data_foundation.holding_issues import record_report_issue
    f = setup_reports(session);first = release_reports(session, f);svc = service(session)
    data = fixture();data['item'][0]['data']['item'][0]['hold_ratio'] = '4.5'
    later = setup_reports(session, data=data, identities=f[3:]);second = release_reports(session, later, first)
    req = request(f, first)
    change = release_changes(session, req, first.id, second.id, svc.owner)['items'][0]
    assert change['kind'] == 'whole_object_changed'
    record_report_issue(session, scope_key=first.scope_key, target=change['target_key'], fund_share_id=f[3],
        period_end=date(2025, 3, 31), fields=['hold_ratio'], state='confirmed', reason='隔离测试错误', evidence={'test': True})
    compared = release_changes(session, req, first.id, second.id, svc.owner)['items'][0]
    assert compared['kind'] == 'restricted_comparison' and 'before' not in compared
    with pytest.raises(FoundationError, match='业务对象'):
        record_report_issue(session, scope_key=first.scope_key, target='f' * 64, fund_share_id=f[3],
            period_end=date(2025, 3, 31), fields=['hold_ratio'], state='confirmed', reason='无匹配目标', evidence={'test': True})


def test_explicit_retain_reuses_original_whole_report_and_lineage(session):
    from app.data_foundation.catalog import register_definition
    f = setup_reports(session);first = release_reports(session, f);svc = service(session)
    item = json.loads(svc.query_official(request(f, first)))['items'][0]
    policy = register_definition(session, kind='policy', name='retain-test', version='1',
        definition=json.loads(f[2].definition_json) | {'retained_report_keys': {item['target_key']: '已核验，继续保留父报告'}})
    second = release_reports(session, (f[0], f[1], policy, *f[3:]), first)
    validate_release(session, second)
    actual = json.loads(svc.query_official(request(f, second)))['items'][0]
    assert actual['official_id'] == item['official_id']
    assert actual['decision_id'] != item['decision_id']
    assert len(session.scalars(select(OfficialReport)).all()) == 1
    assert json.loads(svc.get_lineage(__import__('uuid').UUID(actual['official_id'])))['candidate_id'] == str(session.scalar(select(Candidate.id).where(Candidate.work_id==f[0].id)))


def test_review_apply_is_hash_bound_idempotent_and_does_not_infer_identities(session):
    from app.data_foundation.holding_commands import ReportReview, review, apply
    from app.data_foundation.models import SourceRef
    f = setup_reports(session)
    source = session.get(SourceRef, f[0].source_ref_id)
    body = dict(source_ref_id=source.id, source_hash=source.content_hash, subject='TEST.OF',
        report_keys=['2025-03-31:quarter'], start='2025-01-01', end='2025-03-31',
        reviewer='isolated-fixture', binding_version='review-fixture', identities=[])
    manifest = ReportReview(**body)
    checked = review(session, manifest)
    assert checked['reports'][0]['readiness'] == 'quarantined'
    with pytest.raises(FoundationError, match='审核摘要'):
        apply(session, manifest, expected_hash='0' * 64, execution_id=f[1].id)
    identities = []
    for code, member, kind, iid in [('TEST.OF', 'TEST.OF', 'fund_share', f[3]), ('123456.SH', '123456', 'stock', f[4])]:
        identities.append(dict(source_code=code, member_code=member, asset_class=kind, instrument_id=iid,
            valid_from='2025-01-01', valid_to='2026-01-01',
            directory_evidence={'thscode': code, 'asset_type': 'fund-otc' if kind == 'fund_share' else 'a-share', 'exchange': 'SH' if kind == 'stock' else None},
            references=[{'url': 'https://example.invalid/isolated.pdf', 'sha256': '1' * 64,
                'location': 'fixture only', 'finding': 'isolated identity evidence, not a real-world claim'}]))
    approved = ReportReview(**(body | {'identities': identities}))
    checked = review(session, approved)
    assert checked['reports'][0]['readiness'] == 'ready'
    first = apply(session, approved, expected_hash=checked['review_hash'], execution_id=f[1].id)
    second = apply(session, approved, expected_hash=checked['review_hash'], execution_id=f[1].id)
    assert first['work_id'] == second['work_id'] and first['binding_ids'] == second['binding_ids']
    with pytest.raises(FoundationError, match='审核摘要'):
        changed = approved.model_copy(update={'reviewer': 'another reviewer'})
        apply(session, changed, expected_hash=checked['review_hash'], execution_id=f[1].id)


def test_report_http_protocol_equals_python_and_rejects_market_fields(session):
    import asyncio
    from app.main import create_app
    from app.core.config import get_settings
    from app.db.session import get_db_session
    f = setup_reports(session);release = release_reports(session, f);svc = service(session)
    req = request(f, release)
    app = create_app(get_settings());app.dependency_overrides[get_db_session] = lambda: session
    async def post(path, body, authenticated=True):
        messages=[];sent=False
        async def receive():
            nonlocal sent
            if sent:await asyncio.Future()
            sent=True
            return {'type':'http.request','body':json.dumps(body).encode(),'more_body':False}
        async def send(message):messages.append(message)
        headers=[(b'content-type', b'application/json')]
        if authenticated:headers.append((b'authorization', ('Bearer '+get_settings().api_token.get_secret_value()).encode()))
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http',
            'path':path,'raw_path':path.encode(),'query_string':b'','headers':headers,
            'client':('test',1234),'server':('test',80),'root_path':''},receive,send)
        status=next(m['status'] for m in messages if m['type']=='http.response.start')
        content=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
        return status,json.loads(content)
    async def verify():
        body=req.model_dump(mode='json',by_alias=True)
        status,value=await post('/api/admin/data-foundation/queries',body)
        assert status==200 and value['items']==json.loads(svc.query_official(req))['items']
        assert (await post('/api/admin/data-foundation/queries',body,False))[0]==401
        assert (await post('/api/admin/data-foundation/queries',body|{'fields':['close']}))[0]==422
        status,snapshot=await post('/api/admin/data-foundation/snapshot-resolutions',{'requests':[body]})
        assert status==200
        status,read=await post('/api/admin/data-foundation/snapshot-reads',snapshot)
        assert status==200 and read['items'][0]['release_id']==str(release.id)
        status,changes=await post('/api/admin/data-foundation/datasets/fund.holdings_report/release-changes',
            body|{'previous_release':str(release.id),'current_release':str(release.id)})
        assert status==200 and changes['items'][0]['kind']=='inherited'
    asyncio.run(verify())


def test_competing_report_publishers_cannot_replace_newer_head(session):
    first = setup_reports(session)
    data = fixture();data['item'][0]['data']['item'].pop()
    second = setup_reports(session, data=data, identities=first[3:])
    staged=[]
    for f in (first,second):
        work=create_governance(session,normalization_id=f[0].id,execution_id=f[1].id,policy_id=f[2].id)
        lease=claim(session,work_id=work.id)
        release=stage_decisions(session,work.id,lease.lease_epoch)
        staged.append((work,release))
    assert session.get(Head,first[0].scope_key) is None
    publish(session,staged[0][1].id,staged[0][0].lease_epoch)
    publish(session,staged[1][1].id,staged[1][0].lease_epoch)
    assert session.get(Head,first[0].scope_key).release_id==staged[0][1].id
    assert staged[1][0].status=='superseded'
    with pytest.raises(FoundationError):
        service(session).query_official(request(first,staged[1][1]))


def test_interruption_rolls_back_whole_report_and_retry_commits_once(session):
    from sqlalchemy import event
    f=setup_reports(session)
    work=create_work(session,kind='A',contract_id=f[0].contract_id,execution_id=f[1].id,
        dependency_id=f[0].dependency_id,source_ref_id=f[0].source_ref_id,
        parameters=json.loads(f[0].parameters_json)|{'end':'2025-04-01'})
    lease=claim(session,work_id=work.id);epoch=lease.lease_epoch
    def interrupt(conn,cursor,statement,*args):
        if statement.startswith('INSERT INTO foundation_report_candidate_members'):
            raise RuntimeError('isolated interruption between head and member insert')
    event.listen(session.bind,'before_cursor_execute',interrupt)
    try:
        with pytest.raises(RuntimeError):
            with session.begin_nested():normalize_batch(session,work.id,epoch)
    finally:event.remove(session.bind,'before_cursor_execute',interrupt)
    assert session.get(Work,work.id).cursor==0
    assert session.scalar(select(Candidate.id).where(Candidate.work_id==work.id)) is None
    normalize_batch(session,work.id,epoch)
    assert session.get(Work,work.id).cursor==1
    assert len(session.scalars(select(Candidate).where(Candidate.work_id==work.id)).all())==1


def test_explicit_missing_report_is_incomplete_and_freshness_uses_query_end(session):
    f = setup_reports(session); release = release_reports(session, f); svc = service(session)
    historical = json.loads(svc.check_capability(request(f, release, max_staleness_days=0)))
    assert historical['request_satisfied']
    stale = json.loads(svc.check_capability(request(f, release, max_staleness_days=0,
        business_range={'from': '2025-01-01', 'to': '2025-04-01'})))
    assert next(r for r in stale['requirements'] if r['id'] == 'freshness')['result'] == 'fail'
    missing = json.loads(svc.check_capability(request(f, release, require_complete=True, report_keys=['0' * 64])))
    assert not missing['request_satisfied']
    assert next(r for r in missing['requirements'] if r['id'] == 'completeness')['result'] == 'fail'


def test_report_comparison_cursor_preserves_whole_object_union(session):
    from app.data_foundation.holding_views import release_changes
    first = setup_reports(session); old = release_reports(session, first)
    second = setup_reports(session, identities=(uuid4(), first[4]), subject='SECOND.OF'); new = release_reports(session, second, old)
    requirement = request(first, new, subjects=[first[3], second[3]])
    page = release_changes(session, requirement, old.id, new.id, lambda: 'owner', limit=1)
    assert page['has_more'] and len(page['items']) == 1
    last = release_changes(session, requirement, old.id, new.id, lambda: 'owner', after=page['next_key'], limit=1)
    assert not last['has_more'] and len(last['items']) == 1
    assert page['items'][0]['target_key'] != last['items'][0]['target_key']
    assert {page['items'][0]['kind'], last['items'][0]['kind']} == {'inherited', 'added'}
