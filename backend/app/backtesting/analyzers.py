"""Analyzer specifications, the analyzer engine, and v1 metric formulas.

This module implements task package 06's frozen analyzer contracts:

* :class:`AnalyzerSpec` describes one logical-metric producer with its
  frozen parameters and complete output contract;
* :class:`AnalyzerEngine` accumulates accounting/valuation facts
  (:class:`~app.backtesting.analysis_inputs` objects), enforces run/currency
  identity, strict session ordering, fill deduplication, and producer
  uniqueness, and produces :class:`MetricResult` rows on finalize;
* the five v1 analyzers (``sharpe_simple@1``, ``sharpe_pit_rf@1``,
  ``sharpe_config_rf@1``, ``turnover@1``, ``fee_summary@1``) are built in
  with their frozen formulas, sample-count semantics, and fixed reason
  codes;
* every intermediate computation runs inside a fixed ``Decimal`` context
  (``prec=50``, ``ROUND_HALF_EVEN``); results are quantized to 18 places
  once, immediately before persistence-shaped output.

Unavailable metrics are first-class queryable results carrying a fixed
``reason_code`` inside ``analyzer_metadata`` and user-facing text in
``unavailable_reason`` -- never NaN, Infinity, or a fabricated zero.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable
from uuid import UUID

from app.backtesting.analysis_inputs import (
    ANALYSIS_EVIDENCE_HASH_ALGORITHM,
    AppliedFillFact,
    CanonicalEvidenceValue,
    EquityObservation,
    FillObservation,
    InitialEquitySnapshot,
    PitRateSnapshot,
    canonical_evidence_json,
    compute_input_evidence_signature,
)
from app.backtesting.domain import DomainValidationError

__all__ = [
    "ANALYSIS_DECIMAL_PRECISION",
    "ANALYSIS_PERSISTENCE_SCALE",
    "ANNUALIZATION_FACTOR",
    "AnalysisSnapshot",
    "AnalysisStateConflictError",
    "AnalysisStatus",
    "AnalyzerConfigurationError",
    "AnalyzerEngine",
    "AnalyzerSpec",
    "ANNUAL_RATE_CONVERTER_KEY",
    "ANNUAL_RATE_CONVERTER_VERSION",
    "FEE_SUMMARY_ANALYZER_KEY",
    "FORMULA_VERSION_CUMULATIVE_FEES",
    "FORMULA_VERSION_FEE_TO_GROSS",
    "FORMULA_VERSION_SHARPE_CONFIG_RF",
    "FORMULA_VERSION_SHARPE_PIT_RF",
    "FORMULA_VERSION_SHARPE_SIMPLE",
    "FORMULA_VERSION_TURNOVER",
    "METRIC_KEY_CUMULATIVE_FEES",
    "METRIC_KEY_FEE_TO_GROSS_TRADED_NOTIONAL",
    "METRIC_KEY_SHARPE",
    "METRIC_KEY_TURNOVER",
    "MetricResult",
    "MetricStatus",
    "PIT_RF_ANALYZER_KEY",
    "ReasonCode",
    "SHARPE_SIMPLE_ANALYZER_KEY",
    "CONFIG_RF_ANALYZER_KEY",
    "TURNOVER_ANALYZER_KEY",
    "analyzer_decimal_context",
]


# ---------------------------------------------------------------------------
# Frozen identities and numeric policy
# ---------------------------------------------------------------------------

ANALYSIS_DECIMAL_PRECISION = 50
ANALYSIS_PERSISTENCE_SCALE = 18
ANNUALIZATION_FACTOR = Decimal("252")

SHARPE_SIMPLE_ANALYZER_KEY = "sharpe_simple"
PIT_RF_ANALYZER_KEY = "sharpe_pit_rf"
CONFIG_RF_ANALYZER_KEY = "sharpe_config_rf"
TURNOVER_ANALYZER_KEY = "turnover"
FEE_SUMMARY_ANALYZER_KEY = "fee_summary"

ANNUAL_RATE_CONVERTER_KEY = "annual_rate_div_252"
ANNUAL_RATE_CONVERTER_VERSION = 1

METRIC_KEY_SHARPE = "sharpe"
METRIC_KEY_TURNOVER = "turnover"
METRIC_KEY_CUMULATIVE_FEES = "cumulative_fees"
METRIC_KEY_FEE_TO_GROSS_TRADED_NOTIONAL = "fee_to_gross_traded_notional"

FORMULA_VERSION_SHARPE_SIMPLE = "sharpe_simple_ddof1_252_v1"
FORMULA_VERSION_SHARPE_PIT_RF = "sharpe_pit_rf_ddof1_252_v1"
FORMULA_VERSION_SHARPE_CONFIG_RF = "sharpe_config_rf_ddof1_252_v1"
FORMULA_VERSION_TURNOVER = "turnover_gross_notional_avg_eod_equity_v1"
FORMULA_VERSION_CUMULATIVE_FEES = "cumulative_applied_fill_fees_v1"
FORMULA_VERSION_FEE_TO_GROSS = "fee_to_gross_traded_notional_v1"


def analyzer_decimal_context():
    """Context manager pinning the frozen analyzer Decimal policy.

    The context (``prec=50``, ``ROUND_HALF_EVEN``) applies to every
    intermediate operation including ``sqrt``; the process-global default
    context is never read or modified.
    """

    return localcontext(Context(prec=ANALYSIS_DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN))


def quantize_for_persistence(value: Decimal) -> Decimal:
    """Quantize one result to the persistence scale exactly once."""

    return value.quantize(
        Decimal(1).scaleb(-ANALYSIS_PERSISTENCE_SCALE),
        rounding=ROUND_HALF_EVEN,
        context=Context(prec=ANALYSIS_DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN),
    )


# ---------------------------------------------------------------------------
# Status enums and fixed reason codes
# ---------------------------------------------------------------------------


class MetricStatus(StrEnum):
    """Whether one metric row carries a value or an explicit reason."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class AnalysisStatus(StrEnum):
    """Run-level analysis lifecycle status."""

    PARTIAL = "partial"
    FINAL = "final"
    ABORTED = "aborted"


class ReasonCode(StrEnum):
    """The frozen v1 set of analyzer unavailability/blocked reason codes."""

    MISSING_INITIAL_MARK = "MISSING_INITIAL_MARK"
    NON_POSITIVE_INITIAL_EQUITY = "NON_POSITIVE_INITIAL_EQUITY"
    INSUFFICIENT_RETURNS = "INSUFFICIENT_RETURNS"
    ZERO_RETURN_STDDEV = "ZERO_RETURN_STDDEV"
    INVALID_EQUITY = "INVALID_EQUITY"
    MISSING_PIT_RF = "MISSING_PIT_RF"
    NO_VALID_END_OF_DAY_EQUITY = "NO_VALID_END_OF_DAY_EQUITY"
    NON_POSITIVE_AVERAGE_EQUITY = "NON_POSITIVE_AVERAGE_EQUITY"
    ZERO_GROSS_TRADED_NOTIONAL = "ZERO_GROSS_TRADED_NOTIONAL"
    UNMODELED_EXTERNAL_CASH_FLOW = "UNMODELED_EXTERNAL_CASH_FLOW"
    INVALID_ANALYZER_CONFIG = "INVALID_ANALYZER_CONFIG"


#: User-facing Chinese fallback text for every fixed reason code.
REASON_CODE_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        ReasonCode.MISSING_INITIAL_MARK.value: "初始持仓缺少严格早于首个开盘的 PIT 标记价",
        ReasonCode.NON_POSITIVE_INITIAL_EQUITY.value: "初始资金 E0 小于等于 0",
        ReasonCode.INSUFFICIENT_RETURNS.value: "有效收益点少于 2 个",
        ReasonCode.ZERO_RETURN_STDDEV.value: "收益率标准差为 0",
        ReasonCode.INVALID_EQUITY.value: "存在缺失、非有限或小于等于 0 的日末权益",
        ReasonCode.MISSING_PIT_RF.value: "一个或多个有效交易日缺少 PIT 无风险利率",
        ReasonCode.NO_VALID_END_OF_DAY_EQUITY.value: "没有可用日末权益，无法计算换手率",
        ReasonCode.NON_POSITIVE_AVERAGE_EQUITY.value: "有效日末权益平均值小于等于 0",
        ReasonCode.ZERO_GROSS_TRADED_NOTIONAL.value: "毛成交额为 0，费用占比无分母",
        ReasonCode.UNMODELED_EXTERNAL_CASH_FLOW.value: "现金变动无法归类为已建模的现金流类型",
        ReasonCode.INVALID_ANALYZER_CONFIG.value: "分析器或利率运行配置不完整或非法",
    }
)


class AnalyzerConfigurationError(DomainValidationError):
    """Raised when an analyzer/rate configuration is incomplete or invalid.

    Carries the fixed ``INVALID_ANALYZER_CONFIG`` reason code so callers
    block run creation with the documented reason instead of a bare error.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason_code = ReasonCode.INVALID_ANALYZER_CONFIG.value


class AnalyzerInputError(DomainValidationError):
    """Raised when the engine receives out-of-contract input facts."""


class AnalysisStateConflictError(DomainValidationError):
    """Raised when finalize is attempted twice or after a terminal state."""


class _MetricUnavailable(Exception):
    """Internal control-flow signal carrying one unavailability outcome."""

    def __init__(
        self,
        reason_code: ReasonCode | str,
        *,
        message: str | None = None,
        sample_count: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        code = getattr(reason_code, "value", reason_code)
        super().__init__(message or REASON_CODE_MESSAGES.get(code, code))
        self.reason_code = code
        self.sample_count = sample_count
        self.metadata = dict(metadata or {})


# ---------------------------------------------------------------------------
# Metric results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One persisted-shape metric row produced by one analyzer."""

    run_id: str
    metric_key: str
    formula_version: str
    analyzer_key: str
    analyzer_version: int
    status: MetricStatus | str
    value: Decimal | int | str | None = None
    unit: str | None = None
    sample_count: int | None = None
    unavailable_reason: str | None = None
    analyzer_metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise DomainValidationError("run_id must be non-blank text")
        for name in ("metric_key", "formula_version", "analyzer_key"):
            text = getattr(self, name)
            if not isinstance(text, str) or not text.strip():
                raise DomainValidationError(f"{name} must be non-blank text")
        if (
            isinstance(self.analyzer_version, bool)
            or not isinstance(self.analyzer_version, int)
            or self.analyzer_version <= 0
        ):
            raise DomainValidationError("analyzer_version must be a positive integer")
        try:
            status = MetricStatus(getattr(self.status, "value", self.status))
        except ValueError as exc:
            raise DomainValidationError("status must be available or unavailable") from exc
        object.__setattr__(self, "status", status)
        if self.value is not None:
            if isinstance(self.value, bool) or isinstance(self.value, float) or not isinstance(
                self.value, (Decimal, int, str)
            ):
                raise DomainValidationError(
                    "value must be Decimal, int, or str; binary floats are rejected"
                )
            try:
                normalized = Decimal(str(self.value))
            except InvalidOperation as exc:
                raise DomainValidationError("value is not a valid decimal") from exc
            if not normalized.is_finite():
                raise DomainValidationError(
                    "value must be finite; NaN and Infinity are never persisted"
                )
            object.__setattr__(self, "value", quantize_for_persistence(normalized))
        metadata = self.analyzer_metadata
        if metadata is None:
            object.__setattr__(self, "analyzer_metadata", MappingProxyType({}))
        elif isinstance(metadata, Mapping):
            object.__setattr__(
                self,
                "analyzer_metadata",
                MappingProxyType(dict(metadata)),
            )
        else:
            raise DomainValidationError("analyzer_metadata must be a mapping")
        # Value xor reason mirrors the database constraint exactly.
        if (self.value is None) != (status is MetricStatus.UNAVAILABLE):
            raise DomainValidationError(
                "available metrics carry a value; unavailable metrics carry "
                "only an unavailable_reason"
            )
        if status is MetricStatus.UNAVAILABLE:
            reason = self.unavailable_reason
            if not isinstance(reason, str) or not reason.strip():
                raise DomainValidationError(
                    "unavailable metrics require user-facing text in "
                    "unavailable_reason"
                )
            if "reason_code" not in self.analyzer_metadata:
                raise DomainValidationError(
                    "unavailable metrics must record reason_code in "
                    "analyzer_metadata"
                )
        elif self.unavailable_reason is not None:
            raise DomainValidationError(
                "available metrics cannot carry an unavailable_reason"
            )

    @classmethod
    def available(
        cls,
        *,
        run_id: str,
        spec: "AnalyzerSpec",
        metric_key: str,
        formula_version: str,
        value: Decimal,
        unit: str,
        sample_count: int | None,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> "MetricResult":
        return cls(
            run_id=run_id,
            metric_key=metric_key,
            formula_version=formula_version,
            analyzer_key=spec.analyzer_key,
            analyzer_version=spec.analyzer_version,
            status=MetricStatus.AVAILABLE,
            value=value,
            unit=unit,
            sample_count=sample_count,
            analyzer_metadata=dict(extra_metadata or {}),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        run_id: str,
        spec: "AnalyzerSpec",
        metric_key: str,
        formula_version: str,
        unit: str | None,
        sample_count: int | None,
        reason_code: ReasonCode | str,
        message: str | None = None,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> "MetricResult":
        code = getattr(reason_code, "value", reason_code)
        metadata = {"reason_code": code}
        metadata.update(dict(extra_metadata or {}))
        return cls(
            run_id=run_id,
            metric_key=metric_key,
            formula_version=formula_version,
            analyzer_key=spec.analyzer_key,
            analyzer_version=spec.analyzer_version,
            status=MetricStatus.UNAVAILABLE,
            value=None,
            unit=unit,
            sample_count=sample_count,
            unavailable_reason=message or REASON_CODE_MESSAGES.get(code, code),
            analyzer_metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Analyzer specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricOutputDescriptor:
    """Complete declaration of one metric a spec produces."""

    metric_key: str
    formula_version: str
    unit: str | None = None
    sample_count_semantics: str | None = None

    def __post_init__(self) -> None:
        for name in ("metric_key", "formula_version"):
            text = getattr(self, name)
            if not isinstance(text, str) or not text.strip():
                raise DomainValidationError(f"{name} must be non-blank text")
        if self.unit is not None and (
            not isinstance(self.unit, str) or not self.unit.strip()
        ):
            raise DomainValidationError("unit must be non-blank text when provided")

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_key": self.metric_key,
            "formula_version": self.formula_version,
            "unit": self.unit,
            "sample_count_semantics": self.sample_count_semantics,
        }


@dataclass(frozen=True, slots=True)
class AnalyzerSpec:
    """One immutable logical-metric producer description."""

    analyzer_key: str
    analyzer_version: int
    name_zh: str
    name_en: str
    output_contract: Sequence[MetricOutputDescriptor]
    parameters: Mapping[str, Any] | None = None
    input_contract: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        import re

        if (
            not isinstance(self.analyzer_key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*", self.analyzer_key)
            or "@" in self.analyzer_key
        ):
            raise DomainValidationError(
                "analyzer_key must be lowercase letters, digits, and "
                "underscores without '@'"
            )
        if (
            isinstance(self.analyzer_version, bool)
            or not isinstance(self.analyzer_version, int)
            or self.analyzer_version <= 0
        ):
            raise DomainValidationError("analyzer_version must be a positive integer")
        for field_name in ("name_zh", "name_en"):
            text = getattr(self, field_name)
            if not isinstance(text, str) or not text.strip():
                raise DomainValidationError(f"{field_name} must be non-blank text")
        outputs = tuple(self.output_contract)
        if not outputs:
            raise DomainValidationError("output_contract must declare at least one metric")
        keys = [descriptor.metric_key for descriptor in outputs]
        if len(set(keys)) != len(keys):
            raise DomainValidationError("output_metric_keys must not repeat")
        object.__setattr__(self, "output_contract", outputs)
        frozen_parameters = _freeze_json_mapping(self.parameters, "parameters")
        object.__setattr__(self, "parameters", frozen_parameters)
        object.__setattr__(
            self,
            "input_contract",
            _freeze_json_mapping(self.input_contract, "input_contract"),
        )

    @property
    def output_metric_keys(self) -> tuple[str, ...]:
        return tuple(descriptor.metric_key for descriptor in self.output_contract)

    @property
    def display_identity(self) -> str:
        """Documentation notation ``key@version`` (never a registry key)."""

        return f"{self.analyzer_key}@{self.analyzer_version}"

    def describe(self) -> dict[str, Any]:
        """Frozen JSON-safe snapshot stored into the run summary."""

        return {
            "analyzer_key": self.analyzer_key,
            "analyzer_version": self.analyzer_version,
            "name_zh": self.name_zh,
            "name_en": self.name_en,
            "parameters": json.loads(canonical_evidence_json(dict(self.parameters))),
            "input_contract": json.loads(
                canonical_evidence_json(dict(self.input_contract))
            ),
            "output_contract": [
                descriptor.as_dict() for descriptor in self.output_contract
            ],
        }


def _freeze_json_mapping(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise DomainValidationError(f"{field_name} must be a mapping")
    return MappingProxyType(dict(value))


# ---------------------------------------------------------------------------
# Shared analysis state used by the v1 producers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EquitySeries:
    """Derived view over the observed equity timeline."""

    initial_equity: Decimal
    ordered_observations: tuple[EquityObservation, ...]
    valid_day_count: int
    invalid_dates: tuple[date, ...]

    @property
    def has_invalid_points(self) -> bool:
        return bool(self.invalid_dates)


def _build_equity_series(
    observations: Sequence[EquityObservation],
    initial_equity: Decimal,
) -> _EquitySeries:
    valid_day_count = 0
    invalid_dates: list[date] = []
    for observation in observations:
        equity = observation.equity
        if observation.is_valid and equity is not None and equity > 0:
            valid_day_count += 1
        else:
            invalid_dates.append(observation.session_date)
    return _EquitySeries(
        initial_equity=initial_equity,
        ordered_observations=tuple(observations),
        valid_day_count=valid_day_count,
        invalid_dates=tuple(invalid_dates),
    )


def _daily_returns(series: _EquitySeries) -> list[Decimal]:
    """Simple daily returns ``r_d = E_d / E_(d-1) - 1`` over the timeline.

    The first return compares the first formal session against the frozen
    E0; zero-return days are kept.  The caller guarantees there are no
    invalid points before this helper is invoked.
    """

    equities = [series.initial_equity]
    equities.extend(
        observation.equity
        for observation in series.ordered_observations
        if observation.is_valid
    )
    returns: list[Decimal] = []
    for previous, current in zip(equities, equities[1:]):
        assert previous is not None and current is not None
        with analyzer_decimal_context():
            returns.append(current / previous - 1)
    return returns


def _invalid_equity_unavailable(
    series: _EquitySeries,
) -> _MetricUnavailable:
    return _MetricUnavailable(
        ReasonCode.INVALID_EQUITY,
        metadata={
            "invalid_session_dates": [
                day.isoformat() for day in series.invalid_dates
            ],
        },
    )


def _sharpe_from_x(x_values: Sequence[Decimal]) -> Decimal:
    """ddof=1 Sharpe with the fixed 252 annualization factor."""

    n = len(x_values)
    if n < 2:
        raise _MetricUnavailable(ReasonCode.INSUFFICIENT_RETURNS, sample_count=n)
    mean_x = sum(x_values, Decimal("0")) / n
    variance = sum(((x - mean_x) ** 2 for x in x_values), Decimal("0")) / (n - 1)
    sample_std = variance.sqrt()
    if sample_std == 0:
        raise _MetricUnavailable(ReasonCode.ZERO_RETURN_STDDEV, sample_count=n)
    return mean_x / sample_std * ANNUALIZATION_FACTOR.sqrt()


def _sharpe_result(
    *,
    run_id: str,
    spec: AnalyzerSpec,
    descriptor: MetricOutputDescriptor,
    series: _EquitySeries,
    x_values: Sequence[Decimal] | None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> MetricResult:
    """Build the sharpe MetricResult, converting formula failures."""

    try:
        if series.has_invalid_points:
            raise _invalid_equity_unavailable(series)
        assert x_values is not None
        with analyzer_decimal_context():
            value = _sharpe_from_x(x_values)
    except _MetricUnavailable as unavailable:
        return MetricResult.unavailable(
            run_id=run_id,
            spec=spec,
            metric_key=descriptor.metric_key,
            formula_version=descriptor.formula_version,
            unit=descriptor.unit,
            sample_count=(
                unavailable.sample_count
                if unavailable.sample_count is not None
                else series.valid_day_count
            ),
            reason_code=unavailable.reason_code,
            extra_metadata={**unavailable.metadata, **dict(extra_metadata or {})},
        )
    return MetricResult.available(
        run_id=run_id,
        spec=spec,
        metric_key=descriptor.metric_key,
        formula_version=descriptor.formula_version,
        value=value,
        unit=descriptor.unit or "ratio",
        sample_count=len(x_values),
        extra_metadata={
            "annualization_factor": format(ANNUALIZATION_FACTOR, "f"),
            "std_ddof": 1,
            **dict(extra_metadata or {}),
        },
    )


# ---------------------------------------------------------------------------
# v1 producers
# ---------------------------------------------------------------------------


def _produce_sharpe_simple(
    state: "_EngineState", spec: AnalyzerSpec
) -> tuple[MetricResult, ...]:
    descriptor = spec.output_contract[0]
    series = _build_equity_series(
        state.equity_observations, state.initial_equity_snapshot.equity_e0
    )
    x_values: list[Decimal] | None = None
    if not series.has_invalid_points and series.valid_day_count > 0:
        x_values = _daily_returns(series)
    return (
        _sharpe_result(
            run_id=state.run_id,
            spec=spec,
            descriptor=descriptor,
            series=series,
            x_values=x_values,
        ),
    )


def _resolve_rate_days(
    state: "_EngineState",
) -> tuple[list[Decimal], list[date]]:
    """Excess-return inputs and missing-rate sessions for Sharpe B.

    ``returns[i]`` is the return of observation ``i`` (against its
    predecessor, with E0 before the first), so rates pair with every
    observed session, not with the tail of the series.
    """

    series = _build_equity_series(
        state.equity_observations, state.initial_equity_snapshot.equity_e0
    )
    if series.has_invalid_points:
        return [], []
    rate_snapshot = state.rate_snapshot
    assert rate_snapshot is not None
    x_values: list[Decimal] = []
    missing: list[date] = []
    returns = _daily_returns(series)
    for observation, simple_return in zip(series.ordered_observations, returns):
        rf = rate_snapshot.rate_for(observation.session_date)
        if rf is None:
            missing.append(observation.session_date)
            continue
        with analyzer_decimal_context():
            x_values.append(simple_return - rf)
    return x_values, missing


def _produce_sharpe_pit_rf(
    state: "_EngineState", spec: AnalyzerSpec
) -> tuple[MetricResult, ...]:
    descriptor = spec.output_contract[0]
    series = _build_equity_series(
        state.equity_observations, state.initial_equity_snapshot.equity_e0
    )
    rate_snapshot = state.rate_snapshot
    extra_metadata: dict[str, Any] = {
        "rate_source_key": (
            rate_snapshot.source_key if rate_snapshot is not None else None
        ),
        "rate_source_version": (
            rate_snapshot.source_version if rate_snapshot is not None else None
        ),
        "rate_snapshot_hash": (
            rate_snapshot.snapshot_hash if rate_snapshot is not None else None
        ),
        "missing_ranges": [
            [start.isoformat(), end.isoformat()]
            for start, end in (
                rate_snapshot.missing_ranges if rate_snapshot is not None else ()
            )
        ],
    }
    x_values: list[Decimal] | None = None
    if not series.has_invalid_points and series.valid_day_count > 0:
        x_values, missing_rates = _resolve_rate_days(state)
        if missing_rates:
            return (
                MetricResult.unavailable(
                    run_id=state.run_id,
                    spec=spec,
                    metric_key=descriptor.metric_key,
                    formula_version=descriptor.formula_version,
                    unit=descriptor.unit,
                    sample_count=series.valid_day_count,
                    reason_code=ReasonCode.MISSING_PIT_RF,
                    extra_metadata={
                        **extra_metadata,
                        "missing_rate_session_dates": [
                            day.isoformat() for day in sorted(missing_rates)
                        ],
                    },
                ),
            )
    return (
        _sharpe_result(
            run_id=state.run_id,
            spec=spec,
            descriptor=descriptor,
            series=series,
            x_values=x_values,
            extra_metadata=extra_metadata,
        ),
    )


def resolve_config_rf_daily(spec: AnalyzerSpec) -> Decimal:
    """Validate the C configuration and return its frozen daily rate.

    Uses the registered ``annual_rate_div_252@1`` convention:
    ``rf_daily = rf_annual / 252`` as plain Decimal division.  ``rf_annual``
    must be a finite Decimal strictly greater than -1; negative rates are
    allowed, rates at or below -100% are not.
    """

    parameters = spec.parameters
    if "rf_annual" not in parameters:
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} requires an explicit rf_annual parameter"
        )
    raw = parameters["rf_annual"]
    if isinstance(raw, bool) or isinstance(raw, float) or not isinstance(
        raw, (Decimal, int, str)
    ):
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} rf_annual must be a Decimal, int, or "
            f"decimal string; got {type(raw).__name__}"
        )
    try:
        rf_annual = Decimal(str(raw))
    except InvalidOperation as exc:
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} rf_annual is not a valid decimal: {raw!r}"
        ) from exc
    if not rf_annual.is_finite():
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} rf_annual must be finite"
        )
    if rf_annual <= Decimal("-1"):
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} rf_annual must be strictly greater "
            f"than -1; got {rf_annual}"
        )
    with analyzer_decimal_context():
        return rf_annual / ANNUALIZATION_FACTOR


def _produce_sharpe_config_rf(
    state: "_EngineState", spec: AnalyzerSpec
) -> tuple[MetricResult, ...]:
    descriptor = spec.output_contract[0]
    series = _build_equity_series(
        state.equity_observations, state.initial_equity_snapshot.equity_e0
    )
    rf_daily = resolve_config_rf_daily(spec)
    extra_metadata = {
        "rf_annual": format(
            Decimal(str(spec.parameters["rf_annual"])), "f"
        ),
        "rf_daily": format(rf_daily, "f"),
        "annual_rate_converter": (
            f"{ANNUAL_RATE_CONVERTER_KEY}@{ANNUAL_RATE_CONVERTER_VERSION}"
        ),
    }
    x_values: list[Decimal] | None = None
    if not series.has_invalid_points and series.valid_day_count > 0:
        with analyzer_decimal_context():
            x_values = [simple_return - rf_daily for simple_return in _daily_returns(series)]
    return (
        _sharpe_result(
            run_id=state.run_id,
            spec=spec,
            descriptor=descriptor,
            series=series,
            x_values=x_values,
            extra_metadata=extra_metadata,
        ),
    )


def _produce_turnover(
    state: "_EngineState", spec: AnalyzerSpec
) -> tuple[MetricResult, ...]:
    descriptor = spec.output_contract[0]
    series = _build_equity_series(
        state.equity_observations, state.initial_equity_snapshot.equity_e0
    )
    gross_traded_notional = sum(
        (fill.fact.gross_traded_notional for fill in state.fill_observations),
        Decimal("0"),
    )
    base_metadata = {
        "gross_traded_notional": format(gross_traded_notional, "f"),
        "fill_count": len(state.fill_observations),
    }
    try:
        if series.has_invalid_points:
            raise _invalid_equity_unavailable(series)
        if series.valid_day_count == 0:
            raise _MetricUnavailable(
                ReasonCode.NO_VALID_END_OF_DAY_EQUITY, sample_count=0
            )
        with analyzer_decimal_context():
            total_valid_equity = sum(
                (
                    observation.equity
                    for observation in series.ordered_observations
                    if observation.is_valid and observation.equity is not None
                ),
                Decimal("0"),
            )
            average_equity = total_valid_equity / series.valid_day_count
            if average_equity <= 0:
                raise _MetricUnavailable(
                    ReasonCode.NON_POSITIVE_AVERAGE_EQUITY,
                    sample_count=series.valid_day_count,
                    metadata={"average_end_of_day_equity": format(average_equity, "f")},
                )
            value = gross_traded_notional / average_equity
    except _MetricUnavailable as unavailable:
        return (
            MetricResult.unavailable(
                run_id=state.run_id,
                spec=spec,
                metric_key=descriptor.metric_key,
                formula_version=descriptor.formula_version,
                unit=descriptor.unit,
                sample_count=(
                    unavailable.sample_count
                    if unavailable.sample_count is not None
                    else series.valid_day_count
                ),
                reason_code=unavailable.reason_code,
                extra_metadata={**unavailable.metadata, **base_metadata},
            ),
        )
    return (
        MetricResult.available(
            run_id=state.run_id,
            spec=spec,
            metric_key=descriptor.metric_key,
            formula_version=descriptor.formula_version,
            value=value,
            unit=descriptor.unit or "ratio",
            sample_count=series.valid_day_count,
            extra_metadata={
                **base_metadata,
                "average_end_of_day_equity": format(average_equity, "f"),
            },
        ),
    )


def _produce_fee_summary(
    state: "_EngineState", spec: AnalyzerSpec
) -> tuple[MetricResult, ...]:
    cumulative_descriptor = spec.output_contract[0]
    ratio_descriptor = spec.output_contract[1]
    fill_count = len(state.fill_observations)
    cumulative_fees = sum(
        (fill.fact.fees for fill in state.fill_observations), Decimal("0")
    )
    gross_traded_notional = sum(
        (fill.fact.gross_traded_notional for fill in state.fill_observations),
        Decimal("0"),
    )
    results = [
        MetricResult.available(
            run_id=state.run_id,
            spec=spec,
            metric_key=cumulative_descriptor.metric_key,
            formula_version=cumulative_descriptor.formula_version,
            value=cumulative_fees,
            unit=state.reporting_currency.lower(),
            sample_count=fill_count,
            extra_metadata={
                "gross_traded_notional": format(gross_traded_notional, "f"),
            },
        )
    ]
    if gross_traded_notional == 0:
        results.append(
            MetricResult.unavailable(
                run_id=state.run_id,
                spec=spec,
                metric_key=ratio_descriptor.metric_key,
                formula_version=ratio_descriptor.formula_version,
                unit=ratio_descriptor.unit,
                sample_count=fill_count,
                reason_code=ReasonCode.ZERO_GROSS_TRADED_NOTIONAL,
                extra_metadata={
                    "gross_traded_notional": format(gross_traded_notional, "f")
                },
            )
        )
    else:
        with analyzer_decimal_context():
            ratio = cumulative_fees / gross_traded_notional
        results.append(
            MetricResult.available(
                run_id=state.run_id,
                spec=spec,
                metric_key=ratio_descriptor.metric_key,
                formula_version=ratio_descriptor.formula_version,
                value=ratio,
                unit=ratio_descriptor.unit or "ratio",
                sample_count=fill_count,
                extra_metadata={
                    "gross_traded_notional": format(gross_traded_notional, "f"),
                    "cumulative_fees": format(cumulative_fees, "f"),
                },
            )
        )
    return tuple(results)


#: Built-in v1 producers keyed by exact (analyzer_key, analyzer_version).
_BUILTIN_PRODUCERS: dict[tuple[str, int], Callable[["_EngineState", AnalyzerSpec], tuple[MetricResult, ...]]] = {
    (SHARPE_SIMPLE_ANALYZER_KEY, 1): _produce_sharpe_simple,
    (PIT_RF_ANALYZER_KEY, 1): _produce_sharpe_pit_rf,
    (CONFIG_RF_ANALYZER_KEY, 1): _produce_sharpe_config_rf,
    (TURNOVER_ANALYZER_KEY, 1): _produce_turnover,
    (FEE_SUMMARY_ANALYZER_KEY, 1): _produce_fee_summary,
}


# ---------------------------------------------------------------------------
# Frozen v1 spec factories used by the ComponentRegistry entries
# ---------------------------------------------------------------------------

_INPUT_CONTRACT_FULL = {
    "initial_equity_snapshot": True,
    "equity_observations": True,
    "applied_fill_facts": True,
}

_INPUT_CONTRACT_NO_RATES = {
    **_INPUT_CONTRACT_FULL,
    "pit_rate_snapshot": False,
}


def build_sharpe_simple_spec(parameters: Mapping[str, Any] | None = None) -> AnalyzerSpec:
    return AnalyzerSpec(
        analyzer_key=SHARPE_SIMPLE_ANALYZER_KEY,
        analyzer_version=1,
        name_zh="简单夏普比率",
        name_en="Simple Sharpe Ratio",
        parameters=parameters,
        input_contract=_INPUT_CONTRACT_NO_RATES,
        output_contract=(
            MetricOutputDescriptor(
                metric_key=METRIC_KEY_SHARPE,
                formula_version=FORMULA_VERSION_SHARPE_SIMPLE,
                unit="ratio",
                sample_count_semantics="valid_daily_return_count_including_zero_return_days",
            ),
        ),
    )


def build_sharpe_pit_rf_spec(parameters: Mapping[str, Any] | None = None) -> AnalyzerSpec:
    return AnalyzerSpec(
        analyzer_key=PIT_RF_ANALYZER_KEY,
        analyzer_version=1,
        name_zh="PIT 无风险利率夏普比率",
        name_en="PIT Risk-Free Sharpe Ratio",
        parameters=parameters,
        input_contract={**_INPUT_CONTRACT_FULL, "pit_rate_snapshot": True},
        output_contract=(
            MetricOutputDescriptor(
                metric_key=METRIC_KEY_SHARPE,
                formula_version=FORMULA_VERSION_SHARPE_PIT_RF,
                unit="ratio",
                sample_count_semantics="valid_daily_excess_return_count_including_zero_return_days",
            ),
        ),
    )


def build_sharpe_config_rf_spec(parameters: Mapping[str, Any] | None = None) -> AnalyzerSpec:
    return AnalyzerSpec(
        analyzer_key=CONFIG_RF_ANALYZER_KEY,
        analyzer_version=1,
        name_zh="配置无风险利率夏普比率",
        name_en="Configured Risk-Free Sharpe Ratio",
        parameters=parameters,
        input_contract={**_INPUT_CONTRACT_NO_RATES, "rf_annual": True},
        output_contract=(
            MetricOutputDescriptor(
                metric_key=METRIC_KEY_SHARPE,
                formula_version=FORMULA_VERSION_SHARPE_CONFIG_RF,
                unit="ratio",
                sample_count_semantics="valid_daily_excess_return_count_including_zero_return_days",
            ),
        ),
    )


def build_turnover_spec(parameters: Mapping[str, Any] | None = None) -> AnalyzerSpec:
    return AnalyzerSpec(
        analyzer_key=TURNOVER_ANALYZER_KEY,
        analyzer_version=1,
        name_zh="换手率",
        name_en="Turnover",
        parameters=parameters,
        input_contract=_INPUT_CONTRACT_NO_RATES,
        output_contract=(
            MetricOutputDescriptor(
                metric_key=METRIC_KEY_TURNOVER,
                formula_version=FORMULA_VERSION_TURNOVER,
                unit="ratio",
                sample_count_semantics="valid_end_of_day_equity_count",
            ),
        ),
    )


def build_fee_summary_spec(parameters: Mapping[str, Any] | None = None) -> AnalyzerSpec:
    return AnalyzerSpec(
        analyzer_key=FEE_SUMMARY_ANALYZER_KEY,
        analyzer_version=1,
        name_zh="费用摘要",
        name_en="Fee Summary",
        parameters=parameters,
        input_contract={**_INPUT_CONTRACT_NO_RATES, "initial_equity_snapshot": False},
        output_contract=(
            MetricOutputDescriptor(
                metric_key=METRIC_KEY_CUMULATIVE_FEES,
                formula_version=FORMULA_VERSION_CUMULATIVE_FEES,
                unit="currency",
                sample_count_semantics="applied_fill_count",
            ),
            MetricOutputDescriptor(
                metric_key=METRIC_KEY_FEE_TO_GROSS_TRADED_NOTIONAL,
                formula_version=FORMULA_VERSION_FEE_TO_GROSS,
                unit="ratio",
                sample_count_semantics="applied_fill_count",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Engine state and snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EngineState:
    """Immutable view over everything the producers may read."""

    run_id: str
    reporting_currency: str
    initial_equity_snapshot: InitialEquitySnapshot
    equity_observations: tuple[EquityObservation, ...]
    fill_observations: tuple[FillObservation, ...]
    rate_snapshot: PitRateSnapshot | None
    specs: tuple[AnalyzerSpec, ...]

    def input_evidence_signature(self) -> str:
        return compute_input_evidence_signature(
            initial_equity_snapshot=self.initial_equity_snapshot,
            equity_observations=self.equity_observations,
            fill_facts=self.fill_observations,
            rate_snapshot_hash=(
                self.rate_snapshot.snapshot_hash
                if self.rate_snapshot is not None
                else None
            ),
        )

    def formula_signature(self) -> str:
        payload = {
            "kind": "formula_signature_v1",
            "specs": [spec.describe() for spec in self.specs],
            "annualization_factor": format(ANNUALIZATION_FACTOR, "f"),
            "decimal_policy": {
                "precision": ANALYSIS_DECIMAL_PRECISION,
                "rounding": "ROUND_HALF_EVEN",
                "persistence_scale": ANALYSIS_PERSISTENCE_SCALE,
            },
            "annual_rate_converter": (
                f"{ANNUAL_RATE_CONVERTER_KEY}@{ANNUAL_RATE_CONVERTER_VERSION}"
            ),
        }
        rendered = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return (
            f"{ANALYSIS_EVIDENCE_HASH_ALGORITHM}:"
            f"{hashlib.sha256(rendered).hexdigest()}"
        )

    def compute_results(self) -> tuple[MetricResult, ...]:
        results: list[MetricResult] = []
        for spec in self.specs:
            producer = _BUILTIN_PRODUCERS[(spec.analyzer_key, spec.analyzer_version)]
            with analyzer_decimal_context():
                results.extend(producer(self, spec))
        return tuple(results)

    def summary_counts(self) -> dict[str, Any]:
        series = _build_equity_series(
            self.equity_observations, self.initial_equity_snapshot.equity_e0
        )
        gross_traded_notional = sum(
            (fill.fact.gross_traded_notional for fill in self.fill_observations),
            Decimal("0"),
        )
        cumulative_fees = sum(
            (fill.fact.fees for fill in self.fill_observations), Decimal("0")
        )
        return {
            "initial_equity": self.initial_equity_snapshot.equity_e0,
            "valid_day_count": series.valid_day_count,
            "fill_count": len(self.fill_observations),
            "gross_traded_notional": gross_traded_notional,
            "cumulative_fees": cumulative_fees,
        }


@dataclass(frozen=True, slots=True)
class AnalysisSnapshot:
    """Read-only mid-run view of the analyzer engine state."""

    run_id: str
    status: AnalysisStatus | str
    reporting_currency: str
    initial_equity_snapshot: InitialEquitySnapshot
    equity_observations: tuple[EquityObservation, ...]
    fill_observations: tuple[FillObservation, ...]
    rate_snapshot: PitRateSnapshot | None
    specs: tuple[AnalyzerSpec, ...]
    failure: Mapping[str, Any] | None = None

    @property
    def valid_day_count(self) -> int:
        return _build_equity_series(
            self.equity_observations, self.initial_equity_snapshot.equity_e0
        ).valid_day_count

    @property
    def fill_count(self) -> int:
        return len(self.fill_observations)

    def input_evidence_signature(self) -> str:
        return _as_state(self).input_evidence_signature()

    def formula_signature(self) -> str:
        return _as_state(self).formula_signature()

    def summary_counts(self) -> dict[str, Any]:
        return _as_state(self).summary_counts()

    def compute_provisional_results(self) -> tuple[MetricResult, ...]:
        """Deterministic metrics computed over the facts observed so far.

        Provisional results share the exact formulas of final ones but are
        never persisted into ``backtest_metrics``; only the run summary's
        progress fields may reflect them.
        """

        return _as_state(self).compute_results()


def _as_state(snapshot: AnalysisSnapshot) -> _EngineState:
    return _EngineState(
        run_id=snapshot.run_id,
        reporting_currency=snapshot.reporting_currency,
        initial_equity_snapshot=snapshot.initial_equity_snapshot,
        equity_observations=snapshot.equity_observations,
        fill_observations=snapshot.fill_observations,
        rate_snapshot=snapshot.rate_snapshot,
        specs=snapshot.specs,
    )


# ---------------------------------------------------------------------------
# Analyzer engine
# ---------------------------------------------------------------------------


class AnalyzerEngine:
    """Accumulates determined facts and produces metric results.

    The engine never estimates prices, recomputes fees, converts currency,
    or infers external cash flows.  Chunk boundaries transfer only
    immutable snapshots and accumulated state; identical inputs produce
    identical results regardless of chunking.
    """

    def __init__(
        self,
        *,
        initial_equity_snapshot: InitialEquitySnapshot,
        analyzer_specs: Sequence[AnalyzerSpec],
        frozen_rate_snapshot: PitRateSnapshot | None = None,
        accounting_currency: str | None = None,
    ) -> None:
        if not isinstance(initial_equity_snapshot, InitialEquitySnapshot):
            raise AnalyzerInputError(
                "initial_equity_snapshot must be an InitialEquitySnapshot"
            )
        specs = tuple(analyzer_specs)
        if not specs:
            raise AnalyzerConfigurationError(
                "at least one AnalyzerSpec must be configured for a run"
            )
        seen_producers: dict[tuple[str, str], tuple[str, int]] = {}
        for spec in specs:
            if not isinstance(spec, AnalyzerSpec):
                raise AnalyzerConfigurationError(
                    "analyzer_specs entries must be AnalyzerSpec instances"
                )
            if (spec.analyzer_key, spec.analyzer_version) not in _BUILTIN_PRODUCERS:
                raise AnalyzerConfigurationError(
                    f"no analyzer implementation is registered for "
                    f"{spec.display_identity}; unknown versions never fall back"
                )
            for descriptor in spec.output_contract:
                logical_key = (descriptor.metric_key, descriptor.formula_version)
                existing = seen_producers.get(logical_key)
                if existing is not None and existing != (
                    spec.analyzer_key,
                    spec.analyzer_version,
                ):
                    raise AnalyzerConfigurationError(
                        f"metric {logical_key[0]}@{logical_key[1]} is already "
                        f"produced by {existing[0]}@{existing[1]}; one run "
                        "allows exactly one analyzer producer per logical key"
                    )
                seen_producers[logical_key] = (spec.analyzer_key, spec.analyzer_version)
        reporting_currency = initial_equity_snapshot.reporting_currency
        if accounting_currency is not None:
            normalized = accounting_currency.strip().upper()
            if normalized != reporting_currency:
                raise AnalyzerConfigurationError(
                    f"reporting currency {reporting_currency} does not equal "
                    f"the accounting policy currency {normalized}"
                )
        needs_rate_snapshot = any(
            spec.analyzer_key == PIT_RF_ANALYZER_KEY for spec in specs
        )
        if needs_rate_snapshot and frozen_rate_snapshot is None:
            raise AnalyzerConfigurationError(
                f"{PIT_RF_ANALYZER_KEY}@1 requires a pre-fetched frozen PIT "
                "rate snapshot covering the whole formal window"
            )
        if frozen_rate_snapshot is not None:
            covered_start = frozen_rate_snapshot.coverage_start
            first_formal = initial_equity_snapshot.session_date
            if covered_start is not None and covered_start > first_formal:
                raise AnalyzerConfigurationError(
                    "the frozen PIT rate snapshot starts after the first "
                    "formal session; it must cover the entire window"
                )
        self._initial_equity_snapshot = initial_equity_snapshot
        self._specs = specs
        self._rate_snapshot = frozen_rate_snapshot
        self._run_id = initial_equity_snapshot.run_id
        self._reporting_currency = reporting_currency
        # Admission eagerly resolves C configurations so an invalid
        # rf_annual fails run creation before any trading happens.
        for spec in specs:
            if spec.analyzer_key == CONFIG_RF_ANALYZER_KEY:
                resolve_config_rf_daily(spec)
        self._equity_observations: list[EquityObservation] = []
        self._fills: dict[UUID, FillObservation] = {}
        self._finalized_status: AnalysisStatus | None = None
        self._failure_payload: Mapping[str, Any] | None = None
        self._final_results: tuple[MetricResult, ...] | None = None

    # -- construction -----------------------------------------------------

    @classmethod
    def create(
        cls,
        initial_equity_snapshot: InitialEquitySnapshot,
        analyzer_specs: Sequence[AnalyzerSpec],
        frozen_rate_snapshot: PitRateSnapshot | None = None,
        decimal_policy: Mapping[str, Any] | None = None,
        accounting_currency: str | None = None,
    ) -> "AnalyzerEngine":
        """Admission entry point matching the documented creation order.

        ``decimal_policy`` is accepted for signature completeness: only the
        frozen ``prec=50``/``ROUND_HALF_EVEN`` policy is supported, so any
        other declaration is rejected instead of silently ignored.
        """

        if decimal_policy is not None:
            requested = dict(decimal_policy)
            expected = {
                "precision": ANALYSIS_DECIMAL_PRECISION,
                "rounding": "ROUND_HALF_EVEN",
            }
            if requested != expected:
                raise AnalyzerConfigurationError(
                    f"unsupported decimal policy {requested}; the analyzer "
                    f"subsystem freezes {expected}"
                )
        return cls(
            initial_equity_snapshot=initial_equity_snapshot,
            analyzer_specs=analyzer_specs,
            frozen_rate_snapshot=frozen_rate_snapshot,
            accounting_currency=accounting_currency,
        )

    # -- identity ----------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def reporting_currency(self) -> str:
        return self._reporting_currency

    @property
    def specs(self) -> tuple[AnalyzerSpec, ...]:
        return self._specs

    @property
    def finalized_status(self) -> AnalysisStatus | None:
        return self._finalized_status

    def formula_signature(self) -> str:
        return self._state().formula_signature()

    def input_evidence_signature(self) -> str:
        return self._state().input_evidence_signature()

    # -- observation intake -------------------------------------------------

    def observe_equity(self, observation: EquityObservation) -> None:
        """Record one end-of-day equity fact in strict session order."""

        self._require_open()
        if not isinstance(observation, EquityObservation):
            raise AnalyzerInputError(
                "observe_equity expects an EquityObservation"
            )
        self._require_same_run(observation.run_id)
        if observation.reporting_currency != self._reporting_currency:
            raise AnalyzerInputError(
                f"equity observation reports {observation.reporting_currency} "
                f"but the engine aggregates {self._reporting_currency}"
            )
        if self._equity_observations:
            last_date = self._equity_observations[-1].session_date
            if observation.session_date == last_date:
                raise AnalyzerInputError(
                    f"a duplicate equity observation exists for session "
                    f"{observation.session_date.isoformat()}"
                )
            if observation.session_date < last_date:
                raise AnalyzerInputError(
                    f"equity observations must strictly increase by session: "
                    f"{observation.session_date.isoformat()} follows "
                    f"{last_date.isoformat()}"
                )
        elif observation.session_date < self._initial_equity_snapshot.session_date:
            raise AnalyzerInputError(
                "the first equity observation precedes the first formal "
                "session of the run"
            )
        self._equity_observations.append(observation)

    def observe_fill(self, observation: FillObservation) -> None:
        """Record one applied fill fact, deduplicated by stable fill id."""

        self._require_open()
        if not isinstance(observation, FillObservation):
            raise AnalyzerInputError("observe_fill expects a FillObservation")
        self._require_same_run(observation.fact.run_id)
        if observation.fact.reporting_currency != self._reporting_currency:
            raise AnalyzerInputError(
                f"fill fact reports {observation.fact.reporting_currency} but "
                f"the engine aggregates {self._reporting_currency}"
            )
        fill_id = observation.fact.fill_id
        existing = self._fills.get(fill_id)
        if existing is not None:
            if existing.content_identity != observation.content_identity:
                raise AnalyzerInputError(
                    f"fill {fill_id} was submitted twice with different "
                    "content; applied fill facts are immutable"
                )
            return
        self._fills[fill_id] = observation

    # -- snapshots -----------------------------------------------------------

    def snapshot(self) -> AnalysisSnapshot:
        """Return the current read-only partial snapshot."""

        status = self._finalized_status or AnalysisStatus.PARTIAL
        return AnalysisSnapshot(
            run_id=self._run_id,
            status=status,
            reporting_currency=self._reporting_currency,
            initial_equity_snapshot=self._initial_equity_snapshot,
            equity_observations=tuple(self._equity_observations),
            fill_observations=tuple(self._fills.values()),
            rate_snapshot=self._rate_snapshot,
            specs=self._specs,
            failure=self._failure_payload,
        )

    # -- finalization ----------------------------------------------------------

    def finalize(
        self,
        status: AnalysisStatus | str = AnalysisStatus.FINAL,
        *,
        failure: Mapping[str, Any] | None = None,
    ) -> tuple[MetricResult, ...]:
        """Produce the definitive metric results exactly once.

        Both ``final`` and ``aborted`` are terminal states; a repeated call
        raises instead of silently recomputing, and partial snapshots can
        never be mistaken for finalized output.
        """

        try:
            requested = AnalysisStatus(getattr(status, "value", status))
        except ValueError as exc:
            raise DomainValidationError(
                "finalize status must be final or aborted"
            ) from exc
        if requested is AnalysisStatus.PARTIAL:
            raise DomainValidationError(
                "finalize cannot produce a partial result; use snapshot()"
            )
        if self._finalized_status is not None:
            raise AnalysisStateConflictError(
                f"this engine was already finalized as "
                f"{self._finalized_status.value}; finalize runs exactly once"
            )
        if failure is not None and requested is not AnalysisStatus.ABORTED:
            raise DomainValidationError(
                "only aborted finalizations may carry a failure payload"
            )
        if requested is AnalysisStatus.ABORTED and (failure is None or not failure.get("abort_reason")):
            raise DomainValidationError(
                "aborted finalization requires an abort_reason in its "
                "failure payload"
            )
        state = self._state()
        with analyzer_decimal_context():
            results = state.compute_results()
        self._final_results = results
        self._finalized_status = requested
        if failure is not None:
            self._failure_payload = MappingProxyType(dict(failure))
        return tuple(results)

    @property
    def final_results(self) -> tuple[MetricResult, ...] | None:
        return self._final_results

    # -- internals ---------------------------------------------------------

    def _require_open(self) -> None:
        if self._finalized_status is not None:
            raise AnalysisStateConflictError(
                "this engine is finalized and accepts no further facts"
            )

    def _require_same_run(self, run_id: str) -> None:
        if run_id != self._run_id:
            raise AnalyzerInputError(
                f"fact belongs to run {run_id!r} but the engine aggregates "
                f"run {self._run_id!r}"
            )

    def _state(self) -> _EngineState:
        return _EngineState(
            run_id=self._run_id,
            reporting_currency=self._reporting_currency,
            initial_equity_snapshot=self._initial_equity_snapshot,
            equity_observations=tuple(self._equity_observations),
            fill_observations=tuple(self._fills.values()),
            rate_snapshot=self._rate_snapshot,
            specs=self._specs,
        )
