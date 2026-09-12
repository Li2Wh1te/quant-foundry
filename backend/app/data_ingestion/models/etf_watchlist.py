"""Credential-owned ETF selections, independent of market-data lifecycle."""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EtfWatchlistEntry(Base):
    """Removing a selection must never remove an instrument or its prices.

    The authenticated principal supplies owner_scope; API callers cannot choose
    another owner. Source and code remain explicit so a future data provider
    cannot silently replace the series selected by the operator.
    """

    __tablename__ = "etf_watchlist_entries"

    owner_scope: Mapped[str] = mapped_column(String(80), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
