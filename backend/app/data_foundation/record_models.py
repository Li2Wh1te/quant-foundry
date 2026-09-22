"""Typed domain records attached to the existing candidate/release graph.

Subjects identify observed source-local entities. They are deliberately not
economic instruments and cannot be passed to the legacy instrument/PIT API.
Canonical bodies are validated against a registered executable domain schema;
raw provider payloads remain solely in immutable source evidence.
"""
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.data_foundation.models import Record, fk


class RecordSubject(Record, Base):
    __tablename__ = 'foundation_record_subjects'
    source: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(128))
    evidence_source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'))
    __table_args__ = (UniqueConstraint('source', 'kind', 'source_key'),)


class RecordFields:
    subject_id: Mapped[UUID] = mapped_column(fk('record_subjects'))
    business_key: Mapped[str] = mapped_column(String(64))
    business_date: Mapped[date | None]
    schema_key: Mapped[str] = mapped_column(String(128))
    body_json: Mapped[str] = mapped_column(Text)
    field_quality_json: Mapped[str] = mapped_column(Text)


class CandidateRecord(RecordFields, Base):
    __tablename__ = 'foundation_record_candidates'
    candidate_id: Mapped[UUID] = mapped_column(fk('candidates'), primary_key=True)


class OfficialRecord(Record, RecordFields, Base):
    __tablename__ = 'foundation_record_official_revisions'
    candidate_id: Mapped[UUID] = mapped_column(fk('candidates'))
    decision_id: Mapped[UUID] = mapped_column(fk('decisions'), unique=True)
    values_hash: Mapped[str] = mapped_column(String(64))


class RecordBlockMember(Base):
    __tablename__ = 'foundation_record_block_members'
    block_id: Mapped[UUID] = mapped_column(fk('release_blocks'), primary_key=True)
    target_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(fk('record_subjects'), index=True)
    business_date: Mapped[date | None]
    state: Mapped[str] = mapped_column(String(16))
    official_id: Mapped[UUID | None] = mapped_column(fk('record_official_revisions'))
    decision_id: Mapped[UUID] = mapped_column(fk('decisions'))
    __table_args__ = (CheckConstraint(
        "(state = 'value' AND official_id IS NOT NULL) OR "
        "(state IN ('gap','blocked','withdrawn') AND official_id IS NULL)", name='record_member_state'),)


class RecordBlockVerification(Base):
    """A full validator's receipt for a closed, database-immutable block.

    The validator hash prevents reusing an older implementation's result.
    Receipts are transactional with validation and may not be changed or
    deleted. They attest to member/value/decision checks, not issue resolution
    or publication approval, which remain live checks at their own gates.
    """
    __tablename__ = 'foundation_record_block_verifications'
    block_id: Mapped[UUID] = mapped_column(fk('release_blocks'), primary_key=True)
    validator_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64))
    schema_key: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    governance_work_ids_json: Mapped[str] = mapped_column(Text)
    normalization_work_ids_json: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecordPlanVerification(Base):
    """Proof that an immutable work plan was derived from its sealed inputs."""
    __tablename__ = 'foundation_record_plan_verifications'
    work_id: Mapped[UUID] = mapped_column(fk('work'), primary_key=True)
    validator_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_fingerprint: Mapped[str] = mapped_column(String(64))
    parameters_hash: Mapped[str] = mapped_column(String(64))
    action_count: Mapped[int] = mapped_column(Integer)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecordIssue(Record, Base):
    """Append-only restrictions share the publication scope's issue epoch."""
    __tablename__ = 'foundation_record_issues'
    issue_id: Mapped[UUID]
    revision: Mapped[int]
    scope_key: Mapped[str] = mapped_column(String(64), index=True)
    target_key: Mapped[str] = mapped_column(String(64))
    official_id: Mapped[UUID | None] = mapped_column(fk('record_official_revisions'))
    state: Mapped[str] = mapped_column(String(16))
    fields_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('issue_id', 'revision'), CheckConstraint(
        "state IN ('suspected','confirmed','resolved','dismissed')", name='record_issue_state'))
