"""Authenticated metadata only; M4 owns the eventual public query contract."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db_session
from app.data_foundation.models import Definition, SourceRef, Execution
from app.data_foundation.execution import replay_status

router = APIRouter(prefix='/api/admin/data-foundation', tags=['data-foundation'])


@router.get('/datasets')
def datasets(session: Session = Depends(get_db_session)):
    from app.data_foundation.query import dataset_view
    return foundation_response(lambda: {'items': [dataset_view(session)]})


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
    from app.data_foundation.read_models import processing_view
    rows=session.scalars(select(Work).order_by(Work.created_at.desc(),Work.id).limit(limit)).all()
    return {'items':[processing_view(session,row) for row in rows]}


@router.get('/work/{work_id}')
def work_detail(work_id: UUID, session: Session = Depends(snapshot_session)):
    from app.data_foundation.work_models import Work
    from app.data_foundation.read_models import processing_view
    row=session.get(Work,work_id)
    if row is None:raise HTTPException(404,detail={'code':'WORK_UNAVAILABLE','message':'底座工作不存在。'})
    return processing_view(session,row)


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
        'candidate_manifest_id':row.candidate_manifest_id,'selected_candidate_id':row.selected_candidate_id,
        'parent_official_id':row.parent_official_id,'evidence':json.loads(row.evidence_json)}


# Serialize inside the transaction that holds the current issue scope lock.
# Returning a Response avoids a later framework encoder changing Decimal values.
from fastapi import Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from app.core.auth import require_api_token
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.query import DataRequirement


def current_auth(request):
    def authenticate():
        header=request.headers.get('authorization','')
        scheme,_,token=header.partition(' ')
        credentials=HTTPAuthorizationCredentials(scheme=scheme,credentials=token) if token else None
        return require_api_token(request,credentials).owner_scope
    return authenticate


def foundation_response(callback):
    try:
        payload=callback()
        return Response(payload if isinstance(payload,bytes) else encode(payload),media_type='application/json')
    except FoundationError as exc:
        status={'INVALID_REQUIREMENT':422,'CONTRACT_RELEASE_MISMATCH':422,'RELEASE_UNAVAILABLE':404,
            'READ_CONTEXT_CHANGED':409,'AUTH_REQUIRED':401,'AUTH_CONTEXT_CHANGED':401}.get(exc.code,503)
        raise HTTPException(status,detail={'code':exc.code,'message':str(exc)}) from None


@router.get('/datasets/{dataset_id}')
def describe_dataset(dataset_id: str, contract_version: str='1.0', session: Session=Depends(snapshot_session)):
    from app.data_foundation.query import DATASET,dataset_view
    if dataset_id!=DATASET or contract_version!='1.0':
        raise HTTPException(404,detail={'message':'数据集或契约版本不存在。'})
    return foundation_response(lambda:dataset_view(session))


@router.post('/capability-checks')
def capability_check(body: DataRequirement, request: Request, session: Session=Depends(get_db_session)):
    from app.data_foundation.query import query_official
    return foundation_response(lambda:query_official(session,body,current_auth(request),check_only=True))


@router.post('/queries')
def official_query(body: DataRequirement, request: Request, session: Session=Depends(get_db_session)):
    from app.data_foundation.query import query_official
    return foundation_response(lambda:query_official(session,body,current_auth(request)))


@router.get('/revisions/{revision_id}/lineage')
def revision_lineage(revision_id: UUID, session: Session=Depends(snapshot_session)):
    from app.data_foundation.query import lineage
    return foundation_response(lambda:lineage(session,revision_id))


from pydantic import BaseModel
import json


class CandidateInspection(BaseModel):
    model_config={'extra':'forbid'}
    work_id: UUID


@router.post('/candidate-inspections')
def candidate_inspection(body: CandidateInspection, session: Session=Depends(snapshot_session)):
    from app.data_foundation.work_models import Work,Candidate,Assessment
    row=session.get(Work,body.work_id)
    if row is None or row.kind!='A':
        raise HTTPException(404,detail={'message':'标准化工作不存在。'})
    rows=session.scalars(select(Candidate).where(Candidate.work_id==row.id).order_by(Candidate.occurrence).limit(1001)).all()
    if len(rows)>1000:raise HTTPException(422,detail={'message':'首期诊断最多1000条。'})
    return foundation_response(lambda:{'representation':'candidate','work_id':row.id,'items':[
        {'id':c.id,'source_ref_id':c.source_ref_id,'readiness':c.readiness,
         'quality':json.loads(session.get(Assessment,c.assessment_id).results_json)} for c in rows]})
