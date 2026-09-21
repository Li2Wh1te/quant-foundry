"""Whole-object release comparison; no member values escape the read guard."""
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError
from app.data_foundation.catalog import now
from app.data_foundation.holding_query import evaluate, resolve_release, SORT
from app.data_foundation.holding_models import ReportBlockMember, OfficialReport
from app.data_foundation.work_models import BlockRef, IssueScope, Work


def release_changes(session, requirement, previous_id, current_id, authenticate, *, after=None, limit=50):
    owner = authenticate()
    if not owner:
        raise FoundationError('AUTH_REQUIRED', '报告版本比较需要有效身份。')
    previous = resolve_release(session, requirement.model_copy(update={'release': previous_id}))
    current = resolve_release(session, requirement.model_copy(update={'release': current_id}))
    if previous.scope_key != current.scope_key:
        raise FoundationError('CONTRACT_RELEASE_MISMATCH', '只能比较相同契约和语义的报告版本。')
    guard = session.scalar(select(IssueScope).where(IssueScope.scope_key == current.scope_key)
        .with_for_update(read=True).execution_options(populate_existing=True))
    def objects(release):
        request = requirement.model_copy(update={'release': release.id})
        # This enforces the same 100-object bound, current issues, field
        # availability and time semantics as normal reads, before comparison.
        checked = evaluate(session, request, authenticate, expected_epoch=guard.epoch, check_only=True)
        unavailable = {i['target_key'] for i in checked['excluded']}
        global_failure = any(r['id'] in ('time', 'freshness') and r['result'] != 'pass'
                             for r in checked['requirements'])
        stmt = select(ReportBlockMember).join(BlockRef, BlockRef.block_id == ReportBlockMember.block_id).where(
            BlockRef.release_id == release.id, ReportBlockMember.fund_share_id.in_(request.subjects),
            ReportBlockMember.period_end.between(request.business_range.start, request.business_range.end),
            ReportBlockMember.report_type.in_(request.report_types), ReportBlockMember.scope_kind == request.scope_kind)
        if request.report_keys:
            stmt = stmt.where(ReportBlockMember.target_key.in_(request.report_keys))
        if request.selected_report:
            stmt = stmt.where(ReportBlockMember.target_key == request.selected_report)
        return {m.target_key: m for m in session.scalars(stmt)}, unavailable, global_failure
    old, old_restricted, old_failure = objects(previous)
    new, new_restricted, new_failure = objects(current)
    def key(target):
        row = new.get(target) or old[target]
        return [str(getattr(row, k)) for k in SORT]
    keys = sorted(set(old) | set(new), key=key)
    if after:
        keys = [k for k in keys if key(k) > after]
    from app.data_foundation.views import member_basis
    items = []
    for target in keys[:limit]:
        a, b = old.get(target), new.get(target)
        restricted = ((a is not None and a.state == 'value' and (target in old_restricted or old_failure))
            or (b is not None and b.state == 'value' and (target in new_restricted or new_failure)))
        if b is None:
            kind = 'absent'
        elif b.state != 'value':
            kind = b.state
        elif a is None or a.state != 'value':
            kind = 'restricted_comparison' if restricted else 'added'
        elif restricted:
            kind = 'restricted_comparison'
        elif a.official_id == b.official_id and a.decision_id == b.decision_id:
            kind = 'inherited'
        else:
            before, after_head = session.get(OfficialReport, a.official_id), session.get(OfficialReport, b.official_id)
            kind = 'basis_changed' if before.values_hash == after_head.values_hash else 'whole_object_changed'
        row = b or a
        items.append({'target_key': target, 'fund_share_id': str(row.fund_share_id),
            'period_start': str(row.period_start), 'period_end': str(row.period_end), 'report_type': row.report_type,
            'kind': kind, 'comparison_restricted': restricted,
            'before_official': str(a.official_id) if a and a.official_id else None,
            'after_official': str(b.official_id) if b and b.official_id else None,
            'before_decision': str(a.decision_id) if a else None, 'after_decision': str(b.decision_id) if b else None,
            'basis': {'before':member_basis(session,a),'after':member_basis(session,b)}})
    if authenticate() != owner:
        raise FoundationError('AUTH_CONTEXT_CHANGED', '报告版本比较身份已失效。')
    from app.data_foundation.views import process_detail
    return {'representation': 'official', 'previous_release': str(previous.id), 'release_id': str(current.id),
        'issue_state_version': guard.epoch, 'checked_at': now(), 'items': items, 'has_more': len(keys) > limit,
        'next_key': key(keys[limit - 1]) if len(keys) > limit else None,
        'rules': {'before': process_detail(session, session.get(Work, previous.work_id))['rules'],
                  'after': process_detail(session, session.get(Work, current.work_id))['rules']}}
