"""Bounded M3 official reads shared by HTTP and authenticated Python callers.

A request fixes one release, then projects immutable typed values. Calendar
applicability was persisted during normalization, so neither provider readers
nor source availability participate in ordinary official queries.
"""
from datetime import date
import json
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text

from app.data_foundation.canonical import FoundationError, encode, digest
from app.data_foundation.catalog import now
from app.data_foundation.coverage import coverage_for
from app.data_foundation.models import Definition, SourceRef
from app.data_foundation.quality import read_release
from app.data_foundation.tushare import SERIES
from app.data_foundation.work import scope_key
from app.data_foundation.work_models import (Work, Candidate, CandidateManifest, Release, Head,
    IssueScope, BlockMember, BlockRef, OfficialBar, Decision, Assessment, WorkEvent)

FIELDS = ('open','high','low','close','volume','turnover')
DATASET = 'market.bar.daily'


class BusinessRange(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    start: date = Field(alias='from')
    end: date = Field(alias='to')


class DataRequirement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str
    contract_version: str = Field(pattern=r'^\d+\.\d+$')
    profile_id: str
    semantic_series_id: str
    subjects: list[UUID] = Field(min_length=1,max_length=100)
    business_range: BusinessRange
    fields: list[Literal['open','high','low','close','volume','turnover']] = Field(default_factory=lambda:list(FIELDS[:4]),min_length=1)
    release: UUID | Literal['latest'] = 'latest'
    require_complete: bool = True
    allow_partial: bool = False
    time_mode: Literal['observed','latest_known','strict_public_pit'] = 'observed'
    max_staleness_days: int | None = Field(default=None,ge=0)

    @model_validator(mode='after')
    def validate_request(self):
        if self.business_range.end < self.business_range.start or len(set(self.fields)) != len(self.fields):
            raise ValueError('日期区间或重复字段无效')
        self.subjects = sorted(set(self.subjects),key=str)
        self.fields = [f for f in FIELDS if f in self.fields]
        # This deliberately conservative cap bounds even an unknown calendar.
        # Paging bounds values, while applicability still covers this full range.
        if (self.business_range.end-self.business_range.start).days+1 > 1000//len(self.subjects):
            raise ValueError('首期请求最多1000个标的自然日，请缩小范围')
        return self


def resolve_release(session, request):
    if request.dataset_id != DATASET or request.contract_version != '1.0' or request.profile_id != 'default':
        raise FoundationError('INVALID_REQUIREMENT','首期仅支持日线1.0默认配置。')
    scope = scope_key(DATASET,1,request.profile_id,request.semantic_series_id)
    if request.release == 'latest':
        head = session.get(Head,scope)
        return session.get(Release,head.release_id) if head else None
    release = session.get(Release,request.release)
    if release is None or release.status != 'published':
        raise FoundationError('RELEASE_UNAVAILABLE','指定正式发布不存在。')
    work=session.get(Work,release.work_id)
    contract=session.get(Definition,work.contract_id)
    if release.scope_key != scope or contract.version != request.contract_version:
        raise FoundationError('CONTRACT_RELEASE_MISMATCH','请求契约或语义与指定正式版本不符。')
    return release


def release_origin(session, release):
    work=session.get(Work,release.work_id)
    manifest=session.get(CandidateManifest,work.candidate_manifest_id)
    return work,session.get(Work,manifest.work_id)


def query_official(session, request, authenticate, *, check_only=False, page_after=None,
                   page_size=None, expected_issue_epoch=None, excluded_after=None):
    owner=authenticate()
    if not owner:
        raise FoundationError('AUTH_REQUIRED','正式查询需要有效身份。')
    release=resolve_release(session,request)
    requirements=[]
    def requirement(key,result,reason,message):
        requirements.append({'id':key,'result':result,'reason_code':reason,'message':message,
            'scope':request.model_dump(mode='json',by_alias=True)['business_range'],'evidence_refs':
            [str(release.id)] if release else []})
    response={'representation':'official','contract_version':'1.0','request':request.model_dump(mode='json',by_alias=True),
        'release_id':str(release.id) if release else None,'checked_at':now(), 'read_guard_at':None,
        'as_of':now(),'view_snapshot_id':session.scalar(text('SELECT pg_current_snapshot()::text')),
        'issue_state_version':None,'items':[],'requirements':requirements,'next_cursor':None}
    if release is None:
        requirement('publication','fail','NO_OFFICIAL_RELEASE','所选范围和语义尚无正式发布。')
        response.update(state='unavailable',request_satisfied=False,scope_summary={'expected_business_keys':None,'official_keys':0,'currently_readable_keys':0})
    else:
        work,origin=release_origin(session,release)
        # Acquire the current issue lock before evaluating or serializing any
        # values, even when a capability request ultimately returns no items.
        guard=session.scalar(select(IssueScope).where(IssueScope.scope_key==release.scope_key)
            .with_for_update(read=True).execution_options(populate_existing=True))
        if guard is None:
            raise FoundationError('DEPENDENCY_MISSING','正式读取缺少当前问题上下文。')
        if expected_issue_epoch is not None and guard.epoch != expected_issue_epoch:
            raise FoundationError('READ_CONTEXT_CHANGED','当前数据限制已变化，请基于原版本重新检查。')
        raw=json.loads(read_release(session,release_id=release.id,expected_issue_epoch=guard.epoch,authenticate=authenticate,
            instrument_ids=request.subjects,start=request.business_range.start,end=request.business_range.end,
            fields=request.fields,allow_partial=True,keys_only=True))
        coverage=coverage_for(session,origin.id)
        ids={str(i) for i in request.subjects}
        start,end=str(request.business_range.start),str(request.business_range.end)
        known=(coverage is not None and coverage['status']=='pass' and coverage['start']<=start<=end<=coverage['end']
            and ids <= {s['instrument_id'] for s in coverage['subjects']})
        expected={(k['instrument_id'],k['trade_date']) for k in coverage['expected_keys']
            if k['instrument_id'] in ids and start<=k['trade_date']<=end} if coverage else set()
        all_keys={(i['instrument_id'],i['trade_date']) for i in raw['items']+raw['gaps']}
        readable={(i['instrument_id'],i['trade_date']) for i in raw['items']}
        missing=expected-all_keys
        gaps=raw['gaps']+[{'instrument_id':i,'trade_date':d,'reason':'COVERAGE_GAP'} for i,d in sorted(missing)]
        complete=known and not gaps and readable==expected
        requirement('coverage','not_required' if not request.require_complete else 'pass' if complete else 'fail' if gaps else 'unknown',
            None if complete else 'COVERAGE_GAP' if gaps else 'COVERAGE_DEPENDENCY_MISSING',
            '本次未要求完整覆盖；已有缺口仍保留，不能据此推断区间完整。' if not request.require_complete else '固定日历范围内的正式业务键完整。' if complete else '正式范围有缺口或字段限制。' if gaps else '日历或主体适用范围证据不足。')
        field_missing=any(g['reason']=='field_unavailable' for g in raw['gaps'])
        if field_missing:
            requirement('fields','fail','UNIT_UNVERIFIED' if 'volume' in request.fields else 'OPTIONAL_FIELD_MISSING',
                '所选字段在正式版本中有缺项；成交量换算尚未证实。' if 'volume' in request.fields else '所选可选字段缺失或质量未通过。')
        if any(g['reason']=='current_issue' for g in raw['gaps']):
            requirement('issues','fail','CONFIRMED_DATA_ISSUE','当前问题限制了所选字段或修订。')
        time_ok=request.time_mode=='observed'
        if not time_ok:
            requirement('time','unknown','PUBLIC_TIME_UNPROVEN','首期仅提供本地观察语义，不能证明历史公开时间或最新知识完整性。')
        members=session.scalars(select(BlockMember).join(BlockRef,BlockRef.block_id==BlockMember.block_id).where(
            BlockRef.release_id==release.id,BlockMember.instrument_id.in_(request.subjects),
            BlockMember.trade_date.between(request.business_range.start,request.business_range.end))).all()
        official_count=sum(m.state=='value' for m in members)
        latest=max((str(m.trade_date) for m in members if m.state=='value'),default=None)
        # Freshness is evaluated per subject against the requested end date.
        # A newer neighbour must not conceal stale data for another instrument.
        stale=[]
        for instrument in sorted(ids):
            subject_dates=[str(m.trade_date) for m in members if m.state=='value' and str(m.instrument_id)==instrument]
            zero=known and not any(i==instrument for i,d in expected)
            if request.max_staleness_days is not None and not zero and (not subject_dates or
                (request.business_range.end-date.fromisoformat(max(subject_dates))).days>request.max_staleness_days):
                stale.append(instrument)
        if stale:
            requirement('freshness','fail','STALE_BUSINESS_DATE','部分标的业务截至未满足相对请求结束日的新鲜度要求。')
            requirements[-1]['scope']['subjects']=stale
        effective=readable-{k for k in readable if k[0] in stale}
        gaps += [{'instrument_id':i,'trade_date':d,'reason':'STALE_BUSINESS_DATE'} for i,d in readable if i in stale]
        constrained=any(g['reason']!='gap' for g in raw['gaps']) or bool(stale)
        if not effective and (gaps or (known and expected)):
            state='unavailable'
        elif not time_ok:
            state='unknown'
        elif not request.require_complete and effective:
            state='partial' if constrained else 'available'
        elif request.require_complete and effective and gaps:
            state='partial'
        elif not known:
            state='unknown'
        elif complete and not stale:
            state='available'
        elif effective and gaps:
            state='partial'
        else:
            state='unavailable'
        response.update(state=state,request_satisfied=state=='available',issue_state_version=guard.epoch,
            scope_summary={'expected_business_keys':len(expected) if known else None,'official_keys':official_count,
                'currently_readable_keys':len(effective)}, excluded=gaps,manifest_hash=release.manifest_hash,
            policy_id=str(work.policy_id),source_observed_at=session.get(SourceRef,origin.source_ref_id).observed_at,
            business_as_of=latest,published_at=release.published_at)
        response['total_readable']=len(effective)
        response['assessment_hash']=digest(
            'read-assessment-v1',{'release':str(release.id),'request':response['request'],
            'issue_epoch':guard.epoch,'requirements':requirements,'state':state})
        response['excluded_total']=len(gaps)
        ordered_gaps=sorted(gaps,key=lambda g:(g['trade_date'],g['instrument_id'],g['reason']))
        if page_size is not None:
            if excluded_after:
                ordered_gaps=[g for g in ordered_gaps if (g['trade_date'],g['instrument_id'],g['reason'])>tuple(excluded_after)]
            response['excluded']=ordered_gaps[:page_size]
            response['excluded_has_more']=len(ordered_gaps)>page_size
        if not check_only and (state=='available' or state=='partial' and request.allow_partial):
            ordered=sorted(effective,key=lambda k:(k[1],k[0]))
            if page_after:
                ordered=[k for k in ordered if (k[1],k[0])>tuple(page_after)]
            selected=ordered if page_size is None else ordered[:page_size]
            values=json.loads(read_release(session,release_id=release.id,expected_issue_epoch=guard.epoch,
                authenticate=authenticate,instrument_ids=request.subjects,start=request.business_range.start,
                end=request.business_range.end,fields=request.fields,allow_partial=True,selected_keys=set(selected)))
            response['items']=sorted(values['items'],key=lambda i:(i['trade_date'],i['instrument_id']))
            response['has_more']=page_size is not None and len(ordered)>page_size
        response.setdefault('has_more',False)
        response['returned']=len(response['items'])
    if authenticate()!=owner:
        raise FoundationError('AUTH_CONTEXT_CHANGED','当前认证上下文已改变。')
    response['read_guard_at']=now()
    return encode(response).encode()


def dataset_view(session):
    scope=scope_key(DATASET,1,'default',SERIES)
    head=session.get(Head,scope)
    release=session.get(Release,head.release_id) if head else None
    works=session.scalars(select(Work).where(Work.scope_key==scope).order_by(Work.created_at.desc(),Work.id.desc()).limit(20)).all()
    normalizations=session.scalars(select(Work).where(Work.scope_key==scope,Work.kind=='A').order_by(Work.created_at.desc(),Work.id.desc()).limit(1)).all()
    origin=release_origin(session,release)[1] if release else normalizations[0] if normalizations else None
    coverage=coverage_for(session,origin.id) if origin else None
    params=json.loads(origin.parameters_json) if origin else {}
    latest_a=normalizations[0] if normalizations else None
    candidates=session.scalars(select(Candidate).where(Candidate.work_id==latest_a.id)).all() if latest_a else []
    members=session.scalars(select(BlockMember).join(BlockRef,BlockRef.block_id==BlockMember.block_id)
        .where(BlockRef.release_id==release.id)).all() if release else []
    source=session.get(SourceRef,origin.source_ref_id) if origin else None
    result={'dataset':DATASET,'version':'1.0','name':'ETF 未复权日线','series':SERIES,'profile':'default',
        'view_snapshot_id':session.scalar(text('SELECT pg_current_snapshot()::text')),
        'read_status':'implemented','update_status':'bounded_manual','current_release':str(release.id) if release else None,
        'published_at':release.published_at if release else None,'source_observed_at':source.observed_at if source else None,
        'business_as_of':max((m.trade_date for m in members if m.state=='value'),default=None),
        'range':{'from':params.get('start'),'to':params.get('end')},'subjects':coverage['subjects'] if coverage else [],
        'source_records':latest_a.total if latest_a else None,'candidate_records':len(candidates),
        'candidate_work_id':str(latest_a.id) if latest_a else None,
        'pending_governance':bool(latest_a and (not release or latest_a.id!=origin.id)),
        'quarantined_records':sum(c.readiness=='quarantined' for c in candidates),
        'official_keys':sum(m.state=='value' for m in members),'coverage_status':coverage['status'] if coverage else 'unknown',
        'expected_business_keys':len(coverage['expected_keys']) if coverage and coverage['status']=='pass' else None,
        'work_ids':[str(w.id) for w in works],
        'work_summaries':[{'id':str(w.id),'label':('标准化' if w.kind=='A' else '治理发布')+' · '+w.created_at.strftime('%m-%d %H:%M UTC')} for w in works], 'as_of':now(),
        'fields':[{'key':f,'name':n,'unit':u} for f,n,u in zip(FIELDS,['开盘价','最高价','最低价','收盘价','成交量','成交额'],['元/份']*4+['份','元'])],
        'limitations':['成交量换算未证实，正式值为空。','仅支持已核验的本地观察范围，不提供历史公开时点保证。']}
    from app.data_foundation.work_models import Issue
    history=session.scalars(select(Issue).where(Issue.scope_key==scope).order_by(Issue.revision)).all()
    current={i.issue_id:i for i in history}
    result['active_issue_count']=sum(i.state in ('suspected','confirmed') for i in current.values())
    if result['active_issue_count']:
        result['limitations'].append('当前存在数据问题记录，正式查询将按字段和范围复核限制。')
    if origin:
        finished=session.scalar(select(WorkEvent.created_at).where(WorkEvent.work_id==origin.id,WorkEvent.status=='succeeded').order_by(WorkEvent.sequence.desc()).limit(1))
        # Decimal-free integral seconds remain honest about unknown timestamps.
        result['normalization_delay_seconds']=int((finished-source.observed_at).total_seconds()) if finished and source else None
        result['publication_delay_seconds']=int((release.published_at-finished).total_seconds()) if release and finished else None
    return result


def lineage(session, official_id):
    official=session.get(OfficialBar,official_id)
    if official is None:
        raise FoundationError('RELEASE_UNAVAILABLE','正式修订不存在。')
    published=session.scalar(select(BlockRef.release_id).join(BlockMember,BlockMember.block_id==BlockRef.block_id)
        .join(Release,Release.id==BlockRef.release_id).where(BlockMember.official_id==official.id,Release.status=='published').limit(1))
    if published is None:
        raise FoundationError('RELEASE_UNAVAILABLE','该修订尚未正式发布。')
    candidate=session.get(Candidate,official.candidate_id)
    decision=session.get(Decision,official.decision_id)
    source=session.get(SourceRef,candidate.source_ref_id)
    # Metadata contains no unrestricted historical field values or credentials.
    return {'official_id':official.id,'decision_id':decision.id,'candidate_id':candidate.id,
        'source_ref_id':source.id,'source':source.source,'source_hash':source.content_hash,
        'binding_id':candidate.binding_id,'dependency_id':candidate.dependency_id,
        'assessment_id':candidate.assessment_id,'policy':json.loads(decision.evidence_json),
        'candidate_quality':json.loads(session.get(Assessment,candidate.assessment_id).results_json)}
