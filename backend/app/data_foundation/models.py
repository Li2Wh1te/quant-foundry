"""M2 source/dependency records. Sealed evidence is append-only in PostgreSQL.

Foreign keys encode actual retention dependencies; JSON documents carry only
schema-versioned definitions, complete source rows, or non-secret metadata.
"""
from datetime import date, datetime
from uuid import UUID, uuid4
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


def fk(table):
    return ForeignKey('foundation_' + table + '.id', ondelete='RESTRICT')


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Definition(Record, Base):
    """Versioned contract/series/policy/support definitions; kind is explicit."""
    __tablename__ = 'foundation_definitions'
    __table_args__ = (UniqueConstraint('kind', 'name', 'version'),
        CheckConstraint("kind IN ('contract','series','policy','support')", name='definition_kind'))
    kind: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    definition_json: Mapped[str] = mapped_column(Text)


class Artifact(Record, Base):
    __tablename__ = 'foundation_artifacts'
    __table_args__ = (CheckConstraint('length(payload) <= 16777216', name='artifact_size'),)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class Execution(Record, Base):
    __tablename__ = 'foundation_execution_manifests'
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    artifact_id: Mapped[UUID | None] = mapped_column(fk('artifacts'))
    # A digest is not an archive. Append new verification evidence separately;
    # workers also verify their own allow-listed executable before claiming work.
    replay_status: Mapped[str] = mapped_column(String(32))
    __table_args__ = (CheckConstraint("replay_status IN ('ready','dependency_missing')", name='execution_replay'),)


class Baseline(Record, Base):
    __tablename__ = 'foundation_baselines'
    event_key: Mapped[str] = mapped_column(String(128))
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(32))
    dataset: Mapped[str] = mapped_column(String(128))
    scope_json: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint('source', 'dataset', 'event_key'), CheckConstraint('row_count >= 0', name='baseline_count'),)


class BaselineBlock(Base):
    __tablename__ = 'foundation_baseline_blocks'
    baseline_id: Mapped[UUID] = mapped_column(fk('baselines'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint('row_count >= 0 AND ordinal >= 0', name='block_count'),)


class SourceRef(Record, Base):
    __tablename__ = 'foundation_source_refs'
    __table_args__ = (UniqueConstraint('source', 'representation', 'locator_hash'),
        CheckConstraint("(representation = 'ths_observation' AND observation_id IS NOT NULL AND baseline_id IS NULL) OR (representation = 'local_table_baseline' AND baseline_id IS NOT NULL AND observation_id IS NULL)", name='source_locator'))
    source: Mapped[str] = mapped_column(String(32))
    dataset: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str] = mapped_column(String(64))
    variant: Mapped[str] = mapped_column(String(80))
    representation: Mapped[str] = mapped_column(String(32))
    locator_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_id: Mapped[UUID | None] = mapped_column(ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'))
    baseline_id: Mapped[UUID | None] = mapped_column(fk('baselines'))
    decoder_id: Mapped[UUID] = mapped_column(fk('execution_manifests'))


class ObservationDependency(Base):
    __tablename__ = 'foundation_observation_dependencies'
    source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'), primary_key=True)


class Binding(Record, Base):
    __tablename__ = 'foundation_source_bindings'
    source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'))
    source: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey('instruments.id', ondelete='RESTRICT'))
    valid_from: Mapped[date]
    valid_to: Mapped[date]
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    binding_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24))
    evidence_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('source_ref_id', 'subject', 'binding_version'),
        CheckConstraint('valid_to > valid_from', name='binding_interval'),
        CheckConstraint("status IN ('resolved','unresolved')", name='binding_status'))


class DependencyManifest(Record, Base):
    __tablename__ = 'foundation_dependency_manifests'
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)


class DependencyEntry(Base):
    __tablename__ = 'foundation_dependency_entries'
    manifest_id: Mapped[UUID] = mapped_column(fk('dependency_manifests'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    source_ref_id: Mapped[UUID | None] = mapped_column(fk('source_refs'))
    binding_id: Mapped[UUID | None] = mapped_column(fk('source_bindings'))
    execution_id: Mapped[UUID | None] = mapped_column(fk('execution_manifests'))
    definition_id: Mapped[UUID | None] = mapped_column(fk('definitions'))
    content_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(128))
    __table_args__ = (CheckConstraint('(CASE WHEN source_ref_id IS NULL THEN 0 ELSE 1 END + CASE WHEN binding_id IS NULL THEN 0 ELSE 1 END + CASE WHEN execution_id IS NULL THEN 0 ELSE 1 END + CASE WHEN definition_id IS NULL THEN 0 ELSE 1 END) = 1', name='one_dependency'),)

from sqlalchemy import Index as _Index, text as _text
_Index('ix_foundation_source_refs_observation_lookup', SourceRef.observation_id,
       postgresql_where=_text('observation_id IS NOT NULL'))
