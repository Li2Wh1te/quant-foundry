"""Immutable evidence for frozen roots and bounded full-record validation."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.data_foundation.models import fk


class RecordValidationRoot(Base):
    """Freeze a complete draft reference list before cross-transaction checks."""
    __tablename__ = 'foundation_record_validation_roots'
    release_id: Mapped[UUID] = mapped_column(fk('official_releases'), primary_key=True)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    ref_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('ref_count >= 0', name='validation_root_count'),)


class RecordBlockPageVerification(Base):
    """Full member/value/reference checks for one bounded immutable block page."""
    __tablename__ = 'foundation_record_block_page_verifications'
    block_id: Mapped[UUID] = mapped_column(fk('release_blocks'), primary_key=True)
    validator_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64))
    schema_key: Mapped[str] = mapped_column(String(128))
    after_key: Mapped[str | None] = mapped_column(String(64))
    last_key: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    governance_work_ids_json: Mapped[str] = mapped_column(Text)
    normalization_work_ids_json: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('ordinal >= 0 AND row_count BETWEEN 1 AND 200', name='block_page_bounds'),)
