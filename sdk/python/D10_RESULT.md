# S3-D10 result

D03 Engine now drives Python initialize/session/Bar/Tick/timer/order/trade
callbacks through bounded safe request/reply channels. SDK commands reach the
single D06 OrderManager and D09 Account; D07 BarMatcher uses their real cumulative
FillBudget. No Python execution/account/matching fallback is involved.

The installed wheel exports the strategy API and typed immutable account/event
snapshots, mutable run-local state, immutable parameters, exact Decimal/ns,
initialize-only subscriptions/schedules, query routing, records/logs and bounded
trade pages. It reuses D05 views/indicators, callback leases and data budgets.
Prefetched events are clipped by full published keys. Materialized DataFrames
remain valid after Arrow owners release; live callback bridges/views expire.
Pure Rust execution and bridge waits release the GIL. Exceptions include bounded
source locations; cancellation/wall limits interrupt source connect and I/O.

Validation:

- `QF_S3_PYTHON=python3.12 bash scripts/test_s3_engine.sh` passed with pinned Rust
  1.90.0 and Python 3.12.2: fmt, Clippy, workspace tests, public contract, generated
  hints, release/root checks, release wheel build, separate pip consumer and all
  installed SDK regressions.
- The complete `examples/complete_strategy.py` runs in an independent `python -I`
  worker with old `app.backtesting` imports blocked. Actual Rust rotation waits
  for the sell fill; the hand oracle is three trades, cash 9897, B quantity 10,
  equity 9997 including three fixture minimum commissions of 1.
- Binding regressions cover same-time Bar barriers, exact same-ns Tick sequences,
  explicit/default subscriptions, callback query/order/cancel reentry, atomic
  targets, trade pagination, exception/Arrow cleanup, no run-context leakage,
  GIL release, caught terminal resource/data errors, logging truncation,
  daily/weekly/monthly registration, and D09 close-boundary dividend settlement.
- An independent read-only reviewer reproduced and checked fixes for worker
  module identity, IPC/connect deadlines, SyntaxError location, unknown quote
  staleness, setup cancellation state and visible run-log truncation.
  Temporary result directories/files explicitly use 0700/0600 and are checked
  in a fresh process with umask 022, including owner-drop cleanup.
- `backend/tests/test_s3_data_gateway.py` adds a required S3-CI CurrentStore →
  bounded Arrow → Rust Engine → Python query/order/cancel/notification test with
  explicit synthetic rules and stored prices. Local execution is honestly
  blocked by CurrentStore `UNSUPPORTED_FILESYSTEM` on this overlay workspace;
  its gate was preserved. CI uses the existing supported filesystem/PG setup.

Remaining integration boundaries: D08 Tick matching is not implemented; a live
Tick order reaching matching returns CAPABILITY_UNAVAILABLE. D04/D05 accepted
production mappings remain real missing capabilities, and no production data
was manufactured. D12 owns process/container supervision, noncooperative Python
termination and OS resource/security controls. Private Python attributes do not
constitute a sandbox. D13 owns durable results and evicted terminal-order lookup;
the D10 temporary indexed result owner releases files on close/drop. D11 analysis
and D18 full B1–B4 benchmarks are not claimed here.

The obsolete generated refusal-only strategy runtime was replaced and removed
from the contract generator; `.pyi` signatures continue to come from the shared
contract. No old backend core was moved or used as a fallback; its final physical
retirement remains D17. No production deploy, vendor access or cleanup occurred.
