"""Append-only current restrictions and a serialized, authenticated read guard."""
import json
from uuid import uuid4
from sqlalchemy import select
from app.data_foundation.catalog import now
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.bars import values
from app.data_foundation.work_models import Issue, IssueScope, Release, BlockRef, BlockMember, OfficialBar


def record_issue(session, *, scope_key, instrument_id, start, end, fields, state, reason, evidence,
                 issue_id=None, official_id=None, severity='error'):
    guard = session.scalar(select(IssueScope).where(IssueScope.scope_key == scope_key).with_for_update().execution_options(populate_existing=True))
    if guard is None:
        raise FoundationError('DEPENDENCY_MISSING', '问题作用范围尚未登记。')
    if severity not in ('warning','error','critical'):
        raise ValueError('Invalid issue severity')
    if not fields or not set(fields) <= {'open','high','low','close','volume','turnover'} or not reason or not evidence or end < start:
        raise ValueError('Invalid issue evidence/scope')
    previous = session.scalar(select(Issue).where(Issue.issue_id == issue_id).order_by(Issue.revision.desc()).limit(1)) if issue_id else None
    if issue_id and previous is None:
        raise FoundationError('ISSUE_UNAVAILABLE', '问题历史不存在。')
    allowed = {'suspected': {'confirmed','dismissed'}, 'confirmed': {'resolved'}, 'resolved': set(), 'dismissed': set()}
    if previous:
        if state not in allowed[previous.state] or (scope_key, instrument_id, start, end, official_id, sorted(fields)) != (
                previous.scope_key, previous.instrument_id, previous.start, previous.end, previous.official_id, sorted(json.loads(previous.fields_json))):
            raise FoundationError('ISSUE_TRANSITION_INVALID', '问题修订不能更换作用范围或绕过状态转换。')
        if severity != previous.severity:
            raise FoundationError('ISSUE_TRANSITION_INVALID', '问题严重度不能在修订时静默改变。')
    elif state not in ('suspected', 'confirmed'):
        raise ValueError('Initial issue must be suspected or confirmed')
    if official_id:
        official = session.get(OfficialBar, official_id)
        if official is None or official.instrument_id != instrument_id or not start <= official.trade_date <= end:
            raise FoundationError('ISSUE_SCOPE_INVALID', '问题指定正式修订与作用范围不符。')
    row = Issue(issue_id=issue_id or uuid4(), revision=previous.revision+1 if previous else 1, scope_key=scope_key,
        instrument_id=instrument_id, start=start, end=end, official_id=official_id, state=state,
        fields_json=encode(sorted(fields)), severity=severity, reason=reason, evidence_json=encode(evidence), created_at=now())
    guard.epoch += 1
    session.add(row); session.flush()
    return row


def read_release(session, *, release_id, expected_issue_epoch, authenticate, instrument_ids=None,
                 start=None, end=None, fields=('open','high','low','close'), allow_partial=False, keys_only=False,
                 selected_keys=None):
    """Return serialized bytes while still holding the shared issue lock.

    authenticate is a mandatory current-context check, not a cached success.
    The caller sends these bytes after commit; restrictions committed after
    read_guard_at apply to the next request, not already serialized responses.
    """
    owner = authenticate()
    if not owner:
        raise FoundationError('AUTH_REQUIRED', '读取正式值需要有效认证上下文。')
    if not set(fields) <= {'open','high','low','close','volume','turnover'} or not fields:
        raise ValueError('Invalid field projection')
    release = session.get(Release, release_id)
    if release is None or release.status != 'published':
        raise FoundationError('RELEASE_UNAVAILABLE', '指定正式发布不可读取。')
    guard = session.scalar(select(IssueScope).where(IssueScope.scope_key == release.scope_key).with_for_update(read=True)
        .execution_options(populate_existing=True))
    if guard is None:
        raise FoundationError('DEPENDENCY_MISSING', '正式读取缺少问题限制上下文。')
    if guard.epoch != expected_issue_epoch:
        raise FoundationError('READ_CONTEXT_CHANGED', '当前数据问题限制已改变，请重新检查读取条件。')
    statement = select(BlockMember, *[getattr(OfficialBar, f).is_(None).label(f) for f in fields]).outerjoin(
        OfficialBar, OfficialBar.id == BlockMember.official_id).join(BlockRef, BlockRef.block_id == BlockMember.block_id).where(BlockRef.release_id == release.id)
    if instrument_ids is not None: statement = statement.where(BlockMember.instrument_id.in_(instrument_ids))
    if start: statement = statement.where(BlockMember.trade_date >= start)
    if end: statement = statement.where(BlockMember.trade_date <= end)
    rows = session.execute(statement.order_by(BlockMember.instrument_id, BlockMember.trade_date)).all()
    members = [row[0] for row in rows]
    missing_fields = {(row[0].instrument_id,row[0].trade_date): any(row[1:]) for row in rows}
    # Evaluate quality using null flags only. Fetch Decimal values in one query
    # only for the selected page, never once per member or during a check.
    wanted = {m.official_id for m in members if m.official_id and
        (selected_keys is None or (str(m.instrument_id), str(m.trade_date)) in selected_keys)}
    values_by_id = {} if keys_only or not wanted else {o.id:o for o in session.scalars(
        select(OfficialBar).where(OfficialBar.id.in_(wanted)))}
    history = session.scalars(select(Issue).where(Issue.scope_key == release.scope_key).order_by(Issue.revision)).all()
    current = {item.issue_id: item for item in history}
    # A confirmed bad historical revision stays bad after a replacement fixes
    # the business range. Resolution cannot rehabilitate that exact old value.
    permanent = [item for item in history if item.state == 'confirmed' and item.official_id and item.severity != 'warning']
    restrictions = [i for i in current.values() if i.state in ('suspected','confirmed') and i.severity != 'warning'] + permanent
    items, gaps = [], []
    for member in members:
        blocked = any(issue.instrument_id == member.instrument_id and issue.start <= member.trade_date <= issue.end
            and (issue.official_id is None or issue.official_id == member.official_id)
            and set(fields).intersection(json.loads(issue.fields_json)) for issue in restrictions)
        if member.state != 'value' or blocked:
            gaps.append({'instrument_id': member.instrument_id, 'trade_date': member.trade_date,
                         'reason': 'current_issue' if blocked else member.state})
        else:
            if missing_fields[(member.instrument_id,member.trade_date)]:
                gaps.append({'instrument_id': member.instrument_id, 'trade_date': member.trade_date, 'reason': 'field_unavailable'})
                continue
            if selected_keys is not None and (str(member.instrument_id), str(member.trade_date)) not in selected_keys:
                continue
            if keys_only:
                items.append({'instrument_id':member.instrument_id,'trade_date':member.trade_date})
                continue
            official = values_by_id[member.official_id]
            items.append({'instrument_id': official.instrument_id, 'trade_date': official.trade_date,
                'series': official.series, 'official_id': official.id, 'decision_id': member.decision_id, 'value_decision_id': official.decision_id,
                **{f: getattr(official, f) for f in fields}})
    if authenticate() != owner:
        raise FoundationError('AUTH_CONTEXT_CHANGED', '当前认证上下文已改变。')
    status = 'unknown' if not members else ('partial' if items and gaps else 'unavailable' if gaps else 'available')
    if gaps and not allow_partial: items = []
    # Coverage outside represented manifest keys belongs to M3's calendar-aware
    # query; this internal mechanism makes no full-range completeness claim.
    return encode({'status': status, 'representation': 'official', 'release_id': release.id,
        'issue_epoch': guard.epoch, 'read_guard_at': now(), 'coverage': 'manifest_keys_only',
        'items': items, 'gaps': gaps}).encode()


def assessment(session, *, input_hash, rule_hash, scope_hash, status, results):
    from app.data_foundation.work_models import Assessment
    from app.data_foundation.catalog import lock_key
    lock_key(session, 'assessment', [input_hash, rule_hash, scope_hash])
    old = session.scalar(select(Assessment).where(Assessment.input_hash == input_hash, Assessment.rule_hash == rule_hash, Assessment.scope_hash == scope_hash))
    if old:
        if old.status != status or old.results_json != encode(results):
            raise FoundationError('ASSESSMENT_CONFLICT', '固定输入与规则的评估结果不能变更。')
        return old
    row = Assessment(input_hash=input_hash, rule_hash=rule_hash, scope_hash=scope_hash, status=status, results_json=encode(results), created_at=now())
    session.add(row); session.flush()
    return row
