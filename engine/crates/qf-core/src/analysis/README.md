# D11 performance analysis

`Performance` is a constant-space consumer of the single D09 account's sampled
money and actual fills. `Engine::from_run` writes the exact initial capital,
one close for **every accepted calendar session**, optional completed Bar curves,
and one final summary. `Engine::new` remains the explicitly isolated lower-level
clock adapter and does not invent capital or an analysis policy.

Money/price/cost/fees retain checked Decimal and JSON strings. Only normalized
net value, returns, drawdown, moments, ratios and annual statistics are float64.
D05's `indicators.rs` remains the only technical-indicator implementation.

For initial equity E0 and n accepted close samples E1..En:

- Session return: Et/E(t-1)-1. The exact subtraction precedes the statistical
  float conversion. A missing close breaks both adjacent returns. It remains a
  curve point; no forward fill, zero fill or skip-and-splice occurs.
- Cumulative: En/E0-1. Annualized: expm1(log1p(cumulative)*A/n).
- Drawdown: Et/max(E0..Et)-1; maximum drawdown is a nonnegative loss fraction.
  Summary statistics use session peaks; optional Bar curves use their own
  observed curve peak and flag gaps without changing statistical frequency.
- Sample standard deviation uses ddof=1. Volatility is std*sqrt(A).
- Sharpe: (mean(session returns)-expm1(log1p(annual rf)/A))/std*sqrt(A).
- Benchmark uses the exact same origin and every close, including terminal.
  Excess is strategy cumulative minus benchmark cumulative, in percentage
  **points** (stored as fractions). A missing origin/close or shifted boundary
  makes benchmark/excess unavailable; no default benchmark is selected.
  Session curves retain the aligned exact benchmark value and cumulative
  benchmark/excess metrics. Optional Bar points explicitly leave unsampled
  benchmark metrics NA instead of carrying a prior close forward.
- Turnover: total buy+sell execution notional/(2*mean(all session-close equities)).
  The exact gross notional is also returned. Missing equity makes turnover NA.
- Trade statistics count real fills, buy/sell quantities, gross notional, fees
  and order-state notifications. These are execution fills/transitions, not a
  fabricated count of round trips. Realized P&L, dividend income/tax and trading
  fees are read directly from D09 AccountTotals; no second cost ledger exists.
  `fees_to_initial_equity` is trading fees/initial capital; dividend tax remains
  a separate exact account component. No per-round-trip win rate is invented.

A defaults to 252 and records the actual reference calendar and configured A;
this is a statistical basis, not a claim that every year has 252 sessions.
Minute/Hour/Tick event counts and optional curve frequency never multiply A.
The initial capital-to-first-close interval counts as the first session return.
An empty market source with accepted sessions can correctly have flat cash
returns; an analyzer with no session samples has `NO_SESSION_SAMPLES`.

Every unavailable Metric includes a stable reason: `NO_SESSION_SAMPLES`,
`INCOMPLETE_SESSIONS`, `INSUFFICIENT_RETURN_SAMPLES`, `MISSING_EQUITY`,
`MISSING_EQUITY_SEGMENT`, `NONPOSITIVE_DENOMINATOR`, `ZERO_VOLATILITY`,
`NO_BENCHMARK`, `MISSING_BENCHMARK`,
`BENCHMARK_INTERVAL_MISMATCH_OR_INVALID_VALUE`, `INCOMPLETE_BENCHMARK_INTERVAL`,
`NO_TRADES`, `NUMERIC_RANGE_UNSUPPORTED`, or `PARTIAL_RESULT` as applicable.
Zero volatility itself is 0; its Sharpe is NA. Zero terminal equity can give -1
cumulative return; a subsequent zero denominator still makes moments NA.

The wheel consumes an optional trusted `benchmark_origin` plus actually
published, boundary-aligned prices for an explicitly configured benchmark.
An index outside the run's market stream remains missing until D04 supplies an
accepted benchmark reader. This seam does not claim accepted production mappings.
