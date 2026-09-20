"""Authenticated, read-only foundation queries and bounded operator evidence."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db_session
from app.data_foundation.models import Definition, SourceRef, Execution
from app.data_foundation.execution import replay_status

router = APIRouter(prefix='/api/admin/data-foundation', tags=['data-foundation'])


@router.get('/datasets')
def datasets(request: Request, session: Session = Depends(get_db_session)):
    from app.data_foundation.query import dataset_view
    return foundation_response(lambda: {'items': [json.loads(service(session,request).describe_dataset()), json.loads(service(session,request).describe_dataset('fund.holdings_report'))]})


@router.get('/source-refs/{ref_id}')
def source_ref(ref_id: UUID, session: Session = Depends(get_db_session)):
    row = session.get(SourceRef, ref_id)
    if row is None:
        raise HTTPException(404, detail={'code': 'SOURCE_UNAVAILABLE', 'message': '固定来源引用不存在。'})
    decoder = session.get(Execution, row.decoder_id)
    return {'id': str(row.id), 'source': row.source, 'dataset': row.dataset, 'subject': row.subject,
        'variant': row.variant, 'representation': row.representation, 'content_hash': row.content_hash,
        'observed_at': row.observed_at, 'observation_id': row.observation_id, 'baseline_id': row.baseline_id,
        'decoder_id': row.decoder_id, 'replay_status': replay_status(session, decoder.id), 'retention_state': 'protected'}


def snapshot_session():
    from app.db.session import get_engine
    with get_engine().connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with connection.begin():
            from sqlalchemy import text
            connection.execute(text('SET TRANSACTION READ ONLY'))
            with Session(bind=connection) as session:
                yield session


@router.get('/processing')
def processing(limit: int = Query(20, ge=1, le=100), session: Session = Depends(snapshot_session)):
    from app.data_foundation.work_models import Work
    from app.data_foundation.views import process_detail
    rows=session.scalars(select(Work).order_by(Work.created_at.desc(),Work.id).limit(limit)).all()
    return {'items':[process_detail(session,row) for row in rows]}


@router.get('/work/{work_id}')
def work_detail(work_id: UUID, session: Session = Depends(snapshot_session)):
    from app.data_foundation.work_models import Work
    from app.data_foundation.views import process_detail
    row=session.get(Work,work_id)
    if row is None:raise HTTPException(404,detail={'code':'WORK_UNAVAILABLE','message':'底座工作不存在。'})
    return process_detail(session,row)


@router.get('/releases/{release_id}')
def release_detail(release_id: UUID, session: Session = Depends(snapshot_session)):
    from app.data_foundation.work_models import Release, BlockRef
    row=session.get(Release,release_id)
    if row is None or row.status!='published':raise HTTPException(404,detail={'code':'RELEASE_UNAVAILABLE','message':'正式发布不存在。'})
    return {'id':row.id,'work_id':row.work_id,'parent_id':row.parent_id,'manifest_hash':row.manifest_hash,
        'published_at':row.published_at,'blocks':[{'partition_key':r.partition_key,'block_id':r.block_id}
            for r in session.scalars(select(BlockRef).where(BlockRef.release_id==row.id).order_by(BlockRef.partition_key))]}


@router.get('/decisions/{decision_id}')
def decision_detail(decision_id: UUID, session: Session = Depends(snapshot_session)):
    import json
    from app.data_foundation.work_models import Decision
    row=session.get(Decision,decision_id)
    if row is None:raise HTTPException(404,detail={'code':'DECISION_UNAVAILABLE','message':'治理决策不存在。'})
    return {'id':row.id,'work_id':row.work_id,'target_key':row.target_key,'action':row.action,
        'candidate_manifest_id':row.candidate_manifest_id,'candidate_input_set_id':row.candidate_input_set_id,'selected_candidate_id':row.selected_candidate_id,
        'parent_official_id':row.parent_official_id,'parent_report_id':row.parent_report_id,'evidence':json.loads(row.evidence_json)}


# Serialize inside the transaction that holds the current issue scope lock.
# Returning a Response avoids a later framework encoder changing Decimal values.
from fastapi import Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from app.core.auth import require_api_token
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.query import DataRequirement
from app.data_foundation.holding_query import ReportRequirement
from app.data_foundation.service import FoundationService, PagedRequirement, PageRequest
from pydantic import Field

def service(session,request):
    settings=request.app.state.settings
    # The bearer credential is known to its holder; sign using the existing
    # server-only cursor secret, domain-separated and bound to credential rotation.
    import hashlib
    secret=settings.cursor_signing_key.get_secret_value()+hashlib.sha256(settings.api_token.get_secret_value().encode()).hexdigest()
    return FoundationService(session,current_auth(request),secret)


def current_auth(request):
    def authenticate():
        header=request.headers.get('authorization','')
        scheme,_,token=header.partition(' ')
        credentials=HTTPAuthorizationCredentials(scheme=scheme,credentials=token) if token else None
        return require_api_token(request,credentials)
    return authenticate


def foundation_response(callback):
    try:
        payload=callback()
        return Response(payload if isinstance(payload,bytes) else encode(payload),media_type='application/json',headers={'Cache-Control':'no-store'})
    except FoundationError as exc:
        status={'INVALID_REQUIREMENT':422,'CONTRACT_RELEASE_MISMATCH':422,'RELEASE_UNAVAILABLE':404,
            'READ_CONTEXT_CHANGED':409,'AUTH_REQUIRED':401,'AUTH_CONTEXT_CHANGED':401,
            'INVALID_RESOLUTION':422,'CURSOR_MISMATCH':422,'RESOLUTION_EXPIRED':410,'CONTRACT_RETIRED':410}.get(exc.code,503)
        raise HTTPException(status,detail={'code':exc.code,'message':str(exc)}) from None


@router.get('/datasets/{dataset_id}')
def describe_dataset(dataset_id: str, request: Request, contract_version: str='1.0', session: Session=Depends(snapshot_session)):
    from app.data_foundation.query import DATASET,dataset_view
    if dataset_id not in (DATASET, 'fund.holdings_report') or contract_version!='1.0':
        raise HTTPException(404,detail={'message':'数据集或契约版本不存在。'})
    return foundation_response(lambda:service(session,request).describe_dataset(dataset_id))


@router.post('/capability-checks')
def capability_check(body: ReportRequirement | DataRequirement, request: Request, session: Session=Depends(get_db_session)):
    from app.data_foundation.query import query_official
    return foundation_response(lambda:service(session,request).check_capability(body))


@router.post('/queries')
def official_query(body: PageRequest | ReportRequirement | PagedRequirement | DataRequirement, request: Request, session: Session=Depends(get_db_session)):
    from app.data_foundation.query import query_official
    return foundation_response(lambda:service(session,request).query_official(body))


@router.get('/revisions/{revision_id}/lineage')
def revision_lineage(revision_id: UUID, session: Session=Depends(snapshot_session)):
    from app.data_foundation.query import lineage
    from app.data_foundation.holding_models import OfficialReport
    if session.get(OfficialReport, revision_id):
        from app.data_foundation.holding_query import lineage
    return foundation_response(lambda:lineage(session,revision_id))


from pydantic import BaseModel
import json


class CandidateInspection(BaseModel):
    model_config={'extra':'forbid'}
    work_id: UUID
    after_occurrence: int | None = Field(default=None,ge=0)
    page_size: int = Field(default=100,ge=1,le=1000)


@router.post('/candidate-inspections')
def candidate_inspection(body: CandidateInspection, session: Session=Depends(snapshot_session)):
    from app.data_foundation.work_models import Work,Candidate,Assessment
    row=session.get(Work,body.work_id)
    if row is None or row.kind!='A':
        raise HTTPException(404,detail={'message':'标准化工作不存在。'})
    statement=select(Candidate,Assessment).join(Assessment,Assessment.id==Candidate.assessment_id).where(Candidate.work_id==row.id)
    if body.after_occurrence is not None:statement=statement.where(Candidate.occurrence>body.after_occurrence)
    rows=session.execute(statement.order_by(Candidate.occurrence).limit(body.page_size+1)).all()
    from sqlalchemy import func
    total=session.scalar(select(func.count()).select_from(Candidate).where(Candidate.work_id==row.id))
    return foundation_response(lambda:{'representation':'candidate','work_id':row.id,'total':total,
        'next_occurrence':rows[body.page_size-1][0].occurrence if len(rows)>body.page_size else None,'items':[
        {'id':c.id,'source_ref_id':c.source_ref_id,'readiness':c.readiness,
         'quality':json.loads(a.results_json)} for c,a in rows[:body.page_size]]})



class SnapshotResolution(BaseModel):
    model_config={'extra':'forbid'}
    requests: list[ReportRequirement | DataRequirement] = Field(min_length=1,max_length=8)


class SnapshotEntry(BaseModel):
    model_config={'extra':'forbid'}
    request: ReportRequirement | DataRequirement
    manifest_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    projection_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class SnapshotRead(BaseModel):
    model_config={'extra':'forbid'}
    schema_version: int = Field(alias="schema",ge=1,le=1)
    entries: list[SnapshotEntry] = Field(min_length=1,max_length=8)
    snapshot_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.post('/snapshot-resolutions')
def snapshot_resolution(body: SnapshotResolution, request: Request, session: Session=Depends(get_db_session)):
    return foundation_response(lambda:service(session,request).resolve_snapshot(body.requests))


@router.post('/snapshot-reads')
def snapshot_read(body: SnapshotRead, request: Request, session: Session=Depends(get_db_session)):
    return foundation_response(lambda:service(session,request).read_snapshot(body.model_dump(mode='json',by_alias=True)))


@router.get('/datasets/{dataset_id}/processing')
def dataset_processing(dataset_id: str, request: Request, work_id: UUID | None=None,
                       before: UUID | None=None, limit: int=Query(20,ge=1,le=100), session: Session=Depends(snapshot_session)):
    from app.data_foundation.work_models import Work
    from app.data_foundation.views import process_detail
    from app.data_foundation.work import scope_key
    from app.data_foundation.tushare import SERIES
    if dataset_id not in ('market.bar.daily', 'fund.holdings_report'):raise HTTPException(404,detail={'message':'数据集不存在。'})
    if dataset_id == 'fund.holdings_report':
        from app.data_foundation.holdings import SERIES
    scope=scope_key(dataset_id,1,'default',SERIES)
    statement=select(Work).where(Work.scope_key==scope)
    if work_id:statement=statement.where(Work.id==work_id)
    if before:
        anchor=session.get(Work,before)
        if anchor is None or anchor.scope_key!=scope:raise HTTPException(422,detail={'message':'工作分页位置无效。'})
        from sqlalchemy import tuple_
        statement=statement.where(tuple_(Work.created_at,Work.id)<(anchor.created_at,anchor.id))
    rows=session.scalars(statement.order_by(Work.created_at.desc(),Work.id.desc()).limit(limit+1)).all()
    return foundation_response(lambda:service(session,request).finish({'items':[process_detail(session,w) for w in rows[:limit]],
        'next_cursor':str(rows[limit-1].id) if len(rows)>limit else None}))


@router.get('/datasets/{dataset_id}/releases')
def dataset_releases(dataset_id: str, request: Request, before: UUID | None=None,
                     limit: int=Query(20,ge=1,le=100),session: Session=Depends(snapshot_session)):
    from app.data_foundation.views import release_list
    from app.data_foundation.work import scope_key
    from app.data_foundation.tushare import SERIES
    if dataset_id not in ('market.bar.daily', 'fund.holdings_report'):raise HTTPException(404,detail={'message':'数据集不存在。'})
    if dataset_id == 'fund.holdings_report':
        from app.data_foundation.holdings import SERIES
    return foundation_response(lambda:service(session,request).finish(release_list(session,scope_key(dataset_id,1,'default',SERIES),before,limit)))


class ChangeRequest(DataRequirement):
    previous_release: UUID
    current_release: UUID
    cursor: str | None = Field(default=None,max_length=2048)
    page_size: int = Field(default=50,ge=1,le=1000)


class ReportChangeRequest(ReportRequirement):
    previous_release: UUID
    current_release: UUID
    cursor: str | None = Field(default=None,max_length=2048)


@router.post('/datasets/{dataset_id}/release-changes')
def changes(dataset_id: str, body: ReportChangeRequest | ChangeRequest, request: Request, session: Session=Depends(get_db_session)):
    from app.data_foundation.views import release_changes
    from app.data_foundation.canonical import digest
    def read():
        if dataset_id!=body.dataset_id:raise FoundationError('INVALID_REQUIREMENT','数据集与请求不匹配。')
        svc=service(session,request)
        normalized=body.model_dump(mode='json',by_alias=True,exclude={'cursor','page_size'})
        fingerprint=digest('changes-v1',normalized)
        after=None;expected=None
        if body.cursor:
            payload=svc.codec.read(body.cursor,'changes')
            if payload['fingerprint']!=fingerprint or payload['owner']!=digest('owner-v1',svc.owner()):
                raise FoundationError('CURSOR_MISMATCH','变化游标与当前请求不匹配。')
            after=payload['after'];expected=payload['epoch']
        result=release_changes(session,body,body.previous_release,body.current_release,svc.owner,after=after,limit=body.page_size)
        if expected is not None and expected!=result['issue_state_version']:
            raise FoundationError('READ_CONTEXT_CHANGED','版本变化的当前问题限制已改变，请重新读取。')
        result['next_cursor']=svc.codec.sign('changes',{'fingerprint':fingerprint,'after':result['next_key'],
            'epoch':result['issue_state_version'],'owner':digest('owner-v1',svc.owner())}) if result['next_key'] else None
        return svc.finish(result)
    return foundation_response(read)


@router.get('/datasets/{dataset_id}/batches')
def batches(dataset_id: str, request: Request, cursor: str | None = Query(None,max_length=4096),
            limit: int = Query(50,ge=20,le=100), session: Session = Depends(snapshot_session)):
    from app.data_foundation.batch_views import list_batches
    from app.data_foundation.work import scope_key
    from app.data_foundation.tushare import SERIES
    from app.data_foundation.holdings import SERIES as REPORT_SERIES
    if dataset_id not in ('market.bar.daily','fund.holdings_report'):
        raise HTTPException(404,detail={'code':'DATASET_UNAVAILABLE','message':'数据集不存在。'})
    scope=scope_key(dataset_id,1,'default',REPORT_SERIES if dataset_id=='fund.holdings_report' else SERIES)
    return foundation_response(lambda: service(session,request).finish(list_batches(service(session,request),scope,cursor=cursor,limit=limit)))


@router.get('/batches/{batch_id}')
def batch_detail(batch_id: UUID, request: Request, dataset_id: str | None = None, session: Session = Depends(snapshot_session)):
    from app.data_foundation.batch_models import Batch
    from app.data_foundation.batch_views import batch_summary
    row=session.get(Batch,batch_id)
    if row is None:raise HTTPException(404,detail={'code':'BATCH_UNAVAILABLE','message':'业务批次不存在。'})
    if dataset_id:
        from app.data_foundation.work import scope_key
        from app.data_foundation.tushare import SERIES
        from app.data_foundation.holdings import SERIES as REPORT_SERIES
        if dataset_id not in ('market.bar.daily','fund.holdings_report') or row.scope_key!=scope_key(dataset_id,1,'default',REPORT_SERIES if dataset_id=='fund.holdings_report' else SERIES):
            raise HTTPException(422,detail={'code':'SCOPE_MISMATCH','message':'所选批次不属于当前数据集。'})
    return foundation_response(lambda: service(session,request).finish(batch_summary(session,row)))


@router.get('/batches/{batch_id}/reconciliation')
def batch_reconciliation(batch_id: UUID, request: Request, cursor: str | None = Query(None,max_length=4096),
                         limit: int = Query(50,ge=20,le=100), session: Session = Depends(snapshot_session)):
    from app.data_foundation.batch_views import reconciliation_page
    return foundation_response(lambda: service(session,request).finish(reconciliation_page(service(session,request),batch_id,cursor=cursor,limit=limit)))


@router.get('/datasets/{dataset_id}/intake')
def intake_ledger(dataset_id: str, request: Request, cursor: str | None = Query(None,max_length=4096),
                  limit: int = Query(50,ge=20,le=100), session: Session = Depends(snapshot_session)):
    from app.data_foundation.batch_views import list_intakes
    return foundation_response(lambda: service(session,request).finish(list_intakes(service(session,request),dataset_id,cursor=cursor,limit=limit)))


@router.get('/maintenance/impact')
def maintenance_impact(request: Request, kind: str, dependency_id: UUID, session: Session = Depends(snapshot_session)):
    from app.data_foundation.maintenance import impact
    if kind not in ('source_ref_id','binding_id','execution_id','definition_id'):
        raise HTTPException(422,detail={'code':'INVALID_REQUIREMENT','message':'不支持的依赖类型。'})
    return foundation_response(lambda:service(session,request).finish(impact(session,[{'kind':kind,'id':dependency_id}])))
