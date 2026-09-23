"""Persist object-level source/candidate/decision/release settlement evidence."""
import json
from collections import defaultdict
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.models import SourceRef
from app.data_foundation.source_refs import read_source
from app.data_foundation.work_models import Work, Candidate, CandidateEntry, CandidateManifest, Assessment, Decision, Release, Unit
from app.data_foundation.batch_models import Batch, BatchInput, BatchWork, BatchDisposition, Reconciliation


def restrict_source(session,batch_id,source_ref_id,*,reason_code,evidence):
    if reason_code not in ('SEMANTIC_DEPENDENCY_MISSING','OUT_OF_SCOPE'):
        raise ValueError('Unsupported restricted settlement')
    if set(evidence) != {'reviewer','reference','reason'} or any(not isinstance(v,str) or not v.strip() or len(v)>512 for v in evidence.values()):
        raise ValueError('A reviewed, bounded explanation is required')
    lock_key(session,'batch-control',str(batch_id))
    if session.get(BatchInput,(batch_id,source_ref_id)) is None:
        raise FoundationError('SCOPE_MISMATCH','该来源不属于当前批次。')
    old=session.scalar(select(BatchDisposition).where(BatchDisposition.batch_id==batch_id,BatchDisposition.source_ref_id==source_ref_id))
    if old:
        if old.reason_code!=reason_code or old.evidence_json!=encode(evidence):
            raise FoundationError('INPUT_CHANGED','已记录的受限结论不能覆盖，请建立新的维护批次。')
        return old
    # Even an unadmitted source must have valid retained content. A corrupt
    # delta is an operational failure, never a semantic restriction shortcut.
    read_source(session,source_ref_id)
    row=BatchDisposition(batch_id=batch_id,source_ref_id=source_ref_id,reason_code=reason_code,evidence_json=encode(evidence),created_at=now())
    session.add(row);session.flush()
    return row


def reconcile_batch(session,batch_id):
    lock_key(session,'batch-control',str(batch_id))
    batch=session.get(Batch,batch_id)
    if batch is None:raise FoundationError('BATCH_UNAVAILABLE','业务批次不存在。')
    links=session.scalars(select(BatchWork).where(BatchWork.batch_id==batch_id)).all()
    works=[session.get(Work,l.work_id) for l in links]
    sources=session.scalars(select(BatchInput).where(BatchInput.batch_id==batch_id).order_by(BatchInput.source_ref_id)).all()
    document=json.loads(batch.manifest_json)
    if {str(s.source_ref_id) for s in sources}!={s['id'] for s in document['sources']}:
        raise FoundationError('INPUT_CHANGED','批次来源引用与封存清单不一致。')
    releases=session.scalars(select(Release).where(Release.work_id.in_([w.id for w in works]))).all()
    decisions=session.scalars(select(Decision).where(Decision.work_id.in_([w.id for w in works]))).all()
    from app.data_foundation.inputs import candidate_origins, input_manifests
    admitted={}
    historical=set()
    for work in works:
        if work.kind=='B':
            for cid,mid in candidate_origins(session,work).items():admitted.setdefault(cid,[]).append(work.id)
            all_ids={m.id for m in input_manifests(session,work=work,eligible_only=False)}
            active={m.id for m in input_manifests(session,work=work)}
            historical.update(session.scalars(select(CandidateEntry.candidate_id).where(CandidateEntry.manifest_id.in_(all_ids-active))))
    rows=[]
    def add(stage,key,state,reason,**refs):
        rows.append(dict(stage=stage,key=str(key),state=state,reason_code=reason,**refs))
    for link in sources:
        ref=session.get(SourceRef,link.source_ref_id)
        raw=read_source(session,ref.id)
        origins=[w for w in works if w.kind=='A' and w.source_ref_id==ref.id]
        # A reused normalization can be reached through a B input even if it was
        # originally executed in another batch; retain that real work identity.
        for work in works:
            if work.kind=='B':
                origins += [session.get(Work,m.work_id) for m in input_manifests(session,work=work,eligible_only=False)
                    if session.get(Work,m.work_id).source_ref_id==ref.id]
        origins=list({w.id:w for w in origins}.values())
        if not origins:
            disposition=session.scalar(select(BatchDisposition).where(BatchDisposition.batch_id==batch_id,
                BatchDisposition.source_ref_id==ref.id))
            add('source',ref.id,'restricted' if disposition else 'pending',
                disposition.reason_code if disposition else 'NORMALIZATION_PENDING',source_ref_id=str(ref.id))
            continue
        for work in origins:
            params=json.loads(work.parameters_json)
            expected=len(params['report_keys']) if params['domain']=='holdings-report-v1' else len(raw)
            if params['domain'] == 'typed-record-v1':
                from app.data_foundation.record_adapters import rows_for
                expected = len(rows_for(ref, raw))
            candidates=session.scalars(select(Candidate).where(Candidate.work_id==work.id).order_by(Candidate.occurrence)).all()
            manifest=session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==work.id))
            if work.status!='succeeded':
                add('source',work.id,'pending','NORMALIZATION_'+work.status.upper(),source_ref_id=str(ref.id));continue
            entries=session.scalars(select(CandidateEntry).where(CandidateEntry.manifest_id==manifest.id).order_by(CandidateEntry.ordinal)).all() if manifest else []
            valid=(manifest and len(candidates)==expected==manifest.row_count==work.cursor
                and [c.occurrence for c in candidates]==list(range(expected))
                and [e.candidate_id for e in entries]==[c.id for c in candidates]
                and manifest.manifest_hash==digest('candidates',[[c.id,c.values_hash,c.readiness] for c in candidates]))
            add('source',work.id,'explained' if valid else 'unexplained','SOURCE_CANDIDATE_MATCH' if valid else 'CANDIDATE_MANIFEST_MISMATCH',source_ref_id=str(ref.id))
            # Keep strong references for this finite input. SQLAlchemy's identity
            # map is weak: a get() inside the candidate/decision cross product
            # otherwise repeats the same typed-row SELECT thousands of times.
            assessments = {a.id: a for a in session.scalars(select(Assessment).where(
                Assessment.id.in_([c.assessment_id for c in candidates])))}
            from app.data_foundation.work_models import CandidateBar
            from app.data_foundation.holding_models import CandidateReport
            typed_model = CandidateReport if params['domain'] == 'holdings-report-v1' else CandidateBar
            if params['domain'] == 'typed-record-v1':
                from app.data_foundation.record_models import CandidateRecord
                typed_model = CandidateRecord
            typed_candidates = list(session.scalars(select(typed_model).where(
                typed_model.candidate_id.in_([c.id for c in candidates]))))
            by_candidate, by_target = defaultdict(list), defaultdict(list)
            for ordinal, decision in enumerate(decisions):
                by_candidate[(decision.work_id, decision.selected_candidate_id)].append((ordinal, decision))
                by_target[(decision.work_id, decision.target_key)].append((ordinal, decision))
            mismatches=verify_candidate_values(session,work,candidates,raw)
            for candidate in candidates:
                check=json.loads(assessments[candidate.assessment_id].results_json)
                target = candidate_target(session, candidate, check)
                matched = {}
                for governance_id in admitted.get(candidate.id, []):
                    for ordinal, decision in by_candidate[(governance_id, candidate.id)] + by_target[(governance_id, target)]:
                        matched[ordinal] = decision
                # Preserve the original decision order and OR semantics exactly,
                # including candidates not selected for an otherwise decided key.
                relevant = [matched[index] for index in sorted(matched)]
                if candidate.id in mismatches:state,reason='unexplained','SOURCE_VALUE_MISMATCH'
                elif candidate.readiness=='quarantined':state,reason='restricted','CANDIDATE_QUARANTINED'
                elif relevant:state,reason='explained','GOVERNANCE_DECIDED'
                elif candidate.id in historical:state,reason='explained','HISTORICAL_INPUT_NOT_SELECTED'
                else:state,reason='pending','GOVERNANCE_PENDING'
                add('candidate',candidate.id,state,reason,work_id=str(work.id),source_ref_id=str(ref.id),
                    readiness=candidate.readiness,subject=check.get('source_subject') or ref.subject,
                    business_date=check.get('trade_date') or (check.get('header') or {}).get('period_end'),
                    source_report_key=check.get('source_report_key'),quality_reasons=check.get('reasons',[]),decision_ids=[str(d.id) for d in relevant])
    from app.data_foundation.publication import validate_release
    from app.data_foundation.work_models import BlockRef,BlockMember
    from app.data_foundation.holding_models import ReportBlockMember
    from app.data_foundation.record_models import RecordBlockMember
    for work in works:
        if work.kind!='B':continue
        produced=[r for r in releases if r.work_id==work.id]
        if not produced:
            add('release',work.id,'pending','PUBLICATION_NOT_SEALED');continue
        for release in produced:
            validate_release(session,release,reuse_verified=True)
            blocks=select(BlockRef.block_id).where(BlockRef.release_id==release.id)
            represented=set(session.scalars(select(BlockMember.decision_id).where(BlockMember.block_id.in_(blocks))))
            represented.update(session.scalars(select(ReportBlockMember.decision_id).where(ReportBlockMember.block_id.in_(blocks))))
            represented.update(session.scalars(select(RecordBlockMember.decision_id).where(RecordBlockMember.block_id.in_(blocks))))
            for decision in (d for d in decisions if d.work_id==work.id):
                add('decision',decision.id,'explained' if decision.id in represented else 'unexplained',
                    'DECISION_MANIFEST_MATCH' if decision.id in represented else 'DECISION_MISSING',
                    release_id=str(release.id),publication_status=release.status,action=decision.action,business_key=decision.target_key)
    rows.sort(key=lambda r:(r['stage'],r['key']))
    state='unexplained' if any(r['state']=='unexplained' for r in rows) else 'pending' if any(r['state']=='pending' for r in rows) else 'explained'
    body=dict(batch_hash=batch.manifest_hash,execution_state=execution_state(session,batch_id),items=rows,source_versions=len(sources),
        restricted=sum(r['state']=='restricted' for r in rows),pending=sum(r['state']=='pending' for r in rows),
        unexplained=sum(r['state']=='unexplained' for r in rows))
    fingerprint=digest('batch-reconciliation-v1',body)
    old=session.scalar(select(Reconciliation).where(Reconciliation.batch_id==batch_id,Reconciliation.evidence_hash==fingerprint))
    if old:return old
    row=Reconciliation(batch_id=batch_id,status=state,evidence_hash=fingerprint,evidence_json=encode(body),created_at=now())
    session.add(row);session.flush()
    return row


def candidate_target(session,candidate,check):
    if check.get('target_key'):
        return check['target_key']
    from app.data_foundation.work_models import CandidateBar
    from app.data_foundation.holding_models import CandidateReport
    from app.data_foundation.holding_work import fields,KEY_FIELDS
    from app.data_foundation.holdings import target_key as report_key
    bar=session.get(CandidateBar,candidate.id)
    if bar:return f'{bar.instrument_id}/{bar.trade_date}'
    report=session.get(CandidateReport,candidate.id)
    if report:
        return report_key(report.fund_share_id,report.period_start,report.period_end,report.report_type,report.scope_kind,report.series)
    return None


def execution_state(session,batch_id):
    works=session.scalars(select(Work).join(BatchWork,BatchWork.work_id==Work.id).where(BatchWork.batch_id==batch_id).order_by(Work.id)).all()
    releases=session.scalars(select(Release).where(Release.work_id.in_([w.id for w in works])).order_by(Release.id)).all()
    return digest('batch-execution-state-v1',dict(works=[[w.id,w.fingerprint,w.status,w.cursor,w.total] for w in works],
        releases=[[r.id,r.manifest_hash,r.status] for r in releases]))


def verify_candidate_values(session,work,candidates,raw):
    """Compare typed values to fixed source facts using the installed contract.

    This is a value reconciliation, not a claim to have replayed an unavailable
    historical executable. Candidate manifests and original rule IDs stay fixed.
    """
    from app.data_foundation.work_models import CandidateBar
    from app.data_foundation.bars import values
    params=json.loads(work.parameters_json)
    mismatches=set()
    if params['domain'] == 'typed-record-v1':
        from app.data_foundation.record_adapters import rows_for, convert
        from app.data_foundation.record_work import record_key, value_hash
        from app.data_foundation.record_models import CandidateRecord, RecordSubject
        source = session.get(SourceRef, work.source_ref_id)
        rows = rows_for(source, raw)
        from app.data_foundation.table_updates import is_deleted_change
        deleted_change = is_deleted_change(session, source)
        for candidate in candidates:
            if candidate.readiness != 'ready':
                continue
            typed = session.get(CandidateRecord, candidate.id)
            try:
                value = convert(source, rows[candidate.occurrence])
                if deleted_change:
                    value['field_quality']['source_deleted'] = 'FIXED_LOCAL_ROW_REMOVAL'
                subject = session.get(RecordSubject, typed.subject_id)
                if (typed.body_json != encode(value['body']) or typed.field_quality_json != encode(value['field_quality'])
                        or typed.business_key != record_key(source, value) or value_hash(typed) != candidate.values_hash
                        or (subject.source, subject.kind, subject.source_key) != (source.source, value['subject_kind'], value['subject_key'])):
                    mismatches.add(candidate.id)
            except (ValueError, TypeError, IndexError, AttributeError):
                mismatches.add(candidate.id)
        return mismatches
    if params['domain']=='tushare-local-daily-v1':
        from app.data_foundation.tushare import map_row
        for candidate in candidates:
            if candidate.readiness!='ready':continue
            typed=session.get(CandidateBar,candidate.id)
            try:
                expected,_=map_row(raw[candidate.occurrence],typed.instrument_id)
                if digest('bar-values',expected)!=candidate.values_hash or values(typed)!=expected:mismatches.add(candidate.id)
            except (FoundationError,ValueError,IndexError,TypeError,AttributeError):mismatches.add(candidate.id)
    elif params['domain']=='holdings-report-v1':
        from app.data_foundation.models import Binding,DependencyEntry
        from app.data_foundation.holding_work import report_input,HEADER_FIELDS,MEMBER_FIELDS,object_hash
        from app.data_foundation.holdings import normalize_report,ResolvedIdentity
        from app.data_foundation.holding_models import CandidateReport,CandidateReportMember
        from types import SimpleNamespace
        source=session.get(SourceRef,work.source_ref_id)
        subject,container,receipts=report_input(session,source)
        bindings=session.scalars(select(Binding).join(DependencyEntry,DependencyEntry.binding_id==Binding.id)
            .where(DependencyEntry.manifest_id==work.dependency_id)).all()
        def resolve(code,kind,start,end):
            matches=[b for b in bindings if b.source==source.source and b.subject==f'{kind}:{code}'
                and b.status=='resolved' and b.valid_from<=start<=end<b.valid_to]
            if len(matches)!=1:return None
            b=matches[0];return ResolvedIdentity(b.instrument_id,b.id,b.valid_from,b.valid_to,b.known_at)
        for candidate in candidates:
            expected=normalize_report(subject=subject,selected_key=candidate.unit_key,container=container,
                resolve_identity=resolve,receipt_requests=receipts)
            if expected['readiness']!=candidate.readiness:mismatches.add(candidate.id);continue
            if candidate.readiness!='ready':continue
            header=SimpleNamespace(**({k:expected['header'][k] for k in HEADER_FIELDS if k in expected['header']}
                | {'transport_complete':True,'portfolio_complete':None,'public_at':expected['header'].get('public_at')}))
            members=[SimpleNamespace(**({k:m[k] for k in MEMBER_FIELDS if k in m}
                | {'field_quality_json':encode(m['field_quality'])})) for m in expected['members']]
            typed=session.get(CandidateReport,candidate.id)
            stored=session.scalars(select(CandidateReportMember).where(CandidateReportMember.candidate_id==candidate.id)
                .order_by(CandidateReportMember.member_ordinal)).all()
            if object_hash(header,members)!=candidate.values_hash or object_hash(typed,stored)!=candidate.values_hash:mismatches.add(candidate.id)
    return mismatches
