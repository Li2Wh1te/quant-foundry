<div align="center">

<h1>Quant Foundry</h1>

<p><strong>A self-hosted quantitative research workbench for individual developers</strong></p>

<p>Connect data, investment ideas, and strategy validation so individual investors can conduct systematic research.</p>

<p>
  <a href="./LICENSE"><img alt="Apache License 2.0" src="https://img.shields.io/badge/License-Apache%202.0-D22128?style=flat-square"></a>
  <a href="./compose.yaml"><img alt="Self-hosted" src="https://img.shields.io/badge/Self--hosted-Docker%20Compose-2496ED?style=flat-square&amp;logo=docker&amp;logoColor=white"></a>
  <a href="./backend/pyproject.toml"><img alt="Python 3.12.2" src="https://img.shields.io/badge/Python-3.12.2-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="./frontend/package.json"><img alt="React 19" src="https://img.shields.io/badge/React-19-61DAFB?style=flat-square&amp;logo=react&amp;logoColor=black"></a>
</p>

<p>
  <a href="#why-this-project">Mission</a> ·
  <a href="#screenshots">Screenshots</a> ·
  <a href="#what-you-can-do-today">Capabilities</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#your-first-local-backtest">First Backtest</a> ·
  <a href="#roadmap">Roadmap</a> ·
  <a href="#development-and-operations">Development &amp; Operations</a>
</p>

<p><a href="./README.md">简体中文</a> | English</p>

</div>

> [!NOTE]
> Quant Foundry is in early development. S3 uses a Rust core, Python strategies and one web/CLI/SDK run API.
> Legacy Python execution code is retired; accounts, original strategies and historical results remain.
> Formal data mappings, full D18 acceptance and the P01 production cutover are still pending.
> Module and isolated test success does not establish production readiness. Ingestion boundaries are unchanged.

## Why this project

Individual investors should be able to use quantitative tools to turn investment ideas into research
that can be inspected, tested, and improved. Quant Foundry aims to lower that barrier and gradually
narrow the gap between individuals and professional institutions in research tools and methods.

We are starting with an open-source, self-hosted workbench: collect and manage data, write strategies,
test hypotheses against historical observations, and review results through reports and run records.
Your data, private strategy source, and research results stay in your own deployment. The project code
is open so you can understand the implementation, extend it, and contribute.

The current audience is individual quantitative developers with Python knowledge who are comfortable
self-hosting. The long-term goal is to support a broader range of individual investors, multiple data
sources and asset classes, and eventually paper trading, risk controls, and live execution. ETFs are
the starting point of the implementation.

## v0.3.0

This release unifies workspace layouts, typography, controls, and creation flows, and extends the research workflow:

- Configure encrypted data-source credentials in the UI and inspect ingestion tasks and coverage.
- Name published strategy revisions, search instruments by name or code, and create checked backtests in a stepped Drawer.
- Read bounded result pages, positions, fills and diagnostics; preserve old results as read-only history.
- Compare S3 runs with their configuration differences; retain existing saved comparisons as read-only history.
- Permanently delete unpublished, unreferenced strategies and unreferenced accounts; preserve historical objects through archiving or retirement.

See the [CHANGELOG](./CHANGELOG.md) and [v0.3.0 Release](https://github.com/Li2Wh1te/quant-foundry/releases/tag/v0.3.0).

## Screenshots

**ETF market data:** browse locally stored daily bars and switch between daily, weekly, monthly, and
yearly candles, moving averages, and adjusted chart views. The screenshot shows the new workspace with isolated, generated example bars, not actual market data.

![ETF market data: candles, moving averages, and adjustment views](./assets/readme/etf-market.jpg)

<details>
<summary><strong>Strategy workbench and backtest analysis</strong></summary>

**Strategy workbench:** edit Python drafts, maintain parameters, and manage immutable published
revisions. This screenshot comes from a local demo using only the repository's public `hold` template
and sample records.

![Strategy workbench: source, parameters, validation, and revisions](./assets/readme/strategy-workbench.jpg)

**Backtest analysis:** explore returns, drawdown, monthly performance, positions, fills, and diagnostics
in a dedicated result page. This screenshot uses isolated UI test fixtures to demonstrate layout and
interaction; it does not represent an actual engine run or investment performance.

![Backtest analysis: isolated UI test data](./assets/readme/backtest-report.jpg)

</details>

## What you can do today

| Area | Implemented capabilities |
| --- | --- |
| Data ingestion | Tushare trading calendars, ETF reference data, daily bars, adjustment factors, cash dividends, and suspension/trading status; full, incremental, or recent reconciliation workflows depending on task type |
| Data exploration | ETF search and filters, persistent watchlists, reference details, candles and moving averages, forward/backward adjusted charts, adjustment-factor records, calendar coverage, and synchronization status |
| Strategy management | Python source and parameter editing, draft saving, static validation, named immutable revisions, history, archiving, and safe deletion of unpublished strategies; private source is stored in PostgreSQL |
| Accounts and execution configuration | Preserve account/fee versions and history; new runs explicitly select initial cash, authorized securities, frequency, execution and cost parameters |
| Local backtesting | S3 callbacks, a unified run service, read-only preflight, queue, cancellation, parameter batches and paginated results; unavailable formal dependencies refuse execution |
| Result analysis | S3 equity, orders, trades, positions, records and diagnostics; configuration-aware comparisons and read-only historical results |
| Tasks and operations | Persistent ingestion scheduling, manual execution and history, a consistent workspace UI, structured service logs, and Docker Compose self-hosting |

### Ingest first, then backtest locally

```mermaid
flowchart LR
    Source["External source · currently Tushare"] --> Ingestion["Ingestion tasks"]
    Ingestion --> Database["Local PostgreSQL"]
    Database --> Browse["Data exploration"]
    Database --> Preflight["Backtest preflight"]
    Strategy["Published strategy + account and parameters"] --> Preflight
    Preflight --> Runner["Independent Runner · reads local data"]
    Runner --> Results["Persisted results and reports"]
```

**Backtests use data already stored in the local database. Fetching market data from remote sources
during a backtest is not supported.** Source credentials belong to the ingestion stage. After adding
or repairing data, check coverage and rerun preflight.

### Current boundaries

- S3 code covers A-share stocks, domestic ETFs, daily/minute/hour bars and basic trade/quote ticks. The data foundation decides actual availability.
- Web, `qf backtest` and `BacktestClient` use the same `qf.backtest.v2` API; explicit initial cash is required and initial positions are empty.
- Strategies use `quantfoundry.api` callbacks. The old `run(context, parameters)` execution protocol is retired; original source is retained.
- Missing formal D04/D05 mappings and isolation prerequisites block production execution.
- Historical values are read as saved; failed/cancelled partial results and unavailable metrics remain explicit.
- Full D18 B1–B4, production runner topology, P01 cutover and C01 production cleanup remain pending.

## Quick start

### 1. Start your workbench

Install Git, Docker, Docker Compose v2, and `make`. Keep Docker running and allow network access to pull
images and install build dependencies. Self-hosting does not require Python or Node.js on the host.

```bash
git clone --branch v0.3.0 https://github.com/Li2Wh1te/quant-foundry.git
cd quant-foundry
make selfhost
```

The command initializes the root `.env`, builds images, starts PostgreSQL, applies migrations, and
starts Backend, the independent Runner, and Frontend. Existing deployments reuse their volumes,
preserve valid credentials and configured values, and receive template defaults for missing settings.

### 2. Sign in

Open [http://127.0.0.1:8080](http://127.0.0.1:8080). Copy the value of `QF_API_TOKEN` from the root `.env`
into the login page's **API Token** field. This credential grants workbench access; it serves a different
purpose from the Tushare token configured on the Data Sources page.

**Success check:** you can enter the console and open **策略工作台 (Strategy Workbench)**. Empty data
lists are expected on a fresh deployment. The current interface uses Chinese labels; this guide includes
them so the instructions match what you see.

### 3. Publish your first strategy

Open **策略工作台 → 新建策略 (New Strategy)**, enter a name, and keep the default Python template,
empty parameter schema, and default parameters. Choose **创建并进入编辑器 (Create and Open Editor)**, then
**保存草稿 → 静态检查 → 发布版本 (Save Draft → Static Check → Publish Revision)**.
Give the revision an alias describing the change so it is recognizable when selecting a backtest version.

The default strategy's core behavior is:

```python
def initialize(context):
    """Keep the empty portfolio without submitting orders."""
    pass
```

**Success check:** the strategy has a published revision and offers an entry to its backtest workspace.
This step needs no source token. It exercises editing and publishing without automatically downloading
market data or generating trades.

## Your first local backtest

### 1. Configure ingestion credentials

Open **数据源 (Data Sources)**, select Tushare, and configure its API URL and token. Testing validates
the form; saving validates it before replacing the stored settings. Leave an existing token blank to retain it.
The page never displays the stored token.

`make selfhost` supplies a missing `QF_DATA_SOURCE_ENCRYPTION_KEY`. On first startup, legacy Tushare
settings are migrated from environment variables into the database without changing the original file.
Later environment changes do not overwrite UI-managed settings. Back up the encryption key with the
database and retain it once encrypted credentials exist. Source-only deployments must configure a
64-character random hexadecimal key and apply migrations themselves.

Your Tushare account also needs access to the relevant endpoints. Having a token, endpoint permissions,
and adequate local data coverage are separate prerequisites. The project does not include a data-source
account or a ready-to-run offline market-data bundle.

Tonghuashun (同花顺) has independent API URL (default `https://fuyao.aicubes.cn`) and API Key
settings on the same page, using the existing encrypted database storage without additional
environment credentials. Apply database migrations and restart the service when upgrading.
Its connection test reads one ETF catalog entry; this does not prove access to all endpoints or
local data coverage. Tonghuashun collections remain source-local and cannot yet supply local backtest bars.
The authenticated `GET /api/data-sources/tonghuashun/interfaces` endpoint lists 94 documented
interfaces, with separate implementation and live-verification status; listing them does
not start collection.

Milestone two registers 30 independently configurable collection tasks covering ticker catalogs,
fund profiles/companies/managers, ETF forward-adjusted bars, NAV, dividends, portfolios, holders,
fund financials, A-share raw bars/actions/statements/indicators/valuations, the trading calendar,
and index catalogs/bars/current constituents/quotes. Tasks are not automatically created or enabled.
After migration, collect ticker catalogs first, then fund profiles before related company/manager
records. Verify a few subjects in the deployment environment before enabling full-market schedules.

Authenticated inspection lives under `/api/admin/data-collections/tonghuashun`: `/datasets`
includes scheduler templates; `/tickers` and `/states` expose inventory and progress;
`/{dataset}/{subject}` reads the latest saved default scope, `/{dataset}/{subject}/versions`
lists versions, and `/versions/{version_id}` pins a read to an immutable version. Explicit date
backfills have independent scope heads discoverable through `/states`. Decimal values are
returned as strings; provider fields, units, nulls and disclosure dates are preserved.
Time-series versions use bounded delta chains with periodic full anchors rather than copying
the complete history daily. Fixed-version reads reconstruct and verify the content digest.

Templates check bounded batches every ten minutes without refetching completed subjects. Daily
cutoffs are 20:00 Shanghai time, 20:30 for bars, and 22:30 plus a 09:00 follow-up for NAV.
Reference resources refresh weekly. ETF history reconciles from Sunday 03:00; other historical
collections reconcile monthly from the first day at 03:00. Bootstrap jobs use separate lower
priority schedules. A completed batch is not proof of complete market coverage. Missing reports
remain retryable, failed refreshes retain saved versions, and forward-adjustment changes require
a complete subject refetch. Current index constituents are never backdated. NAV reconciliation
is limited to the provider's rolling five-year window; older versions remain separately readable.

A-share bars support per-symbol REST and Parquet imports into source-local versioned history. Live provider acceptance is
performed after deployment. Tushare storage, the existing ETF market page and backtesting adapters
retain their original source; cross-source identity and unified data APIs are deferred.

### 2. Ingest and inspect local data

Create tasks in **采集任务 (Ingestion Tasks)** in the order below. Select task types by their displayed
Chinese/English names. You can run a task manually after creation and configure future incremental runs.

| Order | Task type | What to check afterward |
| --- | --- | --- |
| 1 | 交易日历采集（Sync Tushare trade calendar） | Configure each required exchange, such as `SSE` and `SZSE`, with a start date covering research and warmup; inspect coverage in 交易日历 (Trading Calendar) |
| 2 | ETF基础信息采集（Sync Tushare ETF basics） | Find the target ETF in A 股市场 (A-share Market), in the ETF list and inspect its details |
| 3 | ETF日线全量采集（Full Tushare ETF daily bars） | Establish history, inspect the actual date range in the ETF market-detail workspace, then schedule incremental daily-bar ingestion |
| 4 | 停牌交易状态采集（Trading status and suspension ingestion）、ETF现金分红全量采集（Full Tushare ETF cash dividends） | Fill the applicable instrument/date coverage, check run records, and use backtest preflight to determine whether the evidence is sufficient |

For the first historical trading-status collection, explicitly set `start_date` and `end_date` to cover
the backtest and warmup windows; an initial run without a date range only collects the current day.
`coverage_confirmed` defaults to false. Enable it only after independently confirming that the source
query covers the requested interval and the response is complete; a successful task alone does not
establish complete coverage evidence.

For adjusted charts, also run **ETF复权因子全量采集（Full Tushare ETF adjustment factors）** and schedule
incremental ingestion and recent reconciliation as needed. Stored factors do not enable adjusted prices
in formal backtests.

Full daily-bar ingestion derives its starting range from ETF reference data and can take time on the
first run. Confirm that ingestion succeeded and data is stored before choosing a fully covered backtest
window. Creating a schedule or seeing a chart alone does not establish backtest readiness.

### 3. Prepare the strategy, account, and instruments

Select a published S3 callback strategy and account configuration. See the
[backtest guide](./docs/backtesting/README.md) and [strategy API](./sdk/python/quantfoundry/api.pyi)
for five migration examples and CLI/SDK commands. Migration creates a new revision and keeps original source/results.
Choose the authorized universe, dates, frequency, execution model and explicit initial cash.

### 4. Check and run

Preflight the current configuration, resolve blockers and submit through the shared API.
Queue, progress, cancellation, paginated results and parameter comparisons use that same service.
Missing formal dependencies or restricted-worker topology refuse execution. Isolated fixtures do not establish production readiness.
Legacy writes return authenticated `LEGACY_ENGINE_RETIRED`; old history URLs remain read-only.
P01 owns the production cutover; this guide does not authorize deployment or user-data cleanup.

## Roadmap

These are development directions, not promised dates or a fixed delivery sequence.

| Direction | Current state | Planned work |
| --- | --- | --- |
| Data sources | Tushare ETF/calendar ingestion; independent Tonghuashun fund/A-share/index collection and version inspection | Live acceptance, bulk import optimization, then a unified data foundation |
| Asset coverage | S3 code covers A-share stocks and domestic ETFs, daily/minute/hour bars and basic ticks | Complete formal mappings and acceptance; futures are outside this scope |
| Research and backtesting | Rust core, Python callbacks, web/CLI/SDK and read-only history | Complete D18 integration acceptance and the separate P01 production cutover |
| Paper trading | Planned | Validate continuously running strategies and trading workflows |
| Risk controls | Planned | Build controls for paper and live trading |
| Live execution | Planned | Integrate execution venues, actual orders, and operational management |
| Usability | Python knowledge and self-hosting skills are currently required | Lower research and operational barriers for a broader range of individual investors |

Use [Issues](https://github.com/Li2Wh1te/quant-foundry/issues) to discuss use cases, data requirements,
and design tradeoffs.

## Upgrade from an older version

Back up PostgreSQL and the root `.env`, including the data-source encryption key. Retain existing
configuration and volumes. In your deployment checkout, run:

```bash
git fetch origin --tags
git checkout v0.3.0
make selfhost
```

Preserve local code changes first. This builds both services, applies migrations, and updates Runner.
Upgrade the complete stack: a frontend/backend version mismatch blocks login. Refresh the browser
and check service status afterward.

Legacy Tushare environment settings migrate into the database on first startup; maintain them through
**数据源 (Data Sources)** afterward. Keep `QF_DATA_SOURCE_ENCRYPTION_KEY` with the database.
This release removes the operational-log, standalone daily-quote, and API-documentation navigation
entries, the `/api/admin/logs`, `/api/admin/logs/clear`, and sample `/api` endpoints, and support for
`QF_LOG_QUERY_MAX_FILES`. ETF market queries, structured log writing, and developer documentation
at `/docs` and `/redoc` remain available.

## Development and operations

<details>
<summary><strong>Architecture and project structure</strong></summary>

```mermaid
flowchart LR
    Browser["Browser"] --> Frontend["Nginx + React"]
    Frontend --> Backend["FastAPI"]
    Backend <--> Database["PostgreSQL 17"]
    Backend --> Scheduler["APScheduler + ingestion worker pool"]
    Scheduler --> Source["Tushare Pro"]
    Scheduler --> Database
    Runner["Independent Runner Supervisor"] <--> Database
    Runner --> Worker["Strategy backtest subprocess"]
    Worker <--> Gateway["Restricted IPC data/result gateway"]
    Gateway <--> Runner
    Backend --> Logs["Structured JSONL logs"]
    Runner --> Logs
    Migration["Alembic"] --> Database
```

Frontend proxies `/api`, API documentation, and readiness requests over the private Compose network.
The scheduler inside Backend manages persistent ingestion tasks and queuing. The independent Runner
supervises backtest execution with separate concurrency and resource settings. PostgreSQL stores
collected data, strategies, account versions, run configurations, and results. Rotating JSONL logs use
a persistent volume.

```text
backend/
├── app/
│   ├── data_ingestion/     # Source clients, ingestion, checkpoints, and queries
│   ├── scheduling/         # Persistent ingestion scheduling and execution
│   ├── strategies/         # Private drafts and immutable strategy revisions
│   ├── calendars/          # Shared calendar facts and bounded queries
│   ├── backtest_service/   # S3 control API, accounts and read-only history
│   └── runner/             # Independent backtest supervision
├── tests/
└── pyproject.toml
frontend/                   # React workspace, charts, and reports
scripts/                    # Self-hosting and release tooling
tests/                      # Deployment script tests
compose.yaml                # Frontend, Backend, Runner, and PostgreSQL
Makefile                    # Deployment and validation commands
```

</details>

<details>
<summary><strong>Deployment commands and LAN access</strong></summary>

```bash
make selfhost-status            # Show service status
make selfhost-logs              # Follow service logs, including Runner
make selfhost-deploy-frontend   # Build and redeploy Frontend
make selfhost-deploy-backend    # Build, migrate, and redeploy Backend and Runner
make selfhost-restart-postgres  # Restart PostgreSQL and wait for readiness
make selfhost-psql              # Open psql
make selfhost-migrate           # Apply pending database migrations
make selfhost-down              # Stop services and preserve data
```

`selfhost-deploy-backend` stops Backend and Runner before migrations, briefly interrupting the API.
If migration fails, the script stops; fix the cause before redeploying. Back up important data before
upgrades: migrations do not automatically create backups.

By default, published Web and PostgreSQL host ports bind only to `127.0.0.1`. Backend does not publish
a host port. For LAN access, set the following in the root `.env`:

```dotenv
QF_SERVER_HOST=0.0.0.0
QF_WEB_HOST=0.0.0.0
QF_WEB_PORT=8080
```

Run `make selfhost`, then access `http://<deployment-machine-LAN-IP>:8080`. `QF_SERVER_HOST` controls
Backend's listener inside the container; `QF_WEB_HOST` controls the host-side Web binding.

`make selfhost-reset` asks for confirmation, permanently deletes the database and log volumes managed
by Compose, and redeploys. Changing an initialized database password requires updating both the
PostgreSQL role and `.env`, not just the configuration file.

</details>

<details>
<summary><strong>Environment configuration</strong></summary>

See [`backend/.env.example`](./backend/.env.example) for all settings and defaults. Self-hosting uses
the root `.env`; source development uses `backend/.env`. Relevant services must reload after changes.

| Setting | Purpose |
| --- | --- |
| `QF_API_TOKEN` | Shared Web and business API access credential |
| `QF_CURSOR_SIGNING_KEY` | Server-only signing key for backtest result cursors |
| `QF_BACKTEST_INTERNAL_TOKEN` | Internal acceptance endpoint credential; unnecessary for ordinary onboarding |
| `QF_DATA_SOURCE_ENCRYPTION_KEY` | Data-source credential encryption key; keep with the database backup |
| `QF_INGESTION_REQUEST_INTERVAL_MS` | Minimum interval between external ingestion requests; task settings can only increase it |
| `QF_DATABASE_*` | PostgreSQL connection settings |
| `QF_SERVER_*` / `QF_WEB_*` | Backend listeners and self-hosted Web addresses/ports |
| `QF_SCHEDULER_*` | Ingestion scheduler enablement, concurrency, queuing, and misfire settings |
| `QF_BACKTEST_*` | Independent Runner concurrency, queue, timeout, memory, heartbeat, and progress persistence settings |
| `QF_LOG_*` | Log directory, level, retention, and queue capacity |
| `QF_ENVIRONMENT` / `QF_DEBUG` | Environment and debugging settings |

Self-hosted initialization generates deployment secrets when missing or known to be invalid, and sets
`.env` permissions to `0600`. Subsequent upgrades preserve valid existing values and add missing settings.
Users supply their own source tokens. Do not commit `.env` or include server-only secrets in screenshots.

</details>

<details>
<summary><strong>Source development and validation</strong></summary>

Install Python 3.12.2, uv, Node.js 22, pnpm 11.19.0, and an accessible PostgreSQL instance. From the
repository root:

```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env`: set `QF_API_TOKEN`, `QF_CURSOR_SIGNING_KEY`, and the database connection settings.
The PostgreSQL user and database must already exist. Both access/signing keys need at least 32 characters.
If you are not using internal acceptance endpoints, remove the empty `QF_BACKTEST_INTERNAL_TOKEN=` line
or give it a separate valid credential.

In the Backend terminal:

```bash
cd backend
uv sync --locked
uv run alembic upgrade head
uv run python -m app
```

In a separate Runner terminal:

```bash
cd backend
uv run python -m app.runner
```

In a separate Frontend terminal:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

Vite proxies API requests to `http://127.0.0.1:8000` by default. Source-based backtesting requires both
Backend and Runner; `make selfhost` starts both automatically.

Run standard validation from the repository root:

```bash
make release-check
make test
cd frontend && pnpm test
```

`make test` runs deployment script tests, backend tests, and frontend type checking/production build.
See the [Validate workflow](./.github/workflows/validate.yml) for full CI configuration, including
PostgreSQL migrations and acceptance tests. Use a dedicated test database for database acceptance runs.

Import new database models in `backend/app/models/__init__.py`, generate and inspect the Alembic
migration, and apply it with `uv run alembic upgrade head`. Commit model changes with their migrations.

</details>

<details>
<summary><strong>API and authentication</strong></summary>

After deployment, [Swagger UI](http://127.0.0.1:8080/docs) and [ReDoc](http://127.0.0.1:8080/redoc)
provide full parameters, response schemas, and interactive API exploration.

| API area | Capabilities |
| --- | --- |
| Authentication and version | Validate an access token and query the deployed version |
| Data queries | Read locally stored trading calendars, ETF reference data, daily bars, and adjustment factors |
| Strategies | Manage drafts, static validation, revision aliases, publishing, archiving, and safe deletion |
| Backtest accounts and configuration | Manage shared accounts, fee versions and historical associations |
| Backtest runs and results | S3 capabilities, preflight, submit, cancel, batches, paginated results and comparisons; old results and saved comparisons remain read-only |
| Ingestion scheduling | Task types, schedules, execution history, and status counts |

Business APIs use `Authorization: Bearer <QF_API_TOKEN>`:

```bash
curl -i -H "Authorization: Bearer <QF_API_TOKEN>" \
  http://127.0.0.1:8080/api/auth/verify
```

`/readyz` and API documentation pages are unauthenticated. Business API calls from Swagger still require
authorization. The shared token does not provide per-user permission isolation; Web stores it in the
current tab's `sessionStorage`. Internet-facing deployments require additional HTTPS, network access
controls, and credential management.

</details>

## FAQ

<details>
<summary><strong>Can I try it without a data-source token?</strong></summary>

You can start the services, sign in, create and publish strategies, and configure accounts and tasks.
Market-data exploration and backtests on real data require ingestion first. The synthetic report
screenshot comes from project tests; the product does not currently offer a selectable offline demo mode.

</details>

<details>
<summary><strong>Why does preflight block a run when I can already see candles?</strong></summary>

Candles only establish that some market data is present. Backtests also require applicable calendars,
instrument identity, trading rules, status, and corporate-action evidence for the selected and warmup
windows. Follow the current preflight report to fill gaps or adjust configuration; blocking requirements
cannot be skipped.

</details>

<details>
<summary><strong>Where are my data and private strategies stored?</strong></summary>

Collected data, private strategy drafts and revisions, accounts, and backtest results reside in your
deployment's PostgreSQL database. Strategy source is not written to the project directory or images.
Docker Compose uses persistent volumes: `make selfhost-down` preserves them, while `make selfhost-reset`
deletes them. Database persistence is not an automatic backup.

</details>

## Contributing

Open an [Issue](https://github.com/Li2Wh1te/quant-foundry/issues) to report a problem, describe a use case,
or discuss the roadmap. Pull Requests improving ingestion, research capabilities, the interface, and
documentation are welcome. Discuss major changes to public APIs, data models, or deployment first.

1. Create a focused development branch from the latest `main`; avoid developing directly on `main`.
2. Add appropriate tests for behavior changes and complete relevant local validation.
3. Merge the latest `origin/main` before pushing, resolve conflicts, and rerun validation.
4. Describe the problem, changes, validation, and compatibility impact in the PR, then merge after the
   required CI checks pass.

## License

Licensed under the [Apache License 2.0](./LICENSE).

Copyright 2026 Quant Foundry contributors.


### Tonghuashun milestone three: local research collections

Adds 27 interfaces/tasks (57 task types and 59 implemented interfaces in total): A-share quote/auction
snapshots, anomaly reasons, popularity histories, price-limit pools and dragon-tiger lists; ETF quotes,
fund returns/drawdowns/performance, manager style/performance, news, offerings and QDII quotas;
and three A-share Parquet import workflows. No diagnostics, generic indicator queries, online backtests,
ticker search, futures or options are registered. The catalog separately identifies 12 non-public routes.

New schedules are never automatically created or enabled. Ten-minute triggers process bounded pending
batches subject to dataset-specific daily/weekly/time-slot boundaries. Selected-stock anomaly/ranking
requests require explicit symbols. QDII defaults cover only the documented examples `nazhi100` and `remen`.
Dated pools use the provider calendar, bounded backfill, five-day rechecks and atomic pagination.
News bootstrap scans at most 500 entries and retains the last 90 days; subsequent catch-up resumes an
opaque cursor after each 25-page batch. No article websites are scraped. All fields and units remain intact.

Parquet files are downloaded without API credentials or redirects, with pinned public HTTPS destinations,
and fully validated before durable per-symbol staging. Each import batch publishes up to 100 subjects;
worker restarts resume from PostgreSQL staging. Signed URLs are never persisted or logged. Limits are
2 GiB/15 minutes per download and 8 GiB/30 million decoded rows; provision temporary disk and database
space. `/api/admin/data-collections/tonghuashun/imports` reports file-level progress. Full bootstrap fills
missing dates, explicit reconciliation can update overlaps, and recent imports revise recent rows while
preserving REST updates acquired after import start. Completed staging is removed transactionally.

Configure bulk schedules before pausing or narrowing redundant full-market A-share REST schedules;
existing saved plans are never rewritten automatically. Use `make selfhost` to apply the staging migration.
No new environment settings or formal release tag are introduced. Live acceptance remains with the user;
local/CI verification uses synthetic responses, Parquet fixtures and disposable PostgreSQL.
