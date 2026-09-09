"""Persist public fields separately from authenticated encrypted credentials."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DataSourceConfig(Base):
    __tablename__ = "data_source_configs"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    # The row is seeded by the migration. Initialization is locked and performed
    # once at startup, including installations that have no legacy credentials.
    initialized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    values: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    encrypted_secrets: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_status: Mapped[str] = mapped_column(String(32), default="not_checked", nullable=False)
    check_message: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
