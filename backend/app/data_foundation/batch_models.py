"""Typed retention edges for business batches and sealed multi-work inputs."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.data_foundation.models import Record, fk


class Batch(Record, Base):
    __tablename__ = 'foundation_batches'
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[str] = mapped_column(Text)
    scope_key: Mapped[str] = mapped_column(String(64), index=True)


class BatchInput(Base):
    __tablename__ = 'foundation_batch_inputs'
    batch_id: Mapped[UUID] = mapped_column(fk('batches'), primary_key=True)
    source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'), primary_key=True)


class BatchWork(Base):
    __tablename__ = 'foundation_batch_work'
    batch_id: Mapped[UUID] = mapped_column(fk('batches'), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(fk('work'), primary_key=True)


class BatchControl(Base):
    __tablename__ = 'foundation_batch_controls'
    batch_id: Mapped[UUID] = mapped_column(fk('batches'), primary_key=True)
    pause_a: Mapped[bool] = mapped_column(default=True)
    pause_b: Mapped[bool] = mapped_column(default=True)
    allow_publish: Mapped[bool] = mapped_column(default=False)


class CandidateInputSet(Record, Base):
    __tablename__ = 'foundation_candidate_input_sets'
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scope_key: Mapped[str] = mapped_column(String(64))
    entry_count: Mapped[int]
    __table_args__ = (CheckConstraint('entry_count > 0 AND entry_count <= 100', name='input_set_count'),)


class CandidateInputEntry(Base):
    __tablename__ = 'foundation_candidate_input_entries'
    input_set_id: Mapped[UUID] = mapped_column(fk('candidate_input_sets'), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(fk('candidate_manifests'), primary_key=True)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(256))
    __table_args__ = (CheckConstraint("role IN ('eligible','historical')", name='input_role'),)


class ReleaseContribution(Base):
    __tablename__ = 'foundation_release_contributions'
    release_id: Mapped[UUID] = mapped_column(fk('official_releases'), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(fk('work'), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    __table_args__ = (CheckConstraint("role IN ('normalization','governance','inherited')", name='contribution_role'),)


class Reconciliation(Record, Base):
    __tablename__ = 'foundation_reconciliations'
    batch_id: Mapped[UUID] = mapped_column(fk('batches'), index=True)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    evidence_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    __table_args__ = (UniqueConstraint('batch_id', 'evidence_hash'),
        CheckConstraint("status IN ('explained','unexplained','pending')", name='reconciliation_status'))


class BatchDisposition(Record, Base):
    """Explicit restricted settlement; never an invented successful candidate."""
    __tablename__ = 'foundation_batch_dispositions'
    batch_id: Mapped[UUID] = mapped_column(fk('batches'))
    source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'))
    reason_code: Mapped[str] = mapped_column(String(48))
    evidence_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('batch_id','source_ref_id'),
        CheckConstraint("reason_code IN ('SEMANTIC_DEPENDENCY_MISSING','OUT_OF_SCOPE')", name='disposition_reason'))


class WorkSourcePointer(Base):
    """Current-state prerequisites for an automatic multi-input publication."""
    __tablename__ = 'foundation_work_source_pointers'
    work_id: Mapped[UUID] = mapped_column(fk('work'), primary_key=True)
    source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'), primary_key=True)
    revision: Mapped[int]


class MaintenancePlan(Record, Base):
    __tablename__ = 'foundation_maintenance_plans'
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    dependency_id: Mapped[UUID] = mapped_column(fk('dependency_manifests'))
    evidence_json: Mapped[str] = mapped_column(Text)


class MaintenanceTarget(Base):
    __tablename__ = 'foundation_maintenance_targets'
    plan_id: Mapped[UUID] = mapped_column(fk('maintenance_plans'), primary_key=True)
    release_id: Mapped[UUID] = mapped_column(fk('official_releases'), primary_key=True)
    reason_json: Mapped[str] = mapped_column(Text)
