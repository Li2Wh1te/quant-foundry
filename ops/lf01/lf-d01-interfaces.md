# LF-D01 v1.1 — current-store internal interfaces

This is the callable D01 kernel, not a public endpoint, default scheduled job,
source adapter or production cutover. D02 supplies verified local source input,
source-order/completeness rules and normalized Arrow batches. D03 owns reviewed
legacy maintenance/reset. D04 registers application routes/tasks. D05 performs
final integration acceptance. No production actions are included here.

## Installation boundary

`20261005_01` is an additive migration after `20261004_02`. It adds only
`data_store_runtime`, `data_store_datasets`, `data_store_files`,
`data_store_scopes`, `data_store_issues`, and `data_store_garbage`. The runtime row
binds one catalog to one shared local root and one finite resource policy; it is
not a new orchestration service. Ordinary upgrade does not initialize a store,
register a source, run a backfill, delete legacy objects or change routes.

The revision freezes its own DDL, with no imports of the live kernel. The new
metadata is included in Alembic without replacing the existing application's
naming convention. A completely unused D01 installation may downgrade its empty
new structures; **any row in the D01 tables blocks downgrade**. Old/shared tables
are never dropped by this revision. D03 must implement reviewed populated-state
maintenance; this package does not provide a way around that guard.

Construct `CurrentStore(engine, absolute_root, cursor_key=..., initialize=True)`
explicitly on the first use after migration. The SQLAlchemy PostgreSQL engine is
owned by the caller. `initialize=False` on subsequent opens. The root must already
exist on a supported ext4/XFS/Btrfs local mount and be shared at the same trusted
location by every process/container. Keep lock files stable; never delete or
replace them to force an operation. The root and its directories must not be
world-writable. Symlinks, remote/unknown filesystems and overlay mounts are rejected
by the production policy. Use the store as a context manager; close it before
forking/recreating it in a child. No import-time I/O is performed.

## Static contracts and exact values

`DatasetSpec(name, schema, key, rule, semantics, report_prefix=0,
partitioning='single', partitioner=None)` is a code-owned contract, not a user DSL.
Specify an explicit Arrow schema and stable business-key tuple. Supported types
are string, bool, int32/int64/uint64, date32, Decimal128 (nonnegative scale, at most
38 digits), and microsecond timestamps with explicit UTC timezone. Unknown types,
floating-point business columns, Decimal256 and timezone-bearing nanosecond
Timestamp columns are rejected rather than silently converted.

Use int64 nanoseconds with field metadata `unit=epoch_ns` when event identity needs
nanoseconds; include real session/event sequence fields in keys where equal time
stamps are legitimate. D02 owns session mapping. Decimal values pass through
explicit schema/Parquet/DuckDB without float conversion. API-boundary Decimal
values are canonical exact strings (trailing zeroes may be removed); integers
outside JavaScript's exact range are strings. Schema metadata still states scale
and units. Date/time conversion is explicit, not inferred from a column name.

String fields accept `max_utf8_bytes` metadata, 1–65536 inclusive; absent bounds
mean 65536. The declared limit is validated and also bounds the number of rows
requested from native Arrow/DuckDB **before materialization**. Set a realistic
bound for short instrument codes. Bounds, metadata, keys and semantics form the
schema identity; changing units is not a compatible cosmetic edit.

The safe default partition is `default`. For multiple partitions, supply a stable
`partitioning` identity and a deterministic code-owned `partitioner(key_tuple)`.
Every written key is checked against it. A full report prefix must always route to
one partition; it can span several current files in that partition. Never use a
run UUID as partition/scope identity or silently change routing under the same ID.

`register(spec)` accepts only identical contracts or nullable-field additions.
Removing required fields, changing types/metadata/units/keys/rules requires
`register(spec, prepare_rebuild=True)`, then bounded `replace_partition` calls.
Registering the new descriptor does not delete or reinterpret the old files.
Capability reports say `rebuild_required`; incompatible reads fail until the
requested partitions are actually rebuilt. Old contracts are not kept queryable.

## Source state, submit and problems

`source_state(dataset, scope)` returns the single current source-scope state or
None. `SourceUpdate` carries `scope_key`, verified `input_token`, `context_token`,
`expected_revision` (0 for absent), bounded `confirmation` and `checkpoint`, and
`qualified`. Nested confirmation/checkpoint values are captured canonically at
construction, before any lazy input generator runs. Scope IDs represent real
ranges/objects, not invocations. Tokens are digests of actual verified input and
rule/context; completion time, container ID and arbitrary counters are not source
order evidence. D02 must retain sparse effective confirmations for sparse data;
a subject-wide maximum timestamp cannot authorize dates that it did not cover.

All submit methods require a `source_check(current_scope)` callable. It is a
**bounded, pure in-process predicate**, checked in planning and again under the
commit lock against freshly read state. Do not perform network calls, unbounded
work, or recursive store operations inside it. D02 defines comparable source
order; the kernel does not infer it. State revision is optimistic concurrency,
not a provider ordering policy. A state conflict fails with `SOURCE_CONFLICT`.

- `upsert(spec, partition, batches, source, lower=..., upper=..., source_check=...)`
  merges a half-open business-key range. Arrow batches must be strictly ordered,
  unique across batches, exact-schema, byte/row bounded and routed correctly.
  Only current shards intersecting this range are read/merged; surviving keys
  outside the replacement range within these shards are preserved.
- `replace_report(..., report_key, ..., complete=True, ...)` replaces the whole
  leading-key report object, never a partial member set. Explicit complete empty
  input removes that object's current members. D02 proves completeness; missing
  response rows by themselves do not authorize empty replacement.
- `replace_partition(..., complete=True, ...)` performs a bounded local rebuild,
  including an incompatible old format. It does not decode old bytes under new
  rules or perform a global rebuild. All input for that atomic partition must fit
  the declared limits; larger jobs must use independently defined smaller scopes.
- `record_problems(...)` atomically records an unqualified failed/processed scope
  and aggregated unresolved `Issue` objects, without opening business files or
  publishing partial reports. Repeated issues update attempts/last_seen in place.

Commit results expose generation, actual content change, idempotence, qualification,
measured metrics and `cleanup_pending`. Identical verified input/rule/context is a
no-op only while its current affected-file basis and scope/confirmation/checkpoint
still match. The lazy input iterator is not read on this path. Changed content by
another scope invalidates the old no-op basis. An identical value from a new
confirmation may advance the scope and generation without rewriting content.

`issues` and `resolved` can be passed with writes. Qualified submissions cannot
simultaneously add unresolved issues. An issue resolution must match **both the
specific issue key and its current evidence token**. A change to another member,
field or order does not clear it. `catalog.issues(dataset, scope=..., limit=...,
after=...)` returns bounded current aggregates. On issue-budget overflow the whole
operation fails explicitly; D02 must aggregate the actual affected range and, if
needed, produce a bounded error sample. D01 does not pretend a truncated sample
represents all bad input, and does not implement D02's sample generation.

## Commit and recovery protocol

One dataset writer lock covers planning. Existing readers continue during this
phase. New files use unique non-overwriting paths; writer closes and validates
them, hashes their content and fsyncs them. A bounded prepared-file intent is
registered before moving them out of scratch. Promotion is exclusive link/unlink
with both directories fsynced; file names are never reused for replacement.

Under the short exclusive commit lock, one PostgreSQL transaction rechecks the
current generation, source CAS/predicate and cancellation, updates **only changed
file entries**, issue state, source checkpoint, counts and generation. The commit
loop has a finite deadline and statement/lock limits. A timeout/cancellation before
commit rolls back all these current facts together. No release root, parent chain,
normal-row assessment/decision or permanent input list is created.

If acknowledgement is lost, `last_commit` is checked while both writer and commit
locks still prevent a successor from changing this sole current marker. An
unreachable confirmation becomes `COMMIT_UNKNOWN`. Do not replay blindly or delete
new files. `recover(dataset)` sweeps only inactive scratch slots, performs bounded
unreferenced-file cleanup, and returns authoritative current counts/generation.
The caller rereads `source_state` before resubmitting. No old execution archive or
historical ID is needed. Cleanup failure cannot relabel a committed write as a
failed write; `cleanup_pending` reports remaining work.

## Reads and dependency coordination

`Query(partitions=('default',), lower=None, upper=None, columns=(), page_size=1000,
cursor=None, require_qualified=True)` accepts only declared partitions, typed bounds
and declared columns. Arbitrary SQL and paths are not inputs. `release` or `snapshot`
arguments always raise `HISTORY_UNSUPPORTED`, never fall back to current data.

`read(spec, query)` reads explicit catalog-listed file descriptors under a shared
read lock, verifies their schema/length/hash, and returns `Page`. The current store
cannot query files merely because they are in its object directory. `Page.to_dict()`
is the exact internal transport contract for D04; **no public HTTP route is
registered by D01**. Its timestamp/Decimal/large-int values are JSON-safe without
precision loss. Tests exercise it through an actual isolated HTTP server.

A signed cursor binds query, bounds, schema/rule, last business key and the current
generation vector. On any change, the next page raises `DATA_CHANGED`. Discard the
partial client result and start again. There is no old-generation query.

`read_many([(price_spec, price_query), (factor_spec, factor_query)],
expected_generations=..., cancelled=...)` acquires all dependency read locks in a
fixed order and keeps them until every page is constructed. Cursors include all
dependency generations. If a required dependency changes, times out, exceeds a
budget or fails validation, the request returns no partial successful bundle.
This is a bounded consistent read, **not** an all-dataset atomic write transaction
or a guarantee of domain-specific semantic compatibility; D02/D04 validate that.

Unresolved issues conservatively restrict a dataset. Normal reads fail with
`DATA_RESTRICTED`; a controlled internal diagnostic read can set
`require_qualified=False`, but every returned page then explicitly reports
`quality_status='restricted'` and its current unresolved issue count. D04 must not
use diagnostic reads as a qualified business-data bypass. Range-aware interpretation
of specific domain issues belongs to D02/D04, not a generic DSL in this kernel.

## Shared resource and retention limits

Defaults are in `StoreLimits`, including 4 GiB total active staging+DuckDB spill,
1 GiB minimum free margin, one concurrent writer, 512 MiB native DuckDB memory,
two native threads, 2 GiB measured process RSS ceiling, 30-second query and
300-second write limits. Writes have bounded input/output rows/bytes and affected
file count; queries have scan/file/partition/row/output caps. Native DuckDB runs
in-memory, with bounded spill and automatic extension install/load disabled.

A finite shared flock-slot pool reserves the same aggregate scratch budget for
all store processes. Inactive crash leftovers must be cleaned before admission;
active slots cannot be swept. Arrow sinks check each allocation; native reads use
contract-derived byte-bounded record batches. A watchdog interrupts native queries
on time/cancellation/RSS/disk/temporary-space pressure. These are admission and
measured runtime controls, not a replacement for deployment cgroup limits or a
promise that arbitrary adapter Python code can be forcibly interrupted.

`cleanup(dataset, limit=...)` checks exact queued paths, writer/commit locks,
reference status, grace and retry state. Failed deletion retries with bounded
backoff. Queue count and bytes are capped; pressure blocks new writes. Current
files, active scratch, unlisted foreign files and source originals are not deleted.
`prune_summary_logs(store)` also applies age limits when there have been no new
writes. Successful/error batch summaries share one 256 MiB / seven-day cap and one
multi-process rotation lock. No record payload is logged.

`prune_completed_runs(catalog)` selects only terminal `data_store.update`,
`data_store.rebuild` and `data_store.retry` task types, with both a 30-day and
1000-per-task limit. Running tasks, unresolved issues and other task types remain.
D04 must register these names and invoke maintenance; D01 does not start timers.

## Isolated acceptance commands

Use the repository's locked Python environment and an explicitly disposable local
PostgreSQL `*_test` database. Never point these commands at production. Test fixtures
create unique schemas/databases and remove only those that they created.

```sh
cd backend
uv sync --locked
POSTGRES_TEST_ENABLED=1 QF_ENVIRONMENT=test uv run pytest tests/test_data_store_*.py -q
```

Configure the normal QF test database variables outside the command without
publishing real connection strings. For B01–B04, from the repository root:

```sh
PYTHONPATH=backend uv run --project backend python scripts/benchmark_current_store.py \
  --root /absolute/trusted/local-test-root --output /absolute/new-benchmark-result.json
```

The benchmark streams generated synthetic bars in bounded batches; it never reads
paid/provider data. Its optional `--allow-overlay-test` mode is solely a declared
restricted-development mechanics run, not supported-filesystem acceptance.

Real cross-container and supported-filesystem tests are runnable with:

```sh
python3 scripts/check_current_store_containers.py \
  --root /absolute/empty/trusted/local-test-root \
  --output /absolute/new-container-result.json --with-suite
```

This uses a unique isolated Compose project, no host database port, no production
environment file and an internal network. Only its exact test container names and
test database volume are removed. The checked-out source's dedicated
`current-store-acceptance.yml` runs this harness without filesystem injection.
**The delivery result records whether these commands actually ran; the presence
of this harness or a workflow file is not a passed acceptance result.**

A capability state of `empty` means the current directory contains zero business
rows. It is not evidence that a provider returned a complete empty response or
that every planned source range has been processed. D02/D04 must use the explicit
source-scope completeness and qualification state for those conclusions.
