"""Persistent current ETF adjustment factors from external data sources."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EtfAdjustmentFactor(Base):
    """The latest authoritative adjustment factor for one ETF trading date.

    The source code is part of the key intentionally. A local ETF identity may
    be reassigned after a reviewed code-mapping correction, while the provider's
    factor series remains attached to the source code it returned.
    """

    __tablename__ = "etf_adjustment_factors"
    __table_args__ = (
        CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(btrim(ts_code)) > 0", name="ts_code_not_blank"),
        CheckConstraint("adj_factor > 0", name="factor_positive"),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        Index(
            "ix_etf_adjustment_factors_trade_date_code",
            "trade_date",
            "source",
            "ts_code",
        ),
    )

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    adj_factor: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
