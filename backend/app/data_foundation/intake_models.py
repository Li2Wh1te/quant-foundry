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
        CheckConstraint("mode IN ('full','recent','frozen')", name='scan_mode'),
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


class ScanTarget(Base):
    """Immutable membership captured before a full-local scan starts.

    Capture only version identities and hashes here; materialization remains
    bounded by the existing intake worker. A frozen scan never consults latest
    to select its inputs, including after a process restart.
    """
    __tablename__ = 'foundation_intake_scan_targets'
    scan_id: Mapped[UUID] = mapped_column(fk('intake_scans'), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'), primary_key=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64))


class ScanFailure(Base):
    """A failed fixed object stays in the denominator and is retried next pass."""
    __tablename__ = 'foundation_intake_scan_failures'
    scan_id: Mapped[UUID] = mapped_column(fk('intake_scans'), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'), primary_key=True)
    error_code: Mapped[str] = mapped_column(String(64))


class ScanSeal(Base):
    """Sealing forbids later membership inserts, including into empty captures."""
    __tablename__ = 'foundation_intake_scan_seals'
    scan_id: Mapped[UUID] = mapped_column(fk('intake_scans'), primary_key=True)
    target_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint('target_count >= 0', name='target_count'),)


class IntakeCampaign(Record, Base):
    """A finite source-key denominator; each linked scan has an exact cutoff."""
    __tablename__ = 'foundation_intake_campaigns'
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    decoder_id: Mapped[UUID] = mapped_column(fk('execution_manifests'))
    datasets_json: Mapped[str] = mapped_column(Text)


class CampaignScan(Base):
    __tablename__ = 'foundation_intake_campaign_scans'
    campaign_id: Mapped[UUID] = mapped_column(fk('intake_campaigns'), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80), primary_key=True)
    scan_id: Mapped[UUID] = mapped_column(fk('intake_scans'), unique=True)
