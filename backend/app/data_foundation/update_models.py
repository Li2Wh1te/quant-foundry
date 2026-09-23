"""Mutable scheduling receipts, separate from immutable publication evidence."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text
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
