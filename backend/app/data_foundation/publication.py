"""Typed decisions, flat manifests, and fenced atomic compare-and-swap publication."""
from datetime import date
import json
import time
from uuid import UUID
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.bars import values, validate_bar
from app.data_foundation.work import fenced, finish_batch, append_event, BATCH_ROWS, BUDGET_SECONDS
from app.data_foundation.work_models import (Work, Candidate, CandidateBar, CandidateEntry, CandidateManifest,
    Decision, OfficialBar, Release, ReleaseBlock, BlockMember, BlockRef, Head, IssueScope, Unit)


def target_key(instrument_id, trade_date):
    return f'{instrument_id}/{trade_date}'


def partition(instrument_id, trade_date):
    return f'{instrument_id}/{str(trade_date)[:7]}'


def stage_decisions(session, work_id, epoch):
    work = fenced(session, work_id, epoch)
    if work.kind != 'B':
        raise ValueError('Governance work required')
    params = json.loads(work.parameters_json)
    actions = params['actions']
    from app.data_foundation.models import Definition, SourceRef
    policy = session.get(Definition, work.policy_id)
    policy_value = json.loads(policy.definition_json)
    admitted = set(session.scalars(select(CandidateEntry.candidate_id).where(CandidateEntry.manifest_id == work.candidate_manifest_id)))
    end = min(work.cursor + BATCH_ROWS, len(actions))
    started = time.monotonic()
    for index, action in enumerate(actions[work.cursor:end], work.cursor):
        if set(action) != {'target_key', 'instrument_id', 'trade_date', 'action', 'candidate_id', 'parent_official_id', 'reason'}:
            raise ValueError('Invalid fixed decision shape')
        iid, business_date = UUID(action['instrument_id']), date.fromisoformat(action['trade_date'])
        if target_key(iid, business_date) != action['target_key'] or not params['start'] <= str(business_date) <= params['end'] or not action['reason']:
            raise FoundationError('SCOPE_MISMATCH', '治理目标超出固定范围或缺少决策依据。')
        choice = action['action']
        candidate_id = UUID(action['candidate_id']) if action['candidate_id'] else None
        parent_id = UUID(action['parent_official_id']) if action['parent_official_id'] else None
        selected = None
        if choice == 'select':
            if candidate_id not in admitted or parent_id:
                raise FoundationError('CANDIDATE_NOT_ADMITTED', '选中候选不属于固定输入清单。')
            candidate = session.get(Candidate, candidate_id)
            selected = session.get(CandidateBar, candidate_id)
            candidate_source = session.get(SourceRef, candidate.source_ref_id)
            if candidate_source.source not in policy_value['source_order']:
                raise FoundationError('CANDIDATE_NOT_ADMITTED', '候选来源未被固定治理政策准入。')
            if candidate.readiness != 'ready' or selected is None or (selected.instrument_id, selected.trade_date, selected.series) != (iid, business_date, params['series']):
                raise FoundationError('CANDIDATE_NOT_READY', '候选未就绪或目标语义不一致。')
        elif choice == 'retain':
            member = parent_member(session, work.parent_release_id, iid, business_date)
            if candidate_id or not member or member.state != 'value' or member.official_id != parent_id:
                raise FoundationError('RETAIN_INVALID', '保留值不属于固定父发布。')
        elif choice not in ('gap', 'block', 'withdraw') or candidate_id or parent_id:
            raise ValueError('Invalid governance action')
        from app.data_foundation.quality import assessment
        checked = assessment(session, input_hash=digest('decision-input', [work.fingerprint, action]), rule_hash=policy.content_hash, scope_hash=work.scope_key, status='pass', results={'action': choice, 'reason': action['reason']})
        decision = Decision(assessment_id=checked.id, work_id=work.id, target_key=action['target_key'], candidate_manifest_id=work.candidate_manifest_id,
            selected_candidate_id=candidate_id, parent_official_id=parent_id, action=choice,
            evidence_json=encode({'reason': action['reason'], 'input_manifest_id': work.candidate_manifest_id,
                'selected': candidate_id, 'excluded_candidates': [str(c) for c in sorted(admitted, key=str) if c != candidate_id],
                'comparison': 'disabled', 'policy_id': work.policy_id}), created_at=now())
        session.add(decision); session.flush()
        if selected:
            typed = validate_bar(values(selected))
            session.add(OfficialBar(decision_id=decision.id, candidate_id=candidate_id, values_hash=digest('bar-values', typed),
                created_at=now(), **typed))
        session.add(Unit(work_id=work.id, unit_key=action['target_key'], row_count=1, result_hash=digest('decision-action', action)))
        if time.monotonic() - started >= BUDGET_SECONDS:
            end = index + 1
            break
    work.cursor = end
    work.total = len(actions)
    session.flush()
    fenced(session, work.id, epoch)
    if end < len(actions):
        finish_batch(session, work, status='queued')
        return None
    return seal_release(session, work)


def parent_member(session, release_id, instrument_id, trade_date):
    if release_id is None:
        return None
    return session.scalar(select(BlockMember).join(BlockRef, BlockRef.block_id == BlockMember.block_id).where(
        BlockRef.release_id == release_id, BlockMember.instrument_id == instrument_id, BlockMember.trade_date == trade_date))


def member_value(member):
    return {'instrument_id': member.instrument_id, 'trade_date': member.trade_date, 'state': member.state,
            'official_id': member.official_id, 'decision_id': member.decision_id}


def seal_release(session, work):
    """Build a complete flat block map; omissions never reveal parent values."""
    existing = session.scalar(select(Release).where(Release.work_id == work.id))
    if existing:
        return existing
    params = json.loads(work.parameters_json)
    decisions = {d.target_key: d for d in session.scalars(select(Decision).where(Decision.work_id == work.id))}
    if work.cursor != work.total or len(decisions) != len(params['actions']):
        raise FoundationError('PUBLICATION_INCOMPLETE', '治理单元尚未全部提交，不能封存发布。')
    blocks = {r.partition_key: r.block_id for r in session.scalars(select(BlockRef).where(BlockRef.release_id == work.parent_release_id))} if work.parent_release_id else {}
    changed = {}
    for action in params['actions']:
        iid, business_date = UUID(action['instrument_id']), date.fromisoformat(action['trade_date'])
        pk = partition(iid, business_date)
        if pk not in changed:
            changed[pk] = {target_key(m.instrument_id, m.trade_date): member_value(m) for m in session.scalars(select(BlockMember).where(BlockMember.block_id == blocks[pk]))} if pk in blocks else {}
        decision = decisions[action['target_key']]
        official_id = None
        if decision.action == 'select':
            official = session.scalar(select(OfficialBar).where(OfficialBar.decision_id == decision.id))
            if official is None:
                raise FoundationError('PUBLICATION_INCOMPLETE', '正式类型化值缺失，不能封存发布。')
            official_id = official.id
        elif decision.action == 'retain':
            official_id = decision.parent_official_id
        state = {'select': 'value', 'retain': 'value', 'gap': 'gap', 'block': 'blocked', 'withdraw': 'withdrawn'}[decision.action]
        changed[pk][action['target_key']] = dict(instrument_id=iid, trade_date=business_date, state=state,
            official_id=official_id, decision_id=decision.id)
    for pk, entries in sorted(changed.items()):
        members = [entries[key] for key in sorted(entries)]
        block = ReleaseBlock(partition_key=pk, row_count=len(members), content_hash=digest('release-block', members), created_at=now())
        session.add(block); session.flush()
        session.add_all([BlockMember(block_id=block.id, **member) for member in members]); session.flush()
        blocks[pk] = block.id
    manifest = [[pk, block_id, session.get(ReleaseBlock, block_id).content_hash] for pk, block_id in sorted(blocks.items())]
    release = Release(work_id=work.id, scope_key=work.scope_key, parent_id=work.parent_release_id,
        manifest_hash=digest('release-manifest', manifest), status='draft', created_at=now())
    session.add(release); session.flush()
    session.add_all([BlockRef(release_id=release.id, partition_key=pk, block_id=bid) for pk, bid in blocks.items()])
    session.flush()
    validate_release(session, release)
    release.status = 'sealed'
    session.flush()
    return release


def validate_release(session, release):
    work = session.get(Work, release.work_id)
    series = json.loads(work.parameters_json)['series']
    manifest = []
    for ref in session.scalars(select(BlockRef).where(BlockRef.release_id == release.id).order_by(BlockRef.partition_key)):
        block = session.get(ReleaseBlock, ref.block_id)
        members = session.scalars(select(BlockMember).where(BlockMember.block_id == block.id).order_by(BlockMember.instrument_id, BlockMember.trade_date)).all()
        if ref.partition_key != block.partition_key or len(members) != block.row_count:
            raise FoundationError('MANIFEST_INVALID', '发布块范围或行数不符。')
        for member in members:
            if partition(member.instrument_id, member.trade_date) != ref.partition_key:
                raise FoundationError('MANIFEST_INVALID', '发布成员不属于当前分区。')
            decision = session.get(Decision, member.decision_id)
            if decision.target_key != target_key(member.instrument_id, member.trade_date):
                raise FoundationError('MANIFEST_INVALID', '成员与决策目标不一致。')
            if member.official_id:
                official = session.get(OfficialBar, member.official_id)
                if (official.instrument_id, official.trade_date, official.series) != (member.instrument_id, member.trade_date, series):
                    raise FoundationError('MANIFEST_INVALID', '成员与正式值语义不一致。')
                if digest('bar-values', values(official)) != official.values_hash:
                    raise FoundationError('MANIFEST_INVALID', '正式值摘要不一致。')
        if digest('release-block', [member_value(m) for m in members]) != block.content_hash:
            raise FoundationError('MANIFEST_INVALID', '发布块内容摘要不一致。')
        manifest.append([ref.partition_key, ref.block_id, block.content_hash])
    if digest('release-manifest', manifest) != release.manifest_hash:
        raise FoundationError('MANIFEST_INVALID', '发布清单摘要不一致。')


def publish(session, release_id, epoch):
    """Lock issue scope -> head -> work; callers commit this short transaction.

    A sealed release has already passed content validation. Immutable database
    guards make a full source scan unnecessary while holding the publication lock.
    """
    release = session.get(Release, release_id)
    if not release:
        raise FoundationError('RELEASE_UNAVAILABLE', '固定发布不存在。')
    scope = session.scalar(select(IssueScope).where(IssueScope.scope_key == release.scope_key).with_for_update(read=True).execution_options(populate_existing=True))
    lock_key(session, 'head', release.scope_key)
    head = session.scalar(select(Head).where(Head.scope_key == release.scope_key).with_for_update().execution_options(populate_existing=True))
    if release.status == 'published':
        return release
    work = fenced(session, release.work_id, epoch)
    if scope is None:
        raise FoundationError('DEPENDENCY_MISSING', '发布缺少问题限制上下文。')
    if scope.epoch != work.expected_issue_epoch:
        finish_batch(session, work, status='superseded', error_code='ISSUE_CONTEXT_CHANGED')
        return None
    if (head.release_id if head else None) != work.parent_release_id or (head.revision if head else 0) != work.expected_head_revision:
        finish_batch(session, work, status='superseded', error_code='HEAD_CHANGED')
        return None
    if release.status != 'sealed' or work.cursor != work.total:
        raise FoundationError('PUBLICATION_INCOMPLETE', '发布尚未封存。')
    release.status = 'published'
    release.published_at = now()
    session.flush()
    if head:
        head.release_id = release.id
        head.revision += 1
    else:
        session.add(Head(scope_key=release.scope_key, release_id=release.id, revision=1))
    finish_batch(session, work, status='succeeded')
    append_event(session, work, 'publication', 'published', {'release_id': release.id, 'manifest_hash': release.manifest_hash})
    session.flush()
    return release
