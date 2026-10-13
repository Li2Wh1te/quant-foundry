// Fixtures are calculation/contract tests only; the workspace loads real APIs.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const ts = require("typescript");
require.extensions[".ts"] = (module, filename) =>
  module._compile(
    ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
      fileName: filename,
    }).outputText,
    filename,
  );
const {
  numeric,
  numberText,
  signed,
  validParams,
} = require("../src/pages/etf/etfData.ts");
test("missing quote facts never render as zero or fabricate a sign", () => {
  for (const value of [
    null,
    undefined,
    "",
    " ",
    "bad",
    NaN,
    Infinity,
    {},
    false,
  ]) {
    assert.equal(numeric(value), null);
    assert.equal(numberText(value), "—");
    assert.equal(signed(value), "—");
  }
  assert.equal(numberText("0"), "0.000");
  assert.equal(signed("0"), "0.000");
  assert.equal(signed("1"), "+1.000");
  assert.equal(signed("-1"), "-1.000");
});
test("indicator periods reject empty, fractional, invalid and inverted MACD settings", () => {
  assert.equal(validParams("MACD", [12, 26, 9]), true);
  assert.equal(validParams("MA", [5, 10, 20, 60]), true);
  for (const params of [
    [0, 10, 20, 60],
    [-1, 10, 20, 60],
    [251, 10, 20, 60],
    [1.5, 10, 20, 60],
    [NaN, 10, 20, 60],
    [],
  ])
    assert.equal(validParams("MA", params), false);
  assert.equal(validParams("MACD", [26, 12, 9]), false);
  assert.equal(validParams("MACD", [12, 12, 9]), false);
});
