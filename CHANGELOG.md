# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and official releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
