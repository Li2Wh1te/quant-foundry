"""Source-local Tonghuashun inventory and immutable acquisition versions.

These tables intentionally have no foreign key to Tushare or a shared instrument
identity. A response version is evidence of what was observed, not evidence that
its contents were historically available on a report's period-end date.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TonghuashunTicker(Base):
    __tablename__ = "tonghuashun_tickers"

    thscode: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(256))
    exchange: Mapped[str | None] = mapped_column(String(16))
    # Preserve exact provider JSON, including nulls and numbers. No implicit
    # float conversion or invented listing-status field is introduced here.
    raw_json: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TonghuashunObservation(Base):
    __tablename__ = "tonghuashun_observations"
    __table_args__ = (
        Index("ix_tonghuashun_observations_scope_time", "dataset", "subject", "variant", "observed_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dataset: Mapped[str] = mapped_column(String(80))
    subject: Mapped[str] = mapped_column(String(64))
    variant: Mapped[str] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_json: Mapped[str] = mapped_column(Text)
    data_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)


class TonghuashunCollectionState(Base):
    """One resumable subject, with a last-good version separate from errors."""

    __tablename__ = "tonghuashun_collection_states"

    dataset: Mapped[str] = mapped_column(String(80), primary_key=True)
    subject: Mapped[str] = mapped_column(String(64), primary_key=True)
    variant: Mapped[str] = mapped_column(String(80), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    observation_id: Mapped[UUID | None] = mapped_column(ForeignKey("tonghuashun_observations.id"))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_kind: Mapped[str | None] = mapped_column(String(40))


class TonghuashunRequestBudget(Base):
    """One database-wide pacing slot shared by scheduler worker processes."""

    __tablename__ = "tonghuashun_request_budget"
    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
