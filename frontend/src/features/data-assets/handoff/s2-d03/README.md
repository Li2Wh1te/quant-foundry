# S2-D03 data catalog

D03 development and isolated browser acceptance are complete. The catalog now
supports stable Chinese display-name sorting/search, status/source/frequency
filters, 20-item pages, URL correction and detail/back/forward scroll recovery.
Current availability and the latest update result remain independent facts.
Technical identifiers are collapsed and can be copied; unknown semantics stay
undeclared. Failed refreshes retain the previous directory and observation time.
New 401/403 responses withdraw the page-owned metadata immediately.

## Verified scope and inputs

- Independent branch: `codex/s2-d03-data-catalog`; isolated cloud worktree.
- Latest main: `4c9e8167d49ef1d6bbc49afc6578208d7a339f97`, fetched again before submission.
- Exact prerequisite commits retained in ancestry:
  D01 `1e7643a96440602575b31995fbec8c03304aa6ab` and
  D02 `9dafeb6eb052796d3b4783785feb104f1de3cf5b` (draft PR #153).
- D03 changes only `CatalogView.tsx`, `CatalogView.css`, its catalog test and this
  package's task/preview/evidence files. D01 data/API files, D02 shell/navigation,
  `DatasetView`, `FieldTable`, backend and R01 were compared with their owners'
  commits and are unchanged. No dependencies or lockfiles were changed.
- No AGENTS.md or `.agents/skills` exists in the cloud checkout. The original
  handoff's isolated-worktree, English-commit, shared-UI, honest-state and
  no-root-docs rules were retained. TASK, COMMON, API_NOTES, INTEGRATION and
  UI_RULES were read from the verified package.
- The original batch ZIP remains in the D01/D02 handoffs, SHA256
  `bcd5a001be9c5a08e47d31e19ebe4133735080fdc43d7d567171145fc9afb344`.
  All 71 manifest entries passed. The adjacent original D03 subpackage ZIP is
  unchanged, SHA256 `4982ce131f6220d982410c1233a1db1214516875a842d662c57d52419beb1561`.

## Validation actually executed

- `pnpm --dir frontend test`: **105 passed**, including **14 D03** tests.
- `node --test frontend/src/features/data-assets/data/tests/*.test.cjs`:
  **31 passed**. Combined unit/contract checks: **136 passed**.
- `pnpm --dir frontend build`: **passed**, including `tsc -b` and Vite. The
  existing large-chunk advisory remains.
- `node frontend/src/features/data-assets/handoff/s2-d03/preview/acceptance.cjs`:
  **27 passed**, **zero page errors**, all exercised API methods **GET**.
- `git diff --check` and both preview scripts' syntax checks: **passed**.

Actual Chromium acceptance covers 1440x900, 1024x768 and 390x844; 0/1/60/121
registrations; all seven pages of the 121-item directory; Chinese/native/ID
search; combined filters and repeated operations; detail/context/scroll return;
loading, empty, no match, partial/retry, changed/pagination/budget cases;
first and mid-refresh failures; maintenance/unknown facts; clipboard/keyboard;
403 clearing, 401 login, and late-response cancellation after SPA navigation.

[Acceptance evidence](screenshots/acceptance.json) records measured geometry and
the case list. Main owns vertical scrolling; only the table scrolls horizontally.
There is no document overflow at the three widths; body and labels are 14px.
The 1500ms stalled 403 body did not delay clearing (167ms in this fixture run,
not a production guarantee). Screenshots were visually inspected, including
the narrow table, expanded technical information, loading, partial/error,
retained refresh results and permission denial.

- [1440px](screenshots/catalog-1440.jpg)
- [1024px](screenshots/catalog-1024.jpg)
- [390px controls](screenshots/catalog-390.jpg) / [390px table](screenshots/catalog-table-390.jpg)
- [Partial directory](screenshots/catalog-partial-390.jpg)
- [Retained refresh failure](screenshots/catalog-refresh-error-390.jpg)
- [Permission denial](screenshots/catalog-forbidden-slow-body-390.jpg)

## Interactive replay

Install from the unchanged lockfile and run two local processes:

```sh
pnpm --dir frontend install --frozen-lockfile
node frontend/src/features/data-assets/handoff/s2-d03/preview/mock-api.cjs
QF_DEV_BACKEND_URL=http://127.0.0.1:18766 pnpm --dir frontend dev --host 127.0.0.1 --port 5179 --strictPort
```

Open `http://127.0.0.1:5179/admin/data-assets` with a synthetic session token
only (`s2-d03-isolated-fixture` in the existing `quant-foundry.api-token`
sessionStorage key). Fixture scenario controls are GET
`http://127.0.0.1:18766/__scenario?name=partial` (or another printed scenario).
Run the acceptance script with the environment's existing Playwright and
Chromium. Set `PLAYWRIGHT_MODULE`, `CHROMIUM_PATH` and `FRONTEND_URL` if needed.
No browser dependency was added to the app. Every screenshot is visibly marked
as an isolated example; the fixture binds loopback and calls no external service.

## Remaining dependencies and next-package seams

- **Real API acceptance is not complete.** The environment's configured local
  backend (`127.0.0.1:8000`) was unavailable; no authenticated live `/datasets`
  or `/datasets/{dataset}` read was performed. Fixture results prove only isolated
  UI behavior. `/query` readiness/readability remains R01/D05's separate contract.
- D01's `loadCatalog` owns its 100/page, ten-page/1000-item budget. D03 exposes
  **retry complete loading**, which restarts that bounded read rather than
  appending snapshots from different times. Beyond-budget completeness needs a
  future D01-owned continuation contract; current counts honestly stay pending.
- The current DTO does not declare a short purpose, units, price basis or
  business cutoff date. The catalog says undeclared. Local aliases translate only
  inspected server-generated native titles, preserve original names in technical
  information and keep original dataset IDs in links. A shared alias/purpose
  contract would belong to D01; D04's content was not changed.
- D02's `search/status/source/frequency/page` whitelist and return-scroll helpers
  are consumed unchanged. D04-D06 can replace their content on the same detail
  routes; no public-shell change is required. D07 should integrate this D03
  content commit on a tree containing both exact prerequisites.
- No main merge, deployment, supplier call, collection/scheduler operation,
  backend gate, security setting or credential change was performed.
