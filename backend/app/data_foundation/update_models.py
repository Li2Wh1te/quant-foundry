"""Mutable scheduling receipts, separate from immutable publication evidence."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.data_foundation.models import fk


class UpdateVisit(Base):
    """Persist rotation before work, so failures and crashes cannot starve peers.

    Revision zero means a historical job. These receipts never authorize a
    publication and are not used to decide whether a source has been published.
    That decision always follows immutable work-to-release provenance.
    """
    __tablename__ = 'foundation_update_visits'
    observation_id: Mapped[UUID] = mapped_column(
        ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'), primary_key=True)
    execution_id: Mapped[UUID] = mapped_column(fk('execution_manifests'), primary_key=True)
    source_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visits: Mapped[int] = mapped_column(Integer)
    result_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (CheckConstraint('source_revision >= 0 AND visits > 0', name='update_visit_counts'),)


class TableChange(Base):
    """Atomic local row change; ID ordering never claims commit ordering."""
    __tablename__ = 'foundation_table_changes'
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    key_json: Mapped[str] = mapped_column(Text)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    event_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    __table_args__ = (CheckConstraint('before_json IS NOT NULL OR after_json IS NOT NULL', name='table_change_nonempty'),
                      Index('ix_foundation_table_changes_dataset_id', 'dataset', 'id'))


class TableChangeVisit(Base):
    """Fair retry rotation, never a substitute for verified release edges."""
    __tablename__ = 'foundation_table_change_visits'
    change_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('foundation_table_changes.id', ondelete='RESTRICT'), primary_key=True)
    execution_id: Mapped[UUID] = mapped_column(fk('execution_manifests'), primary_key=True)
    visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visits: Mapped[int] = mapped_column(Integer)
    source_ref_id: Mapped[UUID | None] = mapped_column(fk('source_refs'))
    result_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (CheckConstraint('visits > 0', name='table_change_visit_positive'),)
