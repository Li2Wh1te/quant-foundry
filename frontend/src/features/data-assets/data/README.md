# S2-D01 shared data exports

Import production helpers from `features/data-assets/data` (the `index.ts` barrel).
The existing `api/dataStore.ts` imports remain compatible and now validate responses.

```ts
import { dataAssetsClient, buildPreviewRequest, createRequestScope, createPreviewSession,
  currentStatus, updateStatus } from "../features/data-assets/data";

const scope = createRequestScope();
const catalog = await scope.run(signal => dataAssetsClient.loadCatalog({ signal }));
// Only catalog.complete has full derived counts; otherwise display
// “目录未完整加载” and use catalog.items as already-read metadata.
const dataset = await dataAssetsClient.getDataset(datasetId, { signal });
const current = currentStatus(dataset.status);
const update = updateStatus(dataset.last_update);
const session = createPreviewSession();
await session.read(buildPreviewRequest(dataset), { signal });
// A subsequent page must use the obtained cursor stack, never a URL/storage cursor.
await session.read(buildPreviewRequest(dataset), { page: 1, signal });
session.dispose(); // Component unmount, object change, or logout cleanup.
scope.dispose();
```

- `listDatasets(options?)`, `getDataset(dataset, options?)`, `listStatus(options?)`,
  `listIssues(dataset?, options?)`, `queryPreview(request, options?)`, `loadCatalog(options?)`.
- Request options: `signal?`, `timeoutMs?`; page options: `limit?`, `offset?`;
  status additionally accepts `state?`. Catalog defaults: 100/page, 10 pages, 1000 items.
- `CatalogSnapshot`: `items,total,phase,next_offset,complete,incompleteReason,counts,readAt,error`.
  The server total and loaded item count are distinct; incomplete snapshots have `counts=null`.
- `currentStatus`, `updateStatus`, `issueReason`, `frequencyLabel`, `datasetPresentation`,
  `formatCount`, `formatValue`, `formatTimestamp`, `errorPresentation` return display-only values.
  Formatters preserve precise text. `representation` is the descriptor layout; queries use
  `preview_key.representation`. Units, price basis, coverage and history remain undeclared.
- `createRequestScope().run(signal => request(signal))` cancels prior work and rejects late results.
  Use a separate scope for each independently refreshed area; dispose it on unmount.
  `createPreviewSession()` owns only the current page/cursors;
  `reset()` clears them on condition changes; `getSnapshot()` exposes `result,page,cursors,readAt`.
- Handle `DataStoreApiError.status===401` with the existing `useAuth().logout()` and login route.
  A 403 clears restricted content and shows a permission error. `isCancellation(error)` is not
  a data-quality failure. `invalidateDataAssetsSession()` clears sessions/cancels work on logout;
  call it alongside the existing explicit logout flow. 401/403 from this transport immediately
  invalidate other data requests and preview caches without reading their error bodies.
  Subscribe with `onDataAssetsInvalidation` to clear page-owned React data as well, and remove
  the subscription on unmount. No helper changes the project's authentication provider.
- `statusByDataset(items)` groups by dataset identity and excludes non-business status entries.
  Issue `total` counts records; `affected_objects` counts affected members without deduplication.
- Fixtures are only in `data/tests/fixtures.cjs`, never exported or imported by production modules.
  Run isolated tests with `node --test frontend/src/features/data-assets/data/tests/*.test.cjs`.

## D02 export alignment

Reviewed the actual D02 checkpoint `6369d6955b34a615323f9fde00dde930821eaeb3`
on `codex/s2-d02-workspace-shell`. The existing `api/dataStore.ts` DTO/error exports
remain compatible; the shared barrel is `data/index.ts`. No D02 files were modified.

| D02 calling area | Shared contract |
| --- | --- |
| Catalog, detail, status, issues | `dataAssetsClient`; `loadCatalog().counts` exists only when `complete` is true. |
| Current state and latest update | `currentStatus(dataset.status)` and `updateStatus(dataset.last_update)` return separate presentations. |
| Counts, timestamps, issue reasons | `formatCount`, `formatTimestamp`, `issueReason`; timestamps retain source precision. |
| Preview conditions, paging, cleanup | `buildPreviewRequest`, `createPreviewSession`; reset on condition changes, dispose on object change/unmount. |
| Refresh cancellation and logout | `createRequestScope`, `isCancellation`, `invalidateDataAssetsSession`, `onDataAssetsInvalidation`. |

Known status/frequency/reason aliases are centralized. Dataset names and source qualifiers
remain exactly as declared; unknown field semantics, units and price basis are not translated
into invented business facts. D02 owns adopting these exports in its extracted page modules.
The isolated D01 suite compiles a strict TypeScript consumer of both import surfaces;
combined UI integration remains a separate D02/D07 check.

No production endpoint was exercised by the fixtures. Current `/query` still requires the
server's global readiness gate. Per-domain readability and exact request coverage await R01's
actual contract; no frontend field, HTTP success, full page or cursor proves those capabilities.
