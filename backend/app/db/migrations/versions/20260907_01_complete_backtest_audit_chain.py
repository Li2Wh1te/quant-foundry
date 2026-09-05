"""Complete the persisted backtest event and decision-to-order audit chain."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260907_01"
down_revision: str | None = "20260906_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Version event payloads and link each persisted order to its decision."""

    op.add_column(
        "backtest_events",
        sa.Column(
            "event_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Version of the event payload contract.",
        ),
    )
    # SQLite cannot drop a column default with ALTER TABLE; retaining the
    # migration-only default there is harmless because the ORM always writes
    # the explicit event version.
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("backtest_events", "event_version", server_default=None)
    op.add_column(
        "backtest_orders",
        sa.Column(
            "decision_id",
            sa.Uuid(),
            nullable=True,
            comment="Originating strategy decision, when the pipeline recorded one.",
        ),
    )
    op.create_index(
        "ix_backtest_orders_run_decision",
        "backtest_orders",
        ["run_id", "decision_id"],
    )


def downgrade() -> None:
    """Remove the event version and direct order decision linkage."""

    op.drop_index(
        "ix_backtest_orders_run_decision", table_name="backtest_orders"
    )
    op.drop_column("backtest_orders", "decision_id")
    op.drop_column("backtest_events", "event_version")
