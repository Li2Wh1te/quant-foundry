"""Read-only data adapters projecting stored ingestion facts onto the
generic backtesting data contract.

Adapters in this package never call external data sources, never mutate
the underlying fact tables, and never define a second identity or bar
model: they project repository rows through the PIT read path of
:mod:`app.backtesting.data.pit_history`.
"""

from app.backtesting.data.adapters.etf import (
    ADJUSTMENT_SERIES_POLICY,
    ETF_ADAPTER_KEY,
    ETF_ADAPTER_VERSION,
    ETF_VALIDATION_RULE_KEY,
    ETF_VALIDATION_RULE_VERSION,
    ETF_PROVIDER_KEY,
    ETF_RULE_PACKAGE,
    EtfFactsAdapter,
    build_data_preflight_payloads,
)
from app.backtesting.data.adjustment_policy import (
    ADJUSTMENT_ADAPTER_VERSION,
    ADJUSTMENT_SERIES_POLICY_KEY,
    ADJUSTMENT_SERIES_POLICY_REF,
    ADJUSTMENT_SERIES_POLICY_VERSION,
    INACTIVE_ADJUSTMENT_POLICY,
    AdjustmentPolicy,
    AdjustmentPolicyStatus,
    AdjustmentSeriesPolicy,
    get_registered_adjustment_policy,
    registered_adjustment_policies,
)
from app.backtesting.data.etf_adjustment import (
    NormalizedAdjustmentFactor,
    build_adjusted_price_bars,
    build_research_price_series,
    normalize_adjustment_factor,
    normalize_adjustment_factors,
)

__all__ = [
    "ADJUSTMENT_SERIES_POLICY",
    "ETF_ADAPTER_KEY",
    "ETF_ADAPTER_VERSION",
    "ETF_VALIDATION_RULE_KEY",
    "ETF_VALIDATION_RULE_VERSION",
    "ETF_PROVIDER_KEY",
    "ETF_RULE_PACKAGE",
    "EtfFactsAdapter",
    "build_data_preflight_payloads",
    "ADJUSTMENT_SERIES_POLICY_KEY",
    "ADJUSTMENT_SERIES_POLICY_VERSION",
    "ADJUSTMENT_SERIES_POLICY_REF",
    "ADJUSTMENT_ADAPTER_VERSION",
    "AdjustmentPolicyStatus",
    "AdjustmentSeriesPolicy",
    "AdjustmentPolicy",
    "INACTIVE_ADJUSTMENT_POLICY",
    "get_registered_adjustment_policy",
    "registered_adjustment_policies",
    "NormalizedAdjustmentFactor",
    "normalize_adjustment_factor",
    "normalize_adjustment_factors",
    "build_research_price_series",
    "build_adjusted_price_bars",
]
