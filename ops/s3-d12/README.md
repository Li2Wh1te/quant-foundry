# S3-D12 runtime

The existing `python -m app.runner` entry imports only the S3 supervisor. Its
code-owned `runtime_services.create_services()` is D13's composition point and
currently refuses admission explicitly. It never restores old checkpoints or
imports the old engine. No production compose, socket permissions or credentials
were changed by D12. P01 coordinates queued/active legacy tasks and deployment.

D13 supplies the `runtime_contracts.RunRepository` methods and a
`DataRunServices(builder, result_root)`. The builder receives an accepted,
snapshotted `AcceptedWork`, current `Claim` and cancellation event and returns a
`Binding` with D04 `RunDataGateway`, its admitted `DependencyContext`, accepted
Rust specification and bounded SDK market/research routes. `prepare()` runs the
real D04 server; the worker uses its sole framing and Rust Arrow consumer.
`commit_success()` invokes D04 `gateway.finalize(context, callback)`; D13 must
atomically compare token/fence/lease/cancel and persist the result pointer using
that **same connection**. Prepare result I/O before entering this final guard.

The run-root `LocalRunRepository` is a bounded scheduling reference and isolated
integration adapter, not the D13 database/API/account/history implementation.
`flock` arbitrates independent processes; a persisted group policy rejects
supervisors with different limits. No Python checkpoint or market version is
stored. Expiry fences IPC and result submission immediately. Reservations remain
until the deterministic run container is confirmed stopped. Each attempt has a
separate token/fence and inode-bound work directory. Docker container names are
unique per run, so a late create cannot run alongside a replacement. Startup and
activity renew leases; crashes restart from the beginning, not exactly once.

`WorkerProcess` accepts only trusted `WorkRoot(runner_path, daemon_host_path)` and
a prebuilt image ID `sha256:...`. Bind sources are resolved on the daemon host;
workers mount only `<attempt>/code` and `<attempt>/ipc`, both read only, never
the work root, repository, CurrentStore, DB secrets or Docker socket. Strategy
source is frozen and verified by SHA256 in the worker, which also checks the run
identity returned by its private endpoint. REST/strategy arguments cannot set
image, command, mount or daemon path. Non-root UID/GID 65532, network none,
read-only root, all capabilities dropped, no-new-privileges, default seccomp,
private IPC/cgroup namespace, no Docker logs, capped tmpfs/shm, CPU/memory/swap,
pids/file descriptors/file size/core/CPU time and a supervisor wall deadline are
mandatory. Unsupported daemon isolation fails closed. Only the trusted runner
receives the socket through an explicitly configured deployment mapping.

Control messages are at most 1MiB, use fixed enums and have no pickle, SQL or
user-selected paths. D04 grants at most two bounded Arrow streams; no unbounded
queue is introduced. Result transfer has one acknowledged batch of at most 64
records. D11's `FileResultSink`, decoder and reader are reused in the trusted
parent. Worker spool pages transfer in deterministic partition order, retaining
within-kind ordering; parent assigns stable per-run result sequence numbers.
Summary, model assumptions, exact money/ns, partial outcomes, and upstream log
truncation survive. Results are sealed before final admission; only the atomic
repository winner publishes retained lifetime. Cancel/stale losers release their
candidate. Failure/OOM/crash always keeps a small authoritative terminal record,
even when there are no writable partial shards.

Default isolated group: at most two workers, each 1 CPU / 512MiB / 32 pids /
64MiB tmp, 32MiB file, 16MiB result, 1MiB stdout; trusted runner/gateway reservation
1 CPU / 1GiB / 64 pids, total ceiling 4 CPU / 8GiB / 256 pids. Deployments must
configure matching limits for the trusted services and DB; D12 does not grant
access or alter production settings. Python/BLAS thread counts are set to one.
Workers/IPC/code/tmp are reclaimed by exact claim/inode after a terminal outcome
or confirmed crash recovery. Retained results are business data and are never
part of this cleanup. Replaced paths, links and unknown files refuse cleanup.
No general prune or production cleanup exists in this runtime.

Run `QF_S3_BUILD_ROOT=/tmp/qf-s3-build bash scripts/test_s3_engine.sh`, then
`QF_S3_BUILD_ROOT=/tmp/qf-s3-build bash scripts/test_s3_runtime.sh`. The latter
builds from tested wheels and existing hashed dependency locks, with Docker
build networking disabled, starts a real private runner container and launches
its sibling workers. The new `S3 worker isolation` workflow runs this same route
and retains actual evidence. The existing S3 workflow additionally runs the D12
queue port tests and supported-filesystem CurrentStore/quality regressions.
Actual passed/failed scope belongs in [RESULT.md](RESULT.md); small isolation
fixtures do not claim formal mappings, full B1–B4 or production acceptance.
