"""Persistent named-exception sets for instrument rule packages.

An exception set routes a stable ``instrument_id`` over an explicit
half-open validity interval to an independently sourced exception fact.
By construction the tables below contain **only references and validity
intervals**: production values such as ``lot_size``, ``price_tick``,
``price_precision``, ``quantity_precision``, ``currency``, or
``trading_session_template`` have no columns here and must never be
added.  Sets are addressed by the exact ``set_key + set_version`` pair;
there is deliberately no "latest version" lookup path.
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.instruments.rule_facts_models import FACT_QUALITY_STATUSES


class InstrumentRuleExceptionSetRecord(Base):
    """One immutable versioned set of named exceptions for one package."""

    __tablename__ = "instrument_rule_exception_sets"
    __table_args__ = (
        UniqueConstraint("set_key", "set_version", name="uq_exception_set_key_version"),
        CheckConstraint("set_version > 0", name="set_version_positive"),
        CheckConstraint("rule_package_version > 0", name="package_version_positive"),
        CheckConstraint("length(trim(set_key)) > 0", name="set_key_not_blank"),
        CheckConstraint(
            "length(trim(rule_package_key)) > 0", name="package_key_not_blank"
        ),
        CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        CheckConstraint(
            "quality_status IN ('complete', 'incomplete')",
            name="quality_status_known",
        ),
        CheckConstraint(
            "length(trim(content_hash)) > 0", name="content_hash_not_blank"
        ),
        Index(
            "ix_instrument_rule_exception_sets_package",
            "rule_package_key",
            "rule_package_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    set_key: Mapped[str] = mapped_column(Text, nullable=False)
    set_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_package_key: Mapped[str] = mapped_column(Text, nullable=False)
    rule_package_version: Mapped[int] = mapped_column(Integer, nullable=False)

    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision: Mapped[str | None] = mapped_column(Text)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    quality_status: Mapped[str] = mapped_column(String(16), nullable=False)
    fixture_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InstrumentRuleExceptionEntryRecord(Base):
    """One routing entry: instrument + interval -> exception fact reference.

    Entries join their set by the exact ``(set_key, set_version)`` pair.
    ``exception_fact_key/version`` points at a full fact row in
    ``instrument_rule_facts``; the entry never carries production values.
    """

    __tablename__ = "instrument_rule_exception_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["set_key", "set_version"],
            [
                "instrument_rule_exception_sets.set_key",
                "instrument_rule_exception_sets.set_version",
            ],
            name="fk_exception_entries_set",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="valid_interval_ordered",
        ),
        CheckConstraint(
            "exception_fact_version > 0", name="exception_fact_version_positive"
        ),
        CheckConstraint(
            "length(trim(exception_fact_key)) > 0",
            name="exception_fact_key_not_blank",
        ),
        Index(
            "ix_instrument_rule_exception_entries_instrument_window",
            "instrument_id",
            "valid_from",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    set_key: Mapped[str] = mapped_column(Text, nullable=False)
    set_version: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    exception_fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    exception_fact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
