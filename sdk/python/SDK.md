# S3 D10 Python strategy SDK and Rust worker binding

The installed CPython 3.12 wheel implements strategy callbacks, scheduling,
subscriptions, read-only context/account snapshots, research queries, orders,
cancellation, bounded trade pages, record and logs. Rust's D03 Engine is the
sole driver; D06 OrderManager and D09 Account are the only order/account owners.
Bar runs consume D07 BarMatcher and the actual cumulative account FillBudget.
Tick callbacks preserve every event identity, including distinct sequences at
the same nanosecond. D08 matching remains unavailable: a live Tick order reaching
a matching event fails with CAPABILITY_UNAVAILABLE, rather than inventing a fill.

`examples/complete_strategy.py` demonstrates initialization, before/after session
callbacks, subscriptions, a daily timer, history/current queries, a cancelled
limit order, atomic targets, order/trade notifications and cash/equity records.
Its rotation waits for the A sale to execute before submitting the B buy. The
installed-wheel test runs this file in a separate `python -I` worker process,
with a named synthetic Arrow peer and old `app.backtesting` imports blocked.
The independent hand oracle is cash 9897, B quantity 10, equity 9997, three real
Rust trades after three minimum commissions of 1. These are fixture fees and
synthetic calendar/market data, never production acceptance.

```sh
QF_S3_PYTHON=python3.12 bash scripts/test_s3_engine.sh
```

Strategy code imports `quantfoundry.api`. `Context.state` starts as a namespace
and can be replaced with any run-local Python object. `params` is recursively
immutable. `portfolio`, orders, trades and quotes are immutable, materialized
snapshots; mutating them cannot change the Rust account. `now_ns` is an exact
integer; `current_dt` is a timezone-aware display with microsecond precision.
Prices and money remain Decimal, order money refuses floats/bools, and record
alone accepts finite float statistics. There is no arbitrary state recovery.

Without explicit subscribe, the module's matching handler subscribes to the
run universe. Explicit initialize-time subscriptions merge/deduplicate and
replace the defaults. Bar boundaries generate one aggregate handle_data call.
Timers retain D03's exchange calendar, trading-day ordinals and registration
order. Daily inputs refuse intraday clock scheduling. Commands reserve/release
synchronously without advancing the clock; notifications drain nonrecursively.

The safe binding uses bounded Rust request/reply channels. A scoped engine host
serves commands while the Python callback runs on the calling interpreter
thread; no Rust mutable borrow is stored in a Python object or held on that
thread across Python execution. Pure Rust computation and waits release the
GIL. The bridge lease and D05 ReadView expire in finally paths, including errors
and cancellation. User DataFrames own their clipped values; they may survive
without retaining an Arrow input pointer. Raw prefetched Arrow is never exposed.
D05 `data/views.rs` and `analysis/indicators.rs` keep their existing roles.

The native event stream and explicit Python research requests share D04's
run-bound IpcGateway framing and connection. Current quotes use Rust's bounded
published-event map; they need no parent query per Tick. Dependency checks occur
around bounded source reads, explicit queries and before success. Result files
contain business records only, indexed on disk; get_trades reads at most the
requested page (1–10000), also bounded to the control-message byte allowance.
Cursors identify this result owner and an already emitted trade sequence.
Result.close (or owner drop) releases the private temporary files. This local
writer is the D10 worker seam; D13 still owns durable trusted result persistence.
The summary's `logs_truncated` reports both message clipping and logs omitted
after the run allowance, including an exact boundary with no clipped message.

The complete input/view/cache/IPC allowance is allocated from the trusted
worker spec's data budget. Orders/commands, result bytes, records, log bytes and
wall clock have actual runtime limits. A caught RESOURCE_LIMIT, DATA_CHANGED,
RESULT_BUDGET_EXCEEDED or CANCELLED still prevents success. CancellationToken
interrupts Rust work, requests and I/O; D12 must terminate noncooperative Python
code and enforce process-wide CPU/memory/pids/output/network/filesystem limits.

The low-level `_worker`/native wiring seam is for a supervisor-owned worker and
isolated tests. Importing it executes no strategy. Its standalone CLI imports
strategy code only in that worker, and currently admits only `--isolated-test`
with an explicitly named synthetic model. It refuses production admission until
D04's actual accepted calendar/rules/actions/data mappings and D12's isolation
are connected. Research mappings continue to refuse missing capabilities;
there is no supplier request, raw-source fallback or fabricated production data.
Private Python attributes are not a security sandbox against reflection/native
extensions. BacktestClient submission/CLI/REST scheduling and durable result
persistence retain D12–D14 ownership. D11 analysis, D18 full B1–B4 benchmarks,
production switching and cleanup are outside this D10 delivery.
Each worker permits one run at a time; concurrent runs use independent workers.
The standard and NumPy random generators are seeded before strategy import.
