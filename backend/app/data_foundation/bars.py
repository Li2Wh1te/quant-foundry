"""Typed daily-bar mechanism. Provider-specific normalization belongs to M3.

The core adapter accepts explicitly standardized local rows only. It never
interprets Tushare volume units or Tonghuashun adjustment semantics by inference.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import time
import json
from uuid import UUID
from sqlalchemy import select
from app.data_foundation.canonical import digest, encode, FoundationError
from app.data_foundation.catalog import now
from app.data_foundation.identity import resolve_binding
from app.data_foundation.models import Binding, DependencyEntry, SourceRef
from app.data_foundation.source_refs import read_source
from app.data_foundation.work import fenced, finish_batch, append_event, BATCH_ROWS, BUDGET_SECONDS
from app.data_foundation.work_models import Work, Unit, Candidate, CandidateBar, CandidateManifest, CandidateEntry

DOMAIN_KEY = 'bar-core-v1'
FIELDS = ('instrument_id', 'trade_date', 'series', 'open', 'high', 'low', 'close', 'volume', 'turnover')


def values(row):
    return {key: getattr(row, key) for key in FIELDS}


def validate_bar(row):
    """Reject excess scale before PostgreSQL can round a Numeric assignment."""
    result = dict(row)
    for key in ('open', 'high', 'low', 'close', 'volume', 'turnover'):
        value = row.get(key)
        if value is None and key in ('volume', 'turnover'):
            result[key] = None
            continue
        try:
            if isinstance(value, (float, bool)) or value is None:
                raise ValueError('Non-decimal numeric input')
            number = Decimal(value)
            precision, scale = (32, 8) if key in ('volume', 'turnover') else (28, 10)
            canonical = encode(number).strip('"')
            digits = canonical.lstrip('-').split('.')
            if not number.is_finite() or len(digits[0]) > precision-scale or (len(digits) > 1 and len(digits[1]) > scale):
                raise ValueError('Numeric precision exceeded')
            if number < 0 or (key not in ('volume', 'turnover') and number == 0):
                raise ValueError('Nonpositive price')
            result[key] = number
        except (InvalidOperation, ValueError, TypeError):
            raise FoundationError('BAR_INVALID', '日线字段无效或超过契约精度，未生成正式值。') from None
    if not result['low'] <= min(result['open'], result['close']) <= max(result['open'], result['close']) <= result['high']:
        raise FoundationError('BAR_INVALID', '日线高低价范围与开收盘价不一致。')
    return result


def domain_hash():
    # Bind the core mechanism to its exact installed source, not a mutable name.
    from pathlib import Path
    return digest('domain-code', Path(__file__).read_text())


def normalize_batch(session, work_id, epoch):
    work = fenced(session, work_id, epoch)
    params = json.loads(work.parameters_json)
    if params['domain'] == 'typed-record-v1':
        from app.data_foundation.record_work import normalize_batch as normalize_records
        return normalize_records(session, work_id, epoch)
    from app.data_foundation import tushare, holding_work
    if params['domain'] == holding_work.DOMAIN_KEY:
        return holding_work.normalize_batch(session, work_id, epoch)
    if params['domain'] == tushare.DOMAIN_KEY:
        return tushare.normalize_batch(session, work_id, epoch)
    if work.kind != 'A' or params['domain'] != DOMAIN_KEY or params['domain_hash'] != domain_hash():
        raise FoundationError('DEPENDENCY_MISSING', '固定转换版本不可加载，不能使用新版本继续工作。')
    source = session.get(SourceRef, work.source_ref_id)
    # Source-local provider rows cannot accidentally enter this adapter.
    if source.dataset != 'standardized_bar_input':
        raise FoundationError('DEPENDENCY_MISSING', '该来源尚未接入已确认的领域转换。')
    data = read_source(session, source.id)
    binding_ids = session.scalars(select(DependencyEntry.binding_id).where(DependencyEntry.manifest_id == work.dependency_id,
        DependencyEntry.binding_id.is_not(None))).all()
    work.total = len(data)
    end = min(work.cursor + BATCH_ROWS, len(data))
    batch = data[work.cursor:end]
    started = time.monotonic()
    for index, raw in enumerate(batch, work.cursor):
        key = str(index)
        binding, typed, quality = None, None, {'status': 'pass', 'reasons': []}
        try:
            if set(raw) != {'schema', 'source_subject', 'trade_date', 'series', 'open', 'high', 'low', 'close', 'volume', 'turnover'} or raw['schema'] != 'standardized-bar-v1':
                raise FoundationError('SOURCE_SCHEMA_INVALID', '输入不是固定的标准日线字段契约。')
            business_date = date.fromisoformat(raw['trade_date'])
            if not params['start'] <= str(business_date) <= params['end'] or raw['series'] != params['series']:
                raise FoundationError('SCOPE_MISMATCH', '候选超出工作日期或语义范围。')
            binding = resolve_binding(session, binding_ids, source.source, raw['source_subject'], business_date)
            typed = validate_bar({k: raw[k] for k in FIELDS if k != 'instrument_id'} | {'instrument_id': binding.instrument_id, 'trade_date': business_date})
        except (FoundationError, ValueError) as exc:
            quality = {'status': 'fail', 'reasons': [getattr(exc, 'code', 'SOURCE_SCHEMA_INVALID')]}
        from app.data_foundation.quality import assessment
        checked = assessment(session, input_hash=digest('candidate-input', [work.fingerprint, index, raw]), rule_hash=params['domain_hash'], scope_hash=work.scope_key, status=quality['status'], results=quality)
        candidate = Candidate(work_id=work.id, source_ref_id=source.id, binding_id=binding.id if binding else None,
            dependency_id=work.dependency_id, unit_key=key, occurrence=index, values_hash=digest('bar-values', typed) if typed else digest('quarantined-input', raw),
            readiness='ready' if typed else 'quarantined', assessment_id=checked.id, created_at=now())
        session.add(candidate); session.flush()
        if typed:
            session.add(CandidateBar(candidate_id=candidate.id, **typed))
        session.add(Unit(work_id=work.id, unit_key=key, result_hash=candidate.values_hash, row_count=1))
        if time.monotonic() - started >= BUDGET_SECONDS:
            end = index + 1
            break
    work.cursor = end
    session.flush()
    # Holding the row lock alone is insufficient if this transaction outlives
    # its lease. Recheck database time immediately before sealing/committing.
    fenced(session, work.id, epoch)
    if end == len(data):
        candidates = session.scalars(select(Candidate).where(Candidate.work_id == work.id).order_by(Candidate.occurrence)).all()
        if len(candidates) != len(data):
            raise FoundationError('CANDIDATE_INCOMPLETE', '候选对象尚未完整装配。')
        manifest = CandidateManifest(work_id=work.id, row_count=len(candidates), manifest_hash=digest('candidates',
            [[c.id, c.values_hash, c.readiness] for c in candidates]), created_at=now())
        session.add(manifest); session.flush()
        session.add_all([CandidateEntry(manifest_id=manifest.id, ordinal=i, candidate_id=c.id) for i, c in enumerate(candidates)])
        append_event(session, work, 'quality', 'evaluated', {'ready': sum(c.readiness == 'ready' for c in candidates), 'quarantined': sum(c.readiness != 'ready' for c in candidates)})
    finish_batch(session, work, status='succeeded' if end == len(data) else 'queued')
    return end
