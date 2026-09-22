"""Bounded immutable registrations for a fenced typed-record page.

The advisory identities match the single-row registrars. Acquire them in a
stable order, fetch existing evidence once, and insert only absent rows. This
retains idempotency/conflict detection without a database round trip per field.
"""
from uuid import uuid4
from sqlalchemy import select, text, tuple_
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Assessment


def lock_many(session, namespace, identities):
    keys = set()
    for identity in identities:
        key = int(digest(namespace, identity)[:16], 16)
        keys.add(key if key < 2**63 else key - 2**64)
    if keys and session.bind.dialect.name == 'postgresql':
        # The sorted input avoids competing pages taking the same set of
        # registration locks in opposing orders. The row's unique constraint
        # remains the final integrity boundary.
        session.execute(text('SELECT pg_advisory_xact_lock(k) FROM '
            '(SELECT unnest(CAST(:keys AS bigint[])) AS k ORDER BY k) AS ordered_keys'),
            {'keys': sorted(keys)}).all()


def register_subjects(session, source, values):
    identities = {(source.source, value['subject_kind'], value['subject_key']) for value in values if value}
    if any(len(kind) > 64 or len(key) > 128 for _, kind, key in identities):
        raise FoundationError('IDENTITY_UNRESOLVED', '来源主体标识超出契约长度。')
    if not identities:
        return {}
    lock_many(session, 'record-subject', sorted(identities))
    rows = session.scalars(select(RecordSubject).where(tuple_(RecordSubject.source,
        RecordSubject.kind, RecordSubject.source_key).in_(identities))).all()
    result = {(r.source, r.kind, r.source_key): r for r in rows}
    for identity in sorted(identities - result.keys()):
        row = RecordSubject(id=uuid4(), source=identity[0], kind=identity[1], source_key=identity[2],
            evidence_source_ref_id=source.id, created_at=now())
        session.add(row)
        result[identity] = row
    session.flush()
    return result


def register_assessments(session, specifications):
    fixed = {}
    for spec in specifications:
        key = (spec['input_hash'], spec['rule_hash'], spec['scope_hash'])
        payload = (spec['status'], encode(spec['results']))
        if key in fixed and fixed[key] != payload:
            raise FoundationError('ASSESSMENT_CONFLICT', '同页固定输入与规则的评估结果不一致。')
        fixed[key] = payload
    if not fixed:
        return {}
    lock_many(session, 'assessment', sorted(fixed))
    rows = session.scalars(select(Assessment).where(tuple_(Assessment.input_hash,
        Assessment.rule_hash, Assessment.scope_hash).in_(fixed))).all()
    result = {(r.input_hash, r.rule_hash, r.scope_hash): r for r in rows}
    for key, (status, body) in fixed.items():
        row = result.get(key)
        if row and (row.status != status or row.results_json != body):
            raise FoundationError('ASSESSMENT_CONFLICT', '固定输入与规则的评估结果不能变更。')
        if row is None:
            row = Assessment(id=uuid4(), input_hash=key[0], rule_hash=key[1], scope_hash=key[2],
                status=status, results_json=body, created_at=now())
            session.add(row)
            result[key] = row
    session.flush()
    return result
