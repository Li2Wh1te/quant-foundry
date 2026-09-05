"""Immutable ETF corporate-action source and normalized facts."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import Date, DateTime, Numeric, String, Uuid, JSON, CheckConstraint, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base

class CorporateActionSourceFact(Base):
    __tablename__ = "corporate_action_source_facts"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False, default="fund_div")
    query_kind: Mapped[str | None] = mapped_column(String(32))
    query_value: Mapped[str | None] = mapped_column(String(32))
    ts_code: Mapped[str] = mapped_column(String(32), nullable=False)
    ann_date: Mapped[date | None] = mapped_column(Date)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # The hash is the source revision identity for this immutable response.
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_corp_source_code_ann", "source", "ts_code", "ann_date"),
                      UniqueConstraint("source", "endpoint", "query_kind", "query_value", "source_hash", name="uq_corp_source_snapshot"))

class CorporateActionFact(Base):
    __tablename__ = "corporate_action_facts"
    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    logical_fact_key: Mapped[str] = mapped_column(String(256), nullable=False)
    fact_version: Mapped[int] = mapped_column(nullable=False, default=1)
    supersedes_fact_id: Mapped[UUID | None] = mapped_column(Uuid)
    instrument_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    record_date: Mapped[date | None] = mapped_column(Date)
    ex_date: Mapped[date | None] = mapped_column(Date)
    source_payment_date: Mapped[date | None] = mapped_column(Date)
    source_arrival_date: Mapped[date | None] = mapped_column(Date)
    cash_effective_date: Mapped[date | None] = mapped_column(Date)
    # The approved phase/date pair is the authoritative effective-time
    # expression for v1; an exact clock instant is optional and never guessed.
    effective_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cash_effective_phase: Mapped[str | None] = mapped_column(String(32))
    cash_amount_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    quantity_ratio: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    quantity_delta: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    currency: Mapped[str | None] = mapped_column(String(16))
    entitlement_rule: Mapped[str | None] = mapped_column(String(64))
    cash_date_rule: Mapped[str | None] = mapped_column(String(64))
    timing_rule: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # These fields separate market-effective dates from source/platform
    # knowledge time.  Legacy rows may be NULL and are reported non-strict.
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    known_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    quality: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_corp_facts_instrument_date", "instrument_id", "cash_effective_date"),
                      UniqueConstraint("logical_fact_key", "fact_version", name="uq_corp_fact_version"))

class CorporateActionCoverageFact(Base):
    __tablename__ = "corporate_action_coverage_facts"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Domain projection fields used by the backtesting coverage contract.
    # ``event_count=0`` is a valid complete-zero assertion; it must not be
    # conflated with an absent coverage row.
    action_type: Mapped[str | None] = mapped_column(String(32))
    event_count: Mapped[int | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="tushare")
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    known_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_rule: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="corporate_action_coverage_date_ordered"),
        CheckConstraint(
            "event_count IS NULL OR event_count >= 0",
            name="corporate_action_coverage_event_count_non_negative",
        ),
    )
