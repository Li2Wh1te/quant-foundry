"""Persistent trading calendar model."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Index, String, JSON, Uuid, func, text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TradingCalendarDay(Base):
    """One exchange's trading status for one calendar date."""

    __tablename__ = "trading_calendar_days"
    __table_args__ = (
        CheckConstraint("length(btrim(exchange)) > 0", name="exchange_not_blank"),
        CheckConstraint(
            "previous_trading_date IS NULL OR previous_trading_date < calendar_date",
            name="previous_date_before_calendar_date",
        ),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        Index(
            "ix_trading_calendar_days_open_date",
            "exchange",
            text("calendar_date DESC"),
            postgresql_where=text("is_open"),
        ),
    )

    exchange: Mapped[str] = mapped_column(String(16), primary_key=True)
    calendar_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean)
    previous_trading_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TradingStatusSourceFact(Base):
    """Immutable raw response snapshot for one suspend_d request."""

    __tablename__ = "trading_status_source_facts"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False, default="suspend_d")
    query_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    query_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "source", "endpoint", "query_kind", "query_value", "source_hash",
            name="uq_trading_status_source_snapshot",
        ),
        Index("ix_trading_status_source_code_observed", "source", "observed_at"),
    )


class TradingStatusFact(Base):
    """Normalized suspend_d fact with explicit effective/PIT metadata.

    The source code remains part of the physical key because this is an
    ingestion fact.  The optional ``instrument_id`` is populated when the
    ingestion worker can resolve the source code.  Legacy rows without that
    stable identity remain unavailable to PIT backtests until re-ingested;
    current mutable catalogue rows are never used as an identity shortcut.
    """

    __tablename__ = "trading_status_facts"
    ts_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    instrument_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False, default="suspension")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="tushare")
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    known_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="trading_status_valid_interval_ordered"),
        CheckConstraint("length(btrim(dimension)) > 0", name="trading_status_dimension_not_blank"),
        Index("ix_trading_status_pit_lookup", "ts_code", "trade_date", "known_at", "observed_at"),
    )


class TradingStatusCoverageFact(Base):
    """Coverage proof for a status dimension over a requested date window."""

    __tablename__ = "trading_status_coverage_facts"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    event_count: Mapped[int] = mapped_column(nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    known_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_rule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="trading_status_coverage_date_ordered"),
        CheckConstraint("event_count >= 0", name="trading_status_coverage_event_count_non_negative"),
        Index("ix_trading_status_coverage_lookup", "instrument_id", "dimension", "start_date", "end_date", "known_at"),
    )


class TradingStatusFactRevisionAudit(Base):
    """Append-only audit trail retaining the superseded status fact."""

    __tablename__ = "trading_status_fact_revision_audits"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    ts_code: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    previous_instrument_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    previous_dimension: Mapped[str] = mapped_column(String(32), nullable=False, default="suspension")
    previous_status: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    previous_valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    previous_source: Mapped[str] = mapped_column(String(32), nullable=False, default="tushare")
    previous_quality_status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    previous_known_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    previous_source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    change_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    changed_fields: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    __table_args__ = (
        CheckConstraint(
            "change_kind IN ('correction', 'metadata_backfill')",
            name="ck_trading_status_revision_audit_change_kind",
        ),
        UniqueConstraint(
            "ts_code", "trade_date", "source_revision",
            name="uq_trading_status_revision_audit_identity",
        ),
        Index(
            "ix_trading_status_revision_audit_lookup",
            "ts_code", "trade_date", "accepted_at",
        ),
    )
