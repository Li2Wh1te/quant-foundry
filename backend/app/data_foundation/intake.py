"""Local-only intake with finite, restartable full reconciliation passes.

Callers own transactions. Each page seals source references and dependencies
before advancing its checkpoint; a failed page rolls back all of that page.
No supplier client is imported or called by this module.
"""
import json
import time
from datetime import date, timedelta, timezone
from sqlalchemy import select, or_, insert, literal, func
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key, now
from app.data_ingestion.tonghuashun.contracts import CollectionError, DATASETS
from app.data_foundation.models import Execution
from app.data_foundation.source_refs import register_observation
from app.data_foundation.intake_models import IntakeScope, IntakeControl, Scan, IntakeItem, ScanEntry, SourcePointer, ScanTarget, ScanFailure, ScanSeal
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation, TonghuashunCollectionState as State


def register_scope(session, *, dataset, subject, variant, selector, decoder_id):
    # Intake is evidence registration, not a claim that a domain reader exists.
    # Full-local authorization must include new source keys rather than silently
    # dropping them because their normalization adapter has not been installed.
    if selector != {'scope': 'all_local'}:
        if set(selector) != {'start', 'end'}:
            raise ValueError('An explicit interval or all_local selector is required')
        start, end = (date.fromisoformat(selector[key]) for key in ('start', 'end'))
        if start > end:
            raise ValueError('Invalid intake interval')
    if not all(isinstance(v, str) and v.strip() for v in (dataset, subject, variant)):
        raise ValueError('Invalid intake scope')
    if len(dataset) > 80 or len(subject) > 64 or len(variant) > 80:
        raise ValueError('Intake scope is too long')
    if session.get(Execution, decoder_id) is None:
        raise FoundationError('DEPENDENCY_MISSING', '来源解码依赖尚未登记。')
    fingerprint = digest('intake-scope-v1', dict(dataset=dataset, subject=subject, variant=variant,
        selector=selector, decoder_id=decoder_id))
    lock_key(session, 'intake-scope', fingerprint)
    row = session.scalar(select(IntakeScope).where(IntakeScope.fingerprint == fingerprint))
    if row is None:
        row = IntakeScope(fingerprint=fingerprint, dataset=dataset, subject=subject, variant=variant,
            selector_json=encode(selector), decoder_id=decoder_id, created_at=now())
        session.add(row); session.flush()
        session.add(IntakeControl(scope_id=row.id, paused=True)); session.flush()
    return row


def set_paused(session, scope_id, paused):
    lock_key(session, 'intake-run', str(scope_id))
    control = session.get(IntakeControl, scope_id)
    if control is None:
        raise FoundationError('SOURCE_UNAVAILABLE', '来源纳入范围不存在。')
    control.paused = bool(paused)
    session.flush()


def scope_conditions(scope):
    """Explicit full-local scopes may cover every actual subject and variant."""
    all_local = json.loads(scope.selector_json) == {'scope': 'all_local'}
    conditions = [Observation.dataset == scope.dataset]
    for name in ('subject', 'variant'):
        value = getattr(scope, name)
        if not (all_local and value == '*'):
            conditions.append(getattr(Observation, name) == value)
    return conditions


def start_scan(session, scope_id, event_key, *, mode='full'):
    if mode not in ('full','recent','frozen'):raise ValueError('Invalid scan mode')
    if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 128:
        raise ValueError('A finite scan event key is required')
    lock_key(session, 'intake-run', str(scope_id))
    scope = session.get(IntakeScope, scope_id)
    if scope is None:
        raise FoundationError('SOURCE_UNAVAILABLE', '来源纳入范围不存在。')
    if json.loads(scope.selector_json) == {'scope': 'all_local'} and mode != 'frozen':
        raise ValueError('Full-local intake requires a frozen version manifest')
    existing = session.scalar(select(Scan).where(Scan.scope_id == scope_id, Scan.event_key == event_key))
    if existing:
        if existing.mode != mode:raise FoundationError('INPUT_CHANGED','同一核对周期不能更换扫描模式。')
        return existing
    active = session.scalar(select(Scan).where(Scan.scope_id == scope_id, Scan.status != 'completed'))
    if active:
        raise FoundationError('WORK_CONFLICT', '该来源范围已有未完成核对周期，请先恢复原周期。')
    row = Scan(scope_id=scope_id, event_key=event_key, mode=mode, created_at=now())
    session.add(row); session.flush()
    if mode == 'frozen':
        # INSERT SELECT captures exact committed membership in one statement,
        # with no payload download and no assumption that UUIDs order commits.
        # Retry of the event returns above and cannot expand this input set.
        session.execute(insert(ScanTarget).from_select(
            ['scan_id', 'observation_id', 'content_hash'],
            select(literal(row.id, type_=ScanTarget.scan_id.type), Observation.id, Observation.content_hash)
            .where(*scope_conditions(scope))))
        count = session.scalar(select(func.count()).select_from(ScanTarget).where(ScanTarget.scan_id == row.id))
        session.add(ScanSeal(scan_id=row.id, target_count=count))
        session.flush()
    return row


def scan_page(session, scan_id, *, limit=200, budget_seconds=30):
    if not 1 <= limit <= 200 or not 0 < budget_seconds <= 30:
        raise ValueError('Intake requires a bounded page and time budget')
    scan = session.get(Scan, scan_id)
    if scan is None:
        raise FoundationError('SOURCE_UNAVAILABLE', '来源核对周期不存在。')
    lock_key(session, 'intake-run', str(scan.scope_id))
    session.refresh(scan)
    scope = session.get(IntakeScope, scan.scope_id)
    control = session.get(IntakeControl, scope.id)
    session.refresh(control)
    if scan.status == 'completed' or control.paused:
        return scan_summary(session, scope, scan, paused=control.paused)
    if scan.mode == 'frozen':
        return frozen_page(session, scope, scan, limit=limit, budget_seconds=budget_seconds)
    deadline = time.monotonic() + budget_seconds
    try:
        # A savepoint keeps the failure outcome durable while rolling back both
        # evidence registration and the page cursor. Retry starts at that cursor.
        with session.begin_nested():
            conditions = scope_conditions(scope)
            if scan.mode == 'recent':
                state=session.get(State,(scope.dataset,scope.subject,scope.variant),populate_existing=True)
                conditions.append(or_(Observation.observed_at >= scan.created_at.replace(tzinfo=timezone.utc)-timedelta(hours=24),
                    Observation.id==state.observation_id if state else False))
            if scan.cursor_id is not None:
                conditions.append(Observation.id > scan.cursor_id)
            rows = session.scalars(select(Observation).where(*conditions).order_by(Observation.id).limit(limit)).all()
            processed = 0
            for observation in rows:
                ref = register_observation(session, observation.id, scope.decoder_id)
                item = session.scalar(select(IntakeItem).where(IntakeItem.scope_id == scope.id,
                    IntakeItem.observation_id == observation.id))
                if item is None:
                    item = IntakeItem(scope_id=scope.id, observation_id=observation.id,
                        source_ref_id=ref.id, first_scan_id=scan.id, created_at=now())
                    session.add(item); session.flush()
                    scan.registered += 1
                session.add(ScanEntry(scan_id=scan.id, item_id=item.id))
                scan.seen += 1
                scan.cursor_id = observation.id
                processed += 1
                if time.monotonic() >= deadline:
                    break
            state = session.get(State, (scope.dataset, scope.subject, scope.variant), populate_existing=True)
            if state and not session.scalar(select(SourcePointer.id).where(SourcePointer.scan_id == scan.id,
                    SourcePointer.revision == state.revision)):
                session.add(SourcePointer(scope_id=scope.id, scan_id=scan.id, revision=state.revision,
                    observation_id=state.observation_id, source_status=state.status, created_at=now()))
            session.flush()
            # Only a short exhausted page completes a pass. A late smaller ID is
            # intentionally found by the next pass, which starts without a cursor.
            if processed == len(rows) and len(rows) < limit:
                scan.status = 'completed'
                scan.completed_at = now()
            else:
                scan.status = 'running'
            scan.error_code = None
            session.flush()
    except (FoundationError, CollectionError) as exc:
        session.refresh(scan)
        scan.status, scan.error_code = 'failed', getattr(exc, 'code', 'SOURCE_INVALID')
        session.flush()
    return summary(scope, scan)


def frozen_page(session, scope, scan, *, limit, budget_seconds):
    """Isolate a corrupt version without blocking unrelated complete objects.

    The outer caller commits the page, its outcomes, and its cursor together.
    Each failed object remains linked to this frozen input and is retried by the
    next pass. Infrastructure/SQL errors are not data failures and propagate,
    rolling back the entire page instead of falsely advancing the checkpoint.
    """
    seal = session.get(ScanSeal, scan.id)
    if seal is None:
        raise FoundationError('SOURCE_INVALID', '全量扫描输入清单尚未封存，未推进检查点。')
    deadline = time.monotonic() + budget_seconds
    query = select(ScanTarget).where(ScanTarget.scan_id == scan.id)
    if scan.cursor_id is not None:
        query = query.where(ScanTarget.observation_id > scan.cursor_id)
    targets = session.scalars(query.order_by(ScanTarget.observation_id).limit(limit)).all()
    processed = 0
    for target in targets:
        registered = False
        try:
            with session.begin_nested():
                observation = session.get(Observation, target.observation_id)
                if (observation is None or observation.content_hash != target.content_hash
                        or observation.dataset != scope.dataset
                        or scope.subject != '*' and observation.subject != scope.subject
                        or scope.variant != '*' and observation.variant != scope.variant):
                    raise FoundationError('SOURCE_MUTATED', '固定来源版本内容或身份发生变化，已隔离。')
                item = session.scalar(select(IntakeItem).where(IntakeItem.scope_id == scope.id,
                    IntakeItem.observation_id == target.observation_id))
                if item is None:
                    ref = register_observation(session, target.observation_id, scope.decoder_id)
                    item = IntakeItem(scope_id=scope.id, observation_id=target.observation_id,
                        source_ref_id=ref.id, first_scan_id=scan.id, created_at=now())
                    session.add(item)
                    session.flush()
                    registered = True
                session.add(ScanEntry(scan_id=scan.id, item_id=item.id))
                session.flush()
        except (FoundationError, CollectionError) as exc:
            session.add(ScanFailure(scan_id=scan.id, observation_id=target.observation_id,
                error_code=getattr(exc, 'code', 'SOURCE_INVALID')))
            registered = False
        scan.seen += 1
        scan.registered += int(registered)
        scan.cursor_id = target.observation_id
        processed += 1
        if time.monotonic() >= deadline:
            break
    if processed == len(targets) and len(targets) < limit:
        if scan.seen != seal.target_count:
            raise FoundationError('SOURCE_MUTATED', '固定输入清单数量不一致，未完成扫描。')
        scan.status = 'completed'
        scan.completed_at = now()
    session.flush()
    return scan_summary(session, scope, scan)


def scan_summary(session, scope, scan, *, paused=False):
    result = summary(scope, scan, paused=paused)
    if scan.mode != 'frozen':
        return result
    seal = session.get(ScanSeal, scan.id)
    total = seal.target_count if seal else None
    failed = session.scalar(select(func.count()).select_from(ScanFailure).where(ScanFailure.scan_id == scan.id))
    result.update(dataset=scope.dataset, fixed_versions=total, failed=failed, reused=scan.seen-scan.registered-failed,
                  traversal_complete=scan.status == 'completed',
                  registration_complete=scan.status == 'completed' and failed == 0)
    label = DATASETS[scope.dataset].name if scope.dataset in DATASETS else '同花顺未识别类型的本地数据'
    result['message'] = (f'{label}（本地完整历史范围）来源版本纳入：固定{total}个，'
        f'已遍历{scan.seen}个、新登记{scan.registered}个、复用{result["reused"]}个、失败隔离{failed}个；'
        f'检查点已推进至累计{scan.seen}个版本，来源纳入不等于业务正式发布。')
    return result


def summary(scope, scan, *, paused=False):
    selector = json.loads(scope.selector_json)
    state = '已暂停' if paused and scan.status != 'completed' else {
        'running': '进行中', 'failed': '失败，检查点未越过失败页', 'completed': '已完成'}[scan.status]
    label = DATASETS[scope.dataset].name if scope.dataset in DATASETS else '同花顺未识别类型的本地数据'
    interval = '本地完整历史范围' if selector == {'scope': 'all_local'} else f'{selector["start"]}至{selector["end"]}'
    return dict(scan_id=str(scan.id), scope_id=str(scope.id), status=scan.status, mode=scan.mode, paused=paused,
        seen=scan.seen, registered=scan.registered, error_code=scan.error_code,
        message=f'{label}（{scope.subject}，{interval}）来源核对{state}，'
            f'累计核对{scan.seen}个版本、新登记{scan.registered}个、复用{scan.seen - scan.registered}个；'
            f'检查点已固定至累计{scan.seen}个版本。')


def next_scan(session,scope_id,event_key):
    """Resume unfinished work; otherwise fast-pass with a daily full safety net."""
    lock_key(session,'intake-run',str(scope_id))
    active=session.scalar(select(Scan).where(Scan.scope_id==scope_id,Scan.status!='completed'))
    if active:return active
    scope = session.get(IntakeScope, scope_id)
    if scope and json.loads(scope.selector_json) == {'scope': 'all_local'}:
        # Reconcile every committed local version on each finite pass. This is
        # intentionally not an observed_at watermark: late backdated commits
        # and repaired old inputs must remain discoverable.
        return start_scan(session,scope_id,event_key,mode='frozen')
    full=session.scalar(select(Scan).where(Scan.scope_id==scope_id,Scan.mode=='full',Scan.status=='completed')
        .order_by(Scan.completed_at.desc()).limit(1))
    mode='full' if full is None or now()-full.completed_at.replace(tzinfo=timezone.utc)>=timedelta(hours=24) else 'recent'
    return start_scan(session,scope_id,event_key,mode=mode)
