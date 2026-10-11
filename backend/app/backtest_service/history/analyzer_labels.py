"""Static labels of retained historical metric identities; no analyzer import."""
CONTRACTS = frozenset([('fee_summary', 1, 'cumulative_fees', 'cumulative_applied_fill_fees_v1'), ('fee_summary', 1, 'fee_to_gross_traded_notional', 'fee_to_gross_traded_notional_v1'), ('performance', 1, 'annualized_return', 'annualized_return_geometric_252_v1'), ('performance', 1, 'max_drawdown', 'max_drawdown_e0_positive_v1'), ('performance', 1, 'total_return', 'total_return_e0_v1'), ('performance', 1, 'volatility', 'volatility_simple_ddof1_252_v1'), ('sharpe_config_rf', 1, 'sharpe', 'sharpe_config_rf_ddof1_252_v1'), ('sharpe_pit_rf', 1, 'sharpe', 'sharpe_pit_rf_ddof1_252_v1'), ('sharpe_simple', 1, 'sharpe', 'sharpe_simple_ddof1_252_v1'), ('turnover', 1, 'turnover', 'turnover_gross_notional_avg_eod_equity_v1')])

def classify(key, version, metric, formula):
    if key is None or version is None:
        return "legacy"
    return "registered" if (key, version, metric, formula) in CONTRACTS else "unknown"
