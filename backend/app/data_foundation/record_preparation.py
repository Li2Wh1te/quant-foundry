"""Convert fixed input once, preserving cross-page duplicates and restart state.

Production preparation runs outside the candidate transaction's work-row lock.
Each prepared page is separately fenced and committed. A restart reopens the
fixed source but converts only uncommitted pages. No candidate consumes these
pages until the whole source's duplicate-key evidence is sealed.
"""
from collections import Counter
from datetime import date
import json
import time
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.models import SourceRef
from app.data_foundation.performance_models import RecordPreparation, RecordPreparedPage
from app.data_foundation.work import BATCH_ROWS, fenced
from app.data_foundation.work_models import Work


def manifest_hash(fingerprint, source_hash, count, hashes, duplicates):
    return digest('typed-record-preparation-v1', [fingerprint, source_hash, count, hashes, duplicates])


def checked_header(session, work, *, sealed=True):
    header = session.get(RecordPreparation, work.id)
    if header is None:
        return None
    source = session.get(SourceRef, work.source_ref_id)
    if (source is None or header.work_fingerprint != work.fingerprint
            or header.source_hash != source.content_hash):
        raise FoundationError('PREPARATION_INVALID', '标准化预处理与固定来源或工作不一致。')
    if not header.sealed:
        if sealed:
            raise FoundationError('PREPARATION_INCOMPLETE', '标准化预处理尚未完整封存。')
        return header
    hashes, duplicates = json.loads(header.page_hashes_json), json.loads(header.duplicate_keys_json)
    if (len(hashes) != (header.row_count + BATCH_ROWS - 1) // BATCH_ROWS
            or duplicates != sorted(set(duplicates))
            or header.manifest_hash != manifest_hash(work.fingerprint, source.content_hash,
                                                      header.row_count, hashes, duplicates)):
        raise FoundationError('PREPARATION_INVALID', '标准化预处理清单摘要或范围不一致。')
    return header


def _source(session, work):
    from app.data_foundation import record_work as records
    params = records.verify(work)
    source = session.get(SourceRef, work.source_ref_id)
    if work.kind != 'A' or source is None or records.dataset_for(source) != params['dataset']:
        raise FoundationError('SCOPE_MISMATCH', '来源与领域契约不一致。')
    raw = records.rows_for(source, records.read_source(session, source.id))
    from app.data_foundation.table_updates import is_deleted_change
    return source, raw, is_deleted_change(session, source)


def _convert_page(work, source, raw, start, deleted):
    # Use exactly the public adapter/key functions used by direct audit callers.
    from app.data_foundation import record_work as records
    parsed = []
    for index in range(start, min(start + BATCH_ROWS, len(raw))):
        item = raw[index]
        try:
            value = records.convert(source, item)
            if deleted:
                value['field_quality']['source_deleted'] = 'FIXED_LOCAL_ROW_REMOVAL'
            key, reason = records.record_key(source, value), None
        except (FoundationError, ValueError, TypeError, OverflowError) as exc:
            value, key, reason = None, None, getattr(exc, 'code', 'SOURCE_SCHEMA_INVALID')
        parsed.append(dict(value=value, key=key, reason=reason,
            input_hash=digest('typed-record-input', [work.fingerprint, index, item]),
            quarantine_hash=digest('quarantined-record', item) if value is None else None))
    return parsed


def _start(session, work, source, count):
    header = checked_header(session, work, sealed=False)
    if header is None:
        header = RecordPreparation(work_id=work.id, work_fingerprint=work.fingerprint,
            source_hash=source.content_hash, row_count=count, page_hashes_json='[]',
            duplicate_keys_json='[]', manifest_hash='', sealed=False, created_at=now())
        session.add(header)
        session.flush()
    if header.row_count != count:
        raise FoundationError('PREPARATION_INVALID', '固定来源预处理行数发生变化。')
    return header


def _next(session, work_id):
    last = session.scalar(select(RecordPreparedPage.ordinal).where(RecordPreparedPage.work_id == work_id)
        .order_by(RecordPreparedPage.ordinal.desc()).limit(1))
    return 0 if last is None else last + 1


def _write_page(session, work, ordinal, values):
    session.add(RecordPreparedPage(work_id=work.id, ordinal=ordinal, payload_json=encode(values),
        keys_json=encode([item['key'] for item in values]), row_count=len(values),
        content_hash=digest('typed-record-prepared-page-v1', values)))
    session.flush()


def _seal(session, work, header):
    # Read only narrow metadata/keys, not converted bodies, when checking the
    # entire source's uniqueness. Page rows are immutable even before this seal.
    pages = list(session.execute(select(RecordPreparedPage.ordinal, RecordPreparedPage.content_hash,
        RecordPreparedPage.keys_json, RecordPreparedPage.row_count)
        .where(RecordPreparedPage.work_id == work.id).order_by(RecordPreparedPage.ordinal)))
    if (len(pages) != (header.row_count + BATCH_ROWS - 1) // BATCH_ROWS
            or any(p.ordinal != index or p.row_count != min(BATCH_ROWS, header.row_count-index*BATCH_ROWS)
                   for index, p in enumerate(pages))):
        raise FoundationError('PREPARATION_INCOMPLETE', '标准化预处理分页不连续或数量不完整。')
    counts = Counter(key for p in pages for key in json.loads(p.keys_json) if key is not None)
    duplicates = sorted(key for key, count in counts.items() if count != 1)
    hashes = [p.content_hash for p in pages]
    header.page_hashes_json, header.duplicate_keys_json = encode(hashes), encode(duplicates)
    header.manifest_hash = manifest_hash(work.fingerprint, header.source_hash, header.row_count, hashes, duplicates)
    header.sealed = True
    session.flush()
    return header


def prepare_inline(session, work, epoch):
    """Synchronous compatibility entry for direct callers with caller-owned TX."""
    header = checked_header(session, work, sealed=False)
    if header is not None and header.sealed:
        return header
    source, raw, deleted = _source(session, work)
    fenced(session, work.id, epoch)
    header = _start(session, work, source, len(raw))
    for ordinal in range(_next(session, work.id), (len(raw) + BATCH_ROWS - 1) // BATCH_ROWS):
        _write_page(session, work, ordinal, _convert_page(work, source, raw, ordinal*BATCH_ROWS, deleted))
    fenced(session, work.id, epoch)
    return _seal(session, work, header)


def prepare_step(engine, work_id, epoch, *, stop, budget_seconds=20, clock=time.monotonic):
    """Commit bounded pages without holding the work row while decoding.

    The existing source adapter materializes one fixed observation. This entry
    bounds conversion/write pages, not the provider's original payload size.
    Heartbeats remain live during that read and the first conversion as well.
    """
    deadline = clock() + budget_seconds
    with Session(engine) as session:
        work = session.get(Work, work_id)
        header = checked_header(session, work, sealed=False)
        if header is not None and header.sealed:
            return True
        source, raw, deleted = _source(session, work)
        ordinal = _next(session, work_id)
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL statement_timeout = '30s'"))
        work_locked = fenced(session, work_id, epoch)
        _start(session, work_locked, source, len(raw))
    page_count = (len(raw) + BATCH_ROWS - 1) // BATCH_ROWS
    while ordinal < page_count:
        if stop.is_set():
            return False
        values = _convert_page(work, source, raw, ordinal*BATCH_ROWS, deleted)
        with Session(engine) as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout = '30s'"))
            work_locked = fenced(session, work_id, epoch)
            _write_page(session, work_locked, ordinal, values)
            fenced(session, work_id, epoch)
        ordinal += 1
        if clock() >= deadline and ordinal < page_count:
            return False
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL statement_timeout = '30s'"))
        work_locked = fenced(session, work_id, epoch)
        header = checked_header(session, work_locked, sealed=False)
        _seal(session, work_locked, header)
        fenced(session, work_id, epoch)
    return True


def candidate_page(session, work, epoch):
    header = checked_header(session, work, sealed=False)
    if header is None or not header.sealed:
        header = prepare_inline(session, work, epoch)
    if work.cursor < 0 or work.cursor > header.row_count or (work.cursor % BATCH_ROWS and work.cursor != header.row_count):
        raise FoundationError('PREPARATION_INVALID', '标准化检查点超出预处理分页边界。')
    if work.cursor == header.row_count:
        return header.row_count, []
    ordinal = work.cursor // BATCH_ROWS
    stored = session.get(RecordPreparedPage, (work.id, ordinal))
    if stored is None:
        raise FoundationError('PREPARATION_INVALID', '标准化预处理分页缺失。')
    values = json.loads(stored.payload_json)
    if (stored.content_hash != json.loads(header.page_hashes_json)[ordinal]
            or digest('typed-record-prepared-page-v1', values) != stored.content_hash
            or len(values) != min(BATCH_ROWS, header.row_count-work.cursor)
            or encode([item['key'] for item in values]) != stored.keys_json):
        raise FoundationError('PREPARATION_INVALID', '标准化预处理分页内容或数量不一致。')
    duplicates = set(json.loads(header.duplicate_keys_json))
    for item in values:
        if item['key'] is not None and item['key'] in duplicates:
            item['reason'] = 'DUPLICATE_BUSINESS_KEY'
        if item['value'] and item['value']['business_date'] is not None:
            item['value']['business_date'] = date.fromisoformat(item['value']['business_date'])
    return header.row_count, values
