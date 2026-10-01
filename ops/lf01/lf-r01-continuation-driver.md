# Finite R01 continuation driver

The versioned entry point is `python -m app.data_store.closeout`. It performs
ordinary bounded local updates of explicitly selected business entries. It
does not initialize a store, collect supplier data, force retries, rebuild,
delete issues, change capacity, or certify production acceptance.

## Why a separate driver

A `RETRY_BACKOFF` result means the native scheduler intentionally deferred an
attempt. It does not mean a job is finished or stuck. The driver keeps that job
until `refresh.next_retry_at`, permits other entries to advance, and waits
without spending the job's attempt budget. Native checkpoints remain the
source of truth. Real cancellation and shared storage errors stop the driver.

The driver runs the existing update API directly with a cooperative per-call
budget of at most 840 seconds. There is no competing 900-second subprocess
timeout that converts a normal budget boundary into SIGTERM. External service
managers must allow graceful shutdown; an actual termination remains a stop,
never a successful continuation or a false complete result.

## Explicit invocation and handoff

Supply an existing trusted current-store root, a new private state-file path,
one or more explicit `--entry` arguments, and the **original** absolute
`--deadline` with timezone. Runtime values and credentials belong to private
configuration, not this repository. `QF_CURSOR_SIGNING_KEY` and the configured
database connection are read from the already configured application runtime.

Optional bounds are `--pass-seconds` (1–840), `--max-attempts` (1–128 per entry),
and `--max-passes` (subject to the existing pipeline limit). Resume with
`--resume` and exactly the same root, entries, deadline and bounds. A resume
cannot reset the original processing window or lost-attempt count.

Before a production handoff, verify the currently active worker and its entry
ownership. Do not start a competing driver for entries it still owns. A prior
driver may have dropped a deferred entry; confirm its native state and expired
backoff before explicitly selecting it here. Selected-partition spool batches
must retain their exact partition identity and be handled by the existing
`retry --partition` workflow; this driver does not reinterpret those batches.

Private batch scripts and evidence are not copied into this repository. This
entry point replaces reusable local-update continuation logic. Source repair
and final acceptance are separate explicit steps, not automatic side effects.

## Evidence and completion

State records per-entry attempts, due time, disposition and reason, and is
replaced atomically after each transition. `blocked` and `limited` entries
remain visible; a finite run stopping is not equivalent to success. Exit 0
means all selected local updates returned both complete and qualified; 2 means
incomplete/error; 130 means cancellation. `acceptance_complete` is always false.

Keep the full source/business denominator. After eligible local processing,
run the existing `verify-coverage`, formal read checks and independent
`audit-export` gates. Missing source inputs or failed supplier collection
remain unresolved; never exclude them or relax validation to pass closeout.

Source diagnostics now report only a validated date, fixed field name and
locally defined invalid-value category. Known vendor business codes are
preserved without vendor text, raw values, connection details or credentials.
These diagnostics improve the next approved collection's evidence; they do
not themselves recover a failed source or authorize a new vendor request.
