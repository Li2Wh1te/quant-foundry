# S2-D02 cloud checkpoint

This checkpoint stops new implementation at the coordinator's cloud-migration instruction. It is an unfinished frontend workspace-shell change, with local verification and replay materials. It must not be merged or deployed without coordination.

## Repository and scope

- Repository: `https://github.com/Li2Wh1te/quant-foundry`
- Development branch: `codex/s2-d02-workspace-shell`
- Base: latest fetched `origin/main`, `4c9e8167d49ef1d6bbc49afc6578208d7a339f97`. The package's compiled baseline is not a rollback instruction.
- Isolated Git copy and worktree were used. R01's workspace and the production checkout were not edited; no backend, supplier, production, secret, gate, or scheduler operations were performed.
- Ownership: `DataAssetsPage` route dispatch; necessary `OverviewShell` navigation wiring; `features/data-assets/` layout primitives and initial D03-D06 content extraction. `App.tsx` already has the required routes and is unchanged.
- `frontend/src/api/dataStore.ts`, `features/data-assets/data/`, backend files, shared fonts, and root `/docs/` are unchanged. No other execution sessions were opened.
- Local repository instructions require English commits, shared Shell/fonts, retained content on refresh, explicit unknown/error states, isolated development branches, and fetching/merging current main before push. The full design specification is local-only and must not be committed under `/docs/` or copied elsewhere in Git as a workaround.

## Saved behavior and module seams

`DataAssetsPage.tsx` dispatches to `CatalogView` or a dataset-keyed `DataAssetsLayout`. The shared Shell highlights Data Assets on both list and detail routes. CSS moved from `pages/DataAssets.css` into scoped `features/data-assets/DataAssetsLayout.css`, retaining the existing visual vocabulary. Tables scroll horizontally; the existing main region owns vertical scrolling.

Feature `index.ts` exports:

| Export | Current contract / future owner |
| --- | --- |
| `CatalogView` | Existing list fetch, filters, 20-item client page, refresh, return context; D03 replaces/finishes catalogue content. |
| `DataAssetsLayout` | `{ datasetId: string }`; shared object fetch, header, refresh, URL `view`, same-object mounted panels. D02 owns this shell. |
| `DatasetView` | `{ dataset: CurrentDataset }`; initial overview extraction for D04. |
| `FieldTable` | `{ fields: CurrentDataset["fields"] }`; initial fields extraction for D04. |
| `PreviewPanel` | `{ dataset: CurrentDataset }`; initial preview/cursor extraction for D05. |
| `UpdateIssuesPanel` | `{ dataset: CurrentDataset; refreshVersion: number }`; initial update/issues extraction for D06. |
| `WorkspaceHeader`, `DataSheet`, `LocalNotice`, `DatasetTabs` | Layout-only components; callers supply wording and availability. No API or business rules in these primitives. |
| `DataAssetsView` | `overview` / `preview` / `updates`. |

`navigation.ts` whitelists list context (`search`, `status`, `source`, `frequency`, `page`), encodes dataset IDs, excludes preview cursors/credentials/raw values, and preserves optional per-list scroll. Tabs support Arrow keys, Home/End, ARIA associations, and one tab entry point. Switching views preserves same-object inputs and preview pages; changing dataset unmounts and cancels the previous panels.

Requests still use the original `dataStoreApi` DTOs. D01's client has not been adopted or cherry-picked. `legacyPresentation.ts` is a temporary extraction of the existing page's presentation functions, to be reconciled with D01's `data/index.ts` in coordination. Existing 401 logout/login behavior remains; 403 clears restricted content and shows a local permission failure. Normal preview uses `preview_key.representation` and hardcodes `allow_partial: false`; the old ordinary partial checkbox was removed for compatibility with D01's confirmed contract.

## Verification completed and limits

- Frontend tests: **91 passed** (85 existing + 6 shell/navigation tests).
- `tsc -b` and Vite production build passed. The pre-existing large-bundle warning remains.
- `git diff --check` passed. Reviewed changed code, fixtures, images, and original requirement archive; no real credentials or user business data were added. Test token/cursor strings are literal sentinels only.
- Tests/build used a copied, isolated dependency directory from the available Mac checkout, without changing dependency manifests or lockfiles. pnpm's dependency auto-install check was disabled for these runs because the host executable was not on PATH. Cloud must install dependencies from the committed lockfile and rerun checks.
- Loopback-only fixture UI at **1440x900**: list, detail overview, current-data preview, cursor next page, view switching and state preservation, keyboard view navigation, filtered page-2 return with scroll restoration, expanded/collapsed sidebar.
- **1024x768**: catalogue screenshot, existing expanded sidebar, computed title/body typography, no document horizontal overflow or nested vertical table scroll. Detail-link navigation was checked with Enter; the full detail-state matrix is unfinished.
- Screenshots use visibly labelled **隔离示例** synthetic values. They prove local UI behavior only; no production API or permission success is claimed.

Pending for the cloud task:

1. Finish **390px** screenshot/acceptance and all affected views at 1024px; exercise loading, refresh, true empty, local errors, 401/403, dataset change, maintenance/generation changes, and sidebar quick jump.
2. Align with D01's coordinator-confirmed remote checkpoint. Latest relay: local D01 commit `2de105b6be519d7bd3581503c3d8094bbfc41e76`, branch `codex/s2-d01-api-status`; its push was awaiting explicit authorization, so do not assume that commit is available remotely. Read its actual handoff/README after it is made accessible.
3. Reconcile shared presentation/client exports without editing D01-owned `data/` or `api/dataStore.ts`; D03-D06 own final catalogue, overview/fields, preview session safety, and update/issues behavior. Initial modules here are extraction seams, not acceptance of their future packages.
4. Complete full-package acceptance, fresh-lockfile validation and review. Missing APIs and coverage must remain unknown/restricted. No backend/production/vendor work, merge, or deployment is authorized by this checkpoint.

## Complete original task package

The adjacent `Quant_Foundry_S2_Batch1_Small_Tasks_v1.0.zip` is the user's complete original archive, copied byte-for-byte rather than reconstructed from D02 requirements.

- Size: **115317 bytes**
- SHA256: **`bcd5a001be9c5a08e47d31e19ebe4133735080fdc43d7d567171145fc9afb344`**
- Contains the root README / COMMON / API_NOTES / UI_RULES / INTEGRATION / SOURCES / manifests and all seven nested task packages.
- The original was safely extracted with path, symlink and size checks; unknown archive scripts were not executed. Root requirements and all D02 requirements/manifests were read. Cloud should verify this hash, safely extract, and read those same requirements before resuming.

The full design specification is saved separately in the user's Library:

- Filename: `Quant_Foundry_Design_Spec_01-06.md`
- Library identity: `libfile_77a33bdcaeb08191a19d655b60bcfeb8`
- File identity: `file_0000000018848207b37be72281458e7e`
- Version: `0`; size: **70631 bytes**
- SHA256: **`4902544169a4e96b9ef0b9e82d465650b67ea5b57ef8af11d62cf8fb18ca4f50`**

Use the exact Library identity to read/materialize it into the local-only design path. These inputs are intended for cloud retrieval; Mac absolute paths and localhost URLs are not a cross-machine handoff.

## Replay the existing isolated interactive preview

From the repository root, install committed frontend dependencies and run checks:

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend test
pnpm --dir frontend build
```

In one terminal, start the saved fixture (the adjacent VERSION snapshot preserves its original relative-path contract):

```sh
node frontend/src/features/data-assets/handoff/s2-d02/preview/mock-api.cjs
```

In another terminal:

```sh
QF_DEV_BACKEND_URL=http://127.0.0.1:18765 pnpm --dir frontend dev --host 127.0.0.1 --port 5178 --strictPort
```

Open the environment's forwarded local port for `/admin/data-assets`. The fixture accepts any synthetic token; use a dummy value, never a production credential. It binds only loopback, supplies only isolated data-assets/auth/version responses and never calls a supplier or backend. Saved scenario controls are `/__scenario?name=normal|empty|error|slow|issues-error|forbidden|changed` on port 18765. Their presence is not evidence that all scenarios were exercised. The Mac preview processes were stopped for handoff.

Images saved under `screenshots/`: `catalog-1440.jpg`, `overview-1440.jpg`, `preview-1440.jpg`, `catalog-1024.jpg`. No 390px screenshot exists at this checkpoint.
