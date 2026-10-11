# S3 D01 shared foundation

The workspace has two production crates: `qf-core` contains checked numerics and
shared contracts; `qf-python` exposes the same validation through a CPython 3.12
extension. Versions are inherited from `workspace.package`, synchronized with
the repository's VERSION by `scripts/release_version.py`. Cargo.lock and the
hashed Python build/test requirements pin dependencies. Backend dependencies
and production entrypoints are unchanged.

Run the complete local/CI checks from the repository root:

```sh
QF_S3_PYTHON=python3.12 bash scripts/test_s3_engine.sh
```

Rustup uses `engine/rust-toolchain.toml`. The script chooses the same explicit
Python interpreter for PyO3 and the wheel; `QF_S3_BUILD_ROOT` optionally retains
the generated wheel and isolated build/consumer environments. The script builds
from `sdk/python/pyproject.toml`, installs the mixed wheel, then imports it in an
independent `python -I` process. It does not deploy or run a strategy.

Core independently compiles/runs with `cargo test -p qf-core --locked` and
`cargo run -p qf-core --locked --example public_contract`, with no Python or
database-client dependency. The public example consumes only the shared Rust
contracts plus synthetic JSON fixtures in `contracts/examples`.

`types::{numeric,keys,market}` owns Decimal, quantity, nanoseconds and event
identity. `rules`, `clock`, `data`, `orders`, `matching`, `accounting`, `analysis`,
`strategy`, `results`, `run` expose stable module paths. DataGateway, AccountPort,
Matcher, StrategyHost, RunRepository and ResultSink are ports for later modules;
no database, matching, scheduling, isolation or accounting implementation is
provided here. Arrow bytes are bounded opaque payloads; D04 owns their codec.

`contracts/run_request.schema.json` specifies shape; `run::RunConfig` validates
and normalizes supported values. Python RunConfig delegates to it. Fee overrides
are typed commission-only fields. Authentication, actual capabilities, dated
rules/fees and calendar resolution still require D13/D02 integration.

`scripts/generate_s3_contracts.py` generates Python wire hints from named Rust
DTO fields, client hints from executable Python signatures, and installed
strategy declarations/refusal functions from the public strategy contract.
`--check` runs in CI. JSON wire money/time/sequence are strings; strategy views
use Decimal/int. TypedDicts supply typing, with runtime validation in Rust.

Capabilities advertise only checked numerics/shared contracts. BacktestClient
transport and strategy operations raise CAPABILITY_UNAVAILABLE. The wheel is
an importable foundation, not a production backtesting engine.
