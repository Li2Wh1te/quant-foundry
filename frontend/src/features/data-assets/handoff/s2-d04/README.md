# S2-D04 dataset overview and field metadata

Development and isolated verification are complete. Real API integration is
not complete, and no production operation, main merge or deployment was run.

## Inputs and scope

- Main was fetched before implementation and again before submission:
  `4c9e8167d49ef1d6bbc49afc6578208d7a339f97`.
- Branch: `codex/s2-d04-dataset-details`, in its own cloud worktree.
- Exact D01 dependency: `1e7643a96440602575b31995fbec8c03304aa6ab`.
- Exact D02 dependency: `9dafeb6eb052796d3b4783785feb104f1de3cf5b`.
- Both original commits and their ancestors are preserved by merges; the
  combined pre-D04 tree is `f2de30b1934f88690317c398d3b3c9b1f9f7d9a5`.
- The original ZIP is already in the D02 handoff directory, SHA256
  `bcd5a001be9c5a08e47d31e19ebe4133735080fdc43d7d567171145fc9afb344`.
  Root and D04 manifests were verified. `requirements/` contains the nine
  original D04 leaf files, copied byte-for-byte with their manifest.
- This cloud checkout has no AGENTS.md or `.agents/skills`. The D01/D02
  recorded rules were retained: isolated worktree, English commits/comments,
  existing shell/fonts, honest missing facts, no root `/docs/`, fresh main.

The D04 delta only changes DatasetView, FieldTable, their local CSS/test and
this handoff. D01 data/client code, CatalogView, the shared D02 shell/routes,
D05/D06 panels, backend, R01, operations, security and dependencies are unchanged.

## Delivered behavior

The overview preserves the descriptor name/source qualifiers and prioritizes
purpose, frequency and limitations. The current DTO has no independent purpose,
business cutoff or unit contract; those facts remain explicitly undeclared.
Current status and last update outcome stay separate. Counts are physical
storage rows; updated_at is a catalog/record update time. Schema, rule,
generation, source representation, business keys and storage partitions are
collapsed diagnostics with copy controls. Missing partition endpoints and
truncated lists do not imply continuous business coverage.

FieldTable separates declared meaning from technical type and calculation
limits. Model paths/identifier tokens do not become business definitions;
their original text remains in a disclosure. Decimal text keeps its technical
contract and never gains direct calculation ability. Search and 20-field
pagination only filter supplied metadata and perform no reads. Same-object
refresh clamps a reduced field list; an object change resets local controls.

D02 remains responsible for dataset-keyed fetching/cancellation and permission
invalidation. Its preview action navigates to D05; update/issues remain D06.

## Verification actually executed

- `pnpm --dir frontend test`: **100 passed**, including nine D04 checks.
- `node --test frontend/src/features/data-assets/data/tests/*.test.cjs`:
  **31 passed**.
- `node node_modules/typescript/bin/tsc -b --force` from frontend: passed.
- `pnpm --dir frontend build`: passed; existing Vite large-bundle advisory.
- D04 `preview/acceptance.cjs`: **24 passed**, zero page errors, only GETs.
  It covers daily/NAV/report descriptors at 1440x900, 1024x768 and 390x844;
  metadata filtering/paging, no matches, repeated Tabs/copy, clipboard failure,
  missing fields/limits/preview key, state distinctions, loading, late/cancelled
  object requests, local issues/refresh errors, malformed/missing DTOs, 403/401.
- D02 scenario replay via D04 `preview/shell-regression.cjs`: **27 passed**,
  zero page errors. The original D02 script stopped at its old metric-band
  status assertion; the D04 wrapper changes exactly that selector and two
  empty/unchecked text assertions, retaining every original scenario. No D02
  file or evidence was edited. D07 should adopt those presentation assertions.
- Script syntax checks, `git diff --check`, and byte-for-byte protected-path
  comparison against the combined dependency tree passed.

`screenshots/acceptance.json` records D04 cases, methods and geometry;
`screenshots/shell-regression.json` records the adapted D02 replay. The 39 D04
JPEGs are visibly labelled isolated fixtures. Overview, field/technical views
at all three widths and failure/loading/denial images were visually inspected.
There is no document horizontal overflow; tables scroll horizontally, while
the existing main region owns vertical scrolling. Body explanations are 14px.

## Interactive loopback preview

Install using the committed lockfile, then start these in separate terminals:

```sh
pnpm --dir frontend install --frozen-lockfile
node frontend/src/features/data-assets/handoff/s2-d04/preview/mock-api.cjs
QF_DEV_BACKEND_URL=http://127.0.0.1:18766 pnpm --dir frontend dev --host 127.0.0.1 --port 5184 --strictPort
```

Open `/admin/data-assets/fixture.daily`, `.nav` or `.report` on that local
frontend. The fixture accepts the synthetic login value
`s2-d04-isolated-fixture`; use it only in this loopback preview. Scenarios are
selected at `http://127.0.0.1:18766/__scenario?name=restricted` (the fixture
prints all choices). Automated preview:

```sh
CHROMIUM_PATH=/usr/bin/chromium node frontend/src/features/data-assets/handoff/s2-d04/preview/acceptance.cjs
```

Use the environment's existing Playwright module, or set PLAYWRIGHT_MODULE;
no browser dependency was added to the application. If pnpm's default cache
is unwritable, set XDG_DATA_HOME/XDG_CACHE_HOME under `/tmp` and place
`--config.store-dir=/tmp/qf-d04-pnpm-store` **before** the script name.

The optional shell replay uses the unchanged D02 mock API on 18765 and another
Vite process on 5185 configured with that backend; its images stay under `/tmp`.

## Remaining seams

- No authorized real development endpoint/session was supplied or exercised.
  All HTTP observations are loopback fixtures. Real descriptor and `/query`
  integration awaits actual access/readiness; fixtures prove neither production
  readability nor R01 completeness.
- D01/API: purpose, confirmed dataset display aliases, business cutoff, units,
  price basis and limitation/field business descriptions are not supplied by
  the current DTO/presentation exports. Raw limitation identifiers are retained
  in diagnostics with an explicit missing-interpretation note. Add confirmed
  shared explanations at the D01 seam rather than a per-view dataset mapping.
- D05/D06: keep the stable DatasetView/FieldTable props; no new API/export or
  route is required. Actual preview and detailed update/issues behavior remain
  their owners' work.
- D07: integrate the D04-only commit after D01/D02, synchronize the three
  presentation assertions documented above, and perform real API acceptance.
