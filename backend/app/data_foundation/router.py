"""Authenticated metadata only; M4 owns the eventual public query contract."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db_session
from app.data_foundation.models import Definition, SourceRef, Execution

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
        'decoder_id': row.decoder_id, 'replay_status': decoder.replay_status, 'retention_state': 'protected'}
