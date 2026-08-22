"""Add generic instruments and PIT code mappings; absorb ETF identities.

Revision ID: 20260822_03
Revises: 20260822_02
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_03"
down_revision: str | None = "20260822_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Stable instrument identity that survives code and name changes."),
        sa.Column("asset_class", sa.String(length=32), nullable=False, comment="Asset-class partition of the identity space."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the identity was first created."),
        sa.CheckConstraint("length(btrim(asset_class)) > 0", name="asset_class_not_blank"),
        sa.PrimaryKeyConstraint("id", name="pk_instruments"),
        comment="Generic stable instrument identities shared by every asset class.",
    )
    op.create_table(
        "instrument_code_mappings",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Immutable mapping-row identifier."),
        sa.Column("instrument_id", sa.Uuid(), nullable=False, comment="Stable instrument identity the code maps to."),
        sa.Column("source", sa.String(length=32), nullable=False, comment="Source system that assigned source_code."),
        sa.Column("source_code", sa.String(length=32), nullable=False, comment="Source-specific instrument code, for example 510300.SH."),
        sa.Column("trading_code", sa.String(length=16), nullable=False, comment="User-facing display trading code, for example 510300."),
        sa.Column("valid_from", sa.Date(), nullable=False, comment="First day (inclusive) on which the mapping is effective."),
        sa.Column("valid_to", sa.Date(), nullable=True, comment="First day (exclusive) after the mapping stops being effective; NULL means still effective."),
        sa.Column("source_revision", sa.String(length=64), nullable=True, comment="Exact source snapshot or revision this fact came from."),
        sa.Column("mapping_source", sa.String(length=64), nullable=False, comment="Evidence channel that established the mapping."),
        sa.Column("evidence", sa.String(length=2048), nullable=False, comment="Concrete evidence reference such as an announcement URL; mandatory for every mapping."),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False, comment="Time at which this fact became known; PIT visibility boundary."),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, comment="Time at which the fact was observed in the source data."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the row was stored."),
        sa.CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(btrim(source_code)) > 0", name="source_code_not_blank"),
        sa.CheckConstraint("length(btrim(trading_code)) > 0", name="trading_code_not_blank"),
        sa.CheckConstraint("length(btrim(mapping_source)) > 0", name="mapping_source_not_blank"),
        sa.CheckConstraint("length(btrim(evidence)) > 0", name="evidence_not_blank"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_interval_ordered"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_instrument_code_mappings_instrument_id_instruments"),
        sa.PrimaryKeyConstraint("id", name="pk_instrument_code_mappings"),
        comment="Append-only evidenced PIT windows mapping source codes to stable instrument identities.",
    )
    op.create_index(
        "ix_instrument_code_mappings_identity_window",
        "instrument_code_mappings",
        ["instrument_id", "source", "valid_from"],
    )
    op.create_index(
        "ix_instrument_code_mappings_source_code",
        "instrument_code_mappings",
        ["source", "source_code"],
    )

    # Absorb existing ETF identities into the generic identity space.  The
    # id values are preserved verbatim so every existing reference keeps
    # pointing at the same economic instrument.
    op.execute(
        "INSERT INTO instruments (id, asset_class, created_at) "
        "SELECT id, 'etf', created_at FROM etf_entities"
    )
    # Enforce that ETF identities are always a subset of the generic space.
    op.create_foreign_key(
        "fk_etf_entities_id_instruments",
        "etf_entities",
        "instruments",
        ["id"],
        ["id"],
    )

    # No historical instrument_code_mappings rows are fabricated here on
    # purpose: existing ETF reference data has no evidenced validity windows,
    # and guessing them would corrupt point-in-time semantics.  Missing
    # mappings stay missing and are blocked by later preflight checks.


def downgrade() -> None:
    op.drop_constraint(
        "fk_etf_entities_id_instruments", "etf_entities", type_="foreignkey"
    )
    op.drop_index(
        "ix_instrument_code_mappings_source_code", table_name="instrument_code_mappings"
    )
    op.drop_index(
        "ix_instrument_code_mappings_identity_window",
        table_name="instrument_code_mappings",
    )
    op.drop_table("instrument_code_mappings")
    op.drop_table("instruments")
