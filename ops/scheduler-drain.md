# Finite scheduler drain for an application deployment

Use `scripts/scheduler_drain.py` from the reviewed, committed checkout. The
operator runs in the foreground on the Linux Docker host, where `/proc/locks`
must be readable before any pause. It uses the existing
backend container's authentication and task pause/resume APIs. It installs no
service, changes no scheduler implementation and never cancels or deletes runs.
Production execution requires separate approval of the exact task scope,
maintenance window, deployment version and missed-cron behavior.

## Plan and review

Use a new canonical absolute state path in a private directory owned by the
operator (0700 directory, 0600 files). Never put the state file in GitHub. The
example paths and project below are placeholders, not a production profile.

```sh
python3 scripts/scheduler_drain.py plan \
  --container APP_BACKEND_CONTAINER \
  --state /path/to/private/episode/state.json \
  --expected-active-count APPROVED_COUNT \
  --episode-seconds 7200 -- \
  docker compose --project-name APPROVED_PROJECT \
    --project-directory /path/to/deployment \
    --env-file /path/to/deployment/.env \
    -f /path/to/deployment/base.yaml \
    -f /path/to/deployment/reviewed-image-overlay.yaml \
    up -d --no-deps --no-build --pull never --wait --wait-timeout 120 backend runner
```

Planning only reads scheduler metadata. It captures the complete registered
active set, exact task versions, recurring cron definitions and digests of
parameters, plus existing accepted run identities and frozen fields. The
explicit expected count must match. Existing paused/completed tasks are not
owned. Once tasks and unregistered pending work fail closed. Inspect the private
state and emitted `plan_sha256`; review the explicit Compose profiles, current
and candidate image digests, CI for the candidate commit, network/volumes,
rollback commands and unchanged database compatibility before proceeding.
No bare `make` or generic self-host target is substituted for the approved
production profile. No build, pull or migration is performed by this tool.

Run exactly the reviewed plan:

```sh
python3 scripts/scheduler_drain.py run \
  --container APP_BACKEND_CONTAINER \
  --state /path/to/private/episode/state.json \
  --plan-sha256 REVIEWED_PLAN_SHA256 \
  --check-seconds 1800
```

The absolute UTC deadline is recorded once at actual start, not at planning,
and emitted immediately with the approved task count for operator reporting.
The episode budget cannot exceed two hours. The last five minutes are reserved
for recovery; insufficient command time stops the switch. Restarts cannot
extend the stored deadline or repeat a partially applied `run`.

## Queue and schedule behavior

Pause increments task versions and removes future cron triggers. A scheduled
enqueue racing with pause must still pass the existing ACTIVE-state check under
the task row lock. Accepted runs retain their original parameters, priority and
task/parameter versions; already queued/running work continues naturally.
State-version changes do not invalidate those copied run parameters.

Paused cron ticks are not logged as accepted work and are not automatically
replayed on resume. Original cron definitions/timezones and source collection
checkpoints are preserved. Resume recreates the next future trigger. This
behavior must be included in the approved window; do not create replacement
collection or backfill runs to disguise missed ticks.

Manual runs remain possible for paused tasks. Keep other operators from manually
running/creating/editing tasks during the window. The tool checks for new run
records, active schedules and concurrent edits at each stage; any such change
aborts deployment and attempts guarded recovery. It does not provide a global
admission lock or take over an uncooperative process. Default database observation
interval is 30 minutes; CLI permits 5–120 minutes. Clock/deadline checks during
the wait do not query production. The existing collector budgets are unchanged.

Before the command, accepted rows must still exist with all frozen fields,
queued/running counts must both be zero, no current-store locks may be held,
no unexpired dump download leases may exist and backtests must have no
queued/starting/running/cancel-requested work. Lock inode
identities are obtained in the container and checked against the host's full
`/proc/locks`, including other containers. The tool compares maintenance phase,
migration head, current file metadata, dataset generations, checkpoints, resource
policy, native range counts and pending SQLite spool hashes before/after the
explicit application-only Compose command. These are preservation checks, not
full source coverage, quality qualification or independent business acceptance.
Image/health/mount verification and formal acceptance remain required after the
normal Compose wait and must be reported separately. Maintenance is not exited.

## Failure and recovery

The private state is atomically replaced, checksummed and guarded by a
non-blocking flock. A second command on that state refuses entry; edits that
ignore the lock are detected instead of overwritten. The complete old active
set and versions are rechecked before the first mutation, and each task uses
the existing optimistic-version API. Only definite successful pause responses
create ownership receipts.

Expiry, interruption, new input, concurrent edits, preservation differences or
command failure trigger restoration of this episode's confirmed changes. Each
resume requires the exact recorded paused version and unchanged task definition.
Other users' changes, original paused tasks and completed tasks are never
overwritten. Expected state versions normally advance by two; versions are not
rewound. Recovery is bounded, with failures retained for inspection. Failure
does not cancel/delete the remaining queue or imply successful deployment.

A timeout/500 response or process loss between request and durable acknowledgment
can leave ownership uncertain. A row merely looking paused is not proof that
this operator owns it. Such intentions remain fenced as `recovery_required`;
the tool does not guess, replay an uncertain mutation (including on a later
`restore` invocation) or silently claim complete
recovery. Review the private journal and actual task history before handling an
uncertain record. SIGKILL, host loss and backend/database outages can prevent
automatic recovery; state is retained, and an explicit recovery command remains
available after deadline expiry:

```sh
python3 scripts/scheduler_drain.py restore \
  --container CURRENT_BACKEND_CONTAINER \
  --state /path/to/private/episode/state.json
```

`status` reads the same private state without contacting Docker. It refuses a
state currently held by another command. A stopped/partially applied plan is
never reset or rotated automatically. No secrets, environment contents or task
parameter values are printed/copied; metadata and their digests stay private.
The Compose argv must reference existing configuration, with no inline credentials.
