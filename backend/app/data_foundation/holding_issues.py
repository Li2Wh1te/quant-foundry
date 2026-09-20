"""Report-specific issue targets reuse common issue epochs and read locks."""
import json
from uuid import uuid4
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.catalog import now
from app.data_foundation.holding_query import FIELDS
from app.data_foundation.holding_models import OfficialReport, ReportIssueTarget, ReportBlockMember
from app.data_foundation.work_models import Issue, IssueScope, Work, Decision, BlockRef, Release
from app.data_foundation.holdings import target_key


def record_report_issue(session, *, scope_key, target, fund_share_id, period_end, fields,
                        state, reason, evidence, official_id=None, issue_id=None):
    guard = session.scalar(select(IssueScope).where(IssueScope.scope_key == scope_key)
        .with_for_update().execution_options(populate_existing=True))
    if guard is None or not fields or not set(fields) <= set(FIELDS) or not reason or not evidence:
        raise FoundationError('ISSUE_SCOPE_INVALID', '报告问题缺少有效范围或证据。')
    if len(target) != 64 or any(c not in '0123456789abcdef' for c in target):
        raise FoundationError('ISSUE_SCOPE_INVALID', '报告问题对象键无效。')
    previous = session.scalar(select(Issue).where(Issue.issue_id == issue_id)
                              .order_by(Issue.revision.desc()).limit(1)) if issue_id else None
    if issue_id and previous is None:
        raise FoundationError('ISSUE_UNAVAILABLE', '报告问题历史不存在。')
    if previous:
        p = session.get(ReportIssueTarget, previous.id)
        allowed = {'suspected': {'confirmed', 'dismissed'}, 'confirmed': {'resolved'}, 'resolved': set(), 'dismissed': set()}
        if (p is None or state not in allowed[previous.state]
                or (p.target_key, p.official_id, previous.instrument_id, previous.scope_key, previous.start, json.loads(previous.fields_json))
                != (target, official_id, fund_share_id, scope_key, period_end, sorted(set(fields)))):
            raise FoundationError('ISSUE_TRANSITION_INVALID', '报告问题修订不能改变原作用范围。')
    elif state not in ('suspected', 'confirmed'):
        raise FoundationError('ISSUE_TRANSITION_INVALID', '报告问题初始状态无效。')
    if official_id:
        report = session.get(OfficialReport, official_id)
        if report is None:
            raise FoundationError('ISSUE_SCOPE_INVALID', '报告修订不存在。')
        work = session.get(Work, session.get(Decision, report.decision_id).work_id)
        key = target_key(report.fund_share_id, report.period_start, report.period_end,
                         report.report_type, report.scope_kind, report.series)
        if (work.scope_key, key, report.fund_share_id, report.period_end) != (scope_key, target, fund_share_id, period_end):
            raise FoundationError('ISSUE_SCOPE_INVALID', '报告修订不属于问题作用范围。')
    else:
        # An object-wide issue must still point at a proven business key. An
        # arbitrary hash could otherwise silently fail to restrict its target.
        found = session.scalar(select(ReportBlockMember.target_key)
            .join(BlockRef, BlockRef.block_id == ReportBlockMember.block_id)
            .join(Release, Release.id == BlockRef.release_id)
            .where(Release.scope_key == scope_key, Release.status == 'published',
                ReportBlockMember.target_key == target, ReportBlockMember.fund_share_id == fund_share_id,
                ReportBlockMember.period_end == period_end).limit(1))
        if found is None:
            raise FoundationError('ISSUE_SCOPE_INVALID', '报告问题未定位到已发布的业务对象。')
    row = Issue(issue_id=issue_id or uuid4(), revision=previous.revision + 1 if previous else 1,
        scope_key=scope_key, instrument_id=fund_share_id, start=period_end, end=period_end,
        official_id=None, state=state, fields_json=encode(sorted(set(fields))), severity='error',
        reason=reason, evidence_json=encode(evidence), created_at=now())
    session.add(row)
    session.flush()
    session.add(ReportIssueTarget(issue_revision_id=row.id, target_key=target, official_id=official_id))
    guard.epoch += 1
    session.flush()
    return row
