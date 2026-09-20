"""Authenticated fixed-release report reads, independent of source decoders."""
from datetime import date
from typing import Literal
from uuid import UUID
import json
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.catalog import now
from app.data_foundation.holdings import DATASET, SERIES
from app.data_foundation.holding_models import OfficialReport, OfficialReportMember, ReportBlockMember, ReportIssueTarget
from app.data_foundation.models import Definition, SourceRef
from app.data_foundation.work_models import Work, Release, Head, BlockRef, IssueScope, Issue, Decision, Candidate
from app.data_foundation.work import scope_key
from app.data_foundation.query import BusinessRange

FIELDS = ('hold_ratio', 'market_value', 'period_change_ratio', 'rank')
SORT = ['period_end', 'fund_share_id', 'report_type', 'period_start', 'scope_kind', 'series', 'target_key']


class ReportRequirement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: Literal['fund.holdings_report']
    contract_version: str = Field(default='1.0', pattern=r'^[1-9]\d*\.\d+$')
    profile_id: Literal['default'] = 'default'
    semantic_series_id: Literal['cn-fund-stock-holdings-provider-reported'] = SERIES
    subjects: list[UUID] = Field(min_length=1, max_length=100)
    business_range: BusinessRange
    report_types: list[Literal['quarter', 'annual', 'semiannual']] = Field(default_factory=lambda: ['quarter'], min_length=1)
    scope_kind: Literal['provider_reported', 'top_n', 'full_portfolio'] = 'provider_reported'
    fields: list[str] = Field(default_factory=lambda: ['hold_ratio'], min_length=1, max_length=20)
    report_keys: list[str] | None = Field(default=None, min_length=1, max_length=100)
    selected_report: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    release: UUID | Literal['latest'] = 'latest'
    require_complete: bool = False
    require_portfolio_complete: bool = False
    allow_partial: bool = False
    time_mode: Literal['observed', 'latest_known', 'strict_public_pit'] = 'observed'
    max_staleness_days: int | None = Field(default=None, ge=0)
    paging_version: Literal[1] = 1
    page_size: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode='after')
    def validate_request(self):
        if self.business_range.start > self.business_range.end or len(set(self.fields)) != len(self.fields):
            raise ValueError('报告期区间或字段无效')
        if self.report_keys and (len(set(self.report_keys)) != len(self.report_keys)
                or any(len(k) != 64 or any(c not in '0123456789abcdef' for c in k) for k in self.report_keys)):
            raise ValueError('报告对象键无效')
        if self.selected_report and self.report_keys and self.selected_report not in self.report_keys:
            raise ValueError('所选报告不属于原请求对象清单')
        self.subjects = sorted(set(self.subjects), key=str)
        self.report_types = sorted(set(self.report_types))
        self.fields = [f for f in FIELDS if f in self.fields] + sorted(set(self.fields) - set(FIELDS))
        if self.report_keys:
            self.report_keys = sorted(self.report_keys)
        return self


def resolve_release(session, request):
    from app.data_foundation.projection import resolve_projected_release
    return resolve_projected_release(session, request)


def _restricted(session, scope):
    rows = session.execute(select(Issue, ReportIssueTarget).join(ReportIssueTarget,
        ReportIssueTarget.issue_revision_id == Issue.id).where(Issue.scope_key == scope)
        .order_by(Issue.revision)).all()
    current = {i.issue_id: (i, t) for i, t in rows}
    permanent = [(i, t) for i, t in rows if i.state == 'confirmed' and t.official_id and i.severity != 'warning']
    return [(i, t) for i, t in current.values() if i.state in ('suspected', 'confirmed') and i.severity != 'warning'] + permanent


def evaluate(session, request, authenticate, *, expected_epoch=None, after=None, page_size=100, check_only=False):
    owner = authenticate()
    if not owner:
        raise FoundationError('AUTH_REQUIRED', '报告读取需要有效身份。')
    from app.data_foundation.projection import projection_for
    projection = projection_for(session, DATASET, request.contract_version)
    if not set(request.fields) <= set(projection['fields']):
        raise FoundationError('INVALID_REQUIREMENT', '报告请求包含所选契约未提供的字段。')
    # Explicitly declared unavailable fields support additive readers over old
    # immutable releases. They never manufacture values from another revision.
    unavailable_fields = set(projection.get('unavailable_fields', []))
    if not set(request.fields) <= set(FIELDS) | unavailable_fields:
        raise FoundationError('INVALID_REQUIREMENT', '报告契约字段尚无可执行的读取投影。')
    release = resolve_release(session, request)
    base = {'dataset': DATASET, 'representation': 'official', 'release_id': str(release.id) if release else None,
        'manifest_hash': release.manifest_hash if release else None, 'checked_at': now(),
        'request': request.model_dump(mode='json', by_alias=True), 'items': [], 'excluded': [],
        'has_more': False, 'next_key': None, 'issue_state_version': 0,
        'request_satisfied': False, 'requirements': [], 'scope_summary': {}, 'state': 'unavailable'}
    if release is None:
        base['requirements'] = [{'id': 'release', 'result': 'fail', 'reason_code': 'RELEASE_UNAVAILABLE', 'message': '尚无正式报告发布。'}]
        return base
    guard = session.scalar(select(IssueScope).where(IssueScope.scope_key == release.scope_key)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if guard is None:
        raise FoundationError('DEPENDENCY_MISSING', '报告读取缺少当前问题上下文。')
    if expected_epoch is not None and guard.epoch != expected_epoch:
        raise FoundationError('READ_CONTEXT_CHANGED', '报告当前限制已改变，请重新检查原版本。')
    base['issue_state_version'] = guard.epoch
    statement = select(ReportBlockMember).join(BlockRef, BlockRef.block_id == ReportBlockMember.block_id).where(
        BlockRef.release_id == release.id, ReportBlockMember.fund_share_id.in_(request.subjects),
        ReportBlockMember.period_end.between(request.business_range.start, request.business_range.end),
        ReportBlockMember.report_type.in_(request.report_types), ReportBlockMember.scope_kind == request.scope_kind)
    if request.report_keys:
        statement = statement.where(ReportBlockMember.target_key.in_(request.report_keys))
    if request.selected_report:
        statement = statement.where(ReportBlockMember.target_key == request.selected_report)
    objects = session.scalars(statement.order_by(*[getattr(ReportBlockMember, k) for k in SORT]).limit(101)).all()
    if len(objects) > 100:
        raise FoundationError('INVALID_REQUIREMENT', '一次报告请求最多100个对象，请缩小报告范围。')
    restrictions = _restricted(session, release.scope_key)
    readable, totals = [], 0
    for obj in objects:
        reason = None if obj.state == 'value' else obj.state
        if any(t.target_key == obj.target_key and (t.official_id is None or t.official_id == obj.official_id)
               and set(request.fields).intersection(json.loads(i.fields_json)) for i, t in restrictions):
            reason = 'current_issue'
        head = session.get(OfficialReport, obj.official_id) if obj.official_id else None
        if head:
            totals += head.member_count
            if request.require_portfolio_complete and head.portfolio_complete is not True:
                reason = reason or 'PORTFOLIO_COMPLETENESS_UNKNOWN'
            # Check only null flags, not sensitive values, until the report passes
            # current restrictions. The official reader never consults candidates.
            columns = [getattr(OfficialReportMember, f).is_(None) for f in request.fields if f in FIELDS]
            flags = session.execute(select(*columns).where(OfficialReportMember.official_id == head.id)).all() if columns else []
            if (any(any(row) for row in flags) or 'market_value' in request.fields
                    or unavailable_fields.intersection(request.fields)):
                reason = reason or 'FIELD_UNAVAILABLE'
        if reason:
            base['excluded'].append({'target_key': obj.target_key, 'fund_share_id': str(obj.fund_share_id),
                                      'period_end': str(obj.period_end), 'reason': reason})
        else:
            readable.append((obj, head))
    keys = {o.target_key for o in objects}
    missing = set(request.report_keys or []) - keys
    if request.selected_report and request.selected_report not in keys:
        missing.add(request.selected_report)
    for key in sorted(missing):
        base['excluded'].append({'target_key': key, 'reason': 'REPORT_MISSING'})
    absent_subjects = set()
    if not request.selected_report:
        absent_subjects = set(request.subjects) - {o.fund_share_id for o in objects}
        for subject in sorted(absent_subjects, key=str):
            base['excluded'].append({'target_key': f'subject:{subject}', 'fund_share_id': str(subject), 'reason': 'SUBJECT_UNAVAILABLE'})
    business_as_of = max((o.period_end for o in objects if o.state == 'value'), default=None)
    fresh = True
    if request.max_staleness_days is not None:
        # Every requested subject must meet freshness independently.
        for subject in request.subjects:
            latest = max((o.period_end for o in objects if o.fund_share_id == subject and o.state == 'value'), default=None)
            if latest is None or (request.business_range.end - latest).days > request.max_staleness_days:
                fresh = False
    unknown = request.require_complete and not request.report_keys and not request.selected_report
    time_ok = request.time_mode == 'observed'
    satisfied = bool(objects) and not base['excluded'] and not unknown and time_ok and fresh
    base['state'] = ('available' if satisfied else 'unavailable' if not time_ok or not fresh
        else 'unknown' if unknown else 'partial' if readable and base['excluded'] else 'unavailable')
    base['request_satisfied'] = satisfied
    base['scope_summary'] = {'expected_objects': 1 if request.selected_report else len(request.report_keys) if request.report_keys else None,
        'official_objects': sum(o.state == 'value' for o in objects), 'manifest_objects': len(objects),
        'readable_objects': len(readable), 'member_count': totals,
        'business_as_of': str(business_as_of) if business_as_of else None}
    base['requirements'] = [
        {'id': 'reports', 'result': 'pass' if objects and not base['excluded'] else 'fail', 'message': '按完整报告对象核验正式值与当前限制。'},
        {'id': 'completeness', 'result': 'unknown' if unknown else 'fail' if request.require_complete and (missing or absent_subjects or any(o.state != 'value' for o in objects)) else 'pass',
         'message': '本次未要求完整报告范围；已收到报告不代表全投资组合。' if not request.require_complete else '范围完整性按明确报告清单核验；已收到报告不代表全投资组合。'},
        {'id': 'time', 'result': 'pass' if time_ok else 'fail', 'message': '仅支持观察时间；未验证历史公开时点。'},
        {'id': 'freshness', 'result': 'pass' if fresh else 'fail', 'message': '按每只基金实际报告截至日期相对查询结束日判断新鲜度。'}]
    if not check_only and time_ok and fresh and (satisfied or request.allow_partial):
        if request.selected_report and readable:
            obj, head = readable[0]
            statement = select(OfficialReportMember).where(OfficialReportMember.official_id == head.id)
            if after is not None:
                statement = statement.where(OfficialReportMember.member_ordinal > after[0])
            rows = session.scalars(statement.order_by(OfficialReportMember.member_ordinal).limit(page_size + 1)).all()
            base['items'] = [{'member_ordinal': m.member_ordinal, 'member_instrument_id': str(m.member_instrument_id),
                'source_member_code': m.source_member_code, 'official_id': str(head.id), 'decision_id': str(obj.decision_id),
                'target_key': obj.target_key, **{f: getattr(m, f) for f in request.fields}} for m in rows[:page_size]]
            base['has_more'] = len(rows) > page_size
            base['next_key'] = [rows[page_size - 1].member_ordinal] if base['has_more'] else None
        elif not request.selected_report:
            def sort_key(pair):
                return [str(getattr(pair[0], k)) for k in SORT]
            rows = [p for p in readable if after is None or sort_key(p) > after]
            base['items'] = [{**{k: getattr(o, k) for k in SORT}, 'official_id': str(h.id),
                'decision_id': str(o.decision_id), 'member_count': h.member_count,
                'transport_complete': h.transport_complete, 'portfolio_complete': h.portfolio_complete,
                'public_at': h.public_at} for o, h in rows[:page_size]]
            base['has_more'] = len(rows) > page_size
            base['next_key'] = sort_key(rows[page_size - 1]) if base['has_more'] else None
    base['assessment_hash'] = digest('report-assessment', {'request': base['request'], 'release': release.id,
        'epoch': guard.epoch, 'requirements': base['requirements'], 'scope': base['scope_summary']})
    if authenticate() != owner:
        raise FoundationError('AUTH_CONTEXT_CHANGED', '报告读取身份已失效。')
    return base


def lineage(session, revision_id):
    head = session.get(OfficialReport, revision_id)
    if head is None:
        raise FoundationError('RELEASE_UNAVAILABLE', '正式报告修订不存在。')
    decision = session.get(Decision, head.decision_id)
    candidate = session.get(Candidate, head.candidate_id)
    source = session.get(SourceRef, candidate.source_ref_id)
    origin = session.get(Work, candidate.work_id)
    return {'representation': 'official', 'official_id': head.id, 'candidate_id': candidate.id,
        'decision_id': decision.id, 'work_id': decision.work_id, 'source_ref_id': source.id,
        'binding_id': candidate.binding_id, 'dependency_id': candidate.dependency_id,
        'assessment_id': candidate.assessment_id, 'contract_id': origin.contract_id,
        'execution_id': origin.execution_id,
        'source': source.source, 'source_hash': source.content_hash, 'source_observed_at': source.observed_at,
        'source_report_key': candidate.unit_key, 'policy': json.loads(decision.evidence_json)}
