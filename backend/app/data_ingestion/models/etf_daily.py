"""Persistent raw daily ETF market bars."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Numeric, String, func
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
