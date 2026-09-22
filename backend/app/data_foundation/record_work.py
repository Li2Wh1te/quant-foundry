"""Typed source-local records on the shared fenced normalization/release kernel."""
from collections import Counter, defaultdict
from pathlib import Path
from uuid import UUID
import json
import time

from sqlalchemy import select

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now, lock_key, register_definition, register_dependencies
from app.data_foundation.models import SourceRef, Definition
from app.data_foundation.source_refs import read_source
from app.data_foundation.record_models import RecordSubject, CandidateRecord, OfficialRecord, RecordBlockMember
from app.data_foundation.record_schemas import schema_for, validate_body
from app.data_foundation.record_adapters import dataset_for, rows_for, convert
from app.data_foundation.work import create_work, fenced, finish_batch, append_event, BATCH_ROWS, BUDGET_SECONDS
from app.data_foundation.work_models import (Work, Candidate, CandidateManifest, CandidateEntry, Assessment,
    Unit, Decision, Release, ReleaseBlock, BlockRef)
from app.data_foundation.quality import assessment

DOMAIN_KEY = 'typed-record-v1'
VALUE_FIELDS = ('subject_id', 'business_key', 'business_date', 'schema_key', 'body_json', 'field_quality_json')
MEMBER_FIELDS = ('target_key', 'subject_id', 'business_date', 'state', 'official_id', 'decision_id')


def domain_hash():
    return digest('typed-record-code-v1', {name: (Path(__file__).parent / name).read_text()
        for name in ('record_work.py', 'record_adapters.py', 'record_schemas.py', 'record_models.py')})


def fields(row, names=VALUE_FIELDS):
    return {name: getattr(row, name) for name in names}


def value_hash(row):
    return digest('typed-record-value-v1', fields(row))


def series_for(dataset, source):
    return f'{source}-{dataset}-observed-v1'


def register_catalog(session, dataset, source):
    schema = schema_for(dataset)
    shape = schema.body.model_json_schema()
    definitions = dict(schema_version=1, subject_kind='source_local:' + schema.identity_kind,
        key_fields=['source', 'subject_kind', 'subject_key', *schema.business_fields],
        core_fields={key: shape['properties'][key] for key in schema.core_fields},
        optional_fields={key: value for key, value in shape['properties'].items() if key not in schema.core_fields},
        time_semantics={'business': list(schema.business_fields), 'observation': 'source_observed_at', 'public_at': 'unverified'},
        atomic_unit='typed-record', supported_filters=['subjects', 'start', 'end'],
        canonical_schema=shape, limitations=list(schema.limitations))
    contract = register_definition(session, kind='contract', name=dataset, version='1.0', definition=definitions)
    series = register_definition(session, kind='series', name=series_for(dataset, source), version='1', definition={
        'schema_version': 1, 'object_kind': 'typed-record', 'source': source,
        'identity_basis': 'source_local_observed', 'public_time_status': 'unverified',
        'schema_key': dataset, 'atomic_unit': 'typed-record'})
    policy = register_definition(session, kind='policy', name=f'{source}-{dataset}', version='1', definition={
        'schema_version': 1, 'dataset': dataset, 'major': 1, 'profile': 'default', 'series': series.name,
        'input_admission': 'fixed-source-and-validated-canonical-record', 'core_fields': list(schema.core_fields),
        'source_order': [source], 'comparison': {'enabled': False}, 'fallback': {'enabled': False},
        'atomicity': 'whole-record'})
    register_definition(session, kind='support', name=dataset, version='typed-record-v1', definition={
        'schema_version': 1, 'read_status': 'implemented', 'update_status': 'bounded_local_versions',
        'replay_status': 'verify_execution_dependencies', 'notice_days': 30, 'approval_required': True,
        'time_modes': ['observed'], 'identity_basis': 'source_local_observed'})
    return contract, series, policy


def create_normalization(session, *, source_ref_id, execution_id):
    source = session.get(SourceRef, source_ref_id)
    if source is None:
        raise FoundationError('SOURCE_UNAVAILABLE', '固定来源不存在。')
    dataset = dataset_for(source)
    contract, series, policy = register_catalog(session, dataset, source.source)
    dependency = register_dependencies(session, [
        {'source_ref_id': source.id, 'purpose': 'fixed_native_record_input'},
        {'definition_id': contract.id, 'purpose': 'canonical_record_schema'},
        {'definition_id': series.id, 'purpose': 'source_local_observation_semantics'},
        {'execution_id': execution_id, 'purpose': 'typed_record_normalization'}])
    # These bounds admit the fixed object's actual business dates; the source
    # reference, not a moving date window or latest pointer, determines input.
    work = create_work(session, kind='A', contract_id=contract.id, execution_id=execution_id,
        dependency_id=dependency.id, source_ref_id=source.id, parameters={
            'dataset': dataset, 'major': 1, 'profile': 'default', 'series': series.name,
            'start': '0001-01-01', 'end': '9999-12-31', 'domain': DOMAIN_KEY, 'domain_hash': domain_hash()})
    return work, policy


def verify(work):
    params = json.loads(work.parameters_json)
    if params['domain'] != DOMAIN_KEY or params['domain_hash'] != domain_hash():
        raise FoundationError('DEPENDENCY_MISSING', '固定领域转换版本与当前运行代码不一致。')
    schema_for(params['dataset'])
    return params


def record_key(source, normalized):
    schema = schema_for(normalized['dataset'])
    return digest('typed-record-business-key-v1', [normalized['dataset'], source.source,
        normalized['subject_kind'], normalized['subject_key'],
        [normalized['body'][name] for name in schema.business_fields]])


def register_subject(session, source, normalized):
    key = (source.source, normalized['subject_kind'], normalized['subject_key'])
    if len(key[1]) > 64 or len(key[2]) > 128:
        raise FoundationError('IDENTITY_UNRESOLVED', '来源主体标识超出契约长度。')
    lock_key(session, 'record-subject', key)
    row = session.scalar(select(RecordSubject).where(RecordSubject.source == key[0],
        RecordSubject.kind == key[1], RecordSubject.source_key == key[2]))
    if row is None:
        row = RecordSubject(source=key[0], kind=key[1], source_key=key[2],
            evidence_source_ref_id=source.id, created_at=now())
        session.add(row)
        session.flush()
    return row


def normalize_batch(session, work_id, epoch):
    work = fenced(session, work_id, epoch)
    params = verify(work)
    source = session.get(SourceRef, work.source_ref_id)
    if work.kind != 'A' or dataset_for(source) != params['dataset']:
        raise FoundationError('SCOPE_MISMATCH', '来源与领域契约不一致。')
    raw_rows = rows_for(source, read_source(session, source.id))
    parsed = []
    for raw in raw_rows:
        try:
            value = convert(source, raw)
            parsed.append((value, record_key(source, value), None))
        except (FoundationError, ValueError, TypeError, OverflowError) as exc:
            parsed.append((None, None, getattr(exc, 'code', 'SOURCE_SCHEMA_INVALID')))
    counts = Counter(key for _, key, _ in parsed if key is not None)
    work.total = len(parsed)
    end = min(work.cursor + BATCH_ROWS, work.total)
    started = time.monotonic()
    for index in range(work.cursor, end):
        value, key, reason = parsed[index]
        subject = register_subject(session, source, value) if value else None
        if key is not None and counts[key] != 1:
            reason = 'DUPLICATE_BUSINESS_KEY'
        typed = dict(subject_id=subject.id, business_key=key, business_date=value['business_date'],
            schema_key=params['dataset'], body_json=encode(value['body']),
            field_quality_json=encode(value['field_quality'])) if value else None
        checked = assessment(session, input_hash=digest('typed-record-input', [work.fingerprint, index, raw_rows[index]]),
            rule_hash=params['domain_hash'], scope_hash=work.scope_key, status='fail' if reason else 'pass',
            results={'reason': reason, 'reasons': [reason] if reason else [], 'target_key': key, 'occurrence': index,
                'field_quality': value['field_quality'] if value else {}})
        candidate = Candidate(work_id=work.id, source_ref_id=source.id, record_subject_id=subject.id if subject else None,
            binding_id=None, dependency_id=work.dependency_id, unit_key=str(index), occurrence=index,
            values_hash=digest('typed-record-value-v1', typed) if typed else digest('quarantined-record', raw_rows[index]),
            assessment_id=checked.id, readiness='quarantined' if reason else 'ready', created_at=now())
        session.add(candidate)
        session.flush()
        if typed:
            session.add(CandidateRecord(candidate_id=candidate.id, **typed))
        session.add(Unit(work_id=work.id, unit_key=str(index), result_hash=candidate.values_hash, row_count=1))
        if time.monotonic() - started >= BUDGET_SECONDS:
            end = index + 1
            break
    work.cursor = end
    session.flush()
    fenced(session, work.id, epoch)
    if end == work.total:
        candidates = session.scalars(select(Candidate).where(Candidate.work_id == work.id).order_by(Candidate.occurrence)).all()
        if len(candidates) != work.total:
            raise FoundationError('CANDIDATE_INCOMPLETE', '领域候选未完整处理。')
        manifest = CandidateManifest(work_id=work.id, row_count=len(candidates),
            manifest_hash=digest('candidates', [[c.id, c.values_hash, c.readiness] for c in candidates]), created_at=now())
        session.add(manifest)
        session.flush()
        session.add_all([CandidateEntry(manifest_id=manifest.id, ordinal=i, candidate_id=c.id) for i, c in enumerate(candidates)])
        append_event(session, work, 'quality', 'evaluated', {'ready': sum(c.readiness == 'ready' for c in candidates),
            'quarantined': sum(c.readiness != 'ready' for c in candidates)})
    finish_batch(session, work, status='succeeded' if end == work.total else 'queued')
    return end


def parent_member(session, release_id, key):
    if release_id is None:
        return None
    return session.scalar(select(RecordBlockMember).join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
        .where(BlockRef.release_id == release_id, RecordBlockMember.target_key == key))


def plan_actions(session, manifest_id, policy_id, parent_release_id=None):
    from app.data_foundation.governance import choose
    manifest = session.get(CandidateManifest, manifest_id)
    origin = session.get(Work, manifest.work_id) if manifest else None
    if origin is None or origin.status != 'succeeded':
        raise FoundationError('CANDIDATE_NOT_SEALED', '领域候选尚未封存。')
    params = verify(origin)
    policy = session.get(Definition, policy_id)
    if policy is None or policy.kind != 'policy':
        raise FoundationError('POLICY_MISMATCH', '领域治理政策不存在。')
    definition = json.loads(policy.definition_json)
    if any(definition[name] != params[name] for name in ('dataset', 'major', 'profile', 'series')):
        raise FoundationError('POLICY_MISMATCH', '领域治理政策范围不符。')
    groups = defaultdict(list)
    for candidate, typed, source in session.execute(select(Candidate, CandidateRecord, SourceRef)
            .join(CandidateEntry, CandidateEntry.candidate_id == Candidate.id)
            .join(CandidateRecord, CandidateRecord.candidate_id == Candidate.id)
            .join(SourceRef, SourceRef.id == Candidate.source_ref_id)
            .where(CandidateEntry.manifest_id == manifest.id)):
        groups[typed.business_key].append((candidate, typed, source))
    actions = []
    for key, group in sorted(groups.items()):
        action, cid, reason = choose([{'id': str(c.id), 'source': s.source, 'series': params['series'],
            'ready': c.readiness == 'ready'} for c, _, s in group], definition)
        parent = parent_member(session, parent_release_id, key)
        retained = None
        if action == 'select' and parent and parent.state == 'value':
            previous = session.get(OfficialRecord, parent.official_id)
            selected = next(c for c, _, _ in group if str(c.id) == cid)
            if previous.values_hash == selected.values_hash:
                action, cid, retained, reason = 'retain', None, str(previous.id), 'UNCHANGED_CANONICAL_RECORD'
        typed = group[0][1]
        actions.append(dict(target_key=key, subject_id=str(typed.subject_id),
            business_date=str(typed.business_date) if typed.business_date else None,
            action=action, candidate_id=cid, parent_record_id=retained, reason=reason))
    # Unlocatable inputs remain quarantined in the candidate ledger. This API
    # advertises published-key coverage only, never whole-source completeness.
    return actions


def create_governance(session, *, normalization_id, execution_id, policy_id, parent_release_id=None,
                      expected_head_revision=0, expected_issue_epoch=0):
    origin = session.get(Work, normalization_id)
    manifest = session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == normalization_id))
    if origin is None or manifest is None:
        raise FoundationError('CANDIDATE_NOT_SEALED', '领域候选尚未封存。')
    params = verify(origin)
    actions = plan_actions(session, manifest.id, policy_id, parent_release_id)
    return create_work(session, kind='B', contract_id=origin.contract_id, execution_id=execution_id,
        dependency_id=origin.dependency_id, candidate_manifest_id=manifest.id, policy_id=policy_id,
        parent_release_id=parent_release_id, expected_head_revision=expected_head_revision,
        expected_issue_epoch=expected_issue_epoch, parameters={**params, 'actions': actions})


def stage_decisions(session, work_id, epoch):
    work = fenced(session, work_id, epoch)
    params = verify(work)
    if work.kind != 'B' or work.candidate_input_set_id:
        raise FoundationError('SCOPE_MISMATCH', '领域治理需要单个固定来源候选清单。')
    manifest = session.get(CandidateManifest, work.candidate_manifest_id)
    origin = session.get(Work, manifest.work_id)
    actions = plan_actions(session, manifest.id, work.policy_id, work.parent_release_id)
    if (params != {**json.loads(origin.parameters_json), 'actions': actions}
            or work.dependency_id != origin.dependency_id):
        raise FoundationError('GOVERNANCE_PLAN_MISMATCH', '领域治理计划与固定输入不一致。')
    policy = session.get(Definition, work.policy_id)
    end = min(work.cursor + BATCH_ROWS, len(actions))
    for action in actions[work.cursor:end]:
        cid = UUID(action['candidate_id']) if action['candidate_id'] else None
        retained = UUID(action['parent_record_id']) if action['parent_record_id'] else None
        checked = assessment(session, input_hash=digest('record-decision-input', [work.fingerprint, action]),
            rule_hash=policy.content_hash, scope_hash=work.scope_key, status='pass', results=action)
        decision = Decision(work_id=work.id, assessment_id=checked.id, target_key=action['target_key'],
            candidate_manifest_id=manifest.id, selected_candidate_id=cid, parent_record_id=retained,
            action=action['action'], evidence_json=encode({'reason': action['reason'], 'policy_id': policy.id,
                'comparison': 'not_applicable', 'fallback': 'disabled'}), created_at=now())
        session.add(decision)
        session.flush()
        if cid:
            typed, candidate = session.get(CandidateRecord, cid), session.get(Candidate, cid)
            validate_body(params['dataset'], json.loads(typed.body_json))
            if (candidate.readiness != 'ready' or candidate.record_subject_id != typed.subject_id
                    or typed.business_key != action['target_key'] or value_hash(typed) != candidate.values_hash):
                raise FoundationError('CANDIDATE_NOT_READY', '领域候选身份、业务键或内容摘要不一致。')
            session.add(OfficialRecord(candidate_id=cid, decision_id=decision.id,
                values_hash=candidate.values_hash, created_at=now(), **fields(typed)))
        session.add(Unit(work_id=work.id, unit_key=action['target_key'], row_count=1,
            result_hash=digest('typed-record-action', action)))
    work.cursor, work.total = end, len(actions)
    session.flush()
    fenced(session, work.id, epoch)
    if end < work.total:
        finish_batch(session, work, status='queued')
        return None
    return seal_release(session, work)


def seal_release(session, work):
    existing = session.scalar(select(Release).where(Release.work_id == work.id))
    if existing:
        return existing
    params = verify(work)
    decisions = {d.target_key: d for d in session.scalars(select(Decision).where(Decision.work_id == work.id))}
    if work.cursor != work.total or len(decisions) != len(params['actions']):
        raise FoundationError('PUBLICATION_INCOMPLETE', '领域治理单元尚未完整提交。')
    blocks = {r.partition_key: r.block_id for r in session.scalars(select(BlockRef).where(BlockRef.release_id == work.parent_release_id))}
    changed = {}
    from datetime import date
    for action in params['actions']:
        key = action['target_key']
        partition = key[:2]
        if partition not in changed:
            changed[partition] = {m.target_key: fields(m, MEMBER_FIELDS) for m in session.scalars(
                select(RecordBlockMember).where(RecordBlockMember.block_id == blocks[partition]))} if partition in blocks else {}
        decision = decisions[key]
        official = session.scalar(select(OfficialRecord).where(OfficialRecord.decision_id == decision.id))
        if decision.action == 'retain':
            official = session.get(OfficialRecord, decision.parent_record_id)
        if decision.action in ('select', 'retain') and official is None:
            raise FoundationError('PUBLICATION_INCOMPLETE', '正式领域记录缺失。')
        changed[partition][key] = dict(target_key=key, subject_id=UUID(action['subject_id']),
            business_date=date.fromisoformat(action['business_date']) if action['business_date'] else None,
            state={'select': 'value', 'retain': 'value', 'block': 'blocked', 'gap': 'gap'}[decision.action],
            official_id=official.id if official else None, decision_id=decision.id)
    for partition, values in sorted(changed.items()):
        members = [values[key] for key in sorted(values)]
        block = ReleaseBlock(partition_key=partition, row_count=len(members),
            content_hash=digest('typed-record-block-v1', members), created_at=now())
        session.add(block)
        session.flush()
        session.add_all([RecordBlockMember(block_id=block.id, **member) for member in members])
        session.flush()
        blocks[partition] = block.id
    manifest = [[key, bid, session.get(ReleaseBlock, bid).content_hash] for key, bid in sorted(blocks.items())]
    release = Release(work_id=work.id, scope_key=work.scope_key, parent_id=work.parent_release_id,
        manifest_hash=digest('release-manifest', manifest), status='draft', created_at=now())
    session.add(release)
    session.flush()
    session.add_all([BlockRef(release_id=release.id, partition_key=key, block_id=bid) for key, bid in blocks.items()])
    session.flush()
    validate_release(session, release)
    release.status = 'sealed'
    session.flush()
    return release


def validate_release(session, release):
    params = verify(session.get(Work, release.work_id))
    manifest = []
    for ref in session.scalars(select(BlockRef).where(BlockRef.release_id == release.id).order_by(BlockRef.partition_key)):
        block = session.get(ReleaseBlock, ref.block_id)
        members = session.scalars(select(RecordBlockMember).where(RecordBlockMember.block_id == block.id)
            .order_by(RecordBlockMember.target_key)).all()
        if block.partition_key != ref.partition_key or len(members) != block.row_count:
            raise FoundationError('MANIFEST_INVALID', '领域发布块范围或数量不一致。')
        if digest('typed-record-block-v1', [fields(m, MEMBER_FIELDS) for m in members]) != block.content_hash:
            raise FoundationError('MANIFEST_INVALID', '领域发布块摘要不一致。')
        for member in members:
            decision = session.get(Decision, member.decision_id)
            if (member.target_key[:2] != ref.partition_key or decision.target_key != member.target_key
                    or session.get(Work, decision.work_id).scope_key != release.scope_key
                    or member.state != {'select': 'value', 'retain': 'value', 'block': 'blocked', 'gap': 'gap', 'withdraw': 'withdrawn'}[decision.action]):
                raise FoundationError('MANIFEST_INVALID', '领域成员与决策范围不一致。')
            if member.official_id:
                official = session.get(OfficialRecord, member.official_id)
                if (official is None or official.schema_key != params['dataset'] or official.subject_id != member.subject_id
                        or official.business_key != member.target_key or official.business_date != member.business_date
                        or value_hash(official) != official.values_hash):
                    raise FoundationError('MANIFEST_INVALID', '正式领域记录与发布成员不一致。')
                validate_body(params['dataset'], json.loads(official.body_json))
                if decision.action == 'retain':
                    parent = parent_member(session, session.get(Work, decision.work_id).parent_release_id, member.target_key)
                    if not parent or parent.state != 'value' or parent.official_id != official.id or decision.parent_record_id != official.id:
                        raise FoundationError('MANIFEST_INVALID', '保留记录不属于固定父发布。')
                elif official.decision_id != decision.id or official.candidate_id != decision.selected_candidate_id:
                    raise FoundationError('MANIFEST_INVALID', '正式记录与选中候选不一致。')
        manifest.append([ref.partition_key, ref.block_id, block.content_hash])
    if digest('release-manifest', manifest) != release.manifest_hash:
        raise FoundationError('MANIFEST_INVALID', '领域发布清单摘要不一致。')
