"""Persistent private strategy drafts and immutable published revisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Strategy(Base):
    """One private strategy identity owned by a self-hosted deployment.

    The current revision pointer is intentionally part of the strategy record
    rather than a mutable flag on revisions.  A composite foreign key ensures
    that the pointer can only reference a revision created for this strategy,
    so a revision from another strategy cannot accidentally become current.
    """

    __tablename__ = "strategies"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint(
            "state IN ('active', 'archived')", name="state_supported"
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        ForeignKeyConstraint(
            ["id", "current_revision_id"],
            ["strategy_revisions.strategy_id", "strategy_revisions.id"],
            name="current_revision_belongs_to_strategy",
            ondelete="RESTRICT",
        ),
        Index("ix_strategies_state_updated_at", "state", text("updated_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    current_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StrategyDraft(Base):
    """The one mutable working copy for a private strategy.

    Source text remains in PostgreSQL instead of a checkout-visible directory.
    The draft's optimistic-lock version is independent from the strategy's
    metadata version so editors can detect a stale source save precisely.
    """

    __tablename__ = "strategy_drafts"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(source_code)) > 0", name="source_code_not_blank"
        ),
        CheckConstraint(
            "octet_length(source_code) <= 1048576", name="source_code_size"
        ),
        CheckConstraint(
            "source_hash ~ '^[0-9a-f]{64}$'", name="source_hash_sha256"
        ),
        CheckConstraint(
            "jsonb_typeof(parameter_schema) = 'object'",
            name="parameter_schema_object",
        ),
        CheckConstraint(
            "jsonb_typeof(default_parameters) = 'object'",
            name="default_parameters_object",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    )

    strategy_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("strategies.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_code: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    default_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StrategyRevision(Base):
    """An append-only strategy source snapshot published for execution.

    Every field needed to interpret a strategy is snapshotted here: source,
    parameter contract, defaults, and runtime manifest.  The database migration
    adds a trigger that rejects updates and deletes, preventing later edits from
    changing what a historical task run means.
    """

    __tablename__ = "strategy_revisions"
    __table_args__ = (
        CheckConstraint("revision_number > 0", name="revision_number_positive"),
        CheckConstraint(
            "length(btrim(source_code)) > 0", name="source_code_not_blank"
        ),
        CheckConstraint(
            "octet_length(source_code) <= 1048576", name="source_code_size"
        ),
        CheckConstraint(
            "source_hash ~ '^[0-9a-f]{64}$'", name="source_hash_sha256"
        ),
        CheckConstraint(
            "jsonb_typeof(parameter_schema) = 'object'",
            name="parameter_schema_object",
        ),
        CheckConstraint(
            "jsonb_typeof(default_parameters) = 'object'",
            name="default_parameters_object",
        ),
        CheckConstraint(
            "jsonb_typeof(runtime_manifest) = 'object'",
            name="runtime_manifest_object",
        ),
        UniqueConstraint(
            "strategy_id", "revision_number", name="strategy_revision_number"
        ),
        # PostgreSQL requires the exact referenced column set to be unique for
        # Strategy.current_revision_id's ownership-enforcing composite foreign key.
        UniqueConstraint("strategy_id", "id", name="strategy_id_id"),
        Index(
            "ix_strategy_revisions_strategy_revision",
            "strategy_id",
            text("revision_number DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("strategies.id", ondelete="RESTRICT"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    source_code: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    default_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runtime_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
