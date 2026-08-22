"""Add backtest result tables for steps, decisions, orders, fills,
positions, equity curve, metrics, data preflight reports, and data chunks.

Revision ID: 20260822_02
Revises: 20260822_01
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260822_02"
down_revision: str | None = "20260822_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(38, 18)
JSON_TYPE = postgresql.JSONB(astext_type=sa.Text())
RUN_ID_COMMENT = (
    "Owning backtest run; every result row is bound to exactly one run. "
    "A foreign key to backtest_runs is added by the run-creation task."
)


def _id_column() -> sa.Column:
    return sa.Column(
        "id",
        sa.Uuid(),
        nullable=False,
        comment="Surrogate primary key; business identity lives in the unique key.",
    )


def _run_id_column() -> sa.Column:
    return sa.Column("run_id", sa.Uuid(), nullable=False, comment=RUN_ID_COMMENT)


def _created_at_column() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        comment="Row creation timestamp.",
    )


def _timestamp_column(name: str, comment: str, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        comment=comment,
    )


def _amount_column(name: str, comment: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, AMOUNT, nullable=nullable, comment=comment)


def upgrade() -> None:
    """Create the append-only result tables for backtest runs."""
    op.create_table(
        "backtest_steps",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "step_sequence",
            sa.Integer(),
            nullable=False,
            comment="Stable in-run step number used as the pagination sort key.",
        ),
        _timestamp_column("time_start", "Inclusive start of the step interval."),
        _timestamp_column("time_end", "Inclusive end of the step interval."),
        _timestamp_column(
            "data_cutoff_at", "Point-in-time data cutoff observed by this step."
        ),
        sa.Column(
            "phase",
            sa.String(length=32),
            nullable=False,
            comment="Coarse step phase (stable persisted enum value).",
        ),
        sa.Column(
            "data_quality",
            sa.String(length=16),
            nullable=False,
            comment="Input data quality outcome for the step.",
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "time_start <= time_end",
            name="step_time_range_ordered",
        ),
    )
    op.create_index(
        "uq_backtest_steps_run_sequence",
        "backtest_steps",
        ["run_id", "step_sequence"],
        unique=True,
    )

    op.create_table(
        "backtest_decisions",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "decision_id",
            sa.Uuid(),
            nullable=False,
            comment="Business identity of the decision within the run.",
        ),
        sa.Column(
            "step_sequence",
            sa.Integer(),
            nullable=False,
            comment="Step that produced the decision.",
        ),
        _timestamp_column("decision_time", "Timezone-aware decision timestamp."),
        sa.Column(
            "mode",
            sa.String(length=64),
            nullable=False,
            comment="Registered strategy decision mode.",
        ),
        sa.Column(
            "targets",
            JSON_TYPE,
            nullable=False,
            comment="Normalized decision targets (no binary floats).",
        ),
        sa.Column(
            "validation_status",
            sa.String(length=16),
            nullable=False,
            comment="Outcome of decision validation.",
        ),
        sa.Column(
            "validation_issues",
            JSON_TYPE,
            nullable=False,
            comment="Structured validation issues when the decision was rejected.",
        ),
        _amount_column("duration_ms", "Strategy wall-clock duration in milliseconds.", True),
        sa.Column(
            "error",
            sa.String(length=1000),
            nullable=True,
            comment="Error summary when the decision failed.",
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_backtest_decisions_run_decision",
        "backtest_decisions",
        ["run_id", "decision_id"],
        unique=True,
    )
    op.create_index(
        "ix_backtest_decisions_run_sort_key",
        "backtest_decisions",
        ["run_id", "step_sequence", "decision_time", "decision_id"],
    )

    op.create_table(
        "backtest_orders",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "order_id",
            sa.Uuid(),
            nullable=False,
            comment="Business identity of the order within the run.",
        ),
        sa.Column(
            "intent_id",
            sa.Uuid(),
            nullable=True,
            comment="Originating order intent, when the pipeline recorded one.",
        ),
        sa.Column(
            "instrument_id",
            sa.Uuid(),
            nullable=False,
            comment="Stable instrument identity; never a trading code.",
        ),
        sa.Column(
            "event_trading_code",
            sa.String(length=64),
            nullable=True,
            comment="Trading code valid at the event time, frozen on write.",
        ),
        sa.Column(
            "event_name",
            sa.String(length=200),
            nullable=True,
            comment="Instrument name valid at the event time, frozen on write.",
        ),
        sa.Column(
            "event_display_name",
            sa.String(length=200),
            nullable=True,
            comment="Display name valid at the event time, frozen on write.",
        ),
        sa.Column(
            "side",
            sa.String(length=8),
            nullable=False,
            comment="Order direction (buy/sell).",
        ),
        sa.Column(
            "order_type",
            sa.String(length=32),
            nullable=False,
            comment="Standard order type identifier.",
        ),
        _amount_column("price", "Limit price; null for market orders.", True),
        _amount_column("quantity", "Ordered quantity."),
        _amount_column(
            "filled_quantity", "Cumulative filled quantity at the latest update."
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            comment="Latest persisted order status.",
        ),
        sa.Column(
            "status_reason",
            sa.String(length=500),
            nullable=True,
            comment="Human-readable reason attached to the latest status.",
        ),
        _timestamp_column(
            "submitted_at",
            "Order submission time; first half of the pagination sort key.",
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity",
            name="order_filled_within_quantity",
        ),
    )
    op.create_index(
        "uq_backtest_orders_run_order",
        "backtest_orders",
        ["run_id", "order_id"],
        unique=True,
    )
    op.create_index(
        "ix_backtest_orders_run_sort_key",
        "backtest_orders",
        ["run_id", "submitted_at", "order_id"],
    )
    op.create_index(
        "ix_backtest_orders_run_instrument",
        "backtest_orders",
        ["run_id", "instrument_id"],
    )

    op.create_table(
        "backtest_order_updates",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "order_id",
            sa.Uuid(),
            nullable=False,
            comment="Order whose state changed.",
        ),
        sa.Column(
            "update_sequence",
            sa.Integer(),
            nullable=False,
            comment="Monotonic update counter within the order.",
        ),
        sa.Column(
            "old_status",
            sa.String(length=24),
            nullable=True,
            comment="Status before the transition; null for the first update.",
        ),
        sa.Column(
            "new_status",
            sa.String(length=24),
            nullable=False,
            comment="Status after the transition.",
        ),
        _timestamp_column("updated_at", "Transition timestamp."),
        sa.Column(
            "reason",
            sa.String(length=500),
            nullable=True,
            comment="Reason recorded with the transition.",
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_backtest_order_updates_run_order_seq",
        "backtest_order_updates",
        ["run_id", "order_id", "update_sequence"],
        unique=True,
    )
    op.create_index(
        "ix_backtest_order_updates_run_sort_key",
        "backtest_order_updates",
        ["run_id", "updated_at", "order_id", "update_sequence"],
    )

    op.create_table(
        "backtest_fills",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "fill_id",
            sa.Uuid(),
            nullable=False,
            comment="Business identity of the fill within the run.",
        ),
        sa.Column(
            "order_id",
            sa.Uuid(),
            nullable=False,
            comment="Order that produced this fill.",
        ),
        sa.Column(
            "instrument_id",
            sa.Uuid(),
            nullable=False,
            comment="Stable instrument identity; never a trading code.",
        ),
        sa.Column(
            "event_trading_code",
            sa.String(length=64),
            nullable=True,
            comment="Trading code valid at the event time, frozen on write.",
        ),
        sa.Column(
            "event_name",
            sa.String(length=200),
            nullable=True,
            comment="Instrument name valid at the event time, frozen on write.",
        ),
        sa.Column(
            "event_display_name",
            sa.String(length=200),
            nullable=True,
            comment="Display name valid at the event time, frozen on write.",
        ),
        sa.Column(
            "side",
            sa.String(length=8),
            nullable=False,
            comment="Fill direction (buy/sell).",
        ),
        _timestamp_column(
            "timestamp",
            "Execution timestamp; first half of the pagination sort key.",
        ),
        _amount_column(
            "reference_price", "Pre-slippage reference price kept for auditability.", True
        ),
        _amount_column("price", "Final execution price."),
        _amount_column("quantity", "Executed quantity."),
        _amount_column("fees", "Total fees charged with this fill."),
        _amount_column("slippage_bps", "Realized slippage in basis points.", True),
        _amount_column(
            "slippage_amount", "Realized slippage amount in currency units.", True
        ),
        sa.Column(
            "slippage_model_key",
            sa.String(length=100),
            nullable=True,
            comment="Slippage model identifier applied to this fill.",
        ),
        sa.Column(
            "slippage_model_version",
            sa.Integer(),
            nullable=True,
            comment="Version of the slippage model applied to this fill.",
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_backtest_fills_run_fill",
        "backtest_fills",
        ["run_id", "fill_id"],
        unique=True,
    )
    op.create_index(
        "ix_backtest_fills_run_sort_key",
        "backtest_fills",
        ["run_id", "timestamp", "fill_id"],
    )
    op.create_index(
        "ix_backtest_fills_run_instrument",
        "backtest_fills",
        ["run_id", "instrument_id"],
    )

    op.create_table(
        "backtest_positions",
        _id_column(),
        _run_id_column(),
        _timestamp_column(
            "as_of",
            "Valuation timestamp; first part of the pagination sort key.",
        ),
        sa.Column(
            "instrument_id",
            sa.Uuid(),
            nullable=False,
            comment="Stable instrument identity; never a trading code.",
        ),
        sa.Column(
            "event_trading_code",
            sa.String(length=64),
            nullable=True,
            comment="Trading code valid at the valuation point, frozen on write.",
        ),
        sa.Column(
            "event_name",
            sa.String(length=200),
            nullable=True,
            comment="Instrument name valid at the valuation point, frozen on write.",
        ),
        sa.Column(
            "event_display_name",
            sa.String(length=200),
            nullable=True,
            comment="Display name valid at the valuation point, frozen on write.",
        ),
        sa.Column(
            "side",
            sa.String(length=8),
            nullable=False,
            comment="Position side (long/short/net).",
        ),
        _amount_column("quantity", "Non-zero held quantity."),
        _amount_column(
            "available_quantity", "Quantity currently sellable under settlement rules."
        ),
        _amount_column("average_price", "Cost-basis average price."),
        _amount_column(
            "mark_price", "Valuation mark price; null when no valid mark existed.", True
        ),
        _amount_column("realized_pnl", "Realized profit and loss up to this point."),
        _amount_column(
            "unrealized_pnl", "Unrealized profit and loss at this valuation point."
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("quantity > 0", name="position_quantity_non_zero"),
        sa.CheckConstraint(
            "available_quantity >= 0 AND available_quantity <= quantity",
            name="position_available_within_quantity",
        ),
    )
    op.create_index(
        "uq_backtest_positions_run_point_instrument_side",
        "backtest_positions",
        ["run_id", "as_of", "instrument_id", "side"],
        unique=True,
    )

    op.create_table(
        "backtest_equity_curve",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "sequence",
            sa.Integer(),
            nullable=False,
            comment="Stable in-run valuation counter.",
        ),
        _timestamp_column("as_of", "Valuation timestamp."),
        sa.Column(
            "valuation_status",
            sa.String(length=16),
            nullable=False,
            comment="Whether the valuation was complete, degraded, or blocked.",
        ),
        sa.Column(
            "valuation_reason",
            sa.String(length=500),
            nullable=True,
            comment="Reason attached to degraded or blocked valuations.",
        ),
        _amount_column("cash", "Cash balance; null only for blocked points.", True),
        _amount_column(
            "market_value", "Marked market value; null for blocked points.", True
        ),
        _amount_column("equity", "Account equity; null for blocked points.", True),
        _amount_column(
            "period_return",
            "Period return versus the previous valid point; null for blocked points.",
            True,
        ),
        _amount_column(
            "total_pnl",
            "Total profit and loss versus initial equity; null for blocked points.",
            True,
        ),
        _amount_column(
            "cumulative_return",
            "Cumulative return versus initial equity; null for blocked points.",
            True,
        ),
        _amount_column(
            "drawdown", "Drawdown from peak equity; null for blocked points.", True
        ),
        _amount_column(
            "cumulative_fees", "Total fees accumulated through this point."
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_backtest_equity_curve_run_sequence",
        "backtest_equity_curve",
        ["run_id", "sequence"],
        unique=True,
    )
    op.create_index(
        "ix_backtest_equity_curve_run_sort_key",
        "backtest_equity_curve",
        ["run_id", "as_of", "sequence"],
    )

    op.create_table(
        "backtest_metrics",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "metric_key",
            sa.String(length=100),
            nullable=False,
            comment="Stable metric key.",
        ),
        sa.Column(
            "formula_version",
            sa.String(length=32),
            nullable=False,
            comment="Version of the metric formula that produced the value.",
        ),
        _amount_column(
            "value",
            "Metric value; null together with unavailable_reason when missing.",
            True,
        ),
        sa.Column(
            "unit",
            sa.String(length=32),
            nullable=True,
            comment="Unit of the metric value.",
        ),
        _amount_column(
            "annualization_factor", "Annualization factor applied by the formula.", True
        ),
        sa.Column(
            "risk_free_rate_note",
            sa.String(length=200),
            nullable=True,
            comment="Risk-free rate convention used by the formula.",
        ),
        sa.Column(
            "sample_count",
            sa.Integer(),
            nullable=True,
            comment="Number of samples behind the value.",
        ),
        sa.Column(
            "unavailable_reason",
            sa.String(length=500),
            nullable=True,
            comment="Why the value is unavailable; required exactly when value is null.",
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(value IS NULL) = (unavailable_reason IS NOT NULL)",
            name="metric_value_xor_reason",
        ),
    )
    op.create_index(
        "uq_backtest_metrics_run_key_version",
        "backtest_metrics",
        ["run_id", "metric_key", "formula_version"],
        unique=True,
    )

    op.create_table(
        "backtest_data_preflight",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "phase",
            sa.String(length=16),
            nullable=False,
            comment="Preflight phase (admission/session).",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="Overall preflight outcome.",
        ),
        sa.Column(
            "report_hash",
            sa.String(length=128),
            nullable=False,
            comment="Hash over the preflight report content.",
        ),
        sa.Column(
            "capabilities",
            JSON_TYPE,
            nullable=False,
            comment="Capability checklist captured by the report.",
        ),
        sa.Column(
            "calendar_summary",
            JSON_TYPE,
            nullable=False,
            comment="Named calendar sets and their coverage summary.",
        ),
        sa.Column(
            "session_summary",
            JSON_TYPE,
            nullable=False,
            comment="Session compatibility and day-level differences summary.",
        ),
        sa.Column(
            "pit_status",
            sa.String(length=32),
            nullable=True,
            comment="Point-in-time readiness conclusion.",
        ),
        sa.Column(
            "coverage",
            JSON_TYPE,
            nullable=False,
            comment="Coverage statistics of the requested data.",
        ),
        sa.Column(
            "source_revisions",
            JSON_TYPE,
            nullable=False,
            comment="Source-data revisions observed by the report.",
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_backtest_data_preflight_run_phase",
        "backtest_data_preflight",
        ["run_id", "phase"],
        unique=True,
    )

    op.create_table(
        "backtest_data_chunks",
        _id_column(),
        _run_id_column(),
        sa.Column(
            "phase",
            sa.String(length=16),
            nullable=False,
            comment="Data phase owning the chunk (admission/session).",
        ),
        sa.Column(
            "chunk_sequence",
            sa.Integer(),
            nullable=False,
            comment="Chunk counter within the run and phase.",
        ),
        _timestamp_column("time_start", "Inclusive chunk start."),
        _timestamp_column("time_end", "Inclusive chunk end."),
        sa.Column(
            "chunk_strategy_version",
            sa.String(length=32),
            nullable=False,
            comment="Version of the chunking policy that produced boundaries.",
        ),
        sa.Column(
            "token_digest",
            sa.String(length=128),
            nullable=False,
            comment="Non-sensitive digest identifying the data token.",
        ),
        sa.Column(
            "validation_status",
            sa.String(length=16),
            nullable=False,
            comment="Chunk validation outcome.",
        ),
        _timestamp_column("started_at", "When chunk validation started.", True),
        _timestamp_column("finished_at", "When chunk validation finished.", True),
        sa.Column(
            "failure_reason",
            sa.String(length=1000),
            nullable=True,
            comment="Failure reason; required exactly for failed chunks.",
        ),
        _created_at_column(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "time_start <= time_end",
            name="chunk_time_range_ordered",
        ),
    )
    op.create_index(
        "uq_backtest_data_chunks_run_phase_seq",
        "backtest_data_chunks",
        ["run_id", "phase", "chunk_sequence"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the backtest result tables in reverse dependency order."""
    op.drop_table("backtest_data_chunks")
    op.drop_table("backtest_data_preflight")
    op.drop_table("backtest_metrics")
    op.drop_table("backtest_equity_curve")
    op.drop_table("backtest_positions")
    op.drop_table("backtest_fills")
    op.drop_table("backtest_order_updates")
    op.drop_table("backtest_orders")
    op.drop_table("backtest_decisions")
    op.drop_table("backtest_steps")
