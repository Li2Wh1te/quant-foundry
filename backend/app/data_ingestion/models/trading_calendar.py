"""Persistent trading calendar model."""

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Index, String, func, text
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
