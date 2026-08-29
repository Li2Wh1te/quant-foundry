"""SQLAlchemy persistence projections for task-11 calendar facts.

The domain value objects live in :mod:`app.backtesting.calendar_axis` and do
not import SQLAlchemy.  These records are append-only projections used by the
SQL provider and ingestion repositories.  PostgreSQL deployments can add
stronger exclusion/trigger constraints in the Alembic migration; SQLite
keeps the same scalar and JSON contract and relies on repository validation.
"""

from __future__ import annotations

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
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base

JsonType = JSONB().with_variant(JSON(), "sqlite")

CALENDAR_TABLE_NAMES = (
    "calendar_registry",
    "calendar_source_priorities",
    "calendar_definitions",
    "calendar_session_facts",
    "calendar_exchange_bindings",
    "calendar_capability_declarations",
    "calendar_resolution_heads",
    "calendar_reconciliation_ranges",
)


class _FactIdentityMixin:
    """Common append-only fact identity and PIT provenance columns."""

    fact_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    fact_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    logical_fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_fact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(256), nullable=False)
    source_priority_fact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    source_priority_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_revision_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bootstrap_seed_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bootstrap_seed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bootstrap_seed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence: Mapped[object] = mapped_column(JsonType, nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    knowledge_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    knowledge_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    knowledge_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quality_status: Mapped[str] = mapped_column(String(24), nullable=False, default="accepted")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CalendarRegistryRecord(Base, _FactIdentityMixin):
    """Versioned canonical calendar registry fact."""

    __tablename__ = "calendar_registry"
    __table_args__ = (
        UniqueConstraint("calendar_id", "registry_version", name="uq_calendar_registry_version"),
        UniqueConstraint("calendar_id", "registry_version", "fact_id", name="uq_calendar_registry_composite_ref"),
        UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_registry_logical_version"),
        CheckConstraint("fact_version > 0", name="calendar_registry_fact_version_positive"),
        CheckConstraint("registry_version > 0", name="calendar_registry_version_positive"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="calendar_registry_valid_range"),
        CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_registry_knowledge_range"),
        CheckConstraint("status IN ('active','deprecated')", name="calendar_registry_status"),
        CheckConstraint("timezone_policy = 'fixed_asia_shanghai'", name="calendar_registry_timezone_policy"),
        CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_registry_source_priority_required",
        ),
        ForeignKeyConstraint(
            ["source", "source_priority_version", "source_priority_fact_id"],
            ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"],
            name="fk_calendar_registry_source_priority",
        ),
        ForeignKeyConstraint(
            ["supersedes_fact_id"], ["calendar_registry.fact_id"],
            name="fk_calendar_registry_supersedes",
        ),
        Index("ix_calendar_registry_lookup", "calendar_id", "valid_from", "known_at"),
    )

    calendar_id: Mapped[str] = mapped_column(String(32), nullable=False)
    registry_version: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone_policy: Mapped[str] = mapped_column(String(64), nullable=False, default="fixed_asia_shanghai")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class CalendarSourcePriorityRecord(Base, _FactIdentityMixin):
    """Versioned source-priority fact rooted in bootstrap provenance."""

    __tablename__ = "calendar_source_priorities"
    __table_args__ = (
        UniqueConstraint("source", "source_priority_version", name="uq_calendar_source_priority_version"),
        UniqueConstraint("source", "source_priority_version", "fact_id", name="uq_calendar_source_priority_composite_ref"),
        UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_source_priority_logical_version"),
        CheckConstraint("fact_version > 0", name="calendar_source_priority_fact_version_positive"),
        CheckConstraint(
            "source_priority_version IS NOT NULL AND source_priority IS NOT NULL "
            "AND source_priority >= 0 AND source_revision_order IS NOT NULL "
            "AND source_revision_order >= 0",
            name="calendar_source_priority_values_required",
        ),
        CheckConstraint(
            "source_priority_fact_id IS NULL AND bootstrap_seed_id IS NOT NULL "
            "AND bootstrap_seed_version IS NOT NULL AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_source_priority_bootstrap_root",
        ),
        ForeignKeyConstraint(
            ["supersedes_fact_id"], ["calendar_source_priorities.fact_id"],
            name="fk_calendar_source_priority_supersedes",
        ),
    )

    # Priority roots deliberately do not self-reference source_priority_fact_id.
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_priority_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    source_revision_order: Mapped[int] = mapped_column(Integer, nullable=False)


class CalendarDefinitionRecord(Base, _FactIdentityMixin):
    """Versioned default session template for one named calendar."""

    __tablename__ = "calendar_definitions"
    __table_args__ = (
        UniqueConstraint("calendar_id", "definition_version", name="uq_calendar_definition_semantic_version"),
        UniqueConstraint("calendar_id", "definition_version", "fact_id", name="uq_calendar_definition_composite_ref"),
        UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_definition_logical_version"),
        CheckConstraint("fact_version > 0", name="calendar_definition_fact_version_positive"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="calendar_definition_valid_range"),
        CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_definition_knowledge_range"),
        CheckConstraint("registry_version > 0", name="calendar_definition_registry_version_positive"),
        CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_definition_source_priority_required",
        ),
        ForeignKeyConstraint(
            ["source", "source_priority_version", "source_priority_fact_id"],
            ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"],
            name="fk_calendar_definition_source_priority",
        ),
        ForeignKeyConstraint(
            ["calendar_id", "registry_version", "registry_fact_id"],
            ["calendar_registry.calendar_id", "calendar_registry.registry_version", "calendar_registry.fact_id"],
            name="fk_calendar_definition_registry",
        ),
        ForeignKeyConstraint(
            ["supersedes_fact_id"], ["calendar_definitions.fact_id"],
            name="fk_calendar_definition_supersedes",
        ),
        Index("ix_calendar_definition_lookup", "calendar_id", "valid_from", "known_at"),
    )

    calendar_id: Mapped[str] = mapped_column(String(32), nullable=False)
    registry_fact_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    registry_version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_version: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    default_sessions: Mapped[object] = mapped_column(JsonType, nullable=False)


class CalendarSessionFactRecord(Base, _FactIdentityMixin):
    """Explicit open/closed fact for every natural calendar date."""

    __tablename__ = "calendar_session_facts"
    __table_args__ = (
        UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_session_logical_version"),
        CheckConstraint("fact_version > 0", name="calendar_session_fact_version_positive"),
        CheckConstraint("valid_to > valid_from", name="calendar_session_valid_range"),
        CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_session_knowledge_range"),
        CheckConstraint("registry_version > 0", name="calendar_session_registry_version_positive"),
        CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_session_source_priority_required",
        ),
        CheckConstraint("override_mode IN ('inherit','explicit')", name="calendar_session_override_mode"),
        CheckConstraint("(is_open = 0 AND override_mode = 'explicit') OR is_open = 1", name="calendar_session_closed_explicit"),
        Index("ix_calendar_session_lookup", "calendar_id", "session_date", "known_at"),
        ForeignKeyConstraint(
            ["source", "source_priority_version", "source_priority_fact_id"],
            ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"],
            name="fk_calendar_session_source_priority",
        ),
        ForeignKeyConstraint(
            ["calendar_id", "registry_version", "registry_fact_id"],
            ["calendar_registry.calendar_id", "calendar_registry.registry_version", "calendar_registry.fact_id"],
            name="fk_calendar_session_registry",
        ),
        ForeignKeyConstraint(
            ["calendar_id", "definition_version", "definition_fact_id"],
            ["calendar_definitions.calendar_id", "calendar_definitions.definition_version", "calendar_definitions.fact_id"],
            name="fk_calendar_session_definition",
        ),
        Index("ix_calendar_session_quality", "calendar_id", "quality_status", "session_date"),
        ForeignKeyConstraint(
            ["supersedes_fact_id"], ["calendar_session_facts.fact_id"],
            name="fk_calendar_session_supersedes",
        ),
    )

    calendar_id: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    registry_fact_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    registry_version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_version: Mapped[str] = mapped_column(String(128), nullable=False)
    definition_fact_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    timezone_override: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sessions_override: Mapped[object | None] = mapped_column(JsonType, nullable=True)
    override_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="inherit")


class CalendarExchangeBindingRecord(Base, _FactIdentityMixin):
    """Versioned explicit alias-to-calendar binding."""

    __tablename__ = "calendar_exchange_bindings"
    __table_args__ = (
        UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_binding_logical_version"),
        CheckConstraint("fact_version > 0", name="calendar_binding_fact_version_positive"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="calendar_binding_valid_range"),
        CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_binding_knowledge_range"),
        CheckConstraint("registry_version > 0", name="calendar_binding_registry_version_positive"),
        CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_binding_source_priority_required",
        ),
        ForeignKeyConstraint(
            ["source", "source_priority_version", "source_priority_fact_id"],
            ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"],
            name="fk_calendar_binding_source_priority",
        ),
        ForeignKeyConstraint(
            ["canonical_calendar_id", "registry_version", "registry_fact_id"],
            ["calendar_registry.calendar_id", "calendar_registry.registry_version", "calendar_registry.fact_id"],
            name="fk_calendar_binding_registry",
        ),
        ForeignKeyConstraint(
            ["supersedes_fact_id"], ["calendar_exchange_bindings.fact_id"],
            name="fk_calendar_binding_supersedes",
        ),
        Index("ix_calendar_binding_lookup", "alias", "valid_from", "known_at"),
    )

    alias: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_calendar_id: Mapped[str] = mapped_column(String(32), nullable=False)
    registry_fact_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    registry_version: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_version: Mapped[str] = mapped_column(String(128), nullable=False)


class CalendarCapabilityDeclarationRecord(Base, _FactIdentityMixin):
    """Single-scope provider/rule/calendar/instrument capability fact."""

    __tablename__ = "calendar_capability_declarations"
    __table_args__ = (
        UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_capability_logical_version"),
        CheckConstraint("fact_version > 0", name="calendar_capability_fact_version_positive"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="calendar_capability_valid_range"),
        CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_capability_knowledge_range"),
        CheckConstraint("scope_kind IN ('provider','rule_package','calendar','instrument')", name="calendar_capability_scope_kind"),
        CheckConstraint("value IN ('supported','unsupported','unknown')", name="calendar_capability_value"),
        CheckConstraint("applicability IS NULL OR applicability IN ('required','not_applicable')", name="calendar_capability_applicability"),
        CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_capability_source_priority_required",
        ),
        CheckConstraint("scope_kind <> 'calendar' OR (registry_fact_id IS NOT NULL AND registry_version IS NOT NULL)", name="calendar_capability_registry_reference"),
        CheckConstraint("scope_kind <> 'calendar' OR calendar_id IS NOT NULL", name="calendar_capability_calendar_id_required"),
        CheckConstraint("scope_kind = 'calendar' OR (calendar_id IS NULL AND registry_fact_id IS NULL AND registry_version IS NULL)", name="calendar_capability_non_calendar_columns"),
        ForeignKeyConstraint(
            ["source", "source_priority_version", "source_priority_fact_id"],
            ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"],
            name="fk_calendar_capability_source_priority",
        ),
        ForeignKeyConstraint(
            ["calendar_id", "registry_version", "registry_fact_id"],
            ["calendar_registry.calendar_id", "calendar_registry.registry_version", "calendar_registry.fact_id"],
            name="fk_calendar_capability_registry",
        ),
        ForeignKeyConstraint(
            ["supersedes_fact_id"], ["calendar_capability_declarations.fact_id"],
            name="fk_calendar_capability_supersedes",
        ),
        Index("ix_calendar_capability_scope", "scope_kind", "scope_key", "capability", "valid_from", "known_at"),
    )

    scope_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    package_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    package_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calendar_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    registry_fact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    registry_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    instrument_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    applicability: Mapped[str | None] = mapped_column(String(24), nullable=True)


class CalendarResolutionHeadRecord(Base):
    """Rebuildable selected-fact head; never the source of historical truth."""

    __tablename__ = "calendar_resolution_heads"
    __table_args__ = (
        # One logical date may have several non-overlapping knowledge slices;
        # the PIT provider selects exactly one slice for its cutoff.
        UniqueConstraint(
            "logical_fact_key",
            "effective_date",
            "knowledge_from",
            name="uq_calendar_resolution_head_slot",
        ),
        Index("ix_calendar_resolution_head_calendar_date", "calendar_id", "effective_date"),
        ForeignKeyConstraint(
            ["selected_fact_id"],
            ["calendar_session_facts.fact_id"],
            name="fk_calendar_resolution_head_selected_fact",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    logical_fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    # The head stores the selected explicit day fact separately from its
    # open/closed semantic; a closed day still has a selected fact.
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    selected_fact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    selected_fact_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    knowledge_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    knowledge_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class CalendarReconciliationRangeRecord(Base):
    """Auditable gap/reconciliation work item scoped by canonical calendar."""

    __tablename__ = "calendar_reconciliation_ranges"
    __table_args__ = (
        CheckConstraint("range_end > range_start", name="calendar_reconciliation_range_ordered"),
        CheckConstraint("status IN ('pending','running','completed','blocked')", name="calendar_reconciliation_status"),
        Index("ix_calendar_reconciliation_pending", "calendar_id", "status", "range_start"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    calendar_id: Mapped[str] = mapped_column(String(32), nullable=False)
    range_start: Mapped[date] = mapped_column(Date, nullable=False)
    range_end: Mapped[date] = mapped_column(Date, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(256), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rescan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


__all__ = [
    "JsonType",
    "CALENDAR_TABLE_NAMES",
    "CalendarRegistryRecord",
    "CalendarSourcePriorityRecord",
    "CalendarDefinitionRecord",
    "CalendarSessionFactRecord",
    "CalendarExchangeBindingRecord",
    "CalendarCapabilityDeclarationRecord",
    "CalendarResolutionHeadRecord",
    "CalendarReconciliationRangeRecord",
]
