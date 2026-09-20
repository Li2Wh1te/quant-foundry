"""Bounded A/B orchestration over an already reviewed finite batch.

The driver never expands the source manifest. It uses the existing singleton,
lease fencing and executable-archive verification for actual normalization and
publication. New candidate sets are frozen only after every A work has sealed.
"""
import argparse
import json
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.data_foundation.canonical import FoundationError,encode
from app.data_foundation.batch_models import Batch,BatchWork,BatchControl
from app.data_foundation.work_models import Work,CandidateManifest,Head,IssueScope
from app.data_foundation.catalog import lock_key
from app.data_foundation.inputs import seal_input_set
from app.data_foundation.governance import create_multi_governance
from app.data_foundation.batches import attach_work
from app.data_foundation.worker import run_once


def prepare_governance(session,batch_id,*,execution_id,policy_id):
    lock_key(session,'batch-control',str(batch_id))
    batch=session.get(Batch,batch_id)
    if batch is None:raise FoundationError('BATCH_UNAVAILABLE','业务批次不存在。')
    control=session.get(BatchControl,batch_id)
    works=session.scalars(select(Work).join(BatchWork,BatchWork.work_id==Work.id).where(BatchWork.batch_id==batch_id)).all()
    active=[w for w in works if w.kind=='B' and w.status not in ('superseded','cancelled')]
    if active or control.pause_b:return active
    origins=[w for w in works if w.kind=='A']
    if not origins or any(w.status!='succeeded' for w in origins):return []
    from app.data_foundation.models import SourceRef
    from app.data_ingestion.models.tonghuashun import TonghuashunCollectionState as State
    entries=[]
    for work in origins:
        manifest=session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==work.id))
        ref=session.get(SourceRef,work.source_ref_id)
        role,reason='eligible','本批次固定来源及标准化依据'
        if ref.representation=='ths_observation':
            state=session.get(State,(ref.dataset,ref.subject,ref.variant))
            if state is None or state.observation_id!=ref.observation_id:
                role,reason='historical','历史版本保留，仅当前已观察指针参与本次选择'
        entries.append(dict(manifest_id=manifest.id,role=role,reason=reason))
    if not any(e['role']=='eligible' for e in entries):return []
    fixed=seal_input_set(session,entries)
    head=session.get(Head,batch.scope_key)
    issue=session.get(IssueScope,batch.scope_key)
    work=create_multi_governance(session,input_set_id=fixed.id,execution_id=execution_id,policy_id=policy_id,
        parent_release_id=head.release_id if head else None,expected_head_revision=head.revision if head else 0,
        expected_issue_epoch=issue.epoch if issue else 0)
    attach_work(session,batch_id,work.id)
    return [work]


def advance(engine,batch_id,*,steps,execution_id,policy_id,runtime_digest,archive_root):
    if not 1<=steps<=100:raise ValueError('Driver requires 1 to 100 bounded steps')
    previous='B'
    for _ in range(steps):
        with Session(engine) as session,session.begin():
            prepare_governance(session,batch_id,execution_id=execution_id,policy_id=policy_id)
            ids=session.scalars(select(BatchWork.work_id).where(BatchWork.batch_id==batch_id)).all()
            rows=session.scalars(select(Work).where(Work.id.in_(ids),Work.status.in_(['queued','running']))
                .order_by(Work.created_at,Work.id)).all()
            control=session.get(BatchControl,batch_id)
            rows=[w for w in rows if not (control.pause_a if w.kind=='A' else control.pause_b)]
            if not rows:return
            preferred='A' if previous=='B' else 'B'
            row=next((w for w in rows if w.kind==preferred),rows[0]);wid=row.id
        kind=run_once(engine,work_id=wid,preferred_kind=preferred,runtime_digest=runtime_digest,archive_root=archive_root)
        if kind is None:return
        previous=kind


def main():
    parser=argparse.ArgumentParser(description='执行已审核有限批次；默认仅影子治理。')
    parser.add_argument('--batch-id',type=UUID,required=True)
    parser.add_argument('--execution-id',type=UUID,required=True)
    parser.add_argument('--policy-id',type=UUID,required=True)
    parser.add_argument('--steps',type=int,default=1)
    args=parser.parse_args()
    from app.db.session import get_engine
    from app.core.config import get_settings
    from app.data_foundation.reconciliation import reconcile_batch
    advance(get_engine(),args.batch_id,steps=args.steps,execution_id=args.execution_id,policy_id=args.policy_id,
        runtime_digest=get_settings().foundation_runtime_image_digest,archive_root='/app/data/foundation-runtime-archives')
    with Session(get_engine()) as session,session.begin():
        row=reconcile_batch(session,args.batch_id)
        result=dict(batch_id=args.batch_id,reconciliation_id=row.id,status=row.status,message='有限批次已完成本次处理预算，检查点已保存，请查看逐项对账和待处理状态。')
    print(encode(result))


if __name__=='__main__':
    try:main()
    except FoundationError as exc:
        print(encode(dict(code=exc.code,message=str(exc),status='failed')))
        raise SystemExit(1)
