"""Add private database-backed strategy storage.

Revision ID: 20260819_01
Revises: 20260816_03
Create Date: 2026-08-19
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260819_01"
down_revision: str | None = "20260816_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create mutable drafts and append-only published strategy revisions."""
    op.create_table(
        "strategies",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            comment="Application-generated UUID for one private strategy.",
        ),
        sa.Column(
            "name",
            sa.String(length=100),
            nullable=False,
            comment="Human-readable private strategy name.",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Optional private strategy description.",
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="active",
            nullable=False,
            comment="Strategy lifecycle state; archival retains private history.",
        ),
        sa.Column(
            "current_revision_id",
            sa.Uuid(),
            nullable=True,
            comment="Latest published revision owned by this strategy.",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
            comment="Optimistic-locking version for strategy metadata.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Strategy creation timestamp.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Latest metadata or current-revision update timestamp.",
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint(
            "state IN ('active', 'archived')", name="state_supported"
        ),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.PrimaryKeyConstraint("id", name="pk_strategies"),
        comment=(
            "Private strategy identities. Source code is stored only in related "
            "database rows, never in the application checkout."
        ),
    )
    op.create_index(
        "ix_strategies_state_updated_at",
        "strategies",
        ["state", sa.text("updated_at DESC")],
    )

    op.create_table(
        "strategy_drafts",
        sa.Column(
            "strategy_id",
            sa.Uuid(),
            nullable=False,
            comment="Private strategy that owns this sole mutable draft.",
        ),
        sa.Column(
            "source_code",
            sa.Text(),
            nullable=False,
            comment="Current private single-module strategy source text.",
        ),
        sa.Column(
            "source_hash",
            sa.String(length=64),
            nullable=False,
            comment="Lowercase SHA-256 digest of source_code UTF-8 bytes.",
        ),
        sa.Column(
            "parameter_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="JSON Schema object for private strategy parameters.",
        ),
        sa.Column(
            "default_parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="Default parameter JSON object paired with the draft source.",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
            comment="Optimistic-locking version for source-editor saves.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Draft creation timestamp.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Latest successful draft save timestamp.",
        ),
        sa.CheckConstraint(
            "length(btrim(source_code)) > 0", name="source_code_not_blank"
        ),
        sa.CheckConstraint(
            "octet_length(source_code) <= 1048576", name="source_code_size"
        ),
        sa.CheckConstraint(
            "source_hash ~ '^[0-9a-f]{64}$'", name="source_hash_sha256"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameter_schema) = 'object'",
            name="parameter_schema_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(default_parameters) = 'object'",
            name="default_parameters_object",
        ),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name="fk_strategy_drafts_strategy_id_strategies",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("strategy_id", name="pk_strategy_drafts"),
        comment=(
            "One mutable private source draft per strategy. The database is the "
            "only persistent source location."
        ),
    )

    op.create_table(
        "strategy_revisions",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            comment="Application-generated UUID for one immutable revision.",
        ),
        sa.Column(
            "strategy_id",
            sa.Uuid(),
            nullable=False,
            comment="Private strategy that owns this published revision.",
        ),
        sa.Column(
            "revision_number",
            sa.Integer(),
            nullable=False,
            comment="Monotonically increasing revision number per strategy.",
        ),
        sa.Column(
            "source_code",
            sa.Text(),
            nullable=False,
            comment="Immutable private source snapshot used by future runs.",
        ),
        sa.Column(
            "source_hash",
            sa.String(length=64),
            nullable=False,
            comment="Lowercase SHA-256 digest of immutable source_code bytes.",
        ),
        sa.Column(
            "parameter_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Immutable JSON Schema snapshot for strategy parameters.",
        ),
        sa.Column(
            "default_parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Immutable default parameter snapshot.",
        ),
        sa.Column(
            "runtime_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Immutable runtime and strategy-contract compatibility metadata.",
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp at which this revision became executable.",
        ),
        sa.CheckConstraint(
            "revision_number > 0", name="revision_number_positive"
        ),
        sa.CheckConstraint(
            "length(btrim(source_code)) > 0", name="source_code_not_blank"
        ),
        sa.CheckConstraint(
            "octet_length(source_code) <= 1048576", name="source_code_size"
        ),
        sa.CheckConstraint(
            "source_hash ~ '^[0-9a-f]{64}$'", name="source_hash_sha256"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameter_schema) = 'object'",
            name="parameter_schema_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(default_parameters) = 'object'",
            name="default_parameters_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(runtime_manifest) = 'object'",
            name="runtime_manifest_object",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name="fk_strategy_revisions_strategy_id_strategies",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_revisions"),
        sa.UniqueConstraint(
            "strategy_id",
            "revision_number",
            name="uq_strategy_revisions_strategy_revision_number",
        ),
        # This redundant-looking pair is required because PostgreSQL only permits
        # a composite foreign key to reference an exact unique column set.
        sa.UniqueConstraint(
            "strategy_id",
            "id",
            name="uq_strategy_revisions_strategy_id_id",
        ),
        comment=(
            "Append-only private strategy snapshots. A database trigger rejects "
            "updates and deletes after publication."
        ),
    )
    op.create_index(
        "ix_strategy_revisions_strategy_revision",
        "strategy_revisions",
        ["strategy_id", sa.text("revision_number DESC")],
    )

    # A normal single-column FK could point current_revision_id at another
    # strategy's revision. Pairing the strategy ID with the revision ID keeps the
    # ownership invariant inside PostgreSQL instead of relying on API discipline.
    op.create_foreign_key(
        "fk_strategies_current_revision_belongs_to_strategy",
        "strategies",
        "strategy_revisions",
        ["id", "current_revision_id"],
        ["strategy_id", "id"],
        ondelete="RESTRICT",
    )

    # Immutability belongs in the database because a future maintenance script or
    # alternate API client must not be able to rewrite an already published run.
    op.execute(
        """
        CREATE FUNCTION prevent_strategy_revision_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy revisions are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER strategy_revisions_immutable
        BEFORE UPDATE OR DELETE ON strategy_revisions
        FOR EACH ROW EXECUTE FUNCTION prevent_strategy_revision_mutation();
        """
    )


def downgrade() -> None:
    """Remove strategy storage in dependency-safe reverse order."""
    op.execute("DROP TRIGGER IF EXISTS strategy_revisions_immutable ON strategy_revisions")
    op.execute("DROP FUNCTION IF EXISTS prevent_strategy_revision_mutation()")
    op.drop_constraint(
        "fk_strategies_current_revision_belongs_to_strategy",
        "strategies",
        type_="foreignkey",
    )
    op.drop_table("strategy_revisions")
    op.drop_table("strategy_drafts")
    op.drop_table("strategies")
