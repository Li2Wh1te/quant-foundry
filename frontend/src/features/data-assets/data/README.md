# S2-D01 shared data exports

Import production helpers from `features/data-assets/data` (the `index.ts` barrel).
The existing `api/dataStore.ts` imports remain compatible and now validate responses.

```ts
import { dataAssetsClient, buildPreviewRequest, createPreviewSession,
  currentStatus, updateStatus } from "../features/data-assets/data";

const catalog = await dataAssetsClient.loadCatalog({ signal });
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
  Dispose the scope on unmount. `createPreviewSession()` owns only the current page/cursors;
  `reset()` clears them on condition changes; `getSnapshot()` exposes `result,page,cursors,readAt`.
- Handle `DataStoreApiError.status===401` with the existing `useAuth().logout()` and login route.
  A 403 clears restricted content and shows a permission error. `isCancellation(error)` is not
  a data-quality failure. `invalidateDataAssetsSession()` clears sessions/cancels work on logout;
  401/403 from this transport automatically invalidate other data requests and preview caches.
- `statusByDataset(items)` groups by dataset identity and excludes non-business status entries.
  Issue `total` counts records; `affected_objects` counts affected members without deduplication.
- Fixtures are only in `data/tests/fixtures.cjs`, never exported or imported by production modules.
  Run isolated tests with `node --test frontend/src/features/data-assets/data/tests/*.test.cjs`.

No production endpoint was exercised by the fixtures. Current `/query` still requires the
server's global readiness gate. Per-domain readability and exact request coverage await R01's
actual contract; no frontend field, HTTP success, full page or cursor proves those capabilities.
