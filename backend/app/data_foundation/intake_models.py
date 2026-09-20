"""Bounded intake ledgers separate source occurrences from processing progress.

A scan cursor belongs to one finite pass. It is never reused as a permanent
high-water mark: a later pass must revisit smaller IDs committed late.
"""
from datetime import datetime
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.data_foundation.models import Record, fk


class IntakeScope(Record, Base):
    __tablename__ = 'foundation_intake_scopes'
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    dataset: Mapped[str] = mapped_column(String(80))
    subject: Mapped[str] = mapped_column(String(64))
    variant: Mapped[str] = mapped_column(String(80))
    selector_json: Mapped[str] = mapped_column(Text)
    decoder_id: Mapped[UUID] = mapped_column(fk('execution_manifests'))


class IntakeControl(Base):
    __tablename__ = 'foundation_intake_controls'
    scope_id: Mapped[UUID] = mapped_column(fk('intake_scopes'), primary_key=True)
    paused: Mapped[bool] = mapped_column(default=True)


class Scan(Record, Base):
    __tablename__ = 'foundation_intake_scans'
    scope_id: Mapped[UUID] = mapped_column(fk('intake_scopes'), index=True)
    event_key: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default='full')
    status: Mapped[str] = mapped_column(String(16), default='running')
    cursor_id: Mapped[UUID | None]
    seen: Mapped[int] = mapped_column(Integer, default=0)
    registered: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint('scope_id', 'event_key'),
        CheckConstraint("mode IN ('full','recent')", name='scan_mode'),
        CheckConstraint("status IN ('running','failed','completed')", name='scan_status'),
        CheckConstraint('seen >= registered AND registered >= 0', name='scan_counts'))


class IntakeItem(Record, Base):
    __tablename__ = 'foundation_intake_items'
    scope_id: Mapped[UUID] = mapped_column(fk('intake_scopes'))
    observation_id: Mapped[UUID] = mapped_column(ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'))
    source_ref_id: Mapped[UUID] = mapped_column(fk('source_refs'))
    first_scan_id: Mapped[UUID] = mapped_column(fk('intake_scans'))
    __table_args__ = (UniqueConstraint('scope_id', 'observation_id'),)


class ScanEntry(Base):
    __tablename__ = 'foundation_intake_scan_entries'
    scan_id: Mapped[UUID] = mapped_column(fk('intake_scans'), primary_key=True)
    item_id: Mapped[UUID] = mapped_column(fk('intake_items'), primary_key=True)


class SourcePointer(Record, Base):
    """Observed current pointers, without inventing historical commit order."""
    __tablename__ = 'foundation_intake_pointers'
    scope_id: Mapped[UUID] = mapped_column(fk('intake_scopes'))
    scan_id: Mapped[UUID] = mapped_column(fk('intake_scans'))
    revision: Mapped[int] = mapped_column(Integer)
    observation_id: Mapped[UUID | None] = mapped_column(ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'))
    source_status: Mapped[str] = mapped_column(String(24))
    __table_args__ = (UniqueConstraint('scan_id', 'revision'),)
