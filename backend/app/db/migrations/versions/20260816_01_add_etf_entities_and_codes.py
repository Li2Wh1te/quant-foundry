"""Add ETF entities, trading codes, and mapping audits.

Revision ID: 20260816_01
Revises: 20260815_03
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260816_01"
down_revision: str | None = "20260815_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "etf_entities",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Local ETF economic identity."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the entity was first created."),
        sa.PrimaryKeyConstraint("id", name="pk_etf_entities"),
        comment="Stores local ETF identities that can span verified code changes.",
    )
    op.create_table(
        "etf_codes",
        sa.Column("source", sa.String(length=32), nullable=False, comment="Source system that assigned ts_code."),
        sa.Column("ts_code", sa.String(length=16), nullable=False, comment="Source-specific ETF trading code."),
        sa.Column("etf_id", sa.Uuid(), nullable=False, comment="Local economic ETF identity."),
        sa.Column("csname", sa.String(length=256), nullable=True, comment="Tushare ETF Chinese short name."),
        sa.Column("extname", sa.String(length=256), nullable=True, comment="Tushare ETF extended name."),
        sa.Column("cname", sa.String(length=512), nullable=True, comment="Tushare ETF Chinese full name."),
        sa.Column("index_code", sa.String(length=32), nullable=True, comment="Tracked index code."),
        sa.Column("index_name", sa.String(length=512), nullable=True, comment="Tracked index full name."),
        sa.Column("setup_date", sa.Date(), nullable=True, comment="Fund establishment date."),
        sa.Column("list_date", sa.Date(), nullable=True, comment="Exchange listing date."),
        sa.Column("list_status", sa.String(length=8), nullable=False, comment="Current Tushare listing status."),
        sa.Column("exchange", sa.String(length=16), nullable=False, comment="Tushare exchange code."),
        sa.Column("mgr_name", sa.String(length=256), nullable=True, comment="Fund manager short name."),
        sa.Column("custod_name", sa.String(length=256), nullable=True, comment="Fund custodian name."),
        sa.Column("mgt_fee", sa.Numeric(precision=10, scale=6), nullable=True, comment="Fund management fee."),
        sa.Column("etf_type", sa.String(length=32), nullable=True, comment="Tushare investment channel type."),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, comment="First successful full refresh that contained this code."),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, comment="Most recent successful full refresh that contained this code."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the ETF was first stored."),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which source fields last changed."),
        sa.CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(btrim(ts_code)) > 0", name="ts_code_not_blank"),
        sa.CheckConstraint("length(btrim(list_status)) > 0", name="status_not_blank"),
        sa.CheckConstraint("length(btrim(exchange)) > 0", name="exchange_not_blank"),
        sa.CheckConstraint("mgt_fee IS NULL OR mgt_fee >= 0", name="management_fee_not_negative"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.ForeignKeyConstraint(["etf_id"], ["etf_entities.id"], name="fk_etf_codes_etf_id_etf_entities"),
        sa.PrimaryKeyConstraint("source", "ts_code", name="pk_etf_codes"),
        comment="Stores the latest complete ETF reference record for each source-specific trading code.",
    )
    op.create_index("ix_etf_codes_entity", "etf_codes", ["etf_id"])
    op.create_index("ix_etf_codes_status_exchange", "etf_codes", ["list_status", "exchange"])
    op.create_index("ix_etf_codes_index_code", "etf_codes", ["index_code"])
    op.create_table(
        "etf_code_mapping_audits",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Immutable mapping-audit identifier."),
        sa.Column("source", sa.String(length=32), nullable=False, comment="Source system that assigned ts_code."),
        sa.Column("ts_code", sa.String(length=16), nullable=False, comment="Reassigned source-specific trading code."),
        sa.Column("old_etf_id", sa.Uuid(), nullable=False, comment="Entity previously associated with the code."),
        sa.Column("new_etf_id", sa.Uuid(), nullable=False, comment="Verified entity now associated with the code."),
        sa.Column("mapping_source", sa.String(length=64), nullable=False, comment="Evidence source for the mapping decision."),
        sa.Column("evidence", sa.String(length=2048), nullable=True, comment="Evidence reference such as an announcement URL."),
        sa.Column("actor", sa.String(length=128), nullable=True, comment="Operator or workflow that made the mapping."),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, comment="Time at which the mapping was recorded."),
        sa.CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(btrim(ts_code)) > 0", name="ts_code_not_blank"),
        sa.CheckConstraint("length(btrim(mapping_source)) > 0", name="mapping_source_not_blank"),
        sa.ForeignKeyConstraint(["source", "ts_code"], ["etf_codes.source", "etf_codes.ts_code"], name="fk_etf_code_mapping_audits_source_ts_code_etf_codes"),
        sa.ForeignKeyConstraint(["old_etf_id"], ["etf_entities.id"], name="fk_etf_code_mapping_audits_old_etf_id_etf_entities"),
        sa.ForeignKeyConstraint(["new_etf_id"], ["etf_entities.id"], name="fk_etf_code_mapping_audits_new_etf_id_etf_entities"),
        sa.PrimaryKeyConstraint("id", name="pk_etf_code_mapping_audits"),
        comment="Audits explicit, evidenced corrections to ETF code-to-entity mappings.",
    )
    op.create_index("ix_etf_code_mapping_audits_code", "etf_code_mapping_audits", ["source", "ts_code", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_table("etf_code_mapping_audits")
    op.drop_table("etf_codes")
    op.drop_table("etf_entities")
