"""Typed source-local records on the shared fenced normalization/release kernel."""
from collections import defaultdict
from pathlib import Path
from uuid import UUID, uuid4
import json

from sqlalchemy import select
from sqlalchemy.orm import aliased

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
from app.data_foundation.record_values import (VALUE_FIELDS, MEMBER_FIELDS, fields, value_hash,
                                               subject_partitioned, partition_key)
OLDER_SOURCE_RETAINED = 'OLDER_SOURCE_RETAINED'


def domain_hash():
    return digest('typed-record-code-v1', {name: (Path(__file__).parent / name).read_text()
        for name in ('record_work.py', 'record_adapters.py', 'record_schemas.py', 'record_models.py',
                     'record_bulk.py', 'record_preparation.py', 'performance_models.py', 'record_values.py',
                     'record_validation.py', 'record_validation_driver.py', 'record_validation_models.py', 'manager_experience.py', 'fund_nav.py', 'fund_offerings.py', 'popularity.py', 'fund_quotas.py', 'daily_windows.py', 'quote_snapshots.py', 'market_activity.py', 'dragon_tiger.py', 'fund_performance.py', 'manager_style.py', 'manager_performance.py', 'distributions.py', 'fund_ownership.py', 'financial_windows.py', 'stock_indicators.py', 'portfolio_windows.py', 'holdings.py', 'narrative_windows.py', 'staged_inputs.py', 'import_progress.py', 'scope_settlement.py', 'table_updates.py', 'local_table_contracts.py', 'table_bootstrap.py')})


def validation_hash():
    from app.data_foundation.record_validation import validation_hash as block_hash
    return block_hash()


def plan_validation_hash():
    # Include shared canonical helpers and the dependency runtime, not just the
    # adapter, so a changed validator cannot inherit earlier proof results.
    from app.data_foundation.execution import installed_code_hash
    import hashlib
    import platform
    return digest('typed-record-validator-v1', [installed_code_hash(), platform.python_version(),
        hashlib.sha256((Path(__file__).resolve().parents[2] / 'uv.lock').read_bytes()).hexdigest()])


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
    from app.data_foundation.record_preparation import candidate_page
    work.total, page = candidate_page(session, work, epoch)
    end = work.cursor + len(page)
    from app.data_foundation.record_bulk import register_subjects, register_assessments
    subjects = register_subjects(session, source, [item['value'] for item in page])
    prepared = []
    for offset, item in enumerate(page):
        index = work.cursor + offset
        value, key, reason = item['value'], item['key'], item['reason']
        subject = subjects[(source.source, value['subject_kind'], value['subject_key'])] if value else None
        typed = dict(subject_id=subject.id, business_key=key, business_date=value['business_date'],
            schema_key=params['dataset'], body_json=encode(value['body']),
            field_quality_json=encode(value['field_quality'])) if value else None
        spec = dict(input_hash=item['input_hash'],
            rule_hash=params['domain_hash'], scope_hash=work.scope_key, status='fail' if reason else 'pass',
            results={'reason': reason, 'reasons': [reason] if reason else [], 'target_key': key, 'occurrence': index,
                'field_quality': value['field_quality'] if value else {}})
        prepared.append((index, subject, typed, reason, spec, item['quarantine_hash']))
    assessments = register_assessments(session, [entry[4] for entry in prepared])
    candidates, typed_rows, units = [], [], []
    for index, subject, typed, reason, spec, quarantine_hash in prepared:
        checked = assessments[(spec['input_hash'], spec['rule_hash'], spec['scope_hash'])]
        candidate = Candidate(id=uuid4(), work_id=work.id, source_ref_id=source.id,
            record_subject_id=subject.id if subject else None, binding_id=None,
            dependency_id=work.dependency_id, unit_key=str(index), occurrence=index,
            values_hash=digest('typed-record-value-v1', typed) if typed else quarantine_hash,
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
    fenced(session, work.id, epoch)
    finish_batch(session, work, status='succeeded' if end == work.total else 'queued')
    return end


def parent_member(session, release_id, key):
    if release_id is None:
        return None
    return session.scalar(select(RecordBlockMember).join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
        .where(BlockRef.release_id == release_id, RecordBlockMember.target_key == key))


def effective_parent_sources(session, parents):
    """Resolve the observation that last justified each current key state.

    A retained official value may originate in an older observation. New
    decisions carry the selected observation explicitly. For immutable releases
    created before that receipt existed, follow only older-source retain edges
    through the parent releases; an unchanged-value retain confirms its own
    input observation and ends the walk.
    """
    partitions = {key: partition for key, (_, _, partition) in parents.items()}
    pending = {key: member.decision_id for key, (member, _, _) in parents.items()}
    source_ids, visited = {}, set()
    governance, origin = aliased(Work), aliased(Work)
    while pending:
        decisions = {decision.id: (decision, work.parent_release_id, source.id)
            for decision, work, source in session.execute(select(Decision, governance, SourceRef)
                .join(governance, governance.id == Decision.work_id)
                .join(CandidateManifest, CandidateManifest.id == governance.candidate_manifest_id)
                .join(origin, origin.id == CandidateManifest.work_id)
                .join(SourceRef, SourceRef.id == origin.source_ref_id)
                .where(Decision.id.in_(pending.values())))}
        inherited = {}
        for key, decision_id in pending.items():
            if (key, decision_id) in visited or decision_id not in decisions:
                raise FoundationError('SOURCE_ORDER_EVIDENCE_MISSING',
                    '正式键的固定来源先后依据缺失或形成循环。')
            visited.add((key, decision_id))
            decision, parent_release_id, input_source_id = decisions[decision_id]
            evidence = json.loads(decision.evidence_json)
            selected_id = evidence.get('effective_source_ref_id')
            if selected_id is not None:
                try:
                    source_ids[key] = UUID(selected_id)
                except (TypeError, ValueError) as exc:
                    raise FoundationError('SOURCE_ORDER_EVIDENCE_MISSING',
                        '正式键的固定来源先后依据无效。') from exc
            elif evidence.get('reason') == OLDER_SOURCE_RETAINED:
                if parent_release_id is None:
                    raise FoundationError('SOURCE_ORDER_EVIDENCE_MISSING',
                        '较旧来源保留决策缺少父发布。')
                inherited[key] = (parent_release_id, partitions[key])
            else:
                source_ids[key] = input_source_id
        if not inherited:
            break
        prior = {(ref.release_id, ref.partition_key, member.target_key): member.decision_id
            for ref, member in session.execute(select(BlockRef, RecordBlockMember)
                .join(RecordBlockMember, RecordBlockMember.block_id == BlockRef.block_id)
                .where(BlockRef.release_id.in_({release for release, _ in inherited.values()}),
                    BlockRef.partition_key.in_({partition for _, partition in inherited.values()}),
                    RecordBlockMember.target_key.in_(inherited)))}
        pending = {}
        for key, (release_id, partition) in inherited.items():
            decision_id = prior.get((release_id, partition, key))
            if decision_id is None:
                raise FoundationError('SOURCE_ORDER_EVIDENCE_MISSING',
                    '较旧来源保留决策的父正式键缺失。')
            pending[key] = decision_id
    sources = {source.id: source for source in session.scalars(select(SourceRef)
        .where(SourceRef.id.in_(source_ids.values())))}
    if len(sources) != len(set(source_ids.values())):
        raise FoundationError('SOURCE_ORDER_EVIDENCE_MISSING', '正式键的固定来源凭据缺失。')
    return {key: sources[source_id] for key, source_id in source_ids.items()}


def plan_actions(session, manifest_id, policy_id, parent_release_id=None, *, preserve_head=False,
                 source_revision=None):
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
        parents = {m.target_key: (m, o, partition) for m, o, partition in session.execute(
            select(RecordBlockMember, OfficialRecord, BlockRef.partition_key)
            .outerjoin(OfficialRecord, OfficialRecord.id == RecordBlockMember.official_id)
            .join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
            .where(BlockRef.release_id == parent_release_id, RecordBlockMember.target_key.in_(groups),
                RecordBlockMember.subject_id.in_(subject_ids)))}
    parent_sources = effective_parent_sources(session, parents) if (parents and
        not preserve_head and source_revision is None) else {}
    actions = []
    for key, group in sorted(groups.items()):
        parent, previous, _ = parents.get(key, (None, None, None))
        previous_source = parent_sources.get(key)
        source = group[0][2]
        same_native_evidence = (previous_source is not None and
            ((source.observation_id is not None and source.observation_id == previous_source.observation_id)
             or (source.baseline_id is not None and source.baseline_id == previous_source.baseline_id)))
        if (not preserve_head and source_revision is None
                and parent is not None and previous_source is not None
                and source.id != previous_source.id and not same_native_evidence
                and (source.source, source.dataset, source.subject, source.variant) ==
                    (previous_source.source, previous_source.dataset,
                     previous_source.subject, previous_source.variant)):
            # Compare immutable observation times only within one source-local
            # lineage. A requeued older backfill may merge new keys, but it
            # must never replace a newer value for an overlapping key.
            if source.observed_at == previous_source.observed_at:
                raise FoundationError('SOURCE_ORDER_AMBIGUOUS', '相同来源的固定观察时间相同，无法安全确定正式值先后。')
            if source.observed_at < previous_source.observed_at:
                if previous is None:
                    raise FoundationError('SOURCE_OLDER_THAN_PARENT_NONVALUE',
                        '较旧固定来源不能覆盖较新观察的非值状态，须单独保留历史发布。')
                typed = group[0][1]
                actions.append(dict(target_key=key, subject_id=str(typed.subject_id),
                    business_date=str(typed.business_date) if typed.business_date else None,
                    action='retain', candidate_id=None, parent_record_id=str(previous.id),
                    reason=OLDER_SOURCE_RETAINED, effective_source_ref_id=str(previous_source.id)))
                continue
        removals = [json.loads(typed.field_quality_json).get('source_deleted') == 'FIXED_LOCAL_ROW_REMOVAL'
                    for _, typed, _ in group]
        if any(removals):
            if len(group) != 1 or not all(removals) or group[0][0].readiness != 'ready':
                raise FoundationError('SOURCE_CONTEXT_CHANGED', '删除候选与同一业务键的有效值冲突。')
            typed = group[0][1]
            actions.append(dict(target_key=key, subject_id=str(typed.subject_id),
                business_date=str(typed.business_date) if typed.business_date else None,
                action='withdraw', candidate_id=None, parent_record_id=None,
                reason='FIXED_LOCAL_ROW_REMOVAL', effective_source_ref_id=str(source.id)))
            continue
        action, cid, reason = choose([{'id': str(c.id), 'source': s.source, 'series': params['series'],
            'ready': c.readiness == 'ready'} for c, _, s in group], definition)
        retained = None
        if action == 'select' and parent and parent.state == 'value':
            selected = next(c for c, _, _ in group if str(c.id) == cid)
            if previous.values_hash == selected.values_hash:
                action, cid, retained, reason = 'retain', None, str(previous.id), 'UNCHANGED_CANONICAL_RECORD'
        typed = group[0][1]
        actions.append(dict(target_key=key, subject_id=str(typed.subject_id),
            business_date=str(typed.business_date) if typed.business_date else None,
            action=action, candidate_id=cid, parent_record_id=retained, reason=reason,
            effective_source_ref_id=str(source.id)))
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
    actions = plan_actions(session, manifest.id, policy_id, parent_release_id,
                           preserve_head=preserve_head, source_revision=source_revision)
    mode = {'head_mode': 'preserve'} if preserve_head else {}
    if source_revision is not None:
        if preserve_head or type(source_revision) is not int or source_revision < 1:
            raise ValueError('Invalid current source revision')
        fixed_source = session.get(SourceRef, origin.source_ref_id)
        if fixed_source.representation == 'local_table_baseline':
            from app.data_foundation.table_updates import is_current_change
            if not is_current_change(session, fixed_source, source_revision):
                raise FoundationError('SOURCE_CONTEXT_CHANGED', '固定本地表变化已非当前来源。')
        elif fixed_source.representation != 'ths_observation':
            raise FoundationError('SCOPE_MISMATCH', '当前来源指针没有支持的固定来源类型。')
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
    _save_plan_verification(session, work, plan_validation_hash(), len(actions))
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
    validator = plan_validation_hash()
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
    actions = plan_actions(session, manifest.id, work.policy_id, work.parent_release_id,
                           preserve_head=params.get('head_mode') == 'preserve',
                           source_revision=params.get('source_revision'))
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
                'comparison': 'not_applicable', 'fallback': 'disabled',
                'effective_source_ref_id': action['effective_source_ref_id']}), created_at=now())
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
    # Carry inherited block hashes with their references. Looking up every
    # inherited block separately turns a one-record publication into hundreds
    # of database round trips once a scope has accumulated many partitions.
    blocks, block_hashes = {}, {}
    for partition, block_id, content_hash in session.execute(select(
            BlockRef.partition_key, BlockRef.block_id, ReleaseBlock.content_hash)
            .outerjoin(ReleaseBlock, ReleaseBlock.id == BlockRef.block_id)
            .where(BlockRef.release_id == work.parent_release_id)):
        if content_hash is None:
            raise FoundationError('MANIFEST_INVALID', '父发布块缺失。')
        blocks[partition], block_hashes[block_id] = block_id, content_hash
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
            state={'select': 'value', 'retain': 'value', 'block': 'blocked', 'gap': 'gap',
                   'withdraw': 'withdrawn'}[decision.action],
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
        block_hashes[block.id] = block.content_hash
    manifest = [[key, bid, block_hashes[bid]] for key, bid in sorted(blocks.items())]
    release = Release(work_id=work.id, scope_key=work.scope_key, parent_id=work.parent_release_id,
        manifest_hash=digest('release-manifest', manifest), status='draft', created_at=now())
    session.add(release)
    session.flush()
    session.add_all([BlockRef(release_id=release.id, partition_key=key, block_id=bid) for key, bid in blocks.items()])
    session.flush()
    if session.info.get('foundation_defer_record_validation'):
        from app.data_foundation.record_validation_driver import freeze_root
        freeze_root(session, release)
        # The worker will validate this closed draft in independent bounded
        # transactions. A draft is never reconciled or published as sealed.
        return release
    validate_release(session, release, reuse_verified=True)
    release.status = 'sealed'
    session.flush()
    return release


def validate_release(session, release, *, reuse_verified=False):
    """Explicit audits still reread every member; production may reuse proofs."""
    from app.data_foundation.record_validation import validate_release as validate
    params = verify(session.get(Work, release.work_id))
    return validate(session, release, params['dataset'], validation_hash(),
                    reuse_verified=reuse_verified, body_validator=validate_body)
