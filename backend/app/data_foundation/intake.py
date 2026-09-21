"""Local-only intake with finite, restartable full reconciliation passes.

Callers own transactions. Each page seals source references and dependencies
before advancing its checkpoint; a failed page rolls back all of that page.
No supplier client is imported or called by this module.
"""
import json
import time
from datetime import date, timedelta, timezone
from sqlalchemy import select, or_
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key, now
from app.data_ingestion.tonghuashun.contracts import CollectionError
from app.data_foundation.models import Execution
from app.data_foundation.source_refs import register_observation
from app.data_foundation.intake_models import IntakeScope, IntakeControl, Scan, IntakeItem, ScanEntry, SourcePointer
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation, TonghuashunCollectionState as State


def register_scope(session, *, dataset, subject, variant, selector, decoder_id):
    # An explicit business interval prevents an observed full-history container
    # from silently expanding the approved downstream processing denominator.
    if dataset not in ('etf_daily','fund_stock_history'):
        raise ValueError('This intake contract supports ETF daily and fund holdings sources only')
    if set(selector) != {'start', 'end'}:
        raise ValueError('An explicit start/end business selector is required')
    start, end = (date.fromisoformat(selector[key]) for key in ('start', 'end'))
    if start > end or not all(isinstance(v, str) and v.strip() for v in (dataset, subject, variant)):
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


def start_scan(session, scope_id, event_key, *, mode='full'):
    if mode not in ('full','recent'):raise ValueError('Invalid scan mode')
    if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 128:
        raise ValueError('A finite scan event key is required')
    lock_key(session, 'intake-run', str(scope_id))
    if session.get(IntakeScope, scope_id) is None:
        raise FoundationError('SOURCE_UNAVAILABLE', '来源纳入范围不存在。')
    existing = session.scalar(select(Scan).where(Scan.scope_id == scope_id, Scan.event_key == event_key))
    if existing:
        if existing.mode != mode:raise FoundationError('INPUT_CHANGED','同一核对周期不能更换扫描模式。')
        return existing
    active = session.scalar(select(Scan).where(Scan.scope_id == scope_id, Scan.status != 'completed'))
    if active:
        raise FoundationError('WORK_CONFLICT', '该来源范围已有未完成核对周期，请先恢复原周期。')
    row = Scan(scope_id=scope_id, event_key=event_key, mode=mode, created_at=now())
    session.add(row); session.flush()
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
        return summary(scope, scan, paused=control.paused)
    deadline = time.monotonic() + budget_seconds
    try:
        # A savepoint keeps the failure outcome durable while rolling back both
        # evidence registration and the page cursor. Retry starts at that cursor.
        with session.begin_nested():
            conditions = [Observation.dataset == scope.dataset, Observation.subject == scope.subject,
                Observation.variant == scope.variant]
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


def summary(scope, scan, *, paused=False):
    selector = json.loads(scope.selector_json)
    state = '已暂停' if paused and scan.status != 'completed' else {
        'running': '进行中', 'failed': '失败，检查点未越过失败页', 'completed': '已完成'}[scan.status]
    label={'etf_daily':'ETF日线','fund_stock_history':'基金持仓报告'}[scope.dataset]
    return dict(scan_id=str(scan.id), scope_id=str(scope.id), status=scan.status, mode=scan.mode, paused=paused,
        seen=scan.seen, registered=scan.registered, error_code=scan.error_code,
        message=f'同花顺{label}（{scope.subject}，{selector["start"]}至{selector["end"]}）来源核对{state}，'
            f'累计核对{scan.seen}个版本、新登记{scan.registered}个、复用{scan.seen - scan.registered}个；'
            f'检查点已固定至累计{scan.seen}个版本。')


def next_scan(session,scope_id,event_key):
    """Resume unfinished work; otherwise fast-pass with a daily full safety net."""
    lock_key(session,'intake-run',str(scope_id))
    active=session.scalar(select(Scan).where(Scan.scope_id==scope_id,Scan.status!='completed'))
    if active:return active
    full=session.scalar(select(Scan).where(Scan.scope_id==scope_id,Scan.mode=='full',Scan.status=='completed')
        .order_by(Scan.completed_at.desc()).limit(1))
    mode='full' if full is None or now()-full.completed_at.replace(tzinfo=timezone.utc)>=timedelta(hours=24) else 'recent'
    return start_scan(session,scope_id,event_key,mode=mode)
