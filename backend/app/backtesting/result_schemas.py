"""HTTP response schemas for backtest result queries.

Decimal values are serialized as strings so no precision is lost between
the API boundary and browser or script clients.  Timestamps stay as
timezone-aware datetimes rendered in ISO-8601.  Every list response uses
one uniform cursor-page envelope; clients must treat ``next_cursor`` as
opaque.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Generic, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic import PlainSerializer
from typing_extensions import Annotated


def _decimal_as_string(value: Decimal | None) -> str | None:
    """Render an exact, exponent-free decimal string.

    ``normalize`` drops redundant trailing zeros from fixed-scale database
    numerics; ``format(..., "f")`` keeps integral values from turning into
    scientific notation (``1E+2``).
    """

    if value is None:
        return None
    # Decimal.normalize() obeys the process-global context and can round a
    # NUMERIC(38,18) value before it reaches the wire.  Formatting the exact
    # coefficient and trimming zeros as text preserves every represented digit.
    rendered = format(value, "f")
    if "." not in rendered:
        return rendered
    integer, fraction = rendered.split(".", 1)
    fraction = fraction.rstrip("0")
    return f"{integer}.{fraction}" if fraction else integer


# Carries exact decimals internally, serializes as a string on the wire.
SerializedDecimal = Annotated[
    Optional[Decimal],
    PlainSerializer(_decimal_as_string, return_type=Optional[str]),
]

# Same wire format, but the field itself is mandatory and non-null.
RequiredDecimal = Annotated[
    Decimal,
    PlainSerializer(_decimal_as_string, return_type=str),
]


class _ResultItem(BaseModel):
    """Common read-only shape for every result row projection."""

    model_config = ConfigDict(from_attributes=True)

    run_id: UUID


class BacktestStepItem(_ResultItem):
    step_sequence: int
    time_start: datetime
    time_end: datetime
    data_cutoff_at: datetime
    phase: str
    data_quality: str


class BacktestDecisionItem(_ResultItem):
    decision_id: UUID
    step_sequence: int
    decision_time: datetime
    mode: str
    targets: dict | list | None = None
    validation_status: str
    validation_issues: list | None = None
    duration_ms: SerializedDecimal = None
    error: str | None = None


class BacktestOrderItem(_ResultItem):
    order_id: UUID
    intent_id: UUID | None = None
    instrument_id: UUID
    event_trading_code: str | None = None
    event_name: str | None = None
    event_display_name: str | None = None
    side: str
    order_type: str
    price: SerializedDecimal = None
    quantity: SerializedDecimal = None
    filled_quantity: SerializedDecimal = None
    status: str
    status_reason: str | None = None
    submitted_at: datetime


class BacktestOrderUpdateItem(_ResultItem):
    order_id: UUID
    update_sequence: int
    old_status: str | None = None
    new_status: str
    updated_at: datetime
    reason: str | None = None


class BacktestFillItem(_ResultItem):
    fill_id: UUID
    order_id: UUID
    instrument_id: UUID
    event_trading_code: str | None = None
    event_name: str | None = None
    event_display_name: str | None = None
    side: str
    timestamp: datetime
    reference_price: SerializedDecimal = None
    price: SerializedDecimal = None
    quantity: SerializedDecimal = None
    fees: SerializedDecimal = None
    slippage_bps: SerializedDecimal = None
    slippage_amount: SerializedDecimal = None
    slippage_model_key: str | None = None
    slippage_model_version: int | None = None


class BacktestPositionItem(_ResultItem):
    as_of: datetime
    instrument_id: UUID
    event_trading_code: str | None = None
    event_name: str | None = None
    event_display_name: str | None = None
    side: str
    quantity: SerializedDecimal = None
    available_quantity: SerializedDecimal = None
    average_price: SerializedDecimal = None
    mark_price: SerializedDecimal = None
    realized_pnl: SerializedDecimal = None
    unrealized_pnl: SerializedDecimal = None


class BacktestEquityCurveItem(_ResultItem):
    sequence: int
    as_of: datetime
    valuation_status: str
    valuation_reason: str | None = None
    # The DTO contract requires cash at every valuation point, blocked or not.
    cash: RequiredDecimal
    market_value: SerializedDecimal = None
    equity: SerializedDecimal = None
    period_return: SerializedDecimal = None
    total_pnl: SerializedDecimal = None
    cumulative_return: SerializedDecimal = None
    drawdown: SerializedDecimal = None
    cumulative_fees: SerializedDecimal = None


class BacktestMetricItem(_ResultItem):
    metric_key: str
    formula_version: str
    value: SerializedDecimal = None
    unit: str | None = None
    annualization_factor: SerializedDecimal = None
    risk_free_rate_note: str | None = None
    sample_count: int | None = None
    unavailable_reason: str | None = None
    analyzer_key: str | None = None
    analyzer_version: int | None = None
    analyzer_metadata: dict | list | None = None
    # legacy (no identity) / unknown (identity no longer resolves) /
    # registered (resolvable in the current registry).
    analyzer_state: str


class BacktestAnalysisSummaryItem(_ResultItem):
    """Run-level analysis summary; never recomputed at read time."""

    status: str
    analyzer_snapshot: dict | list | None = None
    formal_timeline: dict | list | None = None
    formula_signature: str
    input_evidence_signature: str
    reporting_currency: str
    initial_equity: SerializedDecimal = None
    valid_day_count: int | None = None
    candidate_return_count: int | None = None
    fill_count: int | None = None
    gross_traded_notional: SerializedDecimal = None
    cumulative_fees: SerializedDecimal = None
    rate_snapshot: dict | list | None = None
    rate_snapshot_hash: str | None = None
    rate_source_versions: dict | list | None = None
    missing_ranges: list | None = None
    last_chunk_sequence: int | None = None
    last_chunk_token: str | None = None
    completed_through_session: datetime | date | None = None
    abort_reason: str | None = None
    failed_step_sequence: int | None = None
    terminal_fingerprint: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finalized_at: datetime | None = None


class AnalysisAdmissionFailureItem(BaseModel):
    """Synchronous response when analysis run creation is blocked.

    This response is not persisted.  Because no admitted run exists, later
    analysis-summary/metric reads for the proposed run id return 404.
    """

    status: str = "blocked"
    run_id: str | None = None
    title: str = "数据预检未通过"
    reason_code: str
    message: str
    details: dict = Field(default_factory=dict)
    persisted: bool = False


class BacktestDataPreflightItem(_ResultItem):
    phase: str
    status: str
    report_hash: str
    hash_schema_version: int = 1
    section: str | None = None
    capabilities: dict | None = None
    calendar_summary: dict | None = None
    session_summary: dict | None = None
    pit_status: str | None = None
    data_cutoff: str | None = None
    cutoff_local_date: date | None = None
    include_cutoff_day: bool | None = None
    knowledge_as_of: str | None = None
    pit_profile: str | None = None
    profile_version: str | None = None
    non_strict_pit: bool | None = None
    non_strict_pit_capabilities: list[str] | None = None
    calendar_revision_digest: str | None = None
    snapshot_fingerprint: str | None = None
    coverage: dict | None = None
    source_revisions: dict | None = None


class BacktestDataChunkItem(_ResultItem):
    phase: str
    chunk_sequence: int
    time_start: datetime
    time_end: datetime
    chunk_strategy_version: str
    token_digest: str
    validation_status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_reason: str | None = None


ItemT = TypeVar("ItemT", bound=_ResultItem)


class ResultCursorPage(BaseModel, Generic[ItemT]):
    """Uniform envelope for every long-range result list."""

    items: list[ItemT]
    next_cursor: str | None = None
    has_more: bool = False
    # ``truncated`` is the task-11 wire spelling; ``has_more`` remains for
    # existing result clients and is always the same boolean.
    truncated: bool = False


__all__ = [
    "BacktestAnalysisSummaryItem",
    "AnalysisAdmissionFailureItem",
    "BacktestDataChunkItem",
    "BacktestDataPreflightItem",
    "BacktestDecisionItem",
    "BacktestEquityCurveItem",
    "BacktestFillItem",
    "BacktestMetricItem",
    "BacktestOrderItem",
    "BacktestOrderUpdateItem",
    "BacktestPositionItem",
    "BacktestStepItem",
    "RequiredDecimal",
    "ResultCursorPage",
    "SerializedDecimal",
]
