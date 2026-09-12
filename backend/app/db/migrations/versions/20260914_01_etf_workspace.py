"""Persist ETF watchlists and provider-supplied daily change facts."""

from alembic import op
import sqlalchemy as sa

revision = "20260914_01"
down_revision = "20260913_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "etf_watchlist_entries",
        sa.Column("owner_scope", sa.String(80), primary_key=True),
        sa.Column("source", sa.String(32), primary_key=True),
        sa.Column("ts_code", sa.String(16), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    # Legacy rows deliberately remain unknown. Migration must not invent a
    # previous close across missing sessions or a corporate-action boundary.
    for name in ("pre_close", "change", "pct_chg"):
        op.add_column("etf_daily_bars", sa.Column(name, sa.Numeric(20, 6), nullable=True))


def downgrade():
    for name in ("pct_chg", "change", "pre_close"):
        op.drop_column("etf_daily_bars", name)
    op.drop_table("etf_watchlist_entries")
