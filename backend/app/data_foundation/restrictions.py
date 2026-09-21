"""Preserve field-scoped restrictions across new candidate and revision IDs."""
from sqlalchemy import select
from app.data_foundation.work_models import Candidate, OfficialBar


def revision_fingerprints(session, model, current_ids, restricted_ids):
    """Read only immutable official values and provenance, never source payloads.

    Whole-row hashes cannot identify a still-incorrect field after unrelated
    fields change. Each field therefore retains its own identity/value cells.
    Report issues currently cover the whole report field: any reused member
    cell remains restricted. Changing another member, rank or ordinal cannot
    serve as evidence that this cell was corrected. Actual corrected values or
    new source evidence remain distinguishable; business keys prevent leakage
    between dates, subjects and report periods. The no-issue path performs no
    query, and report members are loaded in one batch rather than per object.
    """
    restricted = {i for i in restricted_ids if i is not None}
    if not restricted:
        return {}
    ids = restricted | {i for i in current_ids if i is not None}
    rows = session.execute(select(model, Candidate.source_ref_id, Candidate.binding_id)
        .join(Candidate, Candidate.id == model.candidate_id).where(model.id.in_(ids))).all()
    if model is OfficialBar:
        fields = ('open', 'high', 'low', 'close', 'volume', 'turnover')
        return {row.id: ((source, binding, row.instrument_id, row.trade_date, row.series),
            {field: {getattr(row, field)} for field in fields}) for row, source, binding in rows}
    from app.data_foundation.holding_models import OfficialReport, OfficialReportMember
    if model is not OfficialReport:
        raise ValueError('Unsupported restricted revision type')
    fields = ('hold_ratio', 'market_value', 'period_change_ratio', 'rank')
    result = {row.id: ((source, binding, row.fund_share_id, row.period_start, row.period_end,
        row.report_type, row.scope_kind, row.series), {field: set() for field in fields})
        for row, source, binding in rows}
    for member in session.scalars(select(OfficialReportMember).where(OfficialReportMember.official_id.in_(ids))):
        for field in fields:
            # Do not use ordinal: reordering or removing a different occurrence
            # must not rehabilitate an unchanged restricted member value.
            result[member.official_id][1][field].add((member.member_instrument_id,
                member.binding_id, member.source_member_code, getattr(member, field)))
    return result


def matches_revision(original, current, fingerprints, fields):
    """Block a requested issue field while any of its original cells survives."""
    if not fields:
        return False
    if original is None or original == current:
        return True
    if original not in fingerprints or current not in fingerprints:
        return False
    old, new = fingerprints[original], fingerprints[current]
    return old[0] == new[0] and any(old[1].get(field, set()) & new[1].get(field, set()) for field in fields)
