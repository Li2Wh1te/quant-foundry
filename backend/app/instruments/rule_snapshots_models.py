"""ORM tables freezing the rule snapshot of one formal backtest run.

``backtest_run_rule_snapshots`` holds exactly one row per run; the
per-instrument segment table holds one row per instrument per effective
interval.  Snapshots store fact references and full provenance, never
only final rule numbers, so execution reads a self-contained frozen
selection that later edits to the fact or exception tables cannot alter.

The results schema does not yet declare a common ``backtest_runs``
foreign key; these tables follow the existing result-table convention
and reference ``run_id`` without one.  A future run-creation migration
may add the foreign key without changing any snapshot field semantics.
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
    Uuid,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# PostgreSQL enforces JSON object shape; SQLite test databases use the
# standard JSON type and rely on the domain layer for the same check.
SnapshotJSON = JSONB().with_variant(JSON(), "sqlite")


def _postgresql_only(constraint: CheckConstraint) -> CheckConstraint:
    constraint.ddl_if(dialect="postgresql")
    return constraint


class BacktestRunRuleSnapshotRecord(Base):
    """One run-level rule snapshot row (exactly one per ``run_id``)."""

    __tablename__ = "backtest_run_rule_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_backtest_run_rule_snapshots_run_id"),
        CheckConstraint(
            "length(trim(rule_package_key)) > 0", name="package_key_not_blank"
        ),
        CheckConstraint("rule_package_version > 0", name="package_version_positive"),
        CheckConstraint(
            "length(trim(rule_package_semantic_hash)) > 0",
            name="semantic_hash_not_blank",
        ),
        CheckConstraint(
            "length(trim(parser_revision)) > 0", name="parser_revision_not_blank"
        ),
        CheckConstraint(
            "(exception_set_key IS NULL AND exception_set_version IS NULL "
            "AND exception_set_hash IS NULL) OR "
            "(exception_set_key IS NOT NULL AND exception_set_version IS NOT NULL "
            "AND exception_set_hash IS NOT NULL)",
            name="exception_set_paired",
        ),
        CheckConstraint(
            "length(trim(snapshot_hash)) > 0", name="snapshot_hash_not_blank"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    rule_package_key: Mapped[str] = mapped_column(Text, nullable=False)
    rule_package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_package_semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_revision: Mapped[str] = mapped_column(Text, nullable=False)
    exception_set_key: Mapped[str | None] = mapped_column(Text)
    exception_set_version: Mapped[int | None] = mapped_column(Integer)
    exception_set_hash: Mapped[str | None] = mapped_column(String(64))
    data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BacktestRunInstrumentRuleSnapshotRecord(Base):
    """One instrument's frozen rules for one effective interval of a run."""

    __tablename__ = "backtest_run_instrument_rule_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "instrument_id",
            "effective_from",
            name="uq_backtest_run_instrument_rule_snapshots_segment",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_interval_ordered",
        ),
        CheckConstraint(
            "normal_fact_version > 0", name="normal_fact_version_positive"
        ),
        CheckConstraint(
            "(exception_fact_key IS NULL AND exception_fact_version IS NULL) OR "
            "(exception_fact_key IS NOT NULL AND exception_fact_version IS NOT NULL)",
            name="exception_fact_paired",
        ),
        _postgresql_only(
            CheckConstraint(
                "jsonb_typeof(normalized_values) = 'object'",
                name="normalized_values_is_json_object",
            )
        ),
        _postgresql_only(
            CheckConstraint(
                "jsonb_typeof(capability_declarations) = 'object'",
                name="capability_declarations_is_json_object",
            )
        ),
        _postgresql_only(
            CheckConstraint(
                "jsonb_typeof(provenance) = 'object'",
                name="provenance_is_json_object",
            )
        ),
        CheckConstraint(
            "length(trim(resolution_hash)) > 0",
            name="resolution_hash_not_blank",
        ),
        Index(
            "ix_backtest_run_instrument_rule_snapshots_run",
            "run_id",
            "instrument_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    # No backtest_runs foreign key yet; see module docstring.
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid,
        # Explicit short name: the default convention would exceed
        # PostgreSQL's 63-character identifier limit for this table.
        ForeignKey(
            "instruments.id",
            name="fk_run_instrument_rule_snapshots_instrument",
        ),
        nullable=False,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)

    normal_fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    normal_fact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    exception_fact_key: Mapped[str | None] = mapped_column(Text)
    exception_fact_version: Mapped[int | None] = mapped_column(Integer)

    normalized_values: Mapped[dict] = mapped_column(SnapshotJSON, nullable=False)
    capability_declarations: Mapped[dict] = mapped_column(SnapshotJSON, nullable=False)
    provenance: Mapped[dict] = mapped_column(SnapshotJSON, nullable=False)
    resolution_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
