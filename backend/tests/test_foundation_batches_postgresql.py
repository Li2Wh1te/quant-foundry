"""M6 uses real multi-work publication in an isolated PostgreSQL database."""
import json
from sqlalchemy import select
from app.data_foundation.inputs import seal_input_set
from app.data_foundation.governance import create_multi_governance
from app.data_foundation.publication import stage_decisions, publish
from app.data_foundation.work import claim
from app.data_foundation.work_models import CandidateManifest, Decision
from app.data_foundation.views import process_detail
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_daily_postgresql import setup_daily, query


def multi(session, fixtures):
    manifests = [session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == f[0].id)) for f in fixtures]
    inputs = seal_input_set(session, [dict(manifest_id=m.id, role='eligible', reason='独立标的已核验输入') for m in manifests])
    work = create_multi_governance(session, input_set_id=inputs.id, execution_id=fixtures[0][1].id, policy_id=fixtures[0][2].id)
    return work, manifests


def test_two_normalizations_publish_and_query_all_subjects(session):
    fixtures = [setup_daily(session), setup_daily(session)]
    work, manifests = multi(session, fixtures)
    lease = claim(session, work_id=work.id)
    release = stage_decisions(session, work.id, lease.lease_epoch)
    publish(session, release.id, lease.lease_epoch)
    result = query(session, release, fixtures[0][3], subjects=[f[3] for f in fixtures])
    assert result['state'] == 'available' and len(result['items']) == 4
    assert result['scope_summary']['expected_business_keys'] == 4
    decisions = session.scalars(select(Decision).where(Decision.work_id == work.id)).all()
    assert {d.candidate_manifest_id for d in decisions} == {m.id for m in manifests}
    assert {d.candidate_input_set_id for d in decisions} == {work.candidate_input_set_id}
    detail = process_detail(session, work)
    assert {i['work_id'] for i in detail['input_works']} == {str(f[0].id) for f in fixtures}


def test_batch_shadow_gate_and_multiple_real_releases(session):
    import pytest
    from uuid import uuid4
    from app.data_foundation.canonical import FoundationError
    from app.data_foundation.batches import batch_document,register_batch,attach_work,set_controls
    from app.data_foundation.reconciliation import reconcile_batch
    from app.data_foundation.governance import create_governance
    from app.data_foundation.work import finish_batch
    from app.data_foundation.work_models import Head,Release
    from app.data_foundation.batch_models import BatchWork,ReleaseContribution
    fixtures=[setup_daily(session),setup_daily(session)]
    first=fixtures[0][0]
    document,fingerprint=batch_document(session,scope_key=first.scope_key,source_ids=[f[0].source_ref_id for f in fixtures],
        start='2026-06-05',end='2026-06-08',purpose='shadow')
    batch=register_batch(session,event_key=uuid4().hex,document=document,expected_hash=fingerprint)
    for f in fixtures:attach_work(session,batch.id,f[0].id)
    assert reconcile_batch(session,batch.id).status=='pending'
    b1=create_governance(session,normalization_id=first.id,execution_id=fixtures[0][1].id,policy_id=fixtures[0][2].id)
    attach_work(session,batch.id,b1.id)
    assert claim(session,work_id=b1.id) is None
    set_controls(session,batch.id,pause_a=True,pause_b=False)
    lease=claim(session,work_id=b1.id)
    r1=stage_decisions(session,b1.id,lease.lease_epoch)
    with pytest.raises(FoundationError,match='影子'):publish(session,r1.id,lease.lease_epoch)
    assert session.get(Head,first.scope_key) is None
    finish_batch(session,b1,status='awaiting_publication')
    # The second approved source is still waiting for governance; it must not be
    # hidden by a successful first output when authorizing the whole batch.
    with pytest.raises(FoundationError,match='待处理'):set_controls(session,batch.id,pause_a=False,pause_b=False,allow_publish=True)
    # A separate candidate-reuse batch may publish the first output explicitly.
    doc,fp=batch_document(session,scope_key=first.scope_key,source_ids=[first.source_ref_id],
        start='2026-06-05',end='2026-06-08',purpose='maintenance')
    reused=register_batch(session,event_key=uuid4().hex,document=doc,expected_hash=fp)
    attach_work(session,reused.id,first.id)
    assert session.get(BatchWork,(reused.id,first.id)) is not None
    # Create a real second stage in the original batch with the same fixed
    # parent-less input, then reconcile both sealed outputs before approval.
    b2=create_governance(session,normalization_id=fixtures[1][0].id,execution_id=fixtures[1][1].id,policy_id=fixtures[1][2].id)
    attach_work(session,batch.id,b2.id)
    lease=claim(session,work_id=b2.id)
    r2=stage_decisions(session,b2.id,lease.lease_epoch)
    finish_batch(session,b2,status='awaiting_publication')
    assert reconcile_batch(session,batch.id).status=='explained'
    set_controls(session,batch.id,pause_a=False,pause_b=False,allow_publish=True)
    lease=claim(session,work_id=b1.id)
    publish(session,r1.id,lease.lease_epoch)
    # Concurrent parent-less outputs cannot both advance the same head.
    lease=claim(session,work_id=b2.id)
    assert publish(session,r2.id,lease.lease_epoch) is None
    assert b2.status=='superseded'
    assert session.scalar(select(ReleaseContribution).where(ReleaseContribution.release_id==r1.id)) is not None
    # Replan the superseded publication unit against the newly published head;
    # attaching the new work invalidates approval and demands a fresh settlement.
    b3=create_governance(session,normalization_id=fixtures[1][0].id,execution_id=fixtures[1][1].id,
        policy_id=fixtures[1][2].id,parent_release_id=r1.id,expected_head_revision=1)
    attach_work(session,batch.id,b3.id)
    lease=claim(session,work_id=b3.id)
    r3=stage_decisions(session,b3.id,lease.lease_epoch)
    finish_batch(session,b3,status='awaiting_publication')
    set_controls(session,batch.id,pause_a=False,pause_b=False,allow_publish=True)
    lease=claim(session,work_id=b3.id)
    publish(session,r3.id,lease.lease_epoch)
    assert len(session.scalars(select(Release).where(Release.work_id.in_([b1.id,b2.id,b3.id]),Release.status=='published')).all())==2
    result=query(session,r3,fixtures[0][3],subjects=[f[3] for f in fixtures])
    assert result['state']=='available' and len(result['items'])==4
    from app.data_foundation.maintenance import impact
    affected=impact(session,[{'kind':'source_ref_id','id':first.source_ref_id}])
    assert {t['release_id'] for t in affected['targets']}=={str(r1.id),str(r3.id)}


def test_multi_report_never_mixes_whole_objects(session):
    from tests.test_foundation_holdings_postgresql import setup_reports
    from app.data_foundation.holding_models import OfficialReport,OfficialReportMember
    from uuid import uuid4
    first=setup_reports(session,subject='FIRST.OF')
    fixtures=[first,setup_reports(session,subject='SECOND.OF',identities=(uuid4(),first[4]))]
    work,manifests=multi(session,fixtures)
    release=None
    while release is None:
        lease=claim(session,work_id=work.id)
        release=stage_decisions(session,work.id,lease.lease_epoch)
    publish(session,release.id,lease.lease_epoch)
    reports=session.scalars(select(OfficialReport).join(Decision,Decision.id==OfficialReport.decision_id).where(Decision.work_id==work.id)).all()
    assert len(reports)==2
    from app.data_foundation.reconciliation import verify_candidate_values
    from app.data_foundation.source_refs import read_source
    from app.data_foundation.work_models import Candidate
    for fixture in fixtures:
        candidates=session.scalars(select(Candidate).where(Candidate.work_id==fixture[0].id)).all()
        assert not verify_candidate_values(session,fixture[0],candidates,read_source(session,fixture[0].source_ref_id))
    for report in reports:
        members=session.scalars(select(OfficialReportMember).where(OfficialReportMember.official_id==report.id)).all()
        assert len(members)==report.member_count
        expected=next(f[4] for f in fixtures if f[3]==report.fund_share_id)
        assert all(m.member_instrument_id==expected for m in members)


def test_sealed_input_set_rejects_late_entries_and_mutation(session):
    import pytest
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError
    from app.data_foundation.batch_models import CandidateInputEntry
    fixtures=[setup_daily(session),setup_daily(session)]
    work,manifests=multi(session,fixtures)
    with pytest.raises(DBAPIError):
        with session.begin_nested():session.execute(update(CandidateInputEntry).where(CandidateInputEntry.input_set_id==work.candidate_input_set_id).values(reason='changed'))
    third=setup_daily(session)
    m=session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==third[0].id))
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.add(CandidateInputEntry(input_set_id=work.candidate_input_set_id,manifest_id=m.id,manifest_hash=m.manifest_hash,role='eligible',reason='late'))
            session.flush()


def test_signed_batch_pages_bound_identity_and_scope(session):
    import pytest
    from uuid import uuid4
    from app.core.auth import AuthenticatedPrincipal
    from app.data_foundation.service import FoundationService
    from app.data_foundation.batches import batch_document,register_batch
    from app.data_foundation.batch_views import list_batches,reconciliation_page
    from app.data_foundation.canonical import FoundationError
    f=setup_daily(session)
    doc,fp=batch_document(session,scope_key=f[0].scope_key,source_ids=[f[0].source_ref_id],start='2026-06-05',end='2026-06-08',purpose='shadow')
    for _ in range(21):register_batch(session,event_key=uuid4().hex,document=doc,expected_hash=fp)
    svc=FoundationService(session,lambda:AuthenticatedPrincipal('one'),'server-key')
    page=list_batches(svc,f[0].scope_key,limit=20)
    assert len(page['items'])==20 and page['next_cursor']
    second=list_batches(svc,f[0].scope_key,limit=20,cursor=page['next_cursor'])
    assert len(second['items'])==1
    assert not set(i['id'] for i in page['items']).intersection(i['id'] for i in second['items'])
    other=FoundationService(session,lambda:AuthenticatedPrincipal('two'),'server-key')
    with pytest.raises(FoundationError):list_batches(other,f[0].scope_key,limit=20,cursor=page['next_cursor'])
    with pytest.raises(FoundationError):list_batches(svc,'other-scope',limit=20,cursor=page['next_cursor'])


def test_source_revision_change_invalidates_plan_and_can_replan(session):
    from tests.test_foundation_holdings_postgresql import setup_reports
    from app.data_foundation.source_refs import read_source,register_observation
    from app.data_foundation.work import create_work
    from app.data_foundation.holding_work import normalize_batch
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation,TonghuashunCollectionState as State
    from app.data_ingestion.tonghuashun.contracts import exact_json,content_hash
    from app.data_foundation.catalog import now
    fixture=setup_reports(session)
    original=fixture[0]
    envelope=read_source(session,original.source_ref_id)[0]
    observation=Observation(dataset='fund_stock_history',subject='TEST.OF',variant='default',observed_at=now(),
        request_json=exact_json(envelope['requests']),data_json=exact_json(envelope['data']),content_hash=content_hash(envelope['data']),row_count=1,chain_depth=0)
    session.add(observation);session.flush()
    state=State(dataset=observation.dataset,subject=observation.subject,variant=observation.variant,
        revision=1,observation_id=observation.id,status='succeeded',attempted_at=now())
    session.add(state);session.flush()
    ref=register_observation(session,observation.id,fixture[1].id)
    origin=create_work(session,kind='A',contract_id=original.contract_id,execution_id=original.execution_id,
        dependency_id=original.dependency_id,parameters=json.loads(original.parameters_json),source_ref_id=ref.id)
    lease=claim(session,work_id=origin.id);normalize_batch(session,origin.id,lease.lease_epoch)
    work,_=multi(session,[(origin,*fixture[1:])])
    lease=claim(session,work_id=work.id);release=stage_decisions(session,work.id,lease.lease_epoch)
    state.revision=2;session.flush()
    assert publish(session,release.id,lease.lease_epoch) is None
    assert work.status=='superseded'
    replacement,_=multi(session,[(origin,*fixture[1:])])
    assert replacement.id!=work.id


def test_recent_scan_does_not_replace_full_late_commit_reconciliation(session):
    from app.data_foundation.intake import register_scope,set_paused,start_scan,scan_page
    from app.data_foundation.intake_models import IntakeItem
    from tests.test_foundation_sources import observations,execution
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
    from sqlalchemy.orm import Session
    from uuid import UUID,uuid4
    from datetime import timedelta
    from app.data_foundation.catalog import now
    from app.data_ingestion.tonghuashun.contracts import exact_json,content_hash
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    subject='late-'+uuid4().hex
    ex=execution(session)
    scope=register_scope(session,dataset='etf_daily',subject=subject,variant='default',
        selector={'start':'2026-06-24','end':'2026-09-15'},decoder_id=ex.id)
    set_paused(session,scope.id,False)
    data={'item':[{'date_ms':1,'close':'1.2'}]}
    first=Observation(id=UUID('8'+uuid4().hex[1:]),dataset='etf_daily',subject=subject,variant='default',observed_at=now(),
        request_json='{}',data_json=exact_json(data),content_hash=content_hash(data),row_count=1,chain_depth=0)
    session.add(first);session.flush()
    scan=start_scan(session,scope.id,uuid4().hex)
    scope_id,scan_id=scope.id,scan.id
    session.commit()
    entered,commit=Event(),Event()
    def writer():
        with Session(session.bind) as other:
            row=Observation(id=UUID('0'+uuid4().hex[1:]),dataset='etf_daily',subject=subject,variant='default',observed_at=now()-timedelta(days=3),
                request_json='{}',data_json=exact_json(data),content_hash=content_hash(data),row_count=1,chain_depth=0)
            other.add(row);other.flush();entered.set()
            assert commit.wait(5)
            other.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(writer)
        assert entered.wait(5)
        assert scan_page(session,scan_id)['seen']==1
        session.commit();commit.set();future.result(timeout=6)
    next_cycle=start_scan(session,scope_id,uuid4().hex)
    assert scan_page(session,next_cycle.id)['registered']==1
    assert next_cycle.seen==2
    session.commit()


def test_batch_http_is_read_only_authenticated_and_scope_checked(session):
    import asyncio
    from uuid import uuid4
    from app.main import create_app
    from app.core.config import get_settings
    from app.data_foundation.router import snapshot_session
    from app.data_foundation.batches import batch_document,register_batch
    from sqlalchemy import event
    f=setup_daily(session)
    doc,fp=batch_document(session,scope_key=f[0].scope_key,source_ids=[f[0].source_ref_id],start='2026-06-05',end='2026-06-08',purpose='shadow')
    batch=register_batch(session,event_key=uuid4().hex,document=doc,expected_hash=fp)
    app=create_app(get_settings());app.dependency_overrides[snapshot_session]=lambda:session
    async def get(path,query='',authenticated=True):
        sent=False;messages=[]
        async def receive():
            nonlocal sent
            if sent:await asyncio.Future()
            sent=True;return {'type':'http.request','body':b'','more_body':False}
        async def send(message):messages.append(message)
        headers=[(b'authorization',('Bearer '+get_settings().api_token.get_secret_value()).encode())] if authenticated else []
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET','scheme':'http',
            'path':path,'raw_path':path.encode(),'query_string':query.encode(),'headers':headers,
            'client':('test',1234),'server':('test',80),'root_path':''},receive,send)
        status=next(m['status'] for m in messages if m['type']=='http.response.start')
        body=json.loads(b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body'))
        return status,body
    def deny_write(connection,cursor,statement,*args):
        assert statement.lstrip().split()[0].upper() not in ('INSERT','UPDATE','DELETE')
    event.listen(session.bind,'before_cursor_execute',deny_write)
    try:
        path='/api/admin/data-foundation'
        status,body=asyncio.run(get(path+'/datasets/market.bar.daily/batches'))
        assert status==200 and str(batch.id) in [i['id'] for i in body['items']]
        assert asyncio.run(get(path+'/batches/'+str(batch.id),authenticated=False))[0]==401
        assert asyncio.run(get(path+'/batches/'+str(batch.id),'dataset_id=fund.holdings_report'))[0]==422
        status,body=asyncio.run(get(path+'/batches/'+str(batch.id)+'/reconciliation'))
        assert status==200 and body['status']=='not_reconciled' and body['total'] is None
        status,body=asyncio.run(get(path+'/datasets/fund.holdings_report/intake'))
        assert status==200 and body['items']==[]
    finally:event.remove(session.bind,'before_cursor_execute',deny_write)


def test_maintenance_rejects_oversized_closure_without_partial_plan(session, monkeypatch):
    import pytest
    from app.data_foundation import maintenance
    from app.data_foundation.canonical import FoundationError
    fixture = setup_daily(session)
    monkeypatch.setattr(maintenance, 'MAX_IMPACT_ROWS', 0)
    with pytest.raises(FoundationError, match='上限'):
        maintenance.impact(session, [{'kind':'source_ref_id','id':fixture[0].source_ref_id}])
