"""Persistent, append-only versioned instrument rule facts.

``instrument_rule_facts`` stores one immutable fact row per
``fact_key + fact_version``.  Corrections are new versions; existing rows
are never updated or overwritten.  ``fields`` is physically JSONB purely
as a storage form — it does not make the fields weakly typed or optional:
the rule-package resolver keeps validating every required field, value
type, cross-field constraint, capability declaration, settlement class,
and fixture gate.  Decimal rule values are stored as canonical strings so
no JSON floating-point number ever reaches the database.

There are deliberately no server-side defaults for any production rule
field: a missing field must block resolution instead of falling back to a
platform, asset-class, exchange, or market default.
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


RuleFactJSON = JSONB().with_variant(JSON(), "sqlite")


def _postgresql_only(constraint: CheckConstraint) -> CheckConstraint:
    constraint.ddl_if(dialect="postgresql")
    return constraint

#: Quality statuses supported by the facts table.
FACT_QUALITY_STATUSES: tuple[str, ...] = ("complete", "incomplete")


class InstrumentRuleFactRecord(Base):
    """One immutable versioned rule fact for a stable instrument identity.

    ``fact_key + fact_version`` is the platform-internal reference used by
    resolutions and run snapshots; ``source_revision`` is the external
    source revision and can never substitute for the internal pair.
    ``known_at`` is the point-in-time visibility boundary: queries with a
    ``data_cutoff`` must never observe rows learned after that instant.
    ``rule_exception_key/version`` is set only on exception-sourced facts
    and must be present or absent together with its pair constraint.
    """

    __tablename__ = "instrument_rule_facts"
    __table_args__ = (
        UniqueConstraint("fact_key", "fact_version", name="uq_fact_key_version"),
        CheckConstraint("fact_version > 0", name="fact_version_positive"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="valid_interval_ordered",
        ),
        CheckConstraint("length(trim(fact_key)) > 0", name="fact_key_not_blank"),
        CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        _postgresql_only(
            CheckConstraint(
                "jsonb_typeof(fields) = 'object'", name="fields_is_json_object"
            )
        ),
        CheckConstraint(
            "quality_status IN ('complete', 'incomplete')",
            name="quality_status_known",
        ),
        CheckConstraint(
            "(rule_exception_key IS NULL AND rule_exception_version IS NULL) "
            "OR (rule_exception_key IS NOT NULL AND rule_exception_version IS NOT NULL)",
            name="exception_reference_paired",
        ),
        CheckConstraint(
            "length(trim(content_hash)) > 0", name="content_hash_not_blank"
        ),
        # Covers PIT listing queries by instrument and package window.
        Index(
            "ix_instrument_rule_facts_instrument_package_window",
            "instrument_id",
            "rule_package_key",
            "rule_package_version",
            "valid_from",
        ),
        # Covers point-in-time visibility filtering.
        Index("ix_instrument_rule_facts_known_at", "known_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    # Platform-internal versioned identity of this fact row.
    fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    fact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    # Rule package this fact was authored against.
    rule_package_key: Mapped[str] = mapped_column(Text, nullable=False)
    rule_package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set only when the row is an exception-sourced fact; both columns
    # must be NULL or non-NULL together (see exception_reference_paired).
    rule_exception_key: Mapped[str | None] = mapped_column(Text)
    rule_exception_version: Mapped[int | None] = mapped_column(Integer)

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    # JSON object storage form only; decimal values use canonical strings.
    fields: Mapped[dict] = mapped_column(RuleFactJSON, nullable=False)

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
