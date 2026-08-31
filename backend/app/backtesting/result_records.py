"""ORM records for backtest result tables.

These tables store execution-time facts only; nothing is recomputed at query
time.  Every row is bound to ``run_id`` and carries the run-scoped uniqueness
contract defined by the result specification.  Instrument-bearing rows keep
the stable ``instrument_id`` plus the point-in-time display snapshot frozen
at write time; no ETF-specific column exists anywhere, so any asset class
shares these tables.

JSON columns use a PostgreSQL ``JSONB`` type with a generic ``JSON``
variant so the repository layer can also run against SQLite in tests
without dialect-specific constraints.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base


# Money, prices, quantities, and ratios keep exact decimal semantics.
NUMERIC_PRECISION = 38
NUMERIC_SCALE = 18

JsonType = JSONB().with_variant(JSON(), "sqlite")

# Exact table names owned by this module; used by the SQLite-based tests to
# create only these tables from the shared declarative metadata.
RESULT_TABLE_NAMES = [
    "backtest_steps",
    "backtest_decisions",
    "backtest_orders",
    "backtest_order_updates",
    "backtest_fills",
    "backtest_positions",
    "backtest_equity_curve",
    "backtest_metrics",
    "backtest_data_preflight",
    "backtest_data_chunks",
    "backtest_analysis_summaries",
]


def _amount_column(name: str, comment: str, nullable: bool = False):
    return mapped_column(
        name,
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=nullable,
        comment=comment,
    )


class _RunBoundRecord(Base):
    """Abstract base carrying the shared run binding and timestamps."""

    __abstract__ = True

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
        comment="Surrogate primary key; business identity lives in the unique key.",
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Owning backtest run; every result row is bound to exactly one run. "
        "A foreign key to backtest_runs is added by the run-creation task.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        # Python-side default keeps inserts portable across PostgreSQL and
        # the SQLite test dialect; no database-specific SQL default is used.
        default=lambda: datetime.now(timezone.utc),
        comment="Row creation timestamp.",
    )


class BacktestStepRecord(_RunBoundRecord):
    """One time step of one run."""

    __tablename__ = "backtest_steps"
    __table_args__ = (
        CheckConstraint(
            "time_start <= time_end",
            name="step_time_range_ordered",
        ),
        Index(
            "uq_backtest_steps_run_sequence",
            "run_id",
            "step_sequence",
            unique=True,
        ),
    )

    step_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Stable in-run step number used as the pagination sort key.",
    )
    time_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Inclusive start of the step interval.",
    )
    time_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Inclusive end of the step interval.",
    )
    data_cutoff_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Point-in-time data cutoff observed by this step.",
    )
    phase: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Coarse step phase (stable persisted enum value).",
    )
    data_quality: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Input data quality outcome for the step.",
    )


class BacktestDecisionRecord(_RunBoundRecord):
    """One strategy decision produced during a step."""

    __tablename__ = "backtest_decisions"
    __table_args__ = (
        Index(
            "uq_backtest_decisions_run_decision",
            "run_id",
            "decision_id",
            unique=True,
        ),
        Index(
            "ix_backtest_decisions_run_sort_key",
            "run_id",
            "step_sequence",
            "decision_time",
            "decision_id",
        ),
    )

    decision_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Business identity of the decision within the run.",
    )
    step_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Step that produced the decision.",
    )
    decision_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Timezone-aware decision timestamp.",
    )
    mode: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Registered strategy decision mode.",
    )
    targets: Mapped[dict] = mapped_column(
        JsonType,
        nullable=False,
        comment="Normalized decision targets (no binary floats).",
    )
    validation_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Outcome of decision validation.",
    )
    validation_issues: Mapped[list] = mapped_column(
        JsonType,
        nullable=False,
        comment=(
            "Structured validation issues when the decision was rejected; "
            "candidate PIT qualification evidence uses this existing JSON field."
        ),
    )
    duration_ms: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Strategy wall-clock duration in milliseconds.",
    )
    error: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="Error summary when the decision failed.",
    )


class BacktestOrderResultRecord(_RunBoundRecord):
    """One standard order persisted as a result fact."""

    __tablename__ = "backtest_orders"
    __table_args__ = (
        CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity",
            name="order_filled_within_quantity",
        ),
        Index(
            "uq_backtest_orders_run_order",
            "run_id",
            "order_id",
            unique=True,
        ),
        Index(
            "ix_backtest_orders_run_sort_key",
            "run_id",
            "submitted_at",
            "order_id",
        ),
        Index(
            "ix_backtest_orders_run_instrument",
            "run_id",
            "instrument_id",
        ),
    )

    order_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Business identity of the order within the run.",
    )
    intent_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        comment="Originating order intent, when the pipeline recorded one.",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Stable instrument identity; never a trading code.",
    )
    event_trading_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Trading code valid at the event time, frozen on write.",
    )
    event_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Instrument name valid at the event time, frozen on write.",
    )
    event_display_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Display name valid at the event time, frozen on write.",
    )
    side: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="Order direction (buy/sell).",
    )
    order_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Standard order type identifier.",
    )
    price: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Limit price; null for market orders.",
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Ordered quantity.",
    )
    filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Cumulative filled quantity at the latest update.",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="Latest persisted order status.",
    )
    status_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Human-readable reason attached to the latest status.",
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Order submission time; first half of the pagination sort key.",
    )


class BacktestOrderUpdateRecord(_RunBoundRecord):
    """One order status transition."""

    __tablename__ = "backtest_order_updates"
    __table_args__ = (
        Index(
            "uq_backtest_order_updates_run_order_seq",
            "run_id",
            "order_id",
            "update_sequence",
            unique=True,
        ),
        Index(
            "ix_backtest_order_updates_run_sort_key",
            "run_id",
            "updated_at",
            "order_id",
            "update_sequence",
        ),
    )

    order_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Order whose state changed.",
    )
    update_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Monotonic update counter within the order.",
    )
    old_status: Mapped[str | None] = mapped_column(
        String(24),
        nullable=True,
        comment="Status before the transition; null for the first update.",
    )
    new_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="Status after the transition.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Transition timestamp.",
    )
    reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Reason recorded with the transition.",
    )


class BacktestFillResultRecord(_RunBoundRecord):
    """One simulated fill fact."""

    __tablename__ = "backtest_fills"
    __table_args__ = (
        Index(
            "uq_backtest_fills_run_fill",
            "run_id",
            "fill_id",
            unique=True,
        ),
        Index(
            "ix_backtest_fills_run_sort_key",
            "run_id",
            "timestamp",
            "fill_sequence",
            "fill_id",
        ),
        Index("uq_backtest_fills_run_sequence", "run_id", "fill_sequence", unique=True),
        Index(
            "ix_backtest_fills_run_instrument",
            "run_id",
            "instrument_id",
        ),
    )

    fill_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Business identity of the fill within the run.",
    )
    fill_sequence: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Stable deterministic in-run sequence; never derived from UUID or insertion time.",
    )
    order_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Order that produced this fill.",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Stable instrument identity; never a trading code.",
    )
    event_trading_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Trading code valid at the event time, frozen on write.",
    )
    event_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Instrument name valid at the event time, frozen on write.",
    )
    event_display_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Display name valid at the event time, frozen on write.",
    )
    side: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="Fill direction (buy/sell).",
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Execution timestamp; first half of the pagination sort key.",
    )
    reference_price: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Pre-slippage reference price kept for auditability.",
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Final execution price.",
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Executed quantity.",
    )
    fees: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Total fees charged with this fill.",
    )
    slippage_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Realized slippage in basis points.",
    )
    slippage_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Realized slippage amount in currency units.",
    )
    slippage_model_key: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Slippage model identifier applied to this fill.",
    )
    slippage_model_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Version of the slippage model applied to this fill.",
    )
    currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="CNY",
        server_default="CNY",
        comment="Settlement currency of the fill.",
    )
    contract_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        default=Decimal("1"),
        server_default="1",
        comment="Contract multiplier resolved from the frozen rule snapshot.",
    )
    gross_notional: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="execution_price x quantity x contract_multiplier before fees.",
    )
    fee_breakdown: Mapped[dict | None] = mapped_column(
        JsonType,
        nullable=True,
        comment="Frozen fee components with schedule key/version and rounding contract.",
    )
    settlement_calendar_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Calendar that owns the deferred settlement of a buy fill.",
    )
    settlement_due_session: Mapped[date | None] = mapped_column(
        Date(),
        nullable=True,
        comment="Session whose pre-match boundary releases this buy fill.",
    )
    settlement_boundary_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Boundary identifier that released the due quantity.",
    )


class BacktestPositionResultRecord(_RunBoundRecord):
    """One non-zero position at one valuation point."""

    __tablename__ = "backtest_positions"
    __table_args__ = (
        CheckConstraint(
            "quantity > 0",
            name="position_quantity_non_zero",
        ),
        CheckConstraint(
            "available_quantity >= 0 AND available_quantity <= quantity",
            name="position_available_within_quantity",
        ),
        Index(
            "uq_backtest_positions_run_point_instrument_side",
            "run_id",
            "as_of",
            "instrument_id",
            "side",
            unique=True,
        ),
    )

    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Valuation timestamp; first part of the pagination sort key.",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Stable instrument identity; never a trading code.",
    )
    event_trading_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Trading code valid at the valuation point, frozen on write.",
    )
    event_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Instrument name valid at the valuation point, frozen on write.",
    )
    event_display_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Display name valid at the valuation point, frozen on write.",
    )
    side: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="Position side (long/short/net).",
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Non-zero held quantity.",
    )
    available_quantity: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Quantity currently sellable under settlement rules.",
    )
    average_price: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Cost-basis average price.",
    )
    mark_price: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Valuation mark price; null when no valid mark existed.",
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Realized profit and loss up to this point.",
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Unrealized profit and loss at this valuation point.",
    )


class BacktestEquityCurveRecord(_RunBoundRecord):
    """One account valuation point of the equity curve."""

    __tablename__ = "backtest_equity_curve"
    __table_args__ = (
        Index(
            "uq_backtest_equity_curve_run_sequence",
            "run_id",
            "sequence",
            unique=True,
        ),
        Index(
            "ix_backtest_equity_curve_run_sort_key",
            "run_id",
            "as_of",
            "sequence",
        ),
    )

    sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Stable in-run valuation counter.",
    )
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Valuation timestamp.",
    )
    valuation_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Whether the valuation was complete, degraded, or blocked.",
    )
    valuation_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Reason attached to degraded or blocked valuations.",
    )
    cash: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Cash balance; carried even by blocked valuation points.",
    )
    market_value: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Marked market value; null for blocked points.",
    )
    equity: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Account equity; null for blocked points.",
    )
    period_return: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Period return versus the previous valid point; null for blocked points.",
    )
    total_pnl: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Total profit and loss versus initial equity; null for blocked points.",
    )
    cumulative_return: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Cumulative return versus initial equity; null for blocked points.",
    )
    drawdown: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Drawdown from peak equity; null for blocked points.",
    )
    cumulative_fees: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=False,
        comment="Total fees accumulated through this point.",
    )


class BacktestMetricRecord(_RunBoundRecord):
    """One metric value or an explicit unavailability record."""

    __tablename__ = "backtest_metrics"
    __table_args__ = (
        CheckConstraint(
            "(value IS NULL) = (unavailable_reason IS NOT NULL)",
            name="metric_value_xor_reason",
        ),
        CheckConstraint(
            "(analyzer_key IS NULL) = (analyzer_version IS NULL)",
            name="analyzer_identity_pair",
        ),
        CheckConstraint(
            "length(metric_key) BETWEEN 1 AND 100",
            name="metric_key_length",
        ),
        CheckConstraint(
            "length(formula_version) BETWEEN 1 AND 64",
            name="formula_version_length",
        ),
        CheckConstraint(
            "unit IS NULL OR length(unit) BETWEEN 1 AND 32",
            name="metric_unit_length",
        ),
        CheckConstraint(
            "analyzer_key IS NULL OR length(analyzer_key) BETWEEN 1 AND 100",
            name="analyzer_key_length",
        ),
        Index(
            "uq_backtest_metrics_run_key_version",
            "run_id",
            "metric_key",
            "formula_version",
            unique=True,
        ),
    )

    metric_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Stable metric key.",
    )
    formula_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Version of the metric formula that produced the value.",
    )
    value: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Metric value; null together with unavailable_reason when missing.",
    )
    unit: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Unit of the metric value.",
    )
    annualization_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Annualization factor applied by the formula.",
    )
    risk_free_rate_note: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Risk-free rate convention used by the formula.",
    )
    sample_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of samples behind the value.",
    )
    unavailable_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Why the value is unavailable; required exactly when value is null.",
    )
    # Analyzer identity (task package 06).  Both columns are null together
    # for legacy rows written before analyzers existed; new rows must pair
    # them, enforced by the analyzer_identity_pair check constraint.
    analyzer_key: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Stable key of the analyzer that produced this metric; "
        "null only for legacy rows.",
    )
    analyzer_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Registered version of the producing analyzer; paired with analyzer_key.",
    )
    analyzer_metadata: Mapped[dict | None] = mapped_column(
        JsonType,
        nullable=True,
        comment="Analyzer context such as reason_code, rate convention, and "
        "formula signature references.",
    )

    @property
    def analyzer_state(self) -> str:
        """Registry-relative state exposed verbatim by the result API."""

        from app.backtesting.result_models import resolve_analyzer_state

        return resolve_analyzer_state(
            self.analyzer_key,
            self.analyzer_version,
            self.metric_key,
            self.formula_version,
        ).value


class BacktestDataPreflightResultRecord(_RunBoundRecord):
    """One run-level data preflight report."""

    __tablename__ = "backtest_data_preflight"
    __table_args__ = (
        Index(
            "uq_backtest_data_preflight_run_phase",
            "run_id",
            "phase",
            unique=True,
        ),
    )

    phase: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Preflight phase (admission/session).",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Overall preflight outcome.",
    )
    report_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Hash over the preflight report content.",
    )
    hash_schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Canonical preflight hash payload version (1 legacy, 2 calendar evidence).",
    )
    capabilities: Mapped[dict] = mapped_column(
        JsonType,
        nullable=False,
        comment="Capability checklist captured by the report.",
    )
    calendar_summary: Mapped[dict] = mapped_column(
        JsonType,
        nullable=False,
        comment="Named calendar sets and their coverage summary.",
    )
    session_summary: Mapped[dict] = mapped_column(
        JsonType,
        nullable=False,
        comment="Session compatibility and day-level differences summary.",
    )
    pit_status: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Point-in-time readiness conclusion.",
    )
    coverage: Mapped[dict] = mapped_column(
        JsonType,
        nullable=False,
        comment="Coverage statistics of the requested data.",
    )
    source_revisions: Mapped[dict] = mapped_column(
        JsonType,
        nullable=False,
        comment="Source-data revisions observed by the report.",
    )


class BacktestDataChunkRecord(_RunBoundRecord):
    """One bounded data chunk consistency record."""

    __tablename__ = "backtest_data_chunks"
    __table_args__ = (
        CheckConstraint(
            "time_start <= time_end",
            name="chunk_time_range_ordered",
        ),
        Index(
            "uq_backtest_data_chunks_run_phase_seq",
            "run_id",
            "phase",
            "chunk_sequence",
            unique=True,
        ),
    )

    phase: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Data phase owning the chunk (admission/session).",
    )
    chunk_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Chunk counter within the run and phase.",
    )
    time_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Inclusive chunk start.",
    )
    time_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Inclusive chunk end.",
    )
    chunk_strategy_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Version of the chunking policy that produced boundaries.",
    )
    token_digest: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Non-sensitive digest identifying the data token.",
    )
    consistency_mode: Mapped[str] = mapped_column(String(40), nullable=False, comment="Consistency mode.")
    coverage_summary: Mapped[dict] = mapped_column(JsonType, nullable=False, comment="Bounded consistency coverage evidence.")
    failure_phase: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="Failure phase when validation failed.")
    validation_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Chunk validation outcome.",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When chunk validation started.",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When chunk validation finished.",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="Failure reason; required exactly for failed chunks.",
    )


class BacktestAnalysisSummaryRecord(_RunBoundRecord):
    """Run-level analyzer lifecycle status and frozen analysis evidence.

    This table is the formal landing point of run-level analysis state
    (task package 06); ``status`` is never stored in a free-form JSON
    field.  ``run_id`` stays unique without a ``backtest_runs`` foreign key
    because the unified run table does not exist yet.
    """

    __tablename__ = "backtest_analysis_summaries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('partial', 'final', 'aborted')",
            name="analysis_summary_status_allowed",
        ),
        CheckConstraint(
            "(status = 'aborted') = (abort_reason IS NOT NULL)",
            name="analysis_summary_abort_reason_pair",
        ),
        Index(
            "uq_backtest_analysis_summaries_run",
            "run_id",
            unique=True,
        ),
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Analysis lifecycle status: partial, final, or aborted.",
    )
    analyzer_snapshot: Mapped[dict] = mapped_column(
        JsonType,
        nullable=False,
        comment="Frozen AnalyzerSpec identities, parameters, and converter identity.",
    )
    formal_timeline: Mapped[dict | None] = mapped_column(
        JsonType,
        nullable=True,
        comment="Immutable ordered formal sessions and their timeline hash.",
    )
    formula_signature: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Hash over the logical formula configuration (spec identities, "
        "parameters, decimal policy).",
    )
    input_evidence_signature: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Hash over the actual input evidence (E0, observations, fills, "
        "rate snapshot hash).",
    )
    initial_equity: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Frozen E0 from the initial equity snapshot.",
    )
    valid_day_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Formal days with a valid positive end-of-day equity.",
    )
    candidate_return_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Contiguous return candidates available from E0 without splicing invalid days.",
    )
    fill_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Accounting-applied fill count at the latest update.",
    )
    gross_traded_notional: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Sum of applied gross traded notionals at the latest update.",
    )
    cumulative_fees: Mapped[Decimal | None] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE),
        nullable=True,
        comment="Sum of applied fees at the latest update.",
    )
    rate_snapshot: Mapped[dict | None] = mapped_column(
        JsonType,
        nullable=True,
        comment="Complete frozen PIT daily risk-free rate series (Sharpe B "
        "only); empty for A/C runs.",
    )
    rate_snapshot_hash: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Deterministic hash of the frozen rate snapshot content.",
    )
    rate_source_versions: Mapped[dict | None] = mapped_column(
        JsonType,
        nullable=True,
        comment="Rate source key/version and query parameters of the snapshot.",
    )
    missing_ranges: Mapped[list[dict[str, str]] | None] = mapped_column(
        JsonType,
        nullable=True,
        comment="Deterministic contiguous missing-session ranges of the rate series.",
    )
    reporting_currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="Single reporting currency; equals the accounting policy currency.",
    )
    last_chunk_sequence: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment=(
            "Zero-based sequence of the latest successfully persisted chunk; "
            "aborted runs never record the failed chunk."
        ),
    )
    last_chunk_token: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Opaque digest binding the latest accepted partial chunk.",
    )
    completed_through_session: Mapped[date | None] = mapped_column(
        Date(),
        nullable=True,
        comment="Last session whose end-of-day observation was committed.",
    )
    abort_reason: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="Structured failure reason; required exactly when aborted.",
    )
    failed_step_sequence: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Step whose failure aborted the run.",
    )
    terminal_fingerprint: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Frozen business-content fingerprint for terminal retries.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="Latest write timestamp of this summary.",
    )
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Terminal finalization time; set only for final/aborted rows.",
    )


__all__ = [
    "BacktestAnalysisSummaryRecord",
    "BacktestDataChunkRecord",
    "BacktestDataPreflightResultRecord",
    "BacktestDecisionRecord",
    "BacktestEquityCurveRecord",
    "BacktestFillResultRecord",
    "BacktestMetricRecord",
    "BacktestOrderResultRecord",
    "BacktestOrderUpdateRecord",
    "BacktestPositionResultRecord",
    "BacktestStepRecord",
]
