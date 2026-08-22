"""Generic backtesting data contracts (data-contract version 1).

This package defines the provider-independent "socket standard" only: value
objects, request layering, PIT queries, generic facts, coverage and
preflight reports, consistency objects, runtime protocols, and the stable
error contract.  It contains no ``DataProvider`` implementation, no
database adapter, no chunking algorithm, and no token issuance.

Dependency direction (nothing points back up):

``errors`` -> ``requests`` -> ``facts`` -> ``reports`` -> ``protocols``

Public names are exported lazily (PEP 562): importing a single submodule
such as ``app.backtesting.data.errors`` must not drag the whole package in,
because parent-package initialization otherwise closes an import cycle
with ``app.instruments.domain`` (see ``app/instruments/references.py``).
"""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULE_EXPORTS: dict[str, str] = {
    # errors
    "ERROR_CODES": "errors",
    "DataContractError": "errors",
    "InvalidDataRequestError": "errors",
    "UnsupportedCapabilityError": "errors",
    "DataPreflightBlockedError": "errors",
    "DataPreflightConfirmationMismatchError": "errors",
    "DataSessionClosedError": "errors",
    "DataCutoffExceededError": "errors",
    "LookbackSessionsLimitExceededError": "errors",
    "IdentityMappingIncompleteError": "errors",
    "HistoryIncompleteError": "errors",
    "ConsistencyNotValidatedError": "errors",
    "ConsistencyTokenInvalidError": "errors",
    "ConsistencyTokenExpiredError": "errors",
    "ConsistencyCoverageIncompleteError": "errors",
    "ProviderContractViolationError": "errors",
    "UniverseCalendarNotPreflightedError": "errors",
    "freeze_json": "errors",
    # reports
    "PreflightIssue": "reports",
    "DataCoverageReport": "reports",
    "DataPreflightReport": "reports",
    "canonical_json": "reports",
    "canonical_hash": "reports",
    # protocols
    "DataCapabilityManifest": "protocols",
    "DataConsistencyContext": "protocols",
    "DataConsistencyEvidence": "protocols",
    "ConsistencyTokenStatus": "protocols",
    "CoverageEnvelope": "protocols",
    "DataProvider": "protocols",
    "DataSession": "protocols",
    "DataChunkSession": "protocols",
    # facts
    "FactEvidence": "facts",
    "InstrumentSpec": "facts",
    "InstrumentCodeMapping": "facts",
    "InstrumentDisplay": "facts",
    "TradingRule": "facts",
    "TradingStatus": "facts",
    "Bar": "facts",
    "Tick": "facts",
    "DataPoint": "facts",
    "AdjustedSeriesPoint": "facts",
    "CorporateAction": "facts",
    # sessions + warmup (task 02-02)
    "AuthoritativeDataSession": "sessions",
    "DataSessionState": "sessions",
    "NO_FORMAL_SESSIONS": "warmup",
    "WARMUP_CALENDAR_INCOMPATIBLE": "warmup",
    "WARMUP_COVERAGE_INSUFFICIENT": "warmup",
    "WARMUP_DEFINITION_MISSING": "warmup",
    "WARMUP_FACT_MISSING": "warmup",
    "WARMUP_HISTORY_UNRESOLVED": "warmup",
    "WARMUP_SESSION_UNRESOLVED": "warmup",
    "CoverageBoundedWarmupSessionResolver": "warmup",
    "WarmupCoverageStatus": "warmup",
    "WarmupResolution": "warmup",
    "WarmupSessionResolver": "warmup",
    "WarmupStatus": "warmup",
    "resolve_warmup_sessions": "warmup",
}

_EAGER_EXPORTS = (
    # constants live in requests but requests also defines query DTOs;
    # both are resolved lazily through the same table below.
)

_SUBMODULE_EXPORTS.update(
    {
        name: "requests"
        for name in (
            # constants
            "CALENDAR_AXIS_POLICY",
            "CHUNK_POLICY",
            "DATA_CONTRACT_VERSION",
            "MAX_LOOKBACK_SESSIONS",
            # shared helpers / value objects
            "ContractRef",
            "DateRange",
            "EffectiveDateRange",
            "LookbackWindow",
            "QueryBoundary",
            "MarketScope",
            "UniverseQueryPolicy",
            # enums
            "QualityStatus",
            "IssueSeverity",
            "InstrumentScopeMode",
            "PriceBasis",
            "ConsistencyMode",
            "ConsistencyValidation",
            "PitSupport",
            "PreflightStatus",
            "DataCapability",
            "QualityMode",
            # requests
            "DataPreflightRequest",
            "DataRequest",
            # queries
            "InstrumentQuery",
            "InstrumentMappingQuery",
            "TradingRuleQuery",
            "TradingStatusQuery",
            "UniverseQuery",
            "BarQuery",
            "TickQuery",
            "DataValueQuery",
            "AdjustedSeriesQuery",
            "CorporateActionQuery",
            "CoverageQuery",
            "DataChunkQuery",
        )
    }
)


def __getattr__(name: str) -> Any:
    """Resolve one public export lazily from its home submodule."""

    module_name = _SUBMODULE_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    # Cache so later lookups are plain attribute accesses.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_SUBMODULE_EXPORTS))


__all__ = [
    # constants
    "CALENDAR_AXIS_POLICY",
    "CHUNK_POLICY",
    "DATA_CONTRACT_VERSION",
    "MAX_LOOKBACK_SESSIONS",
    "ERROR_CODES",
    # errors
    "DataContractError",
    "InvalidDataRequestError",
    "UnsupportedCapabilityError",
    "DataPreflightBlockedError",
    "DataPreflightConfirmationMismatchError",
    "DataSessionClosedError",
    "DataCutoffExceededError",
    "LookbackSessionsLimitExceededError",
    "IdentityMappingIncompleteError",
    "HistoryIncompleteError",
    "ConsistencyNotValidatedError",
    "ConsistencyTokenInvalidError",
    "ConsistencyTokenExpiredError",
    "ConsistencyCoverageIncompleteError",
    "ProviderContractViolationError",
    "UniverseCalendarNotPreflightedError",
    # shared helpers / value objects
    "freeze_json",
    "canonical_json",
    "canonical_hash",
    "ContractRef",
    "DateRange",
    "EffectiveDateRange",
    "LookbackWindow",
    "QueryBoundary",
    "MarketScope",
    "UniverseQueryPolicy",
    # enums
    "QualityStatus",
    "PreflightStatus",
    "IssueSeverity",
    "InstrumentScopeMode",
    "PriceBasis",
    "ConsistencyMode",
    "ConsistencyValidation",
    "PitSupport",
    "PreflightStatus",
    "DataCapability",
    "QualityMode",
    # requests
    "DataPreflightRequest",
    "DataRequest",
    # queries
    "InstrumentQuery",
    "InstrumentMappingQuery",
    "TradingRuleQuery",
    "TradingStatusQuery",
    "UniverseQuery",
    "BarQuery",
    "TickQuery",
    "DataValueQuery",
    "AdjustedSeriesQuery",
    "CorporateActionQuery",
    "CoverageQuery",
    "DataChunkQuery",
    # facts
    "FactEvidence",
    "InstrumentSpec",
    "InstrumentCodeMapping",
    "InstrumentDisplay",
    "TradingRule",
    "TradingStatus",
    "Bar",
    "Tick",
    "DataPoint",
    "AdjustedSeriesPoint",
    "CorporateAction",
    # reports
    "PreflightIssue",
    "DataCoverageReport",
    "DataPreflightReport",
    # manifest + consistency + protocols
    "DataCapabilityManifest",
    "DataConsistencyContext",
    "DataConsistencyEvidence",
    "ConsistencyTokenStatus",
    "CoverageEnvelope",
    "DataProvider",
    "DataSession",
    "DataChunkSession",
    # session runtime + warmup resolution (task 02-02)
    "AuthoritativeDataSession",
    "DataSessionState",
    "NO_FORMAL_SESSIONS",
    "WARMUP_CALENDAR_INCOMPATIBLE",
    "WARMUP_COVERAGE_INSUFFICIENT",
    "WARMUP_DEFINITION_MISSING",
    "WARMUP_FACT_MISSING",
    "WARMUP_HISTORY_UNRESOLVED",
    "WARMUP_SESSION_UNRESOLVED",
    "CoverageBoundedWarmupSessionResolver",
    "WarmupCoverageStatus",
    "WarmupResolution",
    "WarmupSessionResolver",
    "WarmupStatus",
    "resolve_warmup_sessions",
]
