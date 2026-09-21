"""Explicit finite M6 operator commands; deployment never enables discovery.

Documents are reviewed local files. Commands print IDs, hashes and Chinese
outcomes, never source payloads, connection settings or signed provider URLs.
"""
import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4
from sqlalchemy.orm import Session
from app.db.session import get_engine
from app.data_foundation.canonical import FoundationError, digest, encode


def main():
    parser=argparse.ArgumentParser(description='有限范围的来源发现、批次对账与维护命令；不请求供应商。')
    parser.add_argument('command',choices=['scope-register','scope-control','scan','batch-preview','batch-register',
        'batch-attach','batch-control','reconcile','restrict-source','inputs-seal','governance-create','maintenance-preview','maintenance-register'])
    parser.add_argument('--document',type=Path)
    parser.add_argument('--expected-hash')
    parser.add_argument('--scope-id',type=UUID)
    parser.add_argument('--batch-id',type=UUID)
    parser.add_argument('--work-id',type=UUID)
    parser.add_argument('--event-key')
    parser.add_argument('--pages',type=int,default=1)
    parser.add_argument('--paused',action=argparse.BooleanOptionalAction,default=True)
    parser.add_argument('--pause-a',action=argparse.BooleanOptionalAction,default=True)
    parser.add_argument('--pause-b',action=argparse.BooleanOptionalAction,default=True)
    parser.add_argument('--allow-publish',action='store_true')
    parser.add_argument('--mode',choices=['full','recent','auto'],default='full')
    args=parser.parse_args()
    if not 1<=args.pages<=100:parser.error('--pages must be between 1 and 100')
    document=json.loads(args.document.read_text()) if args.document else None
    needs_document={'scope-register','batch-preview','batch-register','restrict-source','inputs-seal','governance-create','maintenance-preview','maintenance-register'}
    if args.command in needs_document and document is None:parser.error('--document is required')
    if args.command in {'scope-control','scan'} and not args.scope_id:parser.error('--scope-id is required')
    if args.command in {'batch-attach','batch-control','reconcile','restrict-source'} and not args.batch_id:parser.error('--batch-id is required')
    if args.command=='batch-attach' and not args.work_id:parser.error('--work-id is required')
    if args.command in {'batch-register','scan'} and not args.event_key:parser.error('--event-key is required')
    if args.command=='scan':
        from app.data_foundation.intake import start_scan,next_scan,scan_page
        with Session(get_engine()) as session,session.begin():
            row=next_scan(session,args.scope_id,args.event_key) if args.mode=='auto' else start_scan(session,args.scope_id,args.event_key,mode=args.mode)
            scan_id=row.id
        for _ in range(args.pages):
            with Session(get_engine()) as session,session.begin():result=scan_page(session,scan_id)
            print(encode(result))
            if result['paused'] or result['status'] in ('completed','failed'):break
        return
    with Session(get_engine()) as session,session.begin():
        from app.data_foundation import intake,batches,reconciliation,inputs,governance,maintenance
        if args.command=='scope-register':
            required={'dataset','subject','variant','selector','decoder_id'}
            if set(document)!=required:raise ValueError('Invalid scope document')
            actual=digest('intake-scope-v1',{**document,'decoder_id':UUID(document['decoder_id'])})
            if args.expected_hash!=actual:
                print(encode(dict(status='review_required',expected_hash=actual,message='来源纳入范围尚未登记，请核对摘要后显式执行。')));return
            row=intake.register_scope(session,**{**document,'decoder_id':UUID(document['decoder_id'])})
            result=dict(id=row.id,paused=True)
        elif args.command=='scope-control':
            intake.set_paused(session,args.scope_id,args.paused);result=dict(scope_id=args.scope_id,paused=args.paused)
        elif args.command=='batch-preview':
            doc,fp=batches.batch_document(session,**{**document,'source_ids':[UUID(v) for v in document['source_ids']]})
            result=dict(document=doc,expected_hash=fp)
        elif args.command=='batch-register':
            row=batches.register_batch(session,event_key=args.event_key,document=document,expected_hash=args.expected_hash)
            result=dict(batch_id=row.id,manifest_hash=row.manifest_hash)
        elif args.command=='batch-attach':
            row=batches.attach_work(session,args.batch_id,args.work_id);result=dict(batch_id=args.batch_id,work_id=row.id)
        elif args.command=='batch-control':
            batches.set_controls(session,args.batch_id,pause_a=args.pause_a,pause_b=args.pause_b,allow_publish=args.allow_publish)
            result=dict(batch_id=args.batch_id,allow_publish=args.allow_publish)
        elif args.command=='reconcile':
            row=reconciliation.reconcile_batch(session,args.batch_id)
            result=dict(reconciliation_id=row.id,status=row.status,evidence_hash=row.evidence_hash,
                counts={k:v for k,v in json.loads(row.evidence_json).items() if k!='items'})
        elif args.command=='restrict-source':
            row=reconciliation.restrict_source(session,args.batch_id,**{**document,'source_ref_id':UUID(document['source_ref_id'])})
            result=dict(disposition_id=row.id)
        elif args.command=='inputs-seal':
            row=inputs.seal_input_set(session,[{**e,'manifest_id':UUID(e['manifest_id'])} for e in document])
            result=dict(input_set_id=row.id,manifest_hash=row.manifest_hash)
        elif args.command=='governance-create':
            converted={k:UUID(v) if k.endswith('_id') and v is not None else v for k,v in document.items()}
            row=governance.create_multi_governance(session,**converted)
            if args.batch_id:batches.attach_work(session,args.batch_id,row.id)
            result=dict(work_id=row.id,input_fingerprint=row.fingerprint)
        else:
            deps=[{**e,'id':UUID(e['id'])} for e in document]
            if args.command=='maintenance-preview':result=maintenance.impact(session,deps)
            else:
                row=maintenance.register_maintenance(session,deps);result=dict(plan_id=row.id,fingerprint=row.fingerprint)
    print(encode(result))


if __name__=='__main__':
    try:main()
    except (FoundationError,ValueError) as exc:
        print(encode(dict(status='failed',code=getattr(exc,'code','INVALID_INPUT'),message=str(exc))))
        raise SystemExit(1)
