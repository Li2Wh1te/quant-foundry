"""Evidence-bound identity bridge; never invents instruments or historical PIT."""
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.models import Binding, SourceRef
from app.data_foundation.source_refs import read_source
from app.instruments.models import Instrument


def register_binding(session, *, source_ref_id, subject, instrument_id, valid_from, valid_to,
                     binding_version, status='unresolved', evidence=None):
    ref = session.get(SourceRef, source_ref_id)
    if ref is None or ref.dataset != 'etf_directory' or ref.representation != 'local_table_baseline':
        raise FoundationError('IDENTITY_EVIDENCE_MISSING', '身份绑定需要固定目录基线。')
    if valid_from >= valid_to or status not in {'resolved', 'unresolved'}:
        raise ValueError('Invalid binding interval/status')
    if session.get(Instrument, instrument_id) is None:
        raise FoundationError('IDENTITY_UNRESOLVED', '既有标的身份不存在，不能重新分配身份。')
    rows = [r for r in read_source(session, ref.id) if r['ts_code'] == subject and r['source'] == ref.source]
    if len(rows) != 1 or rows[0]['etf_id'] != str(instrument_id):
        raise FoundationError('IDENTITY_CONFLICT', '固定目录与既有标的身份不一致。')
    if status == 'resolved' and (not evidence or set(evidence) != {'reviewer', 'reference', 'valid_from', 'valid_to'}
        or evidence['valid_from'] != str(valid_from) or evidence['valid_to'] != str(valid_to)
        or not evidence['reviewer'] or not evidence['reference']):
        raise FoundationError('IDENTITY_EVIDENCE_MISSING', '确认绑定必须提供对应有效范围的审核证据。')
    lock_key(session, 'binding', [ref.source, subject, binding_version])
    existing = session.scalars(select(Binding).where(Binding.source == ref.source, Binding.subject == subject,
        Binding.binding_version == binding_version, Binding.valid_from < valid_to, Binding.valid_to > valid_from)).all()
    for row in existing:
        if (row.source_ref_id, row.instrument_id, row.valid_from, row.valid_to, row.status, row.evidence_json) == (
                ref.id, instrument_id, valid_from, valid_to, status, encode(evidence or {})):
            return row
    if existing:
        raise FoundationError('IDENTITY_AMBIGUOUS', '同版本身份绑定的有效范围存在重叠。')
    row = Binding(source_ref_id=ref.id, source=ref.source, subject=subject, instrument_id=instrument_id,
        valid_from=valid_from, valid_to=valid_to, binding_version=binding_version, status=status,
        known_at=now(), evidence_json=encode(evidence or {}), created_at=now())
    session.add(row)
    session.flush()
    return row


def resolve_binding(session, binding_ids, source, subject, business_date):
    rows = session.scalars(select(Binding).where(Binding.id.in_(binding_ids), Binding.source == source,
        Binding.subject == subject, Binding.valid_from <= business_date, Binding.valid_to > business_date,
        Binding.status == 'resolved')).all()
    if len(rows) != 1:
        raise FoundationError('IDENTITY_UNRESOLVED', '固定依赖中的身份绑定未唯一确认。')
    return rows[0]


def register_report_binding(session, *, source_ref_id, source_code, member_code,
                            asset_class, instrument_id, valid_from, valid_to,
                            binding_version, evidence):
    """Register only an explicitly reviewed, bounded report identity.

    Unlike the legacy ETF adapter, a report may introduce a new asset identity.
    The caller supplies its durable UUID from the reviewed registration manifest;
    replaying that manifest reuses the UUID. No identifier is synthesized from a
    ticker prefix, a display name or a source row's position.
    """
    from uuid import UUID
    ref = session.get(SourceRef, source_ref_id)
    if (ref is None or ref.dataset != 'report_identity_directory'
            or ref.representation != 'local_table_baseline'
            or asset_class not in {'fund_share', 'stock'} or valid_from >= valid_to):
        raise FoundationError('IDENTITY_EVIDENCE_MISSING', '报告身份需要固定目录、类型及审核有效范围。')
    subject = f'{asset_class}:{member_code}'
    required = {'reviewer', 'reference', 'valid_from', 'valid_to', 'source_code', 'asset_class'}
    if (len(subject) > 64 or not isinstance(evidence, dict) or set(evidence) != required
            or not evidence['reference'] or not evidence['reviewer']
            or evidence['valid_from'] != str(valid_from) or evidence['valid_to'] != str(valid_to)
            or evidence['source_code'] != source_code or evidence['asset_class'] != asset_class):
        raise FoundationError('IDENTITY_EVIDENCE_MISSING', '身份审核证据与请求范围不一致。')
    rows = [r for r in read_source(session, ref.id)
            if r.get('source_code') == source_code and r.get('asset_class') == asset_class]
    if (len(rows) != 1 or rows[0].get('member_code') != member_code
            or rows[0].get('instrument_id') != str(instrument_id)):
        raise FoundationError('IDENTITY_CONFLICT', '固定身份登记清单与待登记身份不一致。')
    lock_key(session, 'report-identity', [ref.source, subject])
    lock_key(session, 'report-instrument', str(instrument_id))
    conflicts = session.scalars(select(Binding).where(
        Binding.source == ref.source, Binding.subject == subject,
        Binding.valid_from < valid_to, Binding.valid_to > valid_from)).all()
    if any(b.instrument_id != instrument_id for b in conflicts):
        raise FoundationError('IDENTITY_AMBIGUOUS', '报告期内身份绑定冲突，不能重新分配身份。')
    old = session.scalar(select(Binding).where(Binding.source == ref.source,
        Binding.subject == subject, Binding.binding_version == binding_version))
    if old:
        if (old.source_ref_id, old.instrument_id, old.valid_from, old.valid_to, old.evidence_json) != (
                ref.id, instrument_id, valid_from, valid_to, encode(evidence)):
            raise FoundationError('IDENTITY_CONFLICT', '同一报告身份版本不能修改。')
        return old
    instrument = session.get(Instrument, instrument_id)
    if instrument and (instrument.asset_class != asset_class or instrument.merged_into_id is not None):
        raise FoundationError('IDENTITY_CONFLICT', '已有统一身份的资产类型不同或已被合并。')
    if instrument is None:
        session.add(Instrument(id=UUID(str(instrument_id)), asset_class=asset_class))
        session.flush()
    row = Binding(source_ref_id=ref.id, source=ref.source, subject=subject,
        instrument_id=instrument_id, valid_from=valid_from, valid_to=valid_to,
        binding_version=binding_version, status='resolved', known_at=now(),
        evidence_json=encode(evidence), created_at=now())
    session.add(row)
    session.flush()
    return row
