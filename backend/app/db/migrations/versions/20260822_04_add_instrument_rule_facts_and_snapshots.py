"""Add instrument rule facts, named exception sets, and run rule snapshots.

Revision ID: 20260822_04
Revises: 20260822_03
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260822_04"
down_revision: str | None = "20260822_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_storage_type() -> sa.types.TypeEngine:
    """Use JSONB on PostgreSQL and portable JSON on SQLite smoke tests."""

    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql"
    )


def _json_object_check(column: str, name: str) -> sa.CheckConstraint:
    """Build the object-shape check with the active database's JSON function."""

    dialect = op.get_bind().dialect.name
    function = "jsonb_typeof" if dialect == "postgresql" else "json_type"
    return sa.CheckConstraint(
        f"{function}({column}) = 'object'", name=name
    )


def upgrade() -> None:
    op.create_table(
        "instrument_rule_facts",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Immutable fact-row identifier."),
        sa.Column("fact_key", sa.Text(), nullable=False, comment="Platform-internal fact identity; paired with fact_version."),
        sa.Column("fact_version", sa.Integer(), nullable=False, comment="Platform-internal fact version; corrections append new versions."),
        sa.Column("instrument_id", sa.Uuid(), nullable=False, comment="Stable instrument identity the fact describes."),
        sa.Column("rule_package_key", sa.Text(), nullable=False, comment="Rule package key this fact was authored against."),
        sa.Column("rule_package_version", sa.Integer(), nullable=False, comment="Rule package version this fact was authored against."),
        sa.Column("rule_exception_key", sa.Text(), nullable=True, comment="Named exception identity when the row is an exception-sourced fact."),
        sa.Column("rule_exception_version", sa.Integer(), nullable=True, comment="Version of the named exception identity; NULL together with rule_exception_key."),
        sa.Column("valid_from", sa.Date(), nullable=False, comment="First day (inclusive) of the fact validity window."),
        sa.Column("valid_to", sa.Date(), nullable=True, comment="First day (exclusive) after the fact stops being effective; NULL means open-ended."),
        # JSONB is a storage form only: the resolver keeps validating every
        # field; decimals are canonical strings so no JSON floats exist.
        sa.Column("fields", _json_storage_type(), nullable=False, comment="Raw rule fields as a JSON object with canonical decimal strings; validated by the rule package resolver."),
        sa.Column("source", sa.Text(), nullable=False, comment="Source system that provided the fact."),
        sa.Column("source_revision", sa.Text(), nullable=True, comment="External source revision; never substitutes for fact_key + fact_version."),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False, comment="Time at which the fact became known; PIT visibility boundary."),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, comment="Time at which the fact was observed in the source data."),
        sa.Column("quality_status", sa.String(length=16), nullable=False, comment="complete or incomplete; incomplete facts never pass formal preflight."),
        sa.Column("fixture_only", sa.Boolean(), nullable=False, comment="True only for test fixtures; formal mode rejects them."),
        sa.Column("content_hash", sa.String(length=64), nullable=False, comment="SHA-256 over the row's canonical content for drift detection."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the row was stored."),
        sa.CheckConstraint("length(trim(fact_key)) > 0", name="fact_key_not_blank"),
        sa.CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(trim(content_hash)) > 0", name="content_hash_not_blank"),
        sa.CheckConstraint("fact_version > 0", name="fact_version_positive"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_interval_ordered"),
        _json_object_check("fields", "fields_is_json_object"),
        sa.CheckConstraint("quality_status IN ('complete', 'incomplete')", name="quality_status_known"),
        sa.CheckConstraint(
            "(rule_exception_key IS NULL AND rule_exception_version IS NULL) "
            "OR (rule_exception_key IS NOT NULL AND rule_exception_version IS NOT NULL)",
            name="exception_reference_paired",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_instrument_rule_facts_instrument_id_instruments"),
        sa.PrimaryKeyConstraint("id", name="pk_instrument_rule_facts"),
        sa.UniqueConstraint("fact_key", "fact_version", name="uq_fact_key_version"),
        comment="Append-only versioned instrument rule facts; no production defaults.",
    )
    op.create_index(
        "ix_instrument_rule_facts_instrument_package_window",
        "instrument_rule_facts",
        ["instrument_id", "rule_package_key", "rule_package_version", "valid_from"],
    )
    op.create_index(
        "ix_instrument_rule_facts_known_at", "instrument_rule_facts", ["known_at"]
    )

    op.create_table(
        "instrument_rule_exception_sets",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Immutable set-row identifier."),
        sa.Column("set_key", sa.Text(), nullable=False, comment="Named exception set key; paired with set_version."),
        sa.Column("set_version", sa.Integer(), nullable=False, comment="Named exception set version; corrections append new versions."),
        sa.Column("rule_package_key", sa.Text(), nullable=False, comment="Rule package key the set routes exceptions for."),
        sa.Column("rule_package_version", sa.Integer(), nullable=False, comment="Rule package version the set routes exceptions for."),
        sa.Column("source", sa.Text(), nullable=False, comment="Source system that provided the set."),
        sa.Column("source_revision", sa.Text(), nullable=True, comment="External source revision of the set."),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False, comment="Time at which the set became known; PIT visibility boundary."),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, comment="Time at which the set was observed in the source data."),
        sa.Column("quality_status", sa.String(length=16), nullable=False, comment="complete or incomplete; incomplete sets never pass formal preflight."),
        sa.Column("fixture_only", sa.Boolean(), nullable=False, comment="True only for test fixtures; formal mode rejects them."),
        sa.Column("content_hash", sa.String(length=64), nullable=False, comment="Order-independent SHA-256 over the set reference and sorted entries."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the row was stored."),
        sa.CheckConstraint("length(trim(set_key)) > 0", name="set_key_not_blank"),
        sa.CheckConstraint("length(trim(rule_package_key)) > 0", name="package_key_not_blank"),
        sa.CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(trim(content_hash)) > 0", name="content_hash_not_blank"),
        sa.CheckConstraint("set_version > 0", name="set_version_positive"),
        sa.CheckConstraint("rule_package_version > 0", name="package_version_positive"),
        sa.CheckConstraint("quality_status IN ('complete', 'incomplete')", name="quality_status_known"),
        sa.PrimaryKeyConstraint("id", name="pk_instrument_rule_exception_sets"),
        sa.UniqueConstraint("set_key", "set_version", name="uq_exception_set_key_version"),
        comment="Versioned named-exception sets carrying references only, never production values.",
    )
    op.create_index(
        "ix_instrument_rule_exception_sets_package",
        "instrument_rule_exception_sets",
        ["rule_package_key", "rule_package_version"],
    )

    op.create_table(
        "instrument_rule_exception_entries",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Immutable entry identifier."),
        sa.Column("set_key", sa.Text(), nullable=False, comment="Exception set key this entry belongs to."),
        sa.Column("set_version", sa.Integer(), nullable=False, comment="Exception set version this entry belongs to."),
        sa.Column("instrument_id", sa.Uuid(), nullable=False, comment="Stable instrument identity routed to the exception fact."),
        sa.Column("exception_fact_key", sa.Text(), nullable=False, comment="Fact key of the independently sourced exception fact."),
        sa.Column("exception_fact_version", sa.Integer(), nullable=False, comment="Fact version of the exception fact."),
        sa.Column("valid_from", sa.Date(), nullable=False, comment="First day (inclusive) on which the routing applies."),
        sa.Column("valid_to", sa.Date(), nullable=True, comment="First day (exclusive) after the routing stops; NULL means open-ended."),
        sa.CheckConstraint("length(trim(exception_fact_key)) > 0", name="exception_fact_key_not_blank"),
        sa.CheckConstraint("exception_fact_version > 0", name="exception_fact_version_positive"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_interval_ordered"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name="fk_instrument_rule_exception_entries_instrument_id_instruments",
        ),
        sa.ForeignKeyConstraint(
            ["set_key", "set_version"],
            [
                "instrument_rule_exception_sets.set_key",
                "instrument_rule_exception_sets.set_version",
            ],
            name="fk_exception_entries_set",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_instrument_rule_exception_entries"),
        comment="Routing entries instrument + interval -> exception fact reference.",
    )
    op.create_index(
        "ix_instrument_rule_exception_entries_instrument_window",
        "instrument_rule_exception_entries",
        ["instrument_id", "valid_from"],
    )

    op.create_table(
        "backtest_run_rule_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Immutable snapshot-row identifier."),
        sa.Column("run_id", sa.Uuid(), nullable=False, comment="Backtest run the snapshot belongs to; exactly one snapshot per run."),
        sa.Column("rule_package_key", sa.Text(), nullable=False, comment="Selected rule package key frozen for this run."),
        sa.Column("rule_package_version", sa.Integer(), nullable=False, comment="Selected rule package version frozen for this run."),
        sa.Column("rule_package_semantic_hash", sa.String(length=64), nullable=False, comment="Semantic hash of the selected rule package definition."),
        sa.Column("parser_revision", sa.Text(), nullable=False, comment="Resolver parser revision used to produce every segment."),
        sa.Column("exception_set_key", sa.Text(), nullable=True, comment="Selected named exception set key, if any."),
        sa.Column("exception_set_version", sa.Integer(), nullable=True, comment="Selected named exception set version, if any."),
        sa.Column("exception_set_hash", sa.String(length=64), nullable=True, comment="Content hash of the selected exception set, if any."),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False, comment="Knowledge cutoff used while resolving all segments."),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False, comment="Total SHA-256 over the frozen run selection and all segments."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the snapshot was written."),
        sa.CheckConstraint("length(trim(rule_package_key)) > 0", name="package_key_not_blank"),
        sa.CheckConstraint("rule_package_version > 0", name="package_version_positive"),
        sa.CheckConstraint("length(trim(rule_package_semantic_hash)) > 0", name="semantic_hash_not_blank"),
        sa.CheckConstraint("length(trim(parser_revision)) > 0", name="parser_revision_not_blank"),
        sa.CheckConstraint("length(trim(snapshot_hash)) > 0", name="snapshot_hash_not_blank"),
        sa.CheckConstraint(
            "(exception_set_key IS NULL AND exception_set_version IS NULL "
            "AND exception_set_hash IS NULL) OR "
            "(exception_set_key IS NOT NULL AND exception_set_version IS NOT NULL "
            "AND exception_set_hash IS NOT NULL)",
            name="exception_set_paired",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backtest_run_rule_snapshots"),
        sa.UniqueConstraint("run_id", name="uq_backtest_run_rule_snapshots_run_id"),
        comment="Run-level frozen rule snapshot; written once after a ready preflight.",
    )

    op.create_table(
        "backtest_run_instrument_rule_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Immutable segment identifier."),
        sa.Column("run_id", sa.Uuid(), nullable=False, comment="Backtest run the segment belongs to; no backtest_runs foreign key yet."),
        sa.Column("instrument_id", sa.Uuid(), nullable=False, comment="Stable instrument identity whose rules are frozen."),
        sa.Column("effective_from", sa.Date(), nullable=False, comment="First day (inclusive) of the frozen validity segment."),
        sa.Column("effective_to", sa.Date(), nullable=True, comment="First day (exclusive) after the segment ends; NULL extends to the run end."),
        sa.Column("normal_fact_key", sa.Text(), nullable=False, comment="Fact key of the ordinary fact actually used in this segment."),
        sa.Column("normal_fact_version", sa.Integer(), nullable=False, comment="Fact version of the ordinary fact actually used."),
        sa.Column("exception_fact_key", sa.Text(), nullable=True, comment="Fact key of the exception fact used, if an exception matched."),
        sa.Column("exception_fact_version", sa.Integer(), nullable=True, comment="Fact version of the exception fact used, if any."),
        sa.Column("normalized_values", _json_storage_type(), nullable=False, comment="Frozen normalized rule values; restorable verbatim."),
        sa.Column("capability_declarations", _json_storage_type(), nullable=False, comment="Frozen capability declarations including not_applicable entries."),
        sa.Column("provenance", _json_storage_type(), nullable=False, comment="Full fact provenance: keys, versions, sources, revisions, windows, knowledge times, quality and fixture flags."),
        sa.Column("resolution_hash", sa.String(length=64), nullable=False, comment="Semantic hash of the resolver outcome frozen into this segment."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the segment was written."),
        sa.CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="effective_interval_ordered"),
        sa.CheckConstraint("normal_fact_version > 0", name="normal_fact_version_positive"),
        sa.CheckConstraint(
            "(exception_fact_key IS NULL AND exception_fact_version IS NULL) OR "
            "(exception_fact_key IS NOT NULL AND exception_fact_version IS NOT NULL)",
            name="exception_fact_paired",
        ),
        _json_object_check("normalized_values", "normalized_values_is_json_object"),
        _json_object_check("capability_declarations", "capability_declarations_is_json_object"),
        _json_object_check("provenance", "provenance_is_json_object"),
        sa.CheckConstraint("length(trim(resolution_hash)) > 0", name="resolution_hash_not_blank"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name="fk_run_instrument_rule_snapshots_instrument",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backtest_run_instrument_rule_snapshots"),
        sa.UniqueConstraint(
            "run_id",
            "instrument_id",
            "effective_from",
            name="uq_backtest_run_instrument_rule_snapshots_segment",
        ),
        comment="Per-instrument frozen rule segments of one run; execution reads only these.",
    )
    op.create_index(
        "ix_backtest_run_instrument_rule_snapshots_run",
        "backtest_run_instrument_rule_snapshots",
        ["run_id", "instrument_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backtest_run_instrument_rule_snapshots_run",
        table_name="backtest_run_instrument_rule_snapshots",
    )
    op.drop_table("backtest_run_instrument_rule_snapshots")
    op.drop_table("backtest_run_rule_snapshots")
    op.drop_index(
        "ix_instrument_rule_exception_entries_instrument_window",
        table_name="instrument_rule_exception_entries",
    )
    op.drop_table("instrument_rule_exception_entries")
    op.drop_index(
        "ix_instrument_rule_exception_sets_package",
        table_name="instrument_rule_exception_sets",
    )
    op.drop_table("instrument_rule_exception_sets")
    op.drop_index(
        "ix_instrument_rule_facts_known_at", table_name="instrument_rule_facts"
    )
    op.drop_index(
        "ix_instrument_rule_facts_instrument_package_window",
        table_name="instrument_rule_facts",
    )
    op.drop_table("instrument_rule_facts")
