"""Persistent stable instrument identities and PIT source-code mappings.

These tables are the generic identity layer shared by every asset class.
ETF-specific facts stay in their own tables and reference the same
identities: ``etf_entities.id`` is a subset of ``instruments.id``.

``instrument_code_mappings`` is append-only evidence: each row is one
half-open ``[valid_from, valid_to)`` validity window for a data-source
code.  Rows are never rewritten into history; corrections are new rows
with their own ``known_at`` knowledge time.
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Instrument(Base):
    """The generic stable identity of one tradable instrument.

    Identities never change even when codes or names do.  ``asset_class``
    partitions the identity space so asset-specific child tables can rely
    on typed foreign keys later without re-keying history.
    """

    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint("length(btrim(asset_class)) > 0", name="asset_class_not_blank"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InstrumentCodeMappingRecord(Base):
    """One evidenced half-open validity window for a data-source code.

    ``source_code`` is the identifier assigned by the data source (for
    example ``510300.SH``) while ``trading_code`` is the user-facing
    display code (for example ``510300``).  ``valid_to=None`` means the
    mapping is still in force.  ``known_at`` drives point-in-time
    visibility filtering: a query with ``data_cutoff`` must never observe
    rows learned after that instant.
    """

    __tablename__ = "instrument_code_mappings"
    __table_args__ = (
        CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(btrim(source_code)) > 0", name="source_code_not_blank"),
        CheckConstraint("length(btrim(trading_code)) > 0", name="trading_code_not_blank"),
        CheckConstraint(
            "length(btrim(mapping_source)) > 0", name="mapping_source_not_blank"
        ),
        CheckConstraint("length(btrim(evidence)) > 0", name="evidence_not_blank"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="valid_interval_ordered"
        ),
        Index(
            "ix_instrument_code_mappings_identity_window",
            "instrument_id",
            "source",
            "valid_from",
        ),
        Index("ix_instrument_code_mappings_source_code", "source", "source_code"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_code: Mapped[str] = mapped_column(String(32), nullable=False)
    trading_code: Mapped[str] = mapped_column(String(16), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    # Identifier of the exact source snapshot/revision this fact came from;
    # kept optional because not every evidence channel exposes revisions.
    source_revision: Mapped[str | None] = mapped_column(String(64))
    mapping_source: Mapped[str] = mapped_column(String(64), nullable=False)
    # Concrete proof (announcement URL, filing reference, reviewed procedure
    # id).  Mandatory: mapping_source only names the evidence channel.
    evidence: Mapped[str] = mapped_column(String(2_048), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
