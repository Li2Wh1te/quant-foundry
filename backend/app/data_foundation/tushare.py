"""Fixed local Tushare daily bars; no provider client or live table reads.

Only this adapter interprets provider units. The public contract and publication
engine continue to operate on typed domain values. Unverified volume stays null.
"""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import time

from sqlalchemy import select
from app.data_foundation.bars import validate_bar
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.catalog import now
from app.data_foundation.identity import resolve_binding
from app.data_foundation.models import DependencyEntry, SourceRef
from app.data_foundation.source_refs import read_source
from app.data_foundation.work import fenced, finish_batch, append_event, BATCH_ROWS, BUDGET_SECONDS
from app.data_foundation.work_models import Candidate, CandidateBar, CandidateManifest, CandidateEntry, Unit

DOMAIN_KEY = 'tushare-local-daily-v1'
SERIES = 'cn-etf-cny-1d-unadjusted'


def domain_hash():
    return digest('domain-code', Path(__file__).read_text())


def optional_amount(value):
    """Multiply exactly, independent of the process's Decimal precision."""
    if value is None:
        return None, 'OPTIONAL_FIELD_MISSING'
    try:
        if isinstance(value, (float, bool)):
            raise ValueError('Non-decimal input')
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            return None, 'OPTIONAL_VALUE_INVALID'
        # Reject unreasonable exponents before formatting or allocating digits.
        if number and (number.adjusted() > 20 or number.adjusted() < -100):
            return None, 'PRECISION_OVERFLOW'
        with localcontext() as context:
            context.prec = max(40, len(number.as_tuple().digits) + 4)
            converted = number * 1000
        probe = dict(open='1', high='1', low='1', close='1', volume=None, turnover=converted)
        validate_bar(probe)
        return converted, None
    except FoundationError:
        return None, 'PRECISION_OVERFLOW'
    except (ValueError, InvalidOperation, TypeError):
        return None, 'OPTIONAL_VALUE_INVALID'


def map_row(raw, instrument_id):
    required = {'source', 'ts_code', 'trade_date', 'open', 'high', 'low', 'close'}
    if not required <= raw.keys() or raw['source'] != 'tushare':
        raise FoundationError('SOURCE_SCHEMA_INVALID', '本地日线缺少核心字段或来源不符。')
    turnover, reason = optional_amount(raw.get('amount'))
    typed = validate_bar({key: raw[key] for key in ('open', 'high', 'low', 'close')} | {
        'instrument_id': instrument_id, 'trade_date': date.fromisoformat(raw['trade_date']),
        'series': SERIES, 'volume': None, 'turnover': turnover})
    quality = {'volume': 'UNIT_UNVERIFIED'}
    if reason:
        quality['turnover'] = reason
    return typed, quality


def normalize_batch(session, work_id, epoch):
    from app.data_foundation.coverage import seal_coverage
    from app.data_foundation.quality import assessment
    work = fenced(session, work_id, epoch)
    params = json.loads(work.parameters_json)
    source = session.get(SourceRef, work.source_ref_id)
    if (work.kind != 'A' or params['domain'] != DOMAIN_KEY or params['domain_hash'] != domain_hash()
            or params['series'] != SERIES or source.dataset != 'etf_daily'
            or source.source != 'tushare' or source.representation != 'local_table_baseline'):
        raise FoundationError('DEPENDENCY_MISSING', '固定本地日线适配器或来源范围不匹配。')
    raw_rows = read_source(session, source.id)
    if len(raw_rows) > 1000:
        raise FoundationError('SCOPE_LIMIT', '首期本地日线工作最多1000行，请拆分固定范围。')
    binding_ids = session.scalars(select(DependencyEntry.binding_id).where(
        DependencyEntry.manifest_id == work.dependency_id, DependencyEntry.binding_id.is_not(None))).all()
    coverage = seal_coverage(session, work)
    expected = {(x['instrument_id'], x['trade_date']) for x in coverage['expected_keys']}
    counts = Counter((r.get('ts_code'), r.get('trade_date')) for r in raw_rows)
    work.total = len(raw_rows)
    end = min(work.cursor + BATCH_ROWS, work.total)
    started = time.monotonic()
    for index in range(work.cursor, end):
        raw = raw_rows[index]
        binding, typed = None, None
        result = {'status': 'pass', 'reasons': [], 'field_quality': {},
                  'source_subject': raw.get('ts_code'), 'trade_date': raw.get('trade_date')}
        try:
            business_date = date.fromisoformat(raw['trade_date'])
            if not params['start'] <= str(business_date) <= params['end']:
                raise FoundationError('SCOPE_MISMATCH', '来源日线超出已批准日期范围。')
            binding = resolve_binding(session, binding_ids, source.source, raw['ts_code'], business_date)
            result['instrument_id'] = str(binding.instrument_id)
            if counts[(raw['ts_code'], raw['trade_date'])] != 1:
                raise FoundationError('DUPLICATE_BUSINESS_KEY', '同一标的业务日存在重复来源记录，已隔离。')
            if coverage['status'] == 'pass' and (str(binding.instrument_id), str(business_date)) not in expected:
                raise FoundationError('SESSION_NOT_APPLICABLE', '该日不是固定日历中的适用交易会话。')
            typed, result['field_quality'] = map_row(raw, binding.instrument_id)
        except (FoundationError, ValueError, KeyError, TypeError) as exc:
            result['status'] = 'fail'
            result['reasons'] = [getattr(exc, 'code', 'SOURCE_SCHEMA_INVALID')]
        checked = assessment(session, input_hash=digest('candidate-input', [work.fingerprint, index, raw]),
            rule_hash=params['domain_hash'], scope_hash=work.scope_key, status=result['status'], results=result)
        candidate = Candidate(work_id=work.id, source_ref_id=source.id, binding_id=binding.id if binding else None,
            dependency_id=work.dependency_id, unit_key=str(index), occurrence=index,
            values_hash=digest('bar-values', typed) if typed else digest('quarantined-input', raw),
            readiness='ready' if typed else 'quarantined', assessment_id=checked.id, created_at=now())
        session.add(candidate); session.flush()
        if typed:
            session.add(CandidateBar(candidate_id=candidate.id, **typed))
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
            raise FoundationError('CANDIDATE_INCOMPLETE', '候选尚未完整处理，未封存输入。')
        manifest = CandidateManifest(work_id=work.id, row_count=len(candidates), manifest_hash=digest('candidates',
            [[c.id, c.values_hash, c.readiness] for c in candidates]), created_at=now())
        session.add(manifest); session.flush()
        session.add_all([CandidateEntry(manifest_id=manifest.id, ordinal=i, candidate_id=c.id) for i,c in enumerate(candidates)])
        append_event(session, work, 'quality', 'evaluated', {
            'ready': sum(c.readiness == 'ready' for c in candidates),
            'quarantined': sum(c.readiness == 'quarantined' for c in candidates), 'coverage': coverage['status']})
    finish_batch(session, work, status='succeeded' if end == work.total else 'queued')
    return end
