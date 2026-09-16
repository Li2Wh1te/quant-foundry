"""Durable work and typed publication records; current projections stay separate."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.data_foundation.models import Record, fk


class Work(Record, Base):
    __tablename__ = 'foundation_work'
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(1))
    contract_id: Mapped[UUID] = mapped_column(fk('definitions'))
    execution_id: Mapped[UUID] = mapped_column(fk('execution_manifests'))
    dependency_id: Mapped[UUID] = mapped_column(fk('dependency_manifests'))
    # All input choices, including source/parent/series, are fixed here and have
    # typed retention edges in the input dependency or candidate manifest.
    parameters_json: Mapped[str] = mapped_column(Text)
    scope_key: Mapped[str] = mapped_column(String(64), index=True)
    source_ref_id: Mapped[UUID | None] = mapped_column(fk('source_refs'))
    candidate_manifest_id: Mapped[UUID | None] = mapped_column(fk('candidate_manifests'))
    parent_release_id: Mapped[UUID | None] = mapped_column(fk('official_releases'))
    policy_id: Mapped[UUID | None] = mapped_column(fk('definitions'))
    expected_head_revision: Mapped[int] = mapped_column(Integer, default=0)
    expected_issue_epoch: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int | None] = mapped_column(Integer)
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (
        CheckConstraint("kind IN ('A','B')", name='work_kind'),
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled','dependency_missing','superseded')", name='work_status'),
        CheckConstraint('cursor >= 0 AND lease_epoch >= 0 AND (total IS NULL OR total >= cursor)', name='work_counts'),
        CheckConstraint("(kind = 'A' AND source_ref_id IS NOT NULL AND candidate_manifest_id IS NULL AND policy_id IS NULL) OR (kind = 'B' AND source_ref_id IS NULL AND candidate_manifest_id IS NOT NULL AND policy_id IS NOT NULL)", name='work_input'))


class Attempt(Record, Base):
    __tablename__ = 'foundation_work_attempts'
    work_id: Mapped[UUID] = mapped_column(fk('work'))
    epoch: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    details_json: Mapped[str] = mapped_column(Text, default='{}')
    __table_args__ = (UniqueConstraint('work_id', 'epoch', 'outcome'),)


class Unit(Base):
    __tablename__ = 'foundation_work_units'
    work_id: Mapped[UUID] = mapped_column(fk('work'), primary_key=True)
    unit_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    result_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint('row_count >= 0', name='unit_count'),)


class WorkEvent(Record, Base):
    __tablename__ = 'foundation_work_events'
    work_id: Mapped[UUID] = mapped_column(fk('work'))
    sequence: Mapped[int] = mapped_column(Integer)
    step: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('work_id', 'sequence'),)


class Assessment(Record, Base):
    __tablename__ = 'foundation_assessments'
    input_hash: Mapped[str] = mapped_column(String(64))
    rule_hash: Mapped[str] = mapped_column(String(64))
    scope_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    results_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('input_hash', 'rule_hash', 'scope_hash'),
        CheckConstraint("status IN ('pass','fail','unknown','not_applicable')", name='assessment_status'))


class Candidate(Record, Base):
    __tablename__ = 'foundation_candidates'
    work_id: Mapped[UUID] = mapped_column(fk('work'))
    source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'))
    binding_id: Mapped[UUID | None] = mapped_column(fk('source_bindings'))
    dependency_id: Mapped[UUID] = mapped_column(fk('dependency_manifests'))
    unit_key: Mapped[str] = mapped_column(String(128))
    occurrence: Mapped[int] = mapped_column(Integer)
    values_hash: Mapped[str] = mapped_column(String(64))
    assessment_id: Mapped[UUID] = mapped_column(fk('assessments'))
    readiness: Mapped[str] = mapped_column(String(24))
    __table_args__ = (UniqueConstraint('work_id', 'unit_key', 'occurrence'),
        CheckConstraint("readiness IN ('pending','ready','quarantined')", name='candidate_readiness'),
        CheckConstraint("readiness <> 'ready' OR binding_id IS NOT NULL", name='candidate_binding'))


class BarFields:
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey('instruments.id', ondelete='RESTRICT'))
    trade_date: Mapped[date]
    series: Mapped[str] = mapped_column(String(128))
    open: Mapped[Decimal] = mapped_column(Numeric(28,10))
    high: Mapped[Decimal] = mapped_column(Numeric(28,10))
    low: Mapped[Decimal] = mapped_column(Numeric(28,10))
    close: Mapped[Decimal] = mapped_column(Numeric(28,10))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(32,8))
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(32,8))


BAR_CHECK = "open > 0 AND low > 0 AND high >= open AND high >= close AND low <= open AND low <= close AND open < 'Infinity' AND high < 'Infinity' AND low < 'Infinity' AND close < 'Infinity' AND (volume IS NULL OR (volume >= 0 AND volume < 'Infinity')) AND (turnover IS NULL OR (turnover >= 0 AND turnover < 'Infinity'))"


class CandidateBar(BarFields, Base):
    __tablename__ = 'foundation_bar_candidates'
    candidate_id: Mapped[UUID] = mapped_column(fk('candidates'), primary_key=True)
    __table_args__ = (CheckConstraint(BAR_CHECK, name='candidate_bar_values'),)


class CandidateManifest(Record, Base):
    __tablename__ = 'foundation_candidate_manifests'
    work_id: Mapped[UUID] = mapped_column(fk('work'), unique=True)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)


class CandidateEntry(Base):
    __tablename__ = 'foundation_candidate_entries'
    manifest_id: Mapped[UUID] = mapped_column(fk('candidate_manifests'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[UUID] = mapped_column(fk('candidates'))
    __table_args__ = (UniqueConstraint('manifest_id', 'candidate_id'),)


class Decision(Record, Base):
    __tablename__ = 'foundation_decisions'
    assessment_id: Mapped[UUID] = mapped_column(fk('assessments'))
    work_id: Mapped[UUID] = mapped_column(fk('work'))
    target_key: Mapped[str] = mapped_column(String(128))
    candidate_manifest_id: Mapped[UUID] = mapped_column(fk('candidate_manifests'))
    selected_candidate_id: Mapped[UUID | None] = mapped_column(fk('candidates'))
    parent_official_id: Mapped[UUID | None] = mapped_column(fk('bar_official_revisions'))
    action: Mapped[str] = mapped_column(String(16))
    evidence_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('work_id', 'target_key'),
        CheckConstraint("(action = 'select' AND selected_candidate_id IS NOT NULL AND parent_official_id IS NULL) OR (action = 'retain' AND selected_candidate_id IS NULL AND parent_official_id IS NOT NULL) OR (action IN ('gap','block','withdraw') AND selected_candidate_id IS NULL AND parent_official_id IS NULL)", name='decision_action'))


class OfficialBar(Record, BarFields, Base):
    __tablename__ = 'foundation_bar_official_revisions'
    decision_id: Mapped[UUID] = mapped_column(fk('decisions'), unique=True)
    candidate_id: Mapped[UUID] = mapped_column(fk('candidates'))
    values_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (CheckConstraint(BAR_CHECK, name='official_bar_values'),)


class Release(Record, Base):
    __tablename__ = 'foundation_official_releases'
    work_id: Mapped[UUID] = mapped_column(fk('work'), unique=True)
    scope_key: Mapped[str] = mapped_column(String(64), index=True)
    parent_id: Mapped[UUID | None] = mapped_column(fk('official_releases'))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("status IN ('draft','sealed','published','abandoned')", name='release_status'),)


class ReleaseBlock(Record, Base):
    __tablename__ = 'foundation_release_blocks'
    partition_key: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)


class BlockMember(Base):
    __tablename__ = 'foundation_block_members'
    block_id: Mapped[UUID] = mapped_column(fk('release_blocks'), primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey('instruments.id', ondelete='RESTRICT'), primary_key=True)
    trade_date: Mapped[date] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    official_id: Mapped[UUID | None] = mapped_column(fk('bar_official_revisions'))
    decision_id: Mapped[UUID] = mapped_column(fk('decisions'))
    __table_args__ = (CheckConstraint("(state = 'value' AND official_id IS NOT NULL) OR (state IN ('gap','blocked','withdrawn') AND official_id IS NULL)", name='member_state'),)


class BlockRef(Base):
    __tablename__ = 'foundation_release_block_refs'
    release_id: Mapped[UUID] = mapped_column(fk('official_releases'), primary_key=True)
    partition_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    block_id: Mapped[UUID] = mapped_column(fk('release_blocks'))


class Head(Base):
    __tablename__ = 'foundation_heads'
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    release_id: Mapped[UUID] = mapped_column(fk('official_releases'))
    revision: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint('revision >= 1', name='head_revision'),)


class IssueScope(Base):
    __tablename__ = 'foundation_issue_scopes'
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer, default=0)


class Issue(Record, Base):
    __tablename__ = 'foundation_issue_revisions'
    issue_id: Mapped[UUID]
    revision: Mapped[int] = mapped_column(Integer)
    scope_key: Mapped[str] = mapped_column(ForeignKey('foundation_issue_scopes.scope_key', ondelete='RESTRICT'))
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey('instruments.id', ondelete='RESTRICT'))
    start: Mapped[date]
    end: Mapped[date]
    official_id: Mapped[UUID | None] = mapped_column(fk('bar_official_revisions'))
    state: Mapped[str] = mapped_column(String(16))
    fields_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), default='error')
    evidence_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('issue_id', 'revision'), CheckConstraint('"end" >= start', name='issue_interval'),
        CheckConstraint("state IN ('suspected','confirmed','resolved','dismissed')", name='issue_state'))


class RuntimeArchive(Record, Base):
    """Verified Docker save/load evidence, retained independently of work state."""
    __tablename__ = 'foundation_runtime_archives'
    image_digest: Mapped[str] = mapped_column(String(80), unique=True)
    archive_hash: Mapped[str] = mapped_column(String(64))
    archive_key: Mapped[str] = mapped_column(String(128))
    byte_count: Mapped[int] = mapped_column(Integer)
    verification_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (CheckConstraint('byte_count > 0', name='archive_size'),)


class ExecutionArchive(Base):
    __tablename__ = 'foundation_execution_archives'
    execution_id: Mapped[UUID] = mapped_column(fk('execution_manifests'), primary_key=True)
    archive_id: Mapped[UUID] = mapped_column(fk('runtime_archives'))


class WorkCoverage(Base):
    """Immutable applicability evidence, retained independently of source readers."""
    __tablename__ = 'foundation_work_coverage'
    work_id: Mapped[UUID] = mapped_column(fk('work'), primary_key=True)
    assessment_id: Mapped[UUID] = mapped_column(fk('assessments'))
