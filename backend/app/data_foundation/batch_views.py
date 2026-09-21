"""Read-only, authenticated batch ledgers with scoped signed cursors."""
import json
from uuid import UUID
from sqlalchemy import select, tuple_, func
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.catalog import now
from app.data_foundation.batch_models import Batch, BatchWork, BatchInput, BatchControl, Reconciliation, ReleaseContribution
from app.data_foundation.work_models import Work, Release, CandidateManifest, Head


def batch_summary(session,batch):
    document=json.loads(batch.manifest_json)
    works=session.scalars(select(Work).join(BatchWork,BatchWork.work_id==Work.id).where(BatchWork.batch_id==batch.id)).all()
    releases=session.scalars(select(Release).where(Release.work_id.in_([w.id for w in works])).order_by(Release.created_at,Release.id)).all()
    reconciliation=session.scalar(select(Reconciliation).where(Reconciliation.batch_id==batch.id).order_by(Reconciliation.created_at.desc(),Reconciliation.id.desc()).limit(1))
    from app.data_foundation.reconciliation import execution_state
    stale=reconciliation is not None and json.loads(reconciliation.evidence_json).get('execution_state')!=execution_state(session,batch.id)
    control=session.get(BatchControl,batch.id)
    head=session.get(Head,batch.scope_key)
    return dict(id=str(batch.id),created_at=batch.created_at,start=document['start'],end=document['end'],
        purpose=document['purpose'],manifest_hash=batch.manifest_hash,source_versions=len(document['sources']),
        controls=dict(pause_a=control.pause_a,pause_b=control.pause_b,allow_publish=control.allow_publish),
        stages={kind:dict(works=sum(w.kind==kind for w in works),
            completed=sum(w.kind==kind and w.status=='succeeded' for w in works),
            waiting_publication=sum(w.kind==kind and w.status=='awaiting_publication' for w in works),
            failed=sum(w.kind==kind and w.status in ('failed','dependency_missing') for w in works)) for kind in ('A','B')},
        reconciliation=dict(id=str(reconciliation.id),status='stale' if stale else reconciliation.status,checked_at=reconciliation.created_at) if reconciliation else None,
        current_release_id=str(head.release_id) if head else None,
        releases=[dict(id=str(r.id),status=r.status,work_id=str(r.work_id),published_at=r.published_at) for r in releases],
        works=[dict(id=str(w.id),kind=w.kind,status=w.status,processed=w.cursor,total=w.total) for w in works])


def list_batches(service,scope,*,cursor=None,limit=50):
    if limit not in (20,50,100):raise FoundationError('INVALID_REQUIREMENT','分页大小仅支持20、50或100。')
    owner=digest('owner-v1',service.owner())
    statement=select(Batch).where(Batch.scope_key==scope)
    if cursor:
        page=service.codec.read(cursor,'batch-list')
        if page['scope']!=scope or page['owner']!=owner:
            raise FoundationError('CURSOR_MISMATCH','批次游标与范围或当前身份不一致。')
        if page['expires_at']<int(service.codec.clock()):raise FoundationError('RESOLUTION_EXPIRED','批次分页已过期，请刷新。')
        from datetime import datetime
        anchor=(datetime.fromisoformat(page['after'][0]),UUID(page['after'][1]))
        ceiling=(datetime.fromisoformat(page['ceiling'][0]),UUID(page['ceiling'][1]))
        statement=statement.where(tuple_(Batch.created_at,Batch.id)<anchor,tuple_(Batch.created_at,Batch.id)<=ceiling)
    rows=service.session.scalars(statement.order_by(Batch.created_at.desc(),Batch.id.desc()).limit(limit+1)).all()
    next_cursor=None
    if len(rows)>limit:
        last=rows[limit-1]
        next_cursor=service.codec.sign('batch-list',dict(scope=scope,owner=owner,
            ceiling=page['ceiling'] if cursor else [rows[0].created_at.isoformat(),str(rows[0].id)],
            after=[last.created_at.isoformat(),str(last.id)],expires_at=page['expires_at'] if cursor else int(service.codec.clock())+900))
    return dict(items=[batch_summary(service.session,b) for b in rows[:limit]],next_cursor=next_cursor,as_of=now())


def reconciliation_page(service,batch_id,*,cursor=None,limit=50):
    if limit not in (20,50,100):raise FoundationError('INVALID_REQUIREMENT','分页大小仅支持20、50或100。')
    owner=digest('owner-v1',service.owner())
    if cursor:
        page=service.codec.read(cursor,'batch-reconciliation')
        if page['batch_id']!=str(batch_id) or page['owner']!=owner:
            raise FoundationError('CURSOR_MISMATCH','对账游标与批次或当前身份不一致。')
        if page['expires_at']<int(service.codec.clock()):raise FoundationError('RESOLUTION_EXPIRED','对账分页已过期，请刷新。')
        record=service.session.get(Reconciliation,UUID(page['id']));offset=page['offset']
    else:
        record=service.session.scalar(select(Reconciliation).where(Reconciliation.batch_id==batch_id)
            .order_by(Reconciliation.created_at.desc(),Reconciliation.id.desc()).limit(1));offset=0
    if record is None:return dict(items=[],status='not_reconciled',next_cursor=None,total=None)
    if record.batch_id!=batch_id:raise FoundationError('CURSOR_MISMATCH','固定对账记录与批次不一致。')
    body=json.loads(record.evidence_json)
    if digest('batch-reconciliation-v1',body)!=record.evidence_hash:
        raise FoundationError('SOURCE_MUTATED','对账证据摘要不一致。')
    from app.data_foundation.reconciliation import execution_state
    stale=body.get('execution_state')!=execution_state(service.session,batch_id)
    items=body.pop('items')
    token=service.codec.sign('batch-reconciliation',dict(batch_id=str(batch_id),owner=owner,id=str(record.id),offset=offset+limit,
        expires_at=page['expires_at'] if cursor else int(service.codec.clock())+900)) if offset+limit<len(items) else None
    return dict(id=str(record.id),status='stale' if stale else record.status,checked_at=record.created_at,items=items[offset:offset+limit],
        total=len(items),next_cursor=token,counts=body)


def list_intakes(service,dataset,*,cursor=None,limit=50):
    from app.data_foundation.intake_models import IntakeScope,IntakeControl,IntakeItem,Scan
    if limit not in (20,50,100):raise FoundationError('INVALID_REQUIREMENT','分页大小仅支持20、50或100。')
    source_dataset={'market.bar.daily':'etf_daily','fund.holdings_report':'fund_stock_history'}.get(dataset)
    if source_dataset is None:raise FoundationError('INVALID_REQUIREMENT','数据集不存在。')
    owner=digest('owner-v1',service.owner())
    statement=select(IntakeScope).where(IntakeScope.dataset==source_dataset)
    if cursor:
        page=service.codec.read(cursor,'intake-list')
        if page['dataset']!=dataset or page['owner']!=owner:raise FoundationError('CURSOR_MISMATCH','来源台账游标与范围或身份不一致。')
        if page['expires_at']<=int(service.codec.clock()):raise FoundationError('RESOLUTION_EXPIRED','来源台账分页已过期，请刷新。')
        from datetime import datetime
        statement=statement.where(tuple_(IntakeScope.created_at,IntakeScope.id)<(datetime.fromisoformat(page['after'][0]),UUID(page['after'][1])),
            tuple_(IntakeScope.created_at,IntakeScope.id)<=(datetime.fromisoformat(page['ceiling'][0]),UUID(page['ceiling'][1])))
    rows=service.session.scalars(statement.order_by(IntakeScope.created_at.desc(),IntakeScope.id.desc()).limit(limit+1)).all()
    result=[]
    for scope in rows[:limit]:
        control=service.session.get(IntakeControl,scope.id)
        scan=service.session.scalar(select(Scan).where(Scan.scope_id==scope.id).order_by(Scan.created_at.desc(),Scan.id.desc()).limit(1))
        full=service.session.scalar(select(Scan).where(Scan.scope_id==scope.id,Scan.status=='completed',Scan.mode=='full')
            .order_by(Scan.completed_at.desc()).limit(1))
        count=service.session.scalar(select(func.count()).select_from(IntakeItem).where(IntakeItem.scope_id==scope.id))
        result.append(dict(id=str(scope.id),subject=scope.subject,selector=json.loads(scope.selector_json),paused=control.paused,
            fixed_versions=count,last_full_completed_at=full.completed_at if full else None,
            scan=dict(id=str(scan.id),mode=scan.mode,status=scan.status,seen=scan.seen,registered=scan.registered,
                message='本周期核对失败，请维护者检查固定来源或解码依赖。' if scan.status=='failed' else None) if scan else None))
    token=None
    if len(rows)>limit:
        last=rows[limit-1]
        token=service.codec.sign('intake-list',dict(dataset=dataset,owner=owner,after=[last.created_at.isoformat(),str(last.id)],
            ceiling=page['ceiling'] if cursor else [rows[0].created_at.isoformat(),str(rows[0].id)],
            expires_at=page['expires_at'] if cursor else int(service.codec.clock())+900))
    return dict(items=result,next_cursor=token,as_of=now())
