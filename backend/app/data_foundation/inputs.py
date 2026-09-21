"""Seal actual candidate manifests and preserve historical exclusion evidence."""
import json
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.work_models import Work, CandidateManifest, CandidateEntry
from app.data_foundation.batch_models import CandidateInputSet, CandidateInputEntry


def seal_input_set(session, entries):
    if not 1 <= len(entries) <= 100 or len({e['manifest_id'] for e in entries}) != len(entries):
        raise ValueError('Expected 1 to 100 distinct candidate manifests')
    fixed, scopes, contracts = [], set(), set()
    for entry in entries:
        if set(entry) != {'manifest_id', 'role', 'reason'} or entry['role'] not in ('eligible', 'historical'):
            raise ValueError('Invalid input role')
        if not isinstance(entry['reason'], str) or not entry['reason'].strip() or len(entry['reason']) > 256:
            raise ValueError('Each inclusion or exclusion needs a reason')
        manifest = session.get(CandidateManifest, entry['manifest_id'])
        origin = session.get(Work, manifest.work_id) if manifest else None
        if origin is None or origin.kind != 'A' or origin.status != 'succeeded':
            raise FoundationError('CANDIDATE_NOT_SEALED', '输入工作尚未完整封存。')
        scopes.add(origin.scope_key); contracts.add(origin.contract_id)
        fixed.append({**entry, 'manifest_hash': manifest.manifest_hash})
    if len(scopes) != 1 or len(contracts) != 1 or not any(e['role'] == 'eligible' for e in fixed):
        raise FoundationError('SCOPE_MISMATCH', '多输入必须具有相同契约范围及至少一个有效输入。')
    fixed.sort(key=lambda e: str(e['manifest_id']))
    fingerprint = digest('candidate-input-set-v1', fixed)
    lock_key(session, 'candidate-input-set', fingerprint)
    result = session.scalar(select(CandidateInputSet).where(CandidateInputSet.manifest_hash == fingerprint))
    if result:
        input_manifests(session, input_set_id=result.id, eligible_only=False)
        return result
    result = CandidateInputSet(manifest_hash=fingerprint, scope_key=next(iter(scopes)), entry_count=len(fixed), created_at=now())
    session.add(result); session.flush()
    session.add_all([CandidateInputEntry(input_set_id=result.id, **item) for item in fixed]); session.flush()
    return result


def input_manifests(session, *, work=None, manifest_id=None, input_set_id=None, eligible_only=True):
    if work is not None:
        manifest_id, input_set_id = work.candidate_manifest_id, work.candidate_input_set_id
    if bool(manifest_id) == bool(input_set_id):
        raise FoundationError('CANDIDATE_NOT_SEALED', '治理输入模式不明确。')
    if manifest_id:
        row = session.get(CandidateManifest, manifest_id)
        if row is None:
            raise FoundationError('CANDIDATE_NOT_SEALED', '候选清单不存在。')
        return [row]
    sealed = session.get(CandidateInputSet, input_set_id)
    entries = session.scalars(select(CandidateInputEntry).where(CandidateInputEntry.input_set_id == input_set_id)
        .order_by(CandidateInputEntry.manifest_id)).all()
    fixed = [dict(manifest_id=e.manifest_id, role=e.role, reason=e.reason, manifest_hash=e.manifest_hash) for e in entries]
    if sealed is None or len(entries) != sealed.entry_count or digest('candidate-input-set-v1', fixed) != sealed.manifest_hash:
        raise FoundationError('CANDIDATE_NOT_SEALED', '多输入清单完整性校验失败。')
    rows = []
    for entry in entries:
        row = session.get(CandidateManifest, entry.manifest_id)
        origin = session.get(Work, row.work_id) if row else None
        if row is None or row.manifest_hash != entry.manifest_hash or origin.status != 'succeeded' or origin.scope_key != sealed.scope_key:
            raise FoundationError('CANDIDATE_NOT_SEALED', '多输入的候选工作已失配。')
        if not eligible_only or entry.role == 'eligible':
            rows.append(row)
    if not rows:
        raise FoundationError('CANDIDATE_NOT_SEALED', '多输入清单没有有效候选工作。')
    return rows


def candidate_origins(session, work):
    """Map candidate IDs to their actual original manifests, never a main input."""
    ids = [m.id for m in input_manifests(session, work=work)]
    return {e.candidate_id: e.manifest_id for e in session.scalars(select(CandidateEntry).where(CandidateEntry.manifest_id.in_(ids)))}


def combined_parameters(session, manifests):
    values = [json.loads(session.get(Work, m.work_id).parameters_json) for m in manifests]
    common = ('dataset', 'major', 'profile', 'series', 'domain', 'domain_hash')
    if any(any(v[k] != values[0][k] for k in common) for v in values):
        raise FoundationError('SCOPE_MISMATCH', '候选输入的执行语义不一致。')
    result = {k: values[0][k] for k in common}
    result.update(start=min(v['start'] for v in values), end=max(v['end'] for v in values))
    if 'report_keys' in values[0]:
        result['report_keys'] = sorted({k for v in values for k in v['report_keys']})
    return result
