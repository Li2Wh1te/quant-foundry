# R01 cooperative ownership and recovery

This is development support for one finite closeout episode, not a production
switchover or an acceptance receipt. Production adoption/restart requires a
separate, explicit approval of the concrete service changes and their impact.
No supplier collection, reset, forced retry, capacity change, data cleanup or
deadline extension is part of this protocol.

## Supported boundary

The protocol supports explicitly integrated **in-process** local-update owners
on one trusted Linux host with a shared ext4/XFS/Btrfs store and lock namespace.
`data_store.update_local` opts in through its `handoff_epoch` parameter. A task
without that parameter remains uncooperative on an unadopted root; once an
adoption marker exists, omitting the epoch fails closed. Every other writer,
manual maintenance entrypoint and service with resource admission must be
excluded during the episode or integrated with the same gate. The ordinary
`app.data_store` maintenance CLI is not an integrated participant.

The current legacy scheduler is **not supported**. This feature cannot pause or
adopt an already running legacy parent/child tree. It must not run beside the
legacy scheduler, even during an apparent gap: a second 16 GiB reservation plus
the existing 16 GiB reservation and 1.25 GiB headroom exceeds a 32 GiB budget and
can make the legacy scheduler's next admission fail. The global budget stays
unchanged; the gate only serializes cooperative admissions.

SIGSTOP/SIGCONT is not a handoff. A legacy parent's 900-second child timeout
continues while the parent is stopped; resuming it can terminate the child or
record a misleading timeout. File-backed stdout/stderr and a process/lock
snapshot do not prove an atomic safe point. A helper that terminates children
does not implement cooperative drain and must not be used or copied here.
Subprocess owners, detached children, cross-host coordination and recovery from
a different live PID namespace are unsupported.

## Protocol and invariants

One permanent `store.handoff` kernel writer lock spans an integrated scheduler
call, including its resource teardown and result finalization. Closeout acquires
that same lock nonblockingly. A busy owner returns `LOCK_TIMEOUT`; callers can
retry ordinary admission, retaining the original deadline. There is no forced
interruption and no guarantee of fairness under constant scheduler admission.
An integrated scheduler must return from a call only after every reservation,
transaction and store context has closed; it may not launch resource-owning
children inside admission.

The store's existing `.store-id`, device and inode bind the protocol to its
original root. An exclusive, private adoption marker and a private authoritative
`<root>/.locks/closeout-handoff.json` journal use the existing guarded path and
filesystem allowlist. Locks/markers are permanent and never removed by recovery.
Adoption writes and fsyncs the permanent marker before creating the journal. An
interruption or missing journal thereafter cannot enable an uncooperative
scheduler fallback or automatic re-adoption.

Closeout records its fence before opening the store or acquiring resources. The
journal contains the immutable adoption epoch, run UUID, ordered entries,
original absolute deadline, attempt bound, pass bound (at most 840 seconds) and
maximum passes. It also records the original process identity and current
recovery identity: boot UUID, PID, process start ticks and PID namespace. Each
attempt is durably consumed before its callback. Both attempt progress and
ownership use one journal, avoiding a split between two checkpoint authorities.
The journal is fsynced before replacement and its parent directory afterwards.
A checksum detects accidental corruption, including valid JSON with changed
counters; it does not authenticate hostile edits or make storage rollback safe.

Closeout marks release only after its work returns and every resource context
has unwound. An exception, process death or lost terminal/remote connection
leaves a persistent fence. The kernel releases its lock on death, but the journal
still blocks every integrated scheduler. There is no timeout-based release,
expiring lease, watchdog or independent coordinator whose death could authorize
overlap. Deadline expiry stops work; it never authorizes admission while an
owner is still active.

Recovery takes the same lock and requires the exact previous process to be
absent. PID reuse is checked with start ticks, never a PID alone. A missing or
malformed identity observation, different live PID namespace, live owner or
unreaped zombie blocks recovery. A different boot UUID establishes absence only
within the single-host deployment boundary. Copying journals between hosts,
restoring storage snapshots or rolling back the authoritative root is outside
this protocol; do not attempt recovery from such evidence.

Recovery preserves already consumed attempts. An interrupted attempt is not
reissued with its old number: a later ordinary continuation consumes the next
number and uses the existing source/checkpoint rules. Backoff and native source
identity remain owned by ordinary update logic. Corrupt state, mismatched
epochs/runs/budgets, stale submitted checkpoints and false acceptance flags fail
closed. An externally copied PR135 state file cannot authorize recovery. There
are at most 128 recoveries, and no path can expand quotas or delete retained work.

A released run is terminal and immutable, including cancellation, expiry and
ordinary update failure. `--resume` then returns its original result without
replaying work. A persisted terminal decision also remains terminal if its owner
died before publishing release, including a fatal result saved immediately before
the final stop reason. Recovery then releases the fence without re-entering work.
This deliberately supports **one** closeout episode per adoption
journal. It provides no rotation/reset command or migration from unowned PR135
state. A further episode requires a separately designed and approved adoption
that preserves the old evidence and finite bounds.

`processing_finished` only describes the queue. `acceptance_complete` and
`supplier_collection_executed` remain false. Full independent coverage,
formal reads and audit-export retain all existing gates.

## Separately approved legacy restart/adoption plan

Prepare a concrete service-specific plan before requesting approval; keep actual
paths, identities, scripts and evidence in private operational records.

1. Record the original deadline, exact selected scope, native continuation
   identities, attempts, capacity policy and legacy parent/child identities.
   PR135 state is not imported automatically; if finite attempt accounting
   cannot be reconciled without resets, stop and design that reconciliation.
2. Obtain approval for disabling **all** legacy admission sources and their
   restart behavior. A disconnect from a user's computer must not re-enable
   admission. No newly launched legacy job may race adoption.
3. Let the existing child finish naturally and let the legacy parent run its
   required coverage/audit finalization. If it cannot drain naturally, stop and
   request approval of a separate service-specific shutdown plan with explicit
   child and data-integrity consequences. Do not improvise signals or invoke a
   child-terminating helper.
4. Establish exclusive quiescence through the service controller that prevented
   new admission, completed child/parent exit and unchanged resource accounting.
   A one-off `ps`/lock snapshot is insufficient. Preserve retained continuation
   files and charges; do not sweep, reset or enlarge capacity.
5. Deploy the tested code with all permitted writers integrated or disabled.
   Under that externally established offline condition only, a deployment
   operator may call `HandoffGate(root).adopt(approved_epoch)` once. There is no
   automatic adoption CLI. This API records an approved boundary; it cannot
   discover or verify an uncooperative process tree by itself.
6. Configure integrated `data_store.update_local` tasks with that exact
   `handoff_epoch` and pass budgets at most 840 seconds. Verify their controller
   will not restart an old unintegrated process and excludes other writers.
   Start closeout with its original deadline and limits; preserve the run UUID.
7. On completion, perform the independent full coverage/read/audit requirements.
   Release of the admission fence is not acceptance. Restoring any legacy
   scheduler is a distinct, approved switchover after closeout no longer owns
   resources and the integrated admissions have been coordinated.

## Operating an already adopted episode

The CLI requires `--root`, `--state <root>/.locks/closeout-handoff.json`, the
approved `--handoff-epoch`, immutable `--run-id`, ordered `--entry` values,
original timezone-bearing `--deadline` and the original `--max-attempts`,
`--pass-seconds` and `--max-passes`. Signing-key delivery uses the existing
environment. There are no endpoint, credential or quota arguments.

After process loss, use those **same** arguments with `--resume`. A live owner or
busy lock blocks both recovery and cancellation; do not remove locks/journals,
reuse a PID as proof or start another scheduler to recover automatically. When
the owner is verified absent, `--resume --cancel` preserves its checkpoint and
consumed attempts, records cancellation and releases admission without opening
the current store. Recovery after deadline expiry likewise opens no database or
store resources. Exit codes are 0 for all entries done, 130 for cancellation and
2 for incomplete processing or blocked/invalid ownership.

For invalid/missing state or inaccessible identity evidence, keep admissions
disabled and retain the files for operator review. There is intentionally no
force-release or signal-based repair option.

## Validation boundary

Offline tests use synthetic process participants that exit themselves; no
production process is signalled. They cover real flock concurrency, exact
process identity/PID reuse, namespace isolation, transition interruption,
consumed attempts, failure/disconnect without a coordinator, deadline/cancel
recovery, invalid/stale state and path guards. A test-only filesystem-probe
injection follows the repository's existing flock tests and does not establish
power-loss durability or enable real writes on unsupported mounts.

The native-input CLI integration and supported-filesystem acceptance workflow
must pass on an isolated supported Linux filesystem before deployment review.
Cloud overlay/tmpfs rejection remains a real validation limitation; do not
disable it or represent an offline test as production acceptance.
