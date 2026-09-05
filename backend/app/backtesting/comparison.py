"""Typed comparison contract and pure evidence/difference projections."""

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class CurvePoint(BaseModel):
    as_of: datetime
    equity: str | None = None
    drawdown: str | None = None
    valuation_status: str | None = None


class CurveSeries(BaseModel):
    run_id: str
    points: list[CurvePoint]


class ComparisonMetric(BaseModel):
    run_id: str
    metric_key: str
    formula_version: str
    value: str | None
    unit: str | None = None
    sample_count: int | None = None
    unavailable_reason: str | None = None
    analyzer_key: str | None = None
    analyzer_version: int | None = None
    analyzer_metadata: dict[str, Any] = Field(default_factory=dict)
    annualization_factor: str | None = None
    risk_free_rate_note: str | None = None


class RunComparisonSummary(BaseModel):
    run_id: str
    status: str
    terminal_status: str | None = None
    config_hash: str
    parameters: dict[str, Any]
    backtest_config: dict[str, Any]
    data_request: dict[str, Any]
    behavior_versions: dict[str, Any]
    component_snapshot: dict[str, Any]
    random_seed: int | None = None
    data_evidence: dict[str, Any]
    account_snapshot: dict[str, Any]
    config_diff: dict[str, Any]


class BacktestComparison(BaseModel):
    run_summaries: list[RunComparisonSummary]
    # Retain the existing alias while clients adopt the canonical field.
    summaries: list[RunComparisonSummary]
    equity_curve_series: list[CurveSeries]
    equity_curves: list[CurveSeries]
    drawdown_curve_series: list[CurveSeries]
    metric_matrix: list[ComparisonMetric]
    metrics: list[dict[str, Any]]
    configuration_diff: list[dict[str, Any]]


def metric_projection(row):
    """Never recompute metrics or infer missing legacy analyzer conventions."""
    return {
        "run_id": str(row.run_id), "metric_key": row.metric_key,
        "formula_version": row.formula_version,
        "value": str(row.value) if row.value is not None else None,
        "unit": row.unit, "sample_count": row.sample_count,
        "unavailable_reason": row.unavailable_reason,
        "analyzer_key": row.analyzer_key, "analyzer_version": row.analyzer_version,
        "analyzer_metadata": row.analyzer_metadata or {},
        "annualization_factor": str(row.annualization_factor) if getattr(row, "annualization_factor", None) is not None else None,
        "risk_free_rate_note": getattr(row, "risk_free_rate_note", None),
    }


def evidence_projection(root, reports):
    """Keep admission and execution evidence separate, including missingness."""
    from app.backtesting.result_schemas import BacktestDataPreflightItem
    evidence = root.data_evidence or {}
    # Expose the frozen adjustment proof as a first-class comparison field;
    # callers must not reconstruct it from opaque request JSON.
    adjustment = evidence.get("adjustment_series") or evidence.get("adjustment_series_policy") or {}
    if not adjustment:
        for report in reports:
            payload = getattr(report, "report", None) or {}
            if isinstance(payload, dict) and payload.get("adjustment_series_policy"):
                adjustment = payload["adjustment_series_policy"]
                break
    return {
        "admission": evidence,
        "admission_hash": root.data_admission_preflight_hash,
        "session_hash": root.data_preflight_hash,
        "session_evidence_available": any(row.phase == "session" for row in reports),
        "adjustment_series": adjustment,
        "reports": [BacktestDataPreflightItem.model_validate(row).model_dump(mode="json") for row in reports],
    }


def configuration_difference(baseline, current):
    """Compare nested leaves and distinguish missing values from explicit null."""
    fields = ("parameters", "backtest_config", "data_request", "behavior_versions", "component_snapshot", "account_snapshot", "data_evidence", "random_seed")
    result = {}

    def visit(left, right, path, left_present=True, right_present=True):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                visit(left.get(key), right.get(key), f"{path}.{key}", key in left, key in right)
        elif left != right or left_present != right_present:
            result[path] = {"baseline": left, "current": right,
                            "baseline_present": left_present, "current_present": right_present}

    for key in fields:
        visit(baseline.get(key), current.get(key), key, key in baseline, key in current)
    return result
