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
from typing import Generic, Mapping, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
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


class BacktestEventItem(_ResultItem):
    event_sequence: int
    step_sequence: int
    phase_sequence: int
    phase_key: str
    event_type: str
    event_time: datetime
    event_version: int = 1
    payload: dict | list | None = None


class BacktestDecisionItem(_ResultItem):
    decision_id: UUID
    step_sequence: int
    decision_time: datetime
    mode: str
    targets: dict | list | None = None
    validation_status: str
    # The existing JSON projection carries both legacy text issues and
    # structured PIT candidate qualification evidence.
    validation_issues: list[str | dict] | None = None
    duration_ms: SerializedDecimal = None
    error: str | None = None


class BacktestOrderItem(_ResultItem):
    order_id: UUID
    intent_id: UUID | None = None
    decision_id: UUID | None = None
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
    """Operator-safe projection of an admission/session preflight row.

    The database table keeps Phase 2a metadata inside the reserved
    ``capabilities.__preflight__`` object for migration compatibility.  This
    validator promotes those fields to stable top-level response fields so a
    UI never needs to understand the storage detail.
    """

    run_kind: str = "backtest_run"
    preflight_profile_key: str = "formal"
    preflight_profile_version: int = 1
    preflight_profile: str = "formal@1"
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
    data_revision_summary: dict | None = None
    admission_report_hash: str | None = None
    session_report_hash: str | None = None
    hash_match: bool | None = None
    report_diff: list[dict] | None = None
    failure_phase: str | None = None
    fixture_sources: dict | None = None
    scope_summary: dict | None = None
    title: str = "正式回测预检"
    message: str = "正式回测预检结果。"

    @model_validator(mode="before")
    @classmethod
    def _promote_preflight_metadata(cls, value):
        """Promote reserved persistence metadata from mappings or ORM rows."""

        if isinstance(value, Mapping):
            payload = dict(value)
        else:
            names = (
                "run_id",
                "phase",
                "status",
                "report_hash",
                "hash_schema_version",
                "capabilities",
                "calendar_summary",
                "session_summary",
                "pit_status",
                "data_cutoff",
                "cutoff_local_date",
                "include_cutoff_day",
                "knowledge_as_of",
                "pit_profile",
                "profile_version",
                "non_strict_pit",
                "non_strict_pit_capabilities",
                "calendar_revision_digest",
                "snapshot_fingerprint",
                "coverage",
                "source_revisions",
                "data_revision_summary",
                "run_kind",
                "preflight_profile_key",
                "preflight_profile_version",
                "preflight_profile",
                "admission_report_hash",
                "session_report_hash",
                "hash_match",
                "report_diff",
                "failure_phase",
                "fixture_sources",
                "scope_summary",
                "title",
                "message",
            )
            payload = {name: getattr(value, name) for name in names if hasattr(value, name)}
        capabilities = payload.get("capabilities")
        metadata = (
            capabilities.get("__preflight__")
            if isinstance(capabilities, Mapping)
            else None
        )
        if isinstance(metadata, Mapping):
            for name in (
                "run_kind",
                "preflight_profile_key",
                "preflight_profile_version",
                "qualification_hash",
                "admission_report_hash",
                "session_report_hash",
                "hash_match",
                "report_diff",
                "failure_phase",
                "fixture_sources",
                "scope_summary",
            ):
                if name in metadata:
                    payload[name] = metadata[name]
            if "qualification_hash" in metadata:
                payload["report_hash"] = metadata["qualification_hash"]
            key = payload.get("preflight_profile_key", "formal")
            version = payload.get("preflight_profile_version", 1)
            payload["preflight_profile"] = f"{key}@{version}"
            internal = payload.get("run_kind") == "internal_link_acceptance"
            payload.setdefault("title", "内部链路验收" if internal else "正式回测预检")
            payload.setdefault(
                "message",
                "内部链路验收预检结果。" if internal else "正式回测预检结果。",
            )
        else:
            # DTOs produced by an internal adapter may already expose the
            # promoted fields directly.  Keep the operator-facing title and
            # Chinese summary correct without requiring the storage wrapper.
            internal = payload.get("run_kind") == "internal_link_acceptance"
            if internal:
                payload.setdefault("preflight_profile", "internal_link_acceptance@1")
                payload.setdefault("title", "内部链路验收")
                payload.setdefault("message", "内部链路验收预检结果。")
        # Calendar timestamps describe calendar knowledge only. Overall PIT
        # support comes from the explicit provider projection, never inference
        # from a calendar's non_strict_pit flag.
        calendar = payload.get("calendar_summary")
        if not isinstance(calendar, Mapping) or not calendar:
            calendar = payload.get("session_summary")
        if isinstance(calendar, Mapping):
            context = calendar.get("pit_context")
            if isinstance(context, Mapping):
                calendar = {**context, **calendar}
            for name in ("data_cutoff", "cutoff_local_date", "include_cutoff_day",
                         "knowledge_as_of", "pit_profile", "profile_version",
                         "calendar_revision_digest", "snapshot_fingerprint"):
                if payload.get(name) is None and name in calendar:
                    payload[name] = calendar[name]
        pit = capabilities.get("__pit__") if isinstance(capabilities, Mapping) else None
        if isinstance(pit, Mapping):
            for name in ("non_strict_pit", "non_strict_pit_capabilities"):
                if name in pit:
                    payload[name] = pit[name]
        elif payload.get("pit_status") in ("strict", "non_strict"):
            payload["non_strict_pit"] = payload["pit_status"] == "non_strict"
        # Promote the bounded source revision summary while retaining the
        # original source_revisions mapping unchanged for audit detail.
        if payload.get("data_revision_summary") is None:
            revisions = payload.get("source_revisions")
            if isinstance(revisions, Mapping):
                summary = revisions.get("__data_revision_summary__")
                if isinstance(summary, Mapping):
                    payload["data_revision_summary"] = dict(summary)
        run_kind = payload.get("run_kind", "backtest_run")
        profile_key = payload.get("preflight_profile_key", "formal")
        profile_version = payload.get("preflight_profile_version", 1)
        if run_kind == "internal_link_acceptance" and (
            profile_key != "internal_link_acceptance" or profile_version != 1
        ):
            raise ValueError(
                "internal_link_acceptance results require profile internal_link_acceptance@1"
            )
        if run_kind == "backtest_run" and (
            profile_key != "formal" or profile_version != 1
        ):
            raise ValueError("formal results require profile formal@1")
        return payload


class BacktestDataChunkItem(_ResultItem):
    phase: str
    chunk_sequence: int
    time_start: datetime
    time_end: datetime
    chunk_strategy_version: str
    validation_status: str
    token_digest: str | None = None
    consistency_mode: str = "chunked_logical_token"
    coverage_summary: dict | list | None = None
    failure_phase: str | None = None
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
    "BacktestEventItem",
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
