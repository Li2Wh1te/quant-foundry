"""Append-only typed-record restrictions fenced by the shared issue epoch."""
import json
from uuid import UUID, uuid4
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.catalog import now
from app.data_foundation.record_models import RecordIssue, RecordBlockMember, OfficialRecord
from app.data_foundation.record_schemas import schema_for
from app.data_foundation.work_models import IssueScope, Work, Decision, BlockRef, Release


def record_issue(session, *, scope_key, target_key, fields, state, reason, evidence, official_id=None, issue_id=None):
    guard = session.scalar(select(IssueScope).where(IssueScope.scope_key == scope_key)
        .with_for_update().execution_options(populate_existing=True))
    if guard is None or not fields or not reason or not isinstance(evidence, dict) or not evidence:
        raise FoundationError('ISSUE_SCOPE_INVALID', '领域问题缺少有效作用范围或证据。')
    previous = session.scalar(select(RecordIssue).where(RecordIssue.issue_id == issue_id)
        .order_by(RecordIssue.revision.desc()).limit(1)) if issue_id else None
    if issue_id and previous is None:
        raise FoundationError('ISSUE_UNAVAILABLE', '领域问题历史不存在。')
    if previous:
        allowed = {'suspected': {'confirmed', 'dismissed'}, 'confirmed': {'resolved'}, 'resolved': set(), 'dismissed': set()}
        if state not in allowed[previous.state] or (
            previous.scope_key, previous.target_key, previous.official_id, json.loads(previous.fields_json)) != (
                scope_key, target_key, official_id, sorted(set(fields))):
            raise FoundationError('ISSUE_TRANSITION_INVALID', '问题修订不能改变原作用范围。')
    elif state not in ('suspected', 'confirmed'):
        raise FoundationError('ISSUE_TRANSITION_INVALID', '领域问题初始状态无效。')
    if official_id:
        row = session.get(OfficialRecord, official_id)
        work = session.get(Work, session.get(Decision, row.decision_id).work_id) if row else None
        if not work or work.scope_key != scope_key or row.business_key != target_key:
            raise FoundationError('ISSUE_SCOPE_INVALID', '正式修订不属于问题作用范围。')
    else:
        work = session.scalar(select(Work).join(Release, Release.work_id == Work.id)
            .join(BlockRef, BlockRef.release_id == Release.id)
            .join(RecordBlockMember, RecordBlockMember.block_id == BlockRef.block_id)
            .where(Release.scope_key == scope_key, Release.status == 'published',
                RecordBlockMember.target_key == target_key).limit(1))
        if work is None:
            raise FoundationError('ISSUE_SCOPE_INVALID', '问题对象尚无已发布证据。')
    dataset = json.loads(work.parameters_json)['dataset']
    if not set(fields) <= set(schema_for(dataset).body.model_fields):
        raise FoundationError('ISSUE_SCOPE_INVALID', '问题字段不属于正式领域契约。')
    if previous and state == 'resolved' and official_id is not None:
        old = session.get(OfficialRecord, official_id)
        old_body = json.loads(old.body_json)
        nested = [field for field in fields if isinstance(old_body.get(field), (list, dict))]
        if nested:
            # A changed collection alone cannot prove that its affected member
            # was corrected. Require a published replacement and a review note.
            try:
                replacement_id = UUID(str(evidence['replacement_official_id']))
            except (KeyError, TypeError, ValueError) as exc:
                raise FoundationError('ISSUE_RESOLUTION_UNPROVEN', '嵌套字段问题须提供正式替代修订凭据。') from exc
            replacement = session.get(OfficialRecord, replacement_id)
            correction = evidence.get('correction')
            replacement_work = session.get(Work, session.get(Decision, replacement.decision_id).work_id) if replacement else None
            published = session.scalar(select(Release.id).join(BlockRef, BlockRef.release_id == Release.id)
                .join(RecordBlockMember, RecordBlockMember.block_id == BlockRef.block_id)
                .where(Release.scope_key == scope_key, Release.status == 'published',
                    RecordBlockMember.target_key == target_key,
                    RecordBlockMember.official_id == replacement_id).limit(1)) if replacement_work else None
            if (replacement is None or replacement.id == official_id or replacement.business_key != target_key
                    or replacement_work.scope_key != scope_key or published is None
                    or not isinstance(correction, str) or not correction.strip()
                    or any(old_body.get(field) == json.loads(replacement.body_json).get(field) for field in nested)):
                raise FoundationError('ISSUE_RESOLUTION_UNPROVEN', '嵌套字段问题缺少已发布且内容已变化的替代修订及核对说明。')
    row = RecordIssue(issue_id=issue_id or uuid4(), revision=previous.revision + 1 if previous else 1,
        scope_key=scope_key, target_key=target_key, official_id=official_id, state=state,
        fields_json=encode(sorted(set(fields))), reason=reason, evidence_json=encode(evidence), created_at=now())
    session.add(row)
    guard.epoch += 1
    session.flush()
    return row
