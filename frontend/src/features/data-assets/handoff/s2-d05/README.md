# S2-D05 bounded current-object preview

Development and isolated verification are complete. Real API integration is
pending; no production operation, merge or deployment was performed.

## Inputs and scope

- Independent branch: `codex/s2-d05-safe-preview`.
- Exact D04 parent: `79cbd18c6bebe33d3ab884c28c3c9388ee32e8ab` (PR #154).
  D01 `1e7643a96440602575b31995fbec8c03304aa6ab` and D02
  `9dafeb6eb052796d3b4783785feb104f1de3cf5b` remain original ancestors.
- Fresh main: `4c9e8167d49ef1d6bbc49afc6578208d7a339f97`, already an
  ancestor of D04. No main merge or main-only change was necessary.
- No AGENTS.md or `.agents/skills` exists in this checkout or the checked refs.
- Original ZIP remains in the D02 handoff, SHA256
  `bcd5a001be9c5a08e47d31e19ebe4133735080fdc43d7d567171145fc9afb344`.
  Its manifest and the D05 subpackage manifest were verified. D05 ZIP SHA256:
  `c2a9471d4addf6fcebcc0e0b350d6adb1cb1c221638f3bfbfa6a74f147024b28`.
  `requirements/` contains all nine original D05 files, unchanged.

The D05 delta changes only PreviewPanel, its local CSS/test and this handoff.
D01 data/API, D02 shell/components/routes, D03 CatalogView, D04 DatasetView,
FieldTable and styles, D06, backend/R01, dependencies and security are unchanged.
The draft PR is stacked on D04 so its diff contains only this D05 work.

## Delivered behavior

The existing header/tab action reads one bounded example with the real
`preview_key.representation`. Default requests select up to eight declared
columns, including known node identity, and 20 rows; controls enforce 32 columns
and 100 rows. Internal confirmation columns are omitted by default. Undeclared
business meanings receive a technical-preview label. Missing keys or columns
never become invented subjects or an unbounded query.

Subject/range keys remain text because the descriptor does not declare their
date semantics. Representation/schema stay read-only. Normal requests always
use `allow_partial=false`; no diagnostic partial-read mode is added.

D01 owns HTTP validation, cancellation and cursor sessions. D05 clears rows and
page stacks on condition/schema/object changes, generation/quality boundaries,
denial, maintenance and DATA_CHANGED. Late responses cannot populate a new
object. A network failure/timeout may retain the previous page with its original
read time, an explicit stale label and disabled pagination; its cursors are
discarded. GET refreshes, including permission recovery/remount, do not POST.
Only a consumed navigation marker survives in module memory, without any data.

Exact decimal, large integer and ns strings stay text; null is “—” while false
and 0 keep their values. Pages replace each other and never form a complete
report. Actual ranges are labelled “本页范围”; unsatisfied/unverified coverage,
report fragments, empty pages, query misses and descriptor emptiness are distinct.

## Verification

- `pnpm --dir frontend test`: **109 passed**, including nine D05 tests.
- `node --test frontend/src/features/data-assets/data/tests/*.test.cjs`:
  **31 passed**.
- `node node_modules/typescript/bin/tsc -b --force` in frontend: passed.
- `pnpm --dir frontend build`: passed, with the existing Vite bundle-size advisory.
- `node frontend/src/features/data-assets/handoff/s2-d05/preview/acceptance.cjs`:
  **48 passed**, zero page errors; only GET and bounded POST `/query`.
- Script syntax checks, implementation whitespace checks, original requirement
  hashes and protected-path comparisons with the exact D04 parent passed.
  The verbatim original TASK.md retains its two Markdown hard-break trailing
  spaces. Staged whitespace checking excludes only that original leaf file:
  `git diff --cached --check -- . ':(exclude)frontend/src/features/data-assets/handoff/s2-d05/requirements/TASK.md'`.

`screenshots/acceptance.json` records scenarios, request bodies, actual aborts,
an intentionally uncancellable transport, and geometry. All HTTP observations
and values are synthetic loopback fixtures. The 49 labelled JPEGs include
1440x900, 1024x768 and 390x844 entry/table/precision/NAV/report views plus loading,
cancellation, stale reads, generation/quality changes and denial. Representative
images were visually inspected. The document has no horizontal overflow;
tables scroll locally, main owns vertical scrolling, prose is 14px and keyboard
focus is visible.

## Interactive loopback preview

Use the committed lockfile and existing browser tooling:

```sh
pnpm --dir frontend install --frozen-lockfile
node frontend/src/features/data-assets/handoff/s2-d05/preview/mock-api.cjs
QF_DEV_BACKEND_URL=http://127.0.0.1:18767 pnpm --dir frontend dev --host 127.0.0.1 --port 5186 --strictPort
```

Open `http://127.0.0.1:5186/admin/data-assets/fixture.daily?view=preview`
(also `.nav` and `.report`). The fixture accepts the synthetic login value
`s2-d05-isolated-fixture` through the existing auth flow. Use it only on loopback.
Select a scenario at `http://127.0.0.1:18767/__scenario?name=query-changed`;
the fixture prints supported names. Set `CHROMIUM_PATH`/`PLAYWRIGHT_MODULE` if
the environment's existing executables/modules differ. No browser dependency
was added to the application.

## Remaining seams

- No authorized real development endpoint/session was supplied or exercised.
  The actual backend still calls `require_ready` for `/query`. Real descriptor,
  permission, pagination and R01 domain-read integration await its real contract
  and access. Fixtures establish neither production permission nor R01 completion.
- D05 writes no token, cursor, subject, business value or result to URL/storage.
  Existing `auth/tokenStorage.ts` still stores the application's login token in
  sessionStorage. A whole-application prohibition on token persistence requires
  the auth owner to change that existing behavior; this task does not alter auth
  or security. Browser fixtures use only the existing synthetic login flow.
- D07 should integrate the D05 delta after the exact D01/D02/D04 dependencies.
  D02/D04's earlier browser scripts assumed preview navigation never POSTed;
  their assertions need to account for the new single-action preview behavior.
  Their files were not changed. Missing date-key semantics, units and business
  descriptions remain a D01/API seam; no view-specific business mapping was added.
