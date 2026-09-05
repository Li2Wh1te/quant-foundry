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
const { BacktestCurve, MetricMatrix, BacktestComparisonView } = require("../src/components/BacktestReport.tsx");
const render = (component, props) => renderToStaticMarkup(createElement(component, props));

test("object points draw multiple dated lines without joining missing valuations", () => {
  const html = render(BacktestCurve, { title: "权益", field: "equity", series: [
    { run_id: "run-a", points: [{ as_of: "2026-01-01", equity: "100" }, { as_of: "2026-01-02", equity: null }, { as_of: "2026-01-03", equity: "90" }] },
    { run_id: "run-b", points: [{ as_of: "2026-01-02", equity: "80" }, { as_of: "2026-01-03", equity: "100" }] },
  ] });
  const paths = [...html.matchAll(/<path d="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(paths.length, 2);
  assert.equal((paths[0].match(/M/g) || []).length, 2);
  assert.equal((paths[0].match(/L/g) || []).length, 0);
  assert.match(paths[1], /^M420,/); // Jan 2 is the shared axis midpoint.
  assert.match(paths[1], /L760,/);
  assert.match(html, /run-a/);
  assert.match(html, /2026-01-03/);
});

test("missing metrics and mismatched Sharpe conventions are visible side by side", () => {
  const html = render(MetricMatrix, { ids: ["a", "b"], metrics: [
    { run_id: "a", metric_key: "sharpe", formula_version: "simple_v1", value: "1.2" },
    { run_id: "b", metric_key: "sharpe", formula_version: "config_v1", value: null, unavailable_reason: "利率缺失" },
  ] });
  assert.match(html, /口径或产出不同/);
  assert.match(html, /不可计算：利率缺失/);
  assert.match(html, /<th>a<\/th><th>b<\/th>/);
});

test("comparison never treats two missing session reports as proof of comparability", () => {
  const html = render(BacktestComparisonView, { result: {
    run_summaries: [{ run_id: "a", data_evidence: {} }, { run_id: "b", data_evidence: {} }],
    metric_matrix: [], configuration_diff: [{ run_id: "b", fields: {} }],
  } });
  assert.match(html, /缺少会话内最终预检证据/);
  assert.match(html, /缺失证据仍需单独确认/);
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

test("metric comparison retains top-level annualization and rate evidence", () => {
  const html = render(MetricMatrix, { ids: ["a", "b"], metrics: [
    { run_id: "a", metric_key: "sharpe", value: "1", annualization_factor: "252", risk_free_rate_note: "published A" },
    { run_id: "b", metric_key: "sharpe", value: "1", annualization_factor: "365", risk_free_rate_note: "published B" },
  ] });
  assert.match(html, /口径或产出不同/);
  assert.match(html, /published A/);
  assert.match(html, /365/);
});
