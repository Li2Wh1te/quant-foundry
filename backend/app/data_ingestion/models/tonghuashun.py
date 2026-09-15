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
    # Time-series versions may reference a short immutable delta chain. A full
    # anchor is emitted regularly, bounding reconstruction cost without pruning
    # old evidence or duplicating ten years of bars after each daily update.
    base_observation_id: Mapped[UUID | None] = mapped_column(ForeignKey("tonghuashun_observations.id"))
    chain_depth: Mapped[int] = mapped_column(Integer, default=0)


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


class TonghuashunDumpImport(Base):
    """A leased, resumable staging slot; never a credential or signed URL store."""

    __tablename__ = 'tonghuashun_dump_imports'
    dataset: Mapped[str] = mapped_column(String(80), primary_key=True)
    generation: Mapped[UUID] = mapped_column(default=uuid4)
    status: Mapped[str] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    digest: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[str] = mapped_column(Text, default='{}')
    total_subjects: Mapped[int] = mapped_column(Integer, default=0)
    imported_subjects: Mapped[int] = mapped_column(Integer, default=0)
    superseded_subjects: Mapped[int] = mapped_column(Integer, default=0)


class TonghuashunDumpStage(Base):
    """Validated rows grouped on disk first, then stored for worker restarts."""

    __tablename__ = 'tonghuashun_dump_stages'
    dataset: Mapped[str] = mapped_column(ForeignKey('tonghuashun_dump_imports.dataset'), primary_key=True)
    subject: Mapped[str] = mapped_column(String(64), primary_key=True)
    data_json: Mapped[str] = mapped_column(Text)


class TonghuashunWorkUnit(Base):
    """Unpublished request results; scopes include the source head revision."""
    __tablename__ = 'tonghuashun_work_units'
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    data_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
