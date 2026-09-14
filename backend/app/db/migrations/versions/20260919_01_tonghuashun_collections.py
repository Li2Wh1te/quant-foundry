"""Add source-local Tonghuashun collections without modifying Tushare facts."""

from alembic import op
import sqlalchemy as sa

revision = "20260919_01"
down_revision = "20260918_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("tonghuashun_tickers",
        sa.Column("thscode", sa.String(64), primary_key=True),
        sa.Column("asset_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(256)), sa.Column("exchange", sa.String(16)),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_tonghuashun_tickers_asset_type", "tonghuashun_tickers", ["asset_type"])
    op.create_table("tonghuashun_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("subject", sa.String(64), nullable=False),
        sa.Column("variant", sa.String(80), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("base_observation_id", sa.Uuid(), sa.ForeignKey("tonghuashun_observations.id")),
        sa.Column("chain_depth", sa.Integer(), nullable=False))
    op.create_index("ix_tonghuashun_observations_scope_time", "tonghuashun_observations",
        ["dataset", "subject", "variant", "observed_at"])
    op.create_table("tonghuashun_collection_states",
        sa.Column("dataset", sa.String(80), primary_key=True),
        sa.Column("subject", sa.String(64), primary_key=True),
        sa.Column("variant", sa.String(80), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), sa.ForeignKey("tonghuashun_observations.id")),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True)),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("error_kind", sa.String(40)))
    op.create_table("tonghuashun_request_budget",
        sa.Column("key", sa.String(16), primary_key=True),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("tonghuashun_request_budget")
    op.drop_table("tonghuashun_collection_states")
    op.drop_index("ix_tonghuashun_observations_scope_time", "tonghuashun_observations")
    op.drop_table("tonghuashun_observations")
    op.drop_index("ix_tonghuashun_tickers_asset_type", "tonghuashun_tickers")
    op.drop_table("tonghuashun_tickers")
