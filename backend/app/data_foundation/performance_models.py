"""Durable derived evidence for bounded preparation, validation and settlement.

These records never substitute for a publication, execution dependency or live
head/issue gate. Immutable receipts refer to the actual closed input graph.
"""
from datetime import datetime
from uuid import UUID
from sqlalchemy import (CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
                        Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.data_foundation.models import fk


class RecordPreparation(Base):
    __tablename__ = 'foundation_record_preparations'
    work_id: Mapped[UUID] = mapped_column(fk('work'), primary_key=True)
    work_fingerprint: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    page_hashes_json: Mapped[str] = mapped_column(Text)
    duplicate_keys_json: Mapped[str] = mapped_column(Text)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    sealed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('row_count >= 0', name='preparation_count'),)


class RecordPreparedPage(Base):
    __tablename__ = 'foundation_record_prepared_pages'
    work_id: Mapped[UUID] = mapped_column(ForeignKey('foundation_record_preparations.work_id', ondelete='RESTRICT'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text)
    keys_json: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (CheckConstraint('ordinal >= 0 AND row_count BETWEEN 1 AND 200', name='prepared_page_bounds'),)


class BackfillProbe(Base):
    """Rebuildable counters; never a permanent arrival watermark."""
    __tablename__ = 'foundation_backfill_probes'
    scan_id: Mapped[UUID] = mapped_column(fk('intake_scans'), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_count: Mapped[int] = mapped_column(Integer)
    cursor_id: Mapped[UUID | None]
    processed: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('target_count >= processed AND processed >= accepted AND accepted >= 0',
                                    name='backfill_probe_counts'),)


class BackfillReceipt(Base):
    """Append-only settlement edges; quarantine is never an accepted receipt."""
    __tablename__ = 'foundation_backfill_receipts'
    scan_id: Mapped[UUID] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(primary_key=True)
    quality: Mapped[str] = mapped_column(String(16), primary_key=True)
    release_id: Mapped[UUID] = mapped_column(fk('official_releases'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(['scan_id', 'scope_key'], ['foundation_backfill_probes.scan_id', 'foundation_backfill_probes.scope_key'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['scan_id', 'observation_id'], ['foundation_intake_scan_targets.scan_id', 'foundation_intake_scan_targets.observation_id'], ondelete='RESTRICT'),
        CheckConstraint("quality IN ('processed', 'accepted')", name='backfill_receipt_quality'),
    )


class BackfillCompletion(Base):
    """A fixed-denominator completion seal, checked against real receipt edges."""
    __tablename__ = 'foundation_backfill_completions'
    scan_id: Mapped[UUID] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    quality: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_count: Mapped[int] = mapped_column(Integer)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(['scan_id', 'scope_key'], ['foundation_backfill_probes.scan_id', 'foundation_backfill_probes.scope_key'], ondelete='RESTRICT'),
        CheckConstraint("quality IN ('processed', 'accepted') AND target_count >= 0", name='backfill_completion_quality'),
    )


class TableSettlementVisit(Base):
    __tablename__ = 'foundation_table_settlement_visits'
    capture_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    native_dataset: Mapped[str] = mapped_column(String(80))
    source_ids_json: Mapped[str] = mapped_column(Text)
    cursor_id: Mapped[UUID | None]
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TableSettlementReceipt(Base):
    __tablename__ = 'foundation_table_settlement_receipts'
    capture_ref_id: Mapped[UUID] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'), primary_key=True)
    release_id: Mapped[UUID] = mapped_column(fk('official_releases'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(['capture_ref_id', 'scope_key'],
        ['foundation_table_settlement_visits.capture_ref_id', 'foundation_table_settlement_visits.scope_key'], ondelete='RESTRICT'),)


class BackfillResumeWatch(Base):
    """An explicit operator opt-in, not a guess about why a task is paused."""
    __tablename__ = 'foundation_backfill_resume_watches'
    task_id: Mapped[UUID] = mapped_column(ForeignKey('scheduled_tasks.id', ondelete='RESTRICT'), primary_key=True)
    task_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    parameters_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("task_version > 0 AND state IN ('waiting','activated','resumed','cancelled')", name='resume_watch_state'),)
