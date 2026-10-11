/* Replay D02 navigation/cancellation/permission checks without editing its files.
 * D04 clarified overview text; D06 split update sections and clarified issue errors.
 * Adapt only those five text selectors. No behavioral assertion is removed. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const original = path.resolve(__dirname, '../../s2-d02/preview/acceptance.cjs');
let source = fs.readFileSync(original, 'utf8');
const changes = [
  ["assert.ok((await p.locator('.qf-assets-metrics strong').allTextContents()).includes('状态待确认'));",
    "assert.equal(await p.locator('.qf-assets-heading-status').innerText(), '状态待确认');"],
  ["await p.getByText('当前数据集已登记，尚无正式记录。', { exact: true }).waitFor();",
    "await p.getByText('当前数据集已登记，当前物理存储为空；不说明来源或市场上没有这种数据。', { exact: true }).waitFor();"],
  ["await p.getByText('本地来源尚未检查，不能将未检查视为空。', { exact: true }).waitFor();",
    "await p.getByText('本地来源尚未检查，不能将尚未检查视为空。', { exact: true }).waitFor();"],
  ["name: '最近更新与能力限制'", "name: '最近更新结果'"],
  ["hasText: '问题详情读取失败'", "hasText: '问题读取失败'"]
];
for (const [before, after] of changes) {
  assert.equal(source.split(before).length - 1, 1, 'Review the D02 seam if its source changes');
  source = source.replace(before, after);
}
process.env.FRONTEND_URL ||= 'http://127.0.0.1:5187';
process.env.SCREENSHOT_DIR ||= '/tmp/qf-d06-d02-regression';
process.env.CHROMIUM_PATH ||= '/usr/bin/chromium';
const replay = new Module(original, module);
replay.filename = original;
replay.paths = Module._nodeModulePaths(path.dirname(original));
replay._compile(source, original);
