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
import inspect
import json
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import (
    Context,
    Decimal,
    DecimalException,
    InvalidOperation,
    ROUND_HALF_EVEN,
    localcontext,
)
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
    FormalSessionTimeline,
    InitialEquitySnapshot,
    PitRateSnapshot,
    _frozen_mapping,
    canonical_evidence_json,
    compute_formal_timeline_hash,
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
    "ADMISSION_REASON_CODES",
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
    "METRIC_UNAVAILABLE_REASON_CODES",
    "PIT_RF_ANALYZER_KEY",
    "ReasonCode",
    "SHARPE_REASON_PRECEDENCE",
    "SHARPE_SIMPLE_ANALYZER_KEY",
    "CONFIG_RF_ANALYZER_KEY",
    "TURNOVER_ANALYZER_KEY",
    "analyzer_decimal_context",
    "compute_terminal_fingerprint",
    "TERMINAL_FINGERPRINT_CONTRACT_VERSION",
    "frozen_output_contract_for",
    "validate_v1_analyzer_spec",
]


# Terminal retry identity is persisted on ``backtest_analysis_summaries``.
# Its canonical payload is defined by the shared helper below; these fields
# are the complete, order-stable identity rather than an implementation hint.
TERMINAL_FINGERPRINT_CONTRACT_VERSION = "analysis_terminal_v2"
TERMINAL_FINGERPRINT_STATUS_RULE = (
    "same status + same fingerprint => idempotent; all other terminal or "
    "terminal-to-partial transitions => conflict"
)


# Admission trust is established only by the coordinator call path below.
# No capability getter is exposed from this module: a direct engine caller
# cannot obtain a token and invoke the attestation method manually.
_ADMITTED_ENGINE_TOKENS: weakref.WeakKeyDictionary[object, object] = (
    weakref.WeakKeyDictionary()
)
_ADMITTED_CAPABILITY_TOKENS: weakref.WeakSet[object] = weakref.WeakSet()
_ADMISSION_TOKEN_BINDINGS: weakref.WeakKeyDictionary[
    object, dict[str, Any]
] = weakref.WeakKeyDictionary()


class _AdmissionToken:
    """Weak-referenceable opaque token retained by admitted objects."""

    __slots__ = ("__weakref__",)


def _is_coordinator_admitted(engine: object, capability_token: object) -> bool:
    """Validate admission using the coordinator registry, not engine flags."""

    return _ADMITTED_ENGINE_TOKENS.get(engine) is capability_token


def _is_admission_token_valid(
    capability_token: object,
    *,
    run_id: str | None = None,
    initial_equity_hash: str | None = None,
    rate_snapshot_hash: str | None = None,
    analysis_snapshot: object | None = None,
    failure_snapshot_binding: str | None = None,
    failure_envelope: Mapping[str, Any] | None = None,
    terminal_fingerprint: str | None = None,
    require_failure_binding: bool = True,
    require_snapshot_binding: bool = False,
) -> bool:
    """Validate admission identity and an optional runtime-bound snapshot."""

    # WeakSet/WeakKeyDictionary membership requires a hashable,
    # weak-reference-compatible key.  This is an untrusted protocol boundary:
    # malformed callers must receive a normal validation failure rather than
    # leaking TypeError from the container implementation.
    try:
        if capability_token not in _ADMITTED_CAPABILITY_TOKENS:
            return False
        binding = _ADMISSION_TOKEN_BINDINGS.get(capability_token)
    except (TypeError, ValueError):
        return False
    if binding is None:
        return False
    if run_id is not None and binding.get("run_id") != run_id:
        return False
    if (
        initial_equity_hash is not None
        and binding.get("initial_equity_hash") != initial_equity_hash
    ):
        return False
    if (
        rate_snapshot_hash is not None
        and binding.get("rate_snapshot_hash") != rate_snapshot_hash
    ):
        return False
    if (
        terminal_fingerprint is not None
        and binding.get("terminal_fingerprint") != terminal_fingerprint
    ):
        return False
    if analysis_snapshot is not None:
        # A frozen binding is meaningful only for the DTO graph produced by
        # this module.  dataclasses.replace can otherwise inject a look-alike
        # rate snapshot/spec/observation object that happens to expose the
        # attributes used by the hash calculation.
        try:
            if not isinstance(analysis_snapshot, AnalysisSnapshot):
                return False
            if not isinstance(
                getattr(analysis_snapshot, "initial_equity_snapshot", None),
                InitialEquitySnapshot,
            ):
                return False
            if not isinstance(
                getattr(analysis_snapshot, "formal_timeline", None),
                FormalSessionTimeline,
            ):
                return False
            rate_candidate = getattr(analysis_snapshot, "rate_snapshot", None)
            if rate_candidate is not None and not isinstance(
                rate_candidate, PitRateSnapshot
            ):
                return False
            if not isinstance(
                getattr(analysis_snapshot, "registry_snapshot", None), Mapping
            ):
                return False
            if any(
                not isinstance(spec, AnalyzerSpec)
                for spec in getattr(analysis_snapshot, "specs", ())
            ):
                return False
            if any(
                not isinstance(observation, EquityObservation)
                for observation in getattr(
                    analysis_snapshot, "equity_observations", ()
                )
            ):
                return False
            if any(
                not isinstance(observation, FillObservation)
                for observation in getattr(
                    analysis_snapshot, "fill_observations", ()
                )
            ):
                return False
        except Exception:
            return False
        rate_snapshot = getattr(analysis_snapshot, "rate_snapshot", None)
        timeline = getattr(analysis_snapshot, "formal_timeline", None)
        try:
            actual_identity = {
                "run_id": getattr(analysis_snapshot, "run_id", None),
                "reporting_currency": getattr(
                    analysis_snapshot, "reporting_currency", None
                ),
                "initial_equity_hash": getattr(
                    getattr(analysis_snapshot, "initial_equity_snapshot", None),
                    "evidence_hash",
                    None,
                ),
                "rate_snapshot_hash": getattr(rate_snapshot, "snapshot_hash", None),
                "formula_signature": analysis_snapshot.formula_signature(),
                "specs": [spec.describe() for spec in analysis_snapshot.specs],
                "registry_snapshot": analysis_snapshot.registry_snapshot,
                "formal_timeline": timeline.as_payload(),
            }
            if canonical_evidence_json(actual_identity) != canonical_evidence_json(
                binding.get("admission_identity")
            ):
                return False
            actual_failure_binding = _compute_failure_snapshot_binding(
                analysis_snapshot,
                failure_envelope=failure_envelope,
            )
        except Exception:
            return False
        if require_failure_binding:
            recorded_failure_binding = binding.get("failure_snapshot_binding")
            if (
                recorded_failure_binding is None
                or recorded_failure_binding != actual_failure_binding
            ):
                return False
            if (
                failure_snapshot_binding is not None
                and failure_snapshot_binding != actual_failure_binding
            ):
                return False
        if require_snapshot_binding:
            try:
                recorded_snapshot_binding = binding.get("partial_snapshot_binding")
                actual_snapshot_binding = _compute_analysis_snapshot_binding(
                    analysis_snapshot
                )
            except Exception:
                return False
            if (
                recorded_snapshot_binding is None
                or recorded_snapshot_binding != actual_snapshot_binding
                or getattr(analysis_snapshot, "snapshot_binding", None)
                != actual_snapshot_binding
            ):
                return False
    return True


def _compute_analysis_snapshot_binding(analysis_snapshot: object) -> str:
    """Hash a complete immutable partial snapshot for checkpoint writes."""

    payload = {
        "kind": "analysis_partial_snapshot_binding_v1",
        "run_id": analysis_snapshot.run_id,
        "reporting_currency": analysis_snapshot.reporting_currency,
        "status": getattr(analysis_snapshot.status, "value", analysis_snapshot.status),
        "initial_equity_snapshot": (
            analysis_snapshot.initial_equity_snapshot.evidence_payload()
        ),
        "formal_timeline": analysis_snapshot.formal_timeline.as_payload(),
        "specs": [spec.describe() for spec in analysis_snapshot.specs],
        "registry_snapshot": analysis_snapshot.registry_snapshot,
        "rate_snapshot_hash": getattr(
            analysis_snapshot.rate_snapshot,
            "snapshot_hash",
            None,
        ),
        "equity_observations": [
            observation.evidence_payload()
            for observation in analysis_snapshot.equity_observations
        ],
        "fill_observations": [
            observation.evidence_payload()
            for observation in analysis_snapshot.fill_observations
        ],
        "formula_signature": analysis_snapshot.formula_signature(),
        "input_evidence_signature": analysis_snapshot.input_evidence_signature(),
        "summary_counts": analysis_snapshot.summary_counts(),
    }
    rendered = canonical_evidence_json(payload).encode("utf-8")
    return (
        f"{ANALYSIS_EVIDENCE_HASH_ALGORITHM}:"
        f"{hashlib.sha256(rendered).hexdigest()}"
    )


def _compute_failure_snapshot_binding(
    analysis_snapshot: object,
    *,
    failure_envelope: Mapping[str, Any] | None = None,
) -> str:
    """Hash the complete immutable state handed to aborted finalization."""

    payload = {
        "kind": "analysis_failure_snapshot_binding_v1",
        "run_id": analysis_snapshot.run_id,
        "reporting_currency": analysis_snapshot.reporting_currency,
        "initial_equity_snapshot": (
            analysis_snapshot.initial_equity_snapshot.evidence_payload()
        ),
        "formal_timeline": analysis_snapshot.formal_timeline.as_payload(),
        "specs": [spec.describe() for spec in analysis_snapshot.specs],
        "registry_snapshot": analysis_snapshot.registry_snapshot,
        "rate_snapshot_hash": getattr(
            analysis_snapshot.rate_snapshot,
            "snapshot_hash",
            None,
        ),
        "formula_signature": analysis_snapshot.formula_signature(),
        "input_evidence_signature": analysis_snapshot.input_evidence_signature(),
        "summary_counts": analysis_snapshot.summary_counts(),
        # Failure envelope fields are bound separately from the analysis
        # lifecycle status, which changes from partial to aborted on the
        # first coordinator attempt and must not break idempotent replay.
        "failure_envelope": failure_envelope,
    }
    rendered = canonical_evidence_json(payload).encode("utf-8")
    return (
        f"{ANALYSIS_EVIDENCE_HASH_ALGORITHM}:"
        f"{hashlib.sha256(rendered).hexdigest()}"
    )


def _bind_failure_snapshot(
    capability_token: object,
    analysis_snapshot: object,
    failure_envelope: Mapping[str, Any],
) -> str:
    """Bind one runtime-produced failure snapshot to its admission token."""

    caller = inspect.currentframe()
    caller = caller.f_back if caller is not None else None
    if caller is None or caller.f_globals.get("__name__") != "app.backtesting.runtime":
        raise DomainValidationError(
            "only the deterministic runtime may bind a failure snapshot"
        )
    try:
        token_is_admitted = capability_token in _ADMITTED_CAPABILITY_TOKENS
        binding = _ADMISSION_TOKEN_BINDINGS.get(capability_token)
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(
            "failure snapshot has no valid admission token"
        ) from exc
    if not token_is_admitted:
        raise DomainValidationError("failure snapshot has no valid admission token")
    if binding is None:
        raise DomainValidationError("failure snapshot has no admission binding")
    if not _is_admission_token_valid(
        capability_token,
        analysis_snapshot=analysis_snapshot,
        require_failure_binding=False,
    ):
        raise DomainValidationError(
            "failure snapshot differs from its frozen admission identity"
        )
    snapshot_binding = _compute_failure_snapshot_binding(
        analysis_snapshot,
        failure_envelope=failure_envelope,
    )
    existing = binding.get("failure_snapshot_binding")
    if existing is not None and existing != snapshot_binding:
        raise DomainValidationError(
            "admission token is already bound to different failure evidence"
        )
    existing_envelope = binding.get("failure_envelope")
    normalized_envelope = json.loads(canonical_evidence_json(failure_envelope))
    if existing_envelope is not None and existing_envelope != normalized_envelope:
        raise DomainValidationError(
            "admission token is already bound to different failure envelope"
        )
    binding["failure_envelope"] = normalized_envelope
    binding["failure_snapshot_binding"] = snapshot_binding
    return snapshot_binding


def _bind_failure_terminal_fingerprint(
    capability_token: object,
    terminal_fingerprint: str,
) -> None:
    """Bind the runtime-computed terminal identity to the admission token."""

    caller = inspect.currentframe()
    caller = caller.f_back if caller is not None else None
    if caller is None or caller.f_globals.get("__name__") != "app.backtesting.runtime":
        raise DomainValidationError(
            "only the deterministic runtime may bind a terminal fingerprint"
        )
    if not isinstance(terminal_fingerprint, str) or not terminal_fingerprint.strip():
        raise DomainValidationError("terminal_fingerprint must be non-blank text")
    try:
        binding = _ADMISSION_TOKEN_BINDINGS.get(capability_token)
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(
            "terminal fingerprint has no admission binding"
        ) from exc
    if binding is None:
        raise DomainValidationError("terminal fingerprint has no admission binding")
    existing = binding.get("terminal_fingerprint")
    if existing is not None and existing != terminal_fingerprint:
        raise DomainValidationError(
            "admission token is already bound to a different terminal fingerprint"
        )
    binding["terminal_fingerprint"] = terminal_fingerprint


# ---------------------------------------------------------------------------
# Frozen identities and numeric policy
# ---------------------------------------------------------------------------

ANALYSIS_DECIMAL_PRECISION = 50
ANALYSIS_PERSISTENCE_SCALE = 18
ANALYSIS_PERSISTENCE_ABS_LIMIT = Decimal("1e20")
ANALYSIS_PERSISTENCE_QUANTUM = Decimal("0.000000000000000001")
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
    """Quantize one result and enforce the ``NUMERIC(38, 18)`` range."""

    try:
        quantized = value.quantize(
            ANALYSIS_PERSISTENCE_QUANTUM,
            rounding=ROUND_HALF_EVEN,
            context=Context(
                prec=ANALYSIS_DECIMAL_PRECISION,
                rounding=ROUND_HALF_EVEN,
            ),
        )
    except DecimalException as exc:
        raise DomainValidationError(
            "value cannot be represented as NUMERIC(38,18)"
        ) from exc
    # NUMERIC(38,18) has exactly twenty integer digits.  Check after
    # quantization because rounding can carry a boundary value to 1e20.
    if (
        not quantized.is_finite()
        or quantized.copy_abs() >= ANALYSIS_PERSISTENCE_ABS_LIMIT
    ):
        raise DomainValidationError(
            "value cannot be represented as NUMERIC(38,18)"
        )
    return quantized


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
        ReasonCode.ZERO_GROSS_TRADED_NOTIONAL.value: "毛成交额为 0，费用占比无分母",
        ReasonCode.UNMODELED_EXTERNAL_CASH_FLOW.value: "现金变动无法归类为已建模的现金流类型",
        ReasonCode.INVALID_ANALYZER_CONFIG.value: "分析器或利率运行配置不完整或非法",
    }
)

# If several conditions coexist, Sharpe chooses the first applicable reason
# in this order.  ``sample_count`` always remains the count of positive,
# valid EOD return candidates observed before applying the chosen reason.
SHARPE_REASON_PRECEDENCE = (
    ReasonCode.INVALID_EQUITY,
    ReasonCode.MISSING_PIT_RF,
    ReasonCode.INSUFFICIENT_RETURNS,
    ReasonCode.ZERO_RETURN_STDDEV,
)

# Creation-gate reasons never describe an unavailable metric.  Keeping the
# two vocabularies separate prevents a blocked run from being fabricated as a
# normal ``MetricResult.unavailable`` row.
ADMISSION_REASON_CODES = frozenset(
    {
        ReasonCode.MISSING_INITIAL_MARK.value,
        ReasonCode.NON_POSITIVE_INITIAL_EQUITY.value,
        ReasonCode.INVALID_ANALYZER_CONFIG.value,
        ReasonCode.UNMODELED_EXTERNAL_CASH_FLOW.value,
    }
)
METRIC_UNAVAILABLE_REASON_CODES = frozenset(
    {
        code.value
        for code in ReasonCode
        if code.value not in ADMISSION_REASON_CODES
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
        if not isinstance(code, str):
            raise DomainValidationError(
                "reason_code must be a v1 metric unavailable reason string"
            )
        if code not in METRIC_UNAVAILABLE_REASON_CODES:
            raise DomainValidationError(
                f"{code} is not a v1 metric unavailable reason"
            )
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
        for name in ("run_id", "metric_key", "formula_version", "analyzer_key"):
            text = getattr(self, name)
            if not isinstance(text, str) or not text.strip():
                raise DomainValidationError(f"{name} must be non-blank text")
            object.__setattr__(self, name, text.strip())
        limits = {"metric_key": 100, "formula_version": 64, "analyzer_key": 100}
        for name, maximum in limits.items():
            if len(getattr(self, name)) > maximum:
                raise DomainValidationError(f"{name} must not exceed {maximum} characters")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise DomainValidationError("unit must be non-blank text")
        if len(self.unit.strip()) > 32:
            raise DomainValidationError("unit must not exceed 32 characters")
        object.__setattr__(self, "unit", self.unit.strip())
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count < 0
        ):
            raise DomainValidationError(
                "sample_count must be a non-negative integer"
            )
        if (
            isinstance(self.analyzer_version, bool)
            or not isinstance(self.analyzer_version, int)
            or self.analyzer_version <= 0
        ):
            raise DomainValidationError("analyzer_version must be a positive integer")
        try:
            status = MetricStatus(getattr(self.status, "value", self.status))
        except (TypeError, ValueError) as exc:
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
            except (InvalidOperation, ValueError) as exc:
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
                _frozen_mapping(metadata, "analyzer_metadata"),
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
            reason_code = self.analyzer_metadata.get("reason_code")
            reason_code = getattr(reason_code, "value", reason_code)
            if not isinstance(reason_code, str):
                raise DomainValidationError(
                    "unavailable metrics must use a string reason_code"
                )
            if reason_code not in METRIC_UNAVAILABLE_REASON_CODES:
                raise DomainValidationError(
                    "unavailable metrics must use a v1 metric reason_code"
                )
            object.__setattr__(self, "unavailable_reason", reason.strip())
        else:
            if self.unavailable_reason is not None:
                raise DomainValidationError(
                    "available metrics cannot carry an unavailable_reason"
                )
            if "reason_code" in self.analyzer_metadata:
                raise DomainValidationError(
                    "available metrics cannot carry reason_code metadata"
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
        if not isinstance(spec, AnalyzerSpec):
            raise DomainValidationError("spec must be an AnalyzerSpec")
        if extra_metadata is not None and not isinstance(extra_metadata, Mapping):
            raise DomainValidationError("extra_metadata must be a mapping when provided")
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
            analyzer_metadata=dict(extra_metadata) if extra_metadata is not None else {},
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
        if not isinstance(spec, AnalyzerSpec):
            raise DomainValidationError("spec must be an AnalyzerSpec")
        if extra_metadata is not None and not isinstance(extra_metadata, Mapping):
            raise DomainValidationError("extra_metadata must be a mapping when provided")
        code = getattr(reason_code, "value", reason_code)
        if not isinstance(code, str):
            raise DomainValidationError(
                "reason_code must be a v1 metric unavailable reason string"
            )
        if code not in METRIC_UNAVAILABLE_REASON_CODES:
            if code in ADMISSION_REASON_CODES:
                detail = (
                    f"{code} is a creation admission reason and cannot be used "
                    "as a metric unavailable reason"
                )
            else:
                detail = f"{code} is not a v1 metric unavailable reason"
            raise DomainValidationError(
                detail
            )
        metadata = dict(extra_metadata) if extra_metadata is not None else {}
        if "reason_code" in metadata:
            raise DomainValidationError(
                "extra_metadata must not contain the reserved reason_code field"
            )
        metadata["reason_code"] = code
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
    unavailable_reason_codes: Sequence[ReasonCode | str] | None = None

    def __post_init__(self) -> None:
        for name in ("metric_key", "formula_version"):
            text = getattr(self, name)
            if not isinstance(text, str) or not text.strip():
                raise DomainValidationError(f"{name} must be non-blank text")
            object.__setattr__(self, name, text.strip())
        if len(self.metric_key.strip()) > 100:
            raise DomainValidationError("metric_key must not exceed 100 characters")
        if len(self.formula_version.strip()) > 64:
            raise DomainValidationError("formula_version must not exceed 64 characters")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise DomainValidationError("unit must be non-blank text")
        if len(self.unit.strip()) > 32:
            raise DomainValidationError("unit must not exceed 32 characters")
        if (
            not isinstance(self.sample_count_semantics, str)
            or not self.sample_count_semantics.strip()
        ):
            raise DomainValidationError(
                "sample_count_semantics must be non-blank text"
            )
        if self.unavailable_reason_codes is None:
            raise DomainValidationError(
                "unavailable_reason_codes must be explicitly declared"
            )
        if not isinstance(self.unavailable_reason_codes, Sequence) or isinstance(
            self.unavailable_reason_codes, (str, bytes, bytearray)
        ):
            raise DomainValidationError(
                "unavailable_reason_codes must be a sequence of reason codes"
            )
        normalized_reasons: list[str] = []
        reason_values = tuple(self.unavailable_reason_codes)
        for reason_code in reason_values:
            value = getattr(reason_code, "value", reason_code)
            if not isinstance(value, str):
                raise DomainValidationError(
                    "unavailable reason codes must be strings"
                )
            if value not in METRIC_UNAVAILABLE_REASON_CODES:
                raise DomainValidationError(
                    f"{value!r} is not a v1 metric unavailable reason"
                )
            normalized_reasons.append(value)
        if len(set(normalized_reasons)) != len(normalized_reasons):
            raise DomainValidationError(
                "unavailable_reason_codes must not contain duplicates"
            )
        object.__setattr__(self, "unit", self.unit.strip())
        object.__setattr__(
            self, "sample_count_semantics", self.sample_count_semantics.strip()
        )
        object.__setattr__(
            self, "unavailable_reason_codes", tuple(normalized_reasons)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_key": self.metric_key,
            "formula_version": self.formula_version,
            "unit": self.unit,
            "sample_count_semantics": self.sample_count_semantics,
            "unavailable_reason_codes": list(self.unavailable_reason_codes),
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
        if len(self.analyzer_key) > 100:
            raise DomainValidationError("analyzer_key must not exceed 100 characters")
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
        if self.output_contract is None or isinstance(
            self.output_contract, (str, bytes, bytearray, Mapping)
        ):
            raise DomainValidationError(
                "output_contract must be an ordered sequence of "
                "MetricOutputDescriptor instances"
            )
        if not isinstance(self.output_contract, Sequence):
            raise DomainValidationError(
                "output_contract must be an ordered sequence of "
                "MetricOutputDescriptor instances"
            )
        outputs_source = tuple(self.output_contract)
        outputs = tuple(outputs_source)
        if not outputs:
            raise DomainValidationError("output_contract must declare at least one metric")
        if any(not isinstance(descriptor, MetricOutputDescriptor) for descriptor in outputs):
            raise DomainValidationError(
                "output_contract entries must be MetricOutputDescriptor instances"
            )
        keys = [descriptor.metric_key for descriptor in outputs]
        if len(set(keys)) != len(keys):
            raise DomainValidationError("output_metric_keys must not repeat")
        logical_keys = [
            (descriptor.metric_key, descriptor.formula_version)
            for descriptor in outputs
        ]
        if len(set(logical_keys)) != len(logical_keys):
            raise DomainValidationError(
                "output logical identities must not repeat"
            )
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
    """Validate and deep-freeze analyzer JSON evidence consistently."""

    return _frozen_mapping(value, field_name)


# ---------------------------------------------------------------------------
# Shared analysis state used by the v1 producers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EquitySeries:
    """Derived view over the observed equity timeline."""

    initial_equity: Decimal
    ordered_observations: tuple[EquityObservation, ...]
    valid_day_count: int
    candidate_return_count: int
    invalid_dates: tuple[date, ...]

    @property
    def has_invalid_points(self) -> bool:
        return bool(self.invalid_dates)


def _build_equity_series(
    observations: Sequence[EquityObservation],
    initial_equity: Decimal,
) -> _EquitySeries:
    valid_day_count = 0
    candidate_return_count = 0
    invalid_dates: list[date] = []
    invalid_seen = False
    for observation in observations:
        equity = observation.equity
        if observation.is_valid and equity is not None and equity > 0:
            valid_day_count += 1
            # A return candidate may only use the contiguous prefix after E0;
            # counting a valid observation after a blocked day would splice
            # over the invalid predecessor and change the formula's N.
            if not invalid_seen:
                candidate_return_count += 1
        else:
            invalid_dates.append(observation.session_date)
            invalid_seen = True
    return _EquitySeries(
        initial_equity=initial_equity,
        ordered_observations=tuple(observations),
        valid_day_count=valid_day_count,
        candidate_return_count=candidate_return_count,
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
            "valid_equity_day_count": series.valid_day_count,
            "candidate_return_count": series.candidate_return_count,
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
        if x_values is None:
            # No valid end-of-day equity at all (e.g. no observation was
            # ever submitted): a stable unavailability, never an assert.
            raise _MetricUnavailable(
                ReasonCode.INSUFFICIENT_RETURNS,
                sample_count=series.candidate_return_count,
            )
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
                else series.candidate_return_count
            ),
            reason_code=unavailable.reason_code,
            extra_metadata={
                "annualization_factor": format(ANNUALIZATION_FACTOR, "f"),
                "std_ddof": 1,
                "valid_equity_day_count": series.valid_day_count,
                "candidate_return_count": series.candidate_return_count,
                **unavailable.metadata,
                **dict(extra_metadata or {}),
            },
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
            "valid_equity_day_count": series.valid_day_count,
            "candidate_return_count": series.candidate_return_count,
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
        "rate_unit": (
            rate_snapshot.rate_unit if rate_snapshot is not None else None
        ),
        "rate_convention": (
            rate_snapshot.rate_convention if rate_snapshot is not None else None
        ),
        "rate_effective_at": (
            rate_snapshot.effective_at if rate_snapshot is not None else None
        ),
        "rate_session_mapping": (
            rate_snapshot.session_mapping if rate_snapshot is not None else None
        ),
        "rate_cutoff_boundary": (
            rate_snapshot.cutoff_boundary if rate_snapshot is not None else None
        ),
        "rate_data_cutoff_semantics": (
            rate_snapshot.data_cutoff_semantics
            if rate_snapshot is not None
            else None
        ),
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
            {
                "start_session": start.isoformat(),
                "end_session": end.isoformat(),
            }
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
                    sample_count=series.candidate_return_count,
                    reason_code=ReasonCode.MISSING_PIT_RF,
                    extra_metadata={
                        **extra_metadata,
                        "annualization_factor": format(
                            ANNUALIZATION_FACTOR, "f"
                        ),
                        "std_ddof": 1,
                        "valid_equity_day_count": series.valid_day_count,
                        "candidate_return_count": series.candidate_return_count,
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


#: The frozen v1 output contracts per registry identity.  A spec claiming a
#: registered identity must declare exactly this contract -- metric keys,
#: formula versions, units, and sample-count semantics -- so a forged
#: descriptor can never ride on a resolvable (key, version) pair.
def resolve_config_rf_daily(spec: AnalyzerSpec) -> Decimal:
    """Validate the C configuration and return its frozen daily rate.

    Uses the registered ``annual_rate_div_252@1`` convention:
    ``rf_daily = rf_annual / 252`` as plain Decimal division.  ``rf_annual``
    must be a finite Decimal strictly greater than -1; negative rates are
    allowed, rates at or below -100% are not.
    """

    parameters = spec.parameters
    source_note = parameters.get("rf_source_note")
    if (
        not isinstance(source_note, str)
        or not source_note.strip()
        or len(source_note.strip()) > 200
    ):
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} requires an explicit non-blank "
            "rf_source_note parameter of at most 200 characters"
        )
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
    # Resolve the frozen converter from the component registry instead of
    # duplicating its arithmetic here.  The registry identity is part of the
    # formula contract and must remain the single source of convention.
    from app.backtesting.registry import (
        ANNUAL_RATE_CONVERTER_COMPONENT_KIND,
        build_default_component_registry,
    )

    registry = build_default_component_registry()
    try:
        entry = registry.resolve(
            ANNUAL_RATE_CONVERTER_KEY, ANNUAL_RATE_CONVERTER_VERSION
        )
        if entry.component_kind != ANNUAL_RATE_CONVERTER_COMPONENT_KIND:
            raise AnalyzerConfigurationError(
                f"{ANNUAL_RATE_CONVERTER_KEY}@{ANNUAL_RATE_CONVERTER_VERSION} "
                "is not registered as an annual-rate converter"
            )
        converter = entry.construct({})
        return converter.compute(rf_annual)
    except AnalyzerConfigurationError:
        raise
    except Exception as exc:
        raise AnalyzerConfigurationError(
            "the registered annual-rate converter could not be resolved"
        ) from exc


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
        "risk_free_rate_note": spec.parameters["rf_source_note"].strip(),
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
    with analyzer_decimal_context():
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
            # Every member counted as valid is strictly positive; therefore
            # its Decimal average is positive by construction.  A separate
            # NON_POSITIVE_AVERAGE_EQUITY result would be unreachable and is
            # intentionally not part of the v1 reason vocabulary.
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
    with analyzer_decimal_context():
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
            unit=cumulative_descriptor.unit or "currency",
            sample_count=fill_count,
            extra_metadata={
                "gross_traded_notional": format(gross_traded_notional, "f"),
                "cumulative_fees": format(cumulative_fees, "f"),
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
                    "gross_traded_notional": format(gross_traded_notional, "f"),
                    "cumulative_fees": format(cumulative_fees, "f"),
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


def _produce_performance(state, spec):
    """Compute E0-based returns, positive drawdown magnitude and sample risk.

    Annualized return uses 252 official sessions; volatility uses simple daily
    returns with ddof=1. Missing/non-positive equity never gets interpolated.
    """
    series = _build_equity_series(state.equity_observations, state.initial_equity_snapshot.equity_e0)
    returns = [] if series.has_invalid_points else _daily_returns(series)
    results = []
    for descriptor in spec.output_contract:
        reason = ReasonCode.INVALID_EQUITY if series.has_invalid_points else None
        count = series.candidate_return_count
        if reason is None and (not returns or (descriptor.metric_key == "volatility" and len(returns) < 2)):
            reason = ReasonCode.INSUFFICIENT_RETURNS
        metadata = {"annualization_factor": "252", "std_ddof": 1, "initial_equity": str(series.initial_equity), "drawdown_convention": "positive_peak_to_trough", "invalid_session_dates": [day.isoformat() for day in series.invalid_dates], "candidate_return_count": count, "valid_equity_day_count": series.valid_day_count}
        if reason:
            results.append(MetricResult.unavailable(run_id=state.run_id, spec=spec, metric_key=descriptor.metric_key, formula_version=descriptor.formula_version, unit=descriptor.unit, sample_count=count, reason_code=reason, extra_metadata=metadata))
            continue
        equities = [series.initial_equity, *(item.equity for item in series.ordered_observations)]
        ratio = equities[-1] / equities[0]
        if descriptor.metric_key == "total_return":
            value = ratio - 1
        elif descriptor.metric_key == "annualized_return":
            value = ratio ** (ANNUALIZATION_FACTOR / len(returns)) - 1
        elif descriptor.metric_key == "max_drawdown":
            peak, value = equities[0], Decimal(0)
            for equity in equities[1:]:
                peak = max(peak, equity)
                value = max(value, 1 - equity / peak)
        else:
            mean = sum(returns) / len(returns)
            value = (sum((item - mean) ** 2 for item in returns) / (len(returns) - 1) * ANNUALIZATION_FACTOR).sqrt()
        results.append(MetricResult.available(run_id=state.run_id, spec=spec, metric_key=descriptor.metric_key, formula_version=descriptor.formula_version, unit="ratio", value=value, sample_count=count, extra_metadata=metadata))
    return tuple(results)


#: Built-in v1 producers keyed by exact (analyzer_key, analyzer_version).
_BUILTIN_PRODUCERS: dict[tuple[str, int], Callable[["_EngineState", AnalyzerSpec], tuple[MetricResult, ...]]] = {
    ("performance", 1): _produce_performance,
    (SHARPE_SIMPLE_ANALYZER_KEY, 1): _produce_sharpe_simple,
    (PIT_RF_ANALYZER_KEY, 1): _produce_sharpe_pit_rf,
    (CONFIG_RF_ANALYZER_KEY, 1): _produce_sharpe_config_rf,
    (TURNOVER_ANALYZER_KEY, 1): _produce_turnover,
    (FEE_SUMMARY_ANALYZER_KEY, 1): _produce_fee_summary,
}

#: Allowed parameter names per v1 identity; anything else is rejected so
#: undeclared knobs cannot silently alter behavior.
_FROZEN_V1_PARAMETERS: dict[str, tuple[str, ...]] = {
    SHARPE_SIMPLE_ANALYZER_KEY: (),
    PIT_RF_ANALYZER_KEY: (),
    CONFIG_RF_ANALYZER_KEY: ("rf_annual", "rf_source_note"),
    TURNOVER_ANALYZER_KEY: (),
    FEE_SUMMARY_ANALYZER_KEY: (),
}


def frozen_output_contract_for(
    analyzer_key: str, analyzer_version: int
) -> tuple[MetricOutputDescriptor, ...]:
    """Return the frozen v1 output contract of one registered identity."""

    return _FROZEN_V1_OUTPUT_CONTRACTS[(analyzer_key, analyzer_version)]


def validate_v1_analyzer_spec(spec: AnalyzerSpec) -> None:
    """Validate one spec against its identity's frozen v1 contract.

    Rejects forged output contracts and unknown parameters before any
    observation can be accepted.
    """

    identity = (spec.analyzer_key, spec.analyzer_version)
    frozen = _FROZEN_V1_OUTPUT_CONTRACTS.get(identity)
    if frozen is None:
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} is not a registered v1 analyzer "
            "identity"
        )
    actual = tuple(
        (
            descriptor.metric_key,
            descriptor.formula_version,
            descriptor.unit,
            descriptor.sample_count_semantics,
            tuple(descriptor.unavailable_reason_codes),
        )
        for descriptor in spec.output_contract
    )
    expected = tuple(
        (
            descriptor.metric_key,
            descriptor.formula_version,
            descriptor.unit,
            descriptor.sample_count_semantics,
            tuple(descriptor.unavailable_reason_codes),
        )
        for descriptor in frozen
    )
    if actual != expected:
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} declares an output contract that does "
            "not match the frozen registry contract"
        )
    allowed_parameters = _FROZEN_V1_PARAMETERS.get(spec.analyzer_key, ())
    unknown = [
        name for name in spec.parameters if name not in allowed_parameters
    ]
    if unknown:
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} received unknown parameters "
            f"{sorted(unknown)}; the frozen parameter schema does not "
            "declare them"
        )

    # Resolve the exact registry entry and compare the complete constructed
    # spec.  Checking only ``(key, version)`` and output metric names would
    # let forged display names, input contracts, or formula parameters ride
    # on a registered identity.
    from app.backtesting.registry import (
        ANALYZER_COMPONENT_KIND,
        build_default_component_registry,
    )

    registry = build_default_component_registry()
    try:
        entry = registry.resolve(spec.analyzer_key, spec.analyzer_version)
        if entry.component_kind != ANALYZER_COMPONENT_KIND:
            raise AnalyzerConfigurationError(
                f"{spec.display_identity} resolves to a non-analyzer registry "
                "component"
            )
        registered = entry.construct(dict(spec.parameters))
    except AnalyzerConfigurationError:
        raise
    except Exception as exc:
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} could not be resolved from the analyzer "
            "registry"
        ) from exc
    if not isinstance(registered, AnalyzerSpec):
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} registry factory did not return an "
            "AnalyzerSpec"
        )
    if spec.describe() != registered.describe():
        raise AnalyzerConfigurationError(
            f"{spec.display_identity} does not equal the complete spec produced "
            "by the registered analyzer factory"
        )


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
                sample_count_semantics="candidate_return_count_including_zero_return_days",
                unavailable_reason_codes=(
                    ReasonCode.INVALID_EQUITY,
                    ReasonCode.INSUFFICIENT_RETURNS,
                    ReasonCode.ZERO_RETURN_STDDEV,
                ),
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
                sample_count_semantics="candidate_return_count_including_zero_return_days",
                unavailable_reason_codes=(
                    ReasonCode.INVALID_EQUITY,
                    ReasonCode.MISSING_PIT_RF,
                    ReasonCode.INSUFFICIENT_RETURNS,
                    ReasonCode.ZERO_RETURN_STDDEV,
                ),
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
        input_contract={
            **_INPUT_CONTRACT_NO_RATES,
            "rf_annual": True,
            "rf_source_note": True,
        },
        output_contract=(
            MetricOutputDescriptor(
                metric_key=METRIC_KEY_SHARPE,
                formula_version=FORMULA_VERSION_SHARPE_CONFIG_RF,
                unit="ratio",
                sample_count_semantics="candidate_return_count_including_zero_return_days",
                unavailable_reason_codes=(
                    ReasonCode.INVALID_EQUITY,
                    ReasonCode.INSUFFICIENT_RETURNS,
                    ReasonCode.ZERO_RETURN_STDDEV,
                ),
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
                unavailable_reason_codes=(
                    ReasonCode.INVALID_EQUITY,
                    ReasonCode.NO_VALID_END_OF_DAY_EQUITY,
                ),
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
                unavailable_reason_codes=(),
            ),
            MetricOutputDescriptor(
                metric_key=METRIC_KEY_FEE_TO_GROSS_TRADED_NOTIONAL,
                formula_version=FORMULA_VERSION_FEE_TO_GROSS,
                unit="ratio",
                sample_count_semantics="applied_fill_count",
                unavailable_reason_codes=(ReasonCode.ZERO_GROSS_TRADED_NOTIONAL,),
            ),
        ),
    )


def build_performance_spec(parameters=None):
    """One versioned producer for the four basic daily performance metrics."""
    return AnalyzerSpec(
        analyzer_key="performance", analyzer_version=1,
        name_zh="收益与风险", name_en="Return and Risk", parameters=parameters,
        input_contract=_INPUT_CONTRACT_NO_RATES,
        output_contract=tuple(MetricOutputDescriptor(
            metric_key=key, formula_version=formula, unit="ratio",
            sample_count_semantics="candidate_return_count_including_zero_return_days",
            unavailable_reason_codes=(ReasonCode.INVALID_EQUITY, ReasonCode.INSUFFICIENT_RETURNS),
        ) for key, formula in (
            ("total_return", "total_return_e0_v1"),
            ("annualized_return", "annualized_return_geometric_252_v1"),
            ("max_drawdown", "max_drawdown_e0_positive_v1"),
            ("volatility", "volatility_simple_ddof1_252_v1"),
        )),
    )


_FROZEN_V1_OUTPUT_CONTRACTS: dict[tuple[str, int], tuple[MetricOutputDescriptor, ...]] = {
    ("performance", 1): build_performance_spec().output_contract,
    (SHARPE_SIMPLE_ANALYZER_KEY, 1): build_sharpe_simple_spec().output_contract,
    (PIT_RF_ANALYZER_KEY, 1): build_sharpe_pit_rf_spec().output_contract,
    (CONFIG_RF_ANALYZER_KEY, 1): build_sharpe_config_rf_spec().output_contract,
    (TURNOVER_ANALYZER_KEY, 1): build_turnover_spec().output_contract,
    (FEE_SUMMARY_ANALYZER_KEY, 1): build_fee_summary_spec().output_contract,
}


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
    registry_snapshot: Mapping[str, Any]

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
        rendered = canonical_evidence_json(payload).encode("utf-8")
        return (
            f"{ANALYSIS_EVIDENCE_HASH_ALGORITHM}:"
            f"{hashlib.sha256(rendered).hexdigest()}"
        )

    def compute_results(self) -> tuple[MetricResult, ...]:
        results: list[MetricResult] = []
        formula_signature = self.formula_signature()
        input_evidence_signature = self.input_evidence_signature()
        series = _build_equity_series(
            self.equity_observations,
            self.initial_equity_snapshot.equity_e0,
        )
        expected_sample_counts = {
            "candidate_return_count_including_zero_return_days": (
                series.candidate_return_count
            ),
            "valid_end_of_day_equity_count": series.valid_day_count,
            "applied_fill_count": len(self.fill_observations),
        }
        for spec in self.specs:
            producer = _BUILTIN_PRODUCERS[(spec.analyzer_key, spec.analyzer_version)]
            with analyzer_decimal_context():
                raw_produced = producer(self, spec)
            if isinstance(raw_produced, (str, bytes, bytearray)) or not isinstance(
                raw_produced, Sequence
            ):
                raise DomainValidationError(
                    f"{spec.display_identity} producer output must be an "
                    "ordered sequence of MetricResult instances"
                )
            try:
                produced = tuple(raw_produced)
            except TypeError as exc:
                raise DomainValidationError(
                    f"{spec.display_identity} producer output must be an "
                    "ordered sequence of MetricResult instances"
                ) from exc
            if any(not isinstance(item, MetricResult) for item in produced):
                raise DomainValidationError(
                    f"{spec.display_identity} producer output must contain "
                    "only MetricResult instances"
                )
            descriptors = tuple(spec.output_contract)
            expected_keys = [
                (item.metric_key, item.formula_version) for item in descriptors
            ]
            produced_keys = [
                (item.metric_key, item.formula_version) for item in produced
            ]
            if produced_keys != expected_keys:
                raise DomainValidationError(
                    f"{spec.display_identity} produced logical metric outputs "
                    "that do not exactly match output-contract order"
                )
            for result, descriptor in zip(produced, descriptors):
                if result.run_id != self.run_id:
                    raise DomainValidationError(
                        f"{spec.display_identity} produced a result for a "
                        "different run"
                    )
                if (
                    result.analyzer_key != spec.analyzer_key
                    or result.analyzer_version != spec.analyzer_version
                ):
                    raise DomainValidationError(
                        f"{spec.display_identity} produced a result with a "
                        "different analyzer identity"
                    )
                if result.unit != descriptor.unit:
                    raise DomainValidationError(
                        f"{spec.display_identity} produced {result.metric_key} "
                        "with a unit that differs from its output contract"
                    )
                expected_sample_count = expected_sample_counts.get(
                    descriptor.sample_count_semantics
                )
                if expected_sample_count is None:
                    raise DomainValidationError(
                        f"{spec.display_identity} declares unsupported "
                        "sample_count semantics"
                    )
                if result.sample_count != expected_sample_count:
                    raise DomainValidationError(
                        f"{spec.display_identity} produced {result.metric_key} "
                        "with a sample_count that differs from its output "
                        "contract semantics"
                    )
                if result.status is MetricStatus.UNAVAILABLE:
                    reason_code = getattr(
                        result.analyzer_metadata.get("reason_code"),
                        "value",
                        result.analyzer_metadata.get("reason_code"),
                    )
                    if reason_code not in descriptor.unavailable_reason_codes:
                        raise DomainValidationError(
                            f"{spec.display_identity} produced unavailable "
                            f"{result.metric_key} with undeclared reason "
                            f"{reason_code}"
                        )
                results.append(
                    replace(
                        result,
                        analyzer_metadata={
                            **dict(result.analyzer_metadata),
                            "formula_signature": formula_signature,
                            "input_evidence_signature": input_evidence_signature,
                            "contract_unit": descriptor.unit,
                            "sample_count_semantics": (
                                descriptor.sample_count_semantics
                            ),
                        },
                    )
                )
        return tuple(results)

    def summary_counts(self) -> dict[str, Any]:
        series = _build_equity_series(
            self.equity_observations, self.initial_equity_snapshot.equity_e0
        )
        with analyzer_decimal_context():
            gross_traded_notional = sum(
                (
                    fill.fact.gross_traded_notional
                    for fill in self.fill_observations
                ),
                Decimal("0"),
            )
            cumulative_fees = sum(
                (fill.fact.fees for fill in self.fill_observations),
                Decimal("0"),
            )
        return {
            "initial_equity": self.initial_equity_snapshot.equity_e0,
            "valid_day_count": series.valid_day_count,
            "candidate_return_count": series.candidate_return_count,
            "fill_count": len(self.fill_observations),
            "gross_traded_notional": gross_traded_notional,
            "cumulative_fees": cumulative_fees,
        }


def compute_terminal_fingerprint(
    *,
    status: AnalysisStatus | str,
    analysis_snapshot: Any,
    results: Sequence[MetricResult],
    failure: Mapping[str, Any] | None = None,
) -> str:
    """Compute the sole terminal retry identity for final/aborted states.

    The payload intentionally contains both signatures and their source
    material.  A caller cannot turn a self-consistent but incomplete hash
    into an idempotent retry by omitting E0, the formal timeline, rate facts,
    summary aggregates, or nested analyzer metadata.
    """

    rate_snapshot = getattr(analysis_snapshot, "rate_snapshot", None)
    counts = analysis_snapshot.summary_counts()
    rate_payload = None
    if rate_snapshot is not None:
        rate_payload = {
            "source_key": rate_snapshot.source_key,
            "source_version": rate_snapshot.source_version,
            "snapshot_hash": rate_snapshot.snapshot_hash,
            "rate_unit": rate_snapshot.rate_unit,
            "rate_convention": rate_snapshot.rate_convention,
            "effective_at": rate_snapshot.effective_at,
            "session_mapping": rate_snapshot.session_mapping,
            "data_cutoff_semantics": rate_snapshot.data_cutoff_semantics,
            "cutoff_boundary": rate_snapshot.cutoff_boundary,
            "expected_sessions": rate_snapshot.expected_sessions,
            "rates": rate_snapshot.rates,
            "fact_evidence": rate_snapshot.fact_evidence,
            "missing_ranges": rate_snapshot.missing_ranges,
            "query_parameters": rate_snapshot.query_parameters,
        }
    timeline = getattr(analysis_snapshot, "formal_timeline", None)
    if timeline is None:
        timeline = analysis_snapshot.initial_equity_snapshot.formal_timeline
    if timeline is None:  # pragma: no cover - engine admission rejects it
        raise AnalyzerConfigurationError(
            "terminal fingerprint requires a FormalSessionTimeline"
        )
    payload = {
        "contract": TERMINAL_FINGERPRINT_CONTRACT_VERSION,
        "run_id": analysis_snapshot.run_id,
        "status": getattr(status, "value", status),
        "formula_signature": analysis_snapshot.formula_signature(),
        "input_evidence_signature": analysis_snapshot.input_evidence_signature(),
        "initial_equity_snapshot": analysis_snapshot.initial_equity_snapshot.evidence_payload(),
        "formal_timeline": timeline.as_payload(),
        "analyzer_snapshot": [spec.describe() for spec in analysis_snapshot.specs],
        "registry_snapshot": analysis_snapshot.registry_snapshot,
        "summary_counts": counts,
        "rate_snapshot": rate_payload,
        "failure": failure,
        "results": [
            {
                "metric_key": result.metric_key,
                "formula_version": result.formula_version,
                "analyzer_key": result.analyzer_key,
                "analyzer_version": result.analyzer_version,
                "status": result.status.value,
                "value": result.value,
                "unit": result.unit,
                "sample_count": result.sample_count,
                "unavailable_reason": result.unavailable_reason,
                "analyzer_metadata": dict(result.analyzer_metadata),
            }
            for result in results
        ],
    }
    rendered = canonical_evidence_json(payload).encode("utf-8")
    return (
        f"{ANALYSIS_EVIDENCE_HASH_ALGORITHM}:"
        f"{hashlib.sha256(rendered).hexdigest()}"
    )


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
    registry_snapshot: Mapping[str, Any]
    admission_token: object | None = None
    snapshot_binding: str | None = None
    failure: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise DomainValidationError("run_id must be non-blank text")
        object.__setattr__(self, "run_id", self.run_id.strip())
        try:
            normalized_status = AnalysisStatus(
                getattr(self.status, "value", self.status)
            )
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(
                "analysis snapshot status must be partial, final, or aborted"
            ) from exc
        object.__setattr__(self, "status", normalized_status)
        if not isinstance(self.reporting_currency, str) or not self.reporting_currency.strip():
            raise DomainValidationError(
                "analysis snapshot reporting_currency must be non-blank text"
            )
        object.__setattr__(
            self, "reporting_currency", self.reporting_currency.strip().upper()
        )
        if not isinstance(self.initial_equity_snapshot, InitialEquitySnapshot):
            raise DomainValidationError(
                "analysis snapshot initial_equity_snapshot must be an "
                "InitialEquitySnapshot"
            )
        if (
            self.reporting_currency
            != self.initial_equity_snapshot.reporting_currency
        ):
            raise DomainValidationError(
                "analysis snapshot reporting_currency must match the E0 "
                "reporting currency"
            )
        for field_name, expected_type in (
            ("equity_observations", EquityObservation),
            ("fill_observations", FillObservation),
            ("specs", AnalyzerSpec),
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, Sequence)
                or isinstance(value, (str, bytes, bytearray, Mapping))
            ):
                raise DomainValidationError(
                    f"analysis snapshot {field_name} must be an ordered sequence"
                )
            normalized = tuple(value)
            if any(not isinstance(item, expected_type) for item in normalized):
                raise DomainValidationError(
                    f"analysis snapshot {field_name} contains an invalid item"
                )
            object.__setattr__(self, field_name, normalized)
        if self.rate_snapshot is not None and not isinstance(
            self.rate_snapshot, PitRateSnapshot
        ):
            raise DomainValidationError(
                "analysis snapshot rate_snapshot must be a PitRateSnapshot"
            )
        if not isinstance(self.registry_snapshot, Mapping):
            raise DomainValidationError(
                "analysis snapshot registry_snapshot must be a mapping"
            )
        object.__setattr__(
            self,
            "registry_snapshot",
            _frozen_mapping(self.registry_snapshot, "registry_snapshot"),
        )
        if self.snapshot_binding is not None:
            if (
                not isinstance(self.snapshot_binding, str)
                or not self.snapshot_binding.strip()
            ):
                raise DomainValidationError(
                    "analysis snapshot snapshot_binding must be non-blank text"
                )
            object.__setattr__(
                self, "snapshot_binding", self.snapshot_binding.strip()
            )
        if self.failure is not None:
            if not isinstance(self.failure, Mapping):
                raise DomainValidationError(
                    "analysis snapshot failure must be a mapping"
                )
            object.__setattr__(
                self, "failure", _frozen_mapping(self.failure, "failure")
            )

    @property
    def formal_sessions(self) -> tuple[date, ...]:
        """The single official session sequence shared by all producers."""

        return tuple(self.initial_equity_snapshot.formal_sessions)

    @property
    def formal_timeline(self) -> FormalSessionTimeline:
        """The immutable timeline DTO frozen at admission."""

        timeline = self.initial_equity_snapshot.formal_timeline
        if timeline is None:  # pragma: no cover - engine creation rejects it
            raise AnalyzerConfigurationError(
                "analysis snapshot has no FormalSessionTimeline"
            )
        return timeline

    @property
    def timeline_hash(self) -> str | None:
        return self.initial_equity_snapshot.timeline_hash

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
        registry_snapshot=snapshot.registry_snapshot,
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
        formal_timeline: FormalSessionTimeline | None = None,
        first_step_sequence: int = 0,
    ) -> None:
        if not isinstance(initial_equity_snapshot, InitialEquitySnapshot):
            raise AnalyzerInputError(
                "initial_equity_snapshot must be an InitialEquitySnapshot"
            )
        if not initial_equity_snapshot.formal_sessions or not initial_equity_snapshot.timeline_hash:
            raise AnalyzerConfigurationError(
                "AnalyzerEngine creation requires the complete frozen formal "
                "session sequence and its timeline_hash"
            )
        snapshot_timeline = initial_equity_snapshot.formal_timeline
        if snapshot_timeline is None:
            raise AnalyzerConfigurationError(
                "AnalyzerEngine creation requires a FormalSessionTimeline"
            )
        if formal_timeline is not None and formal_timeline != snapshot_timeline:
            raise AnalyzerConfigurationError(
                "formal_timeline does not match the InitialEquitySnapshot"
            )
        formal_timeline = snapshot_timeline
        if isinstance(first_step_sequence, bool) or not isinstance(
            first_step_sequence, int
        ) or first_step_sequence < 0:
            raise AnalyzerConfigurationError(
                "first_step_sequence must be a non-negative integer"
            )
        if analyzer_specs is None or isinstance(
            analyzer_specs, (str, bytes, bytearray, Mapping)
        ) or not isinstance(analyzer_specs, Sequence):
            raise AnalyzerConfigurationError(
                "analyzer_specs must be an ordered sequence of AnalyzerSpec "
                "instances"
            )
        try:
            specs = tuple(analyzer_specs)
        except TypeError as exc:
            raise AnalyzerConfigurationError(
                "analyzer_specs must be a sequence of AnalyzerSpec instances"
            ) from exc
        if not specs:
            raise AnalyzerConfigurationError(
                "at least one AnalyzerSpec must be configured for a run"
            )
        seen_identities: set[tuple[str, int]] = set()
        seen_producers: dict[tuple[str, str], tuple[str, int]] = {}
        for spec in specs:
            if not isinstance(spec, AnalyzerSpec):
                raise AnalyzerConfigurationError(
                    "analyzer_specs entries must be AnalyzerSpec instances"
                )
            identity = (spec.analyzer_key, spec.analyzer_version)
            if identity in seen_identities:
                raise AnalyzerConfigurationError(
                    f"analyzer {spec.display_identity} is configured more than "
                    "once; each analyzer identity may appear only once per run"
                )
            seen_identities.add(identity)
            if identity not in _BUILTIN_PRODUCERS:
                raise AnalyzerConfigurationError(
                    f"no analyzer implementation is registered for "
                    f"{spec.display_identity}; unknown versions never fall back"
                )
            # The declared contract must equal the frozen registry contract
            # for this identity; a resolvable key/version never legitimizes
            # forged metric keys or formula versions.
            validate_v1_analyzer_spec(spec)
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
            if not isinstance(accounting_currency, str) or not accounting_currency.strip():
                raise AnalyzerConfigurationError(
                    "accounting_currency must be non-blank text when provided"
                )
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
        if not needs_rate_snapshot and frozen_rate_snapshot is not None:
            raise AnalyzerConfigurationError(
                "a frozen PIT rate snapshot is only valid when "
                f"{PIT_RF_ANALYZER_KEY}@1 is configured"
            )
        if frozen_rate_snapshot is not None and not isinstance(
            frozen_rate_snapshot, PitRateSnapshot
        ):
            raise AnalyzerConfigurationError(
                "frozen_rate_snapshot must be a PitRateSnapshot"
            )
        if frozen_rate_snapshot is not None:
            covered_start = frozen_rate_snapshot.coverage_start
            first_formal = initial_equity_snapshot.session_date
            if covered_start is not None and covered_start > first_formal:
                raise AnalyzerConfigurationError(
                    "the frozen PIT rate snapshot starts after the first "
                    "formal session; it must cover the entire window"
                )
            formal_sessions = tuple(initial_equity_snapshot.formal_sessions)
            expected_rate_sessions = tuple(frozen_rate_snapshot.expected_sessions)
            if formal_sessions and expected_rate_sessions != formal_sessions:
                raise AnalyzerConfigurationError(
                    "the PIT rate snapshot expected_sessions must equal the "
                    "single frozen formal analyzer timeline"
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
        from app.backtesting.registry import (
            ANNUAL_RATE_CONVERTER_KEY_ANNUAL_RATE_DIV_252,
            build_default_component_registry,
        )

        registry = build_default_component_registry()
        registry_snapshot = {
            "registry_entries": [
                registry.resolve(spec.analyzer_key, spec.analyzer_version).describe()
                for spec in specs
            ],
            "annual_rate_converter": (
                registry.resolve(
                    ANNUAL_RATE_CONVERTER_KEY_ANNUAL_RATE_DIV_252,
                    ANNUAL_RATE_CONVERTER_VERSION,
                ).describe()
                if any(spec.analyzer_key == CONFIG_RF_ANALYZER_KEY for spec in specs)
                else None
            ),
        }
        # Registry names, parameter schemas and capabilities are run-creation
        # evidence.  Freeze them now so a deployment during a long run cannot
        # alter the descriptor later persisted at finalization.
        self._registry_snapshot = _frozen_mapping(
            registry_snapshot,
            "registry_snapshot",
        )
        self._equity_observations: list[EquityObservation] = []
        self._fills: dict[UUID, FillObservation] = {}
        self._finalized_status: AnalysisStatus | None = None
        self._failure_payload: Mapping[str, Any] | None = None
        self._final_results: tuple[MetricResult, ...] | None = None
        self._terminal_fingerprint: str | None = None
        # The formal timeline is bound at construction.  Equity observations
        # must match its official session sequence exactly (no skipped
        # zero-return days, no gaps) with contiguous, monotonically
        # increasing step sequences.  ``attach_formal_timeline`` remains as
        # a compatibility assertion for coordinator integrations.
        self._formal_timeline: FormalSessionTimeline = formal_timeline
        self._formal_sessions: tuple[date, ...] | None = formal_timeline.sessions
        self._first_step_sequence: int = first_step_sequence
        # Run-admission stamp: set exclusively by the admission boundary
        # (:func:`app.backtesting.analysis_admission.admit_analysis_run`).
        # The runner refuses engines without it.
        self._admission_evidence: Mapping[str, Any] | None = None
        self._admission_attested = False

    # -- construction -----------------------------------------------------

    @classmethod
    def create(
        cls,
        initial_equity_snapshot: InitialEquitySnapshot,
        analyzer_specs: Sequence[AnalyzerSpec],
        frozen_rate_snapshot: PitRateSnapshot | None = None,
        decimal_policy: Mapping[str, Any] | None = None,
        accounting_currency: str | None = None,
        formal_timeline: FormalSessionTimeline | None = None,
        first_step_sequence: int = 0,
    ) -> "AnalyzerEngine":
        """Admission entry point matching the documented creation order.

        ``decimal_policy`` is accepted for signature completeness: only the
        frozen ``prec=50``/``ROUND_HALF_EVEN`` policy is supported, so any
        other declaration is rejected instead of silently ignored.
        """

        if decimal_policy is not None:
            if not isinstance(decimal_policy, Mapping):
                raise AnalyzerConfigurationError(
                    "decimal_policy must be a mapping when provided"
                )
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
            formal_timeline=formal_timeline,
            first_step_sequence=first_step_sequence,
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
    def formal_timeline(self) -> FormalSessionTimeline:
        """Return the timeline DTO frozen at engine creation."""

        return self._formal_timeline

    @property
    def finalized_status(self) -> AnalysisStatus | None:
        return self._finalized_status

    def formula_signature(self) -> str:
        return self._state().formula_signature()

    def input_evidence_signature(self) -> str:
        return self._state().input_evidence_signature()

    def attach_formal_timeline(
        self,
        sessions: FormalSessionTimeline | Sequence[date],
        *,
        first_step_sequence: int = 0,
    ) -> None:
        """Pin the official session/step sequence equity must follow.

        Once attached, every :meth:`observe_equity` call is checked against
        the exact next formal session and the contiguous next step, so a
        skipped zero-return day, a gap, or an inverted step can never
        reshape the return series.  Attaching is only possible before any
        observation was submitted.
        """

        if self._equity_observations:
            raise AnalysisStateConflictError(
                "the formal timeline can only be attached before the first "
                "equity observation"
            )
        if isinstance(first_step_sequence, bool) or not isinstance(
            first_step_sequence, int
        ) or first_step_sequence < 0:
            raise DomainValidationError(
                "first_step_sequence must be a non-negative integer"
            )
        if isinstance(sessions, FormalSessionTimeline):
            timeline = sessions
        else:
            if (
                not isinstance(sessions, Sequence)
                or isinstance(sessions, (str, bytes, bytearray))
            ):
                raise DomainValidationError(
                    "sessions must be an ordered sequence or "
                    "FormalSessionTimeline"
                )
            timeline = FormalSessionTimeline(sessions)
        normalized = timeline.sessions
        for session_date in normalized:
            if not isinstance(session_date, date) or isinstance(session_date, datetime):
                raise DomainValidationError(
                    "sessions entries must be calendar dates"
                )
        if not normalized:
            raise DomainValidationError(
                "formal sessions must contain at least one official session"
            )
        snapshot_timeline = self._initial_equity_snapshot.formal_timeline
        if snapshot_timeline is not None and snapshot_timeline != timeline:
            raise AnalysisStateConflictError(
                "the formal timeline differs from the E0 admission snapshot"
            )
        snapshot_hash = self._initial_equity_snapshot.timeline_hash
        if snapshot_hash is not None and snapshot_hash != timeline.timeline_hash:
            raise AnalysisStateConflictError(
                "the formal timeline hash differs from the E0 admission snapshot"
            )
        for index in range(1, len(normalized)):
            if normalized[index] <= normalized[index - 1]:
                raise DomainValidationError(
                    "formal sessions must strictly increase"
                )
        if self._formal_sessions is not None:
            if (
                self._formal_sessions != normalized
                or self._first_step_sequence != first_step_sequence
            ):
                raise AnalysisStateConflictError(
                    "the formal timeline is already frozen and cannot be "
                    "replaced by a different session or step sequence"
                )
            return
        self._formal_timeline = timeline
        self._formal_sessions = normalized
        self._first_step_sequence = first_step_sequence

    def _set_admission_evidence(
        self, evidence: Mapping[str, Any], *, attested: bool
    ) -> None:
        """Store admission evidence while retaining its trust provenance."""

        if self._admission_evidence is not None:
            raise AnalysisStateConflictError(
                "this engine is already stamped with admission evidence"
            )
        if not isinstance(evidence, Mapping) or not evidence:
            raise DomainValidationError(
                "admission evidence must be a non-empty mapping"
            )
        self._admission_evidence = _frozen_mapping(
            evidence, "admission evidence"
        )
        self._admission_attested = attested

    def _mark_admitted(self, evidence: Mapping[str, Any]) -> object:
        """Attach the coordinator-only admission attestation (once only)."""

        caller = inspect.currentframe()
        caller = caller.f_back if caller is not None else None
        caller_module = (
            caller.f_globals.get("__name__") if caller is not None else None
        )
        if caller_module != "app.backtesting.analysis_admission":
            raise DomainValidationError(
                "only the run-admission coordinator call path may attest an "
                "analyzer engine"
            )
        self._set_admission_evidence(evidence, attested=True)
        token = _AdmissionToken()
        self._admission_token = token
        _ADMITTED_ENGINE_TOKENS[self] = token
        _ADMITTED_CAPABILITY_TOKENS.add(token)
        admission_identity = {
            "run_id": self.run_id,
            "reporting_currency": self.reporting_currency,
            "initial_equity_hash": evidence.get("initial_equity_hash"),
            "rate_snapshot_hash": evidence.get("rate_snapshot_hash"),
            "formula_signature": self.formula_signature(),
            "specs": [spec.describe() for spec in self._specs],
            "registry_snapshot": self._registry_snapshot,
            "formal_timeline": self._formal_timeline.as_payload(),
        }
        _ADMISSION_TOKEN_BINDINGS[token] = {
            "run_id": self.run_id,
            "initial_equity_hash": evidence.get("initial_equity_hash"),
            "rate_snapshot_hash": evidence.get("rate_snapshot_hash"),
            "admission_identity": json.loads(
                canonical_evidence_json(admission_identity)
            ),
            "failure_snapshot_binding": None,
            "failure_envelope": None,
            "partial_snapshot_binding": None,
            "terminal_fingerprint": None,
        }
        return token

    def mark_admitted(self, evidence: Mapping[str, Any]) -> None:
        """Compatibility marker for isolated unit tests.

        This deliberately does *not* create a trusted production admission;
        ``DeterministicBacktestRunner`` also requires the coordinator's
        private attestation bit.  Production code must use
        :func:`admit_analysis_run`.
        """

        self._set_admission_evidence(evidence, attested=False)

    @property
    def admission_evidence(self) -> Mapping[str, Any] | None:
        """The frozen admission stamp, or ``None`` before admission."""

        return self._admission_evidence

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
        position = len(self._equity_observations)
        if self._formal_sessions is None:
            # Without the official timeline the engine cannot prove that
            # sessions are contiguous and complete; accepting observations
            # anyway would let skipped zero-return days reshape Sharpe.
            raise AnalyzerInputError(
                "the formal timeline must be attached before equity "
                "observations; construct engines through run admission"
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
        expected_session: date | None = None
        if self._formal_sessions is not None:
            if position >= len(self._formal_sessions):
                raise AnalyzerInputError(
                    "more equity observations than official formal sessions; "
                    "the observation does not belong to the frozen timeline"
                )
            expected_session = self._formal_sessions[position]
            if observation.session_date != expected_session:
                raise AnalyzerInputError(
                    f"equity observation session "
                    f"{observation.session_date.isoformat()} does not match "
                    f"the official timeline's next session "
                    f"{expected_session.isoformat()}; skipping or reordering "
                    "formal sessions would fabricate the return series"
                )
            expected_step = self._first_step_sequence + position
            if observation.step_sequence != expected_step:
                raise AnalyzerInputError(
                    f"equity observation step_sequence "
                    f"{observation.step_sequence} does not match the "
                    f"contiguous timeline expectation {expected_step}"
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
        if self._formal_sessions is None or observation.fact.session_date not in set(
            self._formal_sessions
        ):
            raise AnalyzerInputError(
                f"fill fact session {observation.fact.session_date.isoformat()} "
                "does not belong to the frozen formal session timeline"
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
        snapshot = AnalysisSnapshot(
            run_id=self._run_id,
            status=status,
            reporting_currency=self._reporting_currency,
            initial_equity_snapshot=self._initial_equity_snapshot,
            equity_observations=tuple(self._equity_observations),
            fill_observations=tuple(
                self._fills[fill_id] for fill_id in sorted(self._fills, key=str)
            ),
            rate_snapshot=self._rate_snapshot,
            specs=self._specs,
            registry_snapshot=self._registry_snapshot,
            admission_token=getattr(self, "_admission_token", None),
            failure=self._failure_payload,
        )
        token = getattr(self, "_admission_token", None)
        if token is not None:
            binding = _ADMISSION_TOKEN_BINDINGS.get(token)
            if binding is not None:
                snapshot_binding = _compute_analysis_snapshot_binding(snapshot)
                binding["partial_snapshot_binding"] = snapshot_binding
                object.__setattr__(snapshot, "snapshot_binding", snapshot_binding)
        return snapshot

    # -- finalization ----------------------------------------------------------

    def finalize(
        self,
        status: AnalysisStatus | str = AnalysisStatus.FINAL,
        *,
        failure: Mapping[str, Any] | None = None,
    ) -> tuple[MetricResult, ...]:
        """Produce the definitive metric results exactly once.

        Both ``final`` and ``aborted`` are terminal states.  A same-status
        retry with identical failure evidence returns the frozen results;
        an opposite status or different abort evidence is a hard conflict.
        """

        try:
            requested = AnalysisStatus(getattr(status, "value", status))
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(
                "finalize status must be final or aborted"
            ) from exc
        if requested is AnalysisStatus.PARTIAL:
            raise DomainValidationError(
                "finalize cannot produce a partial result; use snapshot()"
            )
        if failure is not None and requested is not AnalysisStatus.ABORTED:
            raise DomainValidationError(
                "only aborted finalizations may carry a failure payload"
            )
        if failure is not None and not isinstance(failure, Mapping):
            raise DomainValidationError("failure must be a mapping when provided")
        if requested is AnalysisStatus.ABORTED:
            abort_reason = failure.get("abort_reason") if failure is not None else None
            if not isinstance(abort_reason, str) or not abort_reason.strip():
                raise DomainValidationError(
                    "aborted finalization requires a non-blank string "
                    "abort_reason in its failure payload"
                )
        frozen_failure = (
            _frozen_mapping(failure, "failure") if failure is not None else None
        )
        normalized_failure = (
            json.loads(canonical_evidence_json(frozen_failure))
            if frozen_failure is not None
            else None
        )
        if self._finalized_status is not None:
            recorded_failure = (
                json.loads(canonical_evidence_json(dict(self._failure_payload)))
                if self._failure_payload is not None
                else None
            )
            if (
                requested is self._finalized_status
                and normalized_failure == recorded_failure
            ):
                return tuple(self._final_results or ())
            raise AnalysisStateConflictError(
                f"terminal analysis conflict: persisted status is "
                f"{self._finalized_status.value} with fingerprint "
                f"{self._terminal_fingerprint}; requested status is "
                f"{requested.value}"
            )
        if (
            requested is AnalysisStatus.FINAL
            and len(self._equity_observations) != len(self._formal_sessions or ())
        ):
            raise AnalysisStateConflictError(
                "final analysis requires one equity observation for every "
                "frozen formal session; use aborted for a partial timeline"
            )
        if requested is AnalysisStatus.FINAL and any(
            not observation.is_valid for observation in self._equity_observations
        ):
            raise AnalysisStateConflictError(
                "final analysis cannot contain blocked equity observations; "
                "use aborted"
            )
        state = self._state()
        with analyzer_decimal_context():
            results = state.compute_results()
        self._final_results = results
        self._finalized_status = requested
        if frozen_failure is not None:
            self._failure_payload = frozen_failure
        self._terminal_fingerprint = compute_terminal_fingerprint(
            status=requested,
            analysis_snapshot=state,
            results=results,
            failure=normalized_failure,
        )
        return tuple(results)

    @property
    def final_results(self) -> tuple[MetricResult, ...] | None:
        return self._final_results

    @property
    def terminal_fingerprint(self) -> str | None:
        """Immutable fingerprint used to classify terminal retries."""

        return self._terminal_fingerprint

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
            fill_observations=tuple(
                self._fills[fill_id] for fill_id in sorted(self._fills, key=str)
            ),
            rate_snapshot=self._rate_snapshot,
            specs=self._specs,
            registry_snapshot=self._registry_snapshot,
        )
