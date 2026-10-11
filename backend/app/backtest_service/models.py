"""Three small S3 control objects. Existing business/history tables stay intact."""
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CONTROL_JSON = JSON().with_variant(JSONB, "postgresql")


class RunBatch(Base):
    __tablename__ = "s3_run_batches"
    __table_args__ = (UniqueConstraint("owner", "idempotency_key", name="s3_batch_owner_key"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "s3_runs"
    __table_args__ = (
        UniqueConstraint("owner", "idempotency_key", name="s3_run_owner_key"),
        CheckConstraint("status IN ('queued','starting','running','succeeded','failed','cancelled')", name="s3_run_status"),
        CheckConstraint("fence >= 0", name="s3_run_fence"),
        Index("ix_s3_runs_queue", "status", "created_at", "id"),
        Index("ix_s3_runs_owner", "owner", "created_at", "id"),
        Index("ix_s3_runs_batch", "batch_id", "batch_index", "created_at"),
        ForeignKeyConstraint(["account_id", "account_version"], ["backtest_account_profile_versions.profile_id", "backtest_account_profile_versions.version"], ondelete="RESTRICT", name="s3_run_account_version"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    # Real restrictive FKs additionally protect references during concurrent CRUD.
    account_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("backtest_account_profiles.id", ondelete="RESTRICT"), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_revision_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("strategy_revisions.id", ondelete="RESTRICT"), nullable=False)
    batch_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("s3_run_batches.id", ondelete="RESTRICT"))
    batch_index: Mapped[int | None] = mapped_column(Integer)
    retry_of: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("s3_runs.id", ondelete="RESTRICT"))
    config: Mapped[dict[str, Any]] = mapped_column(CONTROL_JSON, nullable=False)
    accepted: Mapped[dict[str, Any]] = mapped_column(CONTROL_JSON, nullable=False)
    strategy_source: Mapped[str] = mapped_column(Text, nullable=False)
    limits: Mapped[dict] = mapped_column(CONTROL_JSON, nullable=False)
    group_policy: Mapped[dict] = mapped_column(CONTROL_JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token: Mapped[str | None] = mapped_column(String(32))
    supervisor_id: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    attempt_identity: Mapped[list[int] | None] = mapped_column(CONTROL_JSON)
    outcome: Mapped[dict | None] = mapped_column(CONTROL_JSON)
    progress: Mapped[dict | None] = mapped_column(CONTROL_JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunResult(Base):
    __tablename__ = "s3_run_results"
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("s3_runs.id", ondelete="RESTRICT"), primary_key=True)
    # D11's bounded manifest and shards live under the server-owned result root.
    directory: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
