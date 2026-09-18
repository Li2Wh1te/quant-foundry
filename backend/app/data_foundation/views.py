"""Bounded operator views with immutable evidence and current restrictions."""
import json
from datetime import timezone
from sqlalchemy import select, func
from app.data_foundation.canonical import FoundationError
from app.data_foundation.models import Definition, Execution
from app.data_foundation.work_models import Work, Release, BlockRef, BlockMember, IssueScope, CandidateManifest
from app.data_foundation.query import resolve_release
from app.data_foundation.quality import read_release
from app.data_foundation.read_models import processing_view
from app.data_foundation.catalog import now


def release_list(session, scope, before=None, limit=20):
    statement=select(Release).where(Release.scope_key==scope,Release.status=='published')
    if before:
        anchor=session.get(Release,before)
        if anchor is None or anchor.scope_key!=scope:raise FoundationError('INVALID_REQUIREMENT','正式版本分页位置无效。')
        from sqlalchemy import tuple_
        statement=statement.where(tuple_(Release.published_at,Release.id)<(anchor.published_at,anchor.id))
    rows=session.scalars(statement.order_by(Release.published_at.desc(),Release.id.desc()).limit(limit+1)).all()
    return {'items':[{'id':r.id,'parent_id':r.parent_id,'published_at':r.published_at,
        'manifest_hash':r.manifest_hash,'work_id':r.work_id} for r in rows[:limit]],
        'next_cursor':str(rows[limit-1].id) if len(rows)>limit else None}


def process_detail(session,work):
    result=processing_view(session,work)
    execution=session.get(Execution,work.execution_id)
    manifest=json.loads(execution.manifest_json) if execution else {}
    contract=session.get(Definition,work.contract_id)
    policy=session.get(Definition,work.policy_id) if work.policy_id else None
    # Expose only version/hash references, not arbitrary runtime settings or
    # raw exception detail that may contain credentials or signed URLs.
    rules={'contract':{'version':contract.version,'hash':contract.content_hash},
        'execution':{'id':str(work.execution_id),'hash':execution.manifest_hash if execution else None}}
    for name in ('transform','quality','governance','parser'):
        value=manifest.get(name,{})
        if isinstance(value,dict):rules[name]={k:value[k] for k in ('version','hash') if k in value}
    if policy:rules['policy']={'id':str(policy.id),'name':policy.name,'version':policy.version,'hash':policy.content_hash}
    result['rules']=rules
    params=json.loads(work.parameters_json)
    result['scope']={k:params.get(k) for k in ('dataset','major','profile','series','start','end')}
    descriptions={'input':'读取已固定的本地来源，不请求供应商。','normalization':'按当次转换版本解释身份、价格及金额单位。',
        'quality':'核验核心字段、可选字段与逐日覆盖证据。','governance':'按固定候选和当次治理规则选择整条正式记录；单源比较不适用。',
        'publication':'提交不可变正式清单，未提交内容不对普通读取可见。'}
    result['authorized_actions']=['view','copy_safe_summary']
    result['evidence_visibility']='metadata_only'
    for attempt in result['attempts']:
        attempt.pop('details',None)
    upstream=None
    if work.kind=='B':
        fixed=session.get(CandidateManifest,work.candidate_manifest_id)
        upstream=process_detail(session,session.get(Work,fixed.work_id))
    for step in result['steps']:
        for event in step['events']:
            detail=event['details']
            event['details']={k:v for k,v in detail.items() if k in ('checkpoint','checkpoint_advanced','committed_rows',
                'total_rows','ready','quarantined','release_id','manifest_hash','coverage')}
        owner=upstream if upstream and step['step'] in ('input','normalization','quality') else result
        step['detail']={'input':owner['input_manifest'],'processing':descriptions[step['step']],
            'output':{'events':len(step['events']),'releases':result['output_releases'] if step['step']=='publication' else []},
            'basis':owner['rules'],'impact':'本次输出与当前正式发布独立展示；工作完成不等于任意请求可用。',
            'next_step':'查看固定依据与当前请求检查；执行失败由维护者核验后处理。'}
    result['diagnostic']={'dataset':params.get('dataset'),'scope':result['scope'],'work_id':str(work.id),
        'current_release':result['current_release'],'output_releases':result['output_releases'],
        'status':work.status,'checked_at':result['as_of']}
    return result


def release_changes(session, requirement, previous_id, current_id, authenticate, *, after=None, limit=50):
    previous=resolve_release(session,requirement.model_copy(update={'release':previous_id}))
    current=resolve_release(session,requirement.model_copy(update={'release':current_id}))
    if previous.scope_key!=current.scope_key:raise FoundationError('CONTRACT_RELEASE_MISMATCH','只能比较相同契约和语义的正式发布。')
    guard=session.scalar(select(IssueScope).where(IssueScope.scope_key==current.scope_key).with_for_update(read=True)
        .execution_options(populate_existing=True))
    def members(release):
        rows=session.scalars(select(BlockMember).join(BlockRef,BlockRef.block_id==BlockMember.block_id).where(
            BlockRef.release_id==release.id,BlockMember.instrument_id.in_(requirement.subjects),
            BlockMember.trade_date.between(requirement.business_range.start,requirement.business_range.end))).all()
        return {(str(m.trade_date),str(m.instrument_id)):m for m in rows}
    old,new=members(previous),members(current)
    keys=sorted(set(old)|set(new))
    if after:keys=[k for k in keys if k>tuple(after)]
    chosen=keys[:limit]
    selected={(i,d) for d,i in chosen}
    def values(release):
        result={}
        for field in requirement.fields:
            raw=json.loads(read_release(session,release_id=release.id,expected_issue_epoch=guard.epoch,
                authenticate=authenticate,instrument_ids=requirement.subjects,start=requirement.business_range.start,
                end=requirement.business_range.end,fields=[field],allow_partial=True,selected_keys=selected))
            for row in raw['items']:result.setdefault((row['trade_date'],row['instrument_id']),{})[field]=row[field]
        return result
    before,after_values=values(previous),values(current)
    items=[]
    for key in chosen:
        a,b=old.get(key),new.get(key)
        a_values,b_values=before.get(key,{}),after_values.get(key,{})
        restricted=(a is not None and a.state=='value' and len(a_values)!=len(requirement.fields)) or (b is not None and b.state=='value' and len(b_values)!=len(requirement.fields))
        if b is None:kind='absent'
        elif b.state!='value':kind=b.state
        elif a is None or a.state!='value':kind='added'
        elif a.official_id==b.official_id and a.decision_id==b.decision_id:kind='inherited'
        elif restricted:kind='restricted_comparison'
        elif a_values!=b_values:kind='value_changed'
        else:kind='basis_changed'
        items.append({'trade_date':key[0],'instrument_id':key[1],'kind':kind,
            'before':a_values,'after':b_values,'restricted_fields':[f for f in requirement.fields if f not in a_values or f not in b_values],
            'before_decision':str(a.decision_id) if a else None,'after_decision':str(b.decision_id) if b else None})
    return {'representation':'official','previous_release':str(previous.id),'release_id':str(current.id),
        'issue_state_version':guard.epoch,'checked_at':now(),'items':items,'has_more':len(keys)>limit,
        'next_key':list(chosen[-1]) if len(keys)>limit else None,
        'rules':{'before':process_detail(session,session.get(Work,previous.work_id))['rules'],
                 'after':process_detail(session,session.get(Work,current.work_id))['rules']}}
