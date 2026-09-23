"""Typed source-local records on the shared fenced normalization/release kernel."""
from collections import Counter, defaultdict
from pathlib import Path
from uuid import UUID, uuid4
import json

from sqlalchemy import select

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now, lock_key, register_definition, register_dependencies
from app.data_foundation.models import SourceRef, Definition
from app.data_foundation.source_refs import read_source
from app.data_foundation.record_models import (RecordSubject, CandidateRecord, OfficialRecord,
    RecordBlockMember, RecordBlockVerification, RecordPlanVerification)
from app.data_foundation.record_schemas import schema_for, validate_body
from app.data_foundation.record_adapters import dataset_for, rows_for, convert
from app.data_foundation.work import create_work, fenced, finish_batch, append_event, BATCH_ROWS
from app.data_foundation.work_models import (Work, Candidate, CandidateManifest, CandidateEntry, Assessment,
    Unit, Decision, Release, ReleaseBlock, BlockRef)

DOMAIN_KEY = 'typed-record-v1'
VALUE_FIELDS = ('subject_id', 'business_key', 'business_date', 'schema_key', 'body_json', 'field_quality_json')
MEMBER_FIELDS = ('target_key', 'subject_id', 'business_date', 'state', 'official_id', 'decision_id')


def domain_hash():
    return digest('typed-record-code-v1', {name: (Path(__file__).parent / name).read_text()
        for name in ('record_work.py', 'record_adapters.py', 'record_schemas.py', 'record_models.py',
                     'record_bulk.py', 'manager_experience.py', 'fund_nav.py', 'fund_offerings.py', 'popularity.py', 'fund_quotas.py', 'daily_windows.py', 'quote_snapshots.py', 'market_activity.py', 'dragon_tiger.py', 'fund_performance.py', 'manager_style.py', 'manager_performance.py')})


def validation_hash():
    # Include shared canonical helpers and the dependency runtime, not just the
    # adapter, so a changed validator cannot inherit earlier proof results.
    from app.data_foundation.execution import installed_code_hash
    import hashlib
    import platform
    return digest('typed-record-validator-v1', [installed_code_hash(), platform.python_version(),
        hashlib.sha256((Path(__file__).resolve().parents[2] / 'uv.lock').read_bytes()).hexdigest()])


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
    from app.data_foundation.record_schemas import DATE_METADATA_REVISIONS
    if dataset in DATE_METADATA_REVISIONS:
        # Keep the original definition byte-for-byte immutable. The minor only
        # describes the already-stored date; it never changes a business key,
        # record body, source binding, major head, or historical public time.
        corrected = definitions | {'time_semantics': definitions['time_semantics'] | {'business': [schema.date_field]}}
        contract = register_definition(session, kind='contract', name=dataset,
            version=DATE_METADATA_REVISIONS[dataset], definition=corrected)

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
    from app.data_foundation.record_bulk import register_subjects, register_assessments
    subjects = register_subjects(session, source, [value for value, _, _ in parsed[work.cursor:end]])
    prepared = []
    for index in range(work.cursor, end):
        value, key, reason = parsed[index]
        subject = subjects[(source.source, value['subject_kind'], value['subject_key'])] if value else None
        if key is not None and counts[key] != 1:
            reason = 'DUPLICATE_BUSINESS_KEY'
        typed = dict(subject_id=subject.id, business_key=key, business_date=value['business_date'],
            schema_key=params['dataset'], body_json=encode(value['body']),
            field_quality_json=encode(value['field_quality'])) if value else None
        spec = dict(input_hash=digest('typed-record-input', [work.fingerprint, index, raw_rows[index]]),
            rule_hash=params['domain_hash'], scope_hash=work.scope_key, status='fail' if reason else 'pass',
            results={'reason': reason, 'reasons': [reason] if reason else [], 'target_key': key, 'occurrence': index,
                'field_quality': value['field_quality'] if value else {}})
        prepared.append((index, subject, typed, reason, spec))
    assessments = register_assessments(session, [entry[4] for entry in prepared])
    candidates, typed_rows, units = [], [], []
    for index, subject, typed, reason, spec in prepared:
        checked = assessments[(spec['input_hash'], spec['rule_hash'], spec['scope_hash'])]
        candidate = Candidate(id=uuid4(), work_id=work.id, source_ref_id=source.id,
            record_subject_id=subject.id if subject else None, binding_id=None,
            dependency_id=work.dependency_id, unit_key=str(index), occurrence=index,
            values_hash=digest('typed-record-value-v1', typed) if typed else digest('quarantined-record', raw_rows[index]),
            assessment_id=checked.id, readiness='quarantined' if reason else 'ready', created_at=now())
        candidates.append(candidate)
        if typed:
            typed_rows.append(CandidateRecord(candidate_id=candidate.id, **typed))
        units.append(Unit(work_id=work.id, unit_key=str(index), result_hash=candidate.values_hash, row_count=1))
    # Flush the parent rows once before child rows; explicit UUIDs allow the
    # entire bounded page to use batched inserts while retaining real FK edges.
    session.add_all(candidates)
    session.flush()
    session.add_all(typed_rows + units)
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


def subject_partitioned(dataset, partitions):
    # Time-series source chunks are ordered by subject/date. Subject blocks
    # avoid copying every historical hash bucket for each 2,000-row chunk.
    # Existing hash-partitioned lineages retain their original layout; a release
    # may never mix the two schemes and accidentally expose a key twice.
    if not partitions:
        return dataset in ('market.adjustment_factor', 'market.fund_daily')
    subject_flags = [key.startswith('subject:') for key in partitions]
    if any(subject_flags) and (not all(subject_flags) or dataset not in ('market.adjustment_factor', 'market.fund_daily')):
        raise FoundationError('MANIFEST_INVALID', '领域发布分块方式不一致。')
    return all(subject_flags)


def partition_key(key, subject_id, by_subject):
    return 'subject:' + str(subject_id) if by_subject else key[:2]


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
    subject_ids = set()
    for candidate, typed, source in session.execute(select(Candidate, CandidateRecord, SourceRef)
            .join(CandidateEntry, CandidateEntry.candidate_id == Candidate.id)
            .join(CandidateRecord, CandidateRecord.candidate_id == Candidate.id)
            .join(SourceRef, SourceRef.id == Candidate.source_ref_id)
            .where(CandidateEntry.manifest_id == manifest.id)):
        groups[typed.business_key].append((candidate, typed, source))
        subject_ids.add(typed.subject_id)
    # Load the finite parent key set once. Per-key lookups multiply every
    # governance page by the entire source directory/calendar size.
    parents = {}
    if parent_release_id and groups:
        parents = {m.target_key: (m, o) for m, o in session.execute(
            select(RecordBlockMember, OfficialRecord)
            .outerjoin(OfficialRecord, OfficialRecord.id == RecordBlockMember.official_id)
            .join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
            .where(BlockRef.release_id == parent_release_id, RecordBlockMember.target_key.in_(groups),
                RecordBlockMember.subject_id.in_(subject_ids)))}
    actions = []
    for key, group in sorted(groups.items()):
        action, cid, reason = choose([{'id': str(c.id), 'source': s.source, 'series': params['series'],
            'ready': c.readiness == 'ready'} for c, _, s in group], definition)
        parent, previous = parents.get(key, (None, None))
        retained = None
        if action == 'select' and parent and parent.state == 'value':
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
                      expected_head_revision=0, expected_issue_epoch=0, preserve_head=False, source_revision=None):
    if not isinstance(preserve_head, bool):
        raise ValueError('Head preservation must be an explicit boolean')
    origin = session.get(Work, normalization_id)
    manifest = session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == normalization_id))
    if origin is None or manifest is None:
        raise FoundationError('CANDIDATE_NOT_SEALED', '领域候选尚未封存。')
    params = verify(origin)
    actions = plan_actions(session, manifest.id, policy_id, parent_release_id)
    mode = {'head_mode': 'preserve'} if preserve_head else {}
    if source_revision is not None:
        if preserve_head or type(source_revision) is not int or source_revision < 1:
            raise ValueError('Invalid current source revision')
        if session.get(SourceRef, origin.source_ref_id).representation != 'ths_observation':
            raise FoundationError('SCOPE_MISMATCH', '当前来源指针仅适用于同花顺固定观察。')
        mode['source_revision'] = source_revision
    work = create_work(session, kind='B', contract_id=origin.contract_id, execution_id=execution_id,
        dependency_id=origin.dependency_id, candidate_manifest_id=manifest.id, policy_id=policy_id,
        parent_release_id=parent_release_id, expected_head_revision=expected_head_revision,
        expected_issue_epoch=expected_issue_epoch, parameters={**params, 'actions': actions, **mode})
    if source_revision is not None:
        from app.data_foundation.batch_models import WorkSourcePointer
        if session.get(WorkSourcePointer, (work.id, origin.source_ref_id)) is None:
            session.add(WorkSourcePointer(work_id=work.id, source_ref_id=origin.source_ref_id, revision=source_revision))
            session.flush()
    # The full sealed input and policy were just used to derive every action.
    # Work inputs/parameters and candidate/policy/parent evidence are immutable;
    # record that derivation transactionally instead of repeating it per page.
    _save_plan_verification(session, work, validation_hash(), len(actions))
    return work


def _plan_parameters_hash(work):
    import hashlib
    return hashlib.sha256(work.parameters_json.encode('utf-8')).hexdigest()


def _save_plan_verification(session, work, validator, action_count):
    from sqlalchemy.dialects.postgresql import insert
    session.execute(insert(RecordPlanVerification).values(work_id=work.id,
        validator_hash=validator, work_fingerprint=work.fingerprint,
        parameters_hash=_plan_parameters_hash(work), action_count=action_count, verified_at=now())
        .on_conflict_do_nothing(index_elements=['work_id', 'validator_hash']))


def verify_governance_plan(session, work, *, force=False):
    """Reuse a matching derivation receipt, or rederive the entire fixed plan.

    Generic work creation cannot forge a receipt: unverified work must pass the
    original full comparison before any page executes. Explicit audit callers
    use force=True to rederive even a previously verified immutable plan.
    """
    params = verify(work)
    if work.kind != 'B' or work.candidate_input_set_id:
        raise FoundationError('SCOPE_MISMATCH', '领域治理需要单个固定来源候选清单。')
    validator = validation_hash()
    receipt = session.get(RecordPlanVerification, (work.id, validator))
    if receipt:
        if (receipt.work_fingerprint != work.fingerprint
                or receipt.parameters_hash != _plan_parameters_hash(work)
                or receipt.action_count != len(params['actions'])):
            raise FoundationError('GOVERNANCE_PLAN_MISMATCH', '治理计划与已核验的固定推导凭据不一致。')
        if not force:
            return params['actions']
    manifest = session.get(CandidateManifest, work.candidate_manifest_id)
    origin = session.get(Work, manifest.work_id)
    actions = plan_actions(session, manifest.id, work.policy_id, work.parent_release_id)
    mode = {'head_mode': 'preserve'} if params.get('head_mode') == 'preserve' else {}
    if 'source_revision' in params:
        from app.data_foundation.batch_models import WorkSourcePointer
        pointers = list(session.scalars(select(WorkSourcePointer).where(WorkSourcePointer.work_id == work.id)))
        if (mode or len(pointers) != 1 or pointers[0].source_ref_id != origin.source_ref_id
                or pointers[0].revision != params['source_revision']):
            raise FoundationError('GOVERNANCE_PLAN_MISMATCH', '当前来源指针凭据与固定治理计划不一致。')
        mode['source_revision'] = params['source_revision']
    if (params != {**json.loads(origin.parameters_json), 'actions': actions, **mode}
            or work.dependency_id != origin.dependency_id):
        raise FoundationError('GOVERNANCE_PLAN_MISMATCH', '领域治理计划与固定输入不一致。')
    if receipt is None:
        _save_plan_verification(session, work, validator, len(actions))
    return actions


def stage_decisions(session, work_id, epoch):
    work = fenced(session, work_id, epoch)
    params = verify(work)
    actions = verify_governance_plan(session, work)
    manifest = session.get(CandidateManifest, work.candidate_manifest_id)
    policy = session.get(Definition, work.policy_id)
    end = min(work.cursor + BATCH_ROWS, len(actions))
    from app.data_foundation.record_bulk import register_assessments
    page = actions[work.cursor:end]
    candidate_ids = [UUID(a['candidate_id']) for a in page if a['candidate_id']]
    selected = {c.id: (c, typed) for c, typed in session.execute(select(Candidate, CandidateRecord)
        .join(CandidateRecord, CandidateRecord.candidate_id == Candidate.id)
        .where(Candidate.id.in_(candidate_ids)))} if candidate_ids else {}
    specifications = [dict(input_hash=digest('record-decision-input', [work.fingerprint, action]),
        rule_hash=policy.content_hash, scope_hash=work.scope_key, status='pass', results=action) for action in page]
    assessments = register_assessments(session, specifications)
    decisions, officials, units = [], [], []
    for action, spec in zip(page, specifications, strict=True):
        cid = UUID(action['candidate_id']) if action['candidate_id'] else None
        retained = UUID(action['parent_record_id']) if action['parent_record_id'] else None
        checked = assessments[(spec['input_hash'], spec['rule_hash'], spec['scope_hash'])]
        decision = Decision(id=uuid4(), work_id=work.id, assessment_id=checked.id, target_key=action['target_key'],
            candidate_manifest_id=manifest.id, selected_candidate_id=cid, parent_record_id=retained,
            action=action['action'], evidence_json=encode({'reason': action['reason'], 'policy_id': policy.id,
                'comparison': 'not_applicable', 'fallback': 'disabled'}), created_at=now())
        decisions.append(decision)
        if cid:
            candidate, typed = selected.get(cid, (None, None))
            if candidate is None:
                raise FoundationError('CANDIDATE_NOT_READY', '领域候选或类型化内容缺失。')
            validate_body(params['dataset'], json.loads(typed.body_json))
            if (candidate.readiness != 'ready' or candidate.record_subject_id != typed.subject_id
                    or typed.business_key != action['target_key'] or value_hash(typed) != candidate.values_hash):
                raise FoundationError('CANDIDATE_NOT_READY', '领域候选身份、业务键或内容摘要不一致。')
            officials.append(OfficialRecord(id=uuid4(), candidate_id=cid, decision_id=decision.id,
                values_hash=candidate.values_hash, created_at=now(), **fields(typed)))
        units.append(Unit(work_id=work.id, unit_key=action['target_key'], row_count=1,
            result_hash=digest('typed-record-action', action)))
    session.add_all(decisions)
    session.flush()
    session.add_all(officials + units)
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
    by_subject = subject_partitioned(params['dataset'], blocks)
    changed = {}
    # Decisions and retained revisions form a finite set. Read them once rather
    # than issuing one revision query for every action during release sealing.
    new_records = {row.decision_id: row for row in session.scalars(select(OfficialRecord)
        .join(Decision, Decision.id == OfficialRecord.decision_id).where(Decision.work_id == work.id))}
    retained_ids = {d.parent_record_id for d in decisions.values() if d.parent_record_id}
    retained_records = {row.id: row for row in session.scalars(select(OfficialRecord)
        .where(OfficialRecord.id.in_(retained_ids)))} if retained_ids else {}
    from datetime import date
    for action in params['actions']:
        key = action['target_key']
        partition = partition_key(key, action['subject_id'], by_subject)
        if partition not in changed:
            changed[partition] = {m.target_key: fields(m, MEMBER_FIELDS) for m in session.scalars(
                select(RecordBlockMember).where(RecordBlockMember.block_id == blocks[partition]))} if partition in blocks else {}
        decision = decisions[key]
        official = new_records.get(decision.id)
        if decision.action == 'retain':
            official = retained_records.get(decision.parent_record_id)
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
    validate_release(session, release, reuse_verified=True)
    release.status = 'sealed'
    session.flush()
    return release


def validate_release(session, release, *, reuse_verified=False):
    """Validate the full root and all new blocks; optionally reuse closed proofs.

    Explicit audit calls default to reading and checking every member again.
    Publication/reconciliation may reuse a receipt only for the exact immutable
    block, scope, schema and validator. Live issue/head gates remain separate.
    """
    params = verify(session.get(Work, release.work_id))
    validator_hash = validation_hash()
    refs = list(session.scalars(select(BlockRef).where(BlockRef.release_id == release.id).order_by(BlockRef.partition_key)))
    by_subject = subject_partitioned(params['dataset'], [r.partition_key for r in refs])
    block_ids = [r.block_id for r in refs]
    blocks = {b.id: b for b in session.scalars(select(ReleaseBlock).where(ReleaseBlock.id.in_(block_ids)))}
    receipts = {v.block_id: v for v in session.scalars(select(RecordBlockVerification).where(
        RecordBlockVerification.block_id.in_(block_ids), RecordBlockVerification.validator_hash == validator_hash))}
    verified = []
    manifest = []
    for ref in refs:
        block = blocks.get(ref.block_id)
        if block is None or block.partition_key != ref.partition_key:
            raise FoundationError('MANIFEST_INVALID', '领域发布块范围不一致。')
        manifest.append([ref.partition_key, ref.block_id, block.content_hash])
        receipt = receipts.get(block.id)
        expected = dict(scope_key=release.scope_key, schema_key=params['dataset'],
            content_hash=block.content_hash, row_count=block.row_count)
        if receipt and any(getattr(receipt, name) != value for name, value in expected.items()):
            raise FoundationError('MANIFEST_INVALID', '领域发布块校验凭据与固定范围不一致。')
        if reuse_verified and receipt:
            continue
        members = session.scalars(select(RecordBlockMember).where(RecordBlockMember.block_id == block.id)
            .order_by(RecordBlockMember.target_key)).all()
        if block.partition_key != ref.partition_key or len(members) != block.row_count:
            raise FoundationError('MANIFEST_INVALID', '领域发布块范围或数量不一致。')
        if digest('typed-record-block-v1', [fields(m, MEMBER_FIELDS) for m in members]) != block.content_hash:
            raise FoundationError('MANIFEST_INVALID', '领域发布块摘要不一致。')
        # Verify all cross-references for this hash partition with bounded
        # bulk queries. Strong references keep SQLAlchemy's weak identity map
        # from issuing one query per inherited candidate/work/official value.
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
            if (partition_key(member.target_key, member.subject_id, by_subject) != ref.partition_key or decision.target_key != member.target_key
                    or works[decision.work_id].scope_key != release.scope_key
                    or member.state != {'select': 'value', 'retain': 'value', 'block': 'blocked', 'gap': 'gap', 'withdraw': 'withdrawn'}[decision.action]):
                raise FoundationError('MANIFEST_INVALID', '领域成员与决策范围不一致。')
            if member.official_id:
                official = officials.get(member.official_id)
                if (official is None or official.schema_key != params['dataset'] or official.subject_id != member.subject_id
                        or official.business_key != member.target_key or official.business_date != member.business_date
                        or value_hash(official) != official.values_hash):
                    raise FoundationError('MANIFEST_INVALID', '正式领域记录与发布成员不一致。')
                validate_body(params['dataset'], json.loads(official.body_json))
                if decision.action == 'retain':
                    if parent_values.get(decision.id) != official.id or decision.parent_record_id != official.id:
                        raise FoundationError('MANIFEST_INVALID', '保留记录不属于固定父发布。')
                elif official.decision_id != decision.id or official.candidate_id != decision.selected_candidate_id:
                    raise FoundationError('MANIFEST_INVALID', '正式记录与选中候选不一致。')
        # Preserve actual contributors of this exact block, not all works ever
        # mentioned by a parent. A forced audit recomputes these lists as well.
        normalized = set(session.scalars(select(Candidate.work_id).where(
            Candidate.id.in_({o.candidate_id for o in officials.values()})).distinct()))
        contributions = dict(governance_work_ids_json=encode(sorted({str(d.work_id) for d in decisions.values()})),
            normalization_work_ids_json=encode(sorted(str(wid) for wid in normalized)))
        if receipt and any(getattr(receipt, name) != value for name, value in contributions.items()):
            raise FoundationError('MANIFEST_INVALID', '领域发布块贡献来源与校验凭据不一致。')
        if receipt is None:
            verified.append(dict(block_id=block.id, validator_hash=validator_hash, **expected, verified_at=now(),
                **contributions))
    if digest('release-manifest', manifest) != release.manifest_hash:
        raise FoundationError('MANIFEST_INVALID', '领域发布清单摘要不一致。')
    if verified:
        # Persist only after the entire root passes. A transaction rollback also
        # rolls back the proof; concurrent audits can share the same exact key.
        from sqlalchemy.dialects.postgresql import insert
        session.execute(insert(RecordBlockVerification).values(verified).on_conflict_do_nothing(
            index_elements=['block_id', 'validator_hash']))
