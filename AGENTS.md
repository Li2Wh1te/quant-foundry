# Project Instructions

## Git Commits

- Write all Git commit messages in English.
- When no branch is specified, use the `main` branch for development by default.

## Code Comments

- Add detailed comments in English to the code.

## Self-hosted Environment Configuration

- `make selfhost` must support upgrading an existing root `.env`, not only creating it on the first deployment.
- When adding a configuration key to `backend/.env.example`, update the self-hosted environment initialization logic so that a missing non-secret key is appended to an existing `.env` with its documented default value.
- Never overwrite an existing `.env` value during this upgrade, and never generate, log, or replace user-provided secrets unless the existing value is explicitly known to be weak or invalid.
- Add or update automated coverage for both first-time initialization and upgrades from an existing `.env` whenever the initialization behavior changes.

## Scheduler Task Type Display

- A scheduler task type must have a stable machine-readable `key`, a Chinese display name, and an English display name when it is registered.
- The frontend must show a task type as `中文名（English name）`; do not expose its internal `key` in ordinary user-facing task-type selectors or labels.
- Keep the `key` unchanged when renaming a task type. It is used by persisted tasks and is not a user-facing label.

## Operational Log Display

- Keep application logs structured for querying, but every operator-facing event must include a concise Chinese `message` field that states the action, scope, and outcome in one sentence.
- For ingestion work, the message must include the data type, applicable start and end dates, and the relevant result counts (for example fetched, changed, unchanged, or failed). It must also state when a checkpoint has advanced.
- The log-query frontend must render a Chinese event title followed by the Chinese `message`. It must not expose raw internal event keys, JSON-only placeholders, or third-party English log messages as the primary user-facing text.
- Preserve technical context such as task ID, run ID, task type, error type, and stack traces as structured fields available from the expanded log detail; do not substitute these fields for the Chinese summary.
- When adding a new recurring system or third-party log event, add its Chinese title and fallback summary to the frontend event presentation mapping.
