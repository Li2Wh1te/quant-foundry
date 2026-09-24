"""Closed typed-record proof semantics, independent of scheduling/reconciliation.

Runtime execution identity remains a separate, stricter gate. Changing this
module, its explicit semantic dependencies, Python or the dependency lock
invalidates proofs. Changing an unrelated foundation module does not.
"""
import hashlib
import json
from pathlib import Path
import platform

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.record_models import RecordBlockMember, RecordBlockVerification, OfficialRecord
from app.data_foundation.record_schemas import validate_body
from app.data_foundation.record_values import fields, value_hash, MEMBER_FIELDS, partition_key, subject_partitioned
from app.data_foundation.work_models import Work, Candidate, Decision, BlockRef, ReleaseBlock

# An explicit, conservative closure. A new semantic helper must be added here
# and in the dependency-closure regression test; do not use a hand-edited label.
SEMANTIC_FILES = ('record_validation.py', 'record_validation_driver.py', 'record_values.py',
                  'record_schemas.py', 'canonical.py', 'record_models.py', 'record_validation_models.py',
                  'work_models.py', 'models.py')


def validation_hash():
    root = Path(__file__).parent
    dependencies = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SEMANTIC_FILES}
    dependencies['db/base.py'] = hashlib.sha256((root.parent / 'db/base.py').read_bytes()).hexdigest()
    return digest('typed-record-block-validator-v2', [dependencies, platform.python_version(),
        hashlib.sha256((root.parents[1] / 'uv.lock').read_bytes()).hexdigest()])


def root_entries(session, release, dataset, validator):
    """Check the entire root and all receipt metadata before trusting any hit."""
    refs = list(session.scalars(select(BlockRef).where(BlockRef.release_id == release.id).order_by(BlockRef.partition_key)))
    by_subject = subject_partitioned(dataset, [r.partition_key for r in refs])
    blocks = {b.id: b for b in session.scalars(select(ReleaseBlock).where(ReleaseBlock.id.in_([r.block_id for r in refs])))}
    receipts = {v.block_id: v for v in session.scalars(select(RecordBlockVerification).where(
        RecordBlockVerification.block_id.in_(blocks), RecordBlockVerification.validator_hash == validator))}
    manifest, entries = [], []
    for ref in refs:
        block = blocks.get(ref.block_id)
        if block is None or block.partition_key != ref.partition_key:
            raise FoundationError('MANIFEST_INVALID', '领域发布块范围不一致。')
        manifest.append([ref.partition_key, ref.block_id, block.content_hash])
        expected = dict(scope_key=release.scope_key, schema_key=dataset,
                        content_hash=block.content_hash, row_count=block.row_count)
        receipt = receipts.get(block.id)
        if receipt and any(getattr(receipt, key) != value for key, value in expected.items()):
            raise FoundationError('MANIFEST_INVALID', '领域发布块校验凭据与固定范围不一致。')
        entries.append((block, receipt, expected))
    if digest('release-manifest', manifest) != release.manifest_hash:
        raise FoundationError('MANIFEST_INVALID', '领域发布清单摘要不一致。')
    return entries, by_subject


def check_members(session, members, *, scope_key, dataset, partition, by_subject, body_validator=validate_body):
    """The same complete reference/value checks serve audits and bounded pages."""
    decisions = {d.id: d for d in session.scalars(select(Decision).where(
        Decision.id.in_({m.decision_id for m in members})))}
    works = {w.id: w for w in session.scalars(select(Work).where(
        Work.id.in_({d.work_id for d in decisions.values()})))}
    officials = {o.id: o for o in session.scalars(select(OfficialRecord).where(
        OfficialRecord.id.in_({m.official_id for m in members if m.official_id})))}
    retained = {d.id for d in decisions.values() if d.action == 'retain'}
    parent_values = dict(session.execute(select(Decision.id, RecordBlockMember.official_id)
        .join(Work, Work.id == Decision.work_id)
        .join(BlockRef, BlockRef.release_id == Work.parent_release_id)
        .join(RecordBlockMember, RecordBlockMember.block_id == BlockRef.block_id)
        .where(Decision.id.in_(retained), RecordBlockMember.target_key == Decision.target_key,
            RecordBlockMember.state == 'value')).all()) if retained else {}
    for member in members:
        decision = decisions.get(member.decision_id)
        if decision is None or decision.work_id not in works:
            raise FoundationError('MANIFEST_INVALID', '领域发布的治理决策缺失。')
        if (partition_key(member.target_key, member.subject_id, by_subject) != partition
                or decision.target_key != member.target_key or works[decision.work_id].scope_key != scope_key
                or member.state != {'select': 'value', 'retain': 'value', 'block': 'blocked', 'gap': 'gap', 'withdraw': 'withdrawn'}[decision.action]):
            raise FoundationError('MANIFEST_INVALID', '领域成员与决策范围不一致。')
        if member.official_id:
            official = officials.get(member.official_id)
            if (official is None or official.schema_key != dataset or official.subject_id != member.subject_id
                    or official.business_key != member.target_key or official.business_date != member.business_date
                    or value_hash(official) != official.values_hash):
                raise FoundationError('MANIFEST_INVALID', '正式领域记录与发布成员不一致。')
            body_validator(dataset, json.loads(official.body_json))
            if decision.action == 'retain':
                if parent_values.get(decision.id) != official.id or decision.parent_record_id != official.id:
                    raise FoundationError('MANIFEST_INVALID', '保留记录不属于固定父发布。')
            elif official.decision_id != decision.id or official.candidate_id != decision.selected_candidate_id:
                raise FoundationError('MANIFEST_INVALID', '正式记录与选中候选不一致。')
        elif member.state == 'value':
            raise FoundationError('MANIFEST_INVALID', '正式值成员缺少正式记录。')
    normalized = set(session.scalars(select(Candidate.work_id).where(
        Candidate.id.in_({o.candidate_id for o in officials.values()})).distinct()))
    return dict(governance_work_ids_json=encode(sorted({str(d.work_id) for d in decisions.values()})),
                normalization_work_ids_json=encode(sorted(str(wid) for wid in normalized)))


def save_receipts(session, receipts):
    if receipts:
        session.execute(insert(RecordBlockVerification).values(receipts).on_conflict_do_nothing(
            index_elements=['block_id', 'validator_hash']))


def validate_release(session, release, dataset, validator, *, reuse_verified=False,
                     require_receipts=False, body_validator=validate_body):
    entries, by_subject = root_entries(session, release, dataset, validator)
    verified = []
    for block, receipt, expected in entries:
        if reuse_verified and receipt:
            continue
        if require_receipts:
            raise FoundationError('VALIDATION_INCOMPLETE', '发布块尚未完成有界校验。')
        members = session.scalars(select(RecordBlockMember).where(RecordBlockMember.block_id == block.id)
            .order_by(RecordBlockMember.target_key)).all()
        if (len(members) != block.row_count
                or digest('typed-record-block-v1', [fields(m, MEMBER_FIELDS) for m in members]) != block.content_hash):
            raise FoundationError('MANIFEST_INVALID', '领域发布块摘要或数量不一致。')
        contributions = check_members(session, members, scope_key=release.scope_key, dataset=dataset,
            partition=block.partition_key, by_subject=by_subject, body_validator=body_validator)
        if receipt and any(getattr(receipt, name) != value for name, value in contributions.items()):
            raise FoundationError('MANIFEST_INVALID', '领域发布块贡献来源与校验凭据不一致。')
        if receipt is None:
            verified.append(dict(block_id=block.id, validator_hash=validator, **expected, verified_at=now(), **contributions))
    # Root and all requested members passed before these proofs become visible.
    save_receipts(session, verified)
