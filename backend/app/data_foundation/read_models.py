"""Truthful committed process summaries; absent evidence stays absent."""
import json
from sqlalchemy import select, text, func
from app.data_foundation.catalog import now
from app.data_foundation.work_models import Work, WorkEvent, Attempt, Release, Head, IssueScope, CandidateManifest, Unit

STEPS=('input','normalization','quality','governance','publication')


def processing_view(session, work):
    events=session.scalars(select(WorkEvent).where(WorkEvent.work_id==work.id).order_by(WorkEvent.sequence)).all()
    if work.kind=='B':
        from app.data_foundation.inputs import input_manifests
        manifests=input_manifests(session,work=work)
        source_events=session.scalars(select(WorkEvent).where(WorkEvent.work_id.in_([m.work_id for m in manifests]),
            WorkEvent.step.in_(['input','normalization','quality'])).order_by(WorkEvent.created_at,WorkEvent.work_id,WorkEvent.sequence)).all()
        events=source_events+[e for e in events if e.step not in ('input','normalization','quality')]
    attempts=session.scalars(select(Attempt).where(Attempt.work_id==work.id).order_by(Attempt.created_at,Attempt.epoch)).all()
    outputs=session.scalars(select(Release).where(Release.work_id==work.id,Release.status=='published')).all()
    head=session.get(Head,work.scope_key)
    issue=session.get(IssueScope,work.scope_key)
    # The router opens a repeatable-read snapshot before querying any rows;
    # selected work, current release and outputs therefore describe one view.
    snapshot=session.scalar(text('SELECT pg_current_snapshot()::text'))
    report = json.loads(work.parameters_json)['dataset'] == 'fund.holdings_report'
    members = session.scalar(select(func.sum(Unit.row_count)).where(Unit.work_id == work.id)) if report else None
    return {'id':str(work.id),'selected_work':str(work.id),'kind':work.kind,'status':work.status,
        'current_release':str(head.release_id) if head else None,'output_releases':[str(r.id) for r in outputs],
        'input_manifest':{'source_ref_id':work.source_ref_id,'candidate_manifest_id':work.candidate_manifest_id,
            'candidate_input_set_id':work.candidate_input_set_id,
            'dependency_id':work.dependency_id,'execution_id':work.execution_id,'fingerprint':work.fingerprint},
        'counters':{'processed':work.cursor,'total':work.total,'unit':'reports' if report else 'rows',
            **({'members': members or 0} if report else {})},'lease_epoch':work.lease_epoch,
        'steps':[{'step':step,'status':next((e.status for e in reversed(events) if e.step==step),'evidence_missing'),
            'events':[{'work_id':str(e.work_id),'sequence':e.sequence,'status':e.status,'message':e.message,'details':json.loads(e.details_json),'at':e.created_at} for e in events if e.step==step]} for step in STEPS],
        'attempts':[{'epoch':a.epoch,'outcome':a.outcome,'error_code':a.error_code,'details':json.loads(a.details_json),'at':a.created_at} for a in attempts],
        'issue_epoch':issue.epoch if issue else None,'as_of':now(),'view_snapshot_id':snapshot}
