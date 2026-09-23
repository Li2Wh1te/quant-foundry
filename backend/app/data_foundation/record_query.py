"""Bounded typed-record reads over immutable releases, without source decoding."""
import json
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.catalog import now
from app.data_foundation.query import BusinessRange
from app.data_foundation.record_schemas import schema_for
from app.data_foundation.record_models import RecordBlockMember, OfficialRecord, RecordSubject, RecordIssue
from app.data_foundation.work_models import BlockRef, IssueScope, Candidate, Decision, Work
from app.data_foundation.models import SourceRef
from app.data_foundation.projection import projection_for, resolve_projected_release


class RecordRequirement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: Literal['fund.manager_performance_window', 'fund.manager_style_snapshot', 'fund.return_snapshot', 'fund.drawdown_snapshot', 'fund.performance_window', 'market.dragon_tiger_snapshot', 'market.auction_snapshot', 'market.auction_benchmark_snapshot', 'market.limit_up_snapshot', 'market.limit_down_snapshot', 'market.limit_break_snapshot', 'market.anomaly_snapshot', 'market.limit_ladder_window', 'market.stock_quote_snapshot', 'market.etf_quote_snapshot', 'market.index_quote_snapshot', 'market.stock_valuation_snapshot', 'instrument.reference', 'fund.company', 'fund.manager', 'fund.profile', 'fund.manager_experience', 'fund.nav_snapshot', 'fund.offering_snapshot', 'market.stock_daily_window', 'market.etf_daily_window', 'market.index_daily_window', 'fund.quota_summary_snapshot', 'fund.quota_list_snapshot', 'market.popularity_snapshot', 'market.rising_popularity_snapshot', 'market.popularity_history_snapshot', 'market.calendar',
                        'market.adjustment_factor', 'market.fund_daily', 'index.category_snapshot', 'index.constituent_snapshot']
    contract_version: str = Field(default='1.0', pattern=r'^[1-9]\d*\.\d+$')
    profile_id: Literal['default'] = 'default'
    semantic_series_id: str = Field(min_length=1, max_length=128)
    subjects: list[UUID] = Field(min_length=1, max_length=100)
    business_range: BusinessRange | None = None
    record_keys: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    fields: list[str] = Field(min_length=1, max_length=100)
    release: UUID | Literal['latest'] = 'latest'
    require_complete: bool = False
    allow_partial: bool = False
    time_mode: Literal['observed', 'latest_known', 'strict_public_pit'] = 'observed'
    paging_version: Literal[1] = 1
    page_size: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode='after')
    def validate_request(self):
        schema = schema_for(self.dataset_id)
        if len(set(self.fields)) != len(self.fields) or not set(self.fields) <= set(schema.body.model_fields):
            raise ValueError('字段不属于所选领域契约或重复')
        if schema.date_field is not None and self.business_range is None:
            raise ValueError('日期型领域读取必须指定业务日期范围')
        if schema.date_field is None and self.business_range is not None:
            raise ValueError('资料观察记录没有业务日期，不能使用日期过滤冒充历史时点读取')
        if self.business_range and self.business_range.start > self.business_range.end:
            raise ValueError('业务日期范围无效')
        if self.record_keys and any(len(k) != 64 or any(c not in '0123456789abcdef' for c in k) for k in self.record_keys):
            raise ValueError('业务对象键无效')
        self.subjects = sorted(set(self.subjects), key=str)
        self.fields = sorted(self.fields)
        if self.record_keys:
            self.record_keys = sorted(set(self.record_keys))
        return self


def restrictions(session, scope):
    rows = session.scalars(select(RecordIssue).where(RecordIssue.scope_key == scope)
        .order_by(RecordIssue.revision)).all()
    latest = {r.issue_id: r for r in rows}
    # Resolving a confirmed issue does not rehabilitate its old immutable value.
    return [r for r in latest.values() if r.state in ('suspected', 'confirmed')] + [
        r for r in rows if r.state == 'confirmed' and r.official_id is not None]


def evaluate(session, request, authenticate, *, expected_epoch=None, after=None, page_size=100, check_only=False):
    owner = authenticate()
    if not owner:
        raise FoundationError('AUTH_REQUIRED', '正式领域读取需要有效身份。')
    projection_for(session, request.dataset_id, request.contract_version)
    release = resolve_projected_release(session, request)
    result = dict(dataset=request.dataset_id, representation='official',
        release_id=str(release.id) if release else None, manifest_hash=release.manifest_hash if release else None,
        checked_at=now(), request=request.model_dump(mode='json', by_alias=True), items=[], excluded=[],
        has_more=False, next_key=None, issue_state_version=0, request_satisfied=False, state='unavailable',
        coverage='published_keys_only', identity_basis='source_local_observed', requirements=[], scope_summary={})
    if release is None:
        result['requirements'] = [{'id': 'release', 'result': 'fail', 'reason_code': 'RELEASE_UNAVAILABLE'}]
        return result
    guard = session.scalar(select(IssueScope).where(IssueScope.scope_key == release.scope_key)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if guard is None:
        raise FoundationError('DEPENDENCY_MISSING', '领域读取缺少当前问题上下文。')
    if expected_epoch is not None and expected_epoch != guard.epoch:
        raise FoundationError('READ_CONTEXT_CHANGED', '当前读取限制已改变，请重新检查固定发布。')
    result['issue_state_version'] = guard.epoch
    statement = select(RecordBlockMember).join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id).where(
        BlockRef.release_id == release.id, RecordBlockMember.subject_id.in_(request.subjects))
    if request.business_range:
        statement = statement.where(RecordBlockMember.business_date.between(request.business_range.start, request.business_range.end))
    if request.record_keys:
        statement = statement.where(RecordBlockMember.target_key.in_(request.record_keys))
    members = session.scalars(statement.order_by(RecordBlockMember.target_key).limit(1001)).all()
    # Capability is evaluated over the entire requested bounded set, not just
    # the first page. Larger callers partition their subject/date/key requests.
    if len(members) > 1000:
        raise FoundationError('INVALID_REQUIREMENT', '一次领域请求最多1000个对象，请缩小主体、日期或对象键范围。')
    issues = restrictions(session, release.scope_key)
    ids = {m.official_id for m in members if m.official_id} | {i.official_id for i in issues if i.official_id}
    rows = session.scalars(select(OfficialRecord).where(OfficialRecord.id.in_(ids))).all() if ids else []
    official = {r.id: r for r in rows}
    bodies = {r.id: json.loads(r.body_json) for r in rows}
    source_ids = dict(session.execute(select(Candidate.id, Candidate.source_ref_id)
        .where(Candidate.id.in_([r.candidate_id for r in rows]))).all()) if rows else {}
    readable = []
    for member in members:
        row = official.get(member.official_id)
        reason = None if member.state == 'value' and row else member.state
        for issue in issues:
            fields = set(request.fields) & set(json.loads(issue.fields_json))
            if issue.target_key != member.target_key or not fields:
                continue
            old = official.get(issue.official_id)
            same = issue.official_id is None or issue.official_id == member.official_id
            # A new revision ID over unchanged evidence and an unchanged issue
            # field is not a correction, even if unrelated attributes changed.
            if old and row and source_ids[old.candidate_id] == source_ids[row.candidate_id]:
                same = same or any(bodies[old.id].get(f) == bodies[row.id].get(f) for f in fields)
            if same:
                reason = 'CURRENT_ISSUE'
        if row and any(bodies[row.id].get(f) is None for f in request.fields):
            reason = reason or 'FIELD_UNAVAILABLE'
        if reason:
            result['excluded'].append({'target_key': member.target_key, 'subject_id': str(member.subject_id), 'reason': reason})
        else:
            readable.append((member, row))
    for key in sorted(set(request.record_keys or []) - {m.target_key for m in members}):
        result['excluded'].append({'target_key': key, 'reason': 'RECORD_MISSING'})
    for subject in sorted(set(request.subjects) - {m.subject_id for m in members}, key=str):
        result['excluded'].append({'target_key': 'subject:' + str(subject), 'reason': 'SUBJECT_UNAVAILABLE'})
    unknown = request.require_complete and not request.record_keys
    time_ok = request.time_mode == 'observed'
    satisfied = bool(members) and not result['excluded'] and not unknown and time_ok
    result.update(request_satisfied=satisfied, state='available' if satisfied else
        'unavailable' if not time_ok else 'unknown' if unknown else 'partial' if readable else 'unavailable')
    result['requirements'] = [
        {'id': 'records', 'result': 'pass' if members and not result['excluded'] else 'fail', 'message': '按固定发布的完整请求范围检查正式字段与当前限制。'},
        {'id': 'completeness', 'result': 'unknown' if unknown else 'pass' if not result['excluded'] else 'fail',
         'message': '仅明确对象清单可证明请求完整；已发布对象不代表全来源覆盖。'},
        {'id': 'time', 'result': 'pass' if time_ok else 'fail', 'message': '仅支持观察时间；未验证历史公开时点。'}]
    result['scope_summary'] = {'manifest_objects': len(members), 'readable_objects': len(readable),
        'expected_objects': len(request.record_keys) if request.record_keys else None}
    if not check_only and time_ok and (satisfied or request.allow_partial):
        page = [(m, r) for m, r in readable if after is None or m.target_key > after[0]][:page_size + 1]
        subjects = {s.id: s for s in session.scalars(select(RecordSubject).where(RecordSubject.id.in_(request.subjects)))}
        result['items'] = [dict(target_key=m.target_key, subject_id=str(m.subject_id),
            source=subjects[m.subject_id].source, subject_kind=subjects[m.subject_id].kind,
            source_key=subjects[m.subject_id].source_key, business_date=m.business_date,
            official_id=str(r.id), decision_id=str(m.decision_id), **{f: bodies[r.id][f] for f in request.fields})
            for m, r in page[:page_size]]
        result['has_more'] = len(page) > page_size
        result['next_key'] = [page[page_size - 1][0].target_key] if result['has_more'] else None
    result['assessment_hash'] = digest('record-assessment-v1', dict(request=result['request'],
        release=release.id, epoch=guard.epoch, requirements=result['requirements'], scope=result['scope_summary']))
    if authenticate() != owner:
        raise FoundationError('AUTH_CONTEXT_CHANGED', '领域读取身份已失效。')
    return result


def lineage(session, revision_id):
    row = session.get(OfficialRecord, revision_id)
    if row is None:
        raise FoundationError('RELEASE_UNAVAILABLE', '正式领域修订不存在。')
    candidate = session.get(Candidate, row.candidate_id)
    decision = session.get(Decision, row.decision_id)
    source = session.get(SourceRef, candidate.source_ref_id)
    origin = session.get(Work, candidate.work_id)
    return dict(representation='official', official_id=row.id, candidate_id=candidate.id,
        decision_id=decision.id, work_id=decision.work_id, source_ref_id=source.id,
        subject_id=row.subject_id, target_key=row.business_key, dependency_id=candidate.dependency_id,
        assessment_id=candidate.assessment_id, contract_id=origin.contract_id, execution_id=origin.execution_id,
        source=source.source, source_hash=source.content_hash, source_observed_at=source.observed_at,
        policy=json.loads(decision.evidence_json))
