"""Allow raw ETF bars to retain source-invalid numeric values."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_02"
down_revision: str | None = "20260906_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PRICE_CONSTRAINTS = (
    "open_not_negative",
    "high_not_negative",
    "low_not_negative",
    "close_not_negative",
    "high_not_below_low",
)
_NUMERIC_COLUMNS = {
    "open": sa.Numeric(20, 6),
    "high": sa.Numeric(20, 6),
    "low": sa.Numeric(20, 6),
    "close": sa.Numeric(20, 6),
    "vol": sa.Numeric(24, 4),
    "amount": sa.Numeric(24, 4),
}


def upgrade() -> None:
    """Move numeric legality checks from storage to the adapter boundary.

    ``etf_daily_bars`` is the current raw source-fact store.  It must accept
    missing values and numerically representable source anomalies so the ETF
    adapter can mark them invalid without changing or discarding their values.
    """
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("etf_daily_bars", recreate="always") as batch:
            for name in _PRICE_CONSTRAINTS:
                batch.drop_constraint(name, type_="check")
            for name, column_type in _NUMERIC_COLUMNS.items():
                batch.alter_column(
                    name,
                    existing_type=column_type,
                    existing_nullable=False,
                    nullable=True,
                )
        return

    for name in _PRICE_CONSTRAINTS:
        op.drop_constraint(name, "etf_daily_bars", type_="check")
    for name, column_type in _NUMERIC_COLUMNS.items():
        op.alter_column(
            "etf_daily_bars",
            name,
            existing_type=column_type,
            existing_nullable=False,
            nullable=True,
        )


def downgrade() -> None:
    """Restore the original strict storage constraints and nullability."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("etf_daily_bars", recreate="always") as batch:
            for name, column_type in _NUMERIC_COLUMNS.items():
                batch.alter_column(
                    name,
                    existing_type=column_type,
                    existing_nullable=True,
                    nullable=False,
                )
            batch.create_check_constraint("open_not_negative", "open >= 0")
            batch.create_check_constraint("high_not_negative", "high >= 0")
            batch.create_check_constraint("low_not_negative", "low >= 0")
            batch.create_check_constraint("close_not_negative", "close >= 0")
            batch.create_check_constraint("high_not_below_low", "high >= low")
        return

    for name, column_type in _NUMERIC_COLUMNS.items():
        op.alter_column(
            "etf_daily_bars",
            name,
            existing_type=column_type,
            existing_nullable=True,
            nullable=False,
        )
    op.create_check_constraint("open_not_negative", "etf_daily_bars", "open >= 0")
    op.create_check_constraint("high_not_negative", "etf_daily_bars", "high >= 0")
    op.create_check_constraint("low_not_negative", "etf_daily_bars", "low >= 0")
    op.create_check_constraint("close_not_negative", "etf_daily_bars", "close >= 0")
    op.create_check_constraint("high_not_below_low", "etf_daily_bars", "high >= low")
