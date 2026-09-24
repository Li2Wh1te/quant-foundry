"""Resumable validation of closed drafts without a long work-row transaction."""
import hashlib
import json
import time
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.record_validation_models import RecordValidationRoot, RecordBlockPageVerification
from app.data_foundation.record_models import RecordBlockMember, RecordBlockVerification
from app.data_foundation.record_values import fields, MEMBER_FIELDS
from app.data_foundation.record_validation import root_entries, check_members, save_receipts, validate_release
from app.data_foundation.work_models import Work, Release
from app.data_foundation.work import BATCH_ROWS, fenced


def freeze_root(session, release):
    """The database also forbids new refs after this append-only marker."""
    from app.data_foundation.record_work import verify, validation_hash
    params = verify(session.get(Work, release.work_id))
    entries, _ = root_entries(session, release, params['dataset'], validation_hash())
    if session.get(RecordValidationRoot, release.id) is None:
        session.add(RecordValidationRoot(release_id=release.id, manifest_hash=release.manifest_hash,
                                        ref_count=len(entries), created_at=now()))
        session.flush()


def _finish_block(session, block, validator, expected, pages):
    """Recompute the original canonical block digest with bounded memory.

    Page proofs do not replace the original digest. This final narrow scan
    checks their exact boundaries/checksums plus the full legacy SHA-256. It
    holds no work-row lock, so heartbeats can continue during a large scan.
    """
    aggregate = hashlib.sha256(b'foundation-canonical-v1\0typed-record-block-v1\0[')
    count, page_values, ordinal, after = 0, [], 0, None
    gov, norm = set(), set()
    query = select(*[getattr(RecordBlockMember, name) for name in MEMBER_FIELDS]).where(
        RecordBlockMember.block_id == block.id).order_by(RecordBlockMember.target_key)
    def check_page(values):
        nonlocal ordinal, after
        if ordinal >= len(pages):
            raise FoundationError('MANIFEST_INVALID', '发布块分页校验凭据缺失。')
        proof = pages[ordinal]
        if (proof.ordinal != ordinal or proof.after_key != after or proof.last_key != values[-1]['target_key']
                or proof.row_count != len(values) or proof.scope_key != expected['scope_key']
                or proof.schema_key != expected['schema_key']
                or proof.content_hash != digest('typed-record-block-page-v1', values)):
            raise FoundationError('MANIFEST_INVALID', '发布块分页校验凭据范围或摘要不一致。')
        gov.update(json.loads(proof.governance_work_ids_json))
        norm.update(json.loads(proof.normalization_work_ids_json))
        after, ordinal = proof.last_key, ordinal+1
    for row in session.execute(query.execution_options(yield_per=BATCH_ROWS)):
        value = dict(row._mapping)
        if count:
            aggregate.update(b',')
        aggregate.update(encode(value).encode())
        count += 1
        page_values.append(value)
        if len(page_values) == BATCH_ROWS:
            check_page(page_values)
            page_values = []
    if page_values:
        check_page(page_values)
    aggregate.update(b']')
    if count != block.row_count or ordinal != len(pages) or aggregate.hexdigest() != block.content_hash:
        raise FoundationError('MANIFEST_INVALID', '领域发布块摘要或数量不一致。')
    save_receipts(session, [dict(block_id=block.id, validator_hash=validator, **expected, verified_at=now(),
        governance_work_ids_json=encode(sorted(gov)), normalization_work_ids_json=encode(sorted(norm)))])


def _page_step(session, block, validator, expected, by_subject):
    """Validate at most 200 full records; the completion pass reads thin fields."""
    last = session.scalar(select(RecordBlockPageVerification).where(
        RecordBlockPageVerification.block_id == block.id,
        RecordBlockPageVerification.validator_hash == validator)
        .order_by(RecordBlockPageVerification.ordinal.desc()).limit(1))
    after = last.last_key if last else None
    members = list(session.scalars(select(RecordBlockMember).where(RecordBlockMember.block_id == block.id,
        RecordBlockMember.target_key > after if after else True)
        .order_by(RecordBlockMember.target_key).limit(BATCH_ROWS)))
    if members:
        contributions = check_members(session, members, scope_key=expected['scope_key'], dataset=expected['schema_key'],
                                      partition=block.partition_key, by_subject=by_subject)
        session.execute(insert(RecordBlockPageVerification).values(block_id=block.id, validator_hash=validator,
            ordinal=last.ordinal+1 if last else 0, scope_key=expected['scope_key'], schema_key=expected['schema_key'],
            after_key=after, last_key=members[-1].target_key, row_count=len(members),
            content_hash=digest('typed-record-block-page-v1', [fields(m, MEMBER_FIELDS) for m in members]),
            **contributions, verified_at=now()).on_conflict_do_nothing(
                index_elements=['block_id', 'validator_hash', 'ordinal']))
    if len(members) < BATCH_ROWS or ((last.ordinal+1 if last else 0)*BATCH_ROWS + len(members) >= block.row_count):
        pages = list(session.scalars(select(RecordBlockPageVerification).where(
            RecordBlockPageVerification.block_id == block.id, RecordBlockPageVerification.validator_hash == validator)
            .order_by(RecordBlockPageVerification.ordinal)))
        _finish_block(session, block, validator, expected, pages)
        return True
    return False


def advance_validation(engine, work_id, epoch, *, stop, budget_seconds=20, max_pages=100, clock=time.monotonic):
    """Commit independent proof pages, then seal with a fresh fenced short TX.

    Existing receipts with different validator identities are never relabelled
    or copied. The first deployment revalidates them incrementally and resumes
    committed pages after interruption. Live publication gates remain later.
    """
    from app.data_foundation.record_work import verify, validation_hash
    deadline = clock() + budget_seconds
    validator = validation_hash()
    with Session(engine) as session:
        work = session.get(Work, work_id)
        params = verify(work)
        release = session.scalar(select(Release).where(Release.work_id == work_id))
        if release is None or release.status not in ('draft', 'sealed'):
            raise FoundationError('PUBLICATION_INCOMPLETE', '待校验发布不存在或状态不符。')
        if release.status == 'sealed':
            return True
        root = session.get(RecordValidationRoot, release.id)
        entries, by_subject = root_entries(session, release, params['dataset'], validator)
        if root is None or root.manifest_hash != release.manifest_hash or root.ref_count != len(entries):
            raise FoundationError('MANIFEST_INVALID', '发布引用清单尚未冻结或摘要不一致。')
        missing = [(block, expected) for block, receipt, expected in entries if receipt is None]
        release_id = release.id
    completed = 0
    for block, expected in missing:
        while True:
            if stop.is_set() or completed >= max_pages or (completed and clock() >= deadline):
                return False
            with Session(engine) as session, session.begin():
                session.execute(text("SET LOCAL statement_timeout = '30s'"))
                done = _page_step(session, block, validator, expected, by_subject)
                # Proof reads never hold the work row. The final write/commit
                # still belongs to the currently live epoch, not a stale owner.
                fenced(session, work_id, epoch)
            completed += 1
            if done:
                break
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL statement_timeout = '30s'"))
        fenced(session, work_id, epoch)
        release = session.get(Release, release_id)
        validate_release(session, release, params['dataset'], validator, reuse_verified=True, require_receipts=True)
        release.status = 'sealed'
        session.flush()
        fenced(session, work_id, epoch)
    return True
