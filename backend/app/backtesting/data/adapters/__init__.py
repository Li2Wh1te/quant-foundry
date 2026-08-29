"""Read-only data adapters projecting stored ingestion facts onto the
generic backtesting data contract.

Adapters in this package never call external data sources, never mutate
the underlying fact tables, and never define a second identity or bar
model: they project repository rows through the PIT read path of
:mod:`app.backtesting.data.pit_history`.
"""

from app.backtesting.data.adapters.etf import (
    ADJUSTMENT_SERIES_POLICY,
    ETF_PROVIDER_KEY,
    ETF_RULE_PACKAGE,
    EtfFactsAdapter,
    build_data_preflight_payloads,
)

__all__ = [
    "ADJUSTMENT_SERIES_POLICY",
    "ETF_PROVIDER_KEY",
    "ETF_RULE_PACKAGE",
    "EtfFactsAdapter",
    "build_data_preflight_payloads",
]
