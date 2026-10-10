# D01 numeric implementation contract v1.1

Trading inputs accept exact Decimal, integer or ordinary decimal strings. Python
bool and float cannot become trading amounts. JSON money/price/time/sequence are
decimal strings; quantities are nonnegative checked i64. NaN/Infinity, exponents
in JSON decimal strings, silent truncation and unsupported range are rejected.
`Decimal` objects with exponent notation are formatted exactly before parsing,
with a bounded conversion that never expands extreme exponents into huge text.
Redundant trailing zeros may be shed losslessly; caller Decimal context is unused.
JSON parameter integer representations are preserved exactly, including values
above u64 or float64 range. JSON real numbers must remain finite in Python.
Parameter containers are decoded from raw JSON so internal serde marker keys
remain ordinary object keys. This JSON-only serde feature does not change trading
Decimal's fixed storage/checked arithmetic.

`types::numeric::ExactDecimal` stores a signed 96-bit coefficient with scale 0..28
using locked rust_decimal. Redundant fractional zeros may be removed losslessly.
Exact add/subtract/multiply use bounded signed 512-bit intermediate coefficients
and discard only zeros. All current intermediates are below 2^384 (maximum scale
56, decimal coefficients below 2^96, quantities below 2^63); no intermediate can
overflow that fixed width. Final storage overflow is NUMERIC_RANGE_UNSUPPORTED.

`round`, `div_rounded`, `mul_rounded` require an explicit scale and policy.
Half-even compares the integer quotient/remainder directly, including negative
midpoints; no library default divide/round decides business precision. A rounded
value may shed redundant zeros to fit storage without changing its value.
Currency/dated fee settlement precision belongs to D02's explicit FeeConfig,
with no implicit market-rate or fee applicability defaults.

Cost totals and displayed averages use scale 12, half-even. Partial cost release
rounds `total * sold / held` once; the final sale releases the complete residual.
Display averages never overwrite the cost total. Normal 1/3 is supported with
defined rounding. `legal_quantity` computes floor of the exact integer rational
at the declared quantity step; it never rounds a target ratio up. D06/D09 must
recheck available funds, fees and dated product rules before accepting/filling.

Finite float64 is available only for research statistics (`FiniteStatistic`), not
trading input. D05/D11 own indicator/return implementations: default cost/display
precision above is shared, but D01 does not implement those business algorithms.

Real regression tests: `engine/crates/qf-core/tests/numeric.rs`; cross-language
wheel checks: `sdk/python/tests/test_contracts.py`. These are synthetic numerical
oracles, not production market/accounting acceptance.
