"""Persistent checkpoints shared by data synchronization workflows."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, SmallInteger, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DataSyncCheckpoint(Base):
    """The last committed position for one logical data synchronization scope."""

    __tablename__ = "data_sync_checkpoints"
    __table_args__ = (
        CheckConstraint("length(btrim(sync_key)) > 0", name="sync_key_not_blank"),
        CheckConstraint("length(btrim(scope_key)) > 0", name="scope_key_not_blank"),
        CheckConstraint("jsonb_typeof(cursor) = 'object'", name="cursor_object"),
        CheckConstraint("cursor_version > 0", name="cursor_version_positive"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    )

    sync_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    cursor_version: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default="1"
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
