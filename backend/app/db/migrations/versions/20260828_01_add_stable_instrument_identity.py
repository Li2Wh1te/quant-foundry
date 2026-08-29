"""Add append-only identity/display facts and PIT mapping revisions.

Revision ID: 20260828_01
Revises: 20260825_04
Create Date: 2026-08-28

The migration deliberately does not manufacture historical mapping or
display facts from ``etf_codes``.  Existing mapping rows (if an operator has
already supplied them) receive a stable legacy logical key derived from the
immutable row id; no effective-history window is inferred from a mutable
current snapshot.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260828_01"
down_revision: str | None = "20260825_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgresql() -> bool:
    """Return whether the migration is running against PostgreSQL."""

    return op.get_bind().dialect.name == "postgresql"


def _date_range_type() -> sa.types.TypeEngine:
    """Use native date ranges in production and text in SQLite smoke tests."""

    return postgresql.DATERANGE() if _is_postgresql() else sa.Text()


def _timestamp_range_type() -> sa.types.TypeEngine:
    """Use native timestamp ranges in production and text in SQLite tests."""

    return postgresql.TSTZRANGE() if _is_postgresql() else sa.Text()


def _create_immutable_trigger(
    table_name: str,
    trigger_name: str,
    function_name: str,
) -> None:
    """Prevent UPDATE/DELETE on an append-only fact or audit table.

    PostgreSQL is the production database and owns the trigger semantics.
    SQLite migration runs are used only by lightweight schema tests, where
    the native range/exclusion features are unavailable; those tests still
    exercise repository-level append-only validation.
    """

    if not _is_postgresql():
        return
    op.execute(
        f"""CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; % is not allowed', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;"""
    )
    op.execute(
        f"""CREATE TRIGGER {trigger_name}
BEFORE UPDATE OR DELETE ON {table_name}
FOR EACH ROW EXECUTE FUNCTION {function_name}();"""
    )


def _drop_immutable_trigger(
    table_name: str,
    trigger_name: str,
    function_name: str,
) -> None:
    """Drop one append-only trigger and its private PostgreSQL function."""

    if not _is_postgresql():
        return
    op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
    op.execute(f"DROP FUNCTION IF EXISTS {function_name}()")


def _alter_column(
    table_name: str,
    column_name: str,
    **kwargs: object,
) -> None:
    """Alter a column on both PostgreSQL and SQLite migration harnesses.

    SQLite cannot execute ``ALTER TABLE ... ALTER COLUMN``.  Alembic's batch
    implementation recreates the table while preserving rows and indexes, so
    local migration smoke tests exercise the same nullability/default
    contract as production instead of stopping at the first DDL statement.
    """

    if _is_postgresql():
        op.alter_column(table_name, column_name, **kwargs)
        return
    with op.batch_alter_table(table_name, recreate="always") as batch:
        batch.alter_column(column_name, **kwargs)


def _create_check_constraint(
    constraint_name: str,
    table_name: str,
    condition: str,
) -> None:
    """Create a check constraint with SQLite batch-DDL compatibility."""

    if _is_postgresql():
        op.create_check_constraint(constraint_name, table_name, condition)
        return
    with op.batch_alter_table(table_name, recreate="always") as batch:
        batch.create_check_constraint(constraint_name, condition)


def _create_foreign_key(
    constraint_name: str,
    source_table: str,
    referent_table: str,
    local_cols: list[str],
    remote_cols: list[str],
) -> None:
    """Create a foreign key on PostgreSQL or through SQLite batch-DDL."""

    if _is_postgresql():
        op.create_foreign_key(
            constraint_name,
            source_table,
            referent_table,
            local_cols,
            remote_cols,
        )
        return
    with op.batch_alter_table(source_table, recreate="always") as batch:
        batch.create_foreign_key(
            constraint_name,
            referent_table,
            local_cols,
            remote_cols,
        )


def _create_unique_constraint(
    constraint_name: str,
    table_name: str,
    columns: list[str],
) -> None:
    """Create a unique constraint on PostgreSQL or SQLite batch-DDL."""

    if _is_postgresql():
        op.create_unique_constraint(constraint_name, table_name, columns)
        return
    with op.batch_alter_table(table_name, recreate="always") as batch:
        batch.create_unique_constraint(constraint_name, columns)


def _drop_constraint(
    constraint_name: str,
    table_name: str,
    constraint_type: str,
) -> None:
    """Drop a constraint using batch mode where SQLite requires recreation."""

    if _is_postgresql():
        op.drop_constraint(constraint_name, table_name, type_=constraint_type)
        return
    with op.batch_alter_table(table_name, recreate="always") as batch:
        batch.drop_constraint(constraint_name, type_=constraint_type)


def _drop_columns(table_name: str, *column_names: str) -> None:
    """Drop columns while keeping the SQLite migration path executable."""

    if _is_postgresql():
        for column_name in column_names:
            op.drop_column(table_name, column_name)
        return
    with op.batch_alter_table(table_name, recreate="always") as batch:
        for column_name in column_names:
            batch.drop_column(column_name)


def upgrade() -> None:
    """Create the stable identity fact and resolution-head schema."""

    if _is_postgresql():
        # btree_gist supplies equality/inequality operators for UUID/text in
        # the exclusion constraints below; the range types are native PG.
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # Lifecycle metadata belongs to the stable identity row, not to a source
    # catalogue's list_status.  Existing identities remain active.
    op.add_column(
        "instruments",
        sa.Column("status", sa.String(length=16), nullable=True, server_default="active"),
    )
    op.add_column(
        "instruments",
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
    )
    op.execute("UPDATE instruments SET status = 'active' WHERE status IS NULL")
    _alter_column("instruments", "status", nullable=False, server_default="active")
    _create_check_constraint(
        "instrument_status_known",
        "instruments",
        "status IN ('active', 'deprecated', 'merged')",
    )
    _create_check_constraint(
        "instrument_status_target_consistent",
        "instruments",
        "(status = 'merged' AND merged_into_id IS NOT NULL) OR "
        "(status IN ('active', 'deprecated') AND merged_into_id IS NULL)",
    )
    _create_check_constraint(
        "instrument_merge_target_distinct",
        "instruments",
        "merged_into_id IS NULL OR merged_into_id <> id",
    )
    _create_foreign_key(
        "fk_instruments_merged_into_id_instruments",
        "instruments",
        "instruments",
        ["merged_into_id"],
        ["id"],
    )

    # Extend the existing mapping fact table.  ``id`` remains its fact_id;
    # this migration does not create a parallel mapping-facts table.
    op.add_column(
        "instrument_code_mappings",
        sa.Column("fact_version", sa.Integer(), nullable=True, server_default="1"),
    )
    op.add_column(
        "instrument_code_mappings",
        sa.Column("logical_fact_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "instrument_code_mappings",
        sa.Column("supersedes_fact_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "instrument_code_mappings",
        sa.Column(
            "effective_range",
            _date_range_type(),
            nullable=True,
        ),
    )
    op.add_column(
        "instrument_code_mappings",
        sa.Column(
            "knowledge_range",
            _timestamp_range_type(),
            nullable=True,
        ),
    )
    # Legacy rows receive a row-id-based logical key only.  This is an audit
    # identity, not a guessed relationship between historical source codes.
    legacy_id_expression = "id::text" if _is_postgresql() else "CAST(id AS TEXT)"
    op.execute(
        "UPDATE instrument_code_mappings "
        f"SET fact_version = 1, logical_fact_key = 'legacy:' || {legacy_id_expression} "
        "WHERE fact_version IS NULL OR logical_fact_key IS NULL"
    )
    # Fact rows are immutable observations.  Their knowledge interval remains
    # open-ended; the logical-key inequality in the exclusion constraint
    # allows revisions in one chain while conflicting chains overlap forever.
    if _is_postgresql():
        op.execute(
            "UPDATE instrument_code_mappings "
            "SET effective_range = daterange(valid_from, valid_to, '[)'), "
            "knowledge_range = tstzrange(known_at, NULL, '[)') "
            "WHERE effective_range IS NULL OR knowledge_range IS NULL"
        )
    else:
        # SQLite has no range type.  Keep a deterministic textual projection
        # so schema smoke tests can still insert and inspect migrated rows;
        # production overlap enforcement remains PostgreSQL-only below.
        op.execute(
            "UPDATE instrument_code_mappings "
            "SET effective_range = '[' || CAST(valid_from AS TEXT) || ',' || "
            "COALESCE(CAST(valid_to AS TEXT), '') || ')', "
            "knowledge_range = '[' || CAST(known_at AS TEXT) || ',)' "
            "WHERE effective_range IS NULL OR knowledge_range IS NULL"
        )
    _alter_column("instrument_code_mappings", "fact_version", nullable=False)
    _alter_column("instrument_code_mappings", "logical_fact_key", nullable=False)
    _alter_column("instrument_code_mappings", "effective_range", nullable=False)
    _alter_column("instrument_code_mappings", "knowledge_range", nullable=False)
    _create_foreign_key(
        "fk_mapping_supersedes_fact_id_mapping",
        "instrument_code_mappings",
        "instrument_code_mappings",
        ["supersedes_fact_id"],
        ["id"],
    )
    _create_check_constraint(
        "fact_version_positive",
        "instrument_code_mappings",
        "fact_version > 0",
    )
    _create_check_constraint(
        "logical_fact_key_not_blank",
        "instrument_code_mappings",
        "length(trim(logical_fact_key)) > 0",
    )
    _create_unique_constraint(
        "uq_mapping_logical_fact_version",
        "instrument_code_mappings",
        ["logical_fact_key", "fact_version"],
    )
    op.create_index(
        "ix_instrument_code_mappings_known_at_effective",
        "instrument_code_mappings",
        ["known_at", "instrument_id", "source", "valid_from"],
    )
    if _is_postgresql():
        op.create_exclude_constraint(
            "ex_mapping_effective_knowledge_overlap",
            "instrument_code_mappings",
            ("instrument_id", "="),
            ("source", "="),
            ("logical_fact_key", "<>"),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
        )

        # The inequality operator means the same source code may be revised
        # for one identity, but can never be assigned to two identities in an
        # overlapping knowledge snapshot.
        op.create_exclude_constraint(
            "ex_mapping_source_code_identity_overlap",
            "instrument_code_mappings",
            ("source", "="),
            ("source_code", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            ("instrument_id", "<>"),
            using="gist",
        )

    # Fact rows and merge audits are append-only.  Resolution heads are
    # intentionally excluded: they are disposable indexes rebuilt by the
    # repository and therefore may be deleted/reinserted.
    _create_immutable_trigger(
        "instrument_code_mappings",
        "instrument_code_mappings_immutable",
        "prevent_instrument_code_mappings_mutation",
    )

    op.create_table(
        "instrument_identity_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version", sa.Integer(), nullable=False),
        sa.Column("logical_fact_key", sa.Text(), nullable=False),
        sa.Column("supersedes_fact_id", sa.Uuid(), nullable=True),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("calendar_id", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        # Persist both PIT axes explicitly.  PostgreSQL stores native range
        # values so the exclusion constraint below can reject conflicting
        # effective/knowledge snapshots; SQLite uses deterministic TEXT for
        # migration smoke tests.
        sa.Column("effective_range", _date_range_type(), nullable=False),
        sa.Column("knowledge_range", _timestamp_range_type(), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("fact_version > 0", name="identity_fact_version_positive"),
        sa.CheckConstraint("length(trim(asset_class)) > 0", name="identity_asset_class_not_blank"),
        sa.CheckConstraint("length(trim(currency)) > 0", name="identity_currency_not_blank"),
        sa.CheckConstraint("length(trim(calendar_id)) > 0", name="identity_calendar_not_blank"),
        sa.CheckConstraint("length(trim(evidence)) > 0", name="identity_evidence_not_blank"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="identity_valid_interval_ordered"),
        sa.UniqueConstraint("logical_fact_key", "fact_version", name="uq_identity_logical_fact_version"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_identity_facts_instrument_id_instruments"),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["instrument_identity_facts.id"], name="fk_identity_facts_supersedes"),
        comment="Append-only point-in-time identity facts; no current snapshot backfill.",
    )
    op.create_index(
        "ix_instrument_identity_facts_instrument_window",
        "instrument_identity_facts",
        ["instrument_id", "valid_from"],
    )
    op.create_index(
        "ix_instrument_identity_facts_known_at", "instrument_identity_facts", ["known_at"]
    )
    if _is_postgresql():
        op.create_exclude_constraint(
            "ex_identity_effective_knowledge_overlap",
            "instrument_identity_facts",
            ("instrument_id", "="),
            ("logical_fact_key", "<>"),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
        )
    _create_immutable_trigger(
        "instrument_identity_facts",
        "instrument_identity_facts_immutable",
        "prevent_instrument_identity_facts_mutation",
    )

    op.create_table(
        "instrument_display_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("fact_version", sa.Integer(), nullable=False),
        sa.Column("logical_fact_key", sa.Text(), nullable=False),
        sa.Column("supersedes_fact_id", sa.Uuid(), nullable=True),
        sa.Column("trading_code", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("display_name", sa.String(length=512), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_revision", sa.String(length=128), nullable=True),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("authority_rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("authority_status", sa.String(length=16), nullable=False, server_default="authoritative"),
        sa.Column("effective_range", _date_range_type(), nullable=False),
        sa.Column("knowledge_range", _timestamp_range_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("fact_version > 0", name="display_fact_version_positive"),
        sa.CheckConstraint("length(trim(source)) > 0", name="display_source_not_blank"),
        sa.CheckConstraint("length(trim(evidence)) > 0", name="display_evidence_not_blank"),
        sa.CheckConstraint("authority_rank >= 0", name="display_authority_rank_non_negative"),
        sa.CheckConstraint("authority_status IN ('authoritative', 'pending', 'rejected')", name="display_authority_status_known"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="display_valid_interval_ordered"),
        sa.UniqueConstraint("logical_fact_key", "fact_version", name="uq_display_logical_fact_version"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_display_facts_instrument_id_instruments"),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["instrument_display_facts.id"], name="fk_display_facts_supersedes"),
        comment="Append-only point-in-time display labels; etf_codes is not a PIT source.",
    )
    op.create_index(
        "ix_instrument_display_facts_instrument_window",
        "instrument_display_facts",
        ["instrument_id", "valid_from"],
    )
    op.create_index(
        "ix_instrument_display_facts_known_at", "instrument_display_facts", ["known_at"]
    )
    if _is_postgresql():
        op.create_exclude_constraint(
            "ex_display_effective_knowledge_overlap",
            "instrument_display_facts",
            ("instrument_id", "="),
            ("logical_fact_key", "<>"),
            ("authority_rank", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            where="authority_status = 'authoritative'",
        )
    _create_immutable_trigger(
        "instrument_display_facts",
        "instrument_display_facts_immutable",
        "prevent_instrument_display_facts_mutation",
    )

    op.create_table(
        "display_resolution_heads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("logical_fact_key", sa.Text(), nullable=False),
        sa.Column("knowledge_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("authority_rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effective_range", _date_range_type(), nullable=False),
        sa.Column("knowledge_range", _timestamp_range_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_display_resolution_heads"),
        sa.UniqueConstraint("logical_fact_key", "knowledge_from", name="uq_display_head_logical_knowledge"),
        sa.ForeignKeyConstraint(["fact_id"], ["instrument_display_facts.id"], name="fk_display_heads_fact_id_facts"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_display_heads_instrument_id_instruments"),
        comment="Rebuildable PIT display resolution index; never a second fact source.",
    )
    op.create_index(
        "ix_display_resolution_heads_lookup",
        "display_resolution_heads",
        ["instrument_id", "knowledge_from"],
    )
    if _is_postgresql():
        op.create_exclude_constraint(
            "ex_display_heads_effective_knowledge_overlap",
            "display_resolution_heads",
            ("instrument_id", "="),
            ("logical_fact_key", "<>"),
            ("authority_rank", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
        )

    op.create_table(
        "mapping_resolution_heads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("logical_fact_key", sa.Text(), nullable=False),
        sa.Column("knowledge_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_code", sa.String(length=32), nullable=False),
        sa.Column("effective_range", _date_range_type(), nullable=False),
        sa.Column("knowledge_range", _timestamp_range_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_mapping_resolution_heads"),
        sa.UniqueConstraint("logical_fact_key", "knowledge_from", name="uq_mapping_head_logical_knowledge"),
        sa.ForeignKeyConstraint(["fact_id"], ["instrument_code_mappings.id"], name="fk_mapping_heads_fact_id_mappings"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_mapping_heads_instrument_id_instruments"),
        comment="Rebuildable PIT mapping resolution index; never a second fact source.",
    )
    op.create_index(
        "ix_mapping_resolution_heads_lookup",
        "mapping_resolution_heads",
        ["instrument_id", "source", "knowledge_from"],
    )
    if _is_postgresql():
        op.create_exclude_constraint(
            "ex_mapping_heads_effective_knowledge_overlap",
            "mapping_resolution_heads",
            ("instrument_id", "="),
            ("source", "="),
            ("logical_fact_key", "<>"),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
        )
        op.create_exclude_constraint(
            "ex_mapping_heads_source_code_identity_overlap",
            "mapping_resolution_heads",
            ("source", "="),
            ("source_code", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            ("instrument_id", "<>"),
            using="gist",
        )

    op.create_table(
        "instrument_identity_merge_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_instrument_id", sa.Uuid(), nullable=False),
        sa.Column("target_instrument_id", sa.Uuid(), nullable=False),
        sa.Column("mapping_source", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=256), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_instrument_identity_merge_audits"),
        sa.ForeignKeyConstraint(["source_instrument_id"], ["instruments.id"], name="fk_merge_audits_source_instruments"),
        sa.ForeignKeyConstraint(["target_instrument_id"], ["instruments.id"], name="fk_merge_audits_target_instruments"),
        sa.CheckConstraint("outcome IN ('accepted', 'rejected')", name="merge_outcome_known"),
        sa.CheckConstraint("length(trim(mapping_source)) > 0", name="merge_mapping_source_not_blank"),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND length(trim(evidence)) > 0) OR outcome = 'rejected'",
            name="merge_evidence_when_accepted",
        ),
        comment="Evidence audit for accepted and rejected stable-identity merges.",
    )
    op.create_index(
        "ix_instrument_identity_merge_audits_source",
        "instrument_identity_merge_audits",
        ["source_instrument_id", "created_at"],
    )
    _create_immutable_trigger(
        "instrument_identity_merge_audits",
        "instrument_identity_merge_audits_immutable",
        "prevent_instrument_identity_merge_audits_mutation",
    )


def downgrade() -> None:
    """Remove only task-10 schema while preserving pre-existing identity ids."""

    _drop_immutable_trigger(
        "instrument_identity_merge_audits",
        "instrument_identity_merge_audits_immutable",
        "prevent_instrument_identity_merge_audits_mutation",
    )
    op.drop_index(
        "ix_instrument_identity_merge_audits_source",
        table_name="instrument_identity_merge_audits",
    )
    op.drop_table("instrument_identity_merge_audits")
    _drop_immutable_trigger(
        "instrument_display_facts",
        "instrument_display_facts_immutable",
        "prevent_instrument_display_facts_mutation",
    )
    if _is_postgresql():
        op.drop_constraint(
            "ex_mapping_heads_source_code_identity_overlap",
            "mapping_resolution_heads",
            type_="exclude",
        )
        op.drop_constraint("ex_mapping_heads_effective_knowledge_overlap", "mapping_resolution_heads", type_="exclude")
    op.drop_index(
        "ix_mapping_resolution_heads_lookup", table_name="mapping_resolution_heads"
    )
    op.drop_table("mapping_resolution_heads")
    if _is_postgresql():
        op.drop_constraint("ex_display_heads_effective_knowledge_overlap", "display_resolution_heads", type_="exclude")
    op.drop_index(
        "ix_display_resolution_heads_lookup", table_name="display_resolution_heads"
    )
    op.drop_table("display_resolution_heads")
    if _is_postgresql():
        op.drop_constraint("ex_display_effective_knowledge_overlap", "instrument_display_facts", type_="exclude")
    op.drop_index(
        "ix_instrument_display_facts_known_at", table_name="instrument_display_facts"
    )
    op.drop_index(
        "ix_instrument_display_facts_instrument_window",
        table_name="instrument_display_facts",
    )
    op.drop_table("instrument_display_facts")
    _drop_immutable_trigger(
        "instrument_identity_facts",
        "instrument_identity_facts_immutable",
        "prevent_instrument_identity_facts_mutation",
    )
    if _is_postgresql():
        op.drop_constraint(
            "ex_identity_effective_knowledge_overlap",
            "instrument_identity_facts",
            type_="exclude",
        )
    op.drop_index(
        "ix_instrument_identity_facts_known_at",
        table_name="instrument_identity_facts",
    )
    op.drop_index(
        "ix_instrument_identity_facts_instrument_window",
        table_name="instrument_identity_facts",
    )
    op.drop_table("instrument_identity_facts")
    _drop_immutable_trigger(
        "instrument_code_mappings",
        "instrument_code_mappings_immutable",
        "prevent_instrument_code_mappings_mutation",
    )
    if _is_postgresql():
        op.drop_constraint("ex_mapping_source_code_identity_overlap", "instrument_code_mappings", type_="exclude")
        op.drop_constraint("ex_mapping_effective_knowledge_overlap", "instrument_code_mappings", type_="exclude")
    op.drop_index(
        "ix_instrument_code_mappings_known_at_effective",
        table_name="instrument_code_mappings",
    )
    _drop_constraint(
        "uq_mapping_logical_fact_version",
        "instrument_code_mappings",
        "unique",
    )
    _drop_constraint(
        "fk_mapping_supersedes_fact_id_mapping",
        "instrument_code_mappings",
        "foreignkey",
    )
    _drop_constraint(
        "logical_fact_key_not_blank",
        "instrument_code_mappings",
        "check",
    )
    _drop_constraint(
        "fact_version_positive",
        "instrument_code_mappings",
        "check",
    )
    _drop_columns(
        "instrument_code_mappings",
        "knowledge_range",
        "effective_range",
        "supersedes_fact_id",
        "logical_fact_key",
        "fact_version",
    )
    _drop_constraint(
        "fk_instruments_merged_into_id_instruments",
        "instruments",
        "foreignkey",
    )
    _drop_constraint(
        "instrument_merge_target_distinct",
        "instruments",
        "check",
    )
    _drop_constraint(
        "instrument_status_target_consistent",
        "instruments",
        "check",
    )
    _drop_constraint("instrument_status_known", "instruments", "check")
    _drop_columns("instruments", "merged_into_id", "status")
