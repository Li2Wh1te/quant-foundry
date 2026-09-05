"""Create a shared immutable fee catalog without rewriting legacy snapshots."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = "20260910_01"
down_revision = "20260909_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("backtest_fee_schedule_versions",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("name_zh", sa.String(200), nullable=False),
        sa.Column("name_en", sa.String(200), nullable=False),
        sa.Column("snapshot", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 0", name="fee_catalog_version_positive"))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION protect_fee_catalog_version() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'fee catalog versions are immutable'; END;
        $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER protect_fee_catalog_version BEFORE UPDATE OR DELETE
        ON backtest_fee_schedule_versions FOR EACH ROW EXECUTE FUNCTION protect_fee_catalog_version()""")


def downgrade():
    op.drop_table("backtest_fee_schedule_versions")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION protect_fee_catalog_version()")
