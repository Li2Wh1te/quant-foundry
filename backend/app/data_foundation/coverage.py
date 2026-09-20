"""Calendar-aware applicability fixed at normalization, never inferred from bars."""
from datetime import date, timedelta
import json
from sqlalchemy import select
from app.data_foundation.canonical import digest, FoundationError
from app.data_foundation.models import DependencyEntry, SourceRef, Binding
from app.data_foundation.work_models import WorkCoverage, Assessment
from app.data_foundation.source_refs import read_source


def coverage_for(session, work_id):
    from app.data_foundation.work_models import Work
    work = session.get(Work, work_id)
    if work and work.kind == 'B':
        from app.data_foundation.inputs import input_manifests
        return merge_coverage([coverage_for(session, m.work_id) for m in input_manifests(session, work=work)])
    link = session.get(WorkCoverage, work_id)
    return json.loads(session.get(Assessment, link.assessment_id).results_json) if link else None


def seal_coverage(session, work):
    from app.data_foundation.quality import assessment
    existing = coverage_for(session, work.id)
    if existing is not None:
        return existing
    params = json.loads(work.parameters_json)
    start, end = date.fromisoformat(params['start']), date.fromisoformat(params['end'])
    if (end-start).days > 366:
        raise FoundationError('SCOPE_LIMIT', '首期日线工作范围不能超过367个自然日。')
    entries = session.scalars(select(DependencyEntry).where(DependencyEntry.manifest_id == work.dependency_id)).all()
    refs = [session.get(SourceRef,e.source_ref_id) for e in entries if e.source_ref_id]
    calendars = [r for r in refs if r.dataset == 'exchange_calendar']
    directories = [r for r in refs if r.dataset == 'etf_directory']
    bindings = [session.get(Binding,e.binding_id) for e in entries if e.binding_id]
    result = {'status': 'unknown', 'start': str(start), 'end': str(end), 'subjects': [],
        'expected_keys': [], 'reasons': [], 'calendar_ref_id': None, 'directory_ref_id': None,
        'time_evidence': 'observed_only'}
    if len(calendars) != 1 or len(directories) != 1 or not bindings:
        result['reasons'].append('COVERAGE_DEPENDENCY_MISSING')
    else:
        calendar, directory = calendars[0], directories[0]
        result.update(calendar_ref_id=str(calendar.id), directory_ref_id=str(directory.id))
        crows, drows = read_source(session,calendar.id), read_source(session,directory.id)
        seen = set()
        for binding in bindings:
            rows = [r for r in drows if r.get('ts_code') == binding.subject and r.get('source') == binding.source]
            if (binding.status != 'resolved' or binding.source_ref_id != directory.id or len(rows) != 1
                    or rows[0].get('etf_id') != str(binding.instrument_id)
                    or binding.instrument_id in seen or binding.valid_from > start or binding.valid_to <= end):
                result['reasons'].append('IDENTITY_UNRESOLVED'); continue
            seen.add(binding.instrument_id)
            row = rows[0]
            market = {'SH': 'SSE', 'SZ': 'SZSE'}.get(row.get('exchange'))
            # A reviewed binding establishes identity, while the fixed listing
            # record establishes applicability. Unknown status stays unknown.
            if not market or not row.get('list_date') or row.get('list_status') != 'L':
                result['reasons'].append('COVERAGE_DEPENDENCY_MISSING'); continue
            try:
                listed = date.fromisoformat(row['list_date'])
            except (ValueError,TypeError):
                result['reasons'].append('COVERAGE_DEPENDENCY_MISSING'); continue
            days = {}
            duplicate = False
            for day in crows:
                if day.get('exchange') == market and str(start) <= day.get('calendar_date','') <= str(end):
                    key = day['calendar_date']
                    if key in days or not isinstance(day.get('is_open'),bool): duplicate = True
                    days[key] = day.get('is_open')
            expected_dates={str(start+timedelta(days=i)) for i in range((end-start).days+1)}
            if duplicate or set(days) != expected_dates:
                result['reasons'].append('COVERAGE_DEPENDENCY_MISSING'); continue
            result['subjects'].append({'instrument_id':str(binding.instrument_id),'code':binding.subject,
                'name':row.get('csname',binding.subject),'exchange':market,'binding_id':str(binding.id)})
            for offset in range((end-start).days+1):
                day = start+timedelta(days=offset)
                if days.get(str(day)) is True and day >= listed:
                    result['expected_keys'].append({'instrument_id':str(binding.instrument_id),'trade_date':str(day)})
        result['status'] = 'pass' if not result['reasons'] else 'unknown'
    result['reasons'] = sorted(set(result['reasons']))
    result['expected_keys'].sort(key=lambda x:(x['trade_date'],x['instrument_id']))
    checked = assessment(session,input_hash=work.fingerprint,rule_hash=digest('coverage-rule','calendar-listing-binding-v1'),
        scope_hash=work.scope_key,status=result['status'],results=result)
    session.add(WorkCoverage(work_id=work.id,assessment_id=checked.id));session.flush()
    return result


def merge_coverage(parts):
    """Union actual applicability, retaining per-subject intervals and evidence.

    A bounding date range alone cannot prove coverage across holes. Readers use
    covers_request below, and overlapping inputs must agree on expected dates.
    """
    if len(parts) == 1:
        return parts[0]
    if not parts or any(not p or p['status'] != 'pass' for p in parts):
        return None
    subjects, spans, expected = {}, {}, set()
    for part in parts:
        keys = {(k['instrument_id'], k['trade_date']) for k in part['expected_keys']}
        for subject in part['subjects']:
            iid = subject['instrument_id']
            prior = subjects.get(iid)
            if prior and any(prior.get(k) != subject.get(k) for k in ('code', 'exchange')):
                raise FoundationError('COVERAGE_CONFLICT', '输入的标的适用依据冲突。')
            for start, end in spans.get(iid, []):
                lo, hi = max(start, part['start']), min(end, part['end'])
                if lo <= hi and ({k for k in expected if k[0] == iid and lo <= k[1] <= hi}
                        != {k for k in keys if k[0] == iid and lo <= k[1] <= hi}):
                    raise FoundationError('COVERAGE_CONFLICT', '重叠输入的交易日适用依据冲突。')
            subjects[iid] = subject
            spans.setdefault(iid, []).extend(part.get('subject_intervals', {}).get(iid, [[part['start'], part['end']]]))
        expected.update(keys)
    return dict(status='pass', start=min(p['start'] for p in parts), end=max(p['end'] for p in parts),
        subjects=[subjects[k] for k in sorted(subjects)], subject_intervals=spans,
        expected_keys=[dict(instrument_id=i, trade_date=d) for i, d in sorted(expected, key=lambda k:(k[1],k[0]))],
        reasons=[], time_evidence='observed_only', input_evidence=parts,
        calendar_ref_id=None, directory_ref_id=None)


def covers_request(coverage, ids, start, end):
    if not coverage or coverage['status'] != 'pass':
        return False
    if not ids <= {s['instrument_id'] for s in coverage['subjects']}:
        return False
    for iid in ids:
        spans = coverage.get('subject_intervals', {}).get(iid, [[coverage['start'], coverage['end']]])
        cursor = date.fromisoformat(start)
        for lower, upper in sorted(spans):
            lo, hi = date.fromisoformat(lower), date.fromisoformat(upper)
            if lo > cursor:
                break
            if hi >= cursor:
                if hi >= date.fromisoformat(end):
                    break
                cursor = hi + timedelta(days=1)
        else:
            return False
        if hi < date.fromisoformat(end) or lo > cursor:
            return False
    return True


def release_coverage(session, release):
    """Include the actual decision origins of inherited blocks and revisions."""
    from app.data_foundation.work_models import BlockRef, BlockMember, Decision
    work_ids = set(session.scalars(select(Decision.work_id).join(BlockMember,BlockMember.decision_id==Decision.id)
        .join(BlockRef,BlockRef.block_id==BlockMember.block_id).where(BlockRef.release_id==release.id)))
    work_ids.add(release.work_id)
    return merge_coverage([coverage_for(session,wid) for wid in sorted(work_ids,key=str)])
