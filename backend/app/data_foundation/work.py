"""Fenced work leases and atomic unit commits; callers own transaction scopes."""
from datetime import timedelta
import json
from uuid import UUID
from sqlalchemy import select, func, or_
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.models import Definition, Execution, DependencyManifest, DependencyEntry, SourceRef
from app.data_foundation.work_models import Work, Attempt, WorkEvent, IssueScope, Unit, CandidateManifest, Release

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15
BATCH_ROWS = 200
BUDGET_SECONDS = 30


def scope_key(dataset, major, profile, series):
    if major < 1 or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in (dataset, profile, series)):
        raise ValueError('Invalid dataset scope')
    return digest('scope', [dataset, major, profile, series])


def create_work(session, *, kind, contract_id, execution_id, dependency_id, parameters,
                source_ref_id=None, candidate_manifest_id=None, parent_release_id=None, policy_id=None,
                expected_head_revision=0, expected_issue_epoch=0, total=None):
    required = {'dataset', 'major', 'profile', 'series', 'start', 'end', 'domain', 'domain_hash'}
    if set(parameters) != (required | ({'actions'} if kind == 'B' else set())) or kind not in ('A', 'B'):
        raise ValueError('Invalid fixed work parameters')
    contract = session.get(Definition, contract_id)
    execution = session.get(Execution, execution_id)
    dependency = session.get(DependencyManifest, dependency_id)
    if not contract or contract.kind != 'contract' or not execution or not dependency:
        raise FoundationError('DEPENDENCY_MISSING', '工作固定依赖不完整。')
    if contract.name != parameters['dataset'] or int(contract.version.split('.')[0]) != parameters['major']:
        raise FoundationError('CONTRACT_MISMATCH', '工作契约与数据范围不匹配。')
    if parameters['start'] > parameters['end']:
        raise ValueError('Invalid work range')
    if kind == 'A' and (not source_ref_id or candidate_manifest_id or policy_id):
        raise ValueError('Normalization requires a fixed source only')
    if kind == 'B' and (source_ref_id or not candidate_manifest_id or not policy_id):
        raise ValueError('Governance requires a candidate manifest and policy')
    scope = scope_key(parameters['dataset'], parameters['major'], parameters['profile'], parameters['series'])
    source = session.get(SourceRef, source_ref_id) if source_ref_id else None
    manifest = session.get(CandidateManifest, candidate_manifest_id) if candidate_manifest_id else None
    policy = session.get(Definition, policy_id) if policy_id else None
    if kind == 'A' and source is None or kind == 'B' and (manifest is None or policy is None or policy.kind != 'policy'):
        raise FoundationError('DEPENDENCY_MISSING', '工作输入或政策不存在。')
    if kind == 'B':
        actions = parameters['actions']
        if not isinstance(actions, list) or len({a['target_key'] for a in actions}) != len(actions):
            raise ValueError('Governance actions must have unique fixed targets')
        total = len(actions)
        origin = session.get(Work, manifest.work_id)
        if origin.status != 'succeeded' or origin.scope_key != scope or origin.contract_id != contract_id:
            raise FoundationError('CANDIDATE_NOT_SEALED', '候选工作未完整封存或契约范围不符。')
        policy_value = json.loads(policy.definition_json)
        if any(policy_value[k] != parameters[k] for k in ('dataset', 'major', 'profile', 'series')):
            raise FoundationError('POLICY_MISMATCH', '治理政策与目标数据范围不符。')
    parent = session.get(Release, parent_release_id) if parent_release_id else None
    if parent_release_id and (not parent or parent.status != 'published' or parent.scope_key != scope):
        raise FoundationError('PARENT_INVALID', '固定父发布不存在或范围不符。')
    inputs = dict(kind=kind, contract=contract.content_hash, execution=execution.manifest_hash,
        dependencies=dependency.manifest_hash, parameters=parameters, source_ref_id=source_ref_id,
        candidate_manifest_id=candidate_manifest_id, candidate_hash=manifest.manifest_hash if manifest else None,
        parent=parent_release_id, policy=policy.content_hash if policy else None,
        head_revision=expected_head_revision, issue_epoch=expected_issue_epoch)
    fingerprint = digest('work', inputs)
    lock_key(session, 'work', fingerprint)
    existing = session.scalar(select(Work).where(Work.fingerprint == fingerprint))
    if existing:
        return existing
    lock_key(session, 'issue-scope-create', scope)
    if session.get(IssueScope, scope) is None:
        session.add(IssueScope(scope_key=scope, epoch=0))
    row = Work(fingerprint=fingerprint, kind=kind, contract_id=contract_id, execution_id=execution_id,
        dependency_id=dependency_id, parameters_json=encode(parameters), scope_key=scope,
        source_ref_id=source_ref_id, candidate_manifest_id=candidate_manifest_id, parent_release_id=parent_release_id,
        policy_id=policy_id, expected_head_revision=expected_head_revision, expected_issue_epoch=expected_issue_epoch,
        total=total, created_at=now())
    session.add(row); session.flush()
    append_event(session, row, 'input', 'fixed', {'input_hash': fingerprint})
    return row


def append_event(session, work, step, status, details):
    sequence = (session.scalar(select(func.max(WorkEvent.sequence)).where(WorkEvent.work_id == work.id)) or 0) + 1
    params = json.loads(work.parameters_json)
    labels = {'input': '固定输入', 'normalization': '标准化', 'quality': '质量检查', 'governance': '治理', 'publication': '发布'}
    state_label = {'fixed': '输入已固定', 'evaluated': '已评估', 'queued': '等待续作', 'succeeded': '成功', 'failed': '失败', 'cancelled': '已取消', 'dependency_missing': '依赖缺失', 'superseded': '父发布已更新', 'published': '已发布'}.get(status, '处理中')
    previous = session.scalar(select(WorkEvent).where(WorkEvent.work_id == work.id).order_by(WorkEvent.sequence.desc()).limit(1))
    previous_cursor = json.loads(previous.details_json).get('checkpoint', 0) if previous else 0
    checkpoint = '已推进' if work.cursor > previous_cursor else '未推进'
    counts = f"，合格{details['ready']}行、隔离{details.get('quarantined', 0)}行" if 'ready' in details else ''
    message = f"日线底座 {params['start']} 至 {params['end']} 的{labels[step]}结果为{state_label}，已提交{work.cursor}行{counts}，检查点{checkpoint}，当前位于{work.cursor}。"
    details = {**details, 'checkpoint': work.cursor, 'checkpoint_advanced': work.cursor > previous_cursor}
    session.add(WorkEvent(work_id=work.id, sequence=sequence, step=step, status=status, message=message,
        details_json=encode(details), created_at=now()))


def database_now(session):
    return session.scalar(select(func.clock_timestamp()))


def claim(session, preferred_kind='A', work_id=None):
    """SKIP LOCKED provides per-work exclusion; the worker singleton provides
    the approved global limit of one foundation unit independently of ingestion.
    """
    at = database_now(session)
    for kind in (preferred_kind, 'B' if preferred_kind == 'A' else 'A'):
        row = session.scalar(select(Work).where(Work.kind == kind, Work.cancelled.is_(False),
            Work.id == work_id if work_id else True,
            or_(Work.status == 'queued', (Work.status == 'running') & (Work.lease_until < at)))
            .order_by(Work.created_at, Work.id).with_for_update(skip_locked=True).execution_options(populate_existing=True).limit(1))
        if row is None:
            continue
        if row.status == 'running':
            session.add(Attempt(work_id=row.id, epoch=row.lease_epoch, outcome='lease_expired', created_at=at))
        row.lease_epoch += 1
        row.status = 'running'
        row.lease_until = at + timedelta(seconds=LEASE_SECONDS)
        session.add(Attempt(work_id=row.id, epoch=row.lease_epoch, outcome='started', created_at=at))
        session.flush()
        return row
    return None


def fenced(session, work_id, epoch):
    row = session.scalar(select(Work).where(Work.id == work_id).with_for_update().execution_options(populate_existing=True))
    if row is None or row.status != 'running' or row.lease_epoch != epoch or row.cancelled or row.lease_until <= database_now(session):
        raise FoundationError('LEASE_LOST', '工作租约失效或已经取消，未提交结果。')
    return row


def heartbeat(session, work_id, epoch):
    row = fenced(session, work_id, epoch)
    row.lease_until = database_now(session) + timedelta(seconds=LEASE_SECONDS)


def finish_batch(session, row, *, status, error_code=None, error_details=None):
    if status not in ('queued', 'succeeded', 'failed', 'cancelled', 'dependency_missing', 'superseded'):
        raise ValueError('Invalid work transition')
    row.status = status
    row.lease_until = None
    session.add(Attempt(work_id=row.id, epoch=row.lease_epoch, outcome=status, error_code=error_code, details_json=encode(error_details or {}), created_at=now()))
    append_event(session, row, 'normalization' if row.kind == 'A' else 'governance', status,
        {'error_code': error_code, 'committed_rows': row.cursor, 'total_rows': row.total})
    session.flush()


def retry(session, work_id):
    row = session.scalar(select(Work).where(Work.id == work_id).with_for_update().execution_options(populate_existing=True))
    if not row or row.status not in ('failed', 'dependency_missing', 'cancelled'):
        raise FoundationError('RETRY_NOT_ALLOWED', '该工作不能在原输入上重试。')
    row.status = 'queued'
    row.cancelled = False
    return row


def cancel(session, work_id):
    row = session.scalar(select(Work).where(Work.id == work_id).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise FoundationError('WORK_UNAVAILABLE', '底座工作不存在。')
    if row.status in ('succeeded', 'superseded', 'cancelled'):
        return row
    row.cancelled = True
    finish_batch(session, row, status='cancelled')
    return row
