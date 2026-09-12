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
  buildSeries,
  numeric,
  numberText,
  signed,
  validParams,
} = require("../src/pages/etf/etfData.ts");
const daily = (date, price = 10, extra = {}) => ({
  ts_code: "TEST.SH",
  trade_date: date,
  open: String(price),
  high: String(price + 2),
  low: String(price - 1),
  close: String(price + 1),
  vol: "100",
  amount: "1000",
  source: "fixture",
  updated_at: "2026-09-01T00:00:00Z",
  ...extra,
});
const factor = (date, value) => ({
  ts_code: "TEST.SH",
  trade_date: date,
  adj_factor: String(value),
  source: "fixture",
  updated_at: "2026-09-01T00:00:00Z",
});

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
test("daily series sorts without mutating API records and retains real volume units", () => {
  const source = [daily("2026-08-04", 20), daily("2026-08-03", 10)];
  const copy = structuredClone(source);
  const result = buildSeries(source, [], "raw", "day");
  assert.equal(result.error, "");
  assert.deepEqual(source, copy);
  assert.deepEqual(
    result.bars.map((x) => x.date),
    ["2026-08-03", "2026-08-04"],
  );
  assert.equal(result.bars[0].volume, 100);
  assert.equal(result.bars[0].turnover, 1000);
});
test("weekly and monthly aggregation uses first open, extrema, last close and summed original volume", () => {
  const rows = [
    daily("2026-07-31", 10),
    daily("2026-08-03", 20),
    daily("2026-08-04", 30),
  ];
  for (const period of ["week", "month"]) {
    const { bars } = buildSeries(rows, [], "raw", period);
    assert.equal(bars.length, 2);
    assert.deepEqual(bars[1], {
      timestamp: Date.parse("2026-08-04T00:00:00Z"),
      date: "2026-08-04",
      open: 20,
      high: 32,
      low: 19,
      close: 31,
      volume: 200,
      turnover: 2000,
    });
  }
  const { bars } = buildSeries(rows, [], "raw", "year");
  assert.equal(bars.length, 1);
  assert.equal(bars[0].open, 10);
  assert.equal(bars[0].close, 31);
});
test("replay excludes future OHLC, future factor anchors and future partial-period records", () => {
  const rows = [
    daily("2026-08-03", 10),
    daily("2026-08-04", 20),
    daily("2026-08-05", 100),
  ];
  const factors = [
    factor("2026-08-03", 1),
    factor("2026-08-04", 2),
    factor("2026-08-05", 10),
  ];
  const replay = buildSeries(rows, factors, "forward", "week", "2026-08-04");
  assert.equal(replay.bars.length, 1);
  assert.equal(replay.bars[0].open, 5);
  assert.equal(replay.bars[0].close, 21);
  assert.equal(replay.bars[0].high, 22);
  assert.equal(replay.bars[0].volume, 200);
  assert.deepEqual(
    replay,
    buildSeries(rows.slice(0, 2), factors.slice(0, 2), "forward", "week"),
  );
});
test("forward/backward factors adjust price only, without changing underlying records", () => {
  const rows = [daily("2026-08-03"), daily("2026-08-04")],
    factors = [factor("2026-08-03", 1), factor("2026-08-04", 2)];
  const forward = buildSeries(rows, factors, "forward", "day").bars,
    backward = buildSeries(rows, factors, "backward", "day").bars;
  assert.equal(forward[0].close, 5.5);
  assert.equal(forward[1].close, 11);
  assert.equal(backward[0].close, 11);
  assert.equal(backward[1].close, 22);
  assert.equal(backward[1].volume, 100);
  assert.equal(rows[0].close, "11");
});
test("incomplete factors block adjusted chart but raw chart remains usable", () => {
  const rows = [daily("2026-08-03"), daily("2026-08-04")];
  for (const value of [0, -1, "bad"]) {
    const result = buildSeries(
      rows,
      [factor("2026-08-03", 1), factor("2026-08-04", value)],
      "forward",
      "day",
    );
    assert.equal(result.bars.length, 0);
    assert.match(result.error, /2026-08-04/);
  }
  assert.equal(buildSeries(rows, [], "raw", "day").bars.length, 2);
  assert.equal(
    buildSeries(rows, [factor("2026-08-03", 1)], "forward", "day", "2026-08-03")
      .error,
    "",
  );
});
test("invalid prices block instead of silently skipping a trading day and bridging MA", () => {
  for (const extra of [
    { open: null },
    { close: "" },
    { high: "3" },
    { low: "15" },
    { close: "0" },
    { high: "Infinity" },
  ]) {
    const result = buildSeries(
      [
        daily("2026-08-03"),
        daily("2026-08-04", 10, extra),
        daily("2026-08-05"),
      ],
      [],
      "raw",
      "day",
    );
    assert.equal(result.bars.length, 0);
    assert.match(result.error, /2026-08-04/);
  }
});
test("missing volume propagates across aggregates without pretending to be zero", () => {
  const result = buildSeries(
    [daily("2026-08-03"), daily("2026-08-04", 10, { vol: null, amount: null })],
    [],
    "raw",
    "week",
  );
  assert.equal(result.volumeMissing, true);
  assert.equal(result.error, "");
  assert.equal(result.bars[0].volume, undefined);
  assert.equal(result.bars[0].turnover, undefined);
  const zero = buildSeries(
    [daily("2026-08-03", 10, { vol: "0", amount: "0" })],
    [],
    "raw",
    "day",
  );
  assert.equal(zero.volumeMissing, false);
  assert.equal(zero.bars[0].volume, 0);
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
