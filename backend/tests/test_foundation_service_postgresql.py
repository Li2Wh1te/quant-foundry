"""M4 invariants against disposable PostgreSQL, never production providers."""
import json
from datetime import date
from unittest.mock import patch
import pytest
from sqlalchemy import event,select,func
from app.core.auth import AuthenticatedPrincipal
from app.data_foundation.service import FoundationService,PageRequest
from app.data_foundation.query import DataRequirement
from app.data_foundation.canonical import FoundationError
from app.data_foundation.quality import record_issue
from app.data_foundation.work_models import OfficialBar
from tests.test_foundation_daily_postgresql import setup_daily,release_daily
from tests.test_foundation_publication_postgresql import session,pytestmark


def prepared(session,**changes):
    fixture=setup_daily(session,**changes);release=release_daily(session,fixture)
    request=DataRequirement(dataset_id='market.bar.daily',contract_version='1.0',profile_id='default',
        semantic_series_id=json.loads(fixture[0].parameters_json)['series'],subjects=[fixture[3]],
        business_range={'from':'2026-06-05','to':'2026-06-08'},fields=['close','turnover'],release=release.id)
    svc=FoundationService(session,lambda:AuthenticatedPrincipal('test'),'server-key')
    return svc,request,fixture,release


def test_pagination_invariance_and_legacy(session):
    svc,req,f,release=prepared(session)
    full=json.loads(svc.query_official(req));assert len(full['items'])==2
    check=json.loads(svc.check_capability(req));assert check['items']==[] and check['resolution_token']
    first=json.loads(svc.query_official(PageRequest(resolution_token=check['resolution_token'],page_size=1)))
    assert first['has_more'] and first['total_readable']==2 and len(first['items'])==1
    second=json.loads(svc.query_official(PageRequest(resolution_token=check['resolution_token'],cursor=first['next_cursor'],page_size=1000)))
    assert first['items']+second['items']==full['items'] and not second['has_more']
    close=json.loads(svc.query_official(req.model_copy(update={'fields':['close']})))
    assert [r['close'] for r in close['items']]==[r['close'] for r in full['items']]
    short=json.loads(svc.query_official(req.model_copy(update={'business_range':req.business_range.model_copy(update={'end':date(2026,6,5)})})))
    assert short['items']==full['items'][:1]


def test_epoch_auth_and_cursor_binding(session):
    svc,req,f,release=prepared(session)
    token=json.loads(svc.check_capability(req))['resolution_token']
    page=json.loads(svc.query_official(PageRequest(resolution_token=token,page_size=1)))
    other=json.loads(svc.check_capability(req.model_copy(update={'fields':['close']})))['resolution_token']
    with pytest.raises(FoundationError,match='游标'):svc.query_official(PageRequest(resolution_token=other,cursor=page['next_cursor']))
    record_issue(session,scope_key=release.scope_key,instrument_id=f[3],start=date(2026,6,5),end=date(2026,6,5),
        fields=['turnover'],state='confirmed',reason='隔离测试问题',evidence={'fixture':True})
    with pytest.raises(FoundationError) as error:svc.query_official(PageRequest(resolution_token=token,cursor=page['next_cursor']))
    assert error.value.code=='READ_CONTEXT_CHANGED'
    with pytest.raises(FoundationError):FoundationService(session,lambda:'untrusted-owner','server-key')
    owner=[AuthenticatedPrincipal('test')]
    changing=FoundationService(session,lambda:owner[0],'server-key');owner[0]=AuthenticatedPrincipal('changed')
    with pytest.raises(FoundationError):changing.check_capability(req)


def test_optional_completeness_preserves_field_and_time_requirements(session):
    svc,req,f,release=prepared(session,missing=True)
    assert json.loads(svc.check_capability(req))['state']=='partial'
    relaxed=req.model_copy(update={'require_complete':False})
    result=json.loads(svc.query_official(relaxed))
    assert result['request_satisfied'] and len(result['items'])==1
    assert next(r for r in result['requirements'] if r['id']=='coverage')['result']=='not_required'
    assert json.loads(svc.check_capability(relaxed.model_copy(update={'fields':['volume']})))['state']=='unavailable'
    assert json.loads(svc.check_capability(relaxed.model_copy(update={'time_mode':'strict_public_pit'})))['state']=='unknown'


def test_snapshots_read_only_without_source_decode_or_value_copy(session):
    svc,req,f,release=prepared(session)
    before=session.scalar(select(func.count()).select_from(OfficialBar))
    def deny(conn,cursor,statement,*args):assert statement.lstrip().split()[0].upper() not in {'INSERT','UPDATE','DELETE'}
    event.listen(session.bind,'before_cursor_execute',deny)
    try:
        with patch('app.data_foundation.source_refs.read_source',side_effect=AssertionError('no source decode')):
            snapshot=json.loads(svc.resolve_snapshot([req]))
            for _ in range(100):
                result=json.loads(svc.read_snapshot(snapshot))
                assert result['items'][0]['release_id']==str(release.id)
                assert result['snapshot_hash']==snapshot['snapshot_hash']
            assert json.loads(svc.query_official(req))['state']=='available'
    finally:event.remove(session.bind,'before_cursor_execute',deny)
    assert session.scalar(select(func.count()).select_from(OfficialBar))==before


def test_check_fetches_null_flags_without_prices(session):
    svc,req,f,release=prepared(session);statements=[]
    def capture(conn,cursor,statement,*args):statements.append(statement)
    event.listen(session.bind,'before_cursor_execute',capture)
    try:svc.check_capability(req)
    finally:event.remove(session.bind,'before_cursor_execute',capture)
    assert not any('foundation_bar_official_revisions.open,' in s for s in statements)
    assert any('IS NULL' in s for s in statements)


def test_pinned_token_survives_new_head_and_new_policy(session):
    from app.data_foundation.catalog import register_definition
    from app.data_foundation.governance import create_governance
    from app.data_foundation.work import claim
    from app.data_foundation.publication import stage_decisions,publish
    svc,req,f,first=prepared(session)
    token=json.loads(svc.check_capability(req.model_copy(update={'release':'latest'})))['resolution_token']
    page=json.loads(svc.query_official(PageRequest(resolution_token=token,page_size=1)))
    a,ex,policy,iid=f
    newpolicy=register_definition(session,kind='policy',name='test-m4-'+str(iid),version='2',definition=json.loads(policy.definition_json))
    work=create_governance(session,normalization_id=a.id,execution_id=ex.id,policy_id=newpolicy.id,
        parent_release_id=first.id,expected_head_revision=1)
    claimed=claim(session,work_id=work.id);second=stage_decisions(session,work.id,claimed.lease_epoch);publish(session,second.id,claimed.lease_epoch)
    result=json.loads(svc.query_official(PageRequest(resolution_token=token,cursor=page['next_cursor'],page_size=1)))
    assert result['release_id']==str(first.id) and len(result['items'])==1
    assert json.loads(svc.check_capability(req.model_copy(update={'release':'latest'})))['release_id']==str(second.id)
    from app.data_foundation.views import release_changes
    changes=release_changes(session,req,first.id,second.id,svc.owner)
    assert {i['kind'] for i in changes['items']}=={'basis_changed'}


def test_frozen_projection_and_wrong_major_are_explicit(session):
    from app.data_foundation.projection import PROJECTIONS
    svc,req,f,release=prepared(session)
    spec=PROJECTIONS[('market.bar.daily','1.0')]
    token=json.loads(svc.check_capability(req))['resolution_token']
    with patch.dict(spec,{'read_status':'frozen'}):
        assert json.loads(svc.query_official(PageRequest(resolution_token=token)))['state']=='available'
        assert json.loads(svc.query_official(req))['state']=='available'
    with patch.dict(spec,{'read_status':'retired'}):
        with pytest.raises(FoundationError) as error:svc.query_official(req)
        assert error.value.code=='CONTRACT_RETIRED'
    with pytest.raises(FoundationError):svc.query_official(req.model_copy(update={'contract_version':'2.0'}))


def test_partial_pages_and_excluded_pages_remain_truthful(session):
    svc,req,f,release=prepared(session,missing=True)
    strict=json.loads(svc.check_capability(req));assert strict['state']=='partial'
    assert json.loads(svc.query_official(PageRequest(resolution_token=strict['resolution_token'])))['items']==[]
    allowed=json.loads(svc.check_capability(req.model_copy(update={'allow_partial':True})))
    result=json.loads(svc.query_official(PageRequest(resolution_token=allowed['resolution_token'],page_size=1)))
    assert not result['request_satisfied'] and len(result['items'])==1 and result['excluded_total']==1


def test_version_comparison_never_exposes_restricted_old_value(session):
    from app.data_foundation.views import release_changes
    svc,req,f,release=prepared(session)
    row=json.loads(svc.query_official(req))['items'][0]
    from uuid import UUID
    record_issue(session,scope_key=release.scope_key,instrument_id=f[3],start=date(2026,6,5),end=date(2026,6,5),
        fields=['turnover'],state='confirmed',official_id=UUID(row['official_id']),reason='隔离错误金额',evidence={'fixture':True})
    result=release_changes(session,req,release.id,release.id,svc.owner)
    assert 'turnover' not in result['items'][0]['before']
    assert 'turnover' not in result['items'][0]['after']
    assert result['items'][0]['before']['close']==row['close']


def test_http_and_python_share_protocol_and_guard(session):
    import asyncio
    from types import SimpleNamespace
    from app.main import create_app
    from app.core.config import get_settings
    from app.db.session import get_db_session
    svc,req,f,release=prepared(session)
    app=create_app(get_settings());app.dependency_overrides[get_db_session]=lambda:session
    async def run():
        class Client:
            async def post(self,path,*,json,headers=None):
                import json as codec
                sent=False;messages=[]
                raw=codec.dumps(json).encode()
                async def receive():
                    nonlocal sent
                    if sent:await asyncio.Future()
                    sent=True
                    return {'type':'http.request','body':raw,'more_body':False}
                async def send(message):messages.append(message)
                scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
                    'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'',
                    'headers':[(b'content-type',b'application/json')]+[(k.lower().encode(),v.encode()) for k,v in (headers or {}).items()],
                    'client':('test',1234),'server':('test',80),'root_path':''}
                await app(scope,receive,send)
                start=next(m for m in messages if m['type']=='http.response.start')
                body=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
                return SimpleNamespace(status_code=start['status'],headers={k.decode():v.decode() for k,v in start['headers']},json=lambda:codec.loads(body))
        client=Client()
        if client:

            headers={'Authorization':'Bearer '+get_settings().api_token.get_secret_value()}
            body=req.model_dump(mode='json',by_alias=True)
            missing=await client.post('/api/admin/data-foundation/snapshot-resolutions',json={'requests':[body]})
            assert missing.status_code==401
            legacy=await client.post('/api/admin/data-foundation/queries',json=body,headers=headers)
            assert legacy.status_code==200 and legacy.json()['items']==json.loads(svc.query_official(req))['items']
            page=await client.post('/api/admin/data-foundation/queries',json={**body,'paging_version':1,'page_size':1},headers=headers)
            assert page.status_code==200 and len(page.json()['items'])==1 and page.json()['next_cursor']
            assert page.headers['cache-control']=='no-store'
            check=await client.post('/api/admin/data-foundation/capability-checks',json=body,headers=headers)
            assert check.status_code==200 and check.json()['items']==[]
            snapshot=await client.post('/api/admin/data-foundation/snapshot-resolutions',json={'requests':[body]},headers=headers)
            assert snapshot.status_code==200
            read=await client.post('/api/admin/data-foundation/snapshot-reads',json=snapshot.json(),headers=headers)
            assert read.status_code==200 and read.json()['items'][0]['release_id']==str(release.id)
    asyncio.run(run())


def test_per_subject_freshness_cannot_hide_missing_subject(session):
    from uuid import uuid4
    svc,req,f,release=prepared(session)
    missing=uuid4()
    checked=json.loads(svc.check_capability(req.model_copy(update={'subjects':[f[3],missing],
        'require_complete':False,'max_staleness_days':0})))
    assert not checked['request_satisfied'] and checked['state']=='partial'
    assert str(missing) in next(r for r in checked['requirements'] if r['id']=='freshness')['scope']['subjects']


def test_composite_snapshot_tamper_and_expired_recheck_original(session):
    svc,req,f,release=prepared(session)
    clock=[100];svc.codec.clock=lambda:clock[0]
    snapshot=json.loads(svc.resolve_snapshot([req,req.model_copy(update={'fields':['close']})]))
    check=json.loads(svc.read_snapshot(snapshot))
    assert len(check['items'])==2
    token=check['items'][0]['resolution_token'];clock[0]=1000
    with pytest.raises(FoundationError) as error:svc.query_official(PageRequest(resolution_token=token))
    assert error.value.code=='RESOLUTION_EXPIRED'
    renewed=json.loads(svc.read_snapshot(snapshot))
    assert renewed['items'][0]['release_id']==str(release.id)
    tampered={**snapshot,'snapshot_hash':'0'*64}
    with pytest.raises(FoundationError):svc.read_snapshot(tampered)


def test_process_uses_upstream_input_and_preserves_current_head(session):
    from app.data_foundation.views import process_detail
    from app.data_foundation.work_models import Work
    svc,req,f,release=prepared(session)
    b=session.get(Work,release.work_id)
    result=process_detail(session,b)
    first=result['steps'][0]
    assert first['detail']['input']['source_ref_id']==f[0].source_ref_id
    assert first['detail']['input']['execution_id']==f[0].execution_id
    a=process_detail(session,f[0])
    assert a['current_release']==str(release.id) and a['output_releases']==[]
    assert a['steps'][-1]['status']=='evidence_missing'


def test_unavailable_request_can_page_all_exclusions(session):
    svc,req,f,release=prepared(session)
    req=req.model_copy(update={'fields':['volume']})
    check=json.loads(svc.check_capability(req))
    assert check['state']=='unavailable' and check['items']==[]
    first=json.loads(svc.query_official(PageRequest(resolution_token=check['resolution_token'],page_size=1)))
    assert first['next_excluded_cursor'] and first['items']==[]
    second=json.loads(svc.query_official(PageRequest(resolution_token=check['resolution_token'],page_size=1,
        excluded_cursor=first['next_excluded_cursor'])))
    assert len(second['excluded'])==1 and second['excluded']!=first['excluded'] and not second['next_excluded_cursor']



def test_read_views_are_read_only_and_redact_runtime_details(session):
    from app.data_foundation.views import process_detail,release_list,release_changes
    from app.data_foundation.router import CandidateInspection,candidate_inspection
    from app.data_foundation.work_models import Work,WorkEvent,Attempt
    from app.data_foundation.canonical import encode
    svc,req,f,release=prepared(session)
    a=f[0]
    # Inject operator-private runtime details only into the disposable fixture.
    from app.data_foundation.catalog import now
    session.add(WorkEvent(created_at=now(),work_id=a.id,sequence=1000,step='input',status='fixed',message='隔离测试：输入已固定。',
        details_json=encode({'raw_error':'private-signed-download-url','checkpoint':2})))
    session.add(Attempt(created_at=now(),work_id=a.id,epoch=1000,outcome='failed',details_json=encode({'password':'private-runtime-secret'})))
    session.flush()
    def deny(conn,cursor,statement,*args):
        assert statement.lstrip().split()[0].upper() not in {'INSERT','UPDATE','DELETE'}
    event.listen(session.bind,'before_cursor_execute',deny)
    try:
        with patch('socket.create_connection',side_effect=AssertionError('no provider network')):
            view=process_detail(session,session.get(Work,release.work_id))
            assert 'private-' not in encode(view)
            assert release_list(session,release.scope_key)['items']
            assert release_changes(session,req,release.id,release.id,svc.owner)['items']
            assert candidate_inspection(CandidateInspection(work_id=a.id,page_size=1),session).status_code==200
            assert json.loads(svc.check_capability(req))['state']=='available'
    finally:event.remove(session.bind,'before_cursor_execute',deny)


def test_failed_work_unknown_total_does_not_replace_asset_head(session):
    from app.data_foundation.work import create_work
    from app.data_foundation.views import process_detail
    svc,req,f,release=prepared(session)
    a=f[0]
    pending=create_work(session,kind='A',contract_id=a.contract_id,execution_id=a.execution_id,
        dependency_id=a.dependency_id,parameters={**json.loads(a.parameters_json),'end':'2026-06-09'},source_ref_id=a.source_ref_id)
    pending.status='failed';pending.total=None;session.flush()
    view=process_detail(session,pending)
    assert view['current_release']==str(release.id) and view['output_releases']==[]
    assert view['counters']['total'] is None and view['status']=='failed'
    assert json.loads(svc.query_official(req))['state']=='available'
