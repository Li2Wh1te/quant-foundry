# S2-D02 workspace shell — cloud continuation

Development and isolated UI acceptance are complete for the D02 shell. This
branch depends on D01; it is a draft frontend deliverable, with no main merge,
production operation or real-data acceptance.

## Verified inputs and ownership

- Repository: `https://github.com/Li2Wh1te/quant-foundry`.
- Resumed the exact D02 checkpoint `6369d6955b34a615323f9fde00dde930821eaeb3`
  from `codex/s2-d02-workspace-shell`, without rewriting the existing UI.
- Main base: `4c9e8167d49ef1d6bbc49afc6578208d7a339f97`; fetched again in cloud.
- Integrated and tested D01's stable head
  `1e7643a96440602575b31995fbec8c03304aa6ab`, including its initial
  `2de105b6be519d7bd3581503c3d8094bbfc41e76` checkpoint.
- Only the separate local integration worktree contains D01's commits. This D02
  branch commits no `features/data-assets/data/` or `api/dataStore.ts` changes.
  Those files were compared byte-for-byte against the stable D01 Git tree.
- D02 owns the route dispatch, detail shell, layout primitives, navigation and
  initial D03–D06 extraction seams. `OverviewShell` additionally highlights
  detail routes and calls D01's session cleanup alongside its existing logout.
  `App.tsx`, shared fonts, backend files and root `/docs/` are unchanged.
- No AGENTS or `.agents/skills` files exist in this cloud checkout. The original
  handoff's rules were retained: isolated branches/worktrees, English commits
  and comments, the shared Shell/fonts, honest unknown/error states, no root
  `/docs/` commits, and fresh main synchronization before push.

The adjacent original task ZIP is unchanged: **115317 bytes**, SHA256
**`bcd5a001be9c5a08e47d31e19ebe4133735080fdc43d7d567171145fc9afb344`**.
It was safely extracted outside Git; the root requirements, complete D02
requirements and manifests were read. All **71 manifest entries** matched,
including the seven complete nested packages.

## Behavior and stable seams

`DataAssetsPage` only dispatches to `CatalogView` or a dataset-keyed
`DataAssetsLayout`. The existing sidebar, quick jump, authentication routes and
visual vocabulary are retained. Scoped styles give vertical scrolling to the
existing main region; wide tables add horizontal scrolling only.

| Export | Contract / next owner |
| --- | --- |
| `CatalogView` | Bounded D01 catalog loading, existing search/status/page controls and return context; D03 owns final catalog content. |
| `DataAssetsLayout` | `{ datasetId: string }`; shared detail fetch/header/refresh, URL view and mounted same-object panels; D02. |
| `DatasetView` | `{ dataset: CurrentDataset }`; initial overview extraction for D04. |
| `FieldTable` | `{ fields: CurrentDataset["fields"] }`; initial fields extraction for D04. |
| `PreviewPanel` | `{ dataset: CurrentDataset }`; D01 in-memory preview session; D05 owns final preview content. |
| `UpdateIssuesPanel` | `{ dataset: CurrentDataset; refreshVersion: number }`; initial update/issues extraction for D06. |
| `WorkspaceHeader`, `DataSheet`, `LocalNotice`, `DatasetTabs` | Caller-provided wording and presentation; no API or business decisions. |
| `DataAssetsView` | `overview` / `preview` / `updates`. |

All extracted views now consume `data/index.ts`: typed client, request scopes,
status/formatting helpers and preview sessions. The temporary duplicate
`legacyPresentation.ts` was removed. Full catalog status counts exist only
when D01 reports completion; partial catalogs keep explicit unknown counts.

`navigation.ts` whitelists `search/status/source/frequency/page`, encodes object
identifiers and preserves list scroll. Cursors and result rows stay in memory.
Tabs support Arrow keys, Home/End, visible focus and associated ARIA panels.
View switching and browser back/forward keep the same object session; an object
change disposes it. Each request scope and invalidation subscription is removed
on cleanup, including StrictMode's mount/cleanup replay.

401 follows the existing logout/login route. D01's immediate 401/403 notification
also clears React-owned caches across mounted panels, so an issues/query denial
cannot leave a stale detail summary or preview. Ordinary refresh errors retain
previous observations with a read time. Restrictions, maintenance, generation
changes and invalid cursors withdraw prior preview rows and page stacks. Normal
preview uses `preview_key.representation` and `allow_partial: false`.

These are D03–D06 extraction seams, not completion of their task packages.
Their final catalog filters/content, overview capability explanations, preview
UX and issue pagination remain their respective responsibilities.

## Validation actually run in cloud

Dependencies were installed afresh from the committed pnpm lockfile; package
manifests and lockfiles were not changed. In the isolated D01+D02 worktree:

- `pnpm --dir frontend test`: **91 passed**.
- `node --test frontend/src/features/data-assets/data/tests/*.test.cjs`:
  **31 passed**; combined run: **122 passed**.
- `pnpm --dir frontend build`: **passed**, including `tsc -b` and Vite. The
  existing large-bundle advisory remains.
- `preview/acceptance.cjs`: **27 passed**, **zero page errors**. It exercises
  1440×900, 1024×768 and 390×844 list/detail/preview/update views; filters,
  page/scroll return; browser back/forward; keyboard Tabs and table scrolling;
  sidebar expansion memory and quick jump; loading/refresh/true empty/unknown/
  missing data/local errors; scope/object changes and delayed responses;
  maintenance/generation/restrictions; cross-panel 403 and logout.
- A 1.5-second stalled error body did not delay 403 clearing or 401 login:
  **66ms** and **50ms** respectively in this loopback run. These are fixture
  interaction observations, not production latency guarantees.
- `git diff --check` and both preview scripts' syntax checks: **passed**.

`screenshots/cloud/acceptance.json` contains the case list, measured geometry,
request methods and denial timings. There is no document horizontal overflow
at the three viewport sizes; the main region owns vertical scrolling, and no
table has a nested vertical overflow. Body typography is 14px and headings
32px. The cloud JPEGs were visually inspected, including narrow controls,
errors, focus, wrapping, and the preserved desktop sidebar preference.

The screenshots and fixture values are visibly labelled **隔离示例**. The
fixture binds only loopback and never calls a backend, supplier or production
service. The browser permits only GETs plus the read-only `/query` POST.

## Replay in an isolated integration worktree

D01 is a prerequisite for this branch. Do not build this D02-only tree as if
it already contains D01's implementation. Combine the original D01 commits in
a separate worktree; this does not merge either feature into main:

```sh
git fetch origin main codex/s2-d02-workspace-shell codex/s2-d01-api-status
git worktree add --detach ../qf-s2-check origin/codex/s2-d02-workspace-shell
git -C ../qf-s2-check cherry-pick 2de105b6be519d7bd3581503c3d8094bbfc41e76 1e7643a96440602575b31995fbec8c03304aa6ab
cd ../qf-s2-check
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend test
node --test frontend/src/features/data-assets/data/tests/*.test.cjs
pnpm --dir frontend build
```

Where the default pnpm cache is not writable, set `XDG_DATA_HOME` and
`XDG_CACHE_HOME` to a writable private directory and pass the same
`--config.store-dir=<writable-store>` for installs and runs.

Start the fixture and frontend in separate terminals of this same environment:

```sh
node frontend/src/features/data-assets/handoff/s2-d02/preview/mock-api.cjs
QF_DEV_BACKEND_URL=http://127.0.0.1:18765 pnpm --dir frontend dev --host 127.0.0.1 --port 5178 --strictPort
```

Open the environment's local forwarded port at `/admin/data-assets`, using a
synthetic login token only. Scenario controls are `/__scenario?name=...` on
port 18765; the fixture prints its available scenarios. Run browser acceptance
with the environment's existing Playwright module; set `PLAYWRIGHT_MODULE` and
`CHROMIUM_PATH` if their locations are not the defaults:

```sh
node frontend/src/features/data-assets/handoff/s2-d02/preview/acceptance.cjs
```

No browser dependency was added to the application. The cloud run used the
available Chromium with an isolated Playwright installation outside Git.

## Remaining dependencies

- **Real API integration is not complete.** Only loopback fixtures were read.
  Actual `/query` readiness/readability still awaits R01's declared contract;
  no per-domain permission, exact coverage, units or price basis is inferred.
- **No production operation was executed.** No deployment, supplier call,
  backend gate, task/scheduler change or main merge was performed.
- The full design specification remains the Library item
  `libfile_77a33bdcaeb08191a19d655b60bcfeb8`. Its authorized materialization failed
  in this environment. UI review used the verified package's UI_RULES and
  retained screenshot/style baseline; no new design system or fonts were made.
