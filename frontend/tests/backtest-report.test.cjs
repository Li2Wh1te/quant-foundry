/* Exercise the actual rendered report with the API's object-point contract.
 * Transpile the existing TypeScript in memory; no test-only browser or package
 * dependency is needed for these pure rendering/contract regressions. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const ts = require("typescript");
for (const extension of [".ts", ".tsx"]) {
  require.extensions[extension] = (module, filename) => {
    const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
      fileName: filename,
    });
    module._compile(compiled.outputText, filename);
  };
}
const { createElement } = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { EvidenceTable } = require("../src/components/EvidenceTable.tsx");
const render = (component, props) => renderToStaticMarkup(createElement(component, props));

test("evidence preserves zero and false values and escapes untrusted markup", () => {
  const html = render(EvidenceTable, { title: "证据", value: { nested: { zero: 0, flag: false, unsafe: "<script>bad</script>" }, absent: null } });
  assert.match(html, /nested \/ zero/); assert.match(html, />0</); assert.match(html, />false</);
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/); assert.match(html, /未记录/);
});

test("creation requires completed current server gates, including degraded confirmation", () => {
  const { admissionAllowsCreation } = require("../src/components/backtestAdmission.ts");
  assert.equal(admissionAllowsCreation({ status: "degraded", report_hash: "data-only" }), false);
  assert.equal(admissionAllowsCreation({ status: "ready", report_hash: "hash", gates: { allowed: false } }), false);
  assert.equal(admissionAllowsCreation({ status: "blocked", report_hash: "hash", gates: { allowed: true } }), false);
  assert.equal(admissionAllowsCreation({ status: "ready", gates: { allowed: true } }), false);
  assert.equal(admissionAllowsCreation({ status: "degraded", report_hash: "confirmed", gates: { allowed: true } }), true);
  assert.equal(admissionAllowsCreation({ status: "ready", report_hash: "hash", gates: { allowed: true } }), true);
});

test("strategy workspace retains run filters and cursor", async () => {
  const { fetchStrategyBacktestWorkspace } = require("../src/api/backtestRuns.ts");
  const requests = [];
  const original = global.fetch;
  global.fetch = async (path) => { requests.push(new URL(path, "http://localhost")); return { ok: true, json: async () => ({ items: [] }) }; };
  const filters = { strategy_revision_id: "revision", status: "succeeded", created_after: "2026-01-01T00:00:00+08:00", created_before: "2026-02-01T00:00:00Z", config_summary: "费用 10% & 参数" };
  try {
    await fetchStrategyBacktestWorkspace("strategy", undefined, "signed+cursor", filters);
    for (const request of requests) for (const [key, value] of Object.entries(filters)) assert.equal(request.searchParams.get(key), value);
    assert.equal(requests[0].searchParams.get("cursor"), "signed+cursor");
  } finally { global.fetch = original; }
});
