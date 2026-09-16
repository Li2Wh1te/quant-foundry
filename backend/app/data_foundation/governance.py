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
    if policy.get('fallback', {}).get('enabled') or policy.get('comparison', {}).get('enabled'):
        raise FoundationError('GOVERNANCE_RULE_UNDEFINED', '首期未启用备用补缺或多源比较规则。')
    eligible = [c for c in candidates if c['source'] in policy['source_order'] and c['series'] == policy['series']]
    ready = [c for c in eligible if c['ready']]
    if len(ready) == 1 and len(eligible) == 1:
        return 'select', ready[0]['id'], 'SINGLE_SOURCE'
    if len(eligible) > 1:
        return 'block', None, 'GOVERNANCE_RULE_UNDEFINED'
    if eligible:
        return 'block', None, 'CORE_VALUE_INVALID'
    return 'gap', None, 'NO_ADMITTED_CANDIDATE'


def plan_actions(session, manifest_id, policy_id):
    manifest = session.get(CandidateManifest, manifest_id)
    if manifest is None:
        raise FoundationError('CANDIDATE_NOT_SEALED', '候选清单不存在。')
    origin = session.get(Work, manifest.work_id)
    if origin.status != 'succeeded':
        raise FoundationError('CANDIDATE_NOT_SEALED', '来源标准化尚未完整封存。')
    coverage = coverage_for(session, origin.id)
    if not coverage or coverage['status'] != 'pass':
        raise FoundationError('COVERAGE_DEPENDENCY_MISSING', '身份或日历适用范围尚未证实，不能自动发布。')
    policy = session.get(Definition, policy_id)
    if not policy or policy.kind != 'policy':
        raise FoundationError('POLICY_MISMATCH', '治理政策不存在。')
    definition = json.loads(policy.definition_json)
    groups = {}
    candidates = session.scalars(select(Candidate).join(CandidateEntry,CandidateEntry.candidate_id == Candidate.id)
        .where(CandidateEntry.manifest_id == manifest_id)).all()
    for candidate in candidates:
        bar = session.get(CandidateBar,candidate.id)
        checked = json.loads(session.get(Assessment,candidate.assessment_id).results_json)
        iid = str(bar.instrument_id) if bar else checked.get('instrument_id')
        day = str(bar.trade_date) if bar else checked.get('trade_date')
        if not iid or not day:
            raise FoundationError('UNLOCATED_CANDIDATE', '隔离记录无法定位业务键，需核实后再治理。')
        source = session.get(SourceRef,candidate.source_ref_id)
        groups.setdefault((iid,day),[]).append({'id':str(candidate.id), 'source':source.source,
            'series':bar.series if bar else json.loads(origin.parameters_json)['series'],
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
    actions = plan_actions(session,manifest.id,policy_id)
    return create_work(session,kind='B',contract_id=origin.contract_id,execution_id=execution_id,
        dependency_id=origin.dependency_id,parameters={**json.loads(origin.parameters_json),'actions':actions},
        candidate_manifest_id=manifest.id,policy_id=policy_id,parent_release_id=parent_release_id,
        expected_head_revision=expected_head_revision,expected_issue_epoch=expected_issue_epoch)


def verify_plan(session, work):
    """Real provider work cannot bypass planning with arbitrary actions."""
    from app.data_foundation.tushare import DOMAIN_KEY
    params=json.loads(work.parameters_json)
    manifest=session.get(CandidateManifest,work.candidate_manifest_id)
    origin=session.get(Work,manifest.work_id)
    original=json.loads(origin.parameters_json)
    if original['domain'] != DOMAIN_KEY and params['domain'] != DOMAIN_KEY:
        return
    if ({k:v for k,v in params.items() if k!='actions'} != original
            or work.dependency_id != origin.dependency_id
            or params['actions'] != plan_actions(session,manifest.id,work.policy_id)):
        raise FoundationError('GOVERNANCE_PLAN_MISMATCH', '治理计划与固定候选和政策推导不一致，未发布。')
