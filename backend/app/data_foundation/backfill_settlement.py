"""Incremental fixed-capture settlement; no polling of historical join counts.

Counters are an optimization only. Completion requires append-only edges to
real published releases and an independently checked fixed-denominator seal.
Processed and fully accepted inputs are deliberately separate dimensions.
"""
from sqlalchemy import select, func, exists
from sqlalchemy.dialects.postgresql import insert
from app.data_foundation.canonical import FoundationError
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.intake_models import ScanSeal, ScanTarget
from app.data_foundation.performance_models import BackfillProbe, BackfillReceipt, BackfillCompletion

PROBE_LIMIT = 200


def _probe(session, scan_id, scope):
    lock_key(session, 'backfill-settlement', [str(scan_id), scope])
    seal = session.get(ScanSeal, scan_id)
    if seal is None:
        raise FoundationError('BACKFILL_REQUIRED', '固定回填范围尚未封存。')
    probe = session.get(BackfillProbe, (scan_id, scope), populate_existing=True)
    if probe is None:
        probe = BackfillProbe(scan_id=scan_id, scope_key=scope, target_count=seal.target_count,
            cursor_id=None, processed=0, accepted=0, checked_at=now())
        session.add(probe)
        session.flush()
    if probe.target_count != seal.target_count:
        raise FoundationError('SETTLEMENT_INVALID', '回填结算的固定分母不一致。')
    return probe


def _complete(session, probe):
    for quality in ('processed', 'accepted'):
        if getattr(probe, quality) != probe.target_count:
            continue
        key = (probe.scan_id, probe.scope_key, quality)
        if session.get(BackfillCompletion, key) is None:
            count = session.scalar(select(func.count()).select_from(BackfillReceipt).where(
                BackfillReceipt.scan_id == probe.scan_id, BackfillReceipt.scope_key == probe.scope_key,
                BackfillReceipt.quality == quality))
            if count != probe.target_count:
                raise FoundationError('SETTLEMENT_INVALID', '回填结算计数与正式发布凭据不一致。')
            session.add(BackfillCompletion(scan_id=probe.scan_id, scope_key=probe.scope_key, quality=quality,
                target_count=probe.target_count, verified_at=now()))
    session.flush()


def _record(session, probe, rows):
    if rows:
        # Qualification is additionally enforced by a database insertion guard;
        # an arbitrary visit, sealed draft or older-head retain cannot qualify.
        written = session.execute(insert(BackfillReceipt).values([
            dict(scan_id=probe.scan_id, scope_key=probe.scope_key, observation_id=oid, quality=quality,
                 release_id=rid, created_at=now()) for oid, quality, rid in rows])
            .on_conflict_do_nothing(index_elements=['scan_id', 'scope_key', 'observation_id', 'quality'])
            .returning(BackfillReceipt.quality)).scalars().all()
        probe.processed += written.count('processed')
        probe.accepted += written.count('accepted')
    probe.checked_at = now()
    session.flush()
    _complete(session, probe)


def probe_page(session, *, native_dataset, scan_id, scope, limit=PROBE_LIMIT):
    """Inspect a bounded rotating page; lower IDs never become unreachable."""
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError('Settlement probe limit must be 1 to 2000')
    probe = _probe(session, scan_id, scope)
    if session.get(BackfillCompletion, (scan_id, scope, 'accepted')) is not None:
        return status(session, probe)
    accepted = exists(select(1).select_from(BackfillReceipt).where(BackfillReceipt.scan_id == scan_id,
        BackfillReceipt.scope_key == scope, BackfillReceipt.quality == 'accepted',
        BackfillReceipt.observation_id == ScanTarget.observation_id))
    base = select(ScanTarget.observation_id).where(ScanTarget.scan_id == scan_id, ~accepted)
    ids = list(session.scalars(base.where(ScanTarget.observation_id > probe.cursor_id if probe.cursor_id else True)
        .order_by(ScanTarget.observation_id).limit(limit)))
    if not ids and probe.cursor_id is not None:
        # This is a finite-pass cursor over frozen membership, never a watermark
        # over late source arrivals. Incomplete/isolated inputs are revisited.
        probe.cursor_id = None
        ids = list(session.scalars(base.order_by(ScanTarget.observation_id).limit(limit)))
    rows = []
    if ids:
        from app.data_foundation.record_updates import publication_receipts
        for quality, include in (('processed', True), ('accepted', False)):
            receipts = publication_receipts(native_dataset, include_quarantined=include, observation_ids=ids)
            found = {}
            for oid, rid in session.execute(select(receipts.c.observation_id, receipts.c.release_id)):
                found.setdefault(oid, rid)
            rows.extend((oid, quality, rid) for oid, rid in found.items())
        probe.cursor_id = ids[-1]
    _record(session, probe, rows)
    return status(session, probe)


def status(session, probe):
    processed = session.get(BackfillCompletion, (probe.scan_id, probe.scope_key, 'processed')) is not None
    accepted = session.get(BackfillCompletion, (probe.scan_id, probe.scope_key, 'accepted')) is not None
    return dict(processed_complete=processed, accepted_complete=accepted,
        pending_backfill=probe.target_count-probe.processed, backfill_issues=probe.processed-probe.accepted,
        fixed_sources=probe.target_count, accepted_sources=probe.accepted,
        # Counts describe verified settlement edges; an initial rebuild may
        # conservatively lag already-published input, but can never run ahead.
        settlement_checked_at=probe.checked_at, settlement_counts_provisional=not accepted)


def record_publication(session, release):
    """Publish-time receipt insertion is atomic with the actual publication."""
    from app.data_foundation.work_models import Work, CandidateManifest, Candidate
    from app.data_foundation.models import SourceRef
    from app.data_foundation.record_work import OLDER_SOURCE_RETAINED
    work = session.get(Work, release.work_id)
    if work.candidate_manifest_id is None or OLDER_SOURCE_RETAINED in work.parameters_json:
        return
    manifest = session.get(CandidateManifest, work.candidate_manifest_id)
    origin = session.get(Work, manifest.work_id)
    source = session.get(SourceRef, origin.source_ref_id)
    if source is not None and source.source == 'tushare':
        from app.data_foundation.table_settlement import record_publication as table_publication
        table_publication(session, source, release)
        return
    if source is None or source.source != 'tonghuashun' or source.observation_id is None:
        return
    scans = list(session.scalars(select(ScanTarget.scan_id).join(ScanSeal, ScanSeal.scan_id == ScanTarget.scan_id)
        .where(ScanTarget.observation_id == source.observation_id).order_by(ScanTarget.scan_id)))
    if not scans:
        return
    quarantined = session.scalar(select(exists().where(Candidate.work_id == origin.id, Candidate.readiness != 'ready')))
    rows = [(source.observation_id, 'processed', release.id)]
    if not quarantined:
        rows.append((source.observation_id, 'accepted', release.id))
    for scan_id in scans:
        _record(session, _probe(session, scan_id, release.scope_key), rows)
