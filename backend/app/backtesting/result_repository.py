"""Persistence and cursor-paginated queries for backtest results.

The repository owns three responsibilities:

1. writing validated result DTOs into the append-only result tables,
   rejecting duplicate business keys inside a run;
2. building stable keyset-paginated reads whose sort keys match the
   result specification exactly (time ties are broken by entity id or
   in-run sequence, never by mutable display fields);
3. issuing opaque cursors bound to a canonicalized query digest and a
   snapshot upper bound so appended rows never leak into an existing page
   walk.

The repository depends on validated DTOs and SQLAlchemy records only; it
never resolves instruments itself, so no market-data client can leak in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.backtesting.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorPage,
    CursorQueryMismatchError,
    build_cursor,
    compute_query_digest,
    encode_sort_element,
    hmac_compare,
    normalize_limit,
    parse_cursor,
)
from app.backtesting.result_models import (
    BacktestAnalysisSummaryRecord as BacktestAnalysisSummaryDto,
    BacktestDataChunkRecord as BacktestDataChunkDto,
    BacktestDataPreflightRecord as BacktestDataPreflightDto,
    BacktestDecisionRecord as BacktestDecisionDto,
    BacktestEquityCurveRecord as BacktestEquityCurveDto,
    BacktestFillRecord as BacktestFillDto,
    BacktestMetricRecord as BacktestMetricDto,
    BacktestOrderRecord as BacktestOrderDto,
    BacktestOrderUpdateRecord as BacktestOrderUpdateDto,
    BacktestPositionRecord as BacktestPositionDto,
    BacktestStepRecord as BacktestStepDto,
)
from app.backtesting.result_records import (
    BacktestAnalysisSummaryRecord,
    BacktestDataChunkRecord,
    BacktestDataPreflightResultRecord,
    BacktestDecisionRecord,
    BacktestEquityCurveRecord,
    BacktestFillResultRecord,
    BacktestMetricRecord,
    BacktestOrderResultRecord,
    BacktestOrderUpdateRecord,
    BacktestPositionResultRecord,
    BacktestStepRecord,
)


class ResultRepositoryError(ValueError):
    """Base class for repository usage errors."""


class UnknownResultKindError(ResultRepositoryError):
    """The requested result kind is not registered."""


class ResultFilterError(ResultRepositoryError):
    """A filter is not supported by the requested result kind."""


class ResultRecordConflictError(Exception):
    """The row violates the run-scoped uniqueness contract."""


def _validate_metric_producer_contract(dto: BacktestMetricDto) -> None:
    """Validate an analyzer-bearing metric against the frozen Registry."""

    if dto.analyzer_key is None or dto.analyzer_version is None:
        raise ResultRepositoryError(
            f"metric ({dto.metric_key!r}, {dto.formula_version!r}) lacks "
            "analyzer identity; producer identity is required for this path"
        )
    from app.backtesting.analyzers import frozen_output_contract_for

    try:
        contract = frozen_output_contract_for(dto.analyzer_key, dto.analyzer_version)
    except Exception as exc:
        raise ResultRecordConflictError(
            f"analyzer identity ({dto.analyzer_key!r}, "
            f"{dto.analyzer_version!r}) is not a registered v1 producer"
        ) from exc
    descriptor = next(
        (
            item
            for item in contract
            if (item.metric_key, item.formula_version)
            == (dto.metric_key, dto.formula_version)
        ),
        None,
    )
    if descriptor is None:
        raise ResultRecordConflictError(
            f"analyzer ({dto.analyzer_key!r}, {dto.analyzer_version!r}) "
            f"cannot produce metric ({dto.metric_key!r}, "
            f"{dto.formula_version!r}); the mapping is fixed by the frozen "
            "registry contract"
        )
    if dto.unit != descriptor.unit:
        raise ResultRecordConflictError(
            f"metric {dto.metric_key!r} unit {dto.unit!r} does not match "
            f"the frozen contract unit {descriptor.unit!r}"
        )
    metadata = dto.analyzer_metadata
    if not isinstance(metadata, Mapping):
        raise ResultRecordConflictError(
            "formal analyzer metrics require complete analyzer_metadata"
        )
    required_metadata = {
        "formula_signature",
        "input_evidence_signature",
        "contract_unit",
        "sample_count_semantics",
    }
    missing = sorted(required_metadata.difference(metadata))
    if missing:
        raise ResultRecordConflictError(
            f"formal analyzer metric metadata is missing {missing}"
        )
    for signature_name in ("formula_signature", "input_evidence_signature"):
        signature = metadata.get(signature_name)
        if (
            not isinstance(signature, str)
            or len(signature) != 71
            or not signature.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in signature[7:])
        ):
            raise ResultRecordConflictError(
                f"{signature_name} must be sha256:<64 lowercase hex digits>"
            )
    if metadata.get("contract_unit") != descriptor.unit:
        raise ResultRecordConflictError(
            "metric contract_unit metadata differs from the frozen descriptor"
        )
    if metadata.get("sample_count_semantics") != descriptor.sample_count_semantics:
        raise ResultRecordConflictError(
            "metric sample_count_semantics differs from the frozen descriptor"
        )
    if dto.sample_count is None:
        raise ResultRecordConflictError(
            "formal analyzer metrics require the sample_count defined by the contract"
        )
    identity_required_metadata = {
        "sharpe_simple": {"valid_equity_day_count", "candidate_return_count"},
        "sharpe_pit_rf": {
            "valid_equity_day_count",
            "candidate_return_count",
            "rate_unit",
            "rate_convention",
            "rate_effective_at",
            "rate_session_mapping",
            "rate_cutoff_boundary",
            "rate_data_cutoff_semantics",
            "rate_source_key",
            "rate_source_version",
            "rate_snapshot_hash",
            "missing_ranges",
        },
        "sharpe_config_rf": {
            "valid_equity_day_count",
            "candidate_return_count",
            "rf_annual",
            "rf_daily",
            "annual_rate_converter",
            "risk_free_rate_note",
        },
        "turnover": {"gross_traded_notional", "fill_count"},
        "fee_summary": {"gross_traded_notional", "cumulative_fees"},
    }
    analyzer_missing = sorted(
        identity_required_metadata.get(dto.analyzer_key, set()).difference(metadata)
    )
    if analyzer_missing:
        raise ResultRecordConflictError(
            f"formal {dto.analyzer_key} metric metadata is missing "
            f"{analyzer_missing}"
        )
    reason_code = metadata.get("reason_code")
    if dto.value is None:
        if reason_code not in descriptor.unavailable_reason_codes:
            raise ResultRecordConflictError(
                "unavailable metric reason_code is absent or not declared by "
                "the frozen descriptor"
            )
    elif reason_code is not None:
        raise ResultRecordConflictError(
            "available metrics cannot carry unavailable reason_code metadata"
        )
    if dto.analyzer_key != "sharpe_config_rf" and dto.risk_free_rate_note is not None:
        raise ResultRecordConflictError(
            "risk_free_rate_note is reserved for configured-rate Sharpe metrics"
        )
    if not dto.analyzer_key.startswith("sharpe_") and dto.annualization_factor is not None:
        raise ResultRecordConflictError(
            "annualization_factor is reserved for Sharpe metrics"
        )
    if reason_code == "ZERO_GROSS_TRADED_NOTIONAL" and (
        _metadata_decimal(metadata, "gross_traded_notional") != 0
    ):
        raise ResultRecordConflictError(
            "ZERO_GROSS_TRADED_NOTIONAL requires zero gross traded notional"
        )
    if reason_code == "NO_VALID_END_OF_DAY_EQUITY" and dto.sample_count != 0:
        raise ResultRecordConflictError(
            "NO_VALID_END_OF_DAY_EQUITY requires zero valid observations"
        )
    if reason_code == "INSUFFICIENT_RETURNS" and dto.sample_count >= 2:
        raise ResultRecordConflictError(
            "INSUFFICIENT_RETURNS requires fewer than two return candidates"
        )
    if reason_code == "ZERO_RETURN_STDDEV" and dto.sample_count < 2:
        raise ResultRecordConflictError(
            "ZERO_RETURN_STDDEV requires at least two return candidates"
        )
    if reason_code == "MISSING_PIT_RF" and not (
        metadata.get("missing_ranges") or metadata.get("missing_rate_session_dates")
    ):
        raise ResultRecordConflictError(
            "MISSING_PIT_RF requires non-empty missing rate evidence"
        )
    if reason_code == "INVALID_EQUITY" and not metadata.get(
        "invalid_session_dates"
    ):
        raise ResultRecordConflictError(
            "INVALID_EQUITY requires non-empty failed-session evidence"
        )
    if dto.analyzer_key.startswith("sharpe_"):
        for count_name in ("valid_equity_day_count", "candidate_return_count"):
            count = metadata.get(count_name)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ResultRecordConflictError(
                    f"Sharpe metadata {count_name} must be a non-negative integer"
                )
        if metadata.get("annualization_factor") != "252" or metadata.get("std_ddof") != 1:
            raise ResultRecordConflictError(
                "Sharpe metadata must freeze annualization_factor=252 and std_ddof=1"
            )
        if dto.annualization_factor != Decimal("252"):
            raise ResultRecordConflictError(
                "Sharpe annualization_factor column must equal 252"
            )
        if dto.sample_count != metadata.get("candidate_return_count"):
            raise ResultRecordConflictError(
                "Sharpe sample_count must equal candidate_return_count"
            )
    if dto.analyzer_key == "sharpe_pit_rf":
        fixed_rate_contract = {
            "rate_unit": "decimal_fraction",
            "rate_convention": "simple_daily_rate",
            "rate_effective_at": "session_date",
            "rate_session_mapping": "exact_formal_session_date",
            "rate_cutoff_boundary": "data_cutoff_at_not_after_session_open",
            "rate_data_cutoff_semantics": (
                "data_cutoff_at_not_after_session_open"
            ),
        }
        for name, expected in fixed_rate_contract.items():
            if metadata.get(name) != expected:
                raise ResultRecordConflictError(
                    f"PIT rate metadata {name} must equal {expected!r}"
                )
        rate_hash = metadata.get("rate_snapshot_hash")
        if (
            not isinstance(rate_hash, str)
            or len(rate_hash) != 71
            or not rate_hash.startswith("sha256:")
            or not isinstance(metadata.get("rate_source_key"), str)
            or not metadata.get("rate_source_key", "").strip()
            or isinstance(metadata.get("rate_source_version"), bool)
            or not isinstance(metadata.get("rate_source_version"), int)
            or metadata.get("rate_source_version") <= 0
            or any(character not in "0123456789abcdef" for character in rate_hash[7:])
            or not isinstance(metadata.get("missing_ranges"), (list, tuple))
        ):
            raise ResultRecordConflictError(
                "PIT rate source, hash, and missing_ranges metadata are invalid"
            )
        for missing_range in metadata["missing_ranges"]:
            if not isinstance(missing_range, Mapping) or set(missing_range) != {
                "start_session",
                "end_session",
            }:
                raise ResultRecordConflictError(
                    "PIT missing_ranges entries must contain start/end sessions"
                )
            try:
                start = date.fromisoformat(missing_range["start_session"])
                end = date.fromisoformat(missing_range["end_session"])
            except (TypeError, ValueError) as exc:
                raise ResultRecordConflictError(
                    "PIT missing_ranges sessions must be ISO calendar dates"
                ) from exc
            if end < start:
                raise ResultRecordConflictError(
                    "PIT missing_ranges end_session must not precede start_session"
                )
    if dto.analyzer_key == "sharpe_config_rf":
        if metadata.get("annual_rate_converter") != "annual_rate_div_252@1":
            raise ResultRecordConflictError(
                "configured-rate Sharpe must use annual_rate_div_252@1"
            )
        note = metadata.get("risk_free_rate_note")
        if not isinstance(note, str) or not note.strip() or dto.risk_free_rate_note != note.strip():
            raise ResultRecordConflictError(
                "configured-rate Sharpe requires the frozen risk-free source note"
            )
        try:
            annual = Decimal(str(metadata.get("rf_annual")))
            daily = Decimal(str(metadata.get("rf_daily")))
        except (InvalidOperation, ValueError) as exc:
            raise ResultRecordConflictError(
                "configured-rate Sharpe rates must be decimal strings"
            ) from exc
        if not annual.is_finite() or annual <= -1 or not daily.is_finite():
            raise ResultRecordConflictError(
                "configured-rate Sharpe rf_annual must be finite and greater than -1"
            )
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            if daily != annual / Decimal("252"):
                raise ResultRecordConflictError(
                    "configured-rate Sharpe rf_daily must equal rf_annual / 252"
                )


def _metadata_decimal(metadata: Mapping[str, Any], name: str) -> Decimal:
    try:
        value = Decimal(str(metadata.get(name)))
    except (InvalidOperation, ValueError) as exc:
        raise ResultRecordConflictError(f"metric metadata {name} must be Decimal") from exc
    if not value.is_finite():
        raise ResultRecordConflictError(f"metric metadata {name} must be finite")
    return value


def _validate_metric_against_summary(
    dto: BacktestMetricDto,
    summary: BacktestAnalysisSummaryRecord,
) -> None:
    """Bind every formal metric to its run's immutable terminal summary."""

    if summary.status not in ("final", "aborted"):
        raise ResultRecordConflictError(
            "formal metrics may only be written under a terminal analysis summary"
        )
    snapshot = summary.analyzer_snapshot
    specs = snapshot.get("specs") if isinstance(snapshot, Mapping) else None
    if not isinstance(specs, (list, tuple)):
        raise ResultRecordConflictError(
            "formal metrics require a structured analyzer_snapshot.specs contract"
        )
    matching_specs = [
        spec
        for spec in specs
        if isinstance(spec, Mapping)
        and spec.get("analyzer_key") == dto.analyzer_key
        and spec.get("analyzer_version") == dto.analyzer_version
    ]
    if len(matching_specs) != 1:
        raise ResultRecordConflictError(
            "metric producer is absent from or duplicated in the terminal analyzer snapshot"
        )
    outputs = matching_specs[0].get("output_contract")
    if not isinstance(outputs, (list, tuple)):
        raise ResultRecordConflictError(
            "metric producer snapshot lacks a structured output contract"
        )
    matching_outputs = [
        output
        for output in outputs
        if isinstance(output, Mapping)
        and output.get("metric_key") == dto.metric_key
        and output.get("formula_version") == dto.formula_version
        and output.get("unit") == dto.unit
    ]
    if len(matching_outputs) != 1:
        raise ResultRecordConflictError(
            "metric output is absent from or duplicated in the terminal analyzer snapshot"
        )
    metadata = dto.analyzer_metadata or {}
    if (
        metadata.get("formula_signature") != summary.formula_signature
        or metadata.get("input_evidence_signature")
        != summary.input_evidence_signature
    ):
        raise ResultRecordConflictError(
            "metric signatures differ from the terminal analysis summary"
        )
    if dto.analyzer_key.startswith("sharpe_"):
        if (
            metadata.get("valid_equity_day_count") != summary.valid_day_count
            or metadata.get("candidate_return_count")
            != summary.candidate_return_count
        ):
            raise ResultRecordConflictError(
                "Sharpe counts differ from the terminal analysis summary"
            )
    if dto.analyzer_key == "sharpe_pit_rf":
        source_versions = summary.rate_source_versions or {}
        rate_snapshot = summary.rate_snapshot or {}
        if (
            metadata.get("rate_source_key") != source_versions.get("source_key")
            or metadata.get("rate_source_version")
            != source_versions.get("source_version")
            or metadata.get("rate_snapshot_hash") != summary.rate_snapshot_hash
            or _thaw_json_value(metadata.get("missing_ranges"))
            != _thaw_json_value(summary.missing_ranges or ())
            or rate_snapshot.get("rate_unit") != metadata.get("rate_unit")
            or rate_snapshot.get("rate_convention")
            != metadata.get("rate_convention")
            or rate_snapshot.get("effective_at")
            != metadata.get("rate_effective_at")
            or rate_snapshot.get("session_mapping")
            != metadata.get("rate_session_mapping")
            or rate_snapshot.get("cutoff_boundary")
            != metadata.get("rate_cutoff_boundary")
            or rate_snapshot.get("data_cutoff_semantics")
            != metadata.get("rate_data_cutoff_semantics")
        ):
            raise ResultRecordConflictError(
                "PIT rate evidence differs from the terminal analysis summary"
            )
    if dto.analyzer_key == "turnover":
        from app.backtesting.analyzers import quantize_for_persistence

        if (
            dto.sample_count != summary.valid_day_count
            or metadata.get("fill_count") != summary.fill_count
            or quantize_for_persistence(
                _metadata_decimal(metadata, "gross_traded_notional")
            )
            != summary.gross_traded_notional
        ):
            raise ResultRecordConflictError(
                "turnover counts or gross notional differ from the summary"
            )
        if dto.value is not None:
            average = _metadata_decimal(metadata, "average_end_of_day_equity")
            gross = _metadata_decimal(metadata, "gross_traded_notional")
            if average <= 0:
                raise ResultRecordConflictError(
                    "turnover average equity must be strictly positive"
                )
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                expected_turnover = quantize_for_persistence(gross / average)
            if dto.value != expected_turnover:
                raise ResultRecordConflictError(
                    "turnover value does not equal gross notional / average equity"
                )
    if dto.analyzer_key == "fee_summary":
        from app.backtesting.analyzers import quantize_for_persistence

        if (
            dto.sample_count != summary.fill_count
            or quantize_for_persistence(
                _metadata_decimal(metadata, "gross_traded_notional")
            )
            != summary.gross_traded_notional
            or quantize_for_persistence(
                _metadata_decimal(metadata, "cumulative_fees")
            )
            != summary.cumulative_fees
        ):
            raise ResultRecordConflictError(
                "fee counts or amounts differ from the terminal summary"
            )
        cumulative = _metadata_decimal(metadata, "cumulative_fees")
        gross = _metadata_decimal(metadata, "gross_traded_notional")
        if (
            dto.metric_key == "cumulative_fees"
            and dto.value != quantize_for_persistence(cumulative)
        ):
            raise ResultRecordConflictError(
                "cumulative_fees metric value differs from accounting metadata"
            )
        if (
            dto.metric_key == "fee_to_gross_traded_notional"
            and dto.value is not None
        ):
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                expected_ratio = (
                    None
                    if gross == 0
                    else quantize_for_persistence(cumulative / gross)
                )
            if expected_ratio is None or dto.value != expected_ratio:
                raise ResultRecordConflictError(
                    "fee ratio metric does not equal cumulative_fees / gross notional"
                )


# ---------------------------------------------------------------------------
# DTO -> record conversion helpers
# ---------------------------------------------------------------------------


def _thaw_json(value: Any) -> Any:
    """Convert frozen containers back into JSON-serializable structures."""

    if isinstance(value, MappingProxyType) or isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _aware(value: datetime) -> datetime:
    """Reattach UTC to naive datetimes returned by non-tz databases.

    PostgreSQL ``timestamptz`` always returns aware values; the UTC fallback
    keeps SQLite-based tests deterministic without weakening production
    semantics.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _normalize_sort_value(kind: str, value: Any) -> Any:
    """Coerce a raw column value into its typed, comparable sort-key form."""

    if kind == "ts":
        return _aware(value)
    if kind == "uuid":
        return value if isinstance(value, UUID) else UUID(str(value))
    if kind == "dec":
        return value if isinstance(value, Decimal) else Decimal(str(value))
    return value


def _step_record(dto: BacktestStepDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "step_sequence": dto.step_sequence,
        "time_start": dto.time_start,
        "time_end": dto.time_end,
        "data_cutoff_at": dto.data_cutoff_at,
        "phase": dto.phase.value,
        "data_quality": dto.data_quality.value,
    }


def _decision_record(dto: BacktestDecisionDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "decision_id": dto.decision_id,
        "step_sequence": dto.step_sequence,
        "decision_time": dto.decision_time,
        "mode": dto.mode,
        "targets": _thaw_json(dict(dto.targets)),
        "validation_status": dto.validation_status.value,
        "validation_issues": list(dto.validation_issues),
        "duration_ms": dto.duration_ms,
        "error": dto.error,
    }


def _order_record(dto: BacktestOrderDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "order_id": dto.order_id,
        "intent_id": dto.intent_id,
        "instrument_id": dto.instrument_id,
        "event_trading_code": dto.display.event_trading_code,
        "event_name": dto.display.event_name,
        "event_display_name": dto.display.event_display_name,
        "side": dto.side.value,
        "order_type": dto.order_type,
        "price": dto.price,
        "quantity": dto.quantity,
        "filled_quantity": dto.filled_quantity,
        "status": dto.status.value,
        "status_reason": dto.status_reason,
        "submitted_at": dto.submitted_at,
    }


def _order_update_record(dto: BacktestOrderUpdateDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "order_id": dto.order_id,
        "update_sequence": dto.update_sequence,
        "old_status": dto.old_status.value if dto.old_status else None,
        "new_status": dto.new_status.value,
        "updated_at": dto.updated_at,
        "reason": dto.reason,
    }


def _fill_record(dto: BacktestFillDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "fill_id": dto.fill_id,
        "order_id": dto.order_id,
        "instrument_id": dto.instrument_id,
        "event_trading_code": dto.display.event_trading_code,
        "event_name": dto.display.event_name,
        "event_display_name": dto.display.event_display_name,
        "side": dto.side.value,
        "timestamp": dto.timestamp,
        "reference_price": dto.reference_price,
        "price": dto.price,
        "quantity": dto.quantity,
        "fees": dto.fees,
        "slippage_bps": dto.slippage_bps,
        "slippage_amount": dto.slippage_amount,
        "slippage_model_key": dto.slippage_model_key,
        "slippage_model_version": dto.slippage_model_version,
        "currency": dto.currency,
        "contract_multiplier": dto.contract_multiplier,
        "gross_notional": dto.gross_notional,
        "fee_breakdown": dto.fee_breakdown,
        "settlement_calendar_id": dto.settlement_calendar_id,
        "settlement_due_session": dto.settlement_due_session,
        "settlement_boundary_id": dto.settlement_boundary_id,
    }


def _position_record(dto: BacktestPositionDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "as_of": dto.as_of,
        "instrument_id": dto.instrument_id,
        "event_trading_code": dto.display.event_trading_code,
        "event_name": dto.display.event_name,
        "event_display_name": dto.display.event_display_name,
        "side": dto.side.value,
        "quantity": dto.quantity,
        "available_quantity": dto.available_quantity,
        "average_price": dto.average_price,
        "mark_price": dto.mark_price,
        "realized_pnl": dto.realized_pnl,
        "unrealized_pnl": dto.unrealized_pnl,
    }


def _equity_record(dto: BacktestEquityCurveDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "sequence": dto.sequence,
        "as_of": dto.as_of,
        "valuation_status": dto.valuation_status.value,
        "valuation_reason": dto.valuation_reason,
        "cash": dto.cash,
        "market_value": dto.market_value,
        "equity": dto.equity,
        "period_return": dto.period_return,
        "total_pnl": dto.total_pnl,
        "cumulative_return": dto.cumulative_return,
        "drawdown": dto.drawdown,
        "cumulative_fees": dto.cumulative_fees,
    }


def _metric_record(dto: BacktestMetricDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "metric_key": dto.metric_key,
        "formula_version": dto.formula_version,
        "value": dto.value,
        "unit": dto.unit,
        "annualization_factor": dto.annualization_factor,
        "risk_free_rate_note": dto.risk_free_rate_note,
        "sample_count": dto.sample_count,
        "unavailable_reason": dto.unavailable_reason,
        "analyzer_key": dto.analyzer_key,
        "analyzer_version": dto.analyzer_version,
        "analyzer_metadata": (
            _thaw_json(dict(dto.analyzer_metadata))
            if dto.analyzer_metadata is not None
            else None
        ),
    }


def _analysis_summary_record(dto: BacktestAnalysisSummaryDto) -> dict[str, Any]:
    """Map the summary DTO onto its ORM columns (identity excluded)."""

    return {
        "run_id": dto.run_id,
        "status": dto.status.value,
        "analyzer_snapshot": _thaw_json(dict(dto.analyzer_snapshot)),
        "formal_timeline": (
            _thaw_json(dict(dto.formal_timeline))
            if dto.formal_timeline is not None
            else None
        ),
        "formula_signature": dto.formula_signature,
        "input_evidence_signature": dto.input_evidence_signature,
        "initial_equity": dto.initial_equity,
        "valid_day_count": dto.valid_day_count,
        "candidate_return_count": dto.candidate_return_count,
        "fill_count": dto.fill_count,
        "gross_traded_notional": dto.gross_traded_notional,
        "cumulative_fees": dto.cumulative_fees,
        "rate_snapshot": (
            _thaw_json_value(dto.rate_snapshot)
            if dto.rate_snapshot is not None
            else None
        ),
        "rate_snapshot_hash": dto.rate_snapshot_hash,
        "rate_source_versions": (
            _thaw_json(dict(dto.rate_source_versions))
            if dto.rate_source_versions is not None
            else None
        ),
        "missing_ranges": (
            [_thaw_json_value(entry) for entry in dto.missing_ranges]
            if dto.missing_ranges is not None
            else None
        ),
        "reporting_currency": dto.reporting_currency,
        "last_chunk_sequence": dto.last_chunk_sequence,
        "last_chunk_token": dto.last_chunk_token,
        "completed_through_session": dto.completed_through_session,
        "abort_reason": dto.abort_reason,
        "failed_step_sequence": dto.failed_step_sequence,
        "terminal_fingerprint": dto.terminal_fingerprint,
        "created_at": dto.created_at,
        "updated_at": dto.updated_at,
        "finalized_at": dto.finalized_at,
    }


def _thaw_json_value(value: Any) -> Any:
    """Thaw one possibly nested frozen value without requiring a mapping."""

    from types import MappingProxyType as _MPT

    if isinstance(value, _MPT) or isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json_value(item) for item in value]
    return value


def _decimal_equal(left: Any, right: Any) -> bool:
    """Compare two optional decimal-ish values exactly."""

    if left is None or right is None:
        return left is right
    left_decimal = left if isinstance(left, Decimal) else Decimal(str(left))
    right_decimal = right if isinstance(right, Decimal) else Decimal(str(right))
    return left_decimal == right_decimal


def _metric_content_fingerprint(dto: BacktestMetricDto) -> tuple[Any, ...]:
    """Full normalized content identity of one metric row.

    Every evidence-bearing column participates, including
    ``annualization_factor`` and ``risk_free_rate_note``; ``sample_count``
    is compared raw so 0 and None stay distinct.
    """

    return (
        _decimal_text(dto.value),
        dto.unit,
        _decimal_text(dto.annualization_factor),
        dto.risk_free_rate_note,
        dto.sample_count,
        dto.unavailable_reason,
        (
            _thaw_json(dict(dto.analyzer_metadata))
            if dto.analyzer_metadata is not None
            else None
        ),
        dto.analyzer_key,
        dto.analyzer_version,
    )


def _orm_metric_matches(record: BacktestMetricRecord, dto: BacktestMetricDto) -> bool:
    """Exact content comparison between a persisted row and a retry DTO."""

    return _metric_content_fingerprint(
        BacktestMetricDto(
            run_id=record.run_id,
            metric_key=record.metric_key,
            formula_version=record.formula_version,
            value=record.value,
            unit=record.unit,
            annualization_factor=record.annualization_factor,
            risk_free_rate_note=record.risk_free_rate_note,
            sample_count=record.sample_count,
            unavailable_reason=record.unavailable_reason,
            analyzer_key=record.analyzer_key,
            analyzer_version=record.analyzer_version,
            analyzer_metadata=(
                dict(record.analyzer_metadata)
                if record.analyzer_metadata is not None
                else None
            ),
        )
    ) == _metric_content_fingerprint(dto)


def _decimal_text(value: Any) -> Any:
    """Return the value unchanged for numeric-equality fingerprinting.

    Decimals compare numerically under ``==`` regardless of exponent, and
    any normalization via ``normalize()`` would round under the process
    default 28-digit precision — colliding distinct NUMERIC(38,18) values.
    """

    return value


def _summary_content_fingerprint(
    dto: BacktestAnalysisSummaryDto,
) -> tuple[Any, ...]:
    """Full business-content fingerprint of a summary row.

    ``created_at``/``updated_at``/``finalized_at`` are audit timestamps
    and excluded; every other column participates so conflicting retries
    with different counts, E0, or rate evidence can never pass as
    idempotent.
    """

    return (
        dto.status.value,
        _thaw_json(dict(dto.analyzer_snapshot)),
        (
            _thaw_json(dict(dto.formal_timeline))
            if dto.formal_timeline is not None
            else None
        ),
        dto.formula_signature,
        dto.input_evidence_signature,
        dto.reporting_currency,
        _decimal_text(dto.initial_equity),
        dto.valid_day_count,
        dto.candidate_return_count,
        dto.fill_count,
        _decimal_text(dto.gross_traded_notional),
        _decimal_text(dto.cumulative_fees),
        (
            _thaw_json_value(dto.rate_snapshot)
            if dto.rate_snapshot is not None
            else None
        ),
        dto.rate_snapshot_hash,
        (
            _thaw_json(dict(dto.rate_source_versions))
            if dto.rate_source_versions is not None
            else None
        ),
        (
            [_thaw_json_value(entry) for entry in dto.missing_ranges]
            if dto.missing_ranges is not None
            else None
        ),
        dto.last_chunk_sequence,
        dto.last_chunk_token,
        dto.completed_through_session,
        dto.abort_reason,
        dto.failed_step_sequence,
        dto.terminal_fingerprint,
    )


def _summary_dto_from_record(
    record: BacktestAnalysisSummaryRecord,
) -> BacktestAnalysisSummaryDto:
    """Rebuild the immutable summary DTO from one ORM row."""

    missing_ranges = record.missing_ranges
    return BacktestAnalysisSummaryDto(
        run_id=record.run_id,
        status=record.status,
        # Thaw JSON payloads into plain containers so API serialization
        # never sees frozen mapping proxies.
        analyzer_snapshot=_thaw_json(record.analyzer_snapshot or {}),
        formal_timeline=(
            _thaw_json(record.formal_timeline)
            if record.formal_timeline is not None
            else None
        ),
        formula_signature=record.formula_signature,
        input_evidence_signature=record.input_evidence_signature,
        reporting_currency=record.reporting_currency,
        initial_equity=record.initial_equity,
        valid_day_count=record.valid_day_count,
        candidate_return_count=record.candidate_return_count,
        fill_count=record.fill_count,
        gross_traded_notional=record.gross_traded_notional,
        cumulative_fees=record.cumulative_fees,
        rate_snapshot=(
            _thaw_json_value(record.rate_snapshot)
            if record.rate_snapshot is not None
            else None
        ),
        rate_snapshot_hash=record.rate_snapshot_hash,
        rate_source_versions=(
            _thaw_json(record.rate_source_versions)
            if record.rate_source_versions is not None
            else None
        ),
        missing_ranges=tuple(missing_ranges) if missing_ranges is not None else None,
        last_chunk_sequence=record.last_chunk_sequence,
        last_chunk_token=record.last_chunk_token,
        completed_through_session=record.completed_through_session,
        abort_reason=record.abort_reason,
        failed_step_sequence=record.failed_step_sequence,
        terminal_fingerprint=record.terminal_fingerprint,
        created_at=_aware(record.created_at),
        updated_at=_aware(record.updated_at),
        finalized_at=(
            _aware(record.finalized_at) if record.finalized_at is not None else None
        ),
    )


def _preflight_record(dto: BacktestDataPreflightDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "phase": dto.phase.value,
        "status": dto.status,
        "report_hash": dto.report_hash,
        "hash_schema_version": dto.hash_schema_version,
        "capabilities": _thaw_json(dict(dto.capabilities)),
        "calendar_summary": _thaw_json(dict(dto.calendar_summary)),
        "session_summary": _thaw_json(dict(dto.session_summary)),
        "pit_status": dto.pit_status,
        "coverage": _thaw_json(dict(dto.coverage)),
        "source_revisions": _thaw_json(dict(dto.source_revisions)),
    }


def _chunk_record(dto: BacktestDataChunkDto) -> dict[str, Any]:
    return {
        "run_id": dto.run_id,
        "phase": dto.phase.value,
        "chunk_sequence": dto.chunk_sequence,
        "time_start": dto.time_start,
        "time_end": dto.time_end,
        "chunk_strategy_version": dto.chunk_strategy_version,
        "token_digest": dto.token_digest,
        "validation_status": dto.validation_status.value,
        "started_at": dto.started_at,
        "finished_at": dto.finished_at,
        "failure_reason": dto.failure_reason,
    }


# ---------------------------------------------------------------------------
# Result kind registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResultKindSpec:
    """Static description of one result type's persistence contract."""

    kind: str
    dto_cls: type
    record_cls: type
    to_record: Callable[[Any], dict[str, Any]]
    # Pagination sort keys: ORM column name plus wire element kind.
    sort_columns: tuple[str, ...]
    key_kinds: tuple[str, ...]
    # Business identity used for in-batch duplicate detection.
    identity_fields: tuple[str, ...]
    # Event timestamp used by inclusive start/end filters, when supported.
    time_column: str | None = None
    allowed_filters: frozenset[str] = frozenset()

    @property
    def upper_bound_columns(self) -> dict[str, str]:
        """Column-to-kind map validated inside every cursor token."""

        return dict(zip(self.sort_columns, self.key_kinds))


_RESULT_KINDS: dict[str, ResultKindSpec] = {
    spec.kind: spec
    for spec in (
        ResultKindSpec(
            kind="steps",
            dto_cls=BacktestStepDto,
            record_cls=BacktestStepRecord,
            to_record=_step_record,
            sort_columns=("step_sequence",),
            key_kinds=("int",),
            identity_fields=("step_sequence",),
            allowed_filters=frozenset({"phase"}),
        ),
        ResultKindSpec(
            kind="decisions",
            dto_cls=BacktestDecisionDto,
            record_cls=BacktestDecisionRecord,
            to_record=_decision_record,
            sort_columns=("step_sequence", "decision_time", "decision_id"),
            key_kinds=("int", "ts", "uuid"),
            identity_fields=("decision_id",),
            time_column="decision_time",
            allowed_filters=frozenset({"mode", "start_time", "end_time"}),
        ),
        ResultKindSpec(
            kind="orders",
            dto_cls=BacktestOrderDto,
            record_cls=BacktestOrderResultRecord,
            to_record=_order_record,
            sort_columns=("submitted_at", "order_id"),
            key_kinds=("ts", "uuid"),
            identity_fields=("order_id",),
            time_column="submitted_at",
            allowed_filters=frozenset(
                {"instrument_id", "status", "side", "start_time", "end_time"}
            ),
        ),
        ResultKindSpec(
            kind="order_updates",
            dto_cls=BacktestOrderUpdateDto,
            record_cls=BacktestOrderUpdateRecord,
            to_record=_order_update_record,
            sort_columns=("updated_at", "order_id", "update_sequence"),
            key_kinds=("ts", "uuid", "int"),
            identity_fields=("order_id", "update_sequence"),
            time_column="updated_at",
            allowed_filters=frozenset({"status", "start_time", "end_time"}),
        ),
        ResultKindSpec(
            kind="fills",
            dto_cls=BacktestFillDto,
            record_cls=BacktestFillResultRecord,
            to_record=_fill_record,
            sort_columns=("timestamp", "fill_id"),
            key_kinds=("ts", "uuid"),
            identity_fields=("fill_id",),
            time_column="timestamp",
            allowed_filters=frozenset(
                {"instrument_id", "side", "start_time", "end_time"}
            ),
        ),
        ResultKindSpec(
            kind="positions",
            dto_cls=BacktestPositionDto,
            record_cls=BacktestPositionResultRecord,
            to_record=_position_record,
            sort_columns=("as_of", "instrument_id", "side"),
            key_kinds=("ts", "uuid", "str"),
            identity_fields=("as_of", "instrument_id", "side"),
            time_column="as_of",
            allowed_filters=frozenset(
                {"instrument_id", "side", "start_time", "end_time"}
            ),
        ),
        ResultKindSpec(
            kind="equity_curve",
            dto_cls=BacktestEquityCurveDto,
            record_cls=BacktestEquityCurveRecord,
            to_record=_equity_record,
            sort_columns=("as_of", "sequence"),
            key_kinds=("ts", "int"),
            identity_fields=("sequence",),
            time_column="as_of",
            allowed_filters=frozenset({"start_time", "end_time"}),
        ),
        ResultKindSpec(
            kind="metrics",
            dto_cls=BacktestMetricDto,
            record_cls=BacktestMetricRecord,
            to_record=_metric_record,
            sort_columns=("metric_key", "formula_version"),
            key_kinds=("str", "str"),
            identity_fields=("metric_key", "formula_version"),
            allowed_filters=frozenset(),
        ),
        ResultKindSpec(
            kind="data_preflight",
            dto_cls=BacktestDataPreflightDto,
            record_cls=BacktestDataPreflightResultRecord,
            to_record=_preflight_record,
            sort_columns=("phase",),
            key_kinds=("str",),
            identity_fields=("phase",),
            allowed_filters=frozenset(),
        ),
        ResultKindSpec(
            kind="data_chunks",
            dto_cls=BacktestDataChunkDto,
            record_cls=BacktestDataChunkRecord,
            to_record=_chunk_record,
            sort_columns=("phase", "chunk_sequence"),
            key_kinds=("str", "int"),
            identity_fields=("phase", "chunk_sequence"),
            allowed_filters=frozenset({"phase"}),
        ),
    )
}


def get_result_kind_spec(kind: str) -> ResultKindSpec:
    try:
        return _RESULT_KINDS[kind]
    except KeyError as exc:
        raise UnknownResultKindError(f"unknown result kind: {kind!r}") from exc


# ---------------------------------------------------------------------------
# Query canonicalization
# ---------------------------------------------------------------------------


def _normalize_instant(value: datetime) -> str:
    """Canonical string form of a filter boundary for digest purposes."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ResultFilterError("start_time/end_time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _normalize_filter_value(name: str, value: Any) -> str | None:
    """Normalize one raw filter argument into its digest representation."""

    if value is None:
        return None
    if name == "instrument_id":
        return str(_require_uuid(name, value))
    if name == "start_time" or name == "end_time":
        return _normalize_instant(value)
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    raise ResultFilterError(f"unsupported filter value type for {name}")


def _require_uuid(name: str, value: Any) -> UUID:
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ResultFilterError(f"{name} must be a valid UUID") from exc
    if not isinstance(value, UUID):
        raise ResultFilterError(f"{name} must be a UUID")
    return value


def build_query_payload(
    spec: ResultKindSpec,
    *,
    run_id: UUID,
    limit: int,
    filters: Mapping[str, str],
    query_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical query description feeding the cursor digest.

    Every condition that can change the result set participates: run id,
    each filter, the fixed ascending sort keys, the page-size policy, and
    the concrete limit requested by the client.
    """

    # The persisted table name is an internal implementation detail.  The
    # preflight API has one canonical cursor resource identifier so tokens
    # remain valid across its legacy redirect and canonical path.
    cursor_kind = "backtest_data_preflight" if spec.kind == "data_preflight" else spec.kind
    payload = {
        "kind": cursor_kind,
        "run_id": str(run_id),
        "filters": dict(sorted(filters.items())),
        "direction": "asc",
        "sort_keys": list(spec.sort_columns),
        "page_size_policy": {"default": DEFAULT_PAGE_SIZE, "max": MAX_PAGE_SIZE},
        "limit": limit,
    }
    if query_context is not None:
        if not isinstance(query_context, Mapping):
            raise ResultFilterError("query_context must be a mapping")
        # Non-filter projections (for example section=calendar) still change
        # the wire result and therefore must be bound into every cursor.
        payload["query_context"] = dict(sorted(query_context.items()))
    return payload


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class BacktestResultRepository:
    """Write result facts and read them back through stable cursor pages."""

    def __init__(self, session: Session, *, cursor_signing_key: str) -> None:
        # Cursors are HMAC-signed server-side; without a secret they could be
        # forged into another syntactically valid shape.
        if not isinstance(cursor_signing_key, str) or not cursor_signing_key.strip():
            raise ValueError("cursor_signing_key must be non-blank text")
        self.session = session
        self._signing_key = cursor_signing_key

    # -- writes ------------------------------------------------------------

    def append(self, kind: str, *dtos: Any) -> int:
        """Persist validated DTOs; return the number of rows written.

        Duplicate business keys inside the batch or already persisted in the
        same run raise :class:`ResultRecordConflictError`; the caller owns
        the surrounding transaction.
        """

        spec = get_result_kind_spec(kind)
        if not dtos:
            return 0
        if kind == "metrics":
            # Analyzer-bearing metric rows must use the same frozen Registry
            # contract regardless of which public repository method is used.
            # Keep the identity-less shape only for legacy rows that cannot
            # claim a producer identity.
            has_identity = [
                getattr(dto, "analyzer_key", None) is not None
                or getattr(dto, "analyzer_version", None) is not None
                for dto in dtos
            ]
            if any(has_identity):
                raise ResultRepositoryError(
                    "analyzer-identified metrics must use append_metrics; "
                    "generic append is reserved for identity-less legacy rows"
                )
        seen: set[tuple[Any, ...]] = set()
        payloads: list[dict[str, Any]] = []
        for dto in dtos:
            if not isinstance(dto, spec.dto_cls):
                raise ResultRepositoryError(
                    f"{kind} expects {spec.dto_cls.__name__}, got "
                    f"{type(dto).__name__}"
                )
            identity = (
                dto.run_id,
                *(getattr(dto, name) for name in spec.identity_fields),
            )
            if identity in seen:
                raise ResultRecordConflictError(
                    f"duplicate {kind} identity {identity[1:]} within the same "
                    "run's batch"
                )
            seen.add(identity)
            payloads.append(spec.to_record(dto))
        try:
            self.session.add_all([spec.record_cls(**payload) for payload in payloads])
            self.session.flush()
        except IntegrityError as exc:
            raise ResultRecordConflictError(
                f"{kind} row violates the run-scoped uniqueness contract"
            ) from exc
        return len(payloads)

    def append_metrics(self, *dtos: Any) -> int:
        """Persist analyzer-produced metrics under v1 producer rules.

        This is the formal new-write path: every row must carry its
        ``(analyzer_key, analyzer_version)`` identity — identity-less
        legacy-shaped rows can only enter through the generic
        :meth:`append` compatibility path.  Enforced here, never only
        through database exceptions:

        1. DTO identity deduplication inside the batch per logical key
           ``(run_id, metric_key, formula_version)``;
        2. one logical key has exactly one producer: a resubmission with a
           different analyzer identity conflicts instead of overwriting;
        3. different formula versions of the same ``metric_key`` may be
           produced by different analyzers (Sharpe A/B/C coexist);
        4. resubmitting the same identity with the same normalized content
           is an idempotent no-op.
        """

        if not dtos:
            return 0
        for dto in dtos:
            if not isinstance(dto, BacktestMetricDto):
                raise ResultRepositoryError(
                    f"append_metrics expects {BacktestMetricDto.__name__}, "
                    f"got {type(dto).__name__}"
                )
            if dto.analyzer_key is None or dto.analyzer_version is None:
                raise ResultRepositoryError(
                    f"metric ({dto.metric_key!r}, "
                    f"{dto.formula_version!r}) lacks analyzer identity; "
                    "new writes must name their producer — the "
                    "identity-less shape is reserved for legacy reads"
                )
            # Registry-contract mapping: a registered analyzer identity may
            # only write the metric keys and formula versions its frozen
            # output contract declares.
            _validate_metric_producer_contract(dto)
        seen_identities: dict[tuple[Any, ...], BacktestMetricDto] = {}
        for dto in dtos:
            identity = (dto.run_id, dto.metric_key, dto.formula_version)
            previous = seen_identities.get(identity)
            if previous is not None:
                # Same logical key twice in one batch: identical content is
                # collapsed; anything else is a conflict.
                if _metric_content_fingerprint(previous) != (
                    _metric_content_fingerprint(dto)
                ):
                    raise ResultRecordConflictError(
                        f"metric ({dto.metric_key!r}, {dto.formula_version!r}) "
                        "was submitted twice within one batch with different "
                        "content"
                    )
                continue
            seen_identities[identity] = dto

        run_ids = {dto.run_id for dto in seen_identities.values()}
        summaries = {
            row.run_id: row
            for row in self.session.scalars(
                select(BacktestAnalysisSummaryRecord).where(
                    BacktestAnalysisSummaryRecord.run_id.in_(run_ids)
                ).with_for_update()
            )
        }
        for dto in seen_identities.values():
            summary = summaries.get(dto.run_id)
            if summary is None:
                raise ResultRecordConflictError(
                    "formal metrics require a terminal analysis summary in "
                    "the same transaction"
                )
            _validate_metric_against_summary(dto, summary)
        metric_keys = {dto.metric_key for dto in seen_identities.values()}
        existing_rows: dict[tuple[UUID, str, str], BacktestMetricRecord] = {}
        rows = self.session.scalars(
            select(BacktestMetricRecord).where(
                BacktestMetricRecord.run_id.in_(run_ids),
                BacktestMetricRecord.metric_key.in_(metric_keys),
            )
        )
        for row in rows:
            existing_rows[(row.run_id, row.metric_key, row.formula_version)] = row

        payloads: list[dict[str, Any]] = []
        for identity, dto in seen_identities.items():
            existing = existing_rows.get(identity)
            if existing is None:
                payloads.append(_metric_record(dto))
                continue
            incoming_producer = (dto.analyzer_key, dto.analyzer_version)
            persisted_producer = (existing.analyzer_key, existing.analyzer_version)
            if persisted_producer != incoming_producer:
                raise ResultRecordConflictError(
                    f"metric ({dto.metric_key!r}, {dto.formula_version!r}) "
                    f"is already produced by {persisted_producer}; refusing "
                    f"the new producer {incoming_producer}"
                )
            if _orm_metric_matches(existing, dto):
                continue  # Idempotent retry: nothing to write.
            raise ResultRecordConflictError(
                f"metric ({dto.metric_key!r}, {dto.formula_version!r}) "
                "already exists with different value or evidence; "
                "metric results are immutable"
            )
        try:
            self.session.add_all(
                [BacktestMetricRecord(**payload) for payload in payloads]
            )
            self.session.flush()
        except IntegrityError as exc:
            raise ResultRecordConflictError(
                "metrics row violates the run-scoped uniqueness contract"
            ) from exc
        return len(payloads)

    def upsert_analysis_summary(
        self, dto: BacktestAnalysisSummaryDto
    ) -> BacktestAnalysisSummaryRecord:
        """Insert or advance the run's analysis summary with terminal protection.

        ``partial`` summaries may be updated freely while they stay partial.
        ``final``/``aborted`` are terminal: an identical retry returns the
        persisted row unchanged, any conflicting write raises
        :class:`ResultRecordConflictError`.
        """

        from datetime import datetime as _datetime

        existing = self.session.scalars(
            select(BacktestAnalysisSummaryRecord).where(
                BacktestAnalysisSummaryRecord.run_id == dto.run_id
            ).with_for_update()
        ).first()
        payload = _analysis_summary_record(dto)
        if existing is None:
            if dto.last_chunk_sequence not in (None, 0):
                raise ResultRecordConflictError(
                    "the first persisted analysis checkpoint must start at sequence 0"
                )
            record = BacktestAnalysisSummaryRecord(**payload)
            self.session.add(record)
            try:
                self.session.flush()
            except IntegrityError as exc:
                raise ResultRecordConflictError(
                    "analysis summary violates its run uniqueness contract"
                ) from exc
            return record

        terminal_statuses = ("final", "aborted")
        if existing.status in terminal_statuses:
            # The terminal fingerprint covers every business field while
            # excluding chunk/audit timestamps.  Same-state/same-fingerprint
            # is the sole idempotent retry; every other transition conflicts.
            if (
                existing.status == dto.status.value
                and existing.terminal_fingerprint == dto.terminal_fingerprint
            ):
                return existing
            raise ResultRecordConflictError(
                f"the analysis summary of this run is already terminal "
                f"(status={existing.status}); partial progress can never "
                "overwrite it"
            )

        # Partial -> anything is an allowed forward transition, but stale
        # progress can never rewind a newer checkpoint: an older chunk
        # sequence (or session boundary) is rejected outright, and the
        # same sequence only passes when its content is identical.
        incoming_status = dto.status.value
        if incoming_status == "aborted" and (
            dto.last_chunk_sequence,
            dto.last_chunk_token,
            dto.completed_through_session,
        ) != (
            existing.last_chunk_sequence,
            existing.last_chunk_token,
            existing.completed_through_session,
        ):
            raise ResultRecordConflictError(
                "aborted finalization must preserve the last successful checkpoint exactly"
            )
        if existing.last_chunk_sequence is not None:
            if dto.last_chunk_sequence is None:
                raise ResultRecordConflictError(
                    "stale progress: incoming checkpoint has no chunk "
                    "sequence after a sequenced checkpoint was persisted"
                )
            if dto.last_chunk_sequence is not None and (
                dto.last_chunk_sequence < existing.last_chunk_sequence
            ):
                raise ResultRecordConflictError(
                    f"stale partial progress: incoming chunk sequence "
                    f"{dto.last_chunk_sequence} precedes persisted "
                    f"{existing.last_chunk_sequence}"
                )
            if (
                incoming_status in ("partial", "final")
                and dto.last_chunk_sequence > existing.last_chunk_sequence + 1
            ):
                raise ResultRecordConflictError(
                    "successful checkpoint sequences must advance contiguously"
                )
            if dto.last_chunk_sequence == existing.last_chunk_sequence:
                if dto.last_chunk_token != existing.last_chunk_token:
                    raise ResultRecordConflictError(
                        "conflicting checkpoint token for the same chunk sequence"
                    )
                if incoming_status == "final":
                    raise ResultRecordConflictError(
                        "a final summary must advance beyond the persisted "
                        "partial chunk sequence"
                    )
                if incoming_status == "partial" and (
                    _summary_content_fingerprint(_summary_dto_from_record(existing))
                    != _summary_content_fingerprint(dto)
                ):
                    raise ResultRecordConflictError(
                        "conflicting partial progress for the same chunk "
                        "sequence; identical retries are accepted, diverging "
                        "content is not"
                    )
            if (
                incoming_status == "aborted"
                and dto.last_chunk_sequence > existing.last_chunk_sequence
            ):
                raise ResultRecordConflictError(
                    "aborted progress cannot claim a checkpoint from the failed chunk"
                )
        if existing.completed_through_session is not None:
            if dto.completed_through_session is None:
                raise ResultRecordConflictError(
                    "stale progress: incoming checkpoint has no session "
                    "boundary after a session boundary was persisted"
                )
            if (
                dto.completed_through_session is not None
                and dto.completed_through_session < existing.completed_through_session
            ):
                raise ResultRecordConflictError(
                    "stale partial progress: incoming completed session "
                    f"{dto.completed_through_session} precedes persisted "
                    f"{existing.completed_through_session}"
                )

        for column, value in payload.items():
            if column in ("created_at",):
                continue
            setattr(existing, column, value)
        existing.updated_at = _datetime.now(timezone.utc)
        self.session.flush()
        return existing

    def get_analysis_summary(
        self,
        run_id: UUID | str,
    ) -> BacktestAnalysisSummaryDto | None:
        """Read the single analysis summary bound to one run."""

        run_uuid = _require_uuid("run_id", run_id)
        record = self.session.scalars(
            select(BacktestAnalysisSummaryRecord).where(
                BacktestAnalysisSummaryRecord.run_id == run_uuid
            )
        ).first()
        if record is None:
            return None
        return _summary_dto_from_record(record)

    # -- reads -------------------------------------------------------------

    def read_page(
        self,
        kind: str,
        *,
        run_id: UUID | str,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        query_context: Mapping[str, Any] | None = None,
        **raw_filters: Any,
    ) -> CursorPage:
        """Return one stable page of results for a run.

        The first call captures the maximum visible sort-key tuple as the
        query's upper bound inside the SAME SQL statement that reads the
        page (window functions), so under READ COMMITTED the page and the
        bound always come from one snapshot.  Every later page reached
        through the returned cursor stays inside that bound, so rows
        appended while a client is paging never create duplicates or shift
        earlier pages.
        """

        spec = get_result_kind_spec(kind)
        run_uuid = _require_uuid("run_id", run_id)
        checked_limit = normalize_limit(limit)

        filters: dict[str, str] = {}
        for name, value in raw_filters.items():
            if name not in spec.allowed_filters:
                raise ResultFilterError(f"{kind} does not support filter {name!r}")
            normalized = _normalize_filter_value(name, value)
            if normalized is not None:
                filters[name] = normalized

        payload = build_query_payload(
            spec,
            run_id=run_uuid,
            limit=checked_limit,
            filters=filters,
            query_context=query_context,
        )

        if cursor is None:
            return self._read_first_page(spec, payload, run_uuid, filters, checked_limit)
        return self._read_continuation_page(
            spec, payload, run_uuid, filters, checked_limit, cursor
        )

    # -- internals ---------------------------------------------------------

    def _read_first_page(
        self,
        spec: ResultKindSpec,
        payload: Mapping[str, Any],
        run_id: UUID,
        filters: Mapping[str, str],
        limit: int,
    ) -> CursorPage:
        """Read the first page and its upper bound from one statement.

        ``first_value(...) OVER (ORDER BY <sort> DESC)`` exposes the maximum
        visible sort-key columns next to every row.  Because window
        functions are evaluated over the full filtered row set before LIMIT,
        the page and the bound cannot come from different snapshots.
        """

        statement = self._first_page_statement(spec, run_id, filters)
        executed = list(self.session.execute(statement.limit(limit + 1)))
        rows = [row[0] for row in executed]
        # Re-sort defensively so tie handling stays observable in Python.
        rows.sort(key=lambda row: self._row_sort_key(spec, row))
        has_more = len(rows) > limit
        items = tuple(rows[:limit])
        if not items:
            return CursorPage(items=(), next_cursor=None, has_more=False)

        next_cursor: str | None = None
        if has_more:
            bound = self._bound_from_window_row(spec, executed[0])
            encoded_bound = self._encode_bound(spec, dict(zip(spec.sort_columns, bound)))
            digest = compute_query_digest({**payload, "query_upper_bound": encoded_bound})
            next_cursor = build_cursor(
                signing_key=self._signing_key,
                query_digest=digest,
                key_kinds=spec.key_kinds,
                last_sort_key=self._row_sort_key(spec, items[-1]),
                upper_bound_columns=spec.upper_bound_columns,
                query_upper_bound=dict(zip(spec.sort_columns, bound)),
            )
        return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    def _read_continuation_page(
        self,
        spec: ResultKindSpec,
        payload: Mapping[str, Any],
        run_id: UUID,
        filters: Mapping[str, str],
        limit: int,
        cursor: str,
    ) -> CursorPage:
        parsed = parse_cursor(
            cursor,
            signing_key=self._signing_key,
            key_kinds=spec.key_kinds,
            upper_bound_columns=spec.upper_bound_columns,
        )
        # The snapshot upper bound of the original first page is reused;
        # recomputing it would admit rows appended after the walk began.
        bound_values = tuple(
            parsed.query_upper_bound[column] for column in spec.sort_columns
        )
        encoded_bound = self._encode_bound(
            spec, dict(zip(spec.sort_columns, bound_values))
        )
        expected_digest = compute_query_digest(
            {**payload, "query_upper_bound": encoded_bound}
        )
        if not hmac_compare(parsed.query_digest, expected_digest):
            raise CursorQueryMismatchError(
                "cursor belongs to a different query; restart from the first page"
            )

        statement = self._base_statement(spec, run_id, filters).where(
            self._keyset_predicate(spec, parsed.last_sort_key, mode="after")
        ).where(
            # Inclusive bound: the maximum row visible at snapshot time must
            # stay readable; appended rows sort strictly beyond it.
            self._keyset_predicate(spec, bound_values, mode="at_or_before")
        )
        rows = list(self.session.scalars(statement.limit(limit + 1)))
        rows.sort(key=lambda row: self._row_sort_key(spec, row))
        has_more = len(rows) > limit
        items = tuple(rows[:limit])
        if not items:
            return CursorPage(items=(), next_cursor=None, has_more=False)

        next_cursor: str | None = None
        if has_more:
            next_cursor = build_cursor(
                signing_key=self._signing_key,
                query_digest=expected_digest,
                key_kinds=spec.key_kinds,
                last_sort_key=self._row_sort_key(spec, items[-1]),
                upper_bound_columns=spec.upper_bound_columns,
                query_upper_bound=dict(zip(spec.sort_columns, bound_values)),
            )
        return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    def _first_page_statement(
        self,
        spec: ResultKindSpec,
        run_id: UUID,
        filters: Mapping[str, str],
    ):
        record_cls = spec.record_cls
        conditions: list[Any] = [record_cls.run_id == run_id]
        conditions.extend(
            self._filter_condition(spec, name, value) for name, value in filters.items()
        )
        # All first_value calls MUST share one window ordered by the FULL
        # sort-key tuple.  Ordering each column independently would pick the
        # maximum of every column from different rows, producing a bound no
        # real row ever had.
        window_order = [
            getattr(record_cls, column_name).desc()
            for column_name in spec.sort_columns
        ]
        window_columns = []
        for position, column_name in enumerate(spec.sort_columns):
            column = getattr(record_cls, column_name)
            # Carry the column type into the function expression so the
            # dialect's result processor still converts raw DB values.
            first_value = func.first_value(column, type_=column.type)
            window_columns.append(
                first_value.over(order_by=window_order).label(
                    f"__upper_bound_{position}"
                )
            )
        return (
            select(record_cls, *window_columns)
            .where(*conditions)
            .order_by(*[getattr(record_cls, column).asc() for column in spec.sort_columns])
        )

    def _encode_bound(self, spec: ResultKindSpec, bound: Mapping[str, Any]) -> dict[str, Any]:
        """Canonical wire form of an upper bound, feeding the query digest.

        Binding the digest to the bound means a cursor can only be paired
        with the exact query state it was issued for.
        """

        return {
            column: encode_sort_element(spec.upper_bound_columns[column], bound[column])
            for column in sorted(bound)
        }

    def _bound_from_window_row(self, spec: ResultKindSpec, row: Any) -> tuple[Any, ...]:
        values: list[Any] = []
        for position, kind in enumerate(spec.key_kinds):
            raw = getattr(row, f"__upper_bound_{position}")
            values.append(_normalize_sort_value(kind, raw))
        return tuple(values)

    def _base_statement(
        self,
        spec: ResultKindSpec,
        run_id: UUID,
        filters: Mapping[str, str],
        *,
        descending: bool = False,
    ):
        record_cls = spec.record_cls
        statement = select(record_cls).where(record_cls.run_id == run_id)
        for name, value in filters.items():
            statement = statement.where(self._filter_condition(spec, name, value))
        directions = (
            [getattr(record_cls, column).desc() for column in spec.sort_columns]
            if descending
            else [getattr(record_cls, column).asc() for column in spec.sort_columns]
        )
        statement = statement.order_by(*directions)
        return statement

    def _filter_condition(self, spec: ResultKindSpec, name: str, value: str):
        record_cls = spec.record_cls
        if name == "instrument_id":
            return record_cls.instrument_id == UUID(value)
        if name == "start_time":
            assert spec.time_column is not None
            return getattr(record_cls, spec.time_column) >= datetime.fromisoformat(value)
        if name == "end_time":
            assert spec.time_column is not None
            return getattr(record_cls, spec.time_column) <= datetime.fromisoformat(value)
        # Remaining filters target a column named after the filter itself,
        # except the order-update alias `status` -> `new_status`.
        column_name = "new_status" if name == "status" and spec.kind == "order_updates" else name
        return getattr(record_cls, column_name) == value

    def _keyset_predicate(
        self,
        spec: ResultKindSpec,
        values: Sequence[Any],
        *,
        mode: str,
    ):
        """Tuple comparison expanded into OR-of-ANDS for portability.

        ``mode="after"`` selects tuples strictly greater than ``values``
        (page continuation).  ``mode="at_or_before"`` selects tuples less
        than or equal to ``values`` (the snapshot upper bound), so the
        maximum row visible when pagination started stays readable.
        """

        columns = [getattr(spec.record_cls, column) for column in spec.sort_columns]
        inclusive = mode == "at_or_before"
        conditions = []
        last_position = len(columns) - 1
        for position in range(len(columns)):
            conjunction = [columns[index] == values[index] for index in range(position)]
            column = columns[position]
            value = values[position]
            if inclusive:
                conjunction.append(
                    column <= value if position == last_position else column < value
                )
            else:
                conjunction.append(column > value)
            conditions.append(and_(*conjunction))
        return or_(*conditions)

    def _row_sort_key(self, spec: ResultKindSpec, row: Any) -> tuple[Any, ...]:
        """Typed, comparable sort-key tuple of one ORM row."""

        return tuple(
            _normalize_sort_value(kind, getattr(row, column))
            for column, kind in zip(spec.sort_columns, spec.key_kinds)
        )


__all__ = [
    "BacktestResultRepository",
    "ResultFilterError",
    "ResultRecordConflictError",
    "ResultRepositoryError",
    "UnknownResultKindError",
    "build_query_payload",
    "get_result_kind_spec",
]
