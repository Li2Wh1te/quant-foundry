# Run service contract v1.1

Resource root `/api/admin/backtests`; new config requires `qf.backtest.v2`.
These paths are an implementation contract, not deployed S3 endpoints.

| Operation | Method/path | Boundary |
|---|---|---|
| Capabilities | GET /capabilities | implemented engine models separate from actual data availability |
| Preflight | POST /preflight | read-only errors/warnings/normalized_config; no strategy import or run creation |
| Submit | POST / | 202; Idempotency-Key binds authenticated owner and normalized config; conflict 409 |
| List/status | GET /; GET /{run_id} | owner isolation, bounded pagination/progress |
| Cancel | POST /{run_id}/cancel | idempotent; preserve authoritative terminal winner |
| Results | GET /{run_id}/results/{kind} | summary/equity/orders/trades/positions/records/logs; bounded stable cursor pages, partial/NA explicit |
| Batch | POST /batches | 1..256 explicit configs, same strategy/account/market/cost context; independent parameters |
| Batch status/cancel | GET /batches/{batch_id}; POST /batches/{batch_id}/cancel | bounded child pages; preserve completed results |
| Compare | POST /compare | 2..16 authorized runs; expose formula/cost/date/model differences without recomputing old results |

Static routes precede /{run_id}. Authenticated legacy write schema returns 410;
unknown new schema returns 422. Existing history read URLs keep a read-only
adapter. D01 makes no routing or production changes.

RunConfig requires positive initial_cash, CNY, an empty initial portfolio and no
external capital. Account selection determines fee/rule permissions, never
implicit starting capital. JSON Schema handles shape; Rust handles dates, exact
decimal range, model/frequency consistency and limits. D13 additionally checks
authorization, actual capabilities, known dated fee facts and reference calendar.
Missing calendar may be resolved only when unambiguous and written into the
normalized configuration. Accept only small strategy/config/rule references,
never market snapshots, vendor credentials, SQL, paths, commands or images.

Statuses: queued -> starting -> running -> succeeded/failed/cancelled.
cancel_requested is a separate nonterminal request. RunRepository (Rust port)
uses current claim token/fence and lease; stale commits are rejected and one
atomic terminal/result winner is authoritative. Crash attempts restart only
after confirming the previous worker is stopped; no exactly-once execution or
arbitrary Python state restoration promise. Failure/cancel outputs are partial.

Limits: control JSON 1MiB; parameters compact sorted UTF-8 <=64KiB, root container
depth <=8 and root properties <=128; Arrow IPC <=64MiB per bounded batch; trade
pages <=10000, cursor <=4096 bytes. Credits bound ResultSink records/bytes; blocked
writers must unblock on cancel/close. Required result overflow is an error,
user logs may truncate visibly. Finish writes and recheck dependency/readability
before authoritative success. D04 owns encoding, D12 scheduling/isolation, D13
persistence/API, D14 client transport. Those ports have no D01 production
implementation; `capabilities()` advertises only numerics/shared contracts.
