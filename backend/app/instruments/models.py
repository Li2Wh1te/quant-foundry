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
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import (
    DATERANGE,
    TSTZRANGE,
    ExcludeConstraint,
    Range,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# PostgreSQL owns the production range semantics.  Rendering the two native
# range types as TEXT on SQLite keeps lightweight unit tests able to create
# only the tables they exercise; Alembic still emits the real PostgreSQL
# types and constraints in the deployment migration.
@compiles(DATERANGE, "sqlite")
def _compile_daterange_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    return "TEXT"


@compiles(TSTZRANGE, "sqlite")
def _compile_tstzrange_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    return "TEXT"


def _dialect_name(context: object) -> str | None:
    """Return the insert dialect for a SQLAlchemy default callable."""

    return getattr(getattr(context, "dialect", None), "name", None)


def _mapping_logical_key_default(context: object) -> str:
    """Provide a deterministic legacy key for direct ORM inserts.

    Repository writes always provide an explicit logical key.  This default
    only keeps older callers that construct ``InstrumentCodeMappingRecord``
    directly from producing a NULL value after the migration.
    """

    params = getattr(context, "get_current_parameters")()
    return f"legacy:{params.get('id')}"


def _effective_range_default(context: object) -> object:
    """Build the persisted half-open date range when old callers omit it."""

    params = getattr(context, "get_current_parameters")()
    start = params.get("valid_from")
    if start is None:
        return None
    end = params.get("valid_to")
    if _dialect_name(context) == "sqlite":
        return f"[{start.isoformat()},{end.isoformat() if end is not None else ''})"
    return Range(start, end, bounds="[)")


def _knowledge_range_default(context: object) -> object:
    """Build an open-ended knowledge range for one immutable fact row."""

    params = getattr(context, "get_current_parameters")()
    start = params.get("known_at")
    if start is None:
        return None
    if _dialect_name(context) == "sqlite":
        return f"[{start.isoformat()},)"
    return Range(start, None, bounds="[)")


def _postgresql_only(constraint: ExcludeConstraint) -> ExcludeConstraint:
    """Keep PostgreSQL-only exclusion DDL out of SQLite test schemas."""

    constraint.ddl_if(dialect="postgresql")
    return constraint


def _identity_logical_key_default(context: object) -> str:
    """Provide a deterministic identity-fact key for direct ORM inserts."""

    params = getattr(context, "get_current_parameters")()
    return f"identity:{params.get('instrument_id')}"


def _display_logical_key_default(context: object) -> str:
    """Provide a deterministic display-fact key for direct ORM inserts."""

    params = getattr(context, "get_current_parameters")()
    return f"display:{params.get('instrument_id')}"


class Instrument(Base):
    """The generic stable identity of one tradable instrument.

    Identities never change even when codes or names do.  ``asset_class``
    partitions the identity space so asset-specific child tables can rely
    on typed foreign keys later without re-keying history.
    """

    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint("length(trim(asset_class)) > 0", name="asset_class_not_blank"),
        CheckConstraint(
            "status IN ('active', 'deprecated', 'merged')",
            name="instrument_status_known",
        ),
        CheckConstraint(
            "(status = 'merged' AND merged_into_id IS NOT NULL) OR "
            "(status IN ('active', 'deprecated') AND merged_into_id IS NULL)",
            name="instrument_status_target_consistent",
        ),
        CheckConstraint(
            "merged_into_id IS NULL OR merged_into_id <> id",
            name="instrument_merge_target_distinct",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active"
    )
    merged_into_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=True
    )
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
        CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(trim(source_code)) > 0", name="source_code_not_blank"),
        CheckConstraint("length(trim(trading_code)) > 0", name="trading_code_not_blank"),
        CheckConstraint(
            "length(trim(mapping_source)) > 0", name="mapping_source_not_blank"
        ),
        CheckConstraint("length(trim(evidence)) > 0", name="evidence_not_blank"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="valid_interval_ordered"
        ),
        CheckConstraint("fact_version > 0", name="fact_version_positive"),
        CheckConstraint(
            "length(trim(logical_fact_key)) > 0",
            name="logical_fact_key_not_blank",
        ),
        Index(
            "ix_instrument_code_mappings_identity_window",
            "instrument_id",
            "source",
            "valid_from",
        ),
        Index("ix_instrument_code_mappings_source_code", "source", "source_code"),
        Index(
            "ix_instrument_code_mappings_known_at_effective",
            "known_at",
            "instrument_id",
            "source",
            "valid_from",
        ),
        UniqueConstraint(
            "logical_fact_key", "fact_version", name="uq_mapping_logical_fact_version"
        ),
        _postgresql_only(
            ExcludeConstraint(
                ("instrument_id", "="),
                ("source", "="),
                ("logical_fact_key", "<>"),
                ("effective_range", "&&"),
                ("knowledge_range", "&&"),
                name="ex_mapping_effective_knowledge_overlap",
                using="gist",
            )
        ),
        _postgresql_only(
            ExcludeConstraint(
                ("source", "="),
                ("source_code", "="),
                ("effective_range", "&&"),
                ("knowledge_range", "&&"),
                ("instrument_id", "<>"),
                name="ex_mapping_source_code_identity_overlap",
                using="gist",
            )
        ),
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
    fact_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    logical_fact_key: Mapped[str] = mapped_column(
        Text, nullable=False, default=_mapping_logical_key_default
    )
    supersedes_fact_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("instrument_code_mappings.id"), nullable=True
    )
    # Native range columns are populated transactionally with the scalar
    # bounds.  They make PostgreSQL exclusion constraints straightforward and
    # avoid deriving ranges differently in different readers.
    effective_range = mapped_column(
        DATERANGE(), nullable=False, default=_effective_range_default
    )
    knowledge_range = mapped_column(
        TSTZRANGE(), nullable=False, default=_knowledge_range_default
    )
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


class InstrumentIdentityFactRecord(Base):
    """Append-only versioned identity attributes for one instrument."""

    __tablename__ = "instrument_identity_facts"
    __table_args__ = (
        UniqueConstraint(
            "logical_fact_key", "fact_version", name="uq_identity_logical_fact_version"
        ),
        CheckConstraint("fact_version > 0", name="identity_fact_version_positive"),
        CheckConstraint(
            "length(trim(asset_class)) > 0", name="identity_asset_class_not_blank"
        ),
        CheckConstraint(
            "length(trim(currency)) > 0", name="identity_currency_not_blank"
        ),
        CheckConstraint(
            "length(trim(calendar_id)) > 0", name="identity_calendar_not_blank"
        ),
        CheckConstraint(
            "length(trim(evidence)) > 0", name="identity_evidence_not_blank"
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="identity_valid_interval_ordered",
        ),
        Index(
            "ix_instrument_identity_facts_instrument_window",
            "instrument_id",
            "valid_from",
        ),
        Index("ix_instrument_identity_facts_known_at", "known_at"),
        _postgresql_only(
            ExcludeConstraint(
                ("instrument_id", "="),
                ("logical_fact_key", "<>"),
                ("effective_range", "&&"),
                ("knowledge_range", "&&"),
                name="ex_identity_effective_knowledge_overlap",
                using="gist",
            )
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    fact_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    logical_fact_key: Mapped[str] = mapped_column(
        Text, nullable=False, default=_identity_logical_key_default
    )
    supersedes_fact_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("instrument_identity_facts.id"), nullable=True
    )
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    # Persist the same half-open effective and knowledge intervals used by
    # mapping/display facts.  PostgreSQL uses native range operators for
    # overlap exclusion; SQLite renders these columns as deterministic TEXT
    # through the compiler hooks above for repository and migration tests.
    effective_range = mapped_column(
        DATERANGE(), nullable=False, default=_effective_range_default
    )
    knowledge_range = mapped_column(
        TSTZRANGE(), nullable=False, default=_knowledge_range_default
    )
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InstrumentDisplayFactRecord(Base):
    """Append-only display labels with effective and knowledge intervals."""

    __tablename__ = "instrument_display_facts"
    __table_args__ = (
        UniqueConstraint(
            "logical_fact_key", "fact_version", name="uq_display_logical_fact_version"
        ),
        CheckConstraint("fact_version > 0", name="display_fact_version_positive"),
        CheckConstraint(
            "length(trim(source)) > 0", name="display_source_not_blank"
        ),
        CheckConstraint(
            "length(trim(evidence)) > 0", name="display_evidence_not_blank"
        ),
        CheckConstraint(
            "authority_status IN ('authoritative', 'pending', 'rejected')",
            name="display_authority_status_known",
        ),
        CheckConstraint(
            "authority_rank >= 0", name="display_authority_rank_non_negative"
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="display_valid_interval_ordered",
        ),
        Index(
            "ix_instrument_display_facts_instrument_window",
            "instrument_id",
            "valid_from",
        ),
        Index("ix_instrument_display_facts_known_at", "known_at"),
        _postgresql_only(
            ExcludeConstraint(
                ("instrument_id", "="),
                ("logical_fact_key", "<>"),
                ("authority_rank", "="),
                ("effective_range", "&&"),
                ("knowledge_range", "&&"),
                name="ex_display_effective_knowledge_overlap",
                using="gist",
                where="authority_status = 'authoritative'",
            )
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    fact_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    logical_fact_key: Mapped[str] = mapped_column(
        Text, nullable=False, default=_display_logical_key_default
    )
    supersedes_fact_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("instrument_display_facts.id"), nullable=True
    )
    trading_code: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(512))
    display_name: Mapped[str | None] = mapped_column(String(512))
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(128))
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    authority_rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    authority_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="authoritative"
    )
    effective_range = mapped_column(
        DATERANGE(), nullable=False, default=_effective_range_default
    )
    knowledge_range = mapped_column(
        TSTZRANGE(), nullable=False, default=_knowledge_range_default
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MappingResolutionHead(Base):
    """Rebuildable pointer to the visible fact for a knowledge boundary."""

    __tablename__ = "mapping_resolution_heads"
    __table_args__ = (
        UniqueConstraint(
            "logical_fact_key", "knowledge_from", name="uq_mapping_head_logical_knowledge"
        ),
        Index(
            "ix_mapping_resolution_heads_lookup",
            "instrument_id",
            "source",
            "knowledge_from",
        ),
        _postgresql_only(
            ExcludeConstraint(
                ("instrument_id", "="),
                ("source", "="),
                ("logical_fact_key", "<>"),
                ("effective_range", "&&"),
                ("knowledge_range", "&&"),
                name="ex_mapping_heads_effective_knowledge_overlap",
                using="gist",
            )
        ),
        _postgresql_only(
            ExcludeConstraint(
                ("source", "="),
                ("source_code", "="),
                ("effective_range", "&&"),
                ("knowledge_range", "&&"),
                ("instrument_id", "<>"),
                name="ex_mapping_heads_source_code_identity_overlap",
                using="gist",
            )
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    logical_fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fact_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instrument_code_mappings.id"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_code: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_range = mapped_column(DATERANGE(), nullable=False)
    knowledge_range = mapped_column(TSTZRANGE(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DisplayResolutionHead(Base):
    """Rebuildable pointer to the visible authoritative display fact."""

    __tablename__ = "display_resolution_heads"
    __table_args__ = (
        UniqueConstraint(
            "logical_fact_key", "knowledge_from", name="uq_display_head_logical_knowledge"
        ),
        Index(
            "ix_display_resolution_heads_lookup",
            "instrument_id",
            "knowledge_from",
        ),
        _postgresql_only(
            ExcludeConstraint(
                ("instrument_id", "="),
                ("logical_fact_key", "<>"),
                ("authority_rank", "="),
                ("effective_range", "&&"),
                ("knowledge_range", "&&"),
                name="ex_display_heads_effective_knowledge_overlap",
                using="gist",
            )
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    logical_fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fact_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instrument_display_facts.id"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    authority_rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    effective_range = mapped_column(DATERANGE(), nullable=False)
    knowledge_range = mapped_column(TSTZRANGE(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InstrumentIdentityMergeAuditRecord(Base):
    """Immutable audit of accepted and rejected identity merge attempts."""

    __tablename__ = "instrument_identity_merge_audits"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('accepted', 'rejected')", name="merge_outcome_known"
        ),
        CheckConstraint(
            "length(trim(mapping_source)) > 0", name="merge_mapping_source_not_blank"
        ),
        CheckConstraint(
            "(outcome = 'accepted' AND length(trim(evidence)) > 0) OR "
            "(outcome = 'rejected')",
            name="merge_evidence_when_accepted",
        ),
        Index(
            "ix_instrument_identity_merge_audits_source",
            "source_instrument_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    target_instrument_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("instruments.id"), nullable=False
    )
    mapping_source: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(256))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
