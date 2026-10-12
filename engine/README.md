# S3 Rust engine

The workspace has two production crates. `qf-core` owns the event clock,
market rules, research views and indicators, orders, Bar/TradeTick/QuoteTick
matching, accounting, session analysis and bounded business results.
`qf-python` binds those implementations to CPython 3.12 and calls real Python
strategy callbacks. Rust remains the sole simulation/account driver.
Versions are inherited from `workspace.package`, synchronized with the
repository's VERSION by `scripts/release_version.py`; Cargo.lock and hashed
Python requirements pin dependencies.

Run the core and installed-SDK checks from the repository root:

```sh
QF_S3_PYTHON=python3.12 bash scripts/test_s3_engine.sh
```

Rustup uses `engine/rust-toolchain.toml`. The script chooses the same explicit
Python interpreter for PyO3 and the wheel; `QF_S3_BUILD_ROOT` optionally retains
the generated wheel and isolated build/consumer environments. The script builds
from `sdk/python/pyproject.toml`, installs the mixed wheel, then imports it and
runs SDK/strategy regressions in an independent `python -I` environment.
It does not deploy production services.

Core independently compiles/runs with `cargo test -p qf-core --locked` and
`cargo run -p qf-core --locked --example public_contract`, with no Python or
database-client dependency. The public example consumes only the shared Rust
contracts plus synthetic JSON fixtures in `contracts/examples`.

`types::{numeric,keys,market}` owns Decimal, quantity, nanoseconds and event
identity. `rules`, `clock`, `data`, `orders`, `matching`, `accounting`, `analysis`,
`strategy`, `results`, `run` expose stable module paths. Arrow codecs and
streaming merge use bounded buffers and preserve nanoseconds and event
identity. Bar boundaries publish all completed securities before a Python
callback; Tick callbacks retain every accepted event. Money and prices remain
Decimal, and account/cost rounding follows the shared numeric policy.

The backend supplies CurrentStore admission, database claims, result
persistence and a trusted supervisor at `python -m app.runner`. That supervisor
starts one constrained sibling worker per run through the configured Docker
daemon. The worker has run-specific read-only code/IPC mounts, no network,
database credentials, Docker socket or complete market directory. The old
Python execution packages have been physically removed; historical business
data remains available through the shared read-only layer.

`contracts/run_request.schema.json` specifies shape; `run::RunConfig` validates
and normalizes supported values. Python RunConfig delegates to it. Fee overrides
are typed commission-only fields. The service authenticates requests and checks
actual capabilities, accepted calendars and dated rules/fees; unresolved formal
projections remain explicit refusals.

`scripts/generate_s3_contracts.py` generates Python wire hints from named Rust
DTO fields, client hints from executable Python signatures, and installed
strategy declarations/refusal functions from the public strategy contract.
`--check` runs in CI. JSON wire money/time/sequence are strings; strategy views
use Decimal/int. TypedDicts supply typing, with runtime validation in Rust.

`BacktestClient`, `qf backtest` and the website use the same v2 service.
Installed engine capabilities and actual data availability are distinct.
The default `FormalProvider` still refuses execution: accepted stable
identity/session, units/raw prices and dated rule/research mappings are not
connected. Synthetic integration does not close that gap or authorize
production switching.

See the [client guide](../docs/backtesting/README.md),
[strategy SDK](../sdk/python/SDK.md) and
[integration/release commands](../ops/s3-d18/README.md).
[D18's concentrated result](../ops/s3-d18/RESULT.md) and the exact-commit CI
artifacts distinguish full fixed benchmarks, smoke fixtures, native release
validation and remaining dependencies. Historical module RESULT files describe
their own delivery point and do not replace final integration acceptance.
