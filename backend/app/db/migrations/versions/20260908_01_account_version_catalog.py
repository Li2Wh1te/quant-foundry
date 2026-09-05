"""Persist immutable account versions and backfill the current definition."""

from alembic import op
import sqlalchemy as sa
from uuid import UUID
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260908_01"
down_revision = "20260907_01"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "backtest_account_profile_versions",
        sa.Column("profile_id", sa.Uuid(), sa.ForeignKey("backtest_account_profiles.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("snapshot", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("status IN ('active', 'inactive', 'retired')", name="status_supported"),
    )
    bind = op.get_bind()
    source = sa.Table("backtest_account_profiles", sa.MetaData(), autoload_with=bind)
    # Older definitions overwritten before this migration cannot be invented.
    # Preserve exactly the current known version; existing run snapshots remain
    # authoritative for historical runs that precede this catalogue.
    for row in bind.execute(sa.select(source)).mappings():
        bind.execute(table.insert().values(
            profile_id=UUID(str(row["id"])), version=row["version"], status=row["status"],
            snapshot={key: row[key] for key in ("name", "fee_schedule_key", "fee_schedule_version", "fee_rules", "fee_schedule_metadata")}
            | {"profile_metadata": row.get("metadata", row.get("profile_metadata", {}))},
        ))
    if bind.dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION protect_backtest_account_version() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'account versions cannot be deleted'; END IF;
          IF NEW.profile_id IS DISTINCT FROM OLD.profile_id OR NEW.version IS DISTINCT FROM OLD.version
             OR NEW.snapshot IS DISTINCT FROM OLD.snapshot OR NEW.created_at IS DISTINCT FROM OLD.created_at
          THEN RAISE EXCEPTION 'account version configuration is immutable'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER protect_backtest_account_version BEFORE UPDATE OR DELETE
        ON backtest_account_profile_versions FOR EACH ROW EXECUTE FUNCTION protect_backtest_account_version()""")


def downgrade():
    op.drop_table("backtest_account_profile_versions")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION protect_backtest_account_version()")
