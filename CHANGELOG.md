# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and official releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-09-14

### Added

- UI-managed, encrypted data-source credentials and connection validation, with
  one-time migration of legacy Tushare environment settings.
- Operational overview, searchable ingestion task and market workspaces,
  trading-calendar details, persistent ETF watchlists, and daily change facts.
- Strategy revision aliases, searchable instrument selection, and a stepped
  backtest creation Drawer shared by strategy and backtest workspaces.
- Safe permanent deletion of unpublished, unreferenced strategies and accounts
  without backtest references; historical objects retain archive/retire paths.
- Dedicated backtest results with returns, drawdown, monthly performance,
  positions, fills, diagnostics, configuration copying, and complete JSON export.
- Comparisons of 2–10 completed runs, including aligned curves, comparable
  metrics, configuration differences, reference selection, and server-persisted
  comparison names, run order, renaming, and deletion.

### Changed

- Unified twelve frontend page surfaces around shared typography, controls,
  responsive layouts, right-side creation Drawers, and unsaved-change guards.
- Improved account editing, fee rules, version history, and usage information.
- Kept existing content and empty states stable during refresh and moved success
  feedback into non-layout-shifting notifications.
- Refreshed both public READMEs, onboarding and upgrade instructions, and screenshots.

### Fixed

- Strategy-scoped PostgreSQL backtest queries and nested API response serialization.
- Stale preflight admission after configuration changes, instrument mapping
  selection, future-date entry, and dropdown/calendar consistency.
- Comparison date alignment, missing-data gaps, metric comparability, and retry
  behavior for persisted comparison changes.

### Removed

- Operational-log, standalone daily-quote, and API-documentation navigation
  entries, along with unused frontend pages and supporting code.
- Legacy `/api/admin/logs`, `/api/admin/logs/clear`, and sample `/api` endpoints,
  and the `QF_LOG_QUERY_MAX_FILES` setting. Structured log writing, ETF market
  queries, and developer `/docs` and `/redoc` remain available.

### Upgrade notes

- Back up PostgreSQL and the root `.env`, then deploy the complete v0.3.0 stack
  with `make selfhost` to apply database migrations and update Backend, Runner,
  and Frontend together. Frontend/backend version mismatches block login.
- Preserve `QF_DATA_SOURCE_ENCRYPTION_KEY` with the database. After legacy source
  settings migrate, edit them in the data-source UI rather than environment variables.

## [0.2.0] - 2026-09-07

### Added

- Tushare trading-calendar and ETF data ingestion, including daily bars,
  adjustment factors, cash dividends, and trading-status data.
- ETF search, market details, multi-period candlestick charts, moving averages,
  adjusted prices, and data coverage and synchronization views.
- A Python strategy workbench with draft editing, parameter management, static
  validation, immutable published versions, and archival.
- Backtest accounts, versioned fee plans, local ETF daily backtesting, and
  reports for equity, drawdown, performance, orders, and trades.
- PostgreSQL acceptance coverage for migrations and persisted backtest analysis.

### Changed

- Strengthened strategy execution isolation, resource limits, input validation,
  pagination integrity, and reproducibility of backtest data.
- Improved self-hosted configuration upgrades, ingestion checkpoint handling,
  and Chinese operational log presentation.
- Refreshed the Chinese and English READMEs with current capabilities,
  setup instructions, and workspace screenshots.

## [0.1.0] - 2026-08-15

### Added

- Initial public release of Quant Foundry, including the administration console,
  API authentication, structured logs, persistent task scheduling, PostgreSQL
  migrations, and self-hosted deployment tooling.
