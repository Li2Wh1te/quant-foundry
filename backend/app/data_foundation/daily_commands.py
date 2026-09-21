"""Explicit, hash-reviewed S1 operations; no schedules or automatic head changes."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import register_dependencies, register_definition
from app.data_foundation.baselines import S1_CODES,S1_START,S1_END
from app.data_foundation.models import SourceRef
from app.data_foundation.source_refs import read_source
from app.data_foundation.identity import register_binding
from app.data_foundation.contracts import register_initial_catalog
from app.data_foundation.tushare import DOMAIN_KEY,SERIES,domain_hash
from app.data_foundation.work import create_work
from app.data_foundation.work_models import Work,Head,IssueScope
from app.instruments.models import Instrument
from app.data_ingestion.models.etf import EtfCode,EtfCodeMappingAudit


def review_s1(session, bars_id, directory_id, calendar_id):
    """Retrospective source-local binding, not a reconstructed public PIT fact.

    A fixed provider listing identifies the local instrument. Review also checks
    current ownership and every local reassignment audit; any ambiguity stops
    admission. This is intentionally bounded to the captured S1 date interval.
    """
    refs={}
    content={}
    for part,rid,dataset in [('bars',bars_id,'etf_daily'),('directory',directory_id,'etf_directory'),('calendar',calendar_id,'exchange_calendar')]:
        ref=session.get(SourceRef,rid)
        if not ref or ref.source!='tushare' or ref.dataset!=dataset or ref.representation!='local_table_baseline':
            raise FoundationError('SOURCE_INVALID','S1固定来源类型不符。')
        refs[part]={'id':ref.id,'hash':ref.content_hash}
        content[part]=read_source(session,rid)
    expected='262b06754e9b537f3ca6166ecc5051eb6a5752c66ad2775c6f42ae8b740093e8'
    capture=digest('s1-capture',{'scope':{'codes':S1_CODES,'start':S1_START,'end':S1_END},'content':content})
    if capture!=expected:
        raise FoundationError('SOURCE_CHANGED','固定S1内容与已批准取证不一致，未创建工作。')
    reviews=[]
    for row in content['directory']:
        iid=UUID(row['etf_id']);instrument=session.get(Instrument,iid)
        codes=session.scalars(select(EtfCode).where(EtfCode.etf_id==iid)).all()
        audits=session.scalars(select(EtfCodeMappingAudit).where(EtfCodeMappingAudit.source=='tushare',EtfCodeMappingAudit.ts_code==row['ts_code'])).all()
        if (instrument is None or instrument.asset_class!='etf' or instrument.status!='active'
                or instrument.merged_into_id is not None or len(codes)!=1 or audits
                or codes[0].ts_code!=row['ts_code'] or codes[0].source!='tushare'
                or codes[0].exchange!=row['exchange'] or codes[0].list_date!=date.fromisoformat(row['list_date'])
                or row['exchange'] not in {'SH','SZ'} or row['list_status']!='L'
                or date.fromisoformat(row['list_date'])>S1_START):
            raise FoundationError('IDENTITY_UNRESOLVED','固定目录、既有身份或本地重分配证据冲突，需人工核实。')
        reviews.append({'subject':row['ts_code'],'instrument_id':iid,'exchange':row['exchange'],
            'list_date':row['list_date'],'fixed_directory_hash':refs['directory']['hash'],
            'ownership':'one_source_code_one_existing_uuid','reassignment_audits':0,
            'interpretation':'retrospective_local_baseline','public_pit_supported':False})
    report={'source_refs':refs,'capture_hash':capture,'reviews':reviews,
        'start':S1_START,'end':S1_END,'source_rows':len(content['bars']),
        'domain':DOMAIN_KEY,'domain_hash':domain_hash()}
    return report,digest('s1-admission-review',report)


def apply_review(session, report, *, execution_id, reviewer, binding_version):
    if not reviewer.strip() or not binding_version.strip():
        raise ValueError('Reviewer and binding version are required')
    contract,series,policy,_=register_initial_catalog(session)
    register_definition(session,kind='support',name='market.bar.daily',version='m3',definition={
        'schema_version':1,'read_status':'implemented','update_status':'bounded_manual',
        'replay_status':'verify_execution_dependencies','notice_days':30,'approval_required':True,
        'supported_series':[SERIES],'time_modes':['observed'],'max_subject_calendar_days':1000,
        'pagination':False,'volume_conversion':'unverified'})
    bindings=[]
    for review in report['reviews']:
        binding=register_binding(session,source_ref_id=UUID(str(report['source_refs']['directory']['id'])),
            subject=review['subject'],instrument_id=UUID(str(review['instrument_id'])),valid_from=S1_START,
            valid_to=S1_END+timedelta(days=1),binding_version=binding_version,status='resolved',
            evidence={'reviewer':reviewer,'reference':review,'valid_from':str(S1_START),'valid_to':str(S1_END+timedelta(days=1))})
        bindings.append(binding)
    deps=register_dependencies(session,[{'binding_id':b.id,'purpose':'reviewed_identity'} for b in bindings]+
        [{'source_ref_id':UUID(str(ref['id'])),'purpose':part} for part,ref in report['source_refs'].items()]+
        [{'execution_id':execution_id,'purpose':'normalization'},{'definition_id':series.id,'purpose':'series'}])
    params=dict(dataset='market.bar.daily',major=1,profile='default',series=SERIES,start=str(S1_START),end=str(S1_END),domain=DOMAIN_KEY,domain_hash=domain_hash())
    work=create_work(session,kind='A',contract_id=contract.id,execution_id=execution_id,dependency_id=deps.id,
        parameters=params,source_ref_id=UUID(str(report['source_refs']['bars']['id'])),total=report['source_rows'])
    return {'work_id':work.id,'policy_id':policy.id,'binding_ids':[b.id for b in bindings]}


def reconcile_s1(session, work):
    """Independent field-by-field acceptance; do not call the mapper under test."""
    from decimal import Decimal, localcontext
    from app.data_foundation.work_models import Candidate, CandidateBar
    from app.data_foundation.models import Binding
    rows=read_source(session,work.source_ref_id)
    candidates=session.scalars(select(Candidate).where(Candidate.work_id==work.id)).all()
    actual={}
    for candidate in candidates:
        bar=session.get(CandidateBar,candidate.id)
        binding=session.get(Binding,candidate.binding_id) if candidate.binding_id else None
        if candidate.readiness!='ready' or not bar or not binding:
            raise FoundationError('S1_INCOMPLETE','S1存在隔离候选，不能发布首批正式值。')
        key=(binding.subject,str(bar.trade_date))
        if key in actual:raise FoundationError('S1_INCOMPLETE','S1候选业务键重复。')
        actual[key]=bar
    if len(rows)!=300 or len(actual)!=300:
        raise FoundationError('S1_INCOMPLETE','S1来源和候选数量不符。')
    for row in rows:
        bar=actual.get((row['ts_code'],row['trade_date']))
        with localcontext() as context:
            context.prec=64
            expected=Decimal(row['amount'])*Decimal('1000')
        if (not bar or any(getattr(bar,k)!=Decimal(row[k]) for k in ('open','high','low','close'))
                or bar.turnover!=expected or bar.volume is not None or bar.series!=SERIES):
            raise FoundationError('S1_RECONCILIATION_FAILED','S1逐键价格、金额或成交量限制不符。')
    return {'source_keys':300,'candidate_keys':300,'field_comparisons':1500,'unverified_volume_nulls':300}


def main():
    from app.db.session import get_engine
    from app.data_foundation.governance import create_governance,plan_actions
    from app.data_foundation.work_models import CandidateManifest
    parser=argparse.ArgumentParser(description='显式核验并处理已批准的S1，不启动供应商采集。')
    parser.add_argument('command',choices=['review','normalize','governance-review','governance'])
    parser.add_argument('--bars',type=UUID);parser.add_argument('--directory',type=UUID);parser.add_argument('--calendar',type=UUID)
    parser.add_argument('--expected-hash');parser.add_argument('--execution-id',type=UUID)
    parser.add_argument('--reviewer');parser.add_argument('--binding-version');parser.add_argument('--work-id',type=UUID)
    parser.add_argument('--policy-id',type=UUID)
    args=parser.parse_args()
    with Session(get_engine()) as session,session.begin():
        if args.command in {'review','normalize'}:
            if not all([args.bars,args.directory,args.calendar]):parser.error('Explicit source IDs required')
            report,review_hash=review_s1(session,args.bars,args.directory,args.calendar)
            if args.command=='review':result={'review':report,'review_hash':review_hash}
            else:
                if args.expected_hash!=review_hash:raise FoundationError('REVIEW_CHANGED','审核摘要不符，未创建工作。')
                if not all([args.execution_id,args.reviewer,args.binding_version]):parser.error('Execution, reviewer and binding version required')
                result=apply_review(session,report,execution_id=args.execution_id,reviewer=args.reviewer,binding_version=args.binding_version)
        else:
            if not args.work_id or not args.policy_id:parser.error('Explicit normalization and policy IDs required')
            work=session.get(Work,args.work_id)
            manifest=session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==args.work_id))
            if not work or not manifest:raise FoundationError('CANDIDATE_NOT_SEALED','候选尚未封存。')
            if session.get(Head,work.scope_key):raise FoundationError('HEAD_CHANGED','首批发布发现已有正式版本，需单独核实。')
            reconciliation=reconcile_s1(session,work)
            actions=plan_actions(session,manifest.id,args.policy_id)
            epoch=session.get(IssueScope,work.scope_key).epoch
            review_hash=digest('governance-review',{'manifest':manifest.manifest_hash,'actions':actions,'policy_id':args.policy_id,'issue_epoch':epoch,'parent':None})
            if args.command=='governance-review':result={'review_hash':review_hash,'reconciliation':reconciliation,'actions':len(actions),'selected':sum(a['action']=='select' for a in actions),'blocked_or_gap':sum(a['action']!='select' for a in actions)}
            else:
                if args.expected_hash!=review_hash or not args.execution_id:raise FoundationError('REVIEW_CHANGED','治理审核摘要或执行版本缺失。')
                # S1 admission is all-or-nothing at the operator boundary. Generic
                # tests can publish explicit gaps, but this approved first slice
                # must reconcile all 300 expected keys before production publish.
                if len(actions)!=300 or any(a['action']!='select' for a in actions):raise FoundationError('S1_INCOMPLETE','S1尚有隔离或缺口，未创建首批正式发布工作。')
                result={'work_id':create_governance(session,normalization_id=work.id,execution_id=args.execution_id,policy_id=args.policy_id,expected_issue_epoch=epoch).id}
    print(encode(result))


if __name__=='__main__':
    try:main()
    except FoundationError as exc:
        print(encode({'status':'failed','code':exc.code,'message':str(exc)}));raise SystemExit(1)
