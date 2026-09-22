"""Derive whole-record choices from sealed candidates before publication."""
import json
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError
from app.data_foundation.models import Definition, SourceRef
from app.data_foundation.work_models import Work, Candidate, CandidateBar, CandidateEntry, CandidateManifest, Assessment
from app.data_foundation.coverage import coverage_for
from app.data_foundation.publication import target_key


def choose(candidates, policy):
    """Unknown multi-source comparison and fallback rules fail closed."""
    comparison=policy.get('comparison', {})
    priority=comparison.get('enabled') is True and comparison.get('mode')=='source_priority'
    if policy.get('fallback', {}).get('enabled') or (comparison.get('enabled') and not priority):
        raise FoundationError('GOVERNANCE_RULE_UNDEFINED', '首期未启用备用补缺或多源比较规则。')
    eligible = [c for c in candidates if c['source'] in policy['source_order'] and c['series'] == policy['series']]
    if priority:
        order=policy['source_order']
        if not order or len(set(order))!=len(order):
            raise FoundationError('POLICY_MISMATCH','来源优先级必须明确且不能重复。')
        # No fallback is enabled: a missing or invalid primary cannot silently
        # acquire a secondary provider's values. Selection remains whole-row.
        primary=[c for c in eligible if c['source']==order[0]]
        if len(primary)>1:return 'block',None,'SOURCE_REVISION_AMBIGUOUS'
        if not primary:return 'gap',None,'PRIMARY_SOURCE_MISSING'
        if not primary[0]['ready']:return 'block',None,'CORE_VALUE_INVALID'
        return 'select',primary[0]['id'],'PRIMARY_SOURCE_PRIORITY'
    ready = [c for c in eligible if c['ready']]
    if len(ready) == 1 and len(eligible) == 1:
        return 'select', ready[0]['id'], 'SINGLE_SOURCE'
    if len(eligible) > 1:
        return 'block', None, 'GOVERNANCE_RULE_UNDEFINED'
    if eligible:
        return 'block', None, 'CORE_VALUE_INVALID'
    return 'gap', None, 'NO_ADMITTED_CANDIDATE'


def plan_actions(session, manifest_id, policy_id, *, input_set_id=None):
    from app.data_foundation.inputs import input_manifests
    from app.data_foundation.coverage import merge_coverage
    manifests = input_manifests(session, manifest_id=manifest_id, input_set_id=input_set_id)
    origins = [session.get(Work, m.work_id) for m in manifests]
    if any(w.status != 'succeeded' for w in origins):
        raise FoundationError('CANDIDATE_NOT_SEALED', '来源标准化尚未完整封存。')
    coverage = merge_coverage([coverage_for(session, w.id) for w in origins])
    if not coverage or coverage['status'] != 'pass':
        raise FoundationError('COVERAGE_DEPENDENCY_MISSING', '身份或日历适用范围尚未证实，不能自动发布。')
    policy = session.get(Definition, policy_id)
    if not policy or policy.kind != 'policy':
        raise FoundationError('POLICY_MISMATCH', '治理政策不存在。')
    definition = json.loads(policy.definition_json)
    groups = {}
    candidates = session.scalars(select(Candidate).join(CandidateEntry,CandidateEntry.candidate_id == Candidate.id)
        .where(CandidateEntry.manifest_id.in_([m.id for m in manifests]))).all()
    for candidate in candidates:
        bar = session.get(CandidateBar,candidate.id)
        checked = json.loads(session.get(Assessment,candidate.assessment_id).results_json)
        iid = str(bar.instrument_id) if bar else checked.get('instrument_id')
        day = str(bar.trade_date) if bar else checked.get('trade_date')
        if not iid or not day:
            raise FoundationError('UNLOCATED_CANDIDATE', '隔离记录无法定位业务键，需核实后再治理。')
        source = session.get(SourceRef,candidate.source_ref_id)
        groups.setdefault((iid,day),[]).append({'id':str(candidate.id), 'source':source.source,
            'series':bar.series if bar else json.loads(session.get(Work, candidate.work_id).parameters_json)['series'],
            'ready':candidate.readiness == 'ready' and bar is not None})
    actions=[]
    for key in coverage['expected_keys']:
        iid,day = key['instrument_id'],key['trade_date']
        action,cid,reason = choose(groups.get((iid,day),[]),definition)
        actions.append({'target_key':target_key(iid,day), 'instrument_id':iid, 'trade_date':day,
            'action':action,'candidate_id':cid,'parent_official_id':None,'reason':reason})
    return actions


def create_governance(session, *, normalization_id, execution_id, policy_id, parent_release_id=None,
                      expected_head_revision=0, expected_issue_epoch=0):
    from app.data_foundation.work import create_work
    origin = session.get(Work, normalization_id)
    manifest = session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == normalization_id))
    if origin is None or manifest is None:
        raise FoundationError('CANDIDATE_NOT_SEALED', '标准化工作尚无封存候选。')
    if json.loads(origin.parameters_json)['domain'] == 'typed-record-v1':
        from app.data_foundation.record_work import create_governance as create_record_governance
        return create_record_governance(session, normalization_id=normalization_id, execution_id=execution_id,
            policy_id=policy_id, parent_release_id=parent_release_id, expected_head_revision=expected_head_revision,
            expected_issue_epoch=expected_issue_epoch)
    actions = plan_actions(session,manifest.id,policy_id)
    return create_work(session,kind='B',contract_id=origin.contract_id,execution_id=execution_id,
        dependency_id=origin.dependency_id,parameters={**json.loads(origin.parameters_json),'actions':actions},
        candidate_manifest_id=manifest.id,policy_id=policy_id,parent_release_id=parent_release_id,
        expected_head_revision=expected_head_revision,expected_issue_epoch=expected_issue_epoch)


def verify_plan(session, work):
    """Real provider work cannot bypass planning with arbitrary actions."""
    from app.data_foundation.tushare import DOMAIN_KEY
    params=json.loads(work.parameters_json)
    if work.candidate_input_set_id:
        verify_multi_plan(session, work)
        return
    manifest=session.get(CandidateManifest,work.candidate_manifest_id)
    origin=session.get(Work,manifest.work_id)
    original=json.loads(origin.parameters_json)
    if original['domain'] != DOMAIN_KEY and params['domain'] != DOMAIN_KEY:
        return
    if ({k:v for k,v in params.items() if k!='actions'} != original
            or work.dependency_id != origin.dependency_id
            or params['actions'] != plan_actions(session,manifest.id,work.policy_id)):
        raise FoundationError('GOVERNANCE_PLAN_MISMATCH', '治理计划与固定候选和政策推导不一致，未发布。')


def create_multi_governance(session, *, input_set_id, execution_id, policy_id,
                          parent_release_id=None, expected_head_revision=0, expected_issue_epoch=0):
    from app.data_foundation.inputs import input_manifests, combined_parameters
    from app.data_foundation.catalog import register_dependencies
    from app.data_foundation.models import DependencyEntry
    from app.data_foundation.work import create_work
    manifests = input_manifests(session, input_set_id=input_set_id)
    origins = [session.get(Work, m.work_id) for m in manifests]
    params = combined_parameters(session, manifests)
    # Retain every actual dependency. The union is canonicalized by the existing
    # registrar; a representative input must never stand in for other workers.
    entries = []
    for origin in origins:
        for entry in session.scalars(select(DependencyEntry).where(DependencyEntry.manifest_id == origin.dependency_id)):
            key = next(k for k in ('source_ref_id','binding_id','execution_id','definition_id') if getattr(entry,k))
            entries.append({key:getattr(entry,key), 'purpose':entry.purpose})
    unique = {json.dumps(e, sort_keys=True, default=str):e for e in entries}
    dependency = register_dependencies(session, list(unique.values()))
    if params['domain'] == 'holdings-report-v1':
        from app.data_foundation.holding_work import plan_actions as plan_reports
        actions = plan_reports(session, None, policy_id, parent_release_id, input_set_id=input_set_id)
    else:
        actions = plan_actions(session, None, policy_id, input_set_id=input_set_id)
    from app.data_foundation.batch_models import WorkSourcePointer
    from app.data_ingestion.models.tonghuashun import TonghuashunCollectionState as State
    from app.data_foundation.canonical import digest
    pointers={}
    for origin in origins:
        source=session.get(SourceRef,origin.source_ref_id)
        if source.representation!='ths_observation':continue
        state=session.get(State,(source.dataset,source.subject,source.variant),populate_existing=True)
        if state is None or state.observation_id!=source.observation_id:
            raise FoundationError('SOURCE_CONTEXT_CHANGED','自动治理的来源不是当前已观察版本。')
        pointers[source.id]=state.revision
    params['source_context_hash']=digest('governance-source-context-v1',sorted(pointers.items(),key=lambda x:str(x[0])))
    work = create_work(session, kind='B', contract_id=origins[0].contract_id, execution_id=execution_id,
        dependency_id=dependency.id, parameters={**params,'actions':actions}, candidate_input_set_id=input_set_id,
        policy_id=policy_id, parent_release_id=parent_release_id,
        expected_head_revision=expected_head_revision, expected_issue_epoch=expected_issue_epoch)
    for source_id,revision in pointers.items():
        old=session.get(WorkSourcePointer,(work.id,source_id))
        if old is None:session.add(WorkSourcePointer(work_id=work.id,source_ref_id=source_id,revision=revision))
    session.flush()
    return work


def verify_multi_plan(session, work):
    from app.data_foundation.inputs import input_manifests, combined_parameters
    manifests = input_manifests(session, work=work)
    params = json.loads(work.parameters_json)
    fixed = combined_parameters(session, manifests)
    from app.data_foundation.batch_models import WorkSourcePointer
    from app.data_foundation.canonical import digest
    pointers=session.scalars(select(WorkSourcePointer).where(WorkSourcePointer.work_id==work.id).order_by(WorkSourcePointer.source_ref_id)).all()
    fixed['source_context_hash']=digest('governance-source-context-v1',[(p.source_ref_id,p.revision) for p in pointers])
    if params['domain'] == 'holdings-report-v1':
        from app.data_foundation.holding_work import plan_actions as plan_reports
        actions = plan_reports(session, None, work.policy_id, work.parent_release_id, input_set_id=work.candidate_input_set_id)
    else:
        actions = plan_actions(session, None, work.policy_id, input_set_id=work.candidate_input_set_id)
    from app.data_foundation.models import DependencyEntry
    columns=('source_ref_id','binding_id','execution_id','definition_id','purpose')
    def dependencies(wid):
        return {tuple(getattr(e,k) for k in columns) for e in session.scalars(select(DependencyEntry).where(DependencyEntry.manifest_id==wid))}
    expected=set().union(*(dependencies(session.get(Work,m.work_id).dependency_id) for m in manifests))
    if dependencies(work.dependency_id)!=expected:
        raise FoundationError('GOVERNANCE_PLAN_MISMATCH','治理依赖未完整覆盖实际输入。')
    if params != {**fixed,'actions':actions}:
        raise FoundationError('GOVERNANCE_PLAN_MISMATCH', '多输入治理计划与封存候选不一致。')
