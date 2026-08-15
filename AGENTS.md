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
