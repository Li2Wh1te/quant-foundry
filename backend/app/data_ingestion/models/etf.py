"""Persistent ETF entities, trading codes, and code-mapping audits."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EtfEntity(Base):
    """A local economic ETF identity that may own more than one trading code.

    Tushare's ETF-basic response has no identifier that survives a code change.
    Consequently, entities are initially created one-to-one with new codes and are
    only linked across codes by an explicit, evidenced mapping operation.
    """

    __tablename__ = "etf_entities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EtfCode(Base):
    """The latest Tushare ETF-basic record for one source-specific trading code."""

    __tablename__ = "etf_codes"
    __table_args__ = (
        CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(btrim(ts_code)) > 0", name="ts_code_not_blank"),
        CheckConstraint("length(btrim(list_status)) > 0", name="status_not_blank"),
        CheckConstraint("length(btrim(exchange)) > 0", name="exchange_not_blank"),
        CheckConstraint(
            "mgt_fee IS NULL OR mgt_fee >= 0", name="management_fee_not_negative"
        ),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        Index("ix_etf_codes_entity", "etf_id"),
        Index("ix_etf_codes_status_exchange", "list_status", "exchange"),
        Index("ix_etf_codes_index_code", "index_code"),
    )

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    etf_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("etf_entities.id"), nullable=False
    )
    csname: Mapped[str | None] = mapped_column(String(256))
    extname: Mapped[str | None] = mapped_column(String(256))
    cname: Mapped[str | None] = mapped_column(String(512))
    index_code: Mapped[str | None] = mapped_column(String(32))
    index_name: Mapped[str | None] = mapped_column(String(512))
    setup_date: Mapped[date | None] = mapped_column(Date)
    list_date: Mapped[date | None] = mapped_column(Date)
    list_status: Mapped[str] = mapped_column(String(8))
    exchange: Mapped[str] = mapped_column(String(16))
    mgr_name: Mapped[str | None] = mapped_column(String(256))
    custod_name: Mapped[str | None] = mapped_column(String(256))
    mgt_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    etf_type: Mapped[str | None] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EtfCodeMappingAudit(Base):
    """An immutable audit record for an explicit ETF-code entity reassignment."""

    __tablename__ = "etf_code_mapping_audits"
    __table_args__ = (
        CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(btrim(ts_code)) > 0", name="ts_code_not_blank"),
        CheckConstraint(
            "length(btrim(mapping_source)) > 0", name="mapping_source_not_blank"
        ),
        ForeignKeyConstraint(
            ["source", "ts_code"], ["etf_codes.source", "etf_codes.ts_code"]
        ),
        Index("ix_etf_code_mapping_audits_code", "source", "ts_code", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    old_etf_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("etf_entities.id"), nullable=False
    )
    new_etf_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("etf_entities.id"), nullable=False
    )
    mapping_source: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str | None] = mapped_column(String(2_048))
    actor: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
