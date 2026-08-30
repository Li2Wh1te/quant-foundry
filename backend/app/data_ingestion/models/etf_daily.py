"""Persistent raw daily ETF market bars."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Numeric, String, Uuid, JSON, UniqueConstraint, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EtfDailyBar(Base):
    """The current authoritative daily bar for one source-assigned ETF code.

    Source codes, rather than local economic identities, form the key because a
    later code-mapping correction must not rewrite the provider's historical bar
    identity. A source correction updates this row in place by design.
    """

    __tablename__ = "etf_daily_bars"
    __table_args__ = (
        CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(btrim(ts_code)) > 0", name="ts_code_not_blank"),
        CheckConstraint("open >= 0", name="open_not_negative"),
        CheckConstraint("high >= 0", name="high_not_negative"),
        CheckConstraint("low >= 0", name="low_not_negative"),
        CheckConstraint("close >= 0", name="close_not_negative"),
        CheckConstraint("vol >= 0", name="volume_not_negative"),
        CheckConstraint("amount >= 0", name="amount_not_negative"),
        CheckConstraint("high >= low", name="high_not_below_low"),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        Index("ix_etf_daily_bars_trade_date_code", "trade_date", "source", "ts_code"),
    )

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    high: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    low: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    vol: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)


class EtfDailyBarRevisionAudit(Base):
    """Append-only evidence of corrections or legacy revision backfills."""

    __tablename__ = "etf_daily_bar_revision_audits"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    previous_source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    batch_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    change_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    changed_fields: Mapped[list] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
    )
    __table_args__ = (
        CheckConstraint("change_kind IN ('correction', 'metadata_backfill')", name="ck_etf_revision_audit_change_kind"),
        UniqueConstraint("source", "ts_code", "trade_date", "source_revision", name="uq_etf_revision_audit_identity"),
        Index("ix_etf_revision_audit_source_date", "source", "trade_date"),
        Index("ix_etf_revision_audit_source_code_date_accepted", "source", "ts_code", "trade_date", "accepted_at"),
        Index("ix_etf_revision_audit_batch_revision", "batch_revision"),
    )
