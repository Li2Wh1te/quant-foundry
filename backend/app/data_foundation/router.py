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
    rows = session.scalars(select(Definition).where(Definition.kind == 'contract').order_by(Definition.name, Definition.version)).all()
    return {'items': [{'id': str(row.id), 'dataset': row.name, 'version': row.version, 'definition_hash': row.content_hash,
        'read_status': 'not_implemented', 'update_status': 'not_implemented'} for row in rows]}


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
